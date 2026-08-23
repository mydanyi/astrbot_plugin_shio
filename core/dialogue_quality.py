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
_VISIBLE_CHARS_RE = re.compile(r"[^\w\u3400-\u9fff]+", flags=re.UNICODE)
_CLAUSE_SPLIT_RE = re.compile(r"[，,。！？!?；;\n]+")
_HAN_RE = re.compile(r"[\u3400-\u9fff]")


def normalize_dialogue_text(text: str) -> str:
    """Normalize only for comparison; never rewrite the visible role reply."""

    value = str(text or "").strip().lower()
    value = re.sub(
        r"^(?:回复|回答|答案(?:是)?)[：:]\s*",
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


def dialogue_structure_signature(text: str) -> tuple[str, ...]:
    """Return a bounded opening frame without encoding any role vocabulary."""

    clauses = tuple(
        normalize_dialogue_text(value)
        for value in _CLAUSE_SPLIT_RE.split(str(text or ""))
        if normalize_dialogue_text(value)
    )
    if not clauses:
        return ()
    first = clauses[0][:8]
    second = clauses[1][:3] if len(clauses) > 1 else ""
    return tuple(value for value in (first, second) if value)


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
    role_phrases: Iterable[str] | None = None,
) -> str:
    """Return a quality violation only for strong, conversational repetition."""

    if _REQUESTED_REPEAT_RE.search(str(current_message or "")):
        return ""
    current = normalize_dialogue_text(text)
    if len(current) < 6:
        return ""

    recent = [str(reply or "") for reply in list(recent_replies or [])[-6:]]
    previous_values = [
        (reply, normalize_dialogue_text(reply))
        for reply in recent
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

    current_signature = dialogue_structure_signature(text)
    if current_signature and len(current_signature[0]) >= 3:
        matching_openings = sum(
            dialogue_structure_signature(previous) == current_signature
            for previous in recent[-4:]
        )
        if matching_openings >= 2:
            return REPETITION_VIOLATION

    for _, normalized in previous_values:
        shared = SequenceMatcher(None, current, normalized).find_longest_match(
            0,
            len(current),
            0,
            len(normalized),
        )
        fragment = current[shared.a : shared.a + shared.size]
        if (
            shared.size >= 6
            and len(_HAN_RE.findall(fragment)) >= 4
            and shared.size / max(1, min(len(current), len(normalized))) >= 0.35
            and fragment not in normalize_dialogue_text(current_message)
        ):
            return CATCHPHRASE_REPETITION_VIOLATION

    phrases = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in list(role_phrases or [])
            if str(value or "").strip()
        )
    )
    for catchphrase in phrases:
        normalized_phrase = normalize_dialogue_text(catchphrase)
        if len(normalized_phrase) < 3 or normalized_phrase not in current:
            continue
        if any(normalized_phrase in normalized for _, normalized in previous_values):
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
