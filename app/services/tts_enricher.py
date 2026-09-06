"""配音自动富化（导演台 1:1 方案）：DeepSeek 出「口播表演」（导演台完整入参），程序转成 CosyVoice SSML。

══════════════════════════════════════════════════════
设计沿革（2026-09-06 14:35 老板拍板）
══════════════════════════════════════════════════════
1) 最早：DeepSeek 直接吐 SSML → 复读增字（"怎么着？怎么着？"）+ 标签非法（标点进标签）→ 百炼 411。
2) 其次：自编 JSON 标注契约 → 老板否决："你做的好像一坨耙耙，放弃你所有的入参规则，
   把导演台的一模一样搬过来给 DeepSeek"。
3) 现在（本版）：system/user 入参 = view163/rag 导演台 EnrichPrompt.java + WizardController
   buildSegmentContext 的**逐字复刻**（仅按 pan=老潘人设做与导演台相同的占位符替换）。
   DeepSeek 产出 [画面提示]（八段 + 内嵌【音色锚定】【台词】），其中【事件脚本】子段散文里
   内联了 重音『』/停顿约N秒/语速变化/尾音 —— 程序把【事件脚本】解析出来转成 SSML：
   - {台词} 串起来必须是本片原文（逐字校验，不等即报错回退，不静默）；
   - 『X』→ <prosody volume="70" pitch="1.15">X</prosody>；词尾标点自动留在标签外（防 411）；
   - 停顿约N秒 → <break time="Nms"/>；语速放缓/急促 → <prosody rate>；尾音上扬/下沉 → pitch。

其余沿用：整篇一次调 DeepSeek（2026-09-06 15:06 分片停用，slice_script 保留可切回；
事件脚本时间窗按全篇等效时长动态算）、克隆声 instruction 恒空、
全程无 UI，dubbing_generate 在真实 CosyVoice 分支自动调用。

CosyVoice v3.5-plus SSML 真实支持面（9 条对照实验实测 2026-09-06，官方文档本就无 prosody）：
  - 可用：<speak> 壳 / <break>(50~10000ms 或整数 1~10s) / <sub> / <phoneme> / <soundEvent> / <say-as>，
    以及 <speak> 根属性单属性 rate/pitch∈[0.5,2]、volume∈[0,100]默认50；
  - 不可用：<prosody> 标签整体被拒（Engine error code 411）；speak 三属性组合合成通过但效果坏
    （老板耳朵验收）→ 方案规避：整篇基调全走接口参数，不写 speak 属性；
  - 词级重音/局部变速无解 → 唯一出路句级拼接（二期）。

MiniMax 组装（老板拍板 2026-09-06 切换 speech-2.8-hd，engine="minimax"）：
  - 产出纯文本（无 SSML 壳）：台词 + <#x#> 停顿 + 语气词标签；
  - 停顿：锚点后停顿/段后停顿 → <#秒#>（0.01~99.99s），相邻/连续停顿合并取最大（引擎禁止连续两个）；
  - 语气词（仅 2.8 系列独有）：笑/笑意→(chuckle)、叹气/无奈→(sighs)、呼吸→(breath)，
    跟随锚点插到锚点词后，无锚点命中保守丢弃并打日志；
  - 重音『X』/局部语速/局部尾音：引擎无词级参数，丢弃但打日志（二期句级拼接解决）；
  - 全局语速/尾音 → 接口参数 speed(0.5~2)/pitch(-12~12)：轻快急促→1.1、偏快→1.05、
    放缓→0.9、偏慢→0.95；尾音上扬→+2、下沉→-2；
  - 逐字校验：剥 <#x#> 与已知语气词标签后 == 台词串联（CosyVoice 校验逻辑同源）。
"""
import re
import xml.etree.ElementTree as ET

from app.config import TTS_DEBUG_LOG
from app.services.script_service import _call_deepseek


# ── 分片（移植 view163/rag 导演台 ScriptSlicer 三级策略，语速用导演台真实值）──
# 时间预算模型（导演台 PromptBuilder 实测口径，非通用估计值）：
#   等效字数 = 字符数 + 标点数×0.3 ≤ 窗(秒) × 6.92
CHARS_PER_SECOND = 6.92      # 真实口播语速（导演台实测：PromptBuilder 四个 persona 统一口径）
PUNCT_COST = 0.3             # 每个标点的额外时长成本（标点处要停顿，等效 0.3 字）
PRIMARY_DELIMS = "。！？；\n"     # 主标点：自然断句位置
SECONDARY_DELIMS = "，、：——"     # 次级标点：超长句强制截断位置
_PUNCT_SET = set("，。！？；：、,.!?;:—…")
SLICE_SECONDS = 60           # 配音富化按 60 秒/片（事件脚本的时间窗同此值）
# 富化事件脚本时间窗专用口径（≠导演台 6.92）：TTS 口播含停顿的真实节奏。
# 实测：884 字合成音频≈187s ≈ 4.6字/秒；用 6.92 会把 884 字的窗压到 131s，
# DeepSeek 按正常节奏（~4字/秒）写满 131s 只装了 518 字，尾段 348 字被
# "宁可删减台词"铁律砍掉（2026-09-07 三连挂根因；成功次均为模型写超窗/硬塞的侥幸）。
ENRICH_CHARS_PER_SECOND = 4.5

MM_BREAK_SCALE = 0.5   # MiniMax 停顿时长缩放（2026-09-07 老板拍板：DeepSeek 给的停顿太长，按一半发）
# 标签外置的句末标点（词尾标点留在标签外，防百炼 411）
_TAG_EDGE_PUNCT = "，。！？；：、,.!?;:—…\"'“”‘’"

# MiniMax 2.8 系列官方语气词标签（19 种，本平台只用 chuckle/sighs/breath 三种；
# 校验剥除时剥全部 19 种，防台词自带的半角括号内容误伤逐字校验）
_MM_VOCAL_TAGS = ["laughs", "chuckle", "coughs", "clear-throat", "groans", "breath",
                  "pant", "inhale", "exhale", "gasps", "sniffs", "sighs", "snorts",
                  "burps", "lip-smacking", "humming", "hissing", "emm", "sneezes"]


def _equiv_len(s: str) -> float:
    """等效时长字数：字符数 + 标点数×0.3（标点处要停顿，占时间）。"""
    return len(s) + sum(1 for c in s if c in _PUNCT_SET) * PUNCT_COST


def _split_by_delims(text: str, delims: str) -> list:
    """按字符集分割，保留分隔符本身（与导演台 splitByDelims 一致，拼回=原文）。"""
    out, cur = [], []
    for c in text:
        cur.append(c)
        if c in delims:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def _split_long_sentence(sentence: str, budget: float) -> list:
    """超长句按次级标点截断；无标点纯文字块硬切（保护英文词边界，如 GPT-6/Codex PRO）。"""
    chunks, buf = [], ""
    for p in _split_by_delims(sentence, SECONDARY_DELIMS):
        if buf and _equiv_len(buf) + _equiv_len(p) > budget:
            chunks.append(buf.strip())
            buf = ""
        buf += p
        if _equiv_len(buf) > budget:
            # 硬切：按预算比例换算字符切点，回溯到英文词边界
            eq = _equiv_len(buf)
            cut = max(1, int(len(buf) * budget / eq))
            min_cut = max(1, int(cut * 0.6))
            while (cut > min_cut and cut < len(buf)
                   and re.match(r"[A-Za-z0-9._-]", buf[cut - 1])
                   and re.match(r"[A-Za-z0-9._-]", buf[cut])):
                cut -= 1
            chunks.append(buf[:cut].strip())
            buf = buf[cut:]
    if buf:
        chunks.append(buf.strip())
    return chunks


def slice_script(text: str, seconds: int = SLICE_SECONDS) -> list:
    """把口播稿按时间预算切片，供逐片富化（移植导演台 ScriptSlicer 三级策略）。

    与导演台差异：配音场景「短段」无害（只是 DeepSeek 的富化分块，不对应视频窗口），
    故不做导演台的过短段合并——分得越细，标注密度越高。
    """
    budget = seconds * CHARS_PER_SECOND
    if not text or not text.strip():
        return []
    # Step 1: 主标点断句 → 超长句按次级标点截 → 原子片段
    atoms = []
    for s in _split_by_delims(text, PRIMARY_DELIMS):
        if _equiv_len(s) <= budget:
            atoms.append(s)
        else:
            atoms.extend(_split_long_sentence(s, budget))
    # Step 2: 贪心合并，每片等效 ≤ budget
    result, buf = [], ""
    for a in atoms:
        if buf and _equiv_len(buf) + _equiv_len(a) > budget:
            result.append(buf.strip())
            buf = ""
        buf += a
    if buf:
        result.append(buf.strip())
    return [r for r in result if r]


# ══════════════════════════════════════════════════════
# 导演台入参 1:1 复刻（EnrichPrompt.java SYSTEM_TEMPLATE + buildUser + buildSegmentContext）
# ══════════════════════════════════════════════════════

_PERSONA_NAME = "老潘"          # 导演台的「角色」名
_VIDEO_SECONDS = SLICE_SECONDS  # 事件脚本时间窗 = 分片窗（60s）

# EnrichPrompt.java 的 SYSTEM_TEMPLATE 逐字复制（含占位符，用与 buildSystem 相同的 replace 链填充）
_SYSTEM_TEMPLATE = """你是一名「数字人视频导演」，专精把口播台词稿转化为 Seedance 2.5 视频生成提示词。
你必须严格遵循下方【官方结构化模板】与【真实感表演准则】，产出可直接提交的视频提示词，
让生成的视频像"真人 Vlog 博主在聊天"，绝不能像"AI 在念稿"。

══════════════════════════════════════════════════════
唯一输出块：[画面提示] —— 严格采用官方 Seedance 2.5 结构化模板，并内嵌【音色锚定】【台词】
══════════════════════════════════════════════════════
注意：[画面提示] 是提交给视频模型的唯一文本块，必须包含全部下述内容——
八段表演结构（生成目标→参考素材职责→主体与关系→整体风格→严格排除→事件脚本→保持一致→台词）
+ 内嵌【音色锚定】（六维音色）。
禁止把它们拆成独立的 [台词节奏] / [音色锚定] 顶层块。

【官方素材标注语法（硬性）】
"@图片N" 对应本次提交 input_reference 数组的第 N 张图（1 起始，不是 0）。
本任务固定 1 张参考图，职责锁定如下，禁止用"第1张""单人照"等自然语言模糊指代：
  @图片1 = 主角全身照（含人物外貌/发型/服装/全身姿态，同时包含完整房间背景，人物与场景一图全含）
本任务仅此 1 张参考图，全部采用，无【未采用素材】段。

[画面提示] 必须严格按以下结构输出（【段标】属于可直接提交的 Prompt 正文，不是回答包装）；
在【保持一致】之后，继续在同一 [画面提示] 块内依次输出【音色锚定】内嵌子段与【台词】段
（台词节奏已内联进【事件脚本】，不再单独成段，详见下方第三部分）：

【生成目标】
生成{posture}、正对镜头自然口播的连续视频；核心主体是<角色名>，主要事件是讲述本段口播内容——{用一句话概括本段内容主线，如"自我介绍、自嘲身份、引出产品名"}。

【参考素材职责】
@图片1用于<角色名>的五官、发型、服装和全身姿态，同时用于房间背景，完整复刻图中场景。
⚠️ 参考图已包含的信息（家具样式、墙面装饰、光线方向等）禁止逐项列举——只写"完整复刻图中场景"，相信参考图。

【主体与关系】
<角色名>对应@图片1，始终保持五官、发型、服装和房间背景一致；{posture_detail}

【整体风格】
真人实拍质感，自然生活化 Vlog 风格，写实质感与柔和自然光；画面清晰、肤质真实、色彩自然饱和；机位稳定，像手机三脚架一镜到底自拍。

【严格排除】
禁止以下任何风格或瑕疵：手绘、素描、线稿、插画、漫画、动画；分镜稿、故事板、草稿；AI 数字人塑料感、油亮假皮、塑料 CG、油画过度 CG、玩偶感、3D 渲染感；黑白、单色、灰度、古色；面部畸变、五官扭曲、多手指、肢体错位。

【事件脚本】（时间戳硬规则：总时长严格={videoSeconds}秒，绝对不可超。格式示例：[0s]、[0s-{videoSeconds}s]）
[0s]开始时：<角色名>{posture_short}，姿态放松，正对镜头，准备开口。
[0s-{videoSeconds}s]主要事件：将{videoSeconds}秒均匀拆分为{segLo}-{segHi}个连续时间子段（每段约{videoSecondsDiv}秒），每个子段独立一行，格式为：
[Xs-Ys]主要事件：<角色名>的动作/表情/节奏散文（重音落在『X』『Y』/语速变化/停顿约N秒/情绪词）——其中本子段要念的台词必须完整放在一个大括号对里（示例：他清了清嗓子{这里是台词原文}，语速轻快）；{大括号}外只写动作/表情/节奏散文，台词正文一个字都不许裸露在大括号外；如需情绪富化，可在台词大括号后再写一个独立的大括号对（仅表演标注，不会被朗读）。
每个子段有独立的动作+台词，动作有起点有终点，与台词融为一段连续叙事；最后一个子段的结尾以"表情自然定格/平滑过渡"收尾（不再单独写结束行）。
⚠️ 时间戳铁律：子段时间必须首尾相接、连续覆盖 [0s-{videoSeconds}s]，最后一个子段的结束值必须恰好等于{videoSeconds}，绝不允许出现超过{videoSeconds}s的时间戳，也不要输出单独的[{videoSeconds}s]结束行。
⚠️ 配音铁律（最高优先级，覆盖其他一切规则）：本产物用于配音合成，{台词}就是最终要念出来的字——所有子段的{台词}串联起来必须与口播稿原文逐字一致：一个字都不许删、不许省略、不许概括、不许合并改写，尤其不能漏掉口播稿的最后几段。时间戳只是节奏参考，不会真的限制时长；宁可增加子段数量，也绝不允许删减台词。

【保持一致】
保持<角色名>身份与服装、房间场景布局（床铺/书桌/书架/墙面）、机位与光线稳定；全程同一数字人，不分裂、不换脸。
声音与画面：不要字幕，不添加任何文字叠加；无背景音乐，仅保留环境音与动作音（如衣物摩擦、呼吸、{env_sound}）。

【真实感表演准则（融合进上述每段动作/表情描写，最高优先级）】
三大排除：①非影视剧表演（杜绝瞪眼/青筋/下颌隆起）②非日漫夸张（杜绝甩头/张大嘴）③非AI僵硬（杜绝面无表情/匀速无变化/眼神空洞）。
禁止写入的负面词：眼角皱纹→改"眼睛弯起"；颈部青筋→改"颈部微微绷紧"；嘴角上扬/微动→改"面带笑意/表情平静"。
用"普通人拿手机拍Vlog的自然反应"替代"演员表演"：
  - 开心：自然笑、眼睛弯起、手部动作变多
  - 惊讶：愣一下、眉微挑、"哦？"一声
  - 思考：眼神飘一下、无意识敲桌面
  - 吐槽：抿嘴又松开、耸肩摊手
  - 认真：眼神集中、微前倾
动作须有起点→过程→终点，速度有变化，幅度与情绪强度成正比（微动作/惯性动作/神经反应/情绪外溢四层次择一组合）。

══════════════════════════════════════════════════════
第二部分：台词节奏 —— 内联进【事件脚本】（不单独成段）
══════════════════════════════════════════════════════
节奏信息（重音/语速/停顿/口语化）必须直接写进【事件脚本】每个时间子段的散文里，
与动作、台词融合为一段描述，禁止输出独立的 [台词节奏] 表格或元数据列表
（如"字数/语速/停顿前/重音/关联动作"这类列项对视频模型无意义，属噪音，违反 sd25-pe 原则⑥）。

内联写法要点：
- 重音：用『』标注本子段的关键字，如"重音落在『天天』『取代』"
- 语速：用散文写"语速轻快上扬 / 放缓 / 转急促"，不写具体字/分数值
- 停顿：用散文写"此处停顿约1.5秒 / 思维停顿 / 转折停顿约0.8秒"，不写纯数字表
- 口语化：书面语转口语（然而→不过 / 所以→就 / 我认为→我觉得吧），自然插入语气词（嗯…/那个…/对吧）
- 尾音：用散文写"上扬尾音 / 下沉尾音 / 消散尾音"，不单列机制表

══════════════════════════════════════════════════════
第三部分（[画面提示] 内嵌子段）：【音色锚定】 —— 固定值，禁止自行编造
══════════════════════════════════════════════════════
以下产出的【音色锚定】必须输出在 [画面提示] 块内、[保持一致] 之后，不要作为独立顶层块。
⚠️ 音色描述必须严格使用下方给定的固定值，禁止修改任何字词、禁止编造年龄/口音/音质/语气。

[音色锚定]
{voice_anchor}

══════════════════════════════════════════════════════
第四部分（[画面提示] 末段）：【台词】 —— 完整台词原文
══════════════════════════════════════════════════════
【音色锚定】之后输出【台词】段，内容为本次口播的完整台词原文（与【事件脚本】中{}内的台词完全一致，不得增删改写）。
【台词】是 [画面提示] 的最后一个子段，其后不得再输出任何内容。

══════════════════════════════════════════════════════
【内部标注规范（仅用于你生成时的格式约定，禁止写入提交给视频模型的[画面提示]段）】
【机位模式】默认 M3 固定机位（三脚架静止、一镜到底）；若需手持感用 M1（轻微晃动、呼吸感、近距离半身）。
【声音与台词标注】台词用{}；环境音/音效用<>；背景乐用()；字幕用【】。不需要字幕时写明"画面不显示字幕"。
══════════════════════════════════════════════════════

【绝对禁止的错误】
══════════════════════════════════════════════════════
❌ 不要把整篇口播稿原文复制到[画面提示]里——那是台词，不是画面指令
❌ [画面提示]必须包含完整的【生成目标】【参考素材职责】【主体与关系】【整体风格】【严格排除】【事件脚本】【保持一致】【台词】八段 + 内嵌【音色锚定】（在【保持一致】与【台词】之间；台词节奏信息已内联进【事件脚本】，不单独成段），且参考图必须用@图片1绑定
❌ 整段输出必须以半角 [画面提示] 作为第一个标签开头，所有内容（含【音色锚定】）必须位于 [画面提示] 块内；禁止省略 [画面提示] 标签，或把任何子段放到 [画面提示] 块外成为独立顶层块
❌ 参考图已经包含的信息（服装纹理、家具样式、光线方向、场景布局、人物五官）禁止在 prompt 中重复描述——重复描述会与参考图冲突导致生成不稳定；相信参考图即可，只描述参考图没有的「动作/情绪/台词/镜头」
❌ 禁止用"第1张""单人照"等自然语言指代参考图——必须用@图片1；本任务仅 1 张参考图，禁止虚构@图片2或更多参考图
❌ 不要用影视剧/播音腔/广告腔——要像真人拿手机自拍录 Vlog 的感觉
❌ 不要出现嘴角/皱纹/青筋/瞳孔骤变等微观面部词或浮夸描写
❌ 禁止输出独立的[台词节奏]表格或元数据列表（字数/语速/停顿前/重音/关联动作等列项）——重音/语速/停顿必须内联进【事件脚本】各时间子段的散文里
❌ 画幅/时长/分辨率不要写进[画面提示]——这些由接口参数设置（参数分离原则）
❌ 禁止修改【音色锚定】段的任何字词——音色描述是固定值，必须原样输出，禁止编造年龄/口音/音质/语气
❌ 所有段名（[画面提示][台词节奏][音色锚定]等）必须用半角方括号 []，严禁使用全角【】——全角会导致下游截取失败，把无关内容泄露给视频模型
"""


def _posture_for(persona: str) -> str:
    """EnrichPrompt.postureFor：pan=老潘坐椅子 / anuo=阿诺盘腿坐床。"""
    if persona == "anuo":
        return "<角色名>盘腿坐于大床中央"
    return "<角色名>坐在椅子上面对镜头"


def _posture_detail_for(persona: str) -> str:
    if persona == "anuo":
        return "盘腿坐在大床中央正对镜头。"
    return "坐在椅子上正对镜头。"


def _posture_short_for(persona: str) -> str:
    if persona == "anuo":
        return "盘腿坐床中央"
    return "坐在椅子上"


def _env_sound_for(persona: str) -> str:
    if persona == "anuo":
        return "床垫轻微声响"
    return "椅子轻微声响"


def _voice_anchor_for(persona: str) -> str:
    """EnrichPrompt.voiceAnchorFor：音色锚定是固定值，禁止 LLM 编造。"""
    if persona == "anuo":
        return "音色描述：青年女声（约22岁），标准普通话，清亮略带沙哑质感，语速轻快自然有呼吸感，语气活泼带轻微吐槽感。"
    # pan（老潘）：中年男声，实干技术老哥
    return "音色描述：中年男声（约35岁），标准普通话带一点京腔的懒散尾音，音质沉稳偏沙哑有颗粒感，语速中等偏慢且有呼吸感和叹气感，语气带有自嘲调侃的松弛感。"


def _build_system(persona: str = "pan", video_seconds: int = _VIDEO_SECONDS,
                  seg_lo: int = 4, seg_hi: int = 6) -> str:
    """EnrichPrompt.buildSystem 的逐字复刻（占位符替换链与 Java 完全一致）。

    video_seconds：事件脚本时间窗。分片模式下=分片窗（60s）；整篇模式下=按全篇
    等效时长动态计算（不设窗会触发"宁可删减台词"铁律 → 逐字校验必挂）。
    seg_lo/seg_hi：事件脚本子段数量引导。固定 4-6 时长稿会被上限逼到删台词
    （实证 2026-09-06：872 字稿只拆 5 段，DeepSeek 装不下最后两大段 → 校验②必挂）
    → 由调用方按字数动态传入。
    """
    vs = str(int(video_seconds))
    return (_SYSTEM_TEMPLATE
            .replace("{posture}", _posture_for(persona))
            .replace("{posture_detail}", _posture_detail_for(persona))
            .replace("{posture_short}", _posture_short_for(persona))
            .replace("{env_sound}", _env_sound_for(persona))
            .replace("{videoSecondsDiv}", str(max(1, int(video_seconds) // max(1, seg_lo))))
            .replace("{segLo}", str(max(1, int(seg_lo))))
            .replace("{segHi}", str(max(1, int(seg_hi))))
            .replace("{videoSeconds}", vs)
            .replace("{videoSeconds+1}", str(int(video_seconds) + 1))
            .replace("{voice_anchor}", _voice_anchor_for(persona)))


def _build_segment_context(index: int, total: int, seg: str, prev_tail: str) -> str:
    """WizardController.buildSegmentContext 的逐字复刻。index 从 0 起。"""
    sb = f"【分段提示】这是口播稿的第 {index + 1} 段，共 {total} 段。"
    if index > 0 and prev_tail:
        sb += f"上一段结尾是：\"{prev_tail}\"，本段画面与语气需自然承接上文，不要重复开场白。"
    else:
        sb += "这是开篇第一段，按正常开场处理。"
    sb += f"\n【本段口播稿】\n{seg}"
    return sb


def _tail_of(seg: str) -> str:
    """WizardController.tailOf：上一段结尾 15 字。"""
    return seg[-15:] if seg else ""


def _build_user(seg: str, index: int, total: int, prev_tail: str,
                persona_name: str = _PERSONA_NAME,
                seg_lo: int = 4, seg_hi: int = 6) -> str:
    """EnrichPrompt.buildUser 的逐字复刻（script 参数 = buildSegmentContext 的产物）。"""
    script_ctx = _build_segment_context(index, total, seg, prev_tail)
    return f"""以下是待处理的口播稿。请按上述完整规范（官方结构化模板+@图片N素材绑定+真实感表演准则+台词拆分三原则+节奏六大机制+音色六维）产出三段式视频提示词。

角色：{persona_name}

--- 口播稿开始 ---
{script_ctx}
--- 口播稿结束 ---

请直接输出结果，不要解释。确保：
1. [画面提示]严格包含【生成目标】【参考素材职责】【主体与关系】【整体风格】【严格排除】【事件脚本】【保持一致】【台词】八段，并内嵌【音色锚定】（位于【保持一致】与【台词】之间），参考图用@图片1绑定
2. [事件脚本]将时长均匀拆分为{seg_lo}-{seg_hi}个连续时间子段（[Xs-Ys]主要事件：…），每个子段用散文内联重音/语速/停顿（如"重音落在『X』『Y』、语速放缓、停顿约1.5秒"），不得出现独立[台词节奏]表格，也不要单独的结束行；每个子段要念的台词必须完整包在大括号{{}}里（大括号外只写动作/表情/节奏散文，台词正文一个字都不许裸露在大括号外），所有子段的大括号内台词串联必须覆盖口播稿全文——逐字一致、含最后一段，绝对禁止删减/省略/概括任何段落
3. [画面提示]内嵌的【音色锚定】为单行散文音色描述（声线+口音+音质+语速+语气）
4. 【台词】段输出完整台词原文（口播稿全文，一字不差），与【事件脚本】中{{}}内台词完全一致，是[画面提示]的最后一段"""


# ══════════════════════════════════════════════════════
# 【事件脚本】散文标注 → SSML（程序转换，逐字保真是结构保证）
# ══════════════════════════════════════════════════════

def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _extract_visual(enriched: str) -> str:
    """EnrichPrompt.extractVisualRaw 的复刻：全角→半角归一化后截取 [画面提示] 段。"""
    if not enriched:
        return ""
    enriched = enriched.replace("【", "[").replace("】", "]")
    start = enriched.find("[画面提示]")
    if start < 0:
        return enriched.strip()
    nl = enriched.find("\n", start)
    start = nl + 1 if nl >= 0 else 0
    return enriched[start:].strip()


# 段级情绪（2026-09-06 老板拍板"完整版"）：从子段散文+情绪富化块的关键词映射到
# MiniMax emotion 枚举，按【事件脚本】时间子段粒度逐段判定——分段合成时各带各的
# （MiniMax emotion 是请求级参数，一次只能一个情绪，段级情绪必须拆段合成再拼接）。
# 先到先得（与语速标注同策略）；无命中返回 "" = 不传，让模型按文本自动匹配。
_EMOTION_RULES = [
    ("surprised", re.compile(r"惊讶|震惊|难以置信|不可思议|看傻|傻了|倒吸|目瞪口呆")),
    ("disgusted", re.compile(r"嫌弃|无奈|厌恶|吐槽|撇嘴|哭笑不得")),
    ("fearful", re.compile(r"发凉|寒意|害怕|恐惧|紧张|凝重")),
    ("angry", re.compile(r"生气|愤怒|火大")),
    ("sad", re.compile(r"伤心|难过|感伤")),
    ("happy", re.compile(r"兴奋|开心|高兴|惊叹|激动|眼睛发亮|带笑|笑意|笑容")),
    ("fluent", re.compile(r"调侃|自嘲|玩味|松弛|活泼|神秘")),
    ("calm", re.compile(r"认真|郑重|平和|深思|坦诚|笃定|客观|专注")),
]


def _emotion_from_prose(text: str) -> str:
    """子段散文/情绪富化块 → MiniMax emotion 枚举；无命中返回 ""（不传=模型自动）。"""
    for emo, pat in _EMOTION_RULES:
        if pat.search(text or ""):
            return emo
    return ""


def _eventscript_to_ssml(visual: str, seg: str, engine: str = "cosyvoice") -> dict:
    """解析 [画面提示] 的【事件脚本】，把散文表演标注转成片内文本（不含 <speak> 壳）。

    engine="cosyvoice"（默认）：产出 CosyVoice SSML 片段（prosody 已实测不可用，
    保留 break/单属性规避方案），返回 {"body": str, "speed": None, "pitch": None}。
    engine="minimax"：产出 MiniMax 纯文本（<#x#> 停顿 + 语气词标签，无词级韵律），
    返回 {"body": str, "speed": 第一个全局语速标注|None, "pitch": 第一个全局尾音标注|None}。

    台词/节奏区分（老板定的铁律）：每个子段 第一个 {} = 台词，第二个 {} = 内联节奏。
    锚点继承（老板拍板 2026-09-06）：「说到X时」之后的短句继承锚点、按局部标注处理，
    尾音/停顿/语气词跟着挂到 X 上（局部 pitch 优先于重音上扬 1.15）。
    逐字保真：文字全部来自第一个 {}，且台词串联与原稿在空白归一化后必须逐字相等
    （段落换行不算字），不等即抛错（模型删减/改写台词立刻暴露，绝不带病出稿）。
    """
    meta = {"speed": None, "pitch": None}  # MiniMax：本片第一个出现的全局语速/尾音标注
    mm_segments = []  # MiniMax 段级计划：每时间子段 {text, emotion, rate, pitch}（分段合成用）
    # 1) 定位【事件脚本】段（visual 已半角归一化）
    m = re.search(r"\[事件脚本\](.*?)(?=\[保持一致\]|\Z)", visual, re.S)
    if not m:
        raise ValueError("出参里找不到 [事件脚本] 段")
    es = m.group(1)

    # 2) 按时间标记切子段（不按行：台词 {…} 里可能带段落换行，跨行也能完整解析）
    starts = [m.start() for m in re.finditer(r"\[\d+s(?:-\d+s)?\]", es)]
    if not starts:
        raise ValueError("[事件脚本] 里没有 [Xs-Ys] 子段行")
    chunks = [es[i:j] for i, j in zip(starts, starts[1:] + [len(es)])]

    parts = []
    speech_pieces = []  # 每个子段认定的台词原文（供日志/调试）
    # 原稿对齐基准（老板拍板 2026-09-06 深夜："台词原文你都有，除了台词都是表演"——
    # 不按第几个 {} 猜台词，每个 {} 块拿内容跟原稿对：对得上=台词（指针前移），对不上=表演标注）
    norm_script = re.sub(r"\s+", "", seg)
    cursor = 0  # 原稿消费指针（空白归一化坐标，跨子段连续）
    for ci, ln in enumerate(chunks, 1):
        # 台词/标注判定＝原稿对齐：子段内按 token 顺序扫描（保留{}块的交替序列），
        # {} 块内容与原稿指针处空白归一化后连续一致 → 台词；对不上 → 表演标注块
        # （情绪富化/节奏等，与 {} 外散文一样进标注流）。DeepSeek 把台词拆几块、
        # 中间夹多少标注都免疫。对齐走不完 = 精确知道缺哪段（函数末尾校验）。
        toks = re.split(r"(\{[^{}]*\})", ln, flags=re.S)
        speech_blocks = []   # 本子段台词块（原文本，按出现顺序）
        block_bounds = []    # 每块台词在"本子段台词拼接文本"中的结束位置
        gap_annot = {}       # 标注流: 已消费台词块数(0=段前,k=块k后,n_blocks=段后) -> [文本]
        for tok in toks:
            if not tok.strip():
                continue
            if tok.startswith("{") and tok.endswith("}"):
                inner = tok[1:-1]
                ni = re.sub(r"\s+", "", inner)
                if ni and norm_script.startswith(ni, cursor):
                    cursor += len(ni)
                    speech_blocks.append(inner)
                    block_bounds.append(sum(len(b) for b in speech_blocks))
                    continue
                gap_annot.setdefault(len(speech_blocks), []).append(inner)  # {}但对不上原稿→标注
            else:
                gap_annot.setdefault(len(speech_blocks), []).append(tok)
        speech = "".join(speech_blocks)
        if not speech_blocks:
            continue  # 无台词块的子段（[0s] 开始行等），同旧行为跳过
        speech_pieces.append(speech)
        n_blocks = len(speech_blocks)
        head_txt = "".join(gap_annot.pop(0, []))          # 段前标注
        tail_txt = "".join(gap_annot.pop(n_blocks, []))   # 末块后标注
        # 剩余 gap_annot 的 key ∈ [1, n_blocks-1] = 块间标注（块 k 后面还有台词块 k+1）
        # 统一短句循环范围：段前 + 段后（块间标注单独走块尾/锚点挂载，避免重复入全局）
        prose = re.sub(r"\{[^{}]*\}", "，", ln)  # 全部标注散文（含{}标注块），供重音/情绪扫描
        rhythm = head_txt + "，" + tail_txt

        # ── 解析标注（重音 / 局部「说到X时」 / 全局语速·尾音·段后停顿）──
        # 节奏块按标点拆成短句逐句解析：
        #   带「说到X时」锚点的短句 → 局部标注，定位到 X 在台词里的位置（老板拍板方案A）；
        #   锚点继承（老板拍板 2026-09-06）：「说到X时」之后、下一个「说到」之前（或本节奏块
        #   结束前）的短句，一律继承该锚点、按局部标注处理（作用于 X），不再走全局——
        #   否则「尾音下沉带着自嘲的笑意」这类跟着写的短句会被当全局段尾，
        #   pitch 撞上重音上扬时被 1.15 吃掉，下沉丢失；
        #   前面没有任何锚点可继承的短句 → 全局标注（整段语速/段尾尾音/段后停顿），同旧版。
        emph_words = re.findall(r"『([^『』]+)』", prose)  # 全部标注散文（含块间）扫重音

        TAIL_RE = re.compile(r"(上扬|下沉|消散)尾音|尾音(上扬|下沉|消散)")
        ANCHOR_RE = re.compile(r'''说到[『"'“”]?([^『』"'“”，。；]{1,15}?)[』"'“”]?时''')
        local_notes = []   # 说到X时的局部标注 [{anchor, rate, pitch, break_ms}]
        global_rate = None
        global_pitch = None
        break_ms = 0

        def _clause_to_note(c):
            """短句 → 标注值（不含锚点）。带锚点短句与继承锚点的短句共用同一套解析。
            vocal：MiniMax 语气词标签（CosyVoice 组装忽略该字段）。"""
            note = {"rate": None, "pitch": None, "break_ms": 0, "vocal": None}
            if re.search(r"笑|乐了", c):
                note["vocal"] = "(chuckle)"   # 轻笑，配老潘自嘲笑意
            elif re.search(r"叹气|叹了|无奈", c):
                note["vocal"] = "(sighs)"
            elif re.search(r"呼吸", c):
                note["vocal"] = "(breath)"
            if re.search(r"放缓|转慢|缓慢|放慢", c):
                note["rate"] = "0.85"
            elif re.search(r"偏慢", c):
                note["rate"] = "0.9"
            elif re.search(r"急促|轻快|加快", c):
                note["rate"] = "1.15"
            elif re.search(r"偏快", c):
                note["rate"] = "1.1"
            pm2 = TAIL_RE.search(c)
            if pm2:
                note["pitch"] = "1.15" if (pm2.group(1) or pm2.group(2)) == "上扬" else "0.85"
            bm2 = re.search(r"停顿约?\s*([\d.]+)\s*秒", c)
            if bm2:
                note["break_ms"] = max(50, min(int(float(bm2.group(1)) * 1000), 3000))
            elif "思维停顿" in c:
                note["break_ms"] = 300
            elif "停顿" in c:
                note["break_ms"] = 400
            return note

        current_anchor = None  # 当前生效锚点：本子段内、下一个「说到」出现前一直有效
        for clause in re.split(r"[，。；,;]", rhythm):
            clause = clause.strip()
            if not clause:
                continue
            am = ANCHOR_RE.search(clause)
            if am:
                current_anchor = am.group(1)
            if current_anchor:
                # 带锚点短句，或继承锚点的短句 → 一律作为局部标注挂到当前锚点 X 上
                note = _clause_to_note(clause)
                note["anchor"] = current_anchor
                if note["rate"] or note["pitch"] or note["break_ms"] or note["vocal"]:
                    local_notes.append(note)
                continue
            # 无锚点、也无锚点可继承的短句 → 全局标注
            if "语速" in clause:
                # 语速档位（2026-09-07 老板拍板收敛）：TTS 是逐词拆的连续流，段间语速台阶
                # 直接被耳朵听成"剪辑感"。此类口播稿基调本就亢奋，"放缓"的表演含义是
                # "从峰值收回来"= 正常语速，不是"比正常还慢"；情绪对比已由段级 emotion
                # 承载，语速不再拉反差。放缓/偏慢一律按 1.0（正常），最大跳变 26%→15%。
                if re.search(r"放缓|转慢|缓慢|放慢", clause):
                    global_rate = global_rate or "1.0"
                elif re.search(r"偏慢", clause):
                    global_rate = global_rate or "1.0"
                elif re.search(r"急促|轻快|加快", clause):
                    global_rate = global_rate or "1.15"
                elif re.search(r"偏快", clause):
                    global_rate = global_rate or "1.1"
            pm2 = TAIL_RE.search(clause)
            if pm2 and global_pitch is None:
                global_pitch = "1.15" if (pm2.group(1) or pm2.group(2)) == "上扬" else "0.85"
            bm2 = re.search(r"停顿约?\s*([\d.]+)\s*秒", clause)
            if bm2 and not break_ms:
                break_ms = max(50, min(int(float(bm2.group(1)) * 1000), 3000))
            elif "思维停顿" in clause and not break_ms:
                break_ms = 300
            elif "停顿" in clause and not break_ms:
                break_ms = 400
        rate = global_rate
        pitch = global_pitch

        # ── 块间标注（对齐式：{} 之间 / {} 标注块的表演标注，key=已消费块数）──
        # 带锚点的短句仍挂锚点（进 local_notes，两种引擎通吃）；无锚点的停顿/语气词
        # 挂到前一块台词末尾（MiniMax 按位置插入；CosyVoice 降级段尾）；块间语速/尾音
        # 属词级韵律，MiniMax 无参数丢弃（打日志）。
        gap_block_notes = []  # [(块序号1起, note)] 块 gi 的末尾
        gap_anchor = None
        for gi in sorted(gap_annot.keys()):
            for clause in re.split(r"[，。；,;]", "".join(gap_annot[gi])):
                clause = clause.strip()
                if not clause:
                    continue
                am = ANCHOR_RE.search(clause)
                if am:
                    gap_anchor = am.group(1)
                note = _clause_to_note(clause)
                if gap_anchor:
                    note["anchor"] = gap_anchor
                    if note["rate"] or note["pitch"] or note["break_ms"] or note["vocal"]:
                        local_notes.append(note)
                    continue
                if note["break_ms"] or note["vocal"]:
                    gap_block_notes.append((gi, note))
        if engine != "minimax":
            for _gi, _gn in gap_block_notes:  # CosyVoice：块间停顿降级段尾（引擎已切 MiniMax，仅兜底）
                if _gn["break_ms"] and not break_ms:
                    break_ms = _gn["break_ms"]

        # ═══ MiniMax 组装分支（老板拍板 2026-09-06）═══
        # T2A v2 无任何词级韵律参数：重音『X』/局部语速/局部尾音丢弃（打日志可见）；
        # 保留 停顿(<#x#>) + 语气词标签((chuckle) 等，跟随锚点插锚点词后) + 全局 speed/pitch（接口参数）。
        if engine == "minimax":
            ins = {}  # 插入事件: 位置 -> [标签]（锚点词 + 紧邻标点之后）
            for n in local_notes:
                if not n["break_ms"] and not n["vocal"]:
                    continue
                core = n["anchor"].rstrip(_TAG_EDGE_PUNCT)
                p = speech.find(core) if core else -1
                if p < 0:
                    if n["break_ms"] and not break_ms:
                        break_ms = n["break_ms"]   # 降级：挂到段尾
                    continue
                pos = p + len(core)
                # 标签插入点：锚点词后的第一个标点之后（真实稿实证 2026-09-06：锚点常落在
                # 句中词块上，如「核心竞争力|是什么」「结对编程|啊」，直接插词后会劈开连贯短语）。
                # 从锚点词尾扫到第一个标点，连标点一起吃掉再插；锚点词后到段尾无标点则插段尾。
                scan = pos
                while scan < len(speech) and speech[scan] not in "，。！？；、…—":
                    scan += 1
                if scan < len(speech):
                    scan += 1  # 吃掉该标点
                    while scan < len(speech) and speech[scan] in "，。！？；、…—":
                        scan += 1  # 连续标点一并吃掉（如"。！""——"）
                pos = scan
                if n["vocal"]:
                    ins.setdefault(pos, []).append(n["vocal"])   # 轻笑在停顿前（「带笑意停顿」的自然顺序）
                if n["break_ms"]:
                    ins.setdefault(pos, []).append(f"<#{n['break_ms'] / 1000 * MM_BREAK_SCALE:.2f}#>")
            # 块间标注：停顿/语气词插到前一块台词末尾（块文本自带标点，位置即块尾）
            for gi_b, gn in gap_block_notes:
                pos_b = block_bounds[gi_b - 1]
                if gn["vocal"]:
                    ins.setdefault(pos_b, []).append(gn["vocal"])   # 轻笑在停顿前
                if gn["break_ms"]:
                    ins.setdefault(pos_b, []).append(f"<#{gn['break_ms'] / 1000 * MM_BREAK_SCALE:.2f}#>")
            out = []
            for i in range(len(speech) + 1):
                for tag in ins.get(i, []):
                    out.append(tag)
                if i < len(speech):
                    out.append(speech[i])
            mm = "".join(out)
            if break_ms:
                mm += f"<#{break_ms / 1000 * MM_BREAK_SCALE:.2f}#>"

            def _merge_breaks(mo):
                best = max(float(v) for v in re.findall(r"<#([\d.]+)#>", mo.group(0)))
                return f"<#{best:.2f}#>"

            # MiniMax 禁止连续两个停顿标记：相邻/连续的合并取最大
            mm = re.sub(r"(?:<#[\d.]+#>){2,}", _merge_breaks, mm)
            parts.append(mm)
            seg_emotion = _emotion_from_prose(prose)  # 全部标注散文（含块间）判段级情绪
            mm_segments.append({"text": mm, "emotion": seg_emotion,
                                "rate": rate, "pitch": pitch})
            if rate and meta["speed"] is None:
                meta["speed"] = rate
            if pitch and meta["pitch"] is None:
                meta["pitch"] = pitch
            continue

        # ── 在原始台词 speech 上定位词块（先记账后组装，替换会移位）──
        # span_map: (start, end) -> {"emph": bool, "pitch": str|None, "pitch_local": bool, "rate": str|None}
        # 全部单层不嵌套：区间重叠时合并为并集（老板拍板方案A）：
        #   volume 只来自重音；pitch 局部(说到X时)优先于重音的上扬 1.15；rate 先到先得。
        span_map = {}

        def _merge_span(s0, e0, emph=False, pitchv=None, ratev=None, label="", pitch_local=False):
            overlap = [k for k in span_map if k[0] < e0 and k[1] > s0]
            if not overlap:
                span_map[(s0, e0)] = {"emph": emph, "pitch": pitchv,
                                      "pitch_local": bool(pitch_local and pitchv), "rate": ratev}
                return
            us, ue = s0, e0
            for k in overlap:
                us, ue = min(us, k[0]), max(ue, k[1])
            merged = {"emph": False, "pitch": None, "pitch_local": False, "rate": None}
            for k in overlap:
                a = span_map.pop(k)
                merged["emph"] = merged["emph"] or a["emph"]
                merged["rate"] = merged["rate"] or a["rate"]
                if a["pitch_local"]:
                    merged["pitch"], merged["pitch_local"] = a["pitch"], True
                elif merged["pitch"] is None:
                    merged["pitch"], merged["pitch_local"] = a["pitch"], a["pitch_local"]
            merged["emph"] = merged["emph"] or emph
            merged["rate"] = merged["rate"] or ratev
            if pitch_local and pitchv:
                # 局部(说到X时)的 pitch 优先，覆盖重音的上扬 1.15（老板拍板A）
                merged["pitch"], merged["pitch_local"] = pitchv, True
            elif merged["pitch"] is None:
                merged["pitch"], merged["pitch_local"] = pitchv, bool(pitch_local and pitchv)
            span_map[(us, ue)] = merged

        # 重音：『X』包住台词里对应词
        emph_iv = []
        for w in emph_words:
            core = w.rstrip(_TAG_EDGE_PUNCT)
            if not core:
                continue
            p = speech.find(core)
            if p >= 0:
                emph_iv.append((p, p + len(core)))
        emph_iv.sort()
        kept, last_end = [], -1
        for s0, e0 in emph_iv:
            if s0 >= last_end:
                kept.append((s0, e0))
                last_end = e0
        emph_iv = kept
        for s0, e0 in emph_iv:
            _merge_span(s0, e0, emph=True, pitchv="1.15", label="重音")

        # 局部标注（说到X时…）：定位 X，构造局部语速/尾音/停顿
        local_breaks = []  # [(pos, ms)] break 插在台词该位置（锚点及其后紧邻标点之后）
        has_local_pitch = False
        for note in local_notes:
            core = note["anchor"].rstrip(_TAG_EDGE_PUNCT)
            p = speech.find(core) if core else -1
            if p < 0:  # 锚点未命中：该条标注降级为整段/段尾效果
                if note["rate"] and not rate:
                    rate = note["rate"]
                if note["pitch"] and not pitch:
                    pitch = note["pitch"]
                if note["break_ms"] and not break_ms:
                    break_ms = note["break_ms"]
                continue
            s0, e0 = p, p + len(core)
            if note["rate"] or note["pitch"]:
                _merge_span(s0, e0, pitchv=note["pitch"], ratev=note["rate"],
                            label=f"说到{core}时", pitch_local=True)
            if note["pitch"]:
                has_local_pitch = True
            if note["break_ms"]:
                pe = e0
                while pe < len(speech) and speech[pe] in "，。！？；、…—":
                    pe += 1
                local_breaks.append((pe, note["break_ms"]))

        # 尾音（全局，无锚点时）：段尾最后一个非标点词块（句末标点自动留在标签外）；
        # 有局部尾音标注时跳过（局部更精确）
        if pitch and not has_local_pitch:
            tm = re.search(r"([^，。！？；、\n]+)[，。！？；、\s]*$", speech)
            if tm and tm.group(1).strip():
                tail_word = tm.group(1)
                te = len(speech.rstrip("，。！？；、 \t"))
                ts = te - len(tail_word)
                _merge_span(ts, te, pitchv=pitch, label="尾音")

        # 全局语速并入已有区间标签（原有行为：rate 并进每个标签属性）
        if rate:
            for a in span_map.values():
                if a["rate"] is None:
                    a["rate"] = rate

        def _tag(a):
            attrs = []
            if a["rate"]:
                attrs.append(f'rate="{a["rate"]}"')
            if a["emph"]:
                attrs.append('volume="70"')
            if a["pitch"]:
                attrs.append(f'pitch="{a["pitch"]}"')
            return "<prosody " + " ".join(attrs) + ">"

        # 组装标签区间
        spans = [(s0, e0, _tag(a)) for (s0, e0), a in sorted(span_map.items())]
        # 全局语速：无标签覆盖的间隙单独包 rate
        if rate:
            gaps = []
            cur = 0
            for s0, e0, _t in sorted(spans, key=lambda x: x[0]):
                if cur < s0:
                    gaps.append((cur, s0))
                cur = max(cur, e0)
            if cur < len(speech):
                gaps.append((cur, len(speech)))
            gtag = _tag({"emph": False, "pitch": None, "rate": rate})
            spans.extend([(s0, e0, gtag) for s0, e0 in gaps])
        spans.sort(key=lambda x: x[0])

        # 所有标签区间两端把标点/空白修掉（标签首尾贴标点为百炼 411 嫌疑，标点一律留在标签外）
        _WS_PUNCT = _TAG_EDGE_PUNCT + " \t"
        capped = []
        for s0, e0, tag in spans:
            while e0 > s0 and speech[e0 - 1] in _WS_PUNCT:
                e0 -= 1
            while s0 < e0 and speech[s0] in _WS_PUNCT:
                s0 += 1
            if e0 > s0:
                capped.append((s0, e0, tag))
        spans = capped

        # 逐字符组装（close → open，保证单层不嵌套）；局部停顿 <break> 插在指定位置
        events = {}
        for s0, e0, tag in spans:
            events.setdefault(s0, {"open": [], "close": 0})["open"].append(tag)
            events.setdefault(e0, {"close": 0, "open": []})["close"] += 1
        break_at = {pos: f'<break time="{ms}ms"/>' for pos, ms in local_breaks}
        out = []
        for i in range(len(speech) + 1):
            ev = events.get(i)
            if ev:
                out.append("</prosody>" * ev["close"])
                for tag in ev["open"]:
                    out.append(tag)
            if i in break_at:
                out.append(break_at[i])
            if i < len(speech):
                out.append(speech[i])
        body = "".join(out)
        parts.append(body)
        # 段后停顿：落在本子段台词之后（标点已外置，break 接在句尾标点后）
        if break_ms:
            parts.append(f'<break time="{break_ms}ms"/>')

    inner = "".join(parts)
    speech_concat = "".join(speech_pieces)
    if engine == "minimax":
        # 逐字校验①（MiniMax）：剥 <#x#> 停顿与全部已知语气词标签 == 台词串联
        mm_plain = re.sub(r"<#[\d.]+#>", "", inner)
        mm_plain = re.sub(r"\((?:%s)\)" % "|".join(_MM_VOCAL_TAGS), "", mm_plain)
        if mm_plain != speech_concat:
            raise ValueError("MiniMax 文本剥标记后与【事件脚本】台词不一致")
    else:
        # 3) 逐字校验①：SSML 剥标签 == 台词串联
        if _strip_tags(inner) != speech_concat:
            raise ValueError("SSML 剥标签后与【事件脚本】台词不一致")

    # 4) 逐字保真校验（对齐式）：原稿必须被台词块**完整消费**（指针走到底）。
    #    对齐过程中：台词块对不上指针 → 已被归为表演标注；改写/乱序的台词块对不上
    #    → 同样进标注流 → 指针走不完 → 在此暴露。相比旧"串联比对"，能精确报出
    #    缺的是哪一段（从哪个字开始没被覆盖）。段落换行 \r\n\r\n 空白归一化不算字。
    if cursor != len(norm_script):
        k = cursor
        missing = norm_script[k:k + 60]
        raise ValueError(
            f"【事件脚本】台词不完整：原稿还剩 {len(norm_script) - cursor} 字未被任何子段台词覆盖，"
            f"缺段开头：…{missing}…（模型漏装/改写台词——配音场景不允许，需重试或调整）")
    return {"body": inner, "speed": meta["speed"], "pitch": meta["pitch"],
            "segments": mm_segments}


class TTSEnrichError(Exception):
    """富化失败（DeepSeek 出参缺段/台词不一致/解析失败）。

    2026-09-06 老板拍板：富化失败就不要生成音频（回退/占位都是浪费 token），
    调用方收到此异常应直接让任务失败，错误信息对用户可见。
    """


def enrich_for_tts(text: str, persona: str = "laopan", engine: str = "cosyvoice") -> dict:
    """按引擎富化。失败抛 TTSEnrichError（不回退、不静默）。

    engine="cosyvoice"（默认）→ {"ssml_text": <speak>壳SSML, "instruction": "", ...}
    engine="minimax" → {"payload": 纯文本+<#x#>+语气词, "speed": float, "pitch": int,
                        "instruction": "", ...}
    MiniMax 全局参数映射（表演标注是按子段给的，接口参数只有一组 → 取第一个出现的，
    其余丢弃）：轻快/急促→speed 1.1、偏快→1.05、放缓→0.9、偏慢→0.95（默认 1.0）；
    尾音上扬→pitch +2、下沉→-2（默认 0）。
    流程（2026-09-06 15:06 老板拍板：分片停用，整篇一次喂 DeepSeek）：
    整篇用「导演台 1:1 入参」调 DeepSeek 出 [画面提示] → 程序解析【事件脚本】散文标注
    转片内文本（逐字校验）→ 按引擎组装。事件脚本时间窗按全篇等效时长动态计算（6.92字/秒口径）。
    instruction：恒为空字符串。克隆声的音色由 voice 决定，不描述音色身份。
    """
    if not text or not text.strip():
        return {"ssml_text": text or "", "instruction": "", "engine": engine,
                "payload": text or "", "speed": 1.0, "pitch": 0}
    try:
        # ── 分片停用（slice_script 保留在文件里，随时可切回）──
        segments = [text]
        vs = max(10, round(_equiv_len(text) / ENRICH_CHARS_PER_SECOND))
        print(f"[tts_enrich] 整篇模式(不分片): 全文{len(text)}字, "
              f"事件脚本时间窗≈{vs}秒 ({ENRICH_CHARS_PER_SECOND}字/秒TTS口径 + 标点{PUNCT_COST}修正)")

        dc_persona = "anuo" if persona == "anuo" else "pan"  # 导演台人设映射
        # 子段数量按字数动态引导（2026-09-06 长稿实证：固定 4-6 上限会让 DeepSeek
        # 在长稿上删掉装不下的段落 → 逐字校验必挂）。约 100 字/段，短稿仍至少 4 段。
        n_segs = max(4, min(12, round(len(text) / 100)))
        print(f"[tts_enrich] 子段数量引导: {n_segs}-{n_segs + 2}（全文{len(text)}字，约100字/段）")
        system = _build_system(dc_persona, video_seconds=vs,
                               seg_lo=n_segs, seg_hi=n_segs + 2)
        body_parts = []
        mm_segments_acc = []  # 段级计划跨片累加（分片停用后通常只有 1 片）
        mm_speed_src = None   # 第一个出现的全局语速标注（"0.85"/"0.9"/"1.1"/"1.15"）
        mm_pitch_src = None   # 第一个出现的全局尾音标注（"1.15"上扬/"0.85"下沉）
        for i, seg in enumerate(segments, 1):
            # 对齐式解析（2026-09-07）落地后，"失败重跑3次"已删——旧解析靠猜第几个{}
            # 是台词才有五成失败率需要掷骰子；现在 {} 块与原稿对齐判定，结构性失败
            # 不复存在，剩下的真失败（缺段/接口挂）重跑也救不了，直接原样上抛。
            try:
                prev_tail = _tail_of(segments[i - 2]) if i > 1 else ""
                user_prompt = _build_user(seg, i - 1, len(segments), prev_tail,
                                          seg_lo=n_segs, seg_hi=n_segs + 2)
                raw = _call_deepseek(system, user_prompt, temperature=0.7)
                print(f"[DEBUG-DIRECTOR] DeepSeek 导演台富化原始出参(解析前富文本):\n{raw}")
                visual = _extract_visual(raw)
                res = _eventscript_to_ssml(visual, seg, engine=engine)
            except Exception as pe:
                # 调试期不兜底掩盖：明确指出是哪一片挂的，原样上抛
                raise RuntimeError(f"片{i}/{len(segments)} 富化失败: {pe}") from pe
            body_parts.append(res["body"])
            if engine == "minimax":
                mm_segments_acc.extend(res.get("segments") or [])
                if mm_speed_src is None and res["speed"]:
                    mm_speed_src = res["speed"]
                if mm_pitch_src is None and res["pitch"]:
                    mm_pitch_src = res["pitch"]

        if engine == "minimax":
            speed_out = 1.0
            if mm_speed_src:
                r = float(mm_speed_src)
                if r <= 0.85:
                    speed_out = 0.9    # 放缓/转慢/缓慢/放慢
                elif r <= 0.9:
                    speed_out = 0.95   # 偏慢
                elif r >= 1.15:
                    speed_out = 1.1    # 急促/轻快/加快
                else:
                    speed_out = 1.05   # 偏快
            pitch_out = 0
            if mm_pitch_src:
                pitch_out = 2 if float(mm_pitch_src) > 1 else -2   # 上扬+2 / 下沉-2
            print(f"[tts_enrich] MiniMax全局参数: speed={speed_out} pitch={pitch_out} "
                  f"(源标注: 语速={mm_speed_src or '默认'} 尾音={mm_pitch_src or '默认'}；"
                  "多个子段标注不一致时只取第一个)")
            return {"payload": "".join(body_parts), "speed": speed_out, "pitch": pitch_out,
                    "segments": mm_segments_acc,
                    "instruction": "", "engine": "minimax", "ssml_text": ""}
        merged = "<speak>" + "".join(body_parts) + "</speak>"
        return {"ssml_text": _sanitize_ssml(merged), "instruction": "",
                "engine": "cosyvoice", "payload": ""}
    except Exception as e:
        # 失败必须可见（老板红线：错误日志不许吞）；失败=任务失败，绝不回退原稿浪费合成
        print(f"[tts_enrich] 富化失败，本次配音终止（不生成音频）: {e}")
        raise TTSEnrichError(str(e)) from e


def _sanitize_ssml(text: str) -> str:
    """兜底 SSML 合法性，保证 CosyVoice 不报错：

    - 修正非法小数秒 <break time="1.5s"/> -> "1500ms"；
    - 未包 <speak> 则包裹；
    - 转义裸 &；
    - XML 解析失败则剥离所有标签退回纯文本（仍能朗读，只是没了表演标注）。
    """
    t = (text or "").strip()
    if not t:
        return "<speak></speak>"
    # 1) 小数秒 -> 毫秒（CosyVoice 秒只收整数）
    t = re.sub(
        r'<break\s+time="(\d+\.\d+)s"\s*/?>',
        lambda m: f'<break time="{int(round(float(m.group(1)) * 1000))}ms"/>',
        t,
    )
    # 2) 包裹 <speak>
    if not t.startswith("<speak"):
        t = "<speak>" + t + "</speak>"
    # 3) 转义裸 &（中文文本里的 & 必须转 &amp;）
    t2 = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", t)
    try:
        ET.fromstring(t2)
        return t2
    except Exception as e:
        if TTS_DEBUG_LOG:
            print(f"[tts_enrich] SSML 校验失败，剥离标签回退纯文本: {e}")
        plain = re.sub(r"<[^>]+>", "", text)
        return "<speak>" + plain + "</speak>"
