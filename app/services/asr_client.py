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
import requests

from app.config import STORAGE_DIR, DASHSCOPE_API_KEY
from app.services import oss_client as oss


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
    """本地音视频 -> 上传 OSS 拿公网签名 URL -> 百炼转写。"""
    if not oss.available():
        raise RuntimeError("未配置 OSS：无法上传音视频转写。请在 start.bat 设置 OSS_* 环境变量。")
    public_url = oss.upload_file(local_path, for_eas=False)  # 百炼在阿里云外，需公网 URL
    return transcribe_url(public_url)


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


def download_video(url: str, dest_dir: str) -> str:
    """优先 yt-dlp（抖音/快手/B站等），失败回退直链下载；都失败则抛明确错误。"""
    last_err = None
    try:
        import yt_dlp
        ydl_opts = {
            "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
            "format": "mp4/best",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)
    except Exception as e:
        last_err = f"yt-dlp 下载失败: {e}"
        # 继续尝试直链回退

    try:
        return _download_direct(url, dest_dir)
    except Exception as e:
        raise RuntimeError(f"{last_err}; 直链回退也失败: {e}")


def extract_from_link(url: str) -> str:
    """从链接提取文案。直链直接转写；页面链接先下载（yt-dlp）再转写。"""
    url = _extract_url(url)
    low = url.lower().split("?")[0]
    if any(low.endswith(ext) for ext in _MEDIA_EXT):
        return transcribe_url(url)
    tmp = os.path.join(STORAGE_DIR, "temp")
    os.makedirs(tmp, exist_ok=True)
    path = download_video(url, tmp)
    return transcribe_file(path)


def extract_from_file(local_path: str) -> str:
    """本地上传的音视频文件 -> 上传 OSS -> 百炼转写。"""
    return transcribe_file(local_path)
