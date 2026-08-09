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
    r"(?:^|[\n。！？!?])\s*(?:主人|Master)(?:大人)?(?:这么|这样|说|要求|想|要|愿意)",
    r"(?:^|[\n。！？!?])\s*(?:主人|Master)(?:大人)?\s*[，,:：！!？?～~]",
)

IDENTITY_VIOLATION = "把普通群友误认成主人、Master或群主"
TOOL_PROTOCOL_VIOLATION = "泄露内部工具调用协议"


TOOL_PROTOCOL_PATTERNS: tuple[str, ...] = (
    # DeepSeek V4 的 DSML 原始工具标签，兼容半角/全角竖线及单双竖线。
    r"<\s*/?\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*(?:tool_calls|invoke|parameter)\b",
    # 某些兼容端点会漏掉 DSML 前缀，只剩 XML 风格工具标签。
    r"<\s*/?\s*(?:tool_calls|invoke|parameter)\b[^>]*>",
)


def contains_tool_protocol(text: str) -> bool:
    """判断模型是否把内部工具协议错误地写进了可见正文。"""
    value = str(text or "")
    return any(
        re.search(pattern, value, flags=re.IGNORECASE)
        for pattern in TOOL_PROTOCOL_PATTERNS
    )


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
) -> list[str]:
    """寻找需要重写的输出问题；软篇幅本身不会触发截断。"""
    value = str(text or "").strip()
    violations: list[str] = []
    if not value:
        return ["回复为空"]

    if contains_tool_protocol(value):
        violations.append(TOOL_PROTOCOL_VIOLATION)

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
        if re.search(pattern, value, flags=re.IGNORECASE):
            violations.append(reason)
    if is_owner is False and any(
        re.search(pattern, value, flags=re.IGNORECASE)
        for pattern in NONOWNER_IDENTITY_PATTERNS
    ):
        violations.append(IDENTITY_VIOLATION)
    return violations


def identity_safe_fallback() -> str:
    """模型连续违反身份边界时使用的自然、无权限歧义回复。"""
    return (
        "等等，刚才差点把人弄混了……\n"
        "你是现在和我说话的群友，不是Master。这次已经重新校准好了！"
    )


def protocol_safe_fallback() -> str:
    """内部协议连续泄漏时使用的自然、不可执行兜底回复。"""
    return "等等，刚才那句不算！只是暂时校准失误啦……\n让我重新说一次嘛。"


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
