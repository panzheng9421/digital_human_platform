"""媒体工具：生成真实可播放的音频/视频/封面。
- 音频：用 wave 合成可播放 WAV（占位配音，生产替换为 CosyVoice/TTS）
- 视频：用 imageio-ffmpeg 自带 ffmpeg 合成（图像+音频），失败则降级
- 封面：用 Pillow 绘制
"""
import os
import re
import glob
import wave
import struct
import math
import random
import subprocess
from PIL import Image, ImageDraw, ImageFont

try:
    import imageio_ffmpeg
    _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _FFMPEG = None


def _pil_font(size):
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsum.ttc",
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    return ImageFont.load_default()


def gen_wav(text: str, emotion: str = "自然", speed: float = 1.0, out_path: str = None) -> str:
    """合成占位配音 WAV（可播放）。生产替换为真实 TTS。"""
    if out_path is None:
        raise ValueError("out_path required")
    # 时长 ~ 字数 * 0.16s / speed，最短 3s
    dur = max(3.0, len(text) * 0.16 / max(0.5, speed))
    sr = 24000
    n = int(dur * sr)
    # 不同情绪不同基频/振幅包络，制造听感差异
    base = {"自然": 180, "高兴": 240, "伤心": 140, "嫌弃": 200, "说教": 160,
            "激动": 260, "生气": 220}.get(emotion, 180)
    amp = 0.25
    frames = bytearray()
    for i in range(n):
        t = i / sr
        # 音节包络（模拟说话的顿挫）
        env = (0.5 + 0.5 * abs(math.sin(2 * math.pi * 4 * t))) * amp
        # 基频 + 两个谐波 + 轻微随机抖动
        v = (math.sin(2 * math.pi * base * t)
             + 0.3 * math.sin(2 * math.pi * base * 2 * t)
             + 0.2 * math.sin(2 * math.pi * (base * 3 + random.uniform(-5, 5)) * t)) * env
        vi = int(max(-1, min(1, v)) * 32767)
        frames += struct.pack("<h", vi)
    with wave.open(out_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))
    return out_path


def make_talking_video(image_path: str, audio_path: str, out_path: str) -> dict:
    """合成口播视频：静态形象 + 配音。返回 {video_path, poster_path}。"""
    poster_path = out_path.replace(".mp4", "_poster.jpg")
    try:
        im = Image.open(image_path).convert("RGB")
        im.thumbnail((720, 1280))
        bg = Image.new("RGB", (720, 1280), (20, 20, 28))
        bg.paste(im, ((720 - im.width) // 2, (1280 - im.height) // 2))
        bg.save(poster_path, quality=85)
    except Exception:
        poster_path = image_path

    if not _FFMPEG:
        # 无 ffmpeg：返回海报+音频，前端以"静态形象+配音"方式预览
        return {"video_path": None, "poster_path": poster_path, "audio_path": audio_path,
                "note": "本环境无 ffmpeg，数字人视频以静态形象+配音预览；上云部署可生成真实 MP4"}

    try:
        cmd = [
            _FFMPEG, "-y", "-loop", "1", "-i", poster_path,
            "-i", audio_path, "-c:v", "libx264", "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart", out_path
        ]
        import subprocess
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            return {"video_path": out_path, "poster_path": poster_path}
    except Exception as e:
        pass
    return {"video_path": None, "poster_path": poster_path, "audio_path": audio_path,
            "note": "视频编码失败，已降级为静态形象+配音预览"}


def _split_sentences(text):
    """文案拆句：按标点切分，超短句合并，最多取 14 句。"""
    if not text:
        return []
    text = re.sub(r"\s+", "", text)
    parts = re.split(r"(?<=[。！？!?；;])", text)
    parts = [p for p in parts if p.strip()]
    out = []
    for p in parts:
        if out and len(out[-1]) <= 4 and len(out[-1]) + len(p) <= 18:
            out[-1] += p
        else:
            out.append(p)
    return out[:14]


def _probe_video_meta(path):
    """探测视频分辨率、帧率、时长、音频流。返回 dict，失败返回 {}。
    关键：必须拿到真实 fps，否则 zoompan 强制 25fps 会改变视频播放速度，
    导致音视频不同步（口型对不上）。
    """
    meta = {}
    if not _FFMPEG:
        return meta
    try:
        # 先尝试 ffprobe（imageio_ffmpeg 通常在同一目录提供）
        ffprobe = None
        base = os.path.dirname(_FFMPEG)
        for name in ("ffprobe", "ffprobe.exe"):
            cand = os.path.join(base, name)
            if os.path.exists(cand):
                ffprobe = cand
                break
        if ffprobe:
            p = subprocess.run([
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration,r_frame_rate",
                "-of", "default=noprint_wrappers=1", path
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
            txt = p.stdout.decode(errors="ignore")
            for line in txt.splitlines():
                if line.startswith("width="):
                    meta["width"] = int(line.split("=", 1)[1])
                elif line.startswith("height="):
                    meta["height"] = int(line.split("=", 1)[1])
                elif line.startswith("duration="):
                    try:
                        meta["duration"] = float(line.split("=", 1)[1])
                    except Exception:
                        pass
                elif line.startswith("r_frame_rate="):
                    try:
                        num, den = line.split("=", 1)[1].split("/")
                        fps = float(num) / float(den)
                        if fps > 0:
                            meta["fps"] = fps
                    except Exception:
                        pass
        # 兜底：imageio_ffmpeg 不打包 ffprobe，所以从 ffmpeg -i 的 stderr 解析
        # 真实宽高、时长与帧率（避免写死 720x1280 把横屏视频误裁成竖屏）
        need_more = ("width" not in meta or "height" not in meta or
                     meta.get("duration") is None or meta.get("fps") is None)
        if need_more:
            p = subprocess.run([_FFMPEG, "-i", path], stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=30)
            txt = p.stderr.decode(errors="ignore")
            if meta.get("duration") is None:
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
                if m:
                    meta["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if "width" not in meta or "height" not in meta:
                m2 = re.search(r"Stream #\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})", txt)
                if m2:
                    meta["width"] = int(m2.group(1))
                    meta["height"] = int(m2.group(2))
                else:
                    meta["width"], meta["height"] = 1280, 720
            if meta.get("fps") is None:
                # 解析 "30 fps" 或 "29.97 fps"
                m3 = re.search(r"(\d+(?:\.\d+)?)\s*fps", txt)
                if m3:
                    meta["fps"] = float(m3.group(1))
        # 探测是否有音频流（供后面 -map 0:a:0 决策）
        if "has_audio" not in meta:
            p2 = subprocess.run([_FFMPEG, "-i", path], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=30)
            txt2 = (p2.stderr or b"").decode(errors="ignore")
            meta["has_audio"] = bool(re.search(r"Stream #\d+:\d+.*?Audio:", txt2))
    except Exception:
        pass
    return meta


def _probe_duration(path):
    """用 ffmpeg 探测视频时长（秒）；失败返回 0。"""
    return _probe_video_meta(path).get("duration") or 0


def _ass_layout(width, height):
    """按视频宽高返回 ASS 字幕排版参数。
    竖屏(9:16)大字居底、短行；横屏(16:9)字号略小、行更长；1:1 居中。
    """
    ratio = width / height if height else 0.5625
    if ratio < 0.85:          # 竖屏 9:16
        return {
            "play_x": 720, "play_y": 1280,
            "font_size": 62, "chars_per_line": 9,
            "margin_v": 150, "margin_lr": 40,
            "outline": 4, "shadow": 1,
            "note": "竖屏 9:16 字幕排版",
        }
    if ratio > 1.18:          # 横屏 16:9
        return {
            "play_x": 1280, "play_y": 720,
            "font_size": 50, "chars_per_line": 14,
            "margin_v": 70, "margin_lr": 60,
            "outline": 3, "shadow": 1,
            "note": "横屏 16:9 字幕排版",
        }
    # 接近 1:1
    return {
        "play_x": 1080, "play_y": 1080,
        "font_size": 56, "chars_per_line": 11,
        "margin_v": 100, "margin_lr": 50,
        "outline": 4, "shadow": 1,
        "note": "方形 1:1 字幕排版",
    }


_PUNCT_RE = re.compile(r"[，。！？、；：,.!?;:\s（）()\[\]【】\"'…—\-/\\]")


def _strip_punct(s):
    """去掉字幕里的中英文标点与空白，只留文字/数字（网感大字不要逗号句号）。"""
    return _PUNCT_RE.sub("", (s or "")).strip()


def _chunk_text(text, max_chars):
    """把长句切成 <= max_chars 的短块，优先按逗号/顿号/分号/冒号切，避免孤立标点或单字碎片。"""
    text = text.strip()
    if not text:
        return []
    # 先按自然气口切，保留句尾标点
    parts = re.split(r"(?<=[，、；：])", text)
    parts = [p for p in parts if p.strip()]
    raw = []
    for p in parts:
        if len(p) <= max_chars:
            raw.append(p)
            continue
        # 去掉末尾标点再硬切，避免把标点孤零零留在最后
        tail_punct = ""
        if p[-1] in "，、；：":
            tail_punct = p[-1]
            p = p[:-1]
        n = len(p)
        if n <= max_chars:
            raw.append(p + tail_punct)
            continue
        full = n // max_chars
        rem = n % max_chars
        if rem == 0 or rem >= 4:
            # 常规切分，余数 >=4 单独成段
            for i in range(0, full * max_chars, max_chars):
                raw.append(p[i:i + max_chars])
            if rem:
                raw.append(p[full * max_chars:] + tail_punct)
        elif full == 1:
            # 只有两段可能且余数太少：直接按 max_chars 硬切（避免单段超长溢出）
            raw.append(p[:max_chars])
            raw.append(p[max_chars:] + tail_punct)
        else:
            # 余数 <4 且段数 >=2：把余数合并到上一段，避免最后只剩几个字
            for i in range(0, (full - 1) * max_chars, max_chars):
                raw.append(p[i:i + max_chars])
            raw.append(p[(full - 1) * max_chars:] + tail_punct)
    # 合并孤立碎片（<=3 字且含标点）到上一块
    out = []
    for c in raw:
        if not out:
            out.append(c)
            continue
        if (len(c) <= 3 and c[-1] in "，、；：。！？!?；;:" and
                len(out[-1]) + len(c) <= max_chars + 3):
            out[-1] += c
        else:
            out.append(c)
    return out


def _ffpath(p):
    """转 ffmpeg 滤镜内路径：正斜杠 + 冒号转义 + 单引号包裹。
    关键：Windows 绝对路径带盘符冒号(C:/...)，滤镜里冒号是选项分隔符，
    必须转义为 '\\:' 否则 ffmpeg 报 'No option name near' 直接失败。"""
    return "'" + p.replace("\\", "/").replace(":", "\\:") + "'"


def _resolve_cjk_font():
    """解析中文字体，用于 ASS(libass) 与 drawtext。
    返回 {"fontfile": 绝对路径} 或 {"fontname": 家族名}；都找不到返回 None。
    优先级：① 项目自带 assets/fonts ② 系统已装字体 ③ fc-list 任意中文家族名。
    找不到时调用方应优雅降级（跳过大字，绝不烧录出方块字）。
    """
    # 1) 项目自带字体目录（部署时往这里丢一个 .ttf/.ttc/.otf 即自动生效，免装系统包）
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (os.path.join(here, "assets", "fonts"),
                os.path.join(here, "..", "assets", "fonts")):
        if os.path.isdir(rel):
            for ext in ("*.ttc", "*.ttf", "*.otf"):
                hits = glob.glob(os.path.join(rel, ext))
                if hits:
                    return {"fontfile": hits[0]}

    # 2) Windows 系统字体
    if os.name == "nt":
        for c in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                  "C:/Windows/Fonts/simsum.ttc"):
            if os.path.exists(c):
                return {"fontfile": c}
        return {"fontname": "Microsoft YaHei"}

    # 3) Linux 常见中文字体绝对路径
    linux_paths = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]
    for c in linux_paths:
        if os.path.exists(c):
            return {"fontfile": c}

    # 4) 用 fc-list 找任意中文家族名，交给 fontconfig 解析
    try:
        out = subprocess.run(["fc-list", ":lang=zh"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10).stdout.decode(errors="ignore")
        for line in out.splitlines():
            fam = line.split(":")[1].strip() if ":" in line else ""
            if fam:
                return {"fontname": fam}
    except Exception:
        pass
    return None


def _make_ass(sentences, dur, out_path, font=None, meta=None):
    """生成网感大字 ASS 字幕：按视频比例排版，长句切块，逐行出现。
    meta: _probe_video_meta() 返回值，用于 9:16/16:9/1:1 自适应字号/行宽/位置。
    """
    layout = _ass_layout(meta.get("width", 720), meta.get("height", 1280)) if meta else _ass_layout(720, 1280)

    # 把文案全部切成短行，按块数重新分配总时长
    chunks = []
    for s in sentences:
        chunks.extend(_chunk_text(s, layout["chars_per_line"]))
    n = len(chunks) or 1
    seg = max(0.8, dur / n)

    def ts(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    style_line = (
        "Style: big,Microsoft YaHei,{font},{pri},{outl},{back},-1,0,{outline},{shadow},2,{ml},{mr},{mv}"
        .format(
            font=layout["font_size"],
            pri="&H00FFFFFF", outl="&H00000000", back="&H64000000",
            outline=layout["outline"], shadow=layout["shadow"],
            ml=layout["margin_lr"], mr=layout["margin_lr"], mv=layout["margin_v"],
        )
    )
    lines = [
        "[Script Info]", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {layout['play_x']}", f"PlayResY: {layout['play_y']}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
        style_line,
        "", "[Events]",
        "Format: Layer, Start, End, Style, Text",
    ]
    # 用解析到的中文字体家族名替换默认的 Microsoft YaHei
    if font:
        fam = None
        if font.get("fontfile"):
            try:
                fam = ImageFont.truetype(font["fontfile"]).getname()[0]
            except Exception:
                fam = os.path.splitext(os.path.basename(font["fontfile"]))[0]
        elif font.get("fontname"):
            fam = font["fontname"]
        if fam:
            parts = lines[8].split(",")
            parts[1] = fam
            lines[8] = ",".join(parts)
    for i, txt in enumerate(chunks):
        # 每行作为一个独立 Dialogue，首尾相接，实现"一行行把字打出来"
        start = i * seg
        end = min(dur, (i + 1) * seg)
        lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},big,{_strip_punct(txt)}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def _make_ass_ts(items, out_path, font=None, meta=None):
    """用百炼 ASR 真实时间戳生成网感大字 ASS 字幕；长句按字数切块，逐行出现。
    items: [{text, begin(ms), end(ms)}]。
    """
    layout = _ass_layout(meta.get("width", 720), meta.get("height", 1280)) if meta else _ass_layout(720, 1280)

    def ts(ms):
        t = max(0.0, float(ms) / 1000.0)
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    style_line = (
        "Style: big,Microsoft YaHei,{font},{pri},{outl},{back},-1,0,{outline},{shadow},2,{ml},{mr},{mv}"
        .format(
            font=layout["font_size"],
            pri="&H00FFFFFF", outl="&H00000000", back="&H64000000",
            outline=layout["outline"], shadow=layout["shadow"],
            ml=layout["margin_lr"], mr=layout["margin_lr"], mv=layout["margin_v"],
        )
    )
    lines = [
        "[Script Info]", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {layout['play_x']}", f"PlayResY: {layout['play_y']}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
        style_line,
        "", "[Events]",
        "Format: Layer, Start, End, Style, Text",
    ]
    if font:
        fam = None
        if font.get("fontfile"):
            try:
                fam = ImageFont.truetype(font["fontfile"]).getname()[0]
            except Exception:
                fam = os.path.splitext(os.path.basename(font["fontfile"]))[0]
        elif font.get("fontname"):
            fam = font["fontname"]
        if fam:
            parts = lines[8].split(",")
            parts[1] = fam
            lines[8] = ",".join(parts)

    # 把每个 ASR 句子按字数切块，块内按线性插值分配时间戳
    for it in items:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        begin = int(it.get("begin", 0) or 0)
        end = int(it.get("end", 0) or 0)
        if end <= begin:
            end = begin + 2000
        chunks = _chunk_text(text, layout["chars_per_line"])
        if not chunks:
            continue
        n = len(chunks)
        span = end - begin
        for i, chunk in enumerate(chunks):
            b = begin + int(span * i / n)
            e = begin + int(span * (i + 1) / n) if i < n - 1 else end
            lines.append(f"Dialogue: 0,{ts(b)},{ts(e)},big,{_strip_punct(chunk)}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def edit_video(in_path: str, poster_path: str, options: dict, audio_path: str, out_path: str, script_text: str = None) -> dict:
    has_video = bool(in_path) and os.path.exists(in_path)
    src_video = in_path if has_video else None
    subtitle_note = None
    # 若只有海报+音频，先合成基础视频
    if not src_video and poster_path:
        base = out_path.replace(".mp4", "_base.mp4")
        r = make_talking_video(poster_path, audio_path, base) if audio_path else None
        if r and r.get("video_path"):
            src_video = r["video_path"]
    if not src_video:
        # 完全降级：直接返回海报
        return {"video_path": None, "poster_path": poster_path, "options": options}

    vid_meta = _probe_video_meta(src_video)
    vid_dur = vid_meta.get("duration") or 0  # 视频真实时长（秒），供字幕卡点与 BGM 长度使用

    # 构造 ffmpeg 滤镜链
    vf = []
    if options.get("color"):       # 自动调色
        vf.append("eq=contrast=1.12:saturation=1.25:brightness=0.03")
    if options.get("mg"):          # MG动画（轻微缩放），输出尺寸与帧率跟随原视频，绝不强制 25fps 导致音画不同步
        z_w, z_h = vid_meta.get("width", 1280), vid_meta.get("height", 720)
        fps = vid_meta.get("fps") or 25
        vf.append(f"zoompan=z='min(zoom+0.0005,1.05)':d=1:s={z_w}x{z_h}:fps={fps},format=yuv420p")
    # 字幕：默认常驻普通字幕（即此前勾选"网感大字"调出的效果——百炼 ASR 对齐 + 文案均分 + 去标点）。
    # 定版（老板 2026-09-02）：不勾选"网感大字" = 之前调试效果 = 普通字幕；
    # 勾选"网感大字" = 未来设计的爆款标题大字（当前尚未实现，勾选仍走普通字幕）。
    # 因此现在字幕始终生成（普通字幕），bigtext 仅作"未来爆款"标记位保留。
    # 日后做爆款大字：在 if bigtext 分支替换为 _make_ass_bigtext* 生成即可，其余逻辑不变。
    _font = _resolve_cjk_font()
    if not _font:
        print("[warn] 未找到中文字体，跳过字幕（避免方块字）。"
              "请在 ECS 执行: apt-get install -y fonts-wqy-zenhei 或往 app/assets/fonts 丢一个中文字体")
        subtitle_note = "未找到中文字体，已跳过字幕（避免方块字）"
    else:
        subs_ok = False
        # 1) 优先：对配音/视频做百炼 ASR，拿真实时间戳对齐字幕
        subs_src = audio_path or src_video
        if subs_src and os.path.exists(subs_src):
            try:
                from app.services import asr_client as ac
                if ac.asr_align_available():
                    print(f"[edit] 尝试百炼 ASR 对齐字幕: {subs_src}")
                    items = ac.transcribe_file_ts(subs_src)
                    if items:
                        _ass = out_path.replace(".mp4", "_subs.ass")
                        if options.get("bigtext"):
                            pass  # TODO: 未来爆款大字（_make_ass_bigtext_ts）
                        _make_ass_ts(items, _ass, _font, vid_meta)
                        sub = "subtitles=" + _ffpath(_ass)
                        if _font.get("fontfile"):
                            sub += ":fontsdir=" + _ffpath(os.path.dirname(_font["fontfile"]))
                        vf.append(sub)
                        subtitle_note = "已生成字幕（已按语速对齐）"
                        subs_ok = True
            except Exception as e:
                print("[warn] ASR 对齐失败，回退按句均分:", str(e)[:200])
        # 2) 回退：按文案均分（无 ASR / 无 OSS / 超时）
        if not subs_ok:
            sents = _split_sentences(script_text) if script_text else []
            if sents:
                _dur = vid_dur or (len(sents) * 2.5)
                _ass = out_path.replace(".mp4", "_subs.ass")
                if options.get("bigtext"):
                    pass  # TODO: 未来爆款大字（_make_ass_bigtext）
                _make_ass(sents, _dur, _ass, _font, vid_meta)
                sub = "subtitles=" + _ffpath(_ass)
                if _font.get("fontfile"):
                    sub += ":fontsdir=" + _ffpath(os.path.dirname(_font["fontfile"]))
                vf.append(sub)
                subtitle_note = "字幕按句均分（未启用 ASR 对齐）"
            else:
                ft = (":fontfile=" + _ffpath(_font["fontfile"])) if _font.get("fontfile") \
                    else (":font=" + _font["fontname"] if _font.get("fontname") else ":font=sans-serif")
                vf.append("drawtext=text='划重点':fontcolor=white:fontsize=72:box=1:boxcolor=red@0.6:"
                          "x=(w-text_w)/2:y=h*0.78" + ft)
                subtitle_note = "未读取到文案，字幕为占位「划重点」"
    vf_str = ",".join(vf) if vf else "null"

    # BGM：生成一段与视频等长的轻柔背景音并混流（避免 amix=shortest 把视频截断成 8 秒）
    bgm_path = None
    if options.get("bgm"):
        bgm_path = out_path.replace(".mp4", "_bgm.wav")
        _gen_bgm(bgm_path, max(8.0, vid_dur))

    if not _FFMPEG:
        return {"video_path": None, "poster_path": poster_path, "options": options,
                "note": "无 ffmpeg，剪辑以静态预览呈现"}

    stderr_log = ""
    try:
        # 统一用 filter_complex 显式命名 [v] 视频流与 [a] 音频流，避免 -map 0 与 -vf 冲突导致滤镜被吞。
        # 无 BGM 时音频 copy，不再重新编码，最大程度保留原始时间戳，防止口型错位。
        has_audio = vid_meta.get("has_audio", True)
        if bgm_path:
            # 对 BGM 先 highpass 砍掉 <200Hz 低频 hum，再 lowpass 削刺耳高频，整体压到 0.5；
            # 与原声 amix 时 BGM 权重 0.18，确保不盖人声。
            filter_complex = (f"[0:v]{vf_str}[v];"
                              f"[1:a]highpass=f=200,lowpass=f=7000,volume=0.5[bg];"
                              f"[0:a][bg]amix=inputs=2:duration=first:weights='1 0.18'[a]")
            cmd = [_FFMPEG, "-y", "-i", src_video, "-i", bgm_path,
                   "-filter_complex", filter_complex,
                   "-map", "[v]", "-map", "[a]",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                   "-movflags", "+faststart", out_path]
        elif has_audio:
            filter_complex = f"[0:v]{vf_str}[v]"
            cmd = [_FFMPEG, "-y", "-i", src_video,
                   "-filter_complex", filter_complex,
                   "-map", "[v]", "-map", "0:a:0",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy",
                   "-movflags", "+faststart", out_path]
        else:
            cmd = [_FFMPEG, "-y", "-i", src_video,
                   "-vf", vf_str,
                   "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", out_path]
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
        stderr_log = (p.stderr or b"").decode(errors="ignore")[-1500:]
        if p.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            r = {"video_path": out_path, "poster_path": poster_path, "options": options}
            if subtitle_note:
                r["note"] = subtitle_note
            return r
    except Exception as e:
        stderr_log = str(e) + "\n" + stderr_log
    # 失败降级：复制原视频
    try:
        import shutil
        shutil.copy(src_video, out_path)
        note = "剪辑处理失败，已保留原视频"
        if stderr_log:
            note += "；ffmpeg: " + stderr_log[:200]
        return {"video_path": out_path, "poster_path": poster_path, "options": options, "note": note}
    except Exception:
        return {"video_path": None, "poster_path": poster_path, "options": options}


def _gen_bgm(out_path: str, dur: float):
    """生成一段柔和的 AI 配乐（pad），从根本上避免低频「嗡嗡」声。

    原实现用 220/277/330Hz 三个低频正弦波恒定叠加，听起来就是典型 hum。
    改进：基频提到中高频（G 大三和弦 392/494/587Hz），叠少量高次泛音更像音乐；
    整体加 ADSR 包络（淡入淡出）与缓慢振幅 LFO（呼吸感），消除恒定 tone 的嗡感；
    峰值压到约 0.16，不掩盖人声。采样率 44100 与原视频一致，避免重采样伪影。
    """
    import numpy as np
    sr = 44100
    n = int(dur * sr)
    if n <= 0:
        return
    t = np.arange(n) / sr
    # 中高频和弦（避免 <250Hz 的低频 hum 感）
    freqs = [392.0, 493.88, 587.33]
    sig = np.zeros(n)
    for f in freqs:
        s = np.sin(2 * np.pi * f * t)
        s += 0.25 * np.sin(2 * math.pi * 2 * f * t)   # 八度泛音，增加音乐感
        s += 0.12 * np.sin(2 * math.pi * 3 * f * t)   # 十二度泛音
        sig += s
    sig /= (len(freqs) * 1.37)
    # 缓慢振幅 LFO（呼吸感），打破恒定 tone
    lfo = 0.75 + 0.25 * np.sin(2 * np.pi * 0.12 * t)
    sig *= lfo
    # 整体 ADSR 包络：淡入 2s、淡出 3s
    env = np.ones(n)
    fin = int(min(2.0 * sr, n))
    fout = int(min(3.0 * sr, n))
    if fin:
        env[:fin] = np.linspace(0.0, 1.0, fin)
    if fout:
        env[-fout:] = np.linspace(1.0, 0.0, fout)
    sig *= env
    sig *= 0.16                       # 压低整体音量
    sig = np.clip(sig, -1.0, 1.0)
    pcm = (sig * 32767).astype("<i2")
    with wave.open(out_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def make_cover(style: str, title: str, subtitle: str, out_path: str) -> str:
    """用 Pillow 生成竖版封面（1080x1350）。"""
    W, H = 1080, 1350
    styles = {
        "大字标题型": ((245, 240, 235), (20, 20, 24)),
        "对比型": ((235, 245, 240), (20, 60, 40)),
        "悬念型": ((30, 30, 40), (255, 220, 120)),
        "表情包型": ((255, 245, 230), (200, 60, 60)),
    }
    bg, fg = styles.get(style, styles["大字标题型"])
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    # 底部色块
    d.rectangle([0, H - 360, W, H], fill=fg if isinstance(fg, tuple) and len(fg) == 3 else (20, 20, 24))
    f_big = _pil_font(96)
    f_small = _pil_font(44)
    # 标题（自动换行）
    title = (title or "未命名")[:30]
    lines = _wrap(title, 9)
    y = H - 360 + 60
    for ln in lines[:3]:
        d.text((60, y), ln, font=f_big, fill=(255, 255, 255))
        y += 110
    if subtitle:
        d.text((60, y + 10), _wrap(subtitle, 14)[0][:16], font=f_small, fill=(230, 230, 230))
    # 顶部标签
    d.text((60, 60), "# " + style, font=f_small, fill=tuple(fg) if isinstance(fg, tuple) else (200, 200, 200))
    im.save(out_path, quality=90)
    return out_path


def _wrap(text: str, per: int):
    return [text[i:i + per] for i in range(0, len(text), per)] or [""]


def download_file(url: str, out_path: str, timeout: int = 300) -> str:
    """从 http(s) URL 下载文件到本地（用于取回 PAI-EAS 上 HeyGem 生成的结果视频）。"""
    import urllib.request
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "digihuman-platform/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return out_path
