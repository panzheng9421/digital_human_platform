"""文案服务：行业爆款改写 / 链接提取。
改写目前用“类型模板 + 人设语气 + 原文要点重组”生成，结构清晰、可直接替换 LLM。
接入真实 LLM：把 rewrite() 内部改为调用 config.LLM_* 即可，输入输出结构不变。
"""
import re
import requests as _requests

TYPES = ["解题型", "推荐型", "揭秘型", "案例型", "疑问型"]
PERSONAS = ["老板", "专家", "邻家大哥", "毒舌朋友"]

REWRITE_WORD_TOLERANCE = 50  # 改写稿相对目标字数的浮动上限（字），内部兜底，不告诉 LLM
REWRITE_MAX_LEN = 400        # 改写稿字数硬上限：原文字数超过它时按 400 压缩（长口播原文不需要等长改写）


def _target_len(original: str) -> int:
    """改写稿的目标字数上限：短原文按原文字数，长原文压到 REWRITE_MAX_LEN。"""
    return min(_word_count(original), REWRITE_MAX_LEN)

_PERSONA_OPEN = {
    "老板": "我是个开了多年店的老板，今天说点大实话。",
    "专家": "从业十几年，今天从专业角度给你讲明白。",
    "邻家大哥": "哥们儿跟你掏心窝子唠两句。",
    "毒舌朋友": "别不爱听，有些话就得有人跟你说透。",
}
_PERSONA_CLOSE = {
    "老板": "做生意讲究个实在，觉得在理点个关注，以后常来。",
    "专家": "记住这点，少走弯路。关注我，持续讲干货。",
    "邻家大哥": "都是自己人，照着做准没错，关注不迷路。",
    "毒舌朋友": "听劝的已经去做了，剩下的随缘。点关注，下次接着骂。",
}

_TYPE_HOOK = {
    "解题型": "很多人卡在这个问题上一直解决不了，其实就三步。",
    "推荐型": "今天给你推荐一个我真心觉得不错的，闭眼入不踩雷。",
    "揭秘型": "行业内不愿让人知道的事，今天我给你掀开说。",
    "案例型": "我身边一个真实例子，看完你就懂了。",
    "疑问型": "你有没有想过，为什么同样的事别人做就行你做就废？",
}


def _split_sentences(text: str):
    text = re.sub(r"\s+", "，", text).strip("，。")
    parts = re.split(r"[。！？.!?]", text)
    return [p for p in parts if len(p) > 4]


def _word_count(text: str) -> int:
    """与前端对齐：去除所有空白字符后的长度。"""
    return len(re.sub(r"\s+", "", text or ""))


def _clamp_text(text: str, max_len: int) -> tuple:
    """将 text 截断到不超过 max_len 字（去空白计数）。
    优先按句子截断以保证语义完整，极端情况下硬截，并返回提示语。"""
    if _word_count(text) <= max_len:
        return text, ""
    # 按句末标点切分，保留标点
    parts = re.split(r"([。！？\.\n])", text)
    # 把标点和前文拼成完整句子
    sentences = []
    i = 0
    while i < len(parts):
        s = parts[i]
        if i + 1 < len(parts) and parts[i + 1] in "。！？.\n":
            s += parts[i + 1]
            i += 2
        else:
            i += 1
        s = s.strip()
        if s:
            sentences.append(s)
    # 贪心按句追加，直到不超限
    buf = ""
    for s in sentences:
        candidate = (buf + "\n" + s).strip() if buf else s
        if _word_count(candidate) <= max_len:
            buf = candidate
        else:
            break
    # 兜底：连第一句都超，就硬截；尽量落在句末标点，避免半截话
    if not buf:
        text_no_space = re.sub(r"\s+", "", text)
        cut = text_no_space[:max_len]
        last_punc = max(cut.rfind(p) for p in "。！？.")
        buf = cut[: last_punc + 1] if last_punc > 0 else cut
    note = (f"改写稿超出字数上限（{max_len} 字 = 目标字数 + {REWRITE_WORD_TOLERANCE} 字浮动余量），"
            f"超出的尾句已丢弃。")
    return buf, note


def _topic_snippet(text: str, max_len: int = 12) -> str:
    """提取标题主题，按词/句边界截断，避免英文单词被拆开。"""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    # 优先按空格或中英文标点切分
    boundaries = " \t，,。！!？?；;:："
    cut = max_len
    while cut > 0 and text[cut] not in boundaries:
        cut -= 1
    if cut <= 0:
        cut = max_len
    return text[:cut].rstrip(boundaries)


def _parse_rewrite_json(raw: str, target_len: int):
    """把 LLM 返回的 JSON（{script, cover_title, cover_subtitle}）解析出来。
    兼容 ```json 围栏与纯文本回退；script 走字数兜底截断，封面字段缺失则为空。
    若 JSON 整体解析失败，仍尝试用正则把 cover_title/cover_subtitle 抠出来，尽量不空手而归。"""
    import json as _json
    import re as _re
    s = (raw or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    script, cover_title, cover_subtitle = raw, "", ""
    try:
        obj = _json.loads(s)
        script = obj.get("script") or raw
        cover_title = (obj.get("cover_title") or "").strip()
        cover_subtitle = (obj.get("cover_subtitle") or "").strip()
    except Exception:
        # 整体 JSON 解析失败时，尽量抢救封面字段
        m_title = _re.search(r'"cover_title"\s*:\s*"([^"]*)"', raw or "")
        m_sub = _re.search(r'"cover_subtitle"\s*:\s*"([^"]*)"', raw or "")
        if m_title:
            cover_title = m_title.group(1).strip()
        if m_sub:
            cover_subtitle = m_sub.group(1).strip()
    script, note = _clamp_text(script, target_len + REWRITE_WORD_TOLERANCE)
    return script, cover_title, cover_subtitle, note


def _key_points(text: str, n=3):
    sents = _split_sentences(text)
    if not sents:
        return ["这个话题值得认真聊聊"]
    return sents[:n]


_PERSONA_DESC = {
    "老板": "你是开了多年实体店的老板，务实老练、说大实话、接地气，像跟老顾客唠嗑，不端着。",
    "专家": "你是从业十几年的行业专家，专业权威、条理清晰，给出可信判断和实用干货。",
    "邻家大哥": "你是亲切随和的邻家大哥，掏心窝子、像朋友聊天，语气轻松好懂。",
    "毒舌朋友": "你是敢说大实话的毒舌朋友，犀利直白、不绕弯子，带点调侃但句句在理。",
}
_TYPE_DESC = {
    "解题型": "结构：先抛出一个普遍痛点，再给'分步解决方案'（第1步/第2步/第3步），每步讲清怎么做。",
    "推荐型": "结构：直接推荐一个具体东西，讲清楚'为什么值得入手/闭眼入不踩雷'，语气真诚。",
    "揭秘型": "结构：逐条揭露'行业内不愿让人知道的真相'（第一个真相/第二个真相...），制造信息差。",
    "案例型": "结构：用一个'我身边/我认识的真实例子'带出观点，有前因后果和结果。",
    "疑问型": "结构：先抛一个'你有没有想过'的扎心问题，再给出直白答案，点破关键。",
}


def _call_deepseek(system: str, user: str, temperature: float = 0.8) -> str:
    """调用 DeepSeek（OpenAI 兼容 /chat/completions）。未配置 key 或失败抛异常。"""
    from app import config
    if not config.LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY（请在 start.bat 设置 LLM_API_KEY）")
    url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    r = _requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def _rewrite_template(original: str, type_: str, persona: str) -> dict:
    """无 LLM key / LLM 失败时的规则模板回退（结构清晰、零依赖）。"""
    type_ = type_ if type_ in TYPES else "解题型"
    persona = persona if persona in PERSONAS else "老板"
    points = _key_points(original, 3)
    open_line = _PERSONA_OPEN.get(persona, _PERSONA_OPEN["老板"])
    hook = _TYPE_HOOK.get(type_, _TYPE_HOOK["解题型"])
    body = ""
    if type_ == "解题型":
        for i, p in enumerate(points, 1):
            body += f"第{i}步，{p}。把这一步做到位，问题就解决一大半。\n"
    elif type_ == "推荐型":
        body = f"它就是——{points[0]}。\n为什么推荐？{points[1] if len(points) > 1 else '用过的人都知道香'}。\n别犹豫，该入手就入手。\n"
    elif type_ == "揭秘型":
        body = f"第一个真相：{points[0]}。\n第二个真相：{points[1] if len(points) > 1 else '水比你想象的深'}。\n第三个真相：{points[2] if len(points) > 2 else '别被表象骗了'}。\n"
    elif type_ == "案例型":
        body = f"我认识一个人，{points[0]}。\n后来他怎么做的？{points[1] if len(points) > 1 else '换了思路'}。\n结果你猜怎么着，{points[2] if len(points) > 2 else '真就翻身了'}。\n"
    elif type_ == "疑问型":
        body = f"其实答案很简单：{points[0]}。\n你一直做不对，是因为{points[1] if len(points) > 1 else '没抓到关键'}。\n换个方法试试，马上不一样。\n"
    close = _PERSONA_CLOSE.get(persona, _PERSONA_CLOSE["老板"])
    generated = f"{open_line}\n{hook}\n{body}\n{close}"
    generated, note = _clamp_text(generated, _target_len(original) + REWRITE_WORD_TOLERANCE)
    topic = _topic_snippet(points[0], 12) if points else "干货分享"
    title = f"【{type_}】{persona}视角：{topic}"
    # 封面标题/副标题：短视频封面要短、有冲突感，能勾人点进来；副标题补一句利益点或悬念
    cover_title = _topic_snippet(points[0], 9) if points else _topic_snippet(topic, 9)
    cover_subtitle = _topic_snippet(f"{persona}亲测，少走弯路", 16)
    res = {"title": title, "generated_text": generated,
           "cover_title": cover_title, "cover_subtitle": cover_subtitle}
    if note:
        res["note"] = note
    return res


def rewrite(original: str, type_: str, persona: str) -> dict:
    """生成改写文案。优先调用 DeepSeek（真实 LLM）；未配置 key 或失败回退规则模板。
    返回 {title, generated_text, source, note}：source=llm|template。"""
    type_ = type_ if type_ in TYPES else "解题型"
    persona = persona if persona in PERSONAS else "老板"
    try:
        original_len = _word_count(original)
        target_len = _target_len(original)
        # 原文超长时明确要求压缩提炼，避免 LLM 逐句复述导致严重超限
        compress_hint = ""
        if target_len < original_len:
            compress_hint = (f"注意：原文有 {original_len} 字，属于长稿，请压缩提炼最核心的观点与信息，"
                             f"不要逐句复述，也不要为了凑字数硬加内容。\n")
        system = (
            "你是短视频口播文案改写助手。任务：把用户给的原始口播文案，改写成一篇新的短视频口播稿。\n"
            "【输出格式，必须严格遵守】请用 JSON 格式输出，不要任何额外解释、不要加 markdown 围栏。"
            '结构：{"script": "改写后的口播文案正文（不要书名号或引号包裹）", '
            '"cover_title": "短视频封面大标题，最多9个字（必须严格控制在9个汉字以内，含数字/字母），必须有冲突感、情绪感或悬念感，能勾人点进来（例如“千万别这样”、“95%人踩坑”），不要直接照搬原文开头", '
            '"cover_subtitle": "封面副标题，不超过16字，补一句利益点、身份背书或悬念，可留空字符串"}\n'
            "内容要求：1) 保留原文核心观点与关键信息，不得编造虚假数据或数字；2) 口语化、有节奏感，适合真人念出来；\n"
            "3) 严格遵循指定'人设语气'；4) 按指定'写法结构'组织内容；5) 结尾自然收束，不堆砌营销话术；\n"
            f"6) 改写后的口播稿字数（去除空白字符后的连续字符数）必须严格不超过 {target_len} 字，"
            f"并尽量接近该字数，不要为凑字数硬加内容；\n"
            "7) 口播断句友好（重要）：语音合成模型容易在「动词 + 方位词」之间误断句，"
            "例如把'修车路上'念成'修车。路上'、把'开车车上'念成'开车。车上'。为避免这种尴尬断句，"
            "输出时请把这类组合写成带'的'的连读形式：'修车路上'→'修车的路上'、'开车车上'→'开车的车上'；"
            "同理'XX店里 / XX家里 / XX学校 / XX车上 / XX路上'也优先写成'XX的店里'等连读形式。"
            "目的只是让句子念出来连贯，不要因此删改原意或硬凑字数。"
        )
        user = (
            f"人设语气：{_PERSONA_DESC.get(persona, _PERSONA_DESC['老板'])}\n"
            f"写法结构：{_TYPE_DESC.get(type_, _TYPE_DESC['解题型'])}\n"
            f"原始文案字数（去除空白）：{original_len} 字\n"
            f"改写稿字数上限：{target_len} 字（不得超过）\n"
            f"{compress_hint}\n"
            f"原始文案：\n{original}\n\n请输出改写后的口播文案："
        )
        raw = _call_deepseek(system, user)
        generated, cover_title, cover_subtitle, note = _parse_rewrite_json(raw, target_len)
        # 封面标题严格不超过 9 字；LLM 没给时从改写稿第一句取（比原文主题更贴近改写后的卖点）
        if cover_title:
            cover_title = _topic_snippet(cover_title, 9)
        else:
            first_line = re.split(r"[。！？\n]", generated)[0] if generated else ""
            cover_title = _topic_snippet(first_line, 9) or _topic_snippet(original, 9) or "干货分享"
        if cover_subtitle:
            cover_subtitle = _topic_snippet(cover_subtitle, 16)
        topic = _topic_snippet(original, 12) or "干货分享"
        title = f"【{type_}】{persona}视角：{topic}"
        return {"title": title, "generated_text": generated,
                "cover_title": cover_title, "cover_subtitle": cover_subtitle,
                "source": "llm", "note": note}
    except Exception as e:
        print(f"[rewrite] LLM 不可用，回退规则模板：{e}")
        res = _rewrite_template(original, type_, persona)
        res["source"] = "template"
        fallback_note = f"LLM 调用失败（{e}），已用本地模板生成"
        existing_note = res.get("note", "")
        res["note"] = f"{fallback_note}{'；' + existing_note if existing_note else ''}"
        return res


def extract_from_link(url: str) -> dict:
    """链接提取文案：接百炼 Paraformer-v2 真实转写；未配置 key 时回退占位提示。
    返回 {original_text, source_url, industry, type, meta, note}，industry/type 为智能分类结果。"""
    from app.services import asr_client as ac
    from app.services import classify as cl
    if not ac.available():
        sample = ("这是从链接提取的示例文案（占位）。已接入百炼转写但缺少 DASHSCOPE_API_KEY，"
                  "请在 start.bat 设置后重试，或在下方文本框直接粘贴真实口播文案。")
        return {"original_text": sample, "source_url": url, "industry": "", "type": "", "meta": {},
                "note": "未配置百炼 DASHSCOPE_API_KEY，当前为占位提取"}
    res = ac.extract_from_link(url)
    text = res.get("text", "")
    if not isinstance(text, str):
        # 防御：极少数情况下 text 不是字符串，强制转字符串避免前端显示 [object Object]
        text = str(text) if text is not None else ""
    return {
        "original_text": text,
        "source_url": url,
        "industry": cl.classify_industry(text),
        "type": cl.classify_type(text),
        "meta": res.get("meta", {}) if isinstance(res.get("meta"), dict) else {},
        "note": "",
    }


def extract_from_file(local_path: str) -> dict:
    """本地上传的音视频文件 -> 百炼 Paraformer-v2 转写；返回含智能分类的 dict。"""
    from app.services import asr_client as ac
    from app.services import classify as cl
    res = ac.extract_from_file(local_path)
    text = res.get("text", "")
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    return {
        "original_text": text,
        "source_url": "",
        "industry": cl.classify_industry(text),
        "type": cl.classify_type(text),
        "meta": res.get("meta", {}) if isinstance(res.get("meta"), dict) else {},
        "note": "",
    }
