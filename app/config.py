"""全局配置。所有 AI 推理相关的密钥/端点都集中在这里，方便以后接入真实服务。"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
DB_PATH = os.path.join(BASE_DIR, "app.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 子目录
for _d in ["audios", "avatars", "videos", "edits", "covers", "timbre", "temp"]:
    os.makedirs(os.path.join(STORAGE_DIR, _d), exist_ok=True)

# JWT
import secrets as _secrets
SECRET_KEY = os.environ.get("DH_SECRET_KEY")
if not SECRET_KEY:
    # 生产必须设置 DH_SECRET_KEY；本地开发可设 ALLOW_INSECURE_KEY=1 放行（用临时随机密钥，重启失效）
    if os.environ.get("ALLOW_INSECURE_KEY") == "1":
        import warnings
        warnings.warn("DH_SECRET_KEY 未设置，使用临时随机密钥（仅开发模式，重启后旧 token 失效）")
        SECRET_KEY = _secrets.token_hex(32)
    else:
        raise RuntimeError(
            "安全启动被拒绝：未设置环境变量 DH_SECRET_KEY。"
            "生产环境必须配置；本地开发可设 ALLOW_INSECURE_KEY=1 放行。"
        )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 天

# ===== AI 推理替换位（生产环境填入真实服务）=====
# 1) 文案生成 / 改写：可接入 DeepSeek / GPT / 通义千问 等 LLM
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

# 2) 配音（语音合成）：可接入 CosyVoice2 / 火山/即梦 TTS
TTS_ENABLED = os.environ.get("TTS_ENABLED", "0") == "1"   # 0=占位音频, 1=接真实TTS
TTS_API_URL = os.environ.get("TTS_API_URL", "")

# 2.5) 配音（声音克隆）：CosyVoice2 on PAI-EAS —— 替代 fish+asr 的真实零样本克隆
#    端点形如 http://cosyvoice001.<id>.cn-hangzhou.pai-eas.aliyuncs.com/
#    鉴权：Authorization: Bearer <COSYVOICE_TOKEN>
COSYVOICE_ENABLED = os.environ.get("COSYVOICE_ENABLED", "0") == "1"
COSYVOICE_ENDPOINT = os.environ.get("COSYVOICE_ENDPOINT", "").rstrip("/")
COSYVOICE_TOKEN = os.environ.get("COSYVOICE_TOKEN", "")
COSYVOICE_FORMAT = os.environ.get("COSYVOICE_FORMAT", "wav")          # wav | mp3
# 合成模式已固定在 cosyvoice_client.SYNTHESIS_MODE，不在此处配置（避免误配导致音色漂移）
# 模型路径以线上服务实际值为准（openapi 默认即此路径）
COSYVOICE_MODEL = os.environ.get("COSYVOICE_MODEL",
                                 "/nasmnt/models/pretrained_models/CosyVoice2-0.5B")

# 2.6) 文案提取 ASR：阿里百炼 Paraformer-v2（录音文件识别，异步）
#     API Key 即百炼 / DashScope 的 API-KEY；从环境变量读取，勿硬编码到代码
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
# 2.7) 阿里百炼语音合成 / 声音复刻（替换自部署 EAS CosyVoice）
#     业务空间 Workspace ID：形如 ws-xxxx 或空白走默认公共 endpoint
DASHSCOPE_WORKSPACE_ID = os.environ.get("DASHSCOPE_WORKSPACE_ID", "")
DASHSCOPE_TTS_MODEL = os.environ.get("DASHSCOPE_TTS_MODEL", "cosyvoice-v3.5-plus")
DASHSCOPE_VOICE_ENROLLMENT_MODEL = os.environ.get("DASHSCOPE_VOICE_ENROLLMENT_MODEL", "voice-enrollment")

# 3) 数字人（对口型视频）：
#    AVATAR_PROVIDER = "mock"    本地产出占位视频（无需 GPU，开发/演示用）
#    AVATAR_PROVIDER = "heygem"  调用 PAI-EAS 上部署的 HeyGem / duix.avatar（需先部署 Docker 镜像）
AVATAR_PROVIDER = os.environ.get("AVATAR_PROVIDER", "mock")

# HeyGem / duix.avatar（Docker 路线，备选）
HEYGEM_ENDPOINT = os.environ.get("HEYGEM_ENDPOINT", "")
HEYGEM_TOKEN = os.environ.get("HEYGEM_TOKEN", "")

# duix.avatar 容器内接口前缀：真实镜像为 /easy/submit、/easy/query。
# EAS 会把完整路径（含 /api/predict/eas001）透传给容器，serve_oss.py 入口已做 catch-all 兼容。
HEYGEM_PATH_PREFIX = os.environ.get("HEYGEM_PATH_PREFIX", "easy")
# 旧的「平台对外基址」方案已废弃（本地平台 localhost EAS 拉不到）。统一改走 OSS 中转。
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")

# 4) OSS 中转（本地平台 <-> 云端 EAS 的文件桥）
#    输入：用户上传的音频/形象先传 OSS，生成 URL 给 EAS 拉取；
#    输出：EAS 跑完把结果回传到 OSS，平台再下载回本地 storage。
#    凭据从环境变量读取（start.bat / EAS 服务环境变量）。
OSS_BUCKET = os.environ.get("OSS_BUCKET", "")
OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com")  # 公网 endpoint
OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
OSS_REGION = os.environ.get("OSS_REGION", "cn-hangzhou")
# True=桶公共读，返回公网直链；False=返回签名 URL（更安全，默认）
OSS_PUBLIC_READ = os.environ.get("OSS_PUBLIC_READ", "0") == "1"

# 内网 endpoint：EAS 容器默认无公网出口，必须用同区域内网 endpoint 访问 OSS；
# 但回传给本地平台的「结果 URL」必须是公网 host（本地机器在阿里云外，拉不到内网）。
# 留空则根据 OSS_ENDPOINT 自动推导（在 .aliyuncs.com 前插入 -internal）。
OSS_ENDPOINT_INTERNAL = os.environ.get("OSS_ENDPOINT_INTERNAL", "")
if not OSS_ENDPOINT_INTERNAL:
    _h = OSS_ENDPOINT
    for _s in ("https://", "http://"):
        if _h.startswith(_s):
            _h = _h[len(_s):]
    if ".aliyuncs.com" in _h and "-internal" not in _h:
        OSS_ENDPOINT_INTERNAL = _h.replace(".aliyuncs.com", "-internal.aliyuncs.com")
    else:
        OSS_ENDPOINT_INTERNAL = _h

# 4) 发布：可接入抖音/视频号开放平台
PUBLISH_ENABLED = os.environ.get("PUBLISH_ENABLED", "0") == "1"

# 买断套餐默认配置
DEFAULT_MONTHLY_QUOTA = 500  # 每客户月额度（条）
PER_USER_CONCURRENCY = 2     # 每用户并发路数（防账号共享/薅GPU）
