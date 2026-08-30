"""文案服务：行业爆款改写 / 链接提取。
改写目前用“类型模板 + 人设语气 + 原文要点重组”生成，结构清晰、可直接替换 LLM。
接入真实 LLM：把 rewrite() 内部改为调用 config.LLM_* 即可，输入输出结构不变。
"""
import re

TYPES = ["解题型", "推荐型", "揭秘型", "案例型", "疑问型"]
PERSONAS = ["老板", "专家", "邻家大哥", "毒舌朋友"]

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


def _key_points(text: str, n=3):
    sents = _split_sentences(text)
    if not sents:
        return ["这个话题值得认真聊聊"]
    return sents[:n]


def rewrite(original: str, type_: str, persona: str) -> dict:
    """生成改写文案。返回 {title, generated_text}。"""
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

    # 标题
    topic = points[0][:12] if points else "干货分享"
    title = f"【{type_}】{persona}视角：{topic}"
    return {"title": title, "generated_text": generated}


def extract_from_link(url: str) -> dict:
    """链接提取文案：接百炼 Paraformer-v2 真实转写；未配置 key 时回退占位提示。"""
    from app.services import asr_client as ac
    if not ac.available():
        sample = ("这是从链接提取的示例文案（占位）。已接入百炼转写但缺少 DASHSCOPE_API_KEY，"
                  "请在 start.bat 设置后重试，或在下方文本框直接粘贴真实口播文案。")
        return {"original_text": sample, "source_url": url,
                "note": "未配置百炼 DASHSCOPE_API_KEY，当前为占位提取"}
    return {"original_text": ac.extract_from_link(url), "source_url": url, "note": ""}


def extract_from_file(local_path: str) -> str:
    """本地上传的音视频文件 -> 百炼 Paraformer-v2 转写。"""
    from app.services import asr_client as ac
    return ac.extract_from_file(local_path)
