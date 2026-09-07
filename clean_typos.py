# -*- coding: utf-8 -*-
"""
批量清洗 ASR 转写错别字：读 scripts_samuel/*.txt（批量提取产物），
逐条调用 DeepSeek 做「只纠错不改写」的校对，清洗稿输出到 clean 目录。
原始文件不动；clean 目录已有同名文件的自动跳过（断点续跑）。

用法（本项目根目录，venv 跑）:
  venv/Scripts/python.exe clean_typos.py --src D:/ai/workbuddy/video/scripts_samuel
产物:
  <src>_clean/ 下同名 .txt（头部保留原元信息，正文为清洗稿）
  <src>_clean/clean_manifest.csv（每条清洗状态/字数变化记账）
"""
import argparse
import concurrent.futures as cf
import csv
import io
import os
import re
import sys
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)


def load_env_from_startbat(path: str) -> int:
    n = 0
    if not os.path.exists(path):
        return 0
    with io.open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = re.match(r"^set\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip(), re.I)
            if m:
                k, v = m.group(1), m.group(2).strip().strip('"')
                if k not in os.environ:
                    os.environ[k] = v
                    n += 1
    return n


SYSTEM = (
    "你是语音转写(ASR)文本校对助手。输入是一段从抖音口播视频转写出来的中文文案，"
    "其中存在同音字错误、英文术语被转成谐音汉字、夹杂乱码等问题。\n"
    "你的任务：只纠正错误，不改写内容。\n"
    "规则：\n"
    "1) 纠正同音字/近音字错别字（如『在』/『再』误用）。\n"
    "2) 还原被转成谐音汉字的英文术语：如『皮T』『pd』『皮P』→PPT，"
    "『欧喷』→Open，『赛』『ساي』→按上下文还原为正确术语（如 Source/Agent/Claude 等），"
    "『扣子』是产品名保留，『拓界』类疑似音译按上下文判断。\n"
    "3) 删除混入的乱码字符（西里尔字母、希腊字母、无意义符号），但不要删正常文字。\n"
    "4) 明显的专有名词修正：产品名/公司名/人名按通用写法（如『豆包』『Kimi』『TRAE』『Gemini』）。\n"
    "5) 严禁：改变语序、增删句子、润色表达、总结缩写。输出必须与输入逐句对应、字数接近。\n"
    "6) 口语中的语气词（啊、呢、哈）保留。\n"
    "7) 直接输出校对后的正文纯文本，不要任何解释、不要 markdown 围栏、不要标题。"
)


def call_deepseek(text: str) -> str:
    import requests
    api_key = os.environ.get("LLM_API_KEY", "")
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    if not api_key:
        raise RuntimeError("LLM_API_KEY 未配置")
    r = requests.post(base + "/chat/completions",
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                      json={"model": model,
                            "messages": [{"role": "system", "content": SYSTEM},
                                         {"role": "user", "content": "待校对的转写文案：\n" + text}],
                            "temperature": 0.1, "max_tokens": 8192},
                      timeout=180)
    r.raise_for_status()
    out = r.json()["choices"][0]["message"]["content"].strip()
    if out.startswith("```"):  # 防围栏
        out = re.sub(r"^```[a-z]*\n?", "", out).rstrip("`").strip()
    return out


def parse_txt(path: str):
    """拆头部元信息行(# 开头)与正文。"""
    header, body = [], []
    with io.open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    seen_body = False
    for ln in lines:
        if not seen_body and ln.startswith("#"):
            header.append(ln)
        else:
            if ln.strip() or seen_body:
                seen_body = True
                body.append(ln)
    return "\n".join(header), "\n".join(body).strip()


def clean_one(src_txt: str, dst_txt: str) -> dict:
    header, body = parse_txt(src_txt)
    if not body:
        return {"file": os.path.basename(src_txt), "status": "skip_empty", "note": "正文为空"}
    cleaned = call_deepseek(body)
    with io.open(dst_txt, "w", encoding="utf-8") as f:
        f.write(header.replace("字数:", "原字数:") + f" | 已清洗错别字\n\n{cleaned}\n")
    w_in = len(re.sub(r"\s+", "", body))
    w_out = len(re.sub(r"\s+", "", cleaned))
    ratio = w_out / w_in if w_in else 0
    # 字数偏差过大视为可疑（LLM 可能擅自缩写），标记出来人工复核
    status = "ok" if 0.85 <= ratio <= 1.10 else "review"
    note = f"字数 {w_in}->{w_out}"
    return {"file": os.path.basename(src_txt), "status": status, "note": note}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="批量提取输出目录（含 *.txt 与 manifest.csv）")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    n_env = load_env_from_startbat(os.path.join(BASE_DIR, "start.bat"))
    print(f"[env] 从 start.bat 加载 {n_env} 个环境变量")

    src_dir = args.src.rstrip("/\\")
    dst_dir = src_dir + "_clean"
    os.makedirs(dst_dir, exist_ok=True)
    manifest_path = os.path.join(dst_dir, "clean_manifest.csv")

    files = sorted(f for f in os.listdir(src_dir) if f.endswith(".txt"))
    todo = []
    for f in files:
        if os.path.exists(os.path.join(dst_dir, f)):
            continue  # 断点续跑
        todo.append(f)
    print(f"[plan] 共 {len(files)} 条，已完成 {len(files)-len(todo)}，待清洗 {len(todo)}，输出 -> {dst_dir}")

    done_cnt = ok_cnt = rev_cnt = fail_cnt = 0
    t0 = time.time()

    def work(fn):
        try:
            return clean_one(os.path.join(src_dir, fn), os.path.join(dst_dir, fn))
        except Exception as e:
            traceback.print_exc(limit=1)
            return {"file": fn, "status": "fail", "note": str(e)[:150]}

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, fn): fn for fn in todo}
        for fut in cf.as_completed(futs):
            res = fut.result()
            done_cnt += 1
            with io.open(manifest_path, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if f.tell() == 0:
                    w.writerow(["file", "status", "note"])
                w.writerow([res["file"], res["status"], res["note"]])
            ok_cnt += res["status"] == "ok"
            rev_cnt += res["status"] == "review"
            fail_cnt += res["status"] == "fail"
            print(f"[{done_cnt}/{len(todo)}] {res['status']:>6} {res['note']}  {res['file'][:50]}")

    print(f"\n[done] 成功 {ok_cnt}，需人工复核 {rev_cnt}，失败 {fail_cnt}。clean 目录: {dst_dir}")


if __name__ == "__main__":
    main()
