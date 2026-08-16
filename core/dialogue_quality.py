from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any


REPETITION_VIOLATION = "与近期机器人回复高度重复"
CATCHPHRASE_REPETITION_VIOLATION = "连续复用同一角色口癖"

_REQUESTED_REPEAT_RE = re.compile(
    r"(?:复读|重复|再说一遍|再讲一遍|照着说|照原样|原样说|复述|原话|"
    r"刚才说什么|你说了什么)",
    flags=re.IGNORECASE,
)
_ROLE_CATCHPHRASES = (
    "高性能机器人",
    "暂时校准失误",
    "机器人条例",
)
_VISIBLE_CHARS_RE = re.compile(r"[^\w\u3400-\u9fff]+", flags=re.UNICODE)


def normalize_dialogue_text(text: str) -> str:
    """Normalize only for comparison; never rewrite the visible role reply."""

    value = str(text or "").strip().lower()
    value = re.sub(
        r"^(?:亚托莉|atri|回复|回答|答案(?:是)?)[：:]\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return _VISIBLE_CHARS_RE.sub("", value)


def dialogue_similarity(left: str, right: str) -> float:
    left_value = normalize_dialogue_text(left)
    right_value = normalize_dialogue_text(right)
    if not left_value or not right_value:
        return 0.0
    return SequenceMatcher(None, left_value, right_value).ratio()


def recent_assistant_replies(
    contexts: Iterable[dict[str, Any]] | None,
    limit: int = 6,
) -> list[str]:
    replies: list[str] = []
    for item in list(contexts or []):
        if not isinstance(item, dict) or str(item.get("role", "")).lower() != "assistant":
            continue
        content = str(item.get("content", "") or "").strip()
        if content:
            replies.append(content)
    return replies[-max(1, int(limit)) :]


def find_dialogue_repetition(
    text: str,
    recent_replies: Iterable[str] | None,
    *,
    current_message: str = "",
) -> str:
    """Return a quality violation only for strong, conversational repetition."""

    if _REQUESTED_REPEAT_RE.search(str(current_message or "")):
        return ""
    current = normalize_dialogue_text(text)
    if len(current) < 6:
        return ""

    previous_values = [
        (reply, normalize_dialogue_text(reply))
        for reply in list(recent_replies or [])[-6:]
    ]
    for previous, normalized in reversed(previous_values):
        if len(normalized) < 6:
            continue
        if current == normalized:
            return REPETITION_VIOLATION
        shorter, longer = sorted((current, normalized), key=len)
        if (
            len(shorter) >= 10
            and shorter in longer
            and len(shorter) / max(1, len(longer)) >= 0.58
        ):
            return REPETITION_VIOLATION
        if min(len(current), len(normalized)) >= 12 and dialogue_similarity(text, previous) >= 0.88:
            return REPETITION_VIOLATION

    for catchphrase in _ROLE_CATCHPHRASES:
        if catchphrase not in text:
            continue
        if any(catchphrase in reply for reply in list(recent_replies or [])[-1:]):
            return CATCHPHRASE_REPETITION_VIOLATION
    return ""


def sanitize_plan_requirements(
    plan: Any,
    recent_replies: Iterable[str] | None,
    *,
    current_message: str = "",
) -> list[str]:
    """Remove final-line fragments copied from recent replies, preserving semantic beats."""

    if _REQUESTED_REPEAT_RE.search(str(current_message or "")):
        return []
    recent = [str(reply or "") for reply in list(recent_replies or []) if str(reply or "").strip()]
    original = list(getattr(plan, "must_include", []) or [])
    kept: list[str] = []
    removed: list[str] = []
    for item in original:
        value = str(item or "").strip()
        normalized = normalize_dialogue_text(value)
        copied = False
        if len(normalized) >= 7:
            for reply in recent[-6:]:
                previous = normalize_dialogue_text(reply)
                if normalized and normalized in previous:
                    copied = True
                    break
                if min(len(normalized), len(previous)) >= 10 and dialogue_similarity(value, reply) >= 0.86:
                    copied = True
                    break
        looks_like_final_line = bool(
            len(normalized) >= 16
            and re.search(r"[。！？!?]|(?:啦|嘛|哦|呢)$", value)
            and re.search(r"(?:我|你|主人|大家)", value)
        )
        if copied or looks_like_final_line:
            removed.append(value)
        else:
            kept.append(value)
    plan.must_include = kept
    return removed


def repetition_safe_fallback(
    seed_text: str = "",
    recent_replies: Iterable[str] | None = None,
) -> str:
    """Last-resort roleful recovery without a fixed machine-status catchphrase."""

    variants = (
        "啊，刚才那句怎么又绕回去了……这次我换个反应。",
        "等等，我刚才好像在原地打转了……你这句我重新接。",
        "唔，刚才那句不算，我明明可以换个说法的。",
        "诶，我怎么又说回去了……好，这次认真听你这句。",
    )
    recent = list(recent_replies or [])
    ranked = sorted(
        variants,
        key=lambda value: max(
            (dialogue_similarity(value, previous) for previous in recent),
            default=0.0,
        ),
    )
    if not ranked:
        return variants[0]
    best_score = max(
        (dialogue_similarity(ranked[0], previous) for previous in recent),
        default=0.0,
    )
    if best_score < 0.70:
        return ranked[0]
    checksum = sum(ord(char) for char in str(seed_text or ""))
    return variants[checksum % len(variants)]
