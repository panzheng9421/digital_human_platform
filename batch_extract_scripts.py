# -*- coding: utf-8 -*-
"""
对标账号文案批量提取：读 douyin-author-videos 产出的 CSV（index,url,desc,create_time,duration,digg），
逐条复用本项目的单条提取链路（yt-dlp 下载 -> OSS -> 百炼 Paraformer-v2 转写），
每条视频的文案落一个 .txt，全部结果汇总到 manifest.csv。

用法（在本项目根目录、用项目 venv 跑）:
  venv/Scripts/python.exe batch_extract_scripts.py <csv_path> --out D:/ai/workbuddy/video/scripts_out
常用参数:
  --min-digg N    只提点赞 >= N 的视频（推荐先跑爆款）
  --limit N       最多提取 N 条
  --year 2026     只提某年发布的
  --order digg    排序 digg(默认,爆款优先) | time(按发布时间)
  --delete-video  转写完成后删除本地视频文件（默认保留，与平台行为一致）
断点续跑：输出目录里已有对应 .txt 的视频自动跳过，可随时中断重跑。
环境变量：自动从同目录 start.bat 的 set 行加载（DASHSCOPE_API_KEY / OSS_* / DH_SECRET_KEY 等）。
"""
import argparse
import csv
import datetime
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
    """从 start.bat 的 set KEY=VALUE 行加载环境变量（不覆盖已有值）。"""
    n = 0
    if not os.path.exists(path):
        return 0
    with io.open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            m = re.match(r"^set\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line, re.I)
            if m:
                k, v = m.group(1), m.group(2).strip().strip('"')
                if k not in os.environ:
                    os.environ[k] = v
                    n += 1
    return n


def slugify(title: str, max_len: int = 40) -> str:
    """标题转安全文件名片段（去 emoji/非法字符/话题标签）。"""
    t = re.sub(r"#[^\s#]+", "", title or "")          # 去话题标签
    t = re.sub(r"[\r\n\t]+", " ", t).strip()
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", t)        # Windows 非法字符
    t = re.sub(r"\s+", " ", t).strip(" .-")
    (enc,) = ["utf-8"]
    while len(t.encode(enc)) > max_len * 3 and len(t) > 4:
        t = t[:-1]
    return t or "untitled"


def main():
    ap = argparse.ArgumentParser(description="对标账号文案批量提取")
    ap.add_argument("csv_path")
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "storage", "scripts_out"))
    ap.add_argument("--min-digg", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--order", choices=["digg", "time"], default="digg")
    ap.add_argument("--delete-video", action="store_true")
    args = ap.parse_args()

    n_env = load_env_from_startbat(os.path.join(BASE_DIR, "start.bat"))
    print(f"[env] 从 start.bat 加载 {n_env} 个环境变量")

    from app.services import asr_client as ac
    if not ac.available():
        print("❌ DASHSCOPE_API_KEY 未配置，无法转写")
        sys.exit(1)

    # 读 CSV
    rows = []
    with io.open(args.csv_path, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.datetime.fromtimestamp(int(r["create_time"]))
            except Exception:
                dt = None
            rows.append({
                "url": r["url"],
                "desc": (r.get("desc") or "").strip(),
                "digg": int(r.get("digg") or 0),
                "dt": dt,
                "aweme_id": r["url"].rstrip("/").rsplit("/", 1)[-1],
            })
    if args.year:
        rows = [r for r in rows if r["dt"] and r["dt"].year == args.year]
    rows = [r for r in rows if r["digg"] >= args.min_digg]
    rows.sort(key=lambda r: (-r["digg"], r["dt"] or datetime.datetime.min)
              if args.order == "digg" else (r["dt"] or datetime.datetime.min, -r["digg"]))
    if args.limit:
        rows = rows[: args.limit]

    os.makedirs(args.out, exist_ok=True)
    manifest_path = os.path.join(args.out, "manifest.csv")
    done = set()
    if os.path.exists(manifest_path):
        with io.open(manifest_path, "r", encoding="utf-8-sig") as f:
            for m in csv.DictReader(f):
                if m.get("status") == "ok":
                    done.add(m.get("aweme_id", ""))

    todo = [r for r in rows if r["aweme_id"] not in done]
    print(f"[plan] CSV 共 {len(rows)} 条参与本次筛选，已完成 {len(done & {r['aweme_id'] for r in rows})}，待提取 {len(todo)}")
    print(f"[plan] 输出目录: {args.out}")

    ok = fail = skip = 0
    t0 = time.time()
    for i, r in enumerate(todo, 1):
        fn_base = f"{r['digg']:>07}_{r['aweme_id']}_{slugify(r['desc'])}"
        txt_path = os.path.join(args.out, fn_base + ".txt")
        if os.path.exists(txt_path):  # 已有产物（可能旧命名），也跳过
            skip += 1
            continue
        print(f"\n=== [{i}/{len(todo)}] {r['dt']:%Y-%m-%d} 赞{r['digg']} {r['desc'][:36]} ===")
        try:
            tmp_dir = ac.STORAGE_DIR and os.path.join(BASE_DIR, "storage", "temp")
            os.makedirs(tmp_dir, exist_ok=True)
            path, meta = ac.download_video(r["url"], tmp_dir)
            try:
                text = ac.transcribe_file(path)
            finally:
                if args.delete_video and path and os.path.exists(path):
                    try:
                        os.remove(path)
                        print("[asr] 本地视频已删除")
                    except Exception as e:
                        print(f"[asr] 删除本地视频失败(忽略): {e}")
            chars = len(re.sub(r"\s+", "", text or ""))
            with io.open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"# {r['desc']}\n# {r['url']}\n# 发布: {r['dt']:%Y-%m-%d} | 点赞: {r['digg']} | 字数: {chars}\n\n")
                f.write((text or "").strip() + "\n")
            ok += 1
            status, note = "ok", ""
            print(f"✅ 提取成功 {chars} 字 -> {os.path.basename(txt_path)}")
        except Exception as e:
            fail += 1
            status, note = "fail", str(e)[:200]
            print(f"❌ 失败: {note}")
            traceback.print_exc(limit=2)
        # 追加写 manifest（断点续跑依据）
        new_file = not os.path.exists(manifest_path)
        with io.open(manifest_path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["aweme_id", "date", "digg", "title", "url", "status", "note", "file"])
            w.writerow([r["aweme_id"], f"{r['dt']:%Y-%m-%d}" if r["dt"] else "",
                        r["digg"], r["desc"][:80], r["url"], status, note,
                        os.path.basename(txt_path) if status == "ok" else ""])
        elapsed = time.time() - t0
        done_n = ok + fail
        if done_n:
            eta = elapsed / done_n * (len(todo) - i)
            print(f"[进度] {done_n}/{len(todo)} 成功{ok} 失败{fail} 已耗时{elapsed/60:.0f}m 预计还需{eta/60:.0f}m")

    print(f"\n[done] 本轮：成功 {ok}，失败 {fail}，跳过 {skip}。manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
