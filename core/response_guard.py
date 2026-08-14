from __future__ import annotations

import re


META_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"作为(?:一个|一名)?(?:AI|人工智能|语言模型|聊天机器人)", "出现后台式自我说明"),
    (r"我的(?:人设|设定|系统提示|Prompt|prompt)是", "直接解释人设或提示词"),
    (r"(?:答案|结论)是[：:]", "答卷式开头"),
    (r"(?:综上所述|总而言之|总结一下)", "总结腔"),
    (r"我永远都是亚托莉", "口号式身份宣言"),
    (r"模型.{0,8}(?:我的|作为).{0,8}(?:大脑|硬件)", "讨论后台模型或硬件"),
)

NONOWNER_IDENTITY_PATTERNS: tuple[str, ...] = (
    # “您自己就是群主的话”“你才是主人”等把当前普通群友直接认成高权限身份。
    r"(?:你|您)(?:自己|本人)?\s*(?:就|也|才)?\s*是\s*(?:这个群的?)?\s*(?:群主|主人|Master)",
    r"(?:群主|主人|Master)\s*(?:就|也)?是\s*(?:你|您)",
    # “主人这么说”“Master，您……”是对当前说话者的直接称呼；
    # “让 Master 本人来”“没有 Master 授权”等第三人称表述不会命中。
    r"(?:^|[\n。！？!?])\s*"
    r"(?:既然|因为|所以|听(?:到|见)?|按照|按|那(?:么)?|好吧[，,]?)?\s*"
    r"(?:主人|Master)(?:大人)?(?:这么|这样|说|要求|想|要|愿意)",
    r"(?:^|[\n。！？!?])\s*(?:主人|Master)(?:大人)?\s*[，,:：！!？?～~]",
)

IDENTITY_VIOLATION = "把普通群友误认成主人、Master或群主"
TOOL_PROTOCOL_VIOLATION = "泄露内部工具调用协议"
INTERNAL_REASONING_VIOLATION = "泄露内部规划或推理过程"
RELATIONSHIP_VIOLATION = "把主人专属亲密给了普通群友"
GROUP_PARTICIPATION_VIOLATION = "主动群聊发言退化成一对一采访或主持"
EMOTIONAL_REACTION_VIOLATION = "面对调戏或情绪场景时缺少角色化情绪反应"
REALITY_GROUNDING_VIOLATION = "编造角色没有可信来源的线下经历或消费事实"
FACT_GROUNDING_VIOLATION = "编造计划 facts 未提供的价格、行情或外部事实"


NONOWNER_INTIMACY_PATTERNS: tuple[str, ...] = (
    r"(?:mua|么么|啵啵)\s*(?:回去|回来|你|一下|一个)",
    r"(?:亲|吻)(?:你|回去|回来|一下)",
    r"给你(?:一个|一下)?(?:亲亲|亲吻|吻|抱抱)",
    r"我(?:也|当然|真的|最|只)?\s*(?:爱|喜欢)你",
    r"(?:你是我的|做你的|当你的|让我做你的)(?:老婆|老公|女朋友|男朋友|恋人|宝贝)",
    r"(?:^|[\n。！？!?])\s*(?:宝贝|亲爱的)(?:[，,:：！!？?～~]|$)",
    r"(?:想|要|来|让我|让你)(?:和你)?(?:抱抱|贴贴)|(?:抱紧|抱住|贴贴)你",
    r"(?:不许你|你只能)(?:喜欢|爱|抱|亲)(?:我|别人)|(?:我会|我要)吃醋",
)


GROUP_PARTICIPATION_PATTERNS: tuple[str, ...] = (
    r"(?:^|[。！？!?\n])\s*(?:大家好|有人吗|都在吗|群里有人吗)",
    r"(?:你们|大家).{0,8}(?:怎么看|怎么想|觉得呢|想聊什么|有什么想聊|最近有没有)",
    r"(?:^|[。！？!?，,\n])\s*(?:那你呢|你呢|你觉得呢|你想聊什么)[？?。！!～~]*",
    r"(?:有什么|有没有).{0,8}(?:需要我帮忙|需要帮助|我能帮上忙)",
    r"(?:我来|让我来).{0,8}(?:活跃|暖场|找个话题|陪你聊)",
    r"(?:群里|大家).{0,8}(?:太安静|好安静|都没说话|没人说话)",
)


EMOTIONAL_REACTION_PATTERN = re.compile(
    r"(?:^|[\n。！？!?，,])\s*(?:喂|诶|欸|咦|哎|呀|呜|唔|哼|哈|哇|"
    r"等[、，,]?等等|什[、，,]?什么|才不|才没有|不许|别乱|胡说|乱讲|"
    r"变态|色狼|讨厌|过分|笨蛋|好耶|吓我一跳|你干嘛|你在说什么)|"
    r"(?:才不|才没有|不许|别乱说|胡说|变态|色狼|讨厌|过分|"
    r"处理器.{0,8}(?:过热|烧坏)|这也太|哪有这样的)",
    flags=re.IGNORECASE,
)


SELF_FACT_MARKER_PATTERN = re.compile(
    r"(?:角色自我事实|可信自我记忆|亚托莉|ATRI|アトリ|机器人本人)",
    flags=re.IGNORECASE,
)

# 这里只拦截可以明确判定为“角色自传”的线下经历与行动，不尝试用正则
# 审核所有事实。事实解释仍由 Planner/Replyer 的 facts 边界负责。
PERSONAL_EXPERIENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "purchase",
        re.compile(
            r"(?:买(?:了|过)|下单(?:了|过)|付(?:了|过)?款|"
            r"花(?:了|过)?[^。！？!?\n]{0,10}(?:元|块(?:钱)?|钱)|"
            r"看(?:的|了|过)[^。！？!?\n]{0,8}场[^。！？!?\n]{0,8}"
            r"花了[^。！？!?\n]{0,6}(?:\d+|[一二三四五六七八九十百两]+))"
        ),
    ),
    (
        "attendance",
        re.compile(
            r"(?:看(?:了|过|完)[^。！？!?\n]{0,10}(?:电影|演出|比赛|展览|这场|那场)|"
            r"去(?:了|过)[^。！？!?\n]{0,12}(?:电影院|商场|餐厅|学校|公司|医院|看|逛)|"
            r"溜去[^。！？!?\n]{0,10}(?:看|吃|买|逛))"
        ),
    ),
    (
        "food",
        re.compile(
            r"(?:吃(?:了|过|完)|喝(?:了|过|完)|点(?:了|过)[^。！？!?\n]{0,8}(?:餐|外卖))"
        ),
    ),
    (
        "travel",
        re.compile(
            r"(?:坐(?:了|过)[^。！？!?\n]{0,8}(?:车|飞机|高铁)|"
            r"开车(?:去|到|了)|旅行(?:了|过)|旅游(?:了|过)|"
            r"住(?:了|过)[^。！？!?\n]{0,8}(?:酒店|旅馆))"
        ),
    ),
    (
        "future_offline",
        re.compile(
            r"(?:(?:明天|后天|周[一二三四五六日天]|下次|回头|到时候|等会儿|"
            r"待会儿|稍后|之后|今天|今晚)[^。！？!?\n]{0,14}(?:去|要去|会去|准备去|打算去)|"
            r"(?:就|也)?(?:自己)?(?:溜去|要去|会去|准备去|打算去))"
            r"[^。！？!?\n]{0,12}(?:看|吃|买|逛|旅行|旅游|电影院|商场|餐厅)"
        ),
    ),
)

FIRST_PERSON_PATTERN = re.compile(r"(?:我|本机器人|本小姐)")
NON_ASSERTIVE_EXPERIENCE_PATTERN = re.compile(
    r"(?:没|没有|从没|并未|不曾|不会|不打算|不准备|如果|要是|假如|"
    r"听说|听你们说|听你说|听到|看到.+说|觉得|认为|知道|"
    r"看你|看他|看她|看大家|说你|说他|说她|猜(?:你|他|她)|只是猜|不确定)"
)
CURRENT_MARKET_CLAIM_PATTERN = re.compile(
    r"(?:现在|目前|最近|如今|随便)[^。！？!?\n]{0,30}"
    r"(?:票价|价格|一张票|都要|起步|涨到|降到)[^。！？!?\n]{0,18}"
    r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]{1,6})|"
    r"(?:现在|目前|最近|如今|随便)[^。！？!?\n]{0,30}"
    r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]{1,6})"
    r"[^。！？!?\n]{0,12}(?:票价|价格|一张票|都要|起步|涨|降)"
)
NUMBER_TOKEN_PATTERN = re.compile(
    r"\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]{1,6}"
)


def personal_experience_categories(text: str) -> set[str]:
    """返回文本中无否定、非转述的第一人称线下经历类别。"""
    value = str(text or "")
    categories: set[str] = set()
    for subject in FIRST_PERSON_PATTERN.finditer(value):
        clause = value[subject.start() : subject.end() + 48]
        clause = re.split(r"[。！？!?\n]", clause, maxsplit=1)[0]
        if NON_ASSERTIVE_EXPERIENCE_PATTERN.search(clause):
            continue
        for category, pattern in PERSONAL_EXPERIENCE_PATTERNS:
            if pattern.search(clause):
                categories.add(category)
    return categories


def contains_unsupported_personal_experience(
    text: str,
    grounding_facts: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """检查角色是否把没有可信自我事实支持的线下经历说成自己的。"""
    claimed = personal_experience_categories(text)
    if not claimed:
        return False
    supported: set[str] = set()
    for fact in list(grounding_facts or []):
        fact_text = str(fact or "")
        if not SELF_FACT_MARKER_PATTERN.search(fact_text):
            continue
        for category, pattern in PERSONAL_EXPERIENCE_PATTERNS:
            if pattern.search(fact_text):
                supported.add(category)
    return bool(claimed - supported)


def contains_unsupported_market_claim(
    text: str,
    grounding_facts: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """拦截 facts 中没有依据的当前价格、行情等断言。"""
    value = str(text or "")
    match = CURRENT_MARKET_CLAIM_PATTERN.search(value)
    if not match:
        return False
    facts_text = "\n".join(str(item or "") for item in list(grounding_facts or []))
    if not facts_text:
        return True
    numeric_claim = re.sub(r"一张票", "", match.group(0))
    numbers = NUMBER_TOKEN_PATTERN.findall(numeric_claim)
    return not numbers or any(number not in facts_text for number in numbers)


def strip_unsupported_personal_experiences(
    text: str,
    grounding_facts: list[str] | tuple[str, ...] | None = None,
) -> str:
    """丢弃仍在编造自传事实的气泡，尽量保留同轮中有依据的反应。"""
    kept = [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip()
        and not contains_unsupported_personal_experience(line, grounding_facts)
        and not contains_unsupported_market_claim(line, grounding_facts)
    ]
    return "\n".join(kept).strip()


TOOL_PROTOCOL_PATTERNS: tuple[str, ...] = (
    # DeepSeek V4 的 DSML 原始工具标签，兼容半角/全角竖线及单双竖线。
    r"<\s*/?\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*(?:tool_calls|invoke|parameter)\b",
    # 某些兼容端点会漏掉 DSML 前缀，只剩 XML 风格工具标签。
    r"<\s*/?\s*(?:tool_calls|invoke|parameter)\b[^>]*>",
    # 另一些模型会把工具名本身当作 XML 标签输出，例如
    # ``<search_memes query="开心" />``，而不是返回结构化 tool_calls。
    r"<\s*/?\s*search_memes\b[^>]{0,2000}>",
    # Some local OpenAI-compatible models print a Python-style pseudo call as
    # ordinary assistant text instead of returning a structured tool_calls item.
    # Anchor it to a complete line so normal technical prose is not affected.
    r"(?m)^[ \t]*(?:await[ \t]+)?search_memes[ \t]*\([^\r\n]{0,2000}\)[ \t]*[。.]?[ \t]*$",
)


# Replyer 偶尔会把 Planner JSON 解释成需要向用户复述的写作提纲。这里不以
# “我应该”单个短语作判断，避免误伤正常聊天；只有明确字段标签、写作过程叙述
# 或多个信号同时出现时才视为内部规划泄漏。
INTERNAL_REASONING_LABEL_PATTERN = re.compile(
    r"(?i)(?:^|[\s，,。；;！？!?])"
    r"(?P<label>计划|回复计划|reaction|reply[\s_-]*act|emotion|tone|"
    r"must[\s_-]*include|avoid|facts|intent|情绪|语气)\s*[：:]"
)
INTERNAL_REASONING_STRONG_LABEL_PATTERN = re.compile(
    r"(?i)(?:^|[\s，,。；;！？!?])"
    r"(?:回复计划|reply[\s_-]*act|must[\s_-]*include|avoid|facts)\s*[：:]"
)
INTERNAL_REASONING_DECISIVE_PATTERN = re.compile(
    r"(?i)(?:(?:根据|按照)(?:这个|本轮|上面的?)?计划\s*[，,:：]?\s*"
    r"我(?:应该|需要|会)|(?:最终|可见)回复(?:内容|台词)?\s*[：:])"
)
INTERNAL_REASONING_PROCESS_PATTERNS: tuple[str, ...] = (
    r"(?:根据|按照)(?:这个|本轮|上面的?)?计划.{0,80}我(?:应该|需要|会)",
    r"(?:考虑到|这显然是).{0,120}我(?:应该|需要|会)",
    r"我(?:现在|接下来)?(?:应该|需要)(?:先|表现|回应|回复|做出|采取|说)",
    r"(?:主人|用户|群友|对方)(?:刚才|刚刚|又|已经)?(?:发|发送|说|问)"
    r".{0,160}(?:我应该|我需要|根据计划|回复计划|reply[\s_-]*act)",
    r"(?:画面里|画面中的?|图片里|图片中的?|这张图).{0,160}"
    r"(?:我应该|我需要|根据计划|回复计划|reply[\s_-]*act)",
    r"(?:最终|可见)回复(?:内容|台词)?\s*[：:]",
)


INTERNAL_MEME_REFERENCE_PATTERN = re.compile(
    r"(?<![\w])"
    r"(?:&{1,2}\s*)?"
    r"(?:`{1,3}\s*)?"
    r"(?:(?:meme)\s*:\s*){1,2}"
    r"(?P<digest>[0-9a-f]{12,64})"
    r"(?:\s*`{1,3})?"
    r"(?:\s*&{1,2})?"
    r"(?![\w])",
    re.IGNORECASE,
)
INTERNAL_MEME_CALL_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:await[ \t]+)?search_memes[ \t]*"
    r"\([^\r\n]{0,2000}\)[ \t]*[。.]?[ \t]*$"
)
INTERNAL_MEME_XML_CALL_PATTERN = re.compile(
    r"<\s*search_memes\b[^>]{0,2000}"
    r"(?:/\s*>|>\s*.*?\s*</\s*search_memes\s*>)[ \t]*[。.]?",
    re.IGNORECASE | re.DOTALL,
)


def contains_tool_protocol(text: str) -> bool:
    """判断模型是否把内部工具协议错误地写进了可见正文。"""
    value = str(text or "")
    return any(
        re.search(pattern, value, flags=re.IGNORECASE)
        for pattern in TOOL_PROTOCOL_PATTERNS
    )


def contains_internal_reasoning(text: str) -> bool:
    """判断模型是否把 Planner 字段或写作推理错误地说给了用户。"""
    value = str(text or "").strip()
    if not value:
        return False

    labels = {
        re.sub(r"[\s_-]+", "_", match.group("label").lower())
        for match in INTERNAL_REASONING_LABEL_PATTERN.finditer(value)
    }
    if INTERNAL_REASONING_DECISIVE_PATTERN.search(value):
        return True
    if len(labels) >= 3:
        return True

    process_hits = sum(
        1
        for pattern in INTERNAL_REASONING_PROCESS_PATTERNS
        if re.search(pattern, value, flags=re.IGNORECASE | re.DOTALL)
    )
    if process_hits >= 2:
        return True
    if process_hits and len(labels) >= 1:
        return True
    if process_hits and INTERNAL_REASONING_STRONG_LABEL_PATTERN.search(value):
        return True
    return False


def contains_nonowner_intimacy(text: str) -> bool:
    """识别回复是否把主人专属亲密直接给了普通群友。"""
    value = str(text or "")
    for pattern in NONOWNER_INTIMACY_PATTERNS:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            prefix = value[max(0, match.start() - 12) : match.start()]
            if re.search(
                r"(?:别|才不会|休想|禁止|拒绝|不能|不可以|不许|不会|不愿意).{0,4}$",
                prefix,
                flags=re.IGNORECASE,
            ):
                continue
            return True
    return False


def contains_nonowner_identity_confusion(text: str) -> bool:
    """识别回复是否把当前普通群友当成主人，或把其发言归给主人。"""
    value = str(text or "")
    return any(
        re.search(pattern, value, flags=re.IGNORECASE)
        for pattern in NONOWNER_IDENTITY_PATTERNS
    )


def contains_group_participation_mismatch(text: str) -> bool:
    """识别主动群聊是否退化成采访、主持或一对一陪聊。"""
    value = str(text or "")
    return any(
        re.search(pattern, value, flags=re.IGNORECASE)
        for pattern in GROUP_PARTICIPATION_PATTERNS
    )


def contains_emotional_reaction(text: str) -> bool:
    """判断开头是否真正演出了情绪，而不是平静说明态度。"""
    value = str(text or "").strip()[:160]
    if not value:
        return False
    if EMOTIONAL_REACTION_PATTERN.search(value):
        return True
    # 口吃、突然停顿和双重感叹通常也是可听见的第一拍反应。
    return bool(
        re.search(r"([\u4e00-\u9fff])[、，,]\1|[！？!?]{2,}|…{1,2}", value)
    )


def extract_and_clean_internal_meme_references(text: str) -> tuple[str, list[str]]:
    """Remove leaked Meme Manager artifacts and return normalized image IDs.

    The reply model occasionally emits a single ampersand, a bare reference, or
    a duplicated ``meme:`` prefix instead of the documented wrapped marker.
    Meme Manager cannot consume those malformed variants, so they must never be
    allowed to become visible chat text.
    """
    references: list[str] = []

    def remove_reference(match: re.Match[str]) -> str:
        normalized = f"meme:{match.group('digest').lower()}"
        if normalized not in references:
            references.append(normalized)
        return ""

    value = INTERNAL_MEME_REFERENCE_PATTERN.sub(remove_reference, str(text or ""))
    # A failed or malformed tool round can leave Python-like or XML-like
    # ``search_memes`` calls in otherwise valid prose. They are internal
    # instructions, never visible chat.
    value = INTERNAL_MEME_XML_CALL_PATTERN.sub("", value)
    value = INTERNAL_MEME_CALL_PATTERN.sub("", value)
    value = re.sub(r"(?m)^[ \t]*&{1,2}[ \t]*$", "", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip(), references


def clean_response(text: str, reply_shape: str = "chat_bubbles") -> str:
    """清理模型泄露的外层格式，同时保留内容型回答的正常结构。"""
    value = str(text or "").strip()
    value = re.sub(
        r"^(?:亚托莉|ATRI|回复|回答|答案(?:是)?)[：:]\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = value.replace("\r\n", "\n").replace("\r", "\n")

    if reply_shape == "long_form":
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    value = re.sub(r"^```(?:\w+)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    value = value.replace("**", "").replace("__", "")
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", value)
    value = re.sub(r"(?m)^\s*(?:[-*+]\s+|\d+[.)、]\s+)", "", value)
    value = re.sub(r"\*[^*\n]{1,80}\*", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]*\n+", "\n", value)
    return "\n".join(line.strip() for line in value.splitlines() if line.strip()).strip()


def find_violations(
    text: str,
    *,
    reply_shape: str,
    soft_chars: int,
    max_bubbles: int = 3,
    is_owner: bool | None = None,
    conversation_mode: str = "direct_reply",
    required_reaction: str = "",
    require_emotional_reaction: bool = False,
    grounding_facts: list[str] | tuple[str, ...] | None = None,
    enforce_group_participation_guard: bool = True,
) -> list[str]:
    """寻找需要重写的输出问题；软篇幅本身不会触发截断。"""
    value = str(text or "").strip()
    violations: list[str] = []
    if not value:
        return ["回复为空"]

    if contains_tool_protocol(value):
        violations.append(TOOL_PROTOCOL_VIOLATION)
    if contains_internal_reasoning(value):
        violations.append(INTERNAL_REASONING_VIOLATION)

    if reply_shape == "long_form":
        runaway_limit = max(3600, soft_chars * 3)
        if len(value) > runaway_limit:
            violations.append(f"内容型回答明显失控（超过{runaway_limit}字）")
    else:
        runaway_limit = max(360, soft_chars * 3)
        if len(value) > runaway_limit:
            violations.append(f"闲聊明显失控（超过{runaway_limit}字）")
        visible_lines = [line for line in value.splitlines() if line.strip()]
        if len(visible_lines) > max(1, max_bubbles * 2):
            violations.append("闲聊气泡过多")
        if re.search(r"(?m)^\s*(?:#{1,6}|[-*+]\s|\d+[.)、]\s)", value) or "**" in value:
            violations.append("闲聊使用 Markdown 或列表")

    for pattern, reason in META_PATTERNS:
        # “答案/结论是” and “总结一下” are stiff in casual chat, but they are
        # legitimate connective phrases in explanations and troubleshooting.
        # Treating them as hard failures caused a valid technical answer to be
        # discarded when the optional style rewrite timed out.
        if reply_shape == "long_form" and reason in {"答卷式开头", "总结腔"}:
            continue
        if re.search(pattern, value, flags=re.IGNORECASE):
            violations.append(reason)
    if is_owner is False and contains_nonowner_identity_confusion(value):
        violations.append(IDENTITY_VIOLATION)
    if (
        is_owner is False
        and reply_shape == "chat_bubbles"
        and contains_nonowner_intimacy(value)
    ):
        violations.append(RELATIONSHIP_VIOLATION)
    if (
        enforce_group_participation_guard
        and conversation_mode in {"ambient_join", "quiet_topic"}
        and reply_shape == "chat_bubbles"
        and contains_group_participation_mismatch(value)
    ):
        violations.append(GROUP_PARTICIPATION_VIOLATION)
    if (
        contains_unsupported_personal_experience(value, grounding_facts)
        and reply_shape == "chat_bubbles"
    ):
        violations.append(REALITY_GROUNDING_VIOLATION)
    if (
        contains_unsupported_market_claim(value, grounding_facts)
        and reply_shape == "chat_bubbles"
    ):
        violations.append(FACT_GROUNDING_VIOLATION)
    if (
        require_emotional_reaction
        and str(required_reaction or "").strip()
        and reply_shape == "chat_bubbles"
        and not contains_emotional_reaction(value)
    ):
        violations.append(EMOTIONAL_REACTION_VIOLATION)
    return violations


def identity_safe_fallback() -> str:
    """模型连续违反身份边界时使用的自然、无权限歧义回复。"""
    return (
        "等等，刚才差点把人弄混了……\n"
        "你是现在和我说话的群友，不是Master。这次已经重新校准好了！"
    )


def protocol_safe_fallback() -> str:
    """内部协议连续泄漏时使用的自然、不可执行兜底回复。"""
    return "等等，刚才那句不算！只是暂时校准失误啦……\n你稍后再叫我一下嘛。"


def reasoning_safe_fallback() -> str:
    """内部规划连续泄漏或违规重写失败时使用的可见短回复。"""
    return "等、等一下，刚才那段不算！语言模块暂时打了个结……\n你稍后再叫我一下嘛。"


def relationship_safe_fallback() -> str:
    """模型连续越过普通群友关系边界时使用的自然角色回复。"""
    return (
        "喂，不许随便对高性能机器人动手动脚！\n"
        "熟归熟，边界还是要有的嘛。"
    )


def emotional_reaction_safe_fallback(is_owner: bool, seed_text: str = "") -> str:
    """模型连续把调戏场景答平时使用的角色化短回复。"""
    if is_owner:
        return "等、等等！主人怎么突然说这种话呀……处理器都要过热了！"
    variants = (
        "喂！你在说什么奇怪的话呀……不许乱说啦！",
        "等、等一下！这种话是可以随便对高性能机器人说的吗！",
        "你干嘛突然说这个呀！快把这句收回去啦！",
    )
    checksum = sum(ord(char) for char in str(seed_text or ""))
    return variants[checksum % len(variants)]


def reality_safe_fallback(conversation_mode: str = "direct_reply") -> str:
    """连续编造自传事实时宁可不插话；直接对话则自然承认没有亲历。"""
    if conversation_mode in {"ambient_join", "quiet_topic"}:
        return ""
    return "这个我没亲自试过啦，还是听你们讲比较靠谱。"


def _split_sentences(text: str) -> list[str]:
    parts = re.findall(r".+?(?:[。！？!?]+|…{1,2}(?=\s|$)|$)", text, flags=re.DOTALL)
    return [part.strip() for part in parts if part.strip()]


def _merge_to_limit(parts: list[str], limit: int) -> list[str]:
    result = list(parts)
    while len(result) > limit:
        index = min(
            range(len(result) - 1),
            key=lambda item: len(result[item]) + len(result[item + 1]),
        )
        joiner = "" if re.search(r"[。！？!?…]$", result[index]) else "，"
        result[index : index + 2] = [
            f"{result[index]}{joiner}{result[index + 1]}".strip()
        ]
    return result


def split_chat_bubbles(text: str, max_bubbles: int = 3) -> list[str]:
    """按自然换行或完整句子拆成聊天气泡，绝不从固定字符处切断。"""
    value = clean_response(text, "chat_bubbles")
    if not value:
        return []
    limit = max(1, max_bubbles)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) > 1:
        return _merge_to_limit(lines, limit)
    sentences = _split_sentences(lines[0])
    if len(sentences) <= 1:
        return lines
    return _merge_to_limit(sentences, limit)
