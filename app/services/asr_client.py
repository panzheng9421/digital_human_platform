"""文案提取：百炼 Paraformer-v2 异步语音转写（替代占位实现）。

流程：
  链接直链(音视频)        -> 直接送百炼转写（百炼需公网可访问 URL）
  抖音/快手/B站等页面链接 -> yt-dlp 下载到本地 -> 上传 OSS 拿公网签名 URL -> 送百炼
  本地上传文件(音视频)    -> 上传 OSS 拿公网签名 URL -> 送百炼

百炼录音文件识别为异步：提交拿 task_id，轮询 tasks/{id} 直到 SUCCEEDED。
支持视频 URL 直接转写（自动抽音轨），无需 ffmpeg。
"""
import os
import re
import time
import contextlib
from urllib.parse import urlparse

import requests

from app.config import STORAGE_DIR, DASHSCOPE_API_KEY
from app.services import oss_client as oss

# 抖音系域名：这些站点对「代理出口」风控极严（实测走本地代理时 iesdouyin 直接 TLS 被掐断、
# douyin 详情页返回 Fresh cookies needed），直连反而稳定 -> 下载时默认绕开代理。
_BYPASS_PROXY_HOSTS = (
    "douyin.com", "iesdouyin.com", "amemv.com", "douyinvod.com",
    "snssdk.com", "tiktokv.com", "byteimg.com", "douyinpic.com",
)
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                   "ALL_PROXY", "all_proxy")


def _extract_url(text: str) -> str:
    """从用户粘贴的文本中提取第一个 http/https URL（处理带中文前缀/口令的情况）。"""
    m = re.search(r"https?://[^\s\u3002\uff0c\uff1f\uff01\uff1b\"'<>\)\]\}]+", text)
    if m:
        return m.group(0).rstrip(".,;?!。，；？！")
    return text.strip()

SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

# 直链媒体后缀（百炼可直接拉公网直链转写）
_MEDIA_EXT = (".mp4", ".mp3", ".wav", ".m4a", ".aac", ".flac",
              ".ogg", ".opus", ".webm", ".mov", ".mkv", ".avi")


def available() -> bool:
    return bool(DASHSCOPE_API_KEY)


def asr_align_available() -> bool:
    """字幕对齐所需完整条件：百炼 API Key + OSS（本地音频需先上传公网给百炼拉取）。
    不满足时调用方应回退「按句均分」，避免无谓网络尝试。"""
    return bool(DASHSCOPE_API_KEY) and oss.available()


def _headers():
    return {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }


def _submit(file_url: str) -> str:
    data = {
        "model": "paraformer-v2",
        "input": {"file_urls": [file_url]},
        "parameters": {"channel_id": [0], "language_hints": ["zh", "en"]},
    }
    r = requests.post(SUBMIT_URL, headers=_headers(), json=data, timeout=30)
    r.raise_for_status()
    return r.json()["output"]["task_id"]


def _wait(task_id: str, timeout: int = 600) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = requests.get(TASK_URL.format(task_id=task_id), headers=_headers(), timeout=30)
        r.raise_for_status()
        out = r.json().get("output", {})
        last = out
        status = out.get("task_status")
        if status == "SUCCEEDED":
            return out
        if status == "FAILED":
            raise RuntimeError("百炼转写失败: " + str(out.get("message") or out))
        time.sleep(4)
    raise TimeoutError(f"百炼转写超时（> {timeout}s），最后状态: {last}")


def _parse_text(out: dict) -> str:
    """从百炼任务结果抽取转写文本。

    百炼返回结构嵌套：output.results[].output.results[].transcription_url
    指向的 JSON 内容为 {"transcripts":[{"text":"..."}]}（字段是 text，非 transcription）。
    """
    parts = []

    def _collect(node):
        if isinstance(node, dict):
            if node.get("transcription"):
                parts.append(node["transcription"])
            tu = node.get("transcription_url")
            if tu:
                try:
                    jr = requests.get(tu, timeout=30).json()
                    for tr in (jr.get("transcripts") or []):
                        if tr.get("text"):
                            parts.append(tr["text"])
                except Exception:
                    pass
            for key in ("output", "results", "result"):
                child = node.get(key)
                if isinstance(child, (dict, list)):
                    _collect(child)
        elif isinstance(node, list):
            for item in node:
                _collect(item)

    _collect(out)
    # 百炼嵌套结构可能重复收集同一段，按出现顺序去重
    uniq = []
    for p in parts:
        if p and p not in uniq:
            uniq.append(p)
    return "\n".join(uniq).strip()


def transcribe_url(public_url: str) -> str:
    """百炼转写公网可访问的音视频 URL。"""
    return _parse_text(_wait(_submit(public_url)))


def transcribe_file(local_path: str) -> str:
    """本地音视频 -> 上传 OSS 拿公网签名 URL -> 百炼转写。转写完成后清理 OSS 中转对象。

    注意：本函数只删 OSS 上的上传副本，不删本地文件——本地文件可能是持久音频
    （如剪辑页字幕对齐传入的配音/源视频），是否删除由调用方（中转入口）决定。
    """
    if not oss.available():
        raise RuntimeError("未配置 OSS：无法上传音视频转写。请在 start.bat 设置 OSS_* 环境变量。")
    public_url = oss.upload_file(local_path, for_eas=False)  # 百炼在阿里云外，需公网 URL
    key = oss.object_key_from_url(public_url)
    try:
        return transcribe_url(public_url)
    finally:
        # 百炼已拉取转写，OSS 上的中转副本可删（失败仅警告）
        oss.delete_object(key)


def _parse_sentences(out: dict):
    """从百炼任务结果抽取带时间戳的句子列表，用于字幕对齐。

    百炼录音文件识别返回结构嵌套：output.results[].output.results[].transcription_url
    指向的 JSON 含 transcripts[].sentences[].{begin_time,end_time,text}（毫秒级时间戳）。
    返回 [{"text":..., "begin":ms(int), "end":ms(int)}, ...]（按原序、去重）。
    """
    items = []

    def _collect(node):
        if isinstance(node, dict):
            tu = node.get("transcription_url")
            if tu:
                try:
                    jr = requests.get(tu, timeout=30).json()
                    for tr in (jr.get("transcripts") or []):
                        for s in (tr.get("sentences") or []):
                            t = (s.get("text") or "").strip()
                            if t:
                                items.append({
                                    "text": t,
                                    "begin": int(s.get("begin_time") or 0),
                                    "end": int(s.get("end_time") or 0),
                                })
                except Exception:
                    pass
            for key in ("output", "results", "result"):
                child = node.get(key)
                if isinstance(child, (dict, list)):
                    _collect(child)
        elif isinstance(node, list):
            for it in node:
                _collect(it)

    _collect(out)
    # 嵌套结构可能重复收集同一段，按内容去重（保留首次出现）
    uniq = []
    for it in items:
        if it not in uniq:
            uniq.append(it)
    return uniq


def transcribe_url_ts(public_url: str, timeout: int = 180) -> list:
    """百炼转写公网可访问音视频 URL，返回带时间戳句子列表 [{"text","begin"(ms),"end"(ms)}]。"""
    return _parse_sentences(_wait(_submit(public_url), timeout=timeout))


def transcribe_file_ts(local_path: str, timeout: int = 180) -> list:
    """本地音视频 -> 上传 OSS 拿公网 URL -> 百炼转写，返回带时间戳句子列表（用于字幕对齐）。

    转写完成后清理 OSS 中转对象（只删 OSS 副本，不删本地文件，理由同 transcribe_file）。
    """
    if not oss.available():
        raise RuntimeError("未配置 OSS：无法上传音视频做 ASR 对齐。请在 start.bat 设置 OSS_* 环境变量。")
    public_url = oss.upload_file(local_path, for_eas=False)  # 百炼在阿里云外，需公网 URL
    key = oss.object_key_from_url(public_url)
    try:
        return transcribe_url_ts(public_url, timeout=timeout)
    finally:
        oss.delete_object(key)


def _download_direct(url: str, dest_dir: str) -> str:
    ext = os.path.splitext(url.split("?")[0])[1].lower() or ".mp4"
    dest = os.path.join(dest_dir, f"dl_{int(time.time() * 1000)}{ext}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ct:
            raise RuntimeError(f"直链返回的是网页(html)，不是音视频：Content-Type={ct}")
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
    if os.path.getsize(dest) < 1024:
        os.remove(dest)
        raise RuntimeError("下载文件小于 1KB，疑似未拿到真实音视频")
    return dest


def _resolve_cookiefile() -> str:
    """解析 cookie 文件来源（优先级：环境变量 > 固定默认路径）。返回空串表示无文件。

    默认位置：storage/cookies/douyin_cookies.txt（用户只需把导出的 cookies.txt 丢这里即可，
    无需设置任何环境变量）。storage/ 已被 .gitignore 忽略，cookie 文件不会进版本库。
    """
    env = os.environ.get("DOUYIN_COOKIES_FILE", "").strip()
    if env and os.path.exists(env):
        return env
    for cand in (
        os.path.join(STORAGE_DIR, "cookies", "www_douyin_com_cookies.txt"),
        os.path.join(STORAGE_DIR, "cookies", "douyin_cookies.txt"),
        os.path.join(STORAGE_DIR, "cookies.txt"),
    ):
        if os.path.exists(cand):
            return cand
    return ""


def _is_media_file(path: str) -> bool:
    """粗略判断文件是否为音视频（而非抖音登录墙/风控返回的 HTML 页面）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except Exception:
        return False
    if not head:
        return False
    low = head[:512].lower()
    # 抖音/快手登录墙通常返回 HTML
    if low.lstrip()[:1] == b"<" or low[:5] == b"<!doc" or b"<html" in low:
        return False
    # 常见音视频 magic number
    return (
        b"ftyp" in head                              # mp4/m4a/mov
        or head.startswith((b"ID3", b"OggS", b"RIFF", b"FLV\x01", b"\x1a\x45\xdf\xa3"))
        or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")  # mp3 帧
    )


def _should_bypass_proxy(url: str) -> bool:
    """URL 是否属于抖音系域名（这类域名直连更稳，需绕开代理）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return bool(host) and any(host == h or host.endswith("." + h) for h in _BYPASS_PROXY_HOSTS)


@contextlib.contextmanager
def _proxy_env(bypass: bool):
    """临时清空/恢复代理环境变量（作用于 os.environ，requests 与 yt-dlp 都会读到）。

    bypass=False 时不做任何改动，保持进程原有环境。
    """
    if not bypass:
        yield
        return
    saved = {k: os.environ.pop(k) for k in _PROXY_ENV_KEYS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def _short_err(e, limit: int = 220) -> str:
    """错误信息瘦身：去掉超长 URL 与 ANSI 颜色码，避免前端 note 被几百字刷屏。"""
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    msg = re.sub(r"https?://\S+", "<链接>", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:limit] + ("…" if len(msg) > limit else "")


def _extract_meta(info: dict) -> dict:
    """从 yt-dlp 的 extract_info 结果抽取视频元数据（抖音可能缺部分字段，缺失即空）。"""
    if not info:
        return {}
    return {
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "share_count": info.get("repost_count") or info.get("share_count"),
        "collect_count": info.get("save_count") or info.get("favorite_count") or info.get("collect_count"),
        "duration": info.get("duration") or 0,
    }


def _download_ytdlp(url: str, dest_dir: str, cookiefile: str, no_proxy: bool):
    """用 yt-dlp 下载。no_proxy=True 时禁用代理（抖音系站点直连更稳）。"""
    import yt_dlp
    ydl_opts = {
        "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
        "format": "mp4/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if no_proxy:
        ydl_opts["proxy"] = ""  # 显式禁用代理（否则 yt-dlp 会读环境变量里的代理）
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    else:
        ydl_opts["cookiesfrombrowser"] = ("chrome",)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        if not _is_media_file(path):
            try:
                os.remove(path)
            except Exception:
                pass
            raise RuntimeError("下载到的不是音视频（疑似抖音登录墙/风控页），请更新 cookies.txt 后重试")
        return path, _extract_meta(info)


def download_video(url: str, dest_dir: str):
    """抖音/快手/B站等页面链接下载（需登录态绕过抖音登录墙）。

    返回 (本地路径, 元数据 dict)；元数据来自 yt-dlp extract_info（点赞/评论/转发/标题/作者/时长）。

    Cookie 来源优先级：
      1) 环境变量 DOUYIN_COOKIES_FILE 指向的 cookies.txt
      2) 固定默认路径 storage/cookies/douyin_cookies.txt（推荐：丢文件即用）
      3) 以上都没有时，回退读取本机 Chrome 登录态（cookiesfrombrowser，受 Chrome ABE 限制可能失败）

    网络策略：抖音系域名先「直连（绕开代理）」再回退「走代理」——实测本机开了 127.0.0.1:7897 代理时，
    抖音会对代理出口报 SSL UNEXPECTED_EOF / Fresh cookies needed，直连反而一次就通。
    下载后校验文件确为音视频，避免把登录墙 html 送百炼产生误导性 400。
    """
    cookiefile = _resolve_cookiefile()
    if cookiefile:
        print(f"[asr] 使用 cookie 文件: {cookiefile}")
    else:
        print("[asr] 未找到 cookie 文件，回退读取本机 Chrome 登录态（可能受 Chrome ABE 限制失败）")

    # 尝试顺序：抖音系 -> [直连, 代理]；其他站点 -> [保持当前环境]
    orders = [True, False] if _should_bypass_proxy(url) else [False]
    errs = []

    for no_proxy in orders:
        tag = "直连(绕过代理)" if no_proxy else "走代理"
        try:
            with _proxy_env(no_proxy):
                return _download_ytdlp(url, dest_dir, cookiefile, no_proxy)
        except Exception as e:
            errs.append(f"yt-dlp {tag}失败: {_short_err(e)}")
            print(f"[asr] yt-dlp {tag}失败：{_short_err(e)}")

    # yt-dlp 全失败 -> 直链回退（同样按上面的顺序各试一次）
    for no_proxy in orders:
        tag = "直连(绕过代理)" if no_proxy else "走代理"
        try:
            with _proxy_env(no_proxy):
                return _download_direct(url, dest_dir), {}
        except Exception as e:
            errs.append(f"直链 {tag}失败: {_short_err(e)}")

    raise RuntimeError("；".join(errs))


def extract_from_link(url: str) -> dict:
    """从链接提取文案。直链直接转写；页面链接先下载（yt-dlp）再转写。
    返回 {"text": 转写文案, "meta": 视频元数据}。"""
    url = _extract_url(url)
    low = url.lower().split("?")[0]
    if any(low.endswith(ext) for ext in _MEDIA_EXT):
        return {"text": transcribe_url(url), "meta": {}}
    tmp = os.path.join(STORAGE_DIR, "temp")
    os.makedirs(tmp, exist_ok=True)
    path, meta = download_video(url, tmp)
    try:
        return {"text": transcribe_file(path), "meta": meta}
    finally:
        # yt-dlp 下载的本地中转文件用完即删（失败仅警告，不影响主流程）
        try:
            if path and os.path.exists(path):
                os.remove(path)
                print(f"[asr] 已清理本地中转文件: {path}")
        except Exception as e:
            print(f"[asr] 清理本地中转文件失败(可忽略): {path} -> {e}")


def extract_from_file(local_path: str) -> dict:
    """本地上传的音视频文件 -> 上传 OSS -> 百炼转写。无视频链接元数据，meta 为空。

    上传的中转文件（storage/temp 下）转写完成后清理；OSS 副本由 transcribe_file 清理。
    """
    try:
        return {"text": transcribe_file(local_path), "meta": {}}
    finally:
        try:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
                print(f"[asr] 已清理本地中转文件: {local_path}")
        except Exception as e:
            print(f"[asr] 清理本地中转文件失败(可忽略): {local_path} -> {e}")
