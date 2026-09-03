"""违禁词 / 广告法极限词库（示例，可扩充）。
用于文案改写后的合规自检。命中后前端高亮提示，用户可手动替换。
生产环境建议接入更完整的词库或第三方合规 API。
"""

# 广告法明令禁止的“绝对化用语”
ABSOLUTE_WORDS = [
    "国家级", "最高级", "最佳", "第一品牌", "独一无二", "绝无仅有", "前无古人",
    "顶级", "顶尖", "极致", "绝对", "百分百", "100%", "全网第一", "销量第一",
    "唯一", "首家", "最强", "最优", "最低价", "最便宜", "王牌", "领袖品牌",
    "世界领先", "行业领先", "领导品牌", "领先上市", "史无前例", "万能",
]

# 医疗/功效违规暗示（非特殊用途化妆品、普通食品不得宣称）
MEDICAL_CLAIM_WORDS = [
    "治疗", "治愈", "根治", "防癌", "抗癌", "消炎", "杀菌", "药用", "疗效",
    "一针见效", "包治", "无副作用", "纯天然无添加", "祛病", "降血糖", "降血压",
]

# 诱导/承诺收益（金融、加盟类高危）
PROMISE_WORDS = [
    "稳赚", "包赚", " guaranteed", " guaranteed收益", "零风险", "一本万利",
    "日入过万", "月入十万", "躺赚", " guaranteed回报", "保底", "无脑赚",
]

# 低俗/敏感
SENSITIVE_WORDS = [
    "微信", "加我", "私聊", "私信我", "二维码", "微信号", "VX", "v信",
    "最便宜", "免费送", "点击链接", "立即下载",
]

ALL_WORDS = ABSOLUTE_WORDS + MEDICAL_CLAIM_WORDS + PROMISE_WORDS + SENSITIVE_WORDS


def check(text: str):
    """返回命中列表：[{word, category}]。"""
    hits = []
    groups = [
        ("绝对化用语(广告法违规)", ABSOLUTE_WORDS),
        ("医疗/功效违规暗示", MEDICAL_CLAIM_WORDS),
        ("诱导/承诺收益", PROMISE_WORDS),
        ("导流/低俗敏感", SENSITIVE_WORDS),
    ]
    seen = set()
    for cat, words in groups:
        for w in words:
            if w and w in text and w not in seen:
                hits.append({"word": w, "category": cat})
                seen.add(w)
    return hits
