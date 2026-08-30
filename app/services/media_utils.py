"""媒体工具：生成真实可播放的音频/视频/封面。
- 音频：用 wave 合成可播放 WAV（占位配音，生产替换为 CosyVoice/TTS）
- 视频：用 imageio-ffmpeg 自带 ffmpeg 合成（图像+音频），失败则降级
- 封面：用 Pillow 绘制
"""
import os
import wave
import struct
import math
import random
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


def edit_video(in_path: str, poster_path: str, options: dict, audio_path: str, out_path: str) -> dict:
    """自动剪辑：调色 / 网感大字 / MG动画 / BGM。输入可能是 mp4 或 海报+音频。"""
    has_video = bool(in_path) and os.path.exists(in_path)
    src_video = in_path if has_video else None
    # 若只有海报+音频，先合成基础视频
    if not src_video and poster_path:
        base = out_path.replace(".mp4", "_base.mp4")
        r = make_talking_video(poster_path, audio_path, base) if audio_path else None
        if r and r.get("video_path"):
            src_video = r["video_path"]
    if not src_video:
        # 完全降级：直接返回海报
        return {"video_path": None, "poster_path": poster_path, "options": options}

    # 构造 ffmpeg 滤镜链
    vf = []
    if options.get("color"):       # 自动调色
        vf.append("eq=contrast=1.12:saturation=1.25:brightness=0.03")
    if options.get("mg"):          # MG动画（轻微缩放+淡入）
        vf.append("zoompan=z='min(zoom+0.0005,1.05)':d=1:s=720x1280:fps=25,format=yuv420p")
    if options.get("bigtext"):     # 网感大字
        vf.append("drawtext=text='划重点':fontcolor=white:fontsize=72:box=1:boxcolor=red@0.6:x=(w-text_w)/2:y=h*0.78")
    vf_str = ",".join(vf) if vf else "null"

    # BGM：生成一段轻柔背景音并混流
    bgm_path = None
    if options.get("bgm"):
        bgm_path = out_path.replace(".mp4", "_bgm.wav")
        _gen_bgm(bgm_path, 8)

    if not _FFMPEG:
        return {"video_path": None, "poster_path": poster_path, "options": options,
                "note": "无 ffmpeg，剪辑以静态预览呈现"}

    try:
        cmd = [_FFMPEG, "-y", "-i", src_video]
        if bgm_path:
            cmd += ["-i", bgm_path, "-filter_complex",
                    "[0:a][1:a]amix=inputs=2:duration=shortest:weights=1 0.15[a]"]
            audio_map = "-map", "0:v", "-map", "[a]"
        else:
            audio_map = ("-map", "0")
        cmd += ["-vf", vf_str]
        cmd = list(cmd) + list(audio_map) + ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                                            "-c:a", "aac", "-shortest", "-movflags", "+faststart", out_path]
        import subprocess
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            return {"video_path": out_path, "poster_path": poster_path, "options": options}
    except Exception:
        pass
    # 失败降级：复制原视频
    try:
        import shutil
        shutil.copy(src_video, out_path)
        return {"video_path": out_path, "poster_path": poster_path, "options": options,
                "note": "部分剪辑效果未生效，已保留原视频"}
    except Exception:
        return {"video_path": None, "poster_path": poster_path, "options": options}


def _gen_bgm(out_path: str, dur: float):
    sr = 24000
    n = int(dur * sr)
    frames = bytearray()
    # 简单和弦 pad
    for i in range(n):
        t = i / sr
        v = (math.sin(2 * math.pi * 220 * t) + math.sin(2 * math.pi * 277 * t)
             + math.sin(2 * math.pi * 330 * t)) / 6 * 0.5
        frames += struct.pack("<h", int(max(-1, min(1, v)) * 32767))
    with wave.open(out_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))


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
