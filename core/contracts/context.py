from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..capability_policy import CapabilityClass
from ..context_assembler import ProvenancedFact, ReplyTarget
from ._validation import (
    ContractViolation,
    normalize_reason_codes,
    require_hex,
    require_nonnegative,
    require_safe_name,
    require_score,
    require_text,
)
from .behavior import _validate_target
from .binding import DecisionBinding


class MemoryMode(str, Enum):
    SKIP = "skip"
    RECENT_ONLY = "recent_only"
    SEMANTIC_RECALL = "semantic_recall"
    DEGRADED = "degraded"


_UNSAFE_MEMORY_SOURCES = {
    "banned",
    "known_bot",
    "plugin_echo",
    "external_plugin_output",
    "meme_query",
    "parser_output",
    "management_output",
}


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    binding: DecisionBinding
    mode: MemoryMode
    current_account_key: str = ""
    selected_facts: tuple[ProvenancedFact, ...] = ()
    max_results: int = 0
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        if not isinstance(self.max_results, int) or not 0 <= self.max_results <= 20:
            raise ContractViolation("memory_max_results_invalid")
        if self.mode in {MemoryMode.SKIP, MemoryMode.DEGRADED}:
            if self.max_results or self.selected_facts:
                raise ContractViolation("inactive_memory_must_be_empty")
        elif self.max_results < 1:
            raise ContractViolation("active_memory_budget_required")
        for fact in self.selected_facts:
            source = str(fact.source_kind or "").strip().lower()
            if source in _UNSAFE_MEMORY_SOURCES:
                raise ContractViolation("unsafe_memory_source_selected")
            if fact.subject_key and (
                not self.current_account_key or fact.subject_key != self.current_account_key
            ):
                raise ContractViolation("other_subject_memory_selected")
            if fact.subject_key and fact.scope not in {"personal", "owner_private"}:
                raise ContractViolation("subject_memory_scope_invalid")
            if not fact.subject_key and fact.scope not in {
                "group",
                "global",
                "public",
                "group_background",
            }:
                raise ContractViolation("unscoped_memory_selected")

    def trace_metadata(self) -> dict[str, str | int]:
        return {
            "memory_mode": self.mode.value,
            "memory_fact_count": len(self.selected_facts),
            "memory_max_results": self.max_results,
            "memory_reason_count": len(self.reason_codes),
        }


class KnowledgeNeed(str, Enum):
    NONE = "none"
    UNKNOWN_TERM = "unknown_term"
    TIME_SENSITIVE = "time_sensitive"
    EXPLICIT_VERIFY = "explicit_verify"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


@dataclass(frozen=True, slots=True)
class KnowledgeGapDecision:
    binding: DecisionBinding
    need: KnowledgeNeed
    requires_evidence: bool
    requested_capability: CapabilityClass | None = None
    max_tool_calls: int = 0
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        if not isinstance(self.max_tool_calls, int) or not 0 <= self.max_tool_calls <= 2:
            raise ContractViolation("knowledge_tool_budget_invalid")
        if self.need is KnowledgeNeed.NONE:
            if self.requires_evidence or self.requested_capability is not None or self.max_tool_calls:
                raise ContractViolation("no_knowledge_gap_must_not_request_tool")
        else:
            if not self.requires_evidence:
                raise ContractViolation("knowledge_evidence_required")
            if self.requested_capability not in {
                CapabilityClass.PUBLIC_WEB_READ,
                CapabilityClass.CHAT_RETRIEVAL,
            }:
                raise ContractViolation("knowledge_capability_invalid")
            if self.max_tool_calls < 1:
                raise ContractViolation("knowledge_tool_budget_required")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "knowledge_need": self.need.value,
            "knowledge_evidence_required": self.requires_evidence,
            "knowledge_tool_budget": self.max_tool_calls,
            "knowledge_reason_count": len(self.reason_codes),
        }


class MediaKind(str, Enum):
    IMAGE = "image"
    AUDIO = "audio"


class MediaOrigin(str, Enum):
    DIRECT = "direct"
    QUOTED = "quoted"
    QUOTED_FALLBACK = "quoted_fallback"


class MediaAvailability(str, Enum):
    RAW_MEDIA = "raw_media"
    NATIVE_CAPTION = "native_caption"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MediaItem:
    item_id: str
    kind: MediaKind
    origin: MediaOrigin
    provider_index: int
    source_message_id: str
    source_sender_key: str
    availability: MediaAvailability
    reference_message_id: str = ""
    reference_sender_key: str = ""
    native_caption_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", require_safe_name(self.item_id, "media_item_id"))
        if not isinstance(self.provider_index, int) or self.provider_index < 0:
            raise ContractViolation("media_provider_index_invalid")
        object.__setattr__(self, "source_message_id", require_text(self.source_message_id, "media_source_message_id"))
        object.__setattr__(self, "source_sender_key", require_text(self.source_sender_key, "media_source_sender_key"))
        if self.origin in {MediaOrigin.QUOTED, MediaOrigin.QUOTED_FALLBACK}:
            if not self.reference_message_id or not self.reference_sender_key:
                raise ContractViolation("quoted_media_reference_required")
        elif self.reference_message_id or self.reference_sender_key:
            raise ContractViolation("direct_media_reference_forbidden")
        if self.availability is MediaAvailability.NATIVE_CAPTION:
            object.__setattr__(
                self,
                "native_caption_digest",
                require_hex(self.native_caption_digest, "native_caption_digest", lengths=(64,)),
            )
        elif self.native_caption_digest:
            raise ContractViolation("caption_digest_without_caption")


@dataclass(frozen=True, slots=True)
class MediaContext:
    binding: DecisionBinding
    items: tuple[MediaItem, ...] = ()
    degradation_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "degradation_reasons", normalize_reason_codes(self.degradation_reasons))
        item_ids = [item.item_id for item in self.items]
        indexes = [item.provider_index for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ContractViolation("media_item_id_duplicate")
        if len(set(indexes)) != len(indexes):
            raise ContractViolation("media_provider_index_duplicate")
        for item in self.items:
            if item.origin is MediaOrigin.DIRECT:
                if (
                    item.source_message_id != self.binding.current_message_id
                    or item.source_sender_key != self.binding.current_sender_key
                ):
                    raise ContractViolation("direct_media_binding_mismatch")
            else:
                if (
                    item.source_message_id != item.reference_message_id
                    or item.source_sender_key != item.reference_sender_key
                ):
                    raise ContractViolation("quoted_media_binding_mismatch")

    def for_repair(self) -> "MediaContext":
        return self

    def trace_metadata(self) -> dict[str, int | bool]:
        return {
            "media_count": len(self.items),
            "media_direct_count": sum(item.origin is MediaOrigin.DIRECT for item in self.items),
            "media_quoted_count": sum(item.origin is not MediaOrigin.DIRECT for item in self.items),
            "media_caption_count": sum(item.availability is MediaAvailability.NATIVE_CAPTION for item in self.items),
            "media_unavailable_count": sum(item.availability is MediaAvailability.UNAVAILABLE for item in self.items),
            "media_degraded": bool(self.degradation_reasons),
        }


@dataclass(frozen=True, slots=True)
class GroundingFact:
    binding: DecisionBinding
    fact_id: str
    claim: str
    source_kind: str
    source_digest: str
    confidence: float
    observed_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_id", require_safe_name(self.fact_id, "grounding_fact_id"))
        object.__setattr__(self, "claim", require_text(self.claim, "grounding_claim"))
        object.__setattr__(self, "source_kind", require_safe_name(self.source_kind, "grounding_source_kind"))
        object.__setattr__(self, "source_digest", require_hex(self.source_digest, "grounding_source_digest", lengths=(64,)))
        object.__setattr__(self, "confidence", require_score(self.confidence, "grounding_confidence"))
        object.__setattr__(self, "observed_at", require_nonnegative(self.observed_at, "grounding_observed_at"))


class ContentIntentKind(str, Enum):
    ANSWER = "answer"
    ACKNOWLEDGE = "acknowledge"
    CLARIFY = "clarify"
    SOCIAL_RESPONSE = "social_response"
    REFUSE = "refuse"
    CORRECT = "correct"


class SemanticAtomKind(str, Enum):
    TOPIC = "topic"
    ENTITY = "entity"
    NEGATION = "negation"
    ACTION = "action"
    FACT = "fact"
    MEDIA_REFERENCE = "media_reference"
    SOCIAL_MOVE = "social_move"


@dataclass(frozen=True, slots=True)
class SemanticAtom:
    kind: SemanticAtomKind
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", require_text(self.value, "semantic_atom_value"))


@dataclass(frozen=True, slots=True)
class ContentIntent:
    binding: DecisionBinding
    reply_target: ReplyTarget
    kind: ContentIntentKind
    required_atoms: tuple[SemanticAtom, ...] = ()
    forbidden_atoms: tuple[SemanticAtom, ...] = ()
    required_media_item_ids: tuple[str, ...] = ()
    grounding_facts: tuple[GroundingFact, ...] = ()
    answer_language: str = "zh-CN"
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_target(self.binding, self.reply_target)
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        object.__setattr__(self, "answer_language", require_text(self.answer_language, "answer_language"))
        required = {(atom.kind, atom.value) for atom in self.required_atoms}
        forbidden = {(atom.kind, atom.value) for atom in self.forbidden_atoms}
        if required.intersection(forbidden):
            raise ContractViolation("semantic_atom_required_and_forbidden")
        if len(set(self.required_media_item_ids)) != len(self.required_media_item_ids):
            raise ContractViolation("required_media_item_duplicate")
        if any(type(fact) is not GroundingFact for fact in self.grounding_facts):
            raise ContractViolation("grounding_fact_type_invalid")
        if any(fact.binding != self.binding for fact in self.grounding_facts):
            raise ContractViolation("grounding_fact_binding_mismatch")

    def trace_metadata(self) -> dict[str, str | int]:
        return {
            "content_intent_kind": self.kind.value,
            "required_semantic_count": len(self.required_atoms),
            "forbidden_semantic_count": len(self.forbidden_atoms),
            "required_media_count": len(self.required_media_item_ids),
            "grounding_fact_count": len(self.grounding_facts),
            "answer_language": self.answer_language,
            "content_reason_count": len(self.reason_codes),
        }
