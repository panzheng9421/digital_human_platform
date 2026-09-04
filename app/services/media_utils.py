"""媒体工具：生成真实可播放的音频/视频/封面。
- 音频：用 wave 合成可播放 WAV（占位配音，生产替换为 CosyVoice/TTS）
- 视频：用 imageio-ffmpeg 自带 ffmpeg 合成（图像+音频），失败则降级
- 封面：用 Pillow 绘制
"""
import os
import re
import difflib
import glob
import wave
import struct
import math
import random
import subprocess
import threading
import time
from PIL import Image, ImageDraw, ImageFont

try:
    import imageio_ffmpeg
    _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _FFMPEG = None


def _pil_font(size, bold=False):
    """加载 Pillow 字体。bold=True 时优先用系统粗体/黑体，让封面大标题更厚重。"""
    candidates = []
    if bold:
        candidates = [
            "C:/Windows/Fonts/HarmonyOS_Sans_SC_Bold.ttf",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
    candidates += [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
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
    """文案拆句：按标点切分，超短句合并，不再硬截断句数。

    此前 `[:14]` 会把长文案的结尾 CTA（如"同行们，今年对你来说是噩梦还是天堂？"）
    直接丢掉；去掉上限后由文案自身决定句数，大字幕位置标签会按 idx%3 循环，不会乱。"""
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
    return out


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


def _ass_bigtext_layout(width, height):
    """网感大字（爆款标题字幕）排版参数：字号适中、行长放宽、上下位置轮换、描边粗。"""
    ratio = width / height if height else 0.5625
    if ratio < 0.85:          # 竖屏 9:16
        return {
            "play_x": 720, "play_y": 1280,
            "font_size": 72, "chars_per_line": 12,
            "margin_v": 170, "margin_lr": 40,
            "outline": 3, "shadow": 0,
            "note": "竖屏 9:16 网感大字",
        }
    if ratio > 1.18:          # 横屏 16:9
        return {
            "play_x": 1280, "play_y": 720,
            "font_size": 58, "chars_per_line": 16,
            "margin_v": 100, "margin_lr": 60,
            "outline": 3, "shadow": 0,
            "note": "横屏 16:9 网感大字",
        }
    return {
            "play_x": 1080, "play_y": 1080,
            "font_size": 64, "chars_per_line": 14,
            "margin_v": 140, "margin_lr": 50,
        "outline": 3, "shadow": 0,
        "note": "方形 1:1 网感大字",
    }


def _bigtext_tags(duration_ms, color="red"):
    """ASS 动画标签：淡入 + 缩放弹入（135% → 100%）。
    duration_ms 为该句显示时长，保证动画参数不超出事件范围。
    color: red / yellow / white。
    """
    dur = max(300, int(duration_ms))
    pop = min(280, dur // 3)
    fade = min(200, dur // 4)
    fade_out_start = max(fade + 100, dur - 250)
    ccode = {"red": "&H0000FF", "yellow": "&H00FFFF", "white": "&HFFFFFF"}.get(color, "&H0000FF")
    # \b1 加粗，\fscx/y 缩放弹入，\fade 淡入淡出
    return f"{{\\b1\\fscx135\\fscy135\\t(0,{pop},\\fscx100\\fscy100)\\fade(0,255,255,0,{fade},{fade_out_start},{dur})\\c{ccode}\\3c&H000000}}"


def _bigtext_font_size(base, length):
    """网感大字动态字号：字数越少字越大。>5 用 base，<=5 逐级放大。"""
    if length <= 2:
        return int(base * 1.6)
    if length <= 4:
        return int(base * 1.3)
    if length <= 5:
        return int(base * 1.15)
    return base


def _bigtext_full_tags(duration_ms, pos="top", base_font_size=72, text_len=12):
    """网感大字整句行的动画标签：轻微淡入 + 白字基底（关键词黄由内联 \\c 控制）+ 按字数动态字号。"""
    dur = max(300, int(duration_ms))
    fade = min(220, dur // 3)
    # \an8 = 顶部居中, \an2 = 底部居中, \an5 = 正中央；默认顶部
    align = {"bottom": "\\an2", "mid": "\\an5"}.get(pos, "\\an8")
    fs = _bigtext_font_size(base_font_size, text_len)
    return f"{{\\b1\\fs{fs}\\fad({fade},{fade}){align}\\c&HFFFFFF&\\3c&H000000&}}"


def _highlight_big(text, kw):
    """整句白字，关键词部分替换为黄色高亮（ASS 内联色标）。
    完整关键词可能跨 chunk（切块比词短），此时退而高亮落在当前 chunk 内的最长关键词子串。
    """
    text = _strip_punct(text)
    if not kw:
        return text
    if kw in text:
        return text.replace(kw, f"{{\\c&H00FFFF&}}{kw}{{\\c&HFFFFFF&}}")
    # 完整词不命中：只取 kw 的后缀子串（>=2 字）高亮，贴合截图"句末黄"的规律
    for st in range(0, len(kw) - 1):
        sub = kw[st:]
        if sub in text:
            return text.replace(sub, f"{{\\c&H00FFFF&}}{sub}{{\\c&HFFFFFF&}}")
    return text


_PUNCT_RE = re.compile(r"[，。！？、；：,.!?;:\s（）()\[\]【】\"'…—\-/\\]")


def _strip_punct(s):
    """去掉字幕里的中英文标点与空白，只留文字/数字（网感大字不要逗号句号）。"""
    return _PUNCT_RE.sub("", (s or "")).strip()


# 提取网感大字关键词时过滤的停用词（口语/虚词），避免把"我/是/的"这种词放大。
_KEYWORD_STOPWORDS = {
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "咱们", "大家", "人家",
    "的", "地", "得", "是", "就", "而", "这", "那", "有", "在", "和", "与", "或",
    "但", "因为", "所以", "如果", "那么", "了", "着", "过", "吗", "呢", "吧", "啊",
    "嗯", "对", "不", "没", "没有", "很", "非常", "特别", "最", "太", "真", "其实",
    "可能", "应该", "需要", "可以", "会", "能", "要", "去", "来", "上", "下", "里",
    "外", "个", "种", "样", "位", "件", "条", "张", "把", "次", "回", "天", "年",
    "月", "日", "时", "分", "秒", "一", "二", "三", "四", "五", "六", "七", "八",
    "九", "十", "几", "一些", "一下", "一直", "一起", "已经", "正在", "还是", "或者",
    "以及", "关于", "对于", "由于", "根据", "按照", "通过", "为了", "为着", "除了",
    "有关", "相关", "随着", "任凭", "即使", "尽管", "虽然", "但是", "然而", "不过",
    "只是", "而且", "并且", "况且", "何况", "与其", "不如", "要么", "假如", "假设",
    "假使", "假定", "譬如", "例如", "比如", "像", "似的", "一样", "一般", "等等",
    "云云", "之类", "什么的", "怎么", "怎样", "如何", "为什么", "为何", "什么", "谁",
    "哪", "哪里", "哪儿", "多少", "多么", "这么", "与否", "能否", "会不会", "是不是",
    "有没有", "要不要", "能不能", "可不可以", "应不应该", "值不值得", "可不", "可曾",
    "未尝", "不曾", "不必", "未必", "也许", "或许", "大概", "大约", "约莫", "差不多",
    "几乎", "简直", "根本", "决", "绝对", "完全", "都", "全", "总", "统统", "一律",
    "一概", "总是", "老是", "一直", "始终", "永远", "永久", "长久", "长期", "暂时",
    "临时", "忽然", "突然", "猛然", "骤然", "陡然", "毅然", "决然", "断然", "果然",
    "居然", "竟然", "偶然", "偶尔", "时常", "常常", "经常", "往往", "每每", "一向",
    "从来", "毕竟", "究竟", "到底", "终归", "终究", "终于", "然后", "而后", "之后",
    "后来", "以后", "以来", "以前", "之前", "当时", "当场", "当下", "立刻", "立即",
    "马上", "赶紧", "赶快", "连忙", "急忙", "匆忙", "仓促", "慌忙", "慌张", "忙乱",
    "慌乱", "赶忙", "顿时", "霎时", "刹那", "瞬间", "顷刻", "片刻", "须臾", "俄顷",
    "一会儿", "一时", "一度", "一致", "一同", "一块儿", "一道", "一并", "通常", "平常",
    "平时", "日常", "往常", "照常", "照旧", "照样", "仍旧", "仍然", "依然", "依旧",
    "仿照", "模仿", "模拟", "效仿", "效法", "学习", "借鉴", "参考", "参照", "依照",
    "遵照", "遵循", "遵从", "服从", "听从", "听任", "随意", "随便", "任意", "肆意",
    "恣意", "尽情", "尽量", "尽力", "竭力", "努力", "奋力", "拼命", "卖力", "用劲",
    "使劲", "着力", "致力", "从事", "参加", "参与", "加入", "投入", "投身", "献身",
    "奉献", "贡献", "捐助", "捐赠", "捐献", "赠送", "馈赠", "送给", "交给", "递给",
    "传给", "传播", "传送", "传递", "传导", "传染", "感染", "影响", "作用", "效用",
    "效果", "成效", "结果", "成果", "后果", "结局", "下场", "硕果", "果实", "果子",
    "种子", "效果", "功效", "效率", "效益", "利益", "利润", "红利", "利息", "股息",
    "彩头", "好处", "益处", "裨益", "用处", "用途", "用场", "使用", "利用", "应用",
    "运用", "采用", "采纳", "采取", "施用", "行使", "执行", "履行", "实行", "实施",
    "实践", "实现", "完成", "达成", "达到", "到达", "抵达", "来到", "贯彻", "落实",
    "施行", "到", "至", "及", "跟", "同",
}


def _extract_keyword(text: str) -> str:
    """从一句口播文案里提取最有冲击力的关键词/短语，用于网感大字。
    规则版：扫描全部分句，优先保留数字、句末实词；句尾分句权重更高。
    返回空字符串表示本句没有合适关键词（不强制出大字）。
    """
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"[，。！？、；：,!?;:\s]", text) if p.strip()]
    if not parts:
        return ""

    def _tail_kw(part: str) -> str:
        """从单个分句提取句末关键词。"""
        part = _strip_punct(part)
        if not part:
            return ""
        while part and part[-1] in "吗呢吧啊嗯哦唉啦哇哈嘿哟呐哩喽嘛":
            part = part[:-1]
        if not part:
            return ""
        tail = part[-8:]
        # 反复去掉结构助词/副词/介词/能愿动词等单字前缀
        single_prefix = r"[在正对从向自由于与和跟同被把让给了着过却还也只才刚又也但而却就总全各每很太最极更越比较稍微有点有些非常特别十分相当格外分外过于的地得着了过可是然而不过]"
        for _ in range(4):
            new_tail = re.sub(f"^{single_prefix}", "", tail)
            if new_tail == tail:
                break
            tail = new_tail
        # 反复去掉常见多字前缀（泛化词、副词、能愿动词）
        multi_prefix = r"^(各种|所有|一些|一个|这个|那个|这些|那些|这样|那样|一下|一直|总是|老是|经常|常常|非常|特别|想|要|会|能|整天|总是|一直|总|老|光|净|才|又|也|还)"
        for _ in range(3):
            new_tail = re.sub(multi_prefix, "", tail)
            if new_tail == tail:
                break
            tail = new_tail
        # 如果包含转折词，取转折词之后的高潮部分（不能用单字"不/过"，避免"赚不到"被误切）
        m = re.search(r"(而是|但是|可是|然而|不过|却|但|而)(.+)$", tail)
        if m and len(m.group(2)) >= 2:
            tail = m.group(2)
        core = "".join([c for c in tail if c not in _KEYWORD_STOPWORDS])
        return tail[:7] if len(core) >= 2 else ""

    candidates = []  # [(keyword, part_index, base_score), ...]
    for idx, part in enumerate(parts):
        part_clean = _strip_punct(part)
        if not part_clean:
            continue

        # 数字+量词+实词（10年Java、第一个坑）
        num = re.search(r"\d+[\d\.]*[十百千万亿]?[个年月份天次种项条张]?[\w\u4e00-\u9fa5]{1,5}", part_clean)
        if num:
            candidates.append((num.group(0)[:7], idx, 4))

        # 句末关键词
        kw = _tail_kw(part)
        if kw:
            base = 3 if idx == len(parts) - 1 else 2
            candidates.append((kw, idx, base))

    if not candidates:
        return ""

    def score(item):
        kw, idx, base = item
        s = base + len(kw)
        if re.search(r"\d", kw):
            s += 2
        if idx == len(parts) - 1:
            s += 1
        # 2-4字最佳，太长减分
        if len(kw) > 4:
            s -= 1
        if len(kw) > 5:
            s -= 2
        # 短词在最后分句更有爆发力
        if idx == len(parts) - 1 and len(kw) <= 3:
            s += 2
        return s

    best = max(candidates, key=score)[0]
    return best


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


def _set_style_font(lines, font, style_idx=8):
    """把 ASS Styles 行里的字体家族名换成实际解析到的中文字体。"""
    if not font:
        return
    fam = None
    if font.get("fontfile"):
        try:
            fam = ImageFont.truetype(font["fontfile"]).getname()[0]
        except Exception:
            fam = os.path.splitext(os.path.basename(font["fontfile"]))[0]
    elif font.get("fontname"):
        fam = font["fontname"]
    if fam and 0 <= style_idx < len(lines):
        parts = lines[style_idx].split(",")
        if len(parts) > 1:
            parts[1] = fam
            lines[style_idx] = ",".join(parts)


def _make_ass_bigtext(sentences, dur, out_path, font=None, meta=None):
    """网感大字 ASS：整屏大字（白字 + 关键词黄高亮），去掉底部小字幕层，居中显示。"""
    layout = _ass_bigtext_layout(meta.get("width", 720), meta.get("height", 1280)) if meta else _ass_bigtext_layout(720, 1280)
    play_x, play_y = layout["play_x"], layout["play_y"]

    # 整屏大字 style：白字黑边，字号适中，顶部居中（alignment=8），上下边距由 margin_v 控制
    big_style = (
        f"Style: big,Microsoft YaHei,{layout['font_size']},&HFFFFFF,&H00000000,&H00000000,-1,0,"
        f"{layout['outline']},0,8,{layout['margin_lr']},{layout['margin_lr']},{layout['margin_v']}"
    )

    lines = [
        "[Script Info]", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {play_x}", f"PlayResY: {play_y}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
        big_style,
        "", "[Events]",
        "Format: Layer, Start, End, Style, Text",
    ]
    _set_style_font(lines, font, style_idx=8)   # big

    def ts_sec(t):
        t = max(0.0, float(t))
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    # 整句大字：按句切块均分时长；关键词在【整句】级别提取，只高亮含该词的 chunk，
    # 白底 + 少量黄关键词（截图风格）。
    all_chunks = []
    for s in sentences:
        all_chunks.extend(_chunk_text(s, layout["chars_per_line"]))
    n = len(all_chunks) or 1
    seg = max(0.6, dur / n)
    cur = 0.0
    for idx, s in enumerate(sentences):
        # 三句一轮换位置：顶 / 正中 / 底，让大字在画面里上下浮动，偶尔一行落在正中央
        pos = ("top", "mid", "bottom")[idx % 3]
        kw = _extract_keyword(s)
        kw = _strip_punct(kw)[:7] if kw else ""
        for txt in _chunk_text(s, layout["chars_per_line"]):
            start = cur
            end = min(dur, cur + seg)
            body = _strip_punct(txt)
            highlighted = _highlight_big(body, kw)
            show_ms = max(300, (end - start) * 1000)
            lines.append(f"Dialogue: 0,{ts_sec(start)},{ts_sec(end)},big,{_bigtext_full_tags(show_ms, pos=pos, base_font_size=layout['font_size'], text_len=len(body))}{highlighted}")
            cur = end

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def _make_ass_bigtext_ts(items, out_path, font=None, meta=None):
    """用百炼 ASR 真实时间戳生成网感大字 ASS：整屏大字（白字 + 关键词黄高亮），去掉底部小字幕层。"""
    layout = _ass_bigtext_layout(meta.get("width", 720), meta.get("height", 1280)) if meta else _ass_bigtext_layout(720, 1280)
    play_x, play_y = layout["play_x"], layout["play_y"]

    big_style = (
        f"Style: big,Microsoft YaHei,{layout['font_size']},&HFFFFFF,&H00000000,&H00000000,-1,0,"
        f"{layout['outline']},0,8,{layout['margin_lr']},{layout['margin_lr']},{layout['margin_v']}"
    )

    lines = [
        "[Script Info]", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {play_x}", f"PlayResY: {play_y}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
        big_style,
        "", "[Events]",
        "Format: Layer, Start, End, Style, Text",
    ]
    _set_style_font(lines, font, style_idx=8)   # big

    def ts(ms):
        t = max(0.0, float(ms) / 1000.0)
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    # 整句大字：每句按字数切块，线性分配时间戳；关键词在【整句】级别提取，
    # 只高亮含该词的 chunk（白底 + 少量黄关键词，贴近截图风格）。
    # 三句一轮换位置：顶 / 正中 / 底，让大字在画面里上下浮动，偶尔一行落在正中央。
    for idx, it in enumerate(items):
        text = (it.get("text") or "").strip()
        if not text:
            continue
        pos = ("top", "mid", "bottom")[idx % 3]
        begin = int(it.get("begin", 0) or 0)
        end = int(it.get("end", 0) or 0)
        if end <= begin:
            end = begin + 2000
        kw = _extract_keyword(text)
        kw = _strip_punct(kw)[:7] if kw else ""
        chunks = _chunk_text(text, layout["chars_per_line"])
        if not chunks:
            chunks = [text]
        n = len(chunks)
        span = end - begin
        for i, chunk in enumerate(chunks):
            b = begin + int(span * i / n)
            e = begin + int(span * (i + 1) / n) if i < n - 1 else end
            body = _strip_punct(chunk)
            highlighted = _highlight_big(body, kw)
            show_ms = max(300, (e - b))
            lines.append(f"Dialogue: 0,{ts(b)},{ts(e)},big,{_bigtext_full_tags(show_ms, pos=pos, base_font_size=layout['font_size'], text_len=len(body))}{highlighted}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def _align_segments_to_items(sentences, items):
    """把改写后文案句子(sentences)对齐到 ASR 真实时间轴(items)，返回等长 [(begin_ms, end_ms), ...]。

    - 句数相等 -> 逐句对齐；
    - 句数不等 -> 按 items 总时长跨度 [首句begin, 末句end] 对 sentences 均分
      （内容正确、卡点大致跟语速，比直接用 ASR 原文错字强）。
    """
    if not items:
        return [(0, 2000) for _ in sentences]
    if len(sentences) == len(items):
        segs = []
        for it in items:
            b = int(it.get("begin", 0) or 0)
            e = int(it.get("end", 0) or 0)
            if e <= b:
                e = b + 2000
            segs.append((b, e))
        return segs
    t0 = int(items[0].get("begin", 0) or 0)
    t1 = int(items[-1].get("end", 0) or 0)
    if t1 <= t0:
        t1 = t0 + 2000 * len(sentences)
    span = (t1 - t0) / max(1, len(sentences))
    return [(int(t0 + i * span), int(t0 + (i + 1) * span)) for i in range(len(sentences))]


def _build_asr_char_timeline(items):
    """用百炼 ASR 段建立『内容字(去标点) -> 说到它的时刻(ms)』连续时间轴。

    关键前提（老板拍板 2026-09-03）：ASR 转写的是你说的话，改写文案是同一句话的清洗版，
    去掉标点后两边字数一致。所以我们不依赖 ASR 在哪断句，而是按『字数位置』对齐。
    返回 (total_chars, time_at, chars)：time_at(g) 返回第 g 个内容字(0-based)的起始时刻(ms)；
    chars 为 ASR 内容字序列(去标点)，供局部对齐用。
    """
    segs = []  # (cum_start, cum_end, begin_ms, end_ms)
    chars = []
    cum = 0
    for it in items:
        c = list(_strip_punct(it.get("text") or ""))
        n = len(c)
        if n == 0:
            continue
        b = int(it.get("begin", 0) or 0)
        e = int(it.get("end", 0) or 0)
        if e <= b:
            e = b + 1
        segs.append((cum, cum + n, b, e))
        chars.extend(c)
        cum += n
    total = cum

    def time_at(g):
        if total == 0 or not segs:
            return 0
        if g <= 0:
            return segs[0][2]
        if g >= total:
            return segs[-1][3]
        for (cs, ce, b, e) in segs:
            if g < ce:
                local = (g - cs) / max(1, (ce - cs))
                return int(b + local * (e - b))
        return segs[-1][3]

    return total, time_at, chars


def _build_char_map(t_chars, g_chars):
    """改写字序列 t_chars -> ASR 字序列 g_chars 的索引映射 g_of(r)。

    用 difflib 做**局部对齐**而非全局线性缩放：内容一致时逐字 1:1；
    用户改写删/加了几个字，差异被 diff 匹配块吸收，只在改动处**局部**偏移，
    不会像全局比例那样越往后累积漂移（现象：前段齐、后段乱）。
    """
    G = len(g_chars)
    T = len(t_chars)
    if T == 0:
        return lambda r: 0
    if T == G:
        return lambda r: min(G, max(0, r))

    sm = difflib.SequenceMatcher(a=t_chars, b=g_chars, autojunk=False)
    blocks = sm.get_matching_blocks()  # [(i1, i2, size), ...]，含尾部 (T,G,0) 哨兵

    def g_of(r):
        r = max(0, min(T - 1, r))
        # 1) 命中某匹配块 -> 1:1
        for (i1, i2, size) in blocks:
            if size > 0 and i1 <= r < i1 + size:
                return i2 + (r - i1)
        # 2) 块间 -> 找左右锚点线性插值
        left = None
        right = None
        for (i1, i2, size) in blocks:
            if size == 0:
                continue
            if i1 + size <= r:
                left = (i1 + size, i2 + size)
            if i1 > r:
                if right is None:
                    right = (i1, i2)
        if left is not None and right is not None:
            lr, lg = left
            rr, rg = right
            if rr == lr:
                return lg
            frac = (r - lr) / (rr - lr)
            return int(round(lg + frac * (rg - lg)))
        if left is not None:
            return min(G, left[1] + (r - left[0]))
        if right is not None:
            return max(0, right[1] - (right[0] - r))
        return min(G, round(r * G / T))  # 极端兜底

    return g_of


def _align_script_to_asr(sentences, items):
    """把改写后文案(sentences)映射到百炼 ASR 真实时间轴(items)，返回 [(text, begin_ms, end_ms), ...]。

    核心逻辑（2026-09-03 字符级映射，替代旧的『逐句 zip / snap 到 ASR 句边界』）：
    - 内容(去标点后)永远用改写好文案，ASR 只提供时间轴；
    - 用 ASR 建连续『字数位置 -> 时刻』轴；改写每句按它在总内容字序列里的位置，
      查这条轴拿到 [begin, end]（即语音真正念到这些字的区间）；
    - 句数相等/不等、句子边界对不对齐都无所谓，行为统一且贴合语音；
    - 改写字数与 ASR 字数不等(用户多删/加字)时按比例映射，平滑过渡；
    - items 为空 / ASR 段全无内容字 -> 按句 2 秒均分兜底。
    """
    if not items:
        return [(s, i * 2000, (i + 1) * 2000) for i, s in enumerate(sentences)]
    if not sentences:
        return []

    asr_total, time_at, g_chars = _build_asr_char_timeline(items)
    if asr_total == 0:
        return [(s, i * 2000, (i + 1) * 2000) for i, s in enumerate(sentences)]

    # 改写全部内容字(去标点)拼成一条序列，用于定位每句的字数位置
    t_chars = []
    for s in sentences:
        t_chars.extend(list(_strip_punct(s)))
    T = len(t_chars)
    if T == 0:
        # 改写无内容字(纯标点)，退回 ASR 原文
        out = []
        for it in items:
            b = int(it.get("begin", 0) or 0)
            e = int(it.get("end", 0) or 0)
            if e <= b:
                e = b + 2000
            out.append((it.get("text", ""), b, e))
        return out

    # 改写字索引 r -> ASR 字索引 g：局部对齐(吸收删/加字差异，不累积漂移)
    g_of = _build_char_map(t_chars, g_chars)

    out = []
    n = len(sentences)
    pos = 0  # 在 t_chars 中的内容字游标
    for i, s in enumerate(sentences):
        cnt = len(list(_strip_punct(s)))
        rs, re_ = pos, pos + cnt
        pos = re_
        g_begin = g_of(rs)
        g_end = g_of(re_) if i < n - 1 else asr_total
        begin = time_at(g_begin)
        end = time_at(g_end)
        if end <= begin:
            end = begin + 1000
        out.append((s, begin, end))
    return out


def _make_ass_with_ts(sentences, items, out_path, font=None, meta=None):
    """普通字幕：内容用改写后好文案(sentences)，时间戳用百炼 ASR 真实时间轴(items)对齐。"""
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
    _set_style_font(lines, font, style_idx=8)

    # 把『句子 -> chunk』扁平化后交给 _align_script_to_asr：去标点后字序列与整句完全一致，
    # 字符映射零变化，只是把粒度从『句』细到『chunk』，让每个 chunk 拿到真实时间戳，
    # 消除原『句内按块数均分』造成的错位（现象：前段短句齐、后段长句乱）。
    flat_chunks = []
    for s in sentences:
        s2 = (s or "").strip()
        if not s2:
            continue
        for ch in _chunk_text(s2, layout["chars_per_line"]):
            flat_chunks.append(ch)
    for text, begin, end in _align_script_to_asr(flat_chunks, items):
        text = (text or "").strip()
        if not text:
            continue
        if end <= begin:
            end = begin + 2000
        lines.append(f"Dialogue: 0,{ts(begin)},{ts(end)},big,{_strip_punct(text)}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def _make_ass_bigtext_with_ts(sentences, items, out_path, font=None, meta=None):
    """网感大字：内容用改写后好文案(sentences)，时间戳用百炼 ASR 真实时间轴(items)对齐。"""
    layout = _ass_bigtext_layout(meta.get("width", 720), meta.get("height", 1280)) if meta else _ass_bigtext_layout(720, 1280)
    play_x, play_y = layout["play_x"], layout["play_y"]

    big_style = (
        f"Style: big,Microsoft YaHei,{layout['font_size']},&HFFFFFF,&H00000000,&H00000000,-1,0,"
        f"{layout['outline']},0,8,{layout['margin_lr']},{layout['margin_lr']},{layout['margin_v']}"
    )

    lines = [
        "[Script Info]", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {play_x}", f"PlayResY: {play_y}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
        big_style,
        "", "[Events]",
        "Format: Layer, Start, End, Style, Text",
    ]
    _set_style_font(lines, font, style_idx=8)

    def ts(ms):
        t = max(0.0, float(ms) / 1000.0)
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    # 句子级提关键词 + 定位置标签；chunk 扁平列表交给 _align_script_to_asr 取真实时间戳。
    # 关键词/位置按整句共享，不满屏黄、不乱跳；时间戳按 chunk 真实时刻（非句内均分）。
    sent_info = []
    for sidx, s in enumerate(sentences):
        s2 = (s or "").strip()
        if not s2:
            continue
        kw = _extract_keyword(s2)
        kw = _strip_punct(kw)[:7] if kw else ""
        pos = ("top", "mid", "bottom")[sidx % 3]
        chunks = _chunk_text(s2, layout["chars_per_line"]) or [s2]
        sent_info.append((chunks, kw, pos))
    flat = [ch for chunks, _, _ in sent_info for ch in chunks]
    chunk_ts = _align_script_to_asr(flat, items)
    ti = 0
    for chunks, kw, pos in sent_info:
        for chunk in chunks:
            if ti >= len(chunk_ts):
                break
            _, begin, end = chunk_ts[ti]
            ti += 1
            if end <= begin:
                end = begin + 2000
            body = _strip_punct(chunk)
            highlighted = _highlight_big(body, kw)
            show_ms = max(300, (end - begin))
            lines.append(f"Dialogue: 0,{ts(begin)},{ts(end)},big,{_bigtext_full_tags(show_ms, pos=pos, base_font_size=layout['font_size'], text_len=len(body))}{highlighted}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def edit_video(in_path: str, poster_path: str, options: dict, audio_path: str, out_path: str,
               script_text: str = None, on_progress=None) -> dict:
    def _prog(p):
        try:
            if on_progress:
                on_progress(max(0, min(100, int(p))))
        except Exception:
            pass

    has_video = bool(in_path) and os.path.exists(in_path)
    src_video = in_path if has_video else None
    subtitle_note = None
    _prog(5)
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

    # 在处理前抽取干净首帧，避免后续加字幕/调色/zoompan 污染封面底图
    frame0_path = os.path.splitext(out_path)[0] + "_frame0.jpg"
    _save_frame0(src_video, frame0_path)
    _prog(15)

    # 构造 ffmpeg 滤镜链
    vf = []
    if options.get("color"):       # 自动调色
        vf.append("eq=contrast=1.12:saturation=1.25:brightness=0.03")
    if options.get("mg"):          # MG动画（轻微缩放），输出尺寸与帧率跟随原视频，绝不强制 25fps 导致音画不同步
        z_w, z_h = vid_meta.get("width", 1280), vid_meta.get("height", 720)
        fps = vid_meta.get("fps") or 25
        vf.append(f"zoompan=z='min(zoom+0.0005,1.05)':d=1:s={z_w}x{z_h}:fps={fps},format=yuv420p")
    # 字幕：
    # - 不勾选"网感大字" = 普通字幕（此前调试效果：百炼 ASR 对齐 + 文案均分 + 去标点，底部居中）。
    # - 勾选"网感大字" = 爆款标题大字（画面居中、黄字黑边、逐句弹出）。
    _font = _resolve_cjk_font()
    if not _font:
        print("[warn] 未找到中文字体，跳过字幕（避免方块字）。"
              "请在 ECS 执行: apt-get install -y fonts-wqy-zenhei 或往 app/assets/fonts 丢一个中文字体")
        subtitle_note = "未找到中文字体，已跳过字幕（避免方块字）"
    else:
        subs_ok = False
        # 字幕内容：优先用改写后好文案（script_text），绝不把 ASR 原文错字带进成片；
        # 仅当用户未改写（无 script_text）时，才退回用 ASR 原文做内容。
        sents = _split_sentences(script_text) if script_text else []
        use_script = bool(sents)

        # 时间戳：优先拿百炼 ASR 真实时间轴（若可用），否则按句均分
        items = None
        subs_src = audio_path or src_video
        if subs_src and os.path.exists(subs_src):
            try:
                from app.services import asr_client as ac
                if ac.asr_align_available():
                    print(f"[edit] 尝试百炼 ASR 对齐字幕: {subs_src}")
                    items = ac.transcribe_file_ts(subs_src)
            except Exception as e:
                print("[warn] ASR 对齐失败，回退按句均分:", str(e)[:200])

        _ass = out_path.replace(".mp4", "_subs.ass")
        if use_script:
            # 内容=改写好文案；时间戳=ASR 对齐（句数对不上则按 ASR 总时长均分），无 ASR 则按句均分
            if items:
                if options.get("bigtext"):
                    _make_ass_bigtext_with_ts(sents, items, _ass, _font, vid_meta)
                    subtitle_note = "已生成爆款标题字幕（文案+语速对齐）"
                else:
                    _make_ass_with_ts(sents, items, _ass, _font, vid_meta)
                    subtitle_note = "已生成字幕（文案+语速对齐）"
            else:
                _dur = vid_dur or (len(sents) * 2.5)
                if options.get("bigtext"):
                    _make_ass_bigtext(sents, _dur, _ass, _font, vid_meta)
                    subtitle_note = "爆款标题字幕按句均分（未启用 ASR 对齐）"
                else:
                    _make_ass(sents, _dur, _ass, _font, vid_meta)
                    subtitle_note = "字幕按句均分（未启用 ASR 对齐）"
            sub = "subtitles=" + _ffpath(_ass)
            if _font.get("fontfile"):
                sub += ":fontsdir=" + _ffpath(os.path.dirname(_font["fontfile"]))
            vf.append(sub)
            subs_ok = True
        else:
            # 无改写文案：退回老行为（直接用 ASR 原文做内容 + 时间戳）
            if items:
                if options.get("bigtext"):
                    _make_ass_bigtext_ts(items, _ass, _font, vid_meta)
                    subtitle_note = "已生成爆款标题字幕（按语速对齐）"
                else:
                    _make_ass_ts(items, _ass, _font, vid_meta)
                    subtitle_note = "已生成字幕（已按语速对齐）"
                sub = "subtitles=" + _ffpath(_ass)
                if _font.get("fontfile"):
                    sub += ":fontsdir=" + _ffpath(os.path.dirname(_font["fontfile"]))
                vf.append(sub)
                subs_ok = True
            else:
                ft = (":fontfile=" + _ffpath(_font["fontfile"])) if _font.get("fontfile") \
                    else (":font=" + _font["fontname"] if _font.get("fontname") else ":font=sans-serif")
                vf.append("drawtext=text='划重点':fontcolor=white:fontsize=72:box=1:boxcolor=red@0.6:"
                          "x=(w-text_w)/2:y=h*0.78" + ft)
                subtitle_note = "未读取到文案，字幕为占位「划重点」"
    vf_str = ",".join(vf) if vf else "null"
    _prog(40)

    # BGM：生成一段与视频等长的轻柔背景音并混流（避免 amix=shortest 把视频截断成 8 秒）
    bgm_path = None
    if options.get("bgm"):
        bgm_path = out_path.replace(".mp4", "_bgm.wav")
        _gen_bgm(bgm_path, max(8.0, vid_dur))
    _prog(55)

    if not _FFMPEG:
        return {"video_path": None, "poster_path": poster_path, "options": options,
                "note": "无 ffmpeg，剪辑以静态预览呈现"}

    stderr_log = ""

    def _ffmpeg_ticker(est_sec, start_pct=55, end_pct=95):
        stop_ev = threading.Event()
        def loop():
            t0 = time.time()
            total = max(0.5, float(est_sec))
            while not stop_ev.wait(0.5):
                ratio = min(1.0, (time.time() - t0) / total)
                p = start_pct + int((end_pct - start_pct) * (ratio ** 0.7))
                _prog(p)
                if ratio >= 1.0:
                    break
        threading.Thread(target=loop, daemon=True).start()
        return stop_ev

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
        # ffmpeg 编码是最耗时的黑盒阶段，按视频时长估算并启动平滑 ticker，
        # 让进度条在渲染期间持续前进，不再卡在 20%。
        est_sec = max(10.0, vid_dur * 1.5 + (10.0 if bgm_path else 0.0))
        tick_stop = _ffmpeg_ticker(est_sec, 55, 95)
        try:
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
        finally:
            tick_stop.set()
        stderr_log = (p.stderr or b"").decode(errors="ignore")[-1500:]
        if p.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            _prog(100)
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


def _save_frame0(video_path: str, out_frame_path: str = None):
    """从源视频抽取干净首帧存盘（命名约定：<视频名>_frame0.jpg），供封面模块复用。
    注意：必须在加字幕/压字幕之前调用，否则封面底图会带上字幕。"""
    if not _FFMPEG or not video_path or not os.path.exists(video_path):
        return None
    jpg = out_frame_path or (os.path.splitext(video_path)[0] + "_frame0.jpg")
    try:
        subprocess.run([_FFMPEG, "-y", "-i", video_path, "-frames:v", "1", "-q:v", "2", jpg],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        return None
    return jpg if os.path.exists(jpg) else None


def _round_corner(im, rad):
    """给 RGBA 图片加圆角蒙版。"""
    circle = Image.new("L", (rad * 2, rad * 2), 0)
    draw = ImageDraw.Draw(circle)
    draw.ellipse((0, 0, rad * 2, rad * 2), fill=255)
    alpha = Image.new("L", im.size, 255)
    w, h = im.size
    alpha.paste(circle.crop((0, 0, rad, rad)), (0, 0))
    alpha.paste(circle.crop((rad, 0, rad * 2, rad)), (w - rad, 0))
    alpha.paste(circle.crop((0, rad, rad, rad * 2)), (0, h - rad))
    alpha.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (w - rad, h - rad))
    im.putalpha(alpha)
    return im


def _make_cover_bold_top(im, title, subtitle, poster_path=None):
    """参考抖音爆款封面：人物底图 + 顶部大标题 + 副标题 + 底部小图。"""
    W, H = im.size
    # 顶部暗色渐变条，保证标题在复杂背景上可读
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for i in range(420):
        a = int(175 * (1 - i / 420))
        gd.line([(0, i), (W, i)], fill=(0, 0, 0, a))
    im = Image.alpha_composite(im.convert("RGBA"), grad).convert("RGB")
    d = ImageDraw.Draw(im)

    # 主标题：根据字数自适应字号，抖音爆款黄填充 + 黑色描边
    t_len = len(title)
    if t_len <= 8:
        big_size = 130
        max_chars = 8
    elif t_len <= 14:
        big_size = 112
        max_chars = 10
    elif t_len <= 22:
        big_size = 96
        max_chars = 12
    else:
        big_size = 84
        max_chars = 14
    f_big = _pil_font(big_size, bold=True)
    lines = _wrap(title, max_chars)[:2]
    line_h = int(big_size * 1.45)  # 3D 挤出会占一定空间，行距放大
    y = 75
    for ln in lines:
        try:
            bbox = d.textbbox((0, 0), ln, font=f_big)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(ln) * big_size
        x = (W - tw) // 2
        _draw_text_3d(d, (x, y), ln, f_big, fill=(255, 220, 0), shadow=(0, 0, 0), depth=6)
        y += line_h

    # 副标题：白色填充 + 黑色描边（主标题是黄字 3D，副标题用白字区分层级）
    if subtitle:
        f_sub = _pil_font(52, bold=True)
        sub_lines = _wrap(subtitle, 16)[:2]
        sy = y + 18
        for sl in sub_lines:
            try:
                bbox = d.textbbox((0, 0), sl, font=f_sub)
                sw = bbox[2] - bbox[0]
            except Exception:
                sw = len(sl) * 26
            sx = (W - sw) // 2
            _draw_text_outline(d, (sx, sy), sl, f_sub, fill=(255, 255, 255), outline=(0, 0, 0), width=3)
            sy += 64

    # 底部小图：poster 缩放、圆角、底部居中叠加
    if poster_path and os.path.exists(poster_path):
        try:
            pim = Image.open(poster_path).convert("RGBA")
            pw = int(W * 0.72)
            ph = int(pw * pim.height / pim.width)
            if ph > 520:
                ph = 520
                pw = int(ph * pim.width / pim.height)
            pim = pim.resize((pw, ph), Image.LANCZOS)
            pim = _round_corner(pim, 24)
            px, py = (W - pw) // 2, H - ph - 70
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            layer.paste(pim, (px, py), pim)
            im = Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")
        except Exception:
            pass
    return im


def make_cover(style: str, title: str, subtitle: str, out_path: str, frame_path: str = None, poster_path: str = None) -> str:
    """用 Pillow 生成竖版封面（9:16 = 1080x1920）。
    优先用视频首帧当底图（真实人物+场景，等比缩放居中裁剪），再叠加大标题；无首帧时纯色背景兜底。"""
    W, H = 1080, 1920
    styles = {
        "大字标题型": ((245, 240, 235), (20, 20, 24)),
        "对比型": ((235, 245, 240), (20, 60, 40)),
        "悬念型": ((30, 30, 40), (255, 220, 120)),
        "表情包型": ((255, 245, 230), (200, 60, 60)),
    }
    bg, fg = styles.get(style, styles["大字标题型"])
    im = None
    use_frame = False
    if frame_path and os.path.exists(frame_path):
        try:
            fim = Image.open(frame_path).convert("RGB")
            sw, sh = fim.size
            scale = max(W / sw, H / sh)
            fim = fim.resize((int(sw * scale), int(sh * scale)), Image.LANCZOS)
            nw, nh = fim.size
            im = fim.crop(((nw - W) // 2, (nh - H) // 2, (nw - W) // 2 + W, (nh - H) // 2 + H))
            use_frame = True
        except Exception:
            im = None
    if im is None:
        im = Image.new("RGB", (W, H), bg)
    title = _clean_cover_title(title)[:30]
    if style == "大字标题型":
        # 新爆款风格：顶部大标题 + 右侧强调字 + 底部小图
        im = _make_cover_bold_top(im, title, subtitle, poster_path)
    else:
        # 其他风格：整体叠海报装饰层 + 底部标题板
        if poster_path and os.path.exists(poster_path):
            try:
                pim = Image.open(poster_path).convert("RGBA").resize((W, H), Image.LANCZOS)
                im = Image.alpha_composite(im.convert("RGBA"), pim).convert("RGB")
            except Exception:
                pass
        ACCENT = (255, 214, 0)  # 网感亮黄
        if use_frame:
            panel_top = H - 580
            overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            dm = ImageDraw.Draw(overlay)
            dm.rectangle([0, panel_top, W, H], fill=(0, 0, 0, 175))       # 底部标题板
            im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
            d = ImageDraw.Draw(im)
            d.rectangle([0, panel_top, W, panel_top + 14], fill=ACCENT)
        else:
            ImageDraw.Draw(im).rectangle([0, H - 480, W, H],
                                         fill=fg if isinstance(fg, tuple) and len(fg) == 3 else (20, 20, 24))
        d = ImageDraw.Draw(im)
        t_len = len(title)
        if t_len <= 10:
            big_size, max_chars, line_h, max_lines = 120, 9, 145, 2
        elif t_len <= 20:
            big_size, max_chars, line_h, max_lines = 108, 10, 128, 3
        else:
            big_size, max_chars, line_h, max_lines = 90, 12, 108, 4
        f_big = _pil_font(big_size)
        f_small = _pil_font(48)
        lines = _wrap(title, max_chars)[:max_lines]
        total_h = len(lines) * line_h
        panel_h = 580 if use_frame else 480
        y = H - panel_h + (panel_h - total_h) // 2 - 30
        for ln in lines:
            try:
                bbox = d.textbbox((0, 0), ln, font=f_big)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = len(ln) * big_size
            x = (W - tw) // 2
            _draw_text_outline(d, (x, y), ln, f_big, ACCENT, outline=(0, 0, 0), width=4)
            y += line_h
        if subtitle:
            sub_lines = _wrap(subtitle, 16)[:2]
            sy = y + 10
            for sl in sub_lines:
                try:
                    bbox = d.textbbox((0, 0), sl, font=f_small)
                    sw = bbox[2] - bbox[0]
                except Exception:
                    sw = len(sl) * 24
                sx = (W - sw) // 2
                _draw_text_outline(d, (sx, sy), sl, f_small, (245, 245, 245), outline=(0, 0, 0), width=2)
                sy += 58
        tag_color = (255, 255, 255) if use_frame else (tuple(fg) if isinstance(fg, tuple) else (200, 200, 200))
        d.text((60, 70), "# " + style, font=f_small, fill=tag_color)
        d.rectangle([60, 70 + 60, 60 + 120, 70 + 66], fill=ACCENT)
    im.save(out_path, quality=90)
    return out_path


def _clean_cover_title(title: str) -> str:
    """清洗文案标题里带的分类/视角前缀，避免封面出现「【揭秘型】」「老板视角：」等套路词。"""
    if not title:
        return "未命名"
    t = title.strip()
    # 去掉 【XX型】前缀
    t = re.sub(r"^【[^】]+】\s*", "", t)
    # 去掉 "XXX视角：" 前缀
    t = re.sub(r"^[^：:]{1,10}视角[：:]\s*", "", t)
    return t.strip() or "未命名"


def _wrap(text: str, per: int):
    """智能换行：中文按字符，英文/数字按词，避免把 Java 拆成 J av。"""
    if not text:
        return [""]
    # 拆成词元：连续 ASCII 字母/数字为一个词元，其他字符单独成词元
    words = []
    cur = ""
    for ch in text:
        is_ascii_word = ord(ch) < 128 and ch.isalnum()
        if cur and ((ord(cur[-1]) < 128 and cur[-1].isalnum()) == is_ascii_word):
            cur += ch
        else:
            if cur:
                words.append(cur)
            cur = ch
    if cur:
        words.append(cur)
    lines, line = [], ""
    for w in words:
        w = w.strip() if w in " \t" else w
        if not w:
            continue
        if len(w) > per:
            if line:
                lines.append(line)
                line = ""
            for i in range(0, len(w), per):
                lines.append(w[i:i + per])
            continue
        if len(line) + len(w) > per and line:
            lines.append(line)
            line = ""
        line += w
    if line:
        lines.append(line)
    return lines or [""]


def _draw_text_outline(d, xy, text, font, fill, outline=(0, 0, 0), width=2):
    """描边文字，增强在复杂背景上的可读性。"""
    x, y = xy
    for dx in range(-width, width + 1):
        for dy in range(-width, width + 1):
            if dx == 0 and dy == 0:
                continue
            d.text((x + dx, y + dy), text, font=font, fill=outline)
    d.text((x, y), text, font=font, fill=fill)


def _draw_text_3d(d, xy, text, font, fill, shadow=(0, 0, 0), depth=6):
    """3D 立体挤出字：参考抖音爆款封面的厚重块面字效果。
    先按 depth 层向右下角错位绘制阴影/挤出面，再在最上层绘制主体色+描边。"""
    x, y = xy
    for i in range(depth, 0, -1):
        # 挤出面由深到浅略有变化，但整体偏暗
        shade = tuple(max(0, min(255, c - int(20 * (depth - i) / depth))) for c in shadow)
        d.text((x + i, y + i), text, font=font, fill=shade)
    # 最上层：主体色 + 黑色细描边，保证边缘锐利
    _draw_text_outline(d, (x, y), text, font, fill=fill, outline=(0, 0, 0), width=2)


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
