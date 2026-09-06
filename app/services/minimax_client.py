"""MiniMax T2A v2 / speech-2.8-hd 客户端（老板拍板 2026-09-06 从 CosyVoice 切换）。

为什么切：CosyVoice v3.5-plus 实测 <prosody> 被拒（411）且无局部韵律标签；
MiniMax speech-2.8-hd 有独有语气词标签（仅 2.8 系列）+ 文本内停顿 <#x#>（0.01~99.99s，
不能连续两个）。词级韵律（重音/局部变速）两家都没有——丢弃并打日志，二期句级拼接。

官方 API（老板 2026-09-06 核验，中文站文档 platform.minimaxi.com/docs/api-reference/speech-t2a-http）：
  合成  POST https://api.minimax.cn/v1/t2a_v2            （备用 api-bj.minimaxi.com）
  上传  POST https://api.minimaxi.com/v1/files/upload    (multipart, purpose=voice_clone)
  克隆  POST https://api.minimaxi.com/v1/voice_clone     (file_id + 自定义 voice_id)
鉴权：Authorization: Bearer <MINIMAX_API_KEY>。**当前版接口 URL 均不带 GroupId**（官方示例实证）。

克隆规则：样本 mp3/m4a/wav，10s~5min，≤20MB；克隆费在**首次合成**时收（官方文档原话）；
克隆出的音色 168 小时(7天)内必须在 T2A 中使用，否则被删（删了重新克隆即可）。

voice_id 落盘 storage/minimax_voice.json（一个平台一个音色，避免重复克隆烧钱）。
错误全部打印（老板规矩：错误日志不许吞），失败抛 RuntimeError 由调用方处理。
"""
import json
import os
import time

import requests

from app.config import MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL, STORAGE_DIR

VOICE_FILE = os.path.join(STORAGE_DIR, "minimax_voice.json")
# 克隆/上传按官方《音色快速复刻》指南主域名；合成按官方 t2a 文档主域名（MINIMAX_BASE_URL 可切）
_CLONE_API = "https://api.minimaxi.com"


def available() -> bool:
    """key 在才算可用（当前版接口不需要 GroupId）。"""
    return bool(MINIMAX_API_KEY)


def _fail(j: dict, what: str) -> str:
    """base_resp 非 0 的标准错误串。"""
    br = (j or {}).get("base_resp") or {}
    return (f"MiniMax {what}失败: status_code={br.get('status_code')} "
            f"({br.get('status_msg')})")


def upload_file(local_path: str) -> int:
    """上传克隆样本音频 -> file_id。purpose 固定 voice_clone。"""
    url = f"{_CLONE_API}/v1/files/upload"
    with open(local_path, "rb") as f:
        r = requests.post(url, headers={"Authorization": f"Bearer {MINIMAX_API_KEY}"},
                          files={"file": (os.path.basename(local_path), f)},
                          data={"purpose": "voice_clone"}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if (j.get("base_resp") or {}).get("status_code") != 0:
        raise RuntimeError(f"{_fail(j, '文件上传')} sample={local_path}")
    fid = (j.get("file") or {}).get("file_id")
    if not fid:
        raise RuntimeError(f"MiniMax 文件上传返回无 file_id: {json.dumps(j, ensure_ascii=False)[:500]}")
    print(f"[minimax] 样本上传成功 file_id={fid} sample={local_path}")
    return fid


def clone_voice(sample_path: str, voice_id: str) -> str:
    """克隆音色。voice_id 自定义（字母开头，字母/数字）。结果落盘 VOICE_FILE。"""
    fid = upload_file(sample_path)
    url = f"{_CLONE_API}/v1/voice_clone"
    r = requests.post(url, headers={"Authorization": f"Bearer {MINIMAX_API_KEY}"},
                      json={"file_id": fid, "voice_id": voice_id}, timeout=60)
    r.raise_for_status()
    j = r.json()
    if (j.get("base_resp") or {}).get("status_code") != 0:
        raise RuntimeError(f"{_fail(j, '克隆')} file_id={fid} voice_id={voice_id}")
    with open(VOICE_FILE, "w", encoding="utf-8") as f:
        json.dump({"voice_id": voice_id, "sample": sample_path,
                   "cloned_at": int(time.time())}, f, ensure_ascii=False, indent=2)
    print(f"[minimax] 克隆成功 voice_id={voice_id}（克隆费在首次合成时收；7 天内要使用一次）"
          f" 已落盘 {VOICE_FILE}")
    return voice_id


def cached_voice() -> str:
    """读缓存的 voice_id；没有/读挂返回空串（调用方走重克隆）。"""
    if not os.path.exists(VOICE_FILE):
        return ""
    try:
        with open(VOICE_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        return j.get("voice_id") or ""
    except Exception as e:
        print(f"[minimax] voice 缓存读取失败(将重新克隆): {e}")
        return ""


def cached_voice_for(sample_path: str) -> str:
    """返回与样本**匹配**的缓存 voice_id；不匹配/没有返回空串（调用方走克隆）。

    json 是全局单值缓存但音色按样本归属——A 音色的配音不能拿到 B 音色克隆的
    voice_id（否则张冠李戴）。正式存储在 timbres.mm_voice_id（按行归属），json
    只作历史备份/迁移用：sample 一致才放行。
    """
    vid = cached_voice()
    if not vid:
        return ""
    try:
        with open(VOICE_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        if os.path.abspath(str(j.get("sample") or "")) == os.path.abspath(sample_path):
            return vid
        print(f"[minimax] json 缓存 sample 不匹配(缓存={j.get('sample')} 当前={sample_path})，"
              f"忽略缓存 voice_id={vid}")
    except Exception as e:
        print(f"[minimax] voice 缓存样本比对失败(按无缓存处理): {e}")
    return ""


def drop_cached_voice():
    """删除 voice 缓存（合成报 voice 不存在时删掉，下一次重新克隆）。"""
    try:
        if os.path.exists(VOICE_FILE):
            os.remove(VOICE_FILE)
            print(f"[minimax] 已删除失效 voice 缓存 {VOICE_FILE}")
    except Exception as e:
        print(f"[minimax] 删除 voice 缓存失败: {e}")


def synthesize(text: str, voice_id: str, speed: float = 1.0,
               pitch: int = 0, vol: float = 1.0, emotion: str = None) -> bytes:
    """T2A v2 非流式合成 -> mp3 bytes。

    speed∈[0.5,2]；vol∈(0,10]；pitch∈[-12,12] 整数——全部**整篇全局**参数，无局部韵律。
    emotion：MiniMax 情绪枚举（happy/sad/angry/fearful/disgusted/surprised/calm/fluent），
    请求级参数一次一个；None/空 = 不传，模型按文本自动匹配情绪（官方默认行为）。
    默认 output_format=hex：data.audio 为十六进制音频数据，bytes.fromhex 直接还原 mp3。
    RPM 限流（2026-09-06 实测：段级并行合成 1 分钟内第 11 个请求报 1002
    rate limit exceeded(RPM)，约 10 次/分钟）：退避等待配额窗口重置后重试，
    最多 3 次（30s/60s/90s），重试全程打日志，耗尽仍失败原样上抛（错误不吞）。
    """
    url = f"{MINIMAX_BASE_URL}/v1/t2a_v2"
    voice_setting = {"voice_id": voice_id, "speed": speed, "vol": vol, "pitch": pitch}
    if emotion:
        voice_setting["emotion"] = emotion
    payload = {
        "model": MINIMAX_MODEL,
        "text": text,
        "stream": False,
        "voice_setting": voice_setting,
        "audio_setting": {"format": "mp3", "sample_rate": 32000},
    }
    print(f"[DEBUG-T2A-BODY] 发给MiniMax的完整body:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
    last_err = None
    for attempt in range(3):
        r = requests.post(url, headers={"Authorization": f"Bearer {MINIMAX_API_KEY}"},
                          json=payload, timeout=300)
        r.raise_for_status()
        j = r.json()
        code = (j.get("base_resp") or {}).get("status_code")
        if code == 0:
            audio = (j.get("data") or {}).get("audio") or ""
            if not audio:
                raise RuntimeError(f"MiniMax 合成返回无 audio 数据: {json.dumps(j, ensure_ascii=False)[:500]}")
            data = bytes.fromhex(audio)
            print(f"[minimax] 合成成功 {len(data)}字节 speed={speed} pitch={pitch} "
                  f"emotion={emotion or '-'} voice={voice_id} text={len(text)}字 model={MINIMAX_MODEL}")
            return data
        last_err = RuntimeError(f"{_fail(j, '合成')} model={MINIMAX_MODEL} voice={voice_id} "
                                f"speed={speed} pitch={pitch} emotion={emotion or '-'} text={len(text)}字")
        if code == 1002 and attempt < 2:
            wait = 30 * (attempt + 1)
            print(f"[minimax] RPM 限流(1002)，退避 {wait}s 后重试({attempt + 1}/3): {last_err}")
            time.sleep(wait)
            continue
        break
    raise last_err
