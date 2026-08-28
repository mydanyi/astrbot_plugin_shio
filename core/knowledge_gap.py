from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .capability_policy import CapabilityClass
from .content_intent_builder import ContentIntentSeed
from .contracts import KnowledgeGapDecision, KnowledgeNeed
from .contracts._validation import normalize_reason_codes, require_score


class KnowledgeGapError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProactiveKnowledgeDecision:
    capability: CapabilityClass | None
    reason_code: str

    @property
    def requires_evidence(self) -> bool:
        return self.capability is not None


_DECLINE_EXTERNAL_RE = re.compile(
    r"(?:不要|别|不用|无需|不必).{0,10}(?:联网|上网|搜索|检索|查证|核实|查资料|查一下)"
)
_EXPLICIT_VERIFY_RE = re.compile(
    r"(?:请(?:你)?|帮我|麻烦(?:你)?|劳驾(?:你)?|你(?:能不能|可以)?)"
    r"(?:联网|上网)?(?:帮我)?"
    r"(?:查证|核实|验证|搜索一下|搜一下|查一下|查查|查资料)"
    r"|(?:联网|上网).{0,6}"
    r"(?:查证|核实|验证|搜索一下|搜一下|查一下|查查|查资料)"
    r"|(?:^|[，,。；;！？!?\s])"
    r"(?:查证|核实|验证|搜索一下|搜一下|查一下|查查|查资料)"
    r"|(?:给出|提供|附上).{0,10}(?:来源|出处|链接)"
)
_SLANG_RE = re.compile(
    r"(?:新梗|热梗|网络梗|什么梗|网络用语|黑话|圈内话|俚语|缩写(?:是什么意思|指什么)?)",
    re.IGNORECASE,
)
_TIME_SENSITIVE_RE = re.compile(
    r"(?:最新|实时|现任|刚刚(?:发布|公布|发生|更新))"
    r"|(?:今天|今日|现在|目前|当前|本周|本月|今年).{0,24}"
    r"(?:价格|汇率|天气|比分|股价|油价|新闻|政策|规定|版本|赛程|发布日期|谁|多少)"
    r"|(?:价格|汇率|天气|比分|股价|油价|新闻|政策|规定|版本|赛程|发布日期).{0,24}"
    r"(?:今天|今日|现在|目前|当前|本周|本月|今年)",
    re.IGNORECASE,
)
_KNOWLEDGE_QUESTION_RE = re.compile(
    r"(?:你|妳|您)?(?:知道|了解|听说过)(?![吗嘛么])[^?？\n]{1,48}(?:吗|嘛|么)[?？]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True, repr=False)
class KnowledgeGapSuggestion:
    """Non-authoritative hint for an ambiguous unfamiliar term."""

    need: KnowledgeNeed
    confidence: float
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.need not in {KnowledgeNeed.NONE, KnowledgeNeed.UNKNOWN_TERM}:
            raise ValueError("knowledge_suggestion_need_not_soft")
        object.__setattr__(
            self,
            "confidence",
            require_score(self.confidence, "knowledge_suggestion_confidence"),
        )
        object.__setattr__(
            self,
            "reason_codes",
            normalize_reason_codes(self.reason_codes),
        )

    def trace_metadata(self) -> dict[str, str | float | int]:
        return {
            "knowledge_suggestion_need": self.need.value,
            "knowledge_suggestion_confidence": round(self.confidence, 3),
            "knowledge_suggestion_reason_count": len(self.reason_codes),
        }

    def __repr__(self) -> str:
        return (
            "KnowledgeGapSuggestion("
            f"need={self.need.value!r}, "
            f"confidence={self.confidence:.3f}, "
            f"reasons={len(self.reason_codes)})"
        )


def _decision(
    seed: ContentIntentSeed,
    need: KnowledgeNeed,
    *reason_codes: str,
    capability: CapabilityClass = CapabilityClass.PUBLIC_WEB_READ,
) -> KnowledgeGapDecision:
    if need is KnowledgeNeed.NONE:
        return KnowledgeGapDecision(
            binding=seed.binding,
            need=need,
            requires_evidence=False,
            requested_capability=None,
            max_tool_calls=0,
            reason_codes=tuple(reason_codes),
        )
    return KnowledgeGapDecision(
        binding=seed.binding,
        need=need,
        requires_evidence=True,
        requested_capability=capability,
        max_tool_calls=1,
        reason_codes=tuple(reason_codes),
    )


def decide_knowledge_gap(
    *,
    content_seed: ContentIntentSeed,
    current_message: str,
    conflicting_evidence: bool = False,
    suggestion: KnowledgeGapSuggestion | None = None,
) -> KnowledgeGapDecision:
    """Decide evidence need without selecting a tool, source, or arguments."""

    if not isinstance(content_seed, ContentIntentSeed):
        raise TypeError("content_intent_seed_required")
    message = str(current_message or "").strip()
    if not message:
        raise KnowledgeGapError("knowledge_current_message_required")
    if hashlib.sha256(message.encode("utf-8")).hexdigest() != (
        content_seed.binding.current_content_digest
    ):
        raise KnowledgeGapError("knowledge_current_message_binding_mismatch")
    if not isinstance(conflicting_evidence, bool):
        raise TypeError("conflicting_evidence_must_be_bool")
    if suggestion is not None and not isinstance(suggestion, KnowledgeGapSuggestion):
        raise TypeError("knowledge_gap_suggestion_invalid")

    # A current, explicit refusal to use external evidence is authoritative.
    if _DECLINE_EXTERNAL_RE.search(message):
        return _decision(
            content_seed,
            KnowledgeNeed.NONE,
            "external_evidence_declined",
        )

    # The remaining hard needs cannot be reduced by a soft hint.
    if conflicting_evidence:
        return _decision(
            content_seed,
            KnowledgeNeed.CONFLICTING_EVIDENCE,
            "conflicting_evidence_observed",
        )
    if _EXPLICIT_VERIFY_RE.search(message):
        return _decision(
            content_seed,
            KnowledgeNeed.EXPLICIT_VERIFY,
            "explicit_verification_requested",
        )
    if _SLANG_RE.search(message):
        return _decision(
            content_seed,
            KnowledgeNeed.UNKNOWN_TERM,
            "unknown_term_marker_present",
            capability=CapabilityClass.CHAT_RETRIEVAL,
        )
    if (
        content_seed.intent.kind.value == "answer"
        and _TIME_SENSITIVE_RE.search(message)
    ):
        return _decision(
            content_seed,
            KnowledgeNeed.TIME_SENSITIVE,
            "time_sensitive_fact_requested",
        )
    if _KNOWLEDGE_QUESTION_RE.search(message):
        return _decision(
            content_seed,
            KnowledgeNeed.UNKNOWN_TERM,
            "knowledge_base_question_present",
            capability=CapabilityClass.CHAT_RETRIEVAL,
        )
    if (
        suggestion is not None
        and suggestion.need is KnowledgeNeed.UNKNOWN_TERM
        and suggestion.confidence >= 0.75
    ):
        return _decision(
            content_seed,
            KnowledgeNeed.UNKNOWN_TERM,
            "soft_unknown_term_accepted",
            *suggestion.reason_codes,
            capability=CapabilityClass.CHAT_RETRIEVAL,
        )
    return _decision(content_seed, KnowledgeNeed.NONE, "no_external_evidence_needed")


def decide_proactive_knowledge_need(topic_text: str) -> ProactiveKnowledgeDecision:
    """Classify one public topic without inventing a query or a user principal."""

    message = str(topic_text or "").strip()
    if not message:
        raise KnowledgeGapError("proactive_knowledge_topic_required")
    if _DECLINE_EXTERNAL_RE.search(message):
        return ProactiveKnowledgeDecision(None, "external_evidence_declined")
    if _TIME_SENSITIVE_RE.search(message) or _EXPLICIT_VERIFY_RE.search(message):
        return ProactiveKnowledgeDecision(
            CapabilityClass.PUBLIC_WEB_READ,
            "proactive_public_verification_needed",
        )
    if _SLANG_RE.search(message) or _KNOWLEDGE_QUESTION_RE.search(message):
        return ProactiveKnowledgeDecision(
            CapabilityClass.CHAT_RETRIEVAL,
            "proactive_stable_knowledge_needed",
        )
    return ProactiveKnowledgeDecision(None, "proactive_no_external_evidence_needed")


__all__ = [
    "KnowledgeGapError",
    "KnowledgeGapSuggestion",
    "ProactiveKnowledgeDecision",
    "decide_knowledge_gap",
    "decide_proactive_knowledge_need",
]
