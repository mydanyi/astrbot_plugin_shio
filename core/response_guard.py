from __future__ import annotations

import re
from collections.abc import Iterable

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

EXPLICIT_FOREIGN_LANGUAGE_REQUEST_PATTERN = re.compile(
    r"(?:用|改用|请用|请以)"
    r"\s*(?:全)?(?:英文|英语|日语|日文|韩语|韩文|法语|德语|俄语|西班牙语|外语)|"
    r"请(?:说|讲|回复|回答|写|输出)\s*(?:全)?"
    r"(?:英文|英语|日语|日文|韩语|韩文|法语|德语|俄语|西班牙语|外语)|"
    r"(?:翻译(?:成|为)?|译成)\s*"
    r"(?:英文|英语|日语|日文|韩语|韩文|法语|德语|俄语|西班牙语|外语)|"
    r"^\s*(?:请|麻烦)?\s*(?:英文|英语|日语|日文|韩语|韩文|法语|德语|俄语|西班牙语|外语)"
    r"\s*(?:回复|回答|作答|输出|写|翻译)\s*[：:,，]|"
    r"\b(?:please\s+)?(?:reply|respond|answer|write|translate|say)\b.{0,24}"
    r"\b(?:in|into|to)\s+(?:English|Japanese|Korean|French|German|Russian|Spanish)\b|"
    r"\bin\s+(?:English|Japanese|Korean|French|German|Russian|Spanish)\b",
    flags=re.IGNORECASE,
)

FOREIGN_LANGUAGE_NEGATION_PATTERN = re.compile(
    r"(?:不要|别|不准|不许|禁止|无需|不用).{0,10}"
    r"(?:英文|英语|日语|日文|韩语|韩文|法语|德语|俄语|西班牙语|外语)|"
    r"(?:为什么|怎么|为何).{0,18}(?:说|用|回复|回答|输出).{0,8}"
    r"(?:英文|英语|日语|日文|韩语|韩文|法语|德语|俄语|西班牙语|外语)|"
    r"\b(?:why|don't|do\s+not|never|stop)\b.{0,40}"
    r"(?:English|Japanese|Korean|French|German|Russian|Spanish)\b",
    flags=re.IGNORECASE,
)


def requests_foreign_language_output(current_message: str) -> bool:
    """Only an explicit language request may override the default Chinese output."""

    message = str(current_message or "")
    if FOREIGN_LANGUAGE_NEGATION_PATTERN.search(message):
        return False
    return bool(EXPLICIT_FOREIGN_LANGUAGE_REQUEST_PATTERN.search(message))


def contains_unexpected_foreign_language(text: str, current_message: str = "") -> bool:
    """Conservatively detect a role reply that drifted away from Chinese.

    Code, URLs and ordinary English proper nouns are ignored. Short internet
    interjections such as ``OK`` are also left alone; the guard is aimed at
    complete foreign-language replies such as ``Wait a second!``.
    """

    if requests_foreign_language_output(current_message):
        return False
    visible = str(text or "")
    visible = re.sub(r"```[\s\S]*?```", " ", visible)
    visible = re.sub(r"`[^`\n]+`", " ", visible)
    visible = re.sub(r"https?://\S+|www\.\S+", " ", visible, flags=re.IGNORECASE)
    han_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", visible))
    latin_words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", visible)
    latin_count = sum(len(word) for word in latin_words)
    if han_count == 0:
        return latin_count >= 8 and len(latin_words) >= 2
    return (
        latin_count >= max(60, han_count * 5)
        and latin_count / max(1, latin_count + han_count) >= 0.8
    )


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


TOOL_PROTOCOL_PATTERNS: tuple[str, ...] = (
    # llama.cpp 原生工具模板包装。模型偶尔只吐出开头或结尾的一半，
    # 即使内部函数名已经丢失，也不能把模板控制标记交给聊天气泡。
    r"<\s*(?:[|｜]\s*(?:tool_call|tool_response)\s*[|｜]?|"
    r"(?:tool_call|tool_response)\s*[|｜])\s*>",
    # llama.cpp 等 OpenAI 兼容端点可能把聊天模板的隐藏通道标记写进正文。
    # 同时兼容标准 ``<|channel|>`` 与 Gemma 偶发生成的单边竖线变体
    # ``<|channel>`` / ``<channel|>``；要求至少一侧有竖线，避免误伤
    # 正常技术讨论中的普通 ``<channel>`` XML 示例。
    r"<\s*(?:[|｜]\s*(?:channel|message|start|end)\s*[|｜]?|"
    r"(?:channel|message|start|end)\s*[|｜])\s*>",
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
    # v2 typed tool result is an internal reference envelope. Even a valid,
    # complete envelope must never be copied into visible chat.
    r"<\s*/?\s*shio_tool_result_reference\b[^>]*>",
)

_HIDDEN_CHANNEL_SECTION_PATTERN = re.compile(
    r"<\s*(?:[|｜]\s*channel\s*[|｜]?|channel\s*[|｜])\s*>\s*"
    r"(?P<channel>analysis|thought|commentary|final)",
    re.IGNORECASE,
)


def strip_hidden_channel_sections(text: str) -> str:
    """Keep explicit final-channel content and discard hidden channel sections."""

    value = str(text or "")
    matches = list(_HIDDEN_CHANNEL_SECTION_PATTERN.finditer(value))
    if not matches:
        return value
    visible: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        if match.group("channel").lower() == "final":
            content = value[start:end].strip()
            if content:
                visible.append(content)
    return "\n".join(visible).strip()


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
INTERNAL_TOOL_RESULT_REFERENCE_PATTERN = re.compile(
    r"<\s*shio_tool_result_reference\b[^>]{0,2000}>"
    r".*?"
    r"<\s*/\s*shio_tool_result_reference\s*>",
    re.IGNORECASE | re.DOTALL,
)

_SAFE_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_TOOL_CALL_SUFFIX_PATTERN = re.compile(
    r"[ \t]*(?:"
    r"<\s*(?:[|｜]\s*tool_call\s*[|｜]?|tool_call\s*[|｜])\s*>|"
    r"<\s*/\s*tool_call\s*>"
    r")?[ \t]*[。.]?",
    re.IGNORECASE,
)

# llama.cpp/Gemma 偶尔会先完成真实的 ``search_memes`` 调用，随后又把仅含
# ``query`` 的 arguments 对象作为普通文本节点返回。此时工具名已经丢失，
# 甚至会出现 ``{，"query": ...}`` 这种畸形开头；按工具名扫描无法命中。
# 这里只识别独占一段的单参数对象，避免误伤正文中内联讲解的 JSON 示例。
_ORPHANED_QUERY_ARGUMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?ms)^[ \t]*\{[ \t]*[,，]?[ \t]*"
        r"(?:\"query\"|'query'|query)[ \t]*[:：][ \t]*"
        r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
        r"\s*[,，]?\s*\}[ \t]*[。.]?[ \t]*$"
    ),
    # 有些 Provider 还会把开头的 ``{`` 吃掉，只留下带引号的 key 和结尾。
    re.compile(
        r"(?ms)^[ \t]*[,，]?[ \t]*(?:\"query\"|'query')"
        r"[ \t]*[:：][ \t]*"
        r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
        r"\s*[,，]?\s*\}[ \t]*[。.]?[ \t]*$"
    ),
)
_ORPHANED_PROTOCOL_CLOSER_PATTERN = re.compile(
    r"^\s*[\"']?\}\s*[。.]?\s*$"
)


def _normalized_tool_names(tool_names: Iterable[str] | None = None) -> tuple[str, ...]:
    names = {"search_memes"}
    for raw_name in tool_names or ():
        name = str(raw_name or "").strip()
        if _SAFE_TOOL_NAME_PATTERN.fullmatch(name):
            names.add(name)
    return tuple(sorted(names, key=lambda item: (-len(item), item)))


def _structured_tool_call_start_pattern(
    tool_names: Iterable[str] | None = None,
) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(name) for name in _normalized_tool_names(tool_names)
    )
    return re.compile(
        r"^[ \t]*"
        r"(?:<\s*(?:[|｜]\s*tool_call\s*[|｜]?|tool_call\s*[|｜])\s*>[ \t]*)?"
        r"(?:(?:call|response)[ \t]*:[ \t]*"
        r"(?:[A-Za-z_][A-Za-z0-9_.-]*[ \t]*:[ \t]*)*)?"
        rf"(?P<tool>{alternatives})[ \t]*(?:[:：][ \t]*)?"
        r"(?:\r?\n[ \t]*)?(?P<opener>[{(])",
        re.IGNORECASE | re.MULTILINE,
    )


def _structured_tool_xml_start_pattern(
    tool_names: Iterable[str] | None = None,
) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(name) for name in _normalized_tool_names(tool_names)
    )
    return re.compile(
        rf"^[ \t]*<\s*(?P<tool>{alternatives})\b[^\r\n>]{{0,2000}}>",
        re.IGNORECASE | re.MULTILINE,
    )


def _bare_tool_name_pattern(
    tool_names: Iterable[str] | None = None,
    *,
    multiline: bool = False,
    require_colon: bool = False,
) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(name) for name in _normalized_tool_names(tool_names)
    )
    colon = r"[:：]" if require_colon else r"[:：]?"
    flags = re.IGNORECASE | (re.MULTILINE if multiline else 0)
    return re.compile(
        rf"^[ \t]*(?:{alternatives})[ \t]*{colon}[ \t]*[。.]?[ \t]*$",
        flags,
    )


def _balanced_tool_call_end(value: str, opener_at: int, opener: str) -> int:
    """Return the end of a JSON/Python-like call, or fail closed to EOF."""
    closer = "}" if opener == "{" else ")"
    depth = 0
    quote = ""
    escaped = False
    for index in range(opener_at, len(value)):
        char = value[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                suffix = _TOOL_CALL_SUFFIX_PATTERN.match(value, index + 1)
                return suffix.end() if suffix is not None else index + 1
    # A line that starts like a real tool call but never closes is more dangerous
    # than losing its trailing fragment. Do not let a partial call reach users.
    return len(value)


def _structured_tool_xml_end(value: str, match: re.Match[str]) -> int:
    start_tag = match.group(0)
    if re.search(r"/\s*>[ \t]*$", start_tag):
        suffix = _TOOL_CALL_SUFFIX_PATTERN.match(value, match.end())
        return suffix.end() if suffix is not None else match.end()
    closing = re.compile(
        rf"<\s*/\s*{re.escape(match.group('tool'))}\s*>",
        re.IGNORECASE,
    ).search(value, match.end())
    if closing is None:
        # A dynamic tool XML tag without a closing tag is an incomplete
        # protocol node, never ordinary visible prose. Fail closed to EOF.
        return len(value)
    suffix = _TOOL_CALL_SUFFIX_PATTERN.match(value, closing.end())
    return suffix.end() if suffix is not None else closing.end()


def _strip_structured_tool_xml_calls(
    text: str,
    tool_names: Iterable[str] | None = None,
) -> tuple[str, bool]:
    value = str(text or "")
    pattern = _structured_tool_xml_start_pattern(tool_names)
    removed = False
    while True:
        match = pattern.search(value)
        if match is None:
            break
        end = _structured_tool_xml_end(value, match)
        value = value[: match.start()] + value[end:]
        removed = True
    return value, removed


def _strip_structured_tool_calls(
    text: str,
    tool_names: Iterable[str] | None = None,
) -> tuple[str, bool]:
    value = str(text or "")
    pattern = _structured_tool_call_start_pattern(tool_names)
    removed = False
    while True:
        match = pattern.search(value)
        if match is None:
            break
        end = _balanced_tool_call_end(
            value,
            match.start("opener"),
            match.group("opener"),
        )
        value = value[: match.start()] + value[end:]
        removed = True
    return value, removed


def _strip_bare_tool_name_fragments(
    text: str,
    tool_names: Iterable[str] | None = None,
    *,
    protocol_context: bool,
) -> tuple[str, bool]:
    """Remove only standalone protocol residue, never names inside prose.

    A bare tool name is ambiguous inside documentation. It becomes protocol
    residue only when it is the entire outgoing node, carries an explicit
    protocol colon on its own line, or sits beside another artifact that this
    guard has already removed.
    """

    value = str(text or "")
    whole = _bare_tool_name_pattern(tool_names)
    if whole.fullmatch(value):
        return "", True
    removed = False
    value, count = _bare_tool_name_pattern(
        tool_names,
        multiline=True,
        require_colon=True,
    ).subn("", value)
    removed = bool(count)
    if protocol_context:
        value, count = _bare_tool_name_pattern(
            tool_names,
            multiline=True,
        ).subn("", value)
        removed = bool(count) or removed
    return value, removed


def _meme_argument_guard_enabled(tool_names: Iterable[str] | None) -> bool:
    if tool_names is None:
        # Context history cleanup does not carry the original request's tool set.
        return True
    return any(
        str(name or "").strip().lower() == "search_memes"
        for name in tool_names
    )


def _strip_orphaned_tool_argument_fragments(
    text: str,
    tool_names: Iterable[str] | None = None,
) -> tuple[str, bool]:
    """Remove name-less tool arguments and delimiter-only tail nodes."""
    value = str(text or "")
    if not _meme_argument_guard_enabled(tool_names):
        return value, False
    removed = False
    for pattern in _ORPHANED_QUERY_ARGUMENT_PATTERNS:
        value, count = pattern.subn("", value)
        removed = bool(count) or removed

    # A removed arguments block may be followed by another delimiter-only text
    # component. Also fail closed when the entire outgoing value is just that
    # orphaned closer, which is what AstrBot logged immediately before sending.
    if removed:
        value, count = re.subn(
            r"(?m)^[ \t]*[\"']?\}[ \t]*[。.]?[ \t]*$",
            "",
            value,
        )
        removed = bool(count) or removed
    if _ORPHANED_PROTOCOL_CLOSER_PATTERN.fullmatch(value):
        return "", True
    return value, removed


def contains_tool_protocol(
    text: str,
    tool_names: Iterable[str] | None = None,
) -> bool:
    """判断模型是否把内部工具协议错误地写进了可见正文。"""
    value = str(text or "")
    if any(
        re.search(pattern, value, flags=re.IGNORECASE)
        for pattern in TOOL_PROTOCOL_PATTERNS
    ):
        return True
    if _structured_tool_call_start_pattern(tool_names).search(value) is not None:
        return True
    if _structured_tool_xml_start_pattern(tool_names).search(value) is not None:
        return True
    if _bare_tool_name_pattern(tool_names).fullmatch(value) is not None:
        return True
    if (
        _bare_tool_name_pattern(
            tool_names,
            multiline=True,
            require_colon=True,
        ).search(value)
        is not None
    ):
        return True
    if _meme_argument_guard_enabled(tool_names):
        if any(pattern.search(value) for pattern in _ORPHANED_QUERY_ARGUMENT_PATTERNS):
            return True
        if _ORPHANED_PROTOCOL_CLOSER_PATTERN.fullmatch(value) is not None:
            return True
    return False


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


def extract_and_clean_internal_meme_references(
    text: str,
    tool_names: Iterable[str] | None = None,
) -> tuple[str, list[str]]:
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
    value = INTERNAL_TOOL_RESULT_REFERENCE_PATTERN.sub("", value)
    value, xml_removed = _strip_structured_tool_xml_calls(value, tool_names)
    value, call_removed = _strip_structured_tool_calls(value, tool_names)
    value, orphan_removed = _strip_orphaned_tool_argument_fragments(
        value,
        tool_names,
    )
    value, _ = _strip_bare_tool_name_fragments(
        value,
        tool_names,
        protocol_context=xml_removed or call_removed or orphan_removed,
    )
    value = re.sub(r"(?m)^[ \t]*&{1,2}[ \t]*$", "", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip(), references


def clean_response(text: str, reply_shape: str = "chat_bubbles") -> str:
    """清理模型泄露的外层格式，同时保留内容型回答的正常结构。"""
    value = strip_hidden_channel_sections(text).strip()
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


_BUBBLE_OPEN_TO_CLOSE = {
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "【": "】",
    "[": "]",
}
_BUBBLE_SENTENCE_END = frozenset("。！？!?")
_BUBBLE_DEPENDENT_START_RE = re.compile(
    r"^(?:"
    r"而是|"
    r"因为|是因为|所以|因此|于是|然后|否则|"
    r"也就是说|换句话说|"
    r"其中|另一个|前者|后者|对此|关于这|"
    r"才|还|仍(?:然)?"
    r")"
)
_BUBBLE_CONDITIONAL_START_RE = re.compile(
    r"^(?:如果|假如|要是|倘若|若|只有|除非|一旦|当|等到)"
)


def _split_sentences(text: str) -> list[str]:
    """Split only at sentence boundaries outside quotes and brackets."""

    parts: list[str] = []
    buffer: list[str] = []
    expected_closers: list[str] = []
    ascii_quote = False

    def flush() -> None:
        value = "".join(buffer).strip()
        buffer.clear()
        if value:
            parts.append(value)

    for index, char in enumerate(text):
        if char == "\n" and not expected_closers and not ascii_quote:
            flush()
            continue
        buffer.append(char)
        if char == '"' and (index == 0 or text[index - 1] != "\\"):
            ascii_quote = not ascii_quote
            if not ascii_quote and len(buffer) > 1 and buffer[-2] in _BUBBLE_SENTENCE_END:
                flush()
            continue
        closer = _BUBBLE_OPEN_TO_CLOSE.get(char)
        if closer is not None:
            expected_closers.append(closer)
            continue
        if expected_closers and char == expected_closers[-1]:
            expected_closers.pop()
            if (
                not expected_closers
                and not ascii_quote
                and len(buffer) > 1
                and buffer[-2] in _BUBBLE_SENTENCE_END
            ):
                flush()
            continue
        if not expected_closers and not ascii_quote and char in _BUBBLE_SENTENCE_END:
            flush()
    flush()
    return parts


def _boundary_requires_merge(left: str, right: str) -> bool:
    left_value = left.strip()
    right_value = right.strip()
    if not left_value or not right_value:
        return True
    left_scope = re.sub(r"[。！？!?…]+$", "", left_value).strip()
    if _BUBBLE_DEPENDENT_START_RE.search(right_value):
        return True
    if re.match(r"^(?:但(?:是)?|不过|可是|然而|却)", right_value) and re.search(
        r"(?:可能|也许|无法|不能|并非|不是|没(?:有)?|未|部分|"
        r"影响|生效|提交|成功|失败|完成|不确定|说不准)",
        right_value,
    ):
        return True
    if (
        _BUBBLE_CONDITIONAL_START_RE.search(left_scope)
        and not re.search(r"[。！？!?].{0,40}(?:才|就|便|那么)", left_value)
    ):
        return True
    if re.search(r"(?:因为|由于|之所以)$", left_scope):
        return True
    if "之所以" in left_scope and "是因为" not in left_scope:
        return True
    if re.search(r"(?:不是|并非)", left_scope) and re.match(
        r"^(?:而是|是因为|只是|其实|实际)", right_value
    ):
        return True
    if "虽然" in left_scope and not re.search(r"(?:但是|不过|仍然|却)", left_scope):
        return True
    if re.search(r"(?:不但|不仅)", left_scope) and not re.search(
        r"(?:而且|还|也)", left_scope
    ):
        return True
    if left_value.endswith(("：", ":", "，", ",", "、", "—", "-")):
        return True
    return False


def _merge_semantic_dependencies(parts: list[str]) -> list[str]:
    merged: list[str] = []
    for part in parts:
        value = part.strip()
        if not value:
            continue
        if merged and _boundary_requires_merge(merged[-1], value):
            joiner = (
                ""
                if re.search(r"[。！？!?…：:，,、—-][”’」』）)\]】]*$", merged[-1])
                else "，"
            )
            merged[-1] = f"{merged[-1]}{joiner}{value}".strip()
        else:
            merged.append(value)
    return merged


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
    """Split only at semantically self-contained, code-owned boundaries."""

    if type(text) is not str:
        raise ValueError("chat_bubble_text_invalid")
    if type(max_bubbles) is not int or not 1 <= max_bubbles <= 8:
        raise ValueError("chat_bubble_limit_invalid")
    value = clean_response(text, "chat_bubbles")
    if not value:
        return []
    semantic_units = _merge_semantic_dependencies(_split_sentences(value))
    if len(semantic_units) <= 1:
        return semantic_units
    return _merge_to_limit(semantic_units, max_bubbles)
