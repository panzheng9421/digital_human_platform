"""CosyVoice 客户端（对接阿里云百炼 DashScope CosyVoice v3.5）。

替代原 PAI-EAS 自部署方案：
  - 声音复刻：POST /api/v1/services/audio/tts/customization 注册音色，返回 voice_id
  - 语音合成：dashscope.audio.tts_v2.SpeechSynthesizer，非流式 call() 返回音频 bytes
  - 模型默认 cosyvoice-v3.5-plus（可配 DASHSCOPE_TTS_MODEL；plus 为超高表现力版）
  - 音色注册 / 合成都需要 DASHSCOPE_API_KEY；可选 DASHSCOPE_WORKSPACE_ID

对外接口保持与旧 EAS 版一致：
  - upload_reference_audio(local_path) -> voice_id
  - synthesize(text, reference_audio_id, speed=1.0, output_format=None, instruct=None,
               pitch=1.0, volume=50, seed=0, on_progress=None) -> bytes
"""
import os
import re
import json
import urllib.request
import urllib.error

from app.config import (
    DASHSCOPE_API_KEY,
    DASHSCOPE_WORKSPACE_ID,
    DASHSCOPE_TTS_MODEL,
    DASHSCOPE_VOICE_ENROLLMENT_MODEL,
    COSYVOICE_FORMAT,
)
from app.services import oss_client as oss

# 复刻时的语种提示（官方可选参数 language_hints）：辅助模型识别样本音频语种，
# 从而更准确地提取音色特征、提升复刻效果。当前版本仅处理第一个元素。
LANGUAGE_HINTS = ["zh"]

# —— 百炼 CosyVoice 声音复刻对样本音频的官方要求 ——
# 来源：阿里云百炼「声音复刻」文档「音频要求」章节（CosyVoice 系列）
#   格式：WAV(16bit) / MP3 / M4A —— 仅这三种；
#        opus / aac / flac / pcm / ogg / webm 官方均不支持
#   时长：推荐 10~20 秒，最长不超过 60 秒
#   大小：≤ 10 MB
#   采样率：≥ 16 kHz；声道：单/双声道（双声道仅处理首声道）
#   内容：至少 5 秒连续清晰朗读，无背景音乐/噪音，不要歌曲
ALLOWED_TIMBRE_FORMATS = {".wav", ".mp3", ".m4a"}
MAX_TIMBRE_SIZE_MB = 10
MAX_TIMBRE_DURATION_SEC = 60.0
RECOMMEND_TIMBRE_DURATION = (10.0, 20.0)

# 情绪标签 -> CosyVoice 风格指令（instruction）。
# 严格遵循官方示例模板：「用{情绪}的语气说，{音调特征}，{场景描述}」
#   —— 官方原文示例：「用激动的语气说，音调上扬，像在跟老朋友分享一个好消息」
# 三段式缺一不可：语气（怎么说）+ 音调（音高走向）+ 场景（对谁说/在什么情境说）。
# 注意：指令里的"音调"描述的是**音高走向与腔调风格**（如上扬/低沉/发紧），
# 与 pitch_rate 参数的**整体音高平移**是两回事，二者互补不冲突，无需回避。
# 所有指令 ≤100 字符（汉字按 2 字符计），本表最长 52 字符单位，安全。
#
# ⚠️ 例外：「自然」取值是 None = **不传 instruction**，让模型按复刻音色的本征韵律走。
# 实测（2026-08-31，v3.5-flash 复刻音色）同一文本横评「尾2秒能量 / 全文中位能量」：
#   不传 instruction → 0.85 / 0.71（<1，结尾自然收音）✅
#   只传"自然"      → 1.08 / 1.05（且比不传慢约 27%、换种子波动最大）
#   三段式指令      → 1.58 / 1.60（>1.5，结尾明显加重，听感"喊/凶"）
# 且不传时**换种子最稳**（时长/能量波动仅 4.2%/2.2%），排除 EAS 时代"不传漂方言"的风险。
# 结论：「自然」不是一个情绪，而是"别干预"的状态——任何指令都是加滤镜，越描述结尾越使劲。
EMOTION_INSTRUCTIONS = {
    "自然": None,  # 不传 instruction，让模型自己来
    "嫌弃": "用嫌弃的语气说，音调懒散拖长，像在不耐烦地挑毛病",
    "高兴": "用高兴的语气说，音调轻快上扬，像在分享一个好消息",
    "伤心": "用伤心的语气说，音调低沉发闷，像在回忆一件遗憾的事",
    "说教": "用说教的语气说，音调稳重下沉，像长辈在语重心长地叮嘱",
    "激动": "用激动的语气说，音调上扬，像在跟老朋友分享一个好消息",
    "生气": "用生气的语气说，音调高亢发紧，像在严厉地纠正一个错误",
}


def build_instruction(emotion: str, custom: str = None):
    """根据情绪标签生成 CosyVoice instruction；返回 None 表示**不传**该参数。

    「自然」刻意返回 None（让模型按复刻音色本征韵律走，实测收尾最自然且最稳定）；
    未登记的情绪也返回 None（不干预），而不是套一个默认指令。
    若提供 custom 则优先用 custom。
    """
    if custom and custom.strip():
        return custom.strip()
    return EMOTION_INSTRUCTIONS.get(emotion)


# 复刻音色时使用的目标模型，必须与合成模型一致
TARGET_MODEL = DASHSCOPE_TTS_MODEL

# 输出格式映射：业务层 wav/mp3 -> DashScope AudioFormat 枚举
FMT_MAP = {
    "wav": "WAV_24000HZ_MONO_16BIT",
    "mp3": "MP3_22050HZ_MONO_256KBPS",
}


def _fmt_enum(fmt: str):
    """把 wav/mp3 等后缀转成 DashScope AudioFormat 枚举名。"""
    from dashscope.audio.tts_v2 import AudioFormat
    name = FMT_MAP.get((fmt or "wav").lower().lstrip("."), FMT_MAP["wav"])
    return getattr(AudioFormat, name, AudioFormat.WAV_24000HZ_MONO_16BIT)


def _strip_markdown(text: str) -> str:
    """去掉常见 markdown 标记，只把要朗读的纯文本送进 TTS。

    实测（2026-08-31，cosyvoice-v3.5-flash 复刻音色）：
      - 官方 additional_params 里的 `enable_markdown_filter` **被服务端忽略**，
        开与不开输出 MD5 完全一致，是个无效参数，不能依赖；
      - 但 `** # *` 这类符号**确实会被念出来**：同样内容带符号比不带符号
        多出 23040 字节 ≈ 0.48 秒废话。
    所以只能本地清洗。LLM 改写稿偶尔会带 markdown，这一步是必要的保险。
    """
    if not text:
        return text
    # 以下缩进类规则一律用 [ \t] 而非 \s，避免 re.M 下 \s 跨行吃掉空行
    t = re.sub(r"```.*?```", "", text, flags=re.S)                   # 代码块整块去掉
    t = re.sub(r"`([^`]*)`", r"\1", t)                               # 行内代码
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)                   # 链接只留文字
    t = re.sub(r"\*\*([^*]*)\*\*", r"\1", t)                         # 加粗 **x**
    t = re.sub(r"\*([^*]*)\*", r"\1", t)                             # 斜体 *x*
    t = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*", "", t, flags=re.M)        # 行首标题号 ## x
    # 其余 #：前面不是字母数字才当标记（保住 C#）；连同其后的空白一起吃掉，避免留下多余空格
    t = re.sub(r"(?<![0-9A-Za-z])[ \t]*#+[ \t]*(?=\s|$)", "", t)
    t = re.sub(r"^[ \t]{0,3}>[ \t]?", "", t, flags=re.M)             # 引用 >
    t = re.sub(r"^[ \t]*([-*+]|\d+\.)[ \t]+", "", t, flags=re.M)     # 列表符号 - / * / 1.
    t = re.sub(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$", "", t, flags=re.M)  # 分隔线 --- ***
    t = re.sub(r"\*{2,}", "", t)                                     # 残留连续星号
    # 注意：不做"汉字间去空格"这类激进处理，原文里本来的空格要保留（可能是断句/停顿）
    return t.strip()


def _clamp_num(v, lo, hi, default):
    """把数值钳制到官方取值范围 [lo, hi]；非数字/为空时回退 default。

    表单传来的值可能是字符串或空值，统一在这里兜底，避免越界导致接口报错。
    """
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _ensure_api_key():
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法使用百炼 CosyVoice")
    import dashscope
    if dashscope.api_key != DASHSCOPE_API_KEY:
        dashscope.api_key = DASHSCOPE_API_KEY
    return dashscope


def _enrollment_url() -> str:
    """声音复刻 REST endpoint：有 WorkspaceId 走业务空间域名，否则走默认 dashscope 域名。"""
    ws = (DASHSCOPE_WORKSPACE_ID or "").strip()
    if ws:
        return f"https://{ws}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization"
    return "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"


def _ws_base_url() -> str:
    """语音合成 WebSocket endpoint。"""
    ws = (DASHSCOPE_WORKSPACE_ID or "").strip()
    if ws:
        return f"wss://{ws}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    return "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def _headers_json() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
    }


def _http_post_json(url: str, payload: dict, timeout: int = 120) -> dict:
    """POST JSON 并返回解析后的 JSON；失败抛异常。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers_json(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        body = e.read()
        code = e.code
    try:
        resp = json.loads(body.decode("utf-8"))
    except Exception:
        resp = {"raw": body[:400].decode("utf-8", "ignore")}
    if code >= 400:
        raise RuntimeError(f"百炼声音复刻失败 HTTP {code}: {resp}")
    return resp


def upload_reference_audio(local_path: str) -> str:
    """上传参考音频到百炼进行声音复刻，返回 voice_id。失败抛异常。

    百炼声音复刻 API 只接受公网可访问的音频 URL，因此先通过 OSS 把本地文件转成 URL，
    复刻成功后再清理 OSS 临时对象（本地 storage/timbre 原文件保留）。

    官方入参（CosyVoice 声音复刻，model=voice-enrollment）：
      必填 model / input.action=create_voice / input.target_model / input.prefix / input.url
      选填 input.language_hints / input.max_prompt_audio_length / input.enable_preprocess
           / input.enable_volume_normalization
    注意：**没有文本字段**。text 参数仅 Qwen-TTS 复刻（model=qwen-voice-enrollment，
    音频走 audio.data base64）支持，CosyVoice 传入会被忽略，故不再先 ASR 转写逐字稿。
    """
    if not os.path.exists(local_path):
        raise RuntimeError(f"参考音频不存在: {local_path}")
    ext = (os.path.splitext(local_path)[1] or "").lower()
    if ext not in ALLOWED_TIMBRE_FORMATS:
        raise RuntimeError(
            "音色格式不被百炼 CosyVoice 支持，仅支持 "
            f"{' / '.join(sorted(f.lstrip('.') for f in ALLOWED_TIMBRE_FORMATS))}（当前：{ext or '无扩展名'}）")
    if not oss.available():
        raise RuntimeError("百炼声音复刻需要先把音频上传到 OSS 获取 URL，但 OSS 未配置")

    # 1) 上传本地音色文件到 OSS，拿到百炼可下载的公网 URL（for_eas=False，不要内网 host）
    audio_url = oss.upload_file(local_path, for_eas=False, expires=1800)
    object_key = None
    try:
        # 从 URL 里尽量解析出 object_key，复刻成功后用于清理
        # 签名 URL 形如 https://bucket.oss-cn-xxx.aliyuncs.com/key?OSSAccessKeyId=...
        from urllib.parse import urlparse, parse_qs, unquote
        parsed = urlparse(audio_url)
        path = unquote(parsed.path)
        # path 通常以 / 开头，去掉开头的 /
        object_key = path.lstrip("/")
    except Exception:
        object_key = None

    payload = {
        "model": DASHSCOPE_VOICE_ENROLLMENT_MODEL,
        "input": {
            "action": "create_voice",
            "target_model": TARGET_MODEL,
            "prefix": "laopan",
            "url": audio_url,
            # 官方可选：辅助识别样本音频语种，提升复刻效果（当前版本仅处理第一个元素）
            "language_hints": LANGUAGE_HINTS,
        },
    }

    # 日志直接从 payload 派生（仅把超长 URL 截断），保证「日志里看到的就是实际发出去的」，
    # 避免手工拼接的日志与真实入参漂移（曾出现过日志写 speed、实际传 speech_rate 的误导）。
    enroll_url = _enrollment_url()
    _log_payload = json.loads(json.dumps(payload))
    _url = _log_payload["input"]["url"]
    if len(_url) > 120:
        _log_payload["input"]["url"] = _url[:120] + f"...[已截断，共{len(_url)}字符]"
    print(f"[bailian] 音色注册 → POST {enroll_url}")
    print(f"[bailian] 音色注册入参(实际发送): {json.dumps(_log_payload, ensure_ascii=False)}")
    print(f"[bailian] 音色注册音频: file={local_path} size={os.path.getsize(local_path)}B")
    try:
        resp = _http_post_json(enroll_url, payload, timeout=120)
    finally:
        # 复刻请求已发出，无论成败都尝试清理 OSS 临时文件
        if object_key:
            try:
                oss._get_bucket().delete_object(object_key)
                print(f"[bailian] 已清理 OSS 临时音色文件: {object_key}")
            except Exception as e:
                print(f"[bailian] 清理 OSS 临时音色文件失败: {e}")

    _resp_str = json.dumps(resp, ensure_ascii=False)
    if len(_resp_str) > 400:
        _resp_str = _resp_str[:400] + f"...[已截断，共{len(_resp_str)}字符]"
    print(f"[bailian] 音色注册出参: {_resp_str}")

    # 返回字段可能是 voice_id 或 voice；优先 voice_id
    voice_id = resp.get("output", {}).get("voice_id") if isinstance(resp.get("output"), dict) else None
    if not voice_id:
        voice_id = resp.get("voice_id")
    if not voice_id:
        voice_id = resp.get("output", {}).get("voice") if isinstance(resp.get("output"), dict) else None
    if not voice_id:
        voice_id = resp.get("voice")
    if not voice_id:
        raise RuntimeError(f"百炼声音复刻未返回 voice_id: {resp}")
    return voice_id


def delete_voice(voice_id: str) -> None:
    """删除百炼云端已注册的音色，释放配额。失败抛异常。

    官方配额：每个百炼账号 CosyVoice 最多 1000 个自定义音色，达到上限后新建会直接失败
    （错误码 40001001 VOICE_LIMIT_ERROR），系统不会自动淘汰最早的音色。
    本地删除音色时应当同步调用本方法，避免云端残留孤儿音色占配额。
    """
    if not voice_id:
        return
    _ensure_api_key()
    from dashscope.audio.tts_v2 import VoiceEnrollmentService
    VoiceEnrollmentService().delete_voice(voice_id)


def synthesize(text: str, reference_audio_id: str, speed: float = 1.0,
               output_format: str = None, instruct: str = None,
               pitch: float = 1.0, volume: int = 50, seed: int = 0,
               on_progress=None) -> bytes:
    """调用百炼 CosyVoice 合成语音，返回音频二进制 bytes。失败抛异常。

    参数与官方 SDK 字段的对应关系（同一参数在不同层名字不同，勿混淆）：
      speed    -> speech_rate  语速，0.5~2.0，默认 1.0（REST parameters 里叫 rate）
      pitch    -> pitch_rate   音调，0.5~2.0，默认 1.0（REST 里叫 pitch）
      volume   -> volume       音量，0~100，默认 50
      seed     -> seed         随机种子，0~65535，默认 0（同参数可复现；换值=换一版效果）
      instruct -> instruction  风格/情感指令，≤100 字符；**传 None 表示不传该参数**。
                               实测「自然」不传效果最好（详见 EMOTION_INSTRUCTIONS 注释），
                               故 build_instruction("自然") 返回 None，这里原样透传。
    output_format：wav 或 mp3，默认读取 COSYVOICE_FORMAT / wav。
    on_progress：兼容旧接口回调 on_progress(done,total)；非流式仅完成时触发 (1,1)。
    """
    # 先清洗 markdown 再校验：LLM 改写稿偶尔整篇带符号，清洗后可能变空
    raw_len = len(text or "")
    text = _strip_markdown(text)
    if raw_len and len(text) != raw_len:
        print(f"[bailian] 已清洗 markdown 符号: {raw_len} -> {len(text)} 字符")
    if not text:
        raise RuntimeError("合成文本不能为空")
    if not reference_audio_id:
        raise RuntimeError("voice_id 不能为空")

    dashscope = _ensure_api_key()
    dashscope.base_websocket_api_url = _ws_base_url()

    fmt = (output_format or COSYVOICE_FORMAT or "wav").lower().lstrip(".")
    audio_format = _fmt_enum(fmt)

    # 官方取值范围钳制（表单值可能是字符串或空，统一兜底）
    speech_rate = _clamp_num(speed, 0.5, 2.0, 1.0)
    pitch_rate = _clamp_num(pitch, 0.5, 2.0, 1.0)
    volume = int(_clamp_num(volume, 0, 100, 50))
    seed = int(_clamp_num(seed, 0, 65535, 0))

    # 情绪/风格指令：为空则传 None = 不传该参数，让模型按复刻音色本征韵律走。
    # （SDK 的 instruction 默认值就是 None，显式传 None 与不传完全等价，日志会显示为 null。）
    instruction = (instruct or "").strip() or None

    from dashscope.audio.tts_v2 import SpeechSynthesizer
    # 同一份 kwargs 同时喂给 SDK 和日志，保证「日志里看到的就是实际发出去的」。
    # 注意：官方 SDK 字段名是 speech_rate / pitch_rate（REST parameters 里叫 rate / pitch）；
    # format 传的是 AudioFormat 枚举（含采样率/位深），日志会打印其 value 以免只看到 "wav" 丢了采样率。
    synth_kwargs = dict(
        model=TARGET_MODEL,
        voice=reference_audio_id,
        format=audio_format,
        speech_rate=speech_rate,
        pitch_rate=pitch_rate,
        volume=volume,
        seed=seed,
        instruction=instruction,
    )
    print(f"[bailian] /speech 入参(实际发送): " + json.dumps(
        {k: (v.value if hasattr(v, "value") else v) for k, v in synth_kwargs.items()},
        ensure_ascii=False))
    print(f"[bailian] /speech 文本: len={len(text)} head={text[:40]!r}")
    synthesizer = SpeechSynthesizer(**synth_kwargs)

    # 非流式 call 返回完整音频 bytes
    audio_bytes = synthesizer.call(text)
    req_id = synthesizer.get_last_request_id() or ""
    first_delay = synthesizer.get_first_package_delay()
    print(f"[bailian] /speech 出参: request_id={req_id} first_package_delay={first_delay}ms bytes={len(audio_bytes or b'')}")

    if not audio_bytes:
        raise RuntimeError("百炼 CosyVoice 合成返回空音频")

    if on_progress:
        try:
            on_progress(1, 1)
        except Exception:
            pass
    return audio_bytes


def available() -> bool:
    """是否已配置可用的百炼 CosyVoice 服务。"""
    return bool(DASHSCOPE_API_KEY)
