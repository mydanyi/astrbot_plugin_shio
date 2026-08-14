from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_QUESTION_OR_REQUEST_RE = re.compile(
    r"[？?]|(?:你|您).{0,8}(?:觉得|看看|知道|记得|能|可以|会不会)|"
    r"(?:怎么|如何|为啥|为什么|是不是|有没有|能不能|可不可以|帮我|告诉我|回答|解释|说说|看看)"
)
_TITLE_CONTEXT_RE = re.compile(
    r"(?:买了|玩了|看了|通关了|下载了|推荐|讨论).{0,6}$|"
    r"^(?:这部|这款|那个)?(?:游戏|作品|动画|视觉小说|角色|表情|图片|壁纸|原作)"
)
_VOCATIVE_PUNCTUATION = "，,、：:；;！？!?。.…~～ "
_TAIL_PARTICLES = "啊呀呢嘛吗吧哦哟啦呐呗诶唉欸"


@dataclass(frozen=True, slots=True)
class NameWakeDecision:
    kind: str
    alias: str = ""
    reason: str = ""

    @property
    def is_direct(self) -> bool:
        return self.kind == "direct"


def _sanitize(text: str) -> str:
    value = _FENCED_CODE_RE.sub(" ", str(text or ""))
    value = _INLINE_CODE_RE.sub(" ", value)
    return _URL_RE.sub(" ", value).strip()


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    if alias.isascii() and any(char.isalnum() for char in alias):
        return re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def _looks_like_title_reference(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 10) : start]
    after = text[end : end + 10]
    if before.endswith(("《", "〈", "「", "『")) and after.startswith(("》", "〉", "」", "』")):
        return True
    return bool(_TITLE_CONTEXT_RE.search(before) or _TITLE_CONTEXT_RE.search(after))


def classify_name_wake(
    text: str,
    aliases: Iterable[str],
    *,
    mode: str = "natural",
) -> NameWakeDecision:
    """区分明确直呼与仅仅谈到角色。

    URL、行内代码和代码块中的名字不会参与匹配。natural 模式把句首、句尾、
    带称呼标点或带明确提问/请求的名字视为直呼；其他情况只算普通提及。
    """

    clean = _sanitize(text)
    if not clean:
        return NameWakeDecision("none", reason="没有可匹配的自然文本")

    normalized_mode = str(mode or "natural").strip().lower()
    for raw_alias in aliases:
        alias = str(raw_alias or "").strip()
        if not alias:
            continue
        match = _alias_pattern(alias).search(clean)
        if match is None:
            continue
        start, end = match.span()
        if _looks_like_title_reference(clean, start, end):
            return NameWakeDecision("mention", alias, "作品名或角色资料语境")
        if normalized_mode == "contains":
            return NameWakeDecision("direct", alias, "配置为名字出现即唤醒")

        before = clean[:start]
        after = clean[end:]
        before_trimmed = before.rstrip(_VOCATIVE_PUNCTUATION)
        after_trimmed = after.strip(_VOCATIVE_PUNCTUATION + _TAIL_PARTICLES)
        at_start = not before_trimmed
        at_end = not after_trimmed
        punctuated = (
            (before and before[-1] in _VOCATIVE_PUNCTUATION)
            or (after and after[0] in _VOCATIVE_PUNCTUATION)
        )
        if clean.casefold() == alias.casefold():
            return NameWakeDecision("direct", alias, "只发送了角色称呼")
        if at_start:
            return NameWakeDecision("direct", alias, "名字位于句首")
        if at_end:
            return NameWakeDecision("direct", alias, "名字位于句尾")
        if punctuated and _QUESTION_OR_REQUEST_RE.search(clean):
            return NameWakeDecision("direct", alias, "带称呼停顿的提问或请求")
        if _QUESTION_OR_REQUEST_RE.search(clean):
            return NameWakeDecision("direct", alias, "包含名字的明确提问或请求")
        return NameWakeDecision("mention", alias, "只是自然提到角色")
    return NameWakeDecision("none", reason="没有命中角色称呼")
