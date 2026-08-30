"""CosyVoice2 客户端（对接 PAI-EAS 部署，替代 fish+asr 作真实声音克隆）。

线上服务为 FastAPI 版 CosyVoice2，已通过 OpenAPI 探活确认契约：
  - POST /api/v1/audio/reference_audio/   multipart(file=音频, text=参考文本)
        返回 {"id": "cosyvoice3_xxx", ...}  ← 音色 id 字段名是 id（不是 reference_audio_id）
  - POST /api/v1/audio/speech/            JSON
        body = {"model": "<CosyVoice2-0.5B 路径>",
                "input": {"mode": "natural_language_replication"|"fast_replication"|"cross_lingual_replication",
                          "text": "<要合成的文字>",
                          "reference_audio_id": "<上面拿到的 id>",
                          "speed": 1.0, "output_format": "wav"|"mp3"},
                "stream": false}
        返回 {"output": {"audio": {"data": "<base64 音频>"}, ...}}
  - 鉴权：Authorization: Bearer <COSYVOICE_TOKEN>
  - 注意：线上服务空闲会缩容为 0，首调返回 503 冷启动，客户端已内置重试。

参考音频的 text 字段服务端会强制前缀 "You are a helpful assistant.<|endofprompt|>",
属该部署的 prompt 封装，本客户端只需传业务参考文本（默认中性中文样本即可）。
"""
import os
import io
import json
import time
import uuid
import base64
import urllib.request
import urllib.error

from app.config import (
    COSYVOICE_ENABLED, COSYVOICE_ENDPOINT, COSYVOICE_TOKEN,
    COSYVOICE_FORMAT, COSYVOICE_MODEL,
)

# 合成模式固定为自然语言复刻（natural_language_replication）。
# 业务上只用这一种，不暴露为可配置项，避免误配成 fast/cross_lingual 导致音色漂移。
SYNTHESIS_MODE = "natural_language_replication"

# 默认参考文本：用户上传音色样本时通常没有逐字稿，传中性样本即可（零样本克隆仍以音色为主）
DEFAULT_REF_TEXT = "这是我的声音样本，请用这个音色朗读下面的文字。"


def _headers_json():
    h = {"Content-Type": "application/json"}
    if COSYVOICE_TOKEN:
        h["Authorization"] = "Bearer " + COSYVOICE_TOKEN
    return h


def _headers_bearer():
    h = {}
    if COSYVOICE_TOKEN:
        h["Authorization"] = "Bearer " + COSYVOICE_TOKEN
    return h


def _url(path: str) -> str:
    base = COSYVOICE_ENDPOINT.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _http_post(url: str, data: bytes, headers: dict, timeout: int, retry: int = 6, wait: int = 8):
    """POST 并内置 EAS 503 冷启动重试，返回 (status_code, body_bytes)。"""
    last_err = None
    for attempt in range(retry + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504):
                last_err = f"HTTP {e.code} (冷启动)"
                time.sleep(wait)
                continue
            # 4xx 等业务错误：直接返回 body，交由调用方解析
            try:
                return e.code, e.read()
            except Exception:
                return e.code, b""
        except Exception as e:  # 连接错误 / 超时等
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
            time.sleep(wait)
            continue
    raise RuntimeError(f"CosyVoice 请求持续失败（冷启动/网络）: {last_err}")


# CosyVoice 服务端要求的参考音频后缀白名单（来自其 401 报错：suffix must be one of .wav/.mp3/.opus/.aac/.flac/.pcm）
COSY_REF_SUFFIX = {".wav", ".mp3", ".opus", ".aac", ".flac", ".pcm"}
_CT_MAP = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".opus": "audio/ogg",
    ".aac": "audio/aac", ".flac": "audio/flac", ".pcm": "application/octet-stream",
}


def upload_reference_audio(local_path: str, text: str = DEFAULT_REF_TEXT) -> str:
    """上传参考音频，返回音色 id（服务端字段名是 id）。失败抛异常。"""
    if not os.path.exists(local_path):
        raise RuntimeError(f"参考音频不存在: {local_path}")
    _ext = (os.path.splitext(local_path)[1] or "").lower()
    if _ext not in COSY_REF_SUFFIX:
        raise RuntimeError("音色格式不被 CosyVoice 支持，请重新上传 wav/mp3/opus/aac/flac/pcm 格式")
    with open(local_path, "rb") as f:
        file_bytes = f.read()
    filename = os.path.basename(local_path) or ("ref" + _ext)
    ctype = _CT_MAP.get(_ext, "application/octet-stream")
    boundary = ("----CosyBoundary" + uuid.uuid4().hex).encode()
    body = b""
    body += b"--" + boundary + b"\r\n"
    body += b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\n'
    body += b"Content-Type: " + ctype.encode() + b"\r\n\r\n"
    body += file_bytes + b"\r\n"
    body += b"--" + boundary + b"\r\n"
    body += b'Content-Disposition: form-data; name="text"\r\n\r\n'
    body += text.encode("utf-8") + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    headers = _headers_bearer()
    headers["Content-Type"] = "multipart/form-data; boundary=" + boundary.decode()

    code, raw = _http_post(_url("/api/v1/audio/reference_audio/"), body, headers,
                           timeout=120)
    if code >= 400:
        raise RuntimeError(f"CosyVoice 注册参考音频失败 HTTP {code}: {raw[:400].decode('utf-8','ignore')}")
    try:
        resp = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError(f"CosyVoice 注册返回非 JSON: {raw[:200]}")
    rid = resp.get("id") or resp.get("reference_audio_id")
    if not rid:
        raise RuntimeError(f"CosyVoice 注册未返回音色 id: {resp}")
    return rid


def synthesize(text: str, reference_audio_id: str, speed: float = 1.0,
               output_format: str = None, timeout: int = 180) -> bytes:
    """合成语音，返回音频二进制（wav/mp3）。失败抛异常。"""
    fmt = (output_format or COSYVOICE_FORMAT or "wav").lower()
    payload = {
        "model": COSYVOICE_MODEL,
        "input": {
            "mode": SYNTHESIS_MODE,
            "text": text,
            "reference_audio_id": reference_audio_id,
            "speed": speed,
            "output_format": fmt,
        },
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    code, raw = _http_post(_url("/api/v1/audio/speech/"), data, _headers_json(),
                           timeout=timeout)
    if code >= 400:
        raise RuntimeError(f"CosyVoice 合成失败 HTTP {code}: {raw[:400].decode('utf-8','ignore')}")
    try:
        resp = json.loads(raw.decode("utf-8"))
    except Exception:
        # 非 JSON：可能直接是裸音频（极少），原样返回
        return raw
    audio = resp.get("output", {}).get("audio") if isinstance(resp.get("output"), dict) else None
    b64 = None
    if isinstance(audio, dict):
        b64 = audio.get("data")
    elif isinstance(audio, str):
        b64 = audio
    if not b64:
        raise RuntimeError(f"CosyVoice 合成返回缺少音频数据: {str(resp)[:300]}")
    try:
        return base64.b64decode(b64)
    except Exception:
        # 已可能是裸二进制
        return audio if isinstance(audio, (bytes, bytearray)) else b""


def available() -> bool:
    """是否已配置可用的 CosyVoice2 服务。"""
    return bool(COSYVOICE_ENABLED and COSYVOICE_ENDPOINT and COSYVOICE_TOKEN)
