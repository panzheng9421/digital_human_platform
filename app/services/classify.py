"""文案智能分类（LLM 语义分类，基于项目已有的 DeepSeek 配置）。

规则关键词打分方案已废弃删除——它只能做字面词命中，读不出语义，
对「AI 个人网站」「我脑子活自己搭网站」这类不出现行业词但语义明确的
内容完全失效，且泛词必产生误判垃圾。

改用 LLM 主分类：把文案 + 行业/类型白名单发给 DeepSeek，要求只输出
固定 JSON，由模型做语义理解归类。模型不可用（无 key / 超时 / 异常）
时安全降级为「其他 / 解题型」，不再回退任何关键词规则。

行业体系与 app/data/viral_scripts.VIRAL_SCRIPTS 的键保持一致。
"""
import hashlib
import json

import requests as _requests

# 行业白名单（须与 VIRAL_SCRIPTS 的行业键保持一致；"其他"为兜底）
INDUSTRY_LIST = ["本地生活", "餐饮美食", "房产中介", "教育培训", "美妆护肤",
                 "服装穿搭", "健身减肥", "数码家电", "二手车", "其他"]
# 写法类型白名单
TYPE_LIST = ["解题型", "推荐型", "疑问型", "揭秘型", "案例型"]

DEFAULT_INDUSTRY = "其他"
DEFAULT_TYPE = "解题型"

# 同一条文案只调一次 LLM（classify_industry / classify_type 共享结果）
_CLASSIFY_CACHE = {}


def _cache_key(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8", "ignore")).hexdigest()


def _extract_json(content: str) -> dict:
    """从模型输出里抠出第一个 {...} JSON 块（容忍 ```json 包裹 / 前后废话）。"""
    if not content:
        return {}
    try:
        s, e = content.find("{"), content.rfind("}")
        if s >= 0 and e > s:
            return json.loads(content[s:e + 1])
    except Exception:
        pass
    return {}


def _call_llm_classify(text: str):
    """返回 (industry, type)。LLM 不可用则安全降级，不回退规则。"""
    key = _cache_key(text)
    if key in _CLASSIFY_CACHE:
        return _CLASSIFY_CACHE[key]

    from app import config
    if not config.LLM_API_KEY:
        print("[classify] 未配置 LLM_API_KEY，分类降级为 其他/解题型")
        result = (DEFAULT_INDUSTRY, DEFAULT_TYPE)
        _CLASSIFY_CACHE[key] = result
        return result

    industry_list = "、".join(INDUSTRY_LIST)
    type_list = "、".join(TYPE_LIST)
    system = (
        "你是短视频口播文案分类器。必须从给定白名单中各选一个行业和写法类型。\n"
        f"行业白名单：{industry_list}\n"
        f"写法类型白名单：{type_list}\n"
        "依据文案整体语义（而非个别关键词）判断。只输出 JSON，格式严格为 "
        '{"industry":"...","type":"..."}，不要任何解释、不要 markdown 代码块。'
    )
    user = f"文案：\n{text}"

    try:
        url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
        payload = {
            "model": config.LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {config.LLM_API_KEY}",
            "Content-Type": "application/json",
        }
        # 超时较短：分类失败不应拖死提取流程，直接降级
        r = _requests.post(url, headers=headers, json=payload, timeout=8)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        data = _extract_json(content)
        industry = data.get("industry", "")
        type_ = data.get("type", "")
        # 校验白名单，越界一律归默认（防模型胡编行业名污染数据）
        industry = industry if industry in INDUSTRY_LIST else DEFAULT_INDUSTRY
        type_ = type_ if type_ in TYPE_LIST else DEFAULT_TYPE
        result = (industry, type_)
    except Exception as e:
        print(f"[classify] LLM 分类调用失败，降级为 其他/解题型: {e}")
        result = (DEFAULT_INDUSTRY, DEFAULT_TYPE)

    _CLASSIFY_CACHE[key] = result
    return result


def classify_industry(text: str) -> str:
    """语义分类行业（LLM 主分类）。"""
    return _call_llm_classify(text)[0]


def classify_type(text: str) -> str:
    """语义分类写法类型（LLM 主分类）。"""
    return _call_llm_classify(text)[1]


def classify(text: str):
    """一次调用同时返回 (industry, type)，供想减少 LLM 往返的调用方使用。"""
    return _call_llm_classify(text)


def normalize_industry(name: str) -> str:
    """把任意输入归一到行业白名单键；匹配不到返回原值。"""
    if not name:
        return DEFAULT_INDUSTRY
    name = name.strip()
    if name in INDUSTRY_LIST:
        return name
    # 模糊包含兜底（数据规范化，非分类规则）
    for k in INDUSTRY_LIST:
        if k in name or name in k:
            return k
    return name
