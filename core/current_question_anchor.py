from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

from .contracts import DecisionBinding, SemanticAtom, SemanticAtomKind


class CurrentTurnKind(str, Enum):
    QUESTION = "question"
    REQUEST = "request"
    STATEMENT = "statement"


_REQUEST_RE = re.compile(
    r"(?:请|麻烦|帮我|替我|给我)(?:查|查找|搜索|解释|比较|分析|修复|总结|翻译|识别|看看|告诉|回答)"
)
_QUESTION_RE = re.compile(
    r"[？?]|(?:为什么|为啥|怎么|如何|什么|谁|哪里|哪儿|多少|是否|是不是|有没有|能不能|可不可以)"
)
_NEGATIONS = ("并不是", "并非", "不要", "不能", "不是", "没有", "无需", "不必", "别", "没")
_ACTIONS = (
    "联网搜索",
    "查找",
    "搜索",
    "解释",
    "比较",
    "分析",
    "修复",
    "总结",
    "翻译",
    "识别",
    "回答",
    "告诉",
    "启动",
    "检查",
    "看看",
)
_MEDIA_REFERENCES = (
    "引用的这张图片",
    "引用的图片",
    "这张图片",
    "这张图",
    "这个截图",
    "截图",
    "图片",
    "视频",
)
_ASCII_ENTITY_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.+-]{1,31}(?![A-Za-z0-9_])")
_QUOTED_ENTITY_RE = re.compile(r"[《〈「『\"“]([^》〉」』\"”]{1,32})[》〉」』\"”]")
_TOPIC_PATTERNS = (
    re.compile(r"(?:解释|查找|搜索|分析|修复|总结|翻译|识别|看看)\s*([^，,。；;！？?]{2,24})"),
    re.compile(r"([^，,。；;！？?]{2,24})(?:是什么意思|是什么原因|为什么|怎么|如何)"),
)
_CHINESE_ENGLISH_REQUEST_RE = re.compile(
    r"^(?:(?:请|麻烦)(?:你)?|(?:你)?(?:可以|能否|可否)|我(?:想让|希望))?\s*"
    r"(?:用|以)\s*(?:英文|英语)(?:来)?\s*(?:回答|回复|作答|解释|说)",
)
_CHINESE_ENGLISH_NEGATION_RE = re.compile(
    r"(?:不要|别|不用|不必|无需|禁止)\s*(?:再)?\s*(?:用|以)?\s*"
    r"(?:英文|英语)(?:来)?\s*(?:回答|回复|作答|解释|说)?",
)
_CHINESE_ENGLISH_META_RE = re.compile(
    r"(?:为什么|为何|为啥|怎么(?:会)?)\s*(?:还|又|总是|要|会)?\s*"
    r"(?:用|以)\s*(?:英文|英语)(?:来)?\s*(?:回答|回复|作答|解释|说)",
)
_ENGLISH_AFFIRMATIVE_REQUEST_RE = re.compile(
    r"^(?:please\s+)?(?:"
    r"(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"|(?:then\s+)?"
    r")?(?:answer|reply|respond)\b.{0,48}\bin\s+english\b",
    re.IGNORECASE,
)
_ENGLISH_NEGATED_REQUEST_RE = re.compile(
    r"\b(?:"
    r"(?:do|does|did|can|could|would|will)\s+(?:you\s+)?not"
    r"|(?:do|does|did|can|could|would|will)n['’]t"
    r"|never"
    r")\s+(?:answer|reply|respond)\b.{0,48}\bin\s+english\b",
    re.IGNORECASE,
)
_ENGLISH_META_REQUEST_RE = re.compile(
    r"^why\b.{0,48}\b(?:answer(?:ed|ing)?|repl(?:y|ied|ying)|respond(?:ed|ing)?)"
    r"\b.{0,32}\bin\s+english\b",
    re.IGNORECASE,
)
_SAFE_MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_COVERAGE_KINDS = {
    SemanticAtomKind.TOPIC,
    SemanticAtomKind.ENTITY,
    SemanticAtomKind.ACTION,
    SemanticAtomKind.MEDIA_REFERENCE,
}


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _has_explicit_english_request(value: str) -> bool:
    """Accept only an affirmative current-turn request, not negation or discussion."""

    message = re.sub(r"^/(?:chat|role)\b\s*", "", str(value or "").strip(), flags=re.I)
    for segment in re.split(r"[，,。；;！？?\n]+", message):
        text = segment.strip()
        if not text:
            continue
        if (
            _CHINESE_ENGLISH_NEGATION_RE.search(text)
            or _CHINESE_ENGLISH_META_RE.search(text)
            or _ENGLISH_NEGATED_REQUEST_RE.search(text)
            or _ENGLISH_META_REQUEST_RE.search(text)
        ):
            continue
        if (
            _CHINESE_ENGLISH_REQUEST_RE.search(text)
            or _ENGLISH_AFFIRMATIVE_REQUEST_RE.search(text)
        ):
            return True
    return False


def _dedup_atoms(atoms: list[SemanticAtom]) -> tuple[SemanticAtom, ...]:
    result: list[SemanticAtom] = []
    seen: set[tuple[SemanticAtomKind, str]] = set()
    for atom in atoms:
        key = (atom.kind, _normalized(atom.value))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(atom)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AnchorCoverage:
    matched_atom_count: int
    total_anchor_atom_count: int
    matched_kinds: tuple[SemanticAtomKind, ...]
    current_topic_supported: bool

    def trace_metadata(self) -> dict[str, int | bool]:
        return {
            "anchor_matched_atom_count": self.matched_atom_count,
            "anchor_total_atom_count": self.total_anchor_atom_count,
            "anchor_matched_kind_count": len(self.matched_kinds),
            "anchor_current_topic_supported": self.current_topic_supported,
        }


@dataclass(frozen=True, slots=True)
class CurrentQuestionAnchor:
    binding: DecisionBinding
    turn_kind: CurrentTurnKind
    semantic_atoms: tuple[SemanticAtom, ...]
    media_item_ids: tuple[str, ...]
    answer_language: str
    question_count: int
    clause_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise TypeError("anchor_binding_required")
        if not isinstance(self.turn_kind, CurrentTurnKind):
            raise TypeError("anchor_turn_kind_invalid")
        if any(not isinstance(atom, SemanticAtom) for atom in self.semantic_atoms):
            raise TypeError("anchor_semantic_atom_invalid")
        if len(set(self.media_item_ids)) != len(self.media_item_ids):
            raise ValueError("anchor_media_item_duplicate")
        if any(not _SAFE_MEDIA_ID_RE.fullmatch(value) for value in self.media_item_ids):
            raise ValueError("anchor_media_item_invalid")
        if self.answer_language not in {"zh-CN", "en"}:
            raise ValueError("anchor_answer_language_invalid")
        if self.question_count < 0 or self.clause_count < 1:
            raise ValueError("anchor_count_invalid")

    def coverage(self, candidate_text: str) -> AnchorCoverage:
        candidate = _normalized(candidate_text)
        matched = tuple(
            atom
            for atom in self.semantic_atoms
            if _normalized(atom.value) in candidate
        )
        kinds = tuple(dict.fromkeys(atom.kind for atom in matched))
        return AnchorCoverage(
            matched_atom_count=len(matched),
            total_anchor_atom_count=len(self.semantic_atoms),
            matched_kinds=kinds,
            current_topic_supported=bool(_COVERAGE_KINDS.intersection(kinds)),
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "current_turn_kind": self.turn_kind.value,
            "current_anchor_atom_count": len(self.semantic_atoms),
            "current_anchor_negation_count": sum(
                atom.kind is SemanticAtomKind.NEGATION
                for atom in self.semantic_atoms
            ),
            "current_anchor_action_count": sum(
                atom.kind is SemanticAtomKind.ACTION
                for atom in self.semantic_atoms
            ),
            "current_anchor_entity_count": sum(
                atom.kind is SemanticAtomKind.ENTITY
                for atom in self.semantic_atoms
            ),
            "current_anchor_media_count": sum(
                atom.kind is SemanticAtomKind.MEDIA_REFERENCE
                for atom in self.semantic_atoms
            ),
            "current_anchor_media_item_count": len(self.media_item_ids),
            "current_anchor_question_count": self.question_count,
            "current_anchor_clause_count": self.clause_count,
            "answer_language_code": self.answer_language,
            "current_anchor_bound": True,
        }


def build_current_question_anchor(
    binding: DecisionBinding,
    current_message: str,
    *,
    media_item_ids: tuple[str, ...] = (),
) -> CurrentQuestionAnchor:
    """Build the immutable current-turn anchor before history or memory exists."""

    if not isinstance(binding, DecisionBinding):
        raise TypeError("anchor_binding_required")
    message = str(current_message or "").strip()
    if not message:
        raise ValueError("anchor_current_message_required")
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    if digest != binding.current_content_digest:
        raise ValueError("anchor_content_binding_mismatch")

    if _REQUEST_RE.search(message):
        turn_kind = CurrentTurnKind.REQUEST
    elif _QUESTION_RE.search(message):
        turn_kind = CurrentTurnKind.QUESTION
    else:
        turn_kind = CurrentTurnKind.STATEMENT

    atoms: list[SemanticAtom] = []
    for negation in _NEGATIONS:
        if negation in message:
            atoms.append(SemanticAtom(SemanticAtomKind.NEGATION, negation))
    for action in _ACTIONS:
        if action in message:
            atoms.append(SemanticAtom(SemanticAtomKind.ACTION, action))
    for value in _ASCII_ENTITY_RE.findall(message):
        atoms.append(SemanticAtom(SemanticAtomKind.ENTITY, value))
    for value in _QUOTED_ENTITY_RE.findall(message):
        atoms.append(SemanticAtom(SemanticAtomKind.ENTITY, value.strip()))
    for pattern in _TOPIC_PATTERNS:
        for value in pattern.findall(message):
            topic = str(value or "").strip(" ：:，,。；;！？?")
            if topic:
                atoms.append(SemanticAtom(SemanticAtomKind.TOPIC, topic))
    for media_reference in _MEDIA_REFERENCES:
        if media_reference in message:
            atoms.append(
                SemanticAtom(SemanticAtomKind.MEDIA_REFERENCE, media_reference)
            )
            break

    question_marks = message.count("？") + message.count("?")
    question_count = max(
        question_marks,
        1 if _QUESTION_RE.search(message) else 0,
    )
    clauses = tuple(
        value
        for value in re.split(r"[，,。；;！？?]+", message)
        if value.strip()
    )
    return CurrentQuestionAnchor(
        binding=binding,
        turn_kind=turn_kind,
        semantic_atoms=_dedup_atoms(atoms),
        media_item_ids=tuple(media_item_ids),
        answer_language="en" if _has_explicit_english_request(message) else "zh-CN",
        question_count=question_count,
        clause_count=max(1, len(clauses)),
    )


__all__ = [
    "AnchorCoverage",
    "CurrentQuestionAnchor",
    "CurrentTurnKind",
    "build_current_question_anchor",
]
