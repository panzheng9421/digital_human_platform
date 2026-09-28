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
# 代理一律用「实例级/请求级参数」控制，绝不读写 os.environ。原因（本机实测，2026-09-06）：
#   1) 不要 pop environ：Windows 上清空 HTTP_PROXY 等变量后，urllib 的 getproxies() 会
#      fallback 到「系统注册表代理」（本机 HKCU\...\Internet Settings\ProxyServer=
#      127.0.0.1:7897），请求反而绕回被抖音风控的代理出口，报 Fresh cookies /
#      SSL UNEXPECTED_EOF。实测：pop environ → 必失败；不 pop → 成功。
#   2) ydl_opts["proxy"]="" 本身就是「显式禁用全部代理」（含注册表），这才是真直连，
#      不需要也不应该再去动 environ。
#   3) 顺带解决并发隐患：os.environ 是进程全局的，多线程 pop/restore 必然互相踩，
#      改成 per-request 参数后天然线程安全。


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


def _download_direct(url: str, dest_dir: str, no_proxy: bool = False) -> str:
    """直链下载。no_proxy=True 时禁用代理。

    用独立 Session 的 trust_env 控制代理，绝不去改进程级 os.environ——
    os.environ 是进程全局的，FastAPI 多线程并发下 pop/restore 会互相踩，
    导致"提取时好时坏"。per-request 参数天然线程安全。
    """
    ext = os.path.splitext(url.split("?")[0])[1].lower() or ".mp4"
    dest = os.path.join(dest_dir, f"dl_{int(time.time() * 1000)}{ext}")
    with requests.Session() as s:
        if no_proxy:
            s.trust_env = False  # 忽略环境变量里的代理，直连
        with s.get(url, stream=True, timeout=120) as r:
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


# 抖音页面里 JSON 数据常见字段名映射（下划线/驼峰混用）
_DOUYIN_META_KEYS = {
    "like_count": ["digg_count", "diggCount"],
    "comment_count": ["comment_count", "commentCount"],
    "share_count": ["share_count", "shareCount", "repost_count", "repostCount", "forward_count", "forwardCount"],
    "collect_count": ["collect_count", "collectCount", "save_count", "saveCount", "favorite_count", "favoriteCount"],
    "duration": ["duration"],
    "uploader": ["nickname"],
}


def _parse_douyin_meta_from_html(html: str, title: str, page_url: str) -> dict:
    """从抖音 PC 页面 HTML 中解析视频元数据（RENDER_DATA / SSR_HYDRATED_DATA / 内联 JSON）。

    抖音把视频详情（点赞/评论/转发/收藏/时长/作者等）塞在页面初始数据里，
    不需要额外请求。优先从 <script id="RENDER_DATA"> 等位置解析。
    """
    import json as _json
    import html as _html

    meta = {"title": title.replace(" - 抖音", "").strip()}

    candidates = []

    # 1) <script id="RENDER_DATA" type="application/json">
    #    抖音 PC 端这里是 percent-encoded JSON（%7B%22...），不是 HTML 实体，
    #    先 urllib.unquote 再 json.loads。
    m = re.search(r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>', html, re.S | re.I)
    if m:
        try:
            from urllib.parse import unquote
            raw = unquote(m.group(1).strip())
            candidates.append(_json.loads(raw))
        except Exception:
            pass

    # 2) window._SSR_HYDRATED_DATA
    m = re.search(r'window\._SSR_HYDRATED_DATA\s*=\s*({.*?});?</script>', html, re.S | re.I)
    if m:
        try:
            candidates.append(_json.loads(m.group(1)))
        except Exception:
            pass

    # 3) 任意内联 JSON script（含 itemInfo / videoInfo / aweme_detail / statistics）
    for script in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.S | re.I):
        txt = script.group(1)
        if not any(k in txt for k in ("aweme", "videoInfo", "itemInfo", "digg_count", "diggCount", "comment_count", "statistics")):
            continue
        try:
            body = txt.strip()
            if body.startswith("window."):
                body = body.split("=", 1)[1].strip().rstrip(";")
            data = _json.loads(body)
            if isinstance(data, (dict, list)):
                candidates.append(data)
        except Exception:
            pass

    # 递归遍历每个候选 JSON，取每个目标字段的首次有效值
    field_values: dict = {}

    def _first_int(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                s = value.strip()
                if not s:
                    return None
                # 兼容 "1.2万" 这种非纯数字（抖音页面里偶尔有格式化的）
                if s.endswith("万"):
                    s = s[:-1]
                    return int(float(s) * 10000)
                return int(float(s))
            except Exception:
                return None
        return None

    def _first_duration(value):
        dur = _first_float(value)
        if dur is None:
            return None
        # 抖音 PC 端常见 duration 字段有秒（25）和毫秒（25780）两种
        if dur > 1000:
            dur = dur / 1000.0
        return dur

    def _first_float(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except Exception:
                return None
        return None

    def _walk(node):
        if isinstance(node, dict):
            # 作者名可能在 author.nickname / user.nickname
            if "uploader" not in field_values:
                for sub_key in ("author", "user"):
                    sub = node.get(sub_key)
                    if isinstance(sub, dict):
                        nn = sub.get("nickname")
                        if isinstance(nn, str) and nn.strip():
                            field_values["uploader"] = nn.strip()
                            break
            for canonical, keys in _DOUYIN_META_KEYS.items():
                if canonical in field_values and field_values[canonical] is not None:
                    continue
                for k in keys:
                    if k in node and node[k] is not None:
                        v = node[k]
                        if canonical == "duration":
                            dur = _first_duration(v)
                            if dur is not None and dur > 0:
                                field_values[canonical] = dur
                                break
                        elif canonical == "uploader":
                            if isinstance(v, str) and v.strip():
                                field_values[canonical] = v.strip()
                                break
                        else:
                            n = _first_int(v)
                            if n is not None:
                                field_values[canonical] = n
                                break
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    for data in candidates:
        _walk(data)

    meta.update(field_values)
    # 标题兜底：如果 HTML 里没拿到就用传入 title
    if not meta.get("title"):
        meta["title"] = title.strip()
    return meta


def _probe_media_duration(path: str) -> float:
    """用 ffprobe 探测本地音视频时长（秒），失败返回 0。"""
    import subprocess
    import json as _json
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = "ffmpeg"
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    if not os.path.isfile(ffprobe):
        # imageio_ffmpeg 在 Windows 上通常是 ffmpeg.exe，ffprobe.exe 同目录
        base = os.path.dirname(ffmpeg)
        for cand in ("ffprobe.exe", "ffprobe"):
            p = os.path.join(base, cand)
            if os.path.isfile(p):
                ffprobe = p
                break
        else:
            return 0.0
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=30, check=True,
        )
        d = _json.loads(r.stdout)
        return float(d.get("format", {}).get("duration") or 0)
    except Exception:
        return 0.0


def _download_douyin_browser(url: str, dest_dir: str):
    """抖音链接下载：用真实 Chrome（DrissionPage 控制）+ 复制的登录 profile 打开视频页，
    绕过抖音对 yt-dlp/f2 的签名风控与「验证码中间页」，再从 performance 资源里抠出 DASH
    视频轨/音频轨直链，分别下载后用 ffmpeg 合并成完整 mp4。返回 (本地路径, meta)。

    关键约束（实测）：
      - 必须非 headless：headless 会被抖音识别并弹「验证码中间页」，拿不到视频。
      - 依赖 storage/chrome_profile：从已登录抖音的 Chrome 复制而来；抖音登录态过期
        后该 profile 也失效，需要重新复制（见下方 _refresh_douyin_profile 说明）。
      - 会弹出一个真实 Chrome 窗口（已用 --mute-audio 静音、--autoplay 自动播放）。
    """
    import subprocess
    try:
        from DrissionPage import ChromiumPage, ChromiumOptions
    except ImportError:
        raise RuntimeError("未安装 DrissionPage，无法使用抖音浏览器下载方案")

    chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    profile_dir = os.path.join(STORAGE_DIR, "chrome_profile")
    if not os.path.isdir(profile_dir):
        raise RuntimeError(
            "未找到 Chrome profile（storage/chrome_profile 不存在）；"
            "请先在 Chrome 登录抖音，再把 User Data 复制/软链到 storage/chrome_profile")
    if not os.path.isfile(chrome_exe):
        raise RuntimeError(f"未找到系统 Chrome：{chrome_exe}")

    opts = ChromiumOptions()
    opts.set_browser_path(chrome_exe)
    opts.set_user_data_path(profile_dir)
    opts.headless(False)  # 必须非 headless：抖音检测 headless 会弹验证码
    opts.set_argument("--disable-blink-features=AutomationControlled")
    opts.set_argument("--autoplay-policy=no-user-gesture-required")
    opts.set_argument("--mute-audio")

    # 直链防盗链需要 referer + cookie（抖音 v11/v26-web.douyinvod.com 校验 Referer）
    ck = {}
    cf = _resolve_cookiefile()
    if cf and os.path.exists(cf):
        for line in open(cf, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) >= 7:
                ck[p[5]] = p[6]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://www.douyin.com/",
    }

    tmp_v = os.path.join(dest_dir, "_dy_video.mp4")
    tmp_a = os.path.join(dest_dir, "_dy_audio.m4a")
    out = os.path.join(dest_dir, "douyin_video.mp4")  # 默认占位，下面会根据 video_id 重算唯一名

    def _dl(u, path):
        with requests.get(u, headers=headers, cookies=ck, timeout=600, stream=True) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

    def _grab() -> tuple:
        # 1) 优先从页面渲染后的 HTML 抠直链：抖音把视频直链放在页面数据（RENDER_DATA / url_list）
        #    里，直链是音视频合并流（play_addr），不依赖 <video> 元素渲染或自动播放，
        #    对短链 / 未自动播放的视频都稳。直链形式为 https://vXX-web.douyinvod.com/...
        html = getattr(page, "html", "") or ""
        raw = re.findall(r'https?://[^\s"\'\\<>]+?douyinvod\.com[^\s"\'\\<>]*', html)
        urls = []
        for u in raw:
            u = u.replace("&amp;", "&")  # HTML 实体解码
            if u not in urls:
                urls.append(u)
        video = audio = None
        for u in urls:
            if "/audio/" in u:
                audio = audio or u
            elif "v26-web" in u or "/video/" in u:
                video = video or u
        if video:
            return video, audio
        # 2) 兜底：performance API 枚举已请求资源（视频自动播放后才出现 DASH 分片）
        js = """
        var es = performance.getEntriesByType('resource');
        var v_avc='',v_hvc='',a='';
        for (var i=0;i<es.length;i++){ var u=es[i].name;
          if (/media-video-avc/.test(u) && !v_avc) v_avc=u;
          else if (/media-video-hvc1/.test(u) && !v_hvc) v_hvc=u;
          else if (/media-audio-und-mp4a|media-audio/.test(u) && !a) a=u;
        }
        return {v: v_avc||v_hvc, a:a};
        """
        r = page.run_js(js) or {}
        return r.get("v") or "", r.get("a") or ""

    page = ChromiumPage(addr_or_opts=opts)
    try:
        page.get(url, timeout=30)
        time.sleep(4)
        page.run_js("document.querySelectorAll('video').forEach(function(v){try{v.muted=true;v.play()}catch(e){}});")
        time.sleep(8)
        title = page.title or ""
        html = getattr(page, "html", "") or ""
        meta = _parse_douyin_meta_from_html(html, title, page.url or url)
        if "验证码" in title or "登录" in title:
            raise RuntimeError(
                "抖音弹出验证码/登录页：storage/chrome_profile 登录态已过期；"
                "请在 Chrome 登录抖音后，把 User Data 重新复制/覆盖到 storage/chrome_profile")
        vurl, aurl = _grab()
        if not vurl:  # 首轮没拿到（页面懒加载/未自动播放），触发播放后再试
            page.run_js("document.querySelectorAll('video').forEach(function(v){try{v.muted=true;v.play()}catch(e){}});")
            time.sleep(12)
            vurl, aurl = _grab()
        if not vurl:
            raise RuntimeError("未能从页面提取到视频直链（抖音可能改版，请反馈）")
        # 生成唯一输出文件名：用 video_id + 标题 slug + 时间戳，避免多次提取互相覆盖
        final_url = page.url or url
        m_id = re.search(r"/video/(\d+)", final_url) or re.search(r"modal_id=(\d+)", final_url)
        aweme_id = m_id.group(1) if m_id else time.strftime("%Y%m%d_%H%M%S")
        raw_title = title.replace(" - 抖音", "").strip().split("#")[0].strip()
        slug = re.sub(r"[^\w\s-]", "_", raw_title, flags=re.U)[:30]
        slug = re.sub(r"[\s_]+", "_", slug).strip("_") or "dy"
        out = os.path.join(dest_dir, f"douyin_{aweme_id}_{slug}_{time.strftime('%H%M%S')}.mp4")
        _dl(vurl, tmp_v)
        if aurl:
            _dl(aurl, tmp_a)
    finally:
        page.quit()

    # ffmpeg 合并：优先 h264 视频轨（兼容性更好）；缺音轨则只拷视频
    try:
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG = "ffmpeg"
    if os.path.exists(tmp_a) and os.path.getsize(tmp_a) > 0:
        cmd = [FFMPEG, "-y", "-i", tmp_v, "-i", tmp_a, "-c", "copy", out]
    else:
        cmd = [FFMPEG, "-y", "-i", tmp_v, "-c", "copy", out]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)

    for t in (tmp_v, tmp_a):
        if os.path.exists(t):
            try:
                os.remove(t)
            except Exception:
                pass

    # 如果页面数据里没拿到时长，用 ffprobe 探测本地视频兜底
    if not meta.get("duration") and os.path.exists(out):
        meta["duration"] = _probe_media_duration(out)
    if not meta.get("title"):
        meta["title"] = title.replace(" - 抖音", "").strip()
    return out, meta


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

    网络策略：抖音系域名依次尝试 [直连, 走代理]，谁先成用谁。
    直连靠 ydl_opts["proxy"]="" 显式禁用全部代理；**不要**去清空 os.environ——Windows 上清空后
    urllib 会 fallback 到系统注册表代理（本机 127.0.0.1:7897），反而绕回被风控的出口
    （详见文件头注释与 2026-09-06 实测：pop environ 必失败、不 pop 才成）。
    实测「直连/走代理」哪条能通会随抖音风控波动（2026-09-06 这次是直连失败、走代理成功），
    所以保留两种都试的回退顺序，别写死单一策略。
    下载后校验文件确为音视频，避免把登录墙 html 送百炼产生误导性 400。
    """
    cookiefile = _resolve_cookiefile()
    if cookiefile:
        print(f"[asr] 使用 cookie 文件: {cookiefile}")
    else:
        print("[asr] 未找到 cookie 文件，回退读取本机 Chrome 登录态（可能受 Chrome ABE 限制失败）")

    # 抖音系：yt-dlp/f2 已被抖音签名风控掐死（长链进 DouyinIE 也 403、短链不被解析），
    # 优先走浏览器下载方案（真实 Chrome + 登录 profile 绕过验证码中间页与签名风控）。
    if _should_bypass_proxy(url):
        try:
            return _download_douyin_browser(url, dest_dir)
        except Exception as e:
            print(f"[asr] 抖音浏览器方案失败，回退 yt-dlp/直链: {_short_err(e)}")

    # 尝试顺序：抖音系 -> [直连, 代理]；其他站点 -> [保持当前环境]
    orders = [True, False] if _should_bypass_proxy(url) else [False]
    errs = []

    for no_proxy in orders:
        tag = "直连(绕过代理)" if no_proxy else "走代理"
        try:
            # 代理由 _download_ytdlp 的 ydl_opts["proxy"] 控制，不动 os.environ
            return _download_ytdlp(url, dest_dir, cookiefile, no_proxy)
        except Exception as e:
            errs.append(f"yt-dlp {tag}失败: {_short_err(e)}")
            print(f"[asr] yt-dlp {tag}失败：{_short_err(e)}")

    # yt-dlp 全失败 -> 直链回退（同样按上面的顺序各试一次）
    for no_proxy in orders:
        tag = "直连(绕过代理)" if no_proxy else "走代理"
        try:
            return _download_direct(url, dest_dir, no_proxy=no_proxy), {}
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
        # 本地视频保留（老板要求：提取到的视频不删），仅打印留存位置便于后续复用，不做清理
        if path and os.path.exists(path):
            print(f"[asr] 本地视频已保留(未清理): {path}")


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
