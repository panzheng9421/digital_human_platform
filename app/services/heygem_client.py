"""HeyGem / duix.avatar 真实调用客户端（对接 PAI-EAS 部署，OSS 中转版）。

部署在 EAS 上的是 guiji2025/duix.avatar 镜像，容器内接口为：
  - POST /easy/submit   body={audio_url, video_url, code, chaofen, watermark_switch, pn}
  - GET  /easy/query?code={uuid}
EAS 会把完整路径（含 /api/predict/eas001）透传给容器；容器入口 serve_oss.py 已做
catch-all 兼容，因此这里统一用 HEYGEM_PATH_PREFIX（默认 easy）拼路径即可。

文件互通（OSS 中转）：
  - audio_url / video_url 必须是 EAS 容器能拉取的 URL —— 由调用方先上传到 OSS 得到。
  - 生成结果 result 由 serve_oss.py 改写为 OSS 可访问 URL（也可能是容器内路径，调用方需再处理）。

重要部署约束（详见部署说明）：
  1. EAS 空闲自动缩容为 0，首调会 503 冷启动，客户端已内置重试。
  2. 返回 result 若为 OSS URL 可直接下载；若为本地路径则说明容器未回传 OSS（需检查 serve_oss 部署）。
"""
import urllib.request
import urllib.error
import json
import time
import uuid

from app.config import (HEYGEM_ENDPOINT, HEYGEM_TOKEN, HEYGEM_PATH_PREFIX,
                         AVATAR_PROVIDER)


def _headers():
    h = {"Content-Type": "application/json"}
    if HEYGEM_TOKEN:
        h["Authorization"] = HEYGEM_TOKEN
    return h


def _url(path):
    base = HEYGEM_ENDPOINT.rstrip("/")
    prefix = (HEYGEM_PATH_PREFIX or "").strip("/")
    if prefix:
        return f"{base}/{prefix}/{path.lstrip('/')}"
    return f"{base}/{path.lstrip('/')}"


def submit_video(audio_url, video_url, timeout=60):
    """提交对口型任务，返回 (code, resp_json)。code 为本端生成的任务 UUID。"""
    code = str(uuid.uuid4())
    payload = {
        "audio_url": audio_url,
        "video_url": video_url,
        "code": code,
        "chaofen": 0,
        "watermark_switch": 0,
        "pn": 1,
    }
    url = _url("/submit")
    print(f"[HeyGem] submit -> {url} body={payload}")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
            print(f"[HeyGem] submit resp <- {resp}")
            return code, resp
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        print(f"[HeyGem] submit HTTP error {e.code}: {body}")
        return code, {"code": e.code, "msg": body}
    except Exception as e:  # 含 EAS 503 冷启动
        print(f"[HeyGem] submit exception: {type(e).__name__}: {str(e)[:300]}")
        return code, {"code": -1, "msg": f"{type(e).__name__}: {str(e)[:300]}"}


def query_video(code, timeout=30):
    """查询任务状态，返回 resp_json。"""
    url = _url(f"/query?code={code}")
    print(f"[HeyGem] query  -> {url}")
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
            print(f"[HeyGem] query  resp <- {resp}")
            return resp
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        print(f"[HeyGem] query HTTP error {e.code}: {body}")
        return {"code": e.code, "msg": body}
    except Exception as e:
        print(f"[HeyGem] query exception: {type(e).__name__}: {str(e)[:200]}")
        return {"code": -1, "msg": f"{type(e).__name__}: {str(e)[:200]}"}


def _status_result(res):
    """从返回里抽取 (status, result)，兼容 data 嵌套或顶层两种结构。"""
    if not isinstance(res, dict):
        return None, None
    d = res.get("data")
    if isinstance(d, dict) and ("status" in d or "result" in d):
        return d.get("status"), d.get("result")
    if "status" in res or "result" in res:
        return res.get("status"), res.get("result")
    return None, None


def _looks_like_local_path(s) -> bool:
    """判断 result 是否仍是容器内本地路径（未回传 OSS）。"""
    return isinstance(s, str) and (s.startswith("/") or (len(s) > 1 and s[1] == ":"))


def generate_talking_video(audio_url, video_url,
                           timeout=900, interval=5,
                           cold_retries=12, cold_wait=10):
    """提交并轮询直到完成，返回 {video_url, progress} 或抛异常。

    cold_retries: 提交时遇 EAS 503 冷启动的重试次数。
    audio_url / video_url: 必须为 EAS 容器可访问的 URL（OSS 上传得到）。
    """
    code, sub = submit_video(audio_url, video_url)
    # 冷启动：503 / -1 时重试提交
    attempts = 0
    while sub.get("code") in (-1, 503) and attempts < cold_retries:
        time.sleep(cold_wait)
        code, sub = submit_video(audio_url, video_url)
        attempts += 1

    # 提交成功判定：API 返回 10000，或回显了我们下发的任务 code（视为已受理）
    sc = sub.get("code")
    if sc in (10000, code):
        pass  # 受理成功
    elif sc in (-1, 503):
        raise RuntimeError(f"HeyGem 提交持续冷启动失败（503）: {sub}")
    else:
        raise RuntimeError(f"HeyGem 提交失败: {sub}")

    # 冷启动竞态防护：submit 可能落到即将被回收/替换的容器，服务端虽回"成功"
    # 但实际没登记任务（后续 query 会返回 10004 任务不存在）。立即 query 一次确认，
    # 若丢失则整段重提交（换新 code），最多重试 cold_retries 次。
    if query_video(code).get("code") == 10004:
        for _ in range(cold_retries):
            time.sleep(cold_wait)
            code, sub = submit_video(audio_url, video_url)
            sc = sub.get("code")
            if sc in (10000, code) and query_video(code).get("code") != 10004:
                break
        else:
            raise RuntimeError(f"HeyGem 提交后任务不存在（疑似容器被替换），已重试仍失败: {sub}")

    waited = 0
    while waited < timeout:
        res = query_video(code)
        c = res.get("code")
        if c in (9999, 10002, 10003, 10004):
            raise RuntimeError(f"HeyGem 任务不存在/失败: {res}")
        if c in (-1, 503):
            # 查询也遇冷启动，继续等
            time.sleep(interval)
            waited += interval
            continue
        st, result = _status_result(res)
        if st is None and result is None and c not in (10000,):
            # 结构异常，但非明确失败，再等等
            time.sleep(interval)
            waited += interval
            continue
        if st == 2:
            if _looks_like_local_path(result):
                raise RuntimeError(
                    f"EAS 已生成视频但未回传 OSS，返回的是容器内路径: {result!r}。"
                    f"请检查：1) EAS 部署的入口是否为 serve_oss.py；2) OSS 环境变量是否配置正确；"
                    f"3) 容器内是否能访问 OSS。完整响应: {res}"
                )
            return {"video_url": result, "progress": res.get("progress")}
        if st == 3:
            raise RuntimeError(f"HeyGem 生成失败: {res.get('msg') or res}")
        # st == 1 或 None：处理中，继续轮询
        time.sleep(interval)
        waited += interval
    raise RuntimeError("HeyGem 生成超时（可能 EAS 冷启动未完成或网络不可达）")


def available():
    """当前是否配置了真实 HeyGem 服务。"""
    return AVATAR_PROVIDER == "heygem" and bool(HEYGEM_ENDPOINT)
