from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .contracts import (
    ContractViolation,
    DecisionBinding,
    EvidenceConsumer,
    ExternalPluginEvidence,
    HistoryVisibility,
    PluginEvidenceKind,
    PluginEvidenceStatus,
)
from .conversation_ledger import (
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
    ledger_content_digest,
)
from .send_receipt import SegmentSendStatus, SendSegmentAttempt, SentReplyRecord


class HistoryDisposition(str, Enum):
    """Closed destinations for normalized history."""

    CHARACTER_THREAD = "character_thread"
    CURRENT_TURN_REFERENCE = "current_turn_reference"
    DROP = "drop"


class HistoryOrigin(str, Enum):
    """Closed, evidence-derived history source taxonomy."""

    SHIO_ASSISTANT = "shio_assistant"
    EXTERNAL_ASSISTANT = "external_assistant"
    LIVING_MEMORY = "livingmemory"
    ANYSEARCH = "anysearch"
    MEME_MANAGER = "meme_manager"
    PARSER = "parser"
    RENEBAN = "reneban"
    EXTERNAL_PLUGIN = "external_plugin"


_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_HISTORY_CONSUMERS = frozenset(
    {
        EvidenceConsumer.PUBLIC_SCENE,
        EvidenceConsumer.PERSONAL_MEMORY,
        EvidenceConsumer.PERSONA,
        EvidenceConsumer.LEARNING,
    }
)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ContractViolation(f"{field_name}_invalid")
    normalized = value.strip()
    if not normalized:
        raise ContractViolation(f"{field_name}_required")
    return normalized


def _reasons(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(str(value or "").strip() for value in values))
    if not normalized or any(not _REASON_RE.fullmatch(value) for value in normalized):
        raise ContractViolation("history_reason_invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class HistoryNormalizationContext:
    """The exact conversation revision and action allowed to consume history."""

    binding: DecisionBinding = field(repr=False)
    current_action_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise ContractViolation("history_binding_required")
        object.__setattr__(
            self,
            "current_action_id",
            _required_text(self.current_action_id, "current_action_id"),
        )


@dataclass(frozen=True, slots=True)
class PluginHistoryCandidate:
    """Typed plugin/tool history offered to the normalizer.

    The evidence owns scope and sender provenance.  Tool coordinates are
    additionally bound to the one action that produced the result.  Message
    text is carried only inside ``record`` and is never emitted by trace data.
    """

    evidence: ExternalPluginEvidence = field(repr=False)
    record: LedgerRecord | None = field(default=None, repr=False)
    action_id: str = field(default="", repr=False)
    tool_name: str = field(default="", repr=False)
    tool_call_id: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, ExternalPluginEvidence):
            raise ContractViolation("plugin_history_evidence_required")
        if self.record is not None and not isinstance(self.record, LedgerRecord):
            raise ContractViolation("plugin_history_record_invalid")
        for name in ("action_id", "tool_name", "tool_call_id"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ContractViolation(f"{name}_invalid")
            object.__setattr__(self, name, value.strip())


@dataclass(frozen=True, slots=True)
class NormalizedHistoryItem:
    """One safe history decision.

    Dropped inputs never retain content.  Accepted items can feed only the
    current-turn consumer; Scene, memory, Persona and Learning are excluded at
    this boundary rather than left to prompt instructions.
    """

    origin: HistoryOrigin
    disposition: HistoryDisposition
    history_visibility: HistoryVisibility
    allowed_consumers: tuple[EvidenceConsumer, ...] = ()
    content: str = field(default="", repr=False)
    content_digest: str = ""
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.origin, HistoryOrigin):
            raise ContractViolation("history_origin_invalid")
        if not isinstance(self.disposition, HistoryDisposition):
            raise ContractViolation("history_disposition_invalid")
        if not isinstance(self.history_visibility, HistoryVisibility):
            raise ContractViolation("history_visibility_invalid")
        consumers = tuple(dict.fromkeys(self.allowed_consumers))
        if any(not isinstance(value, EvidenceConsumer) for value in consumers):
            raise ContractViolation("history_consumer_invalid")
        if _FORBIDDEN_HISTORY_CONSUMERS.intersection(consumers):
            raise ContractViolation("history_consumer_forbidden")
        object.__setattr__(self, "allowed_consumers", consumers)
        object.__setattr__(self, "reason_codes", _reasons(self.reason_codes))

        content = str(self.content or "")
        object.__setattr__(self, "content", content)
        if self.disposition is HistoryDisposition.DROP:
            if (
                self.history_visibility is not HistoryVisibility.DROP
                or consumers
                or content
                or self.content_digest
            ):
                raise ContractViolation("dropped_history_must_be_empty")
            return

        expected_visibility = {
            HistoryDisposition.CHARACTER_THREAD: HistoryVisibility.PERSONAL_REFERENCE,
            HistoryDisposition.CURRENT_TURN_REFERENCE: (
                HistoryVisibility.CURRENT_TURN_REFERENCE
            ),
        }[self.disposition]
        if self.history_visibility is not expected_visibility:
            raise ContractViolation("history_destination_visibility_mismatch")
        if consumers != (EvidenceConsumer.CURRENT_TURN,):
            raise ContractViolation("history_current_turn_consumer_required")
        if not content:
            raise ContractViolation("normalized_history_content_required")
        digest = ledger_content_digest(content)
        if self.content_digest != digest:
            raise ContractViolation("normalized_history_digest_mismatch")

    def can_feed(self, consumer: EvidenceConsumer) -> bool:
        return (
            self.disposition is not HistoryDisposition.DROP
            and consumer in self.allowed_consumers
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        """Return taxonomy and counts only; never content or source IDs."""

        return {
            "history_origin": self.origin.value,
            "history_disposition": self.disposition.value,
            "history_visibility": self.history_visibility.value,
            "history_consumer_count": len(self.allowed_consumers),
            "history_has_content": bool(self.content),
            "history_reason": self.reason_codes[0],
            "history_reason_count": len(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class _PluginHistoryPolicy:
    origin: HistoryOrigin
    evidence_kind: PluginEvidenceKind
    history_visibility: HistoryVisibility
    allowed_consumers: frozenset[EvidenceConsumer]
    current_turn_reference: bool


_PLUGIN_POLICIES: Mapping[str, _PluginHistoryPolicy] = MappingProxyType(
    {
        "livingmemory": _PluginHistoryPolicy(
            origin=HistoryOrigin.LIVING_MEMORY,
            evidence_kind=PluginEvidenceKind.MEMORY_REFERENCE,
            history_visibility=HistoryVisibility.CURRENT_TURN_REFERENCE,
            allowed_consumers=frozenset({EvidenceConsumer.CURRENT_TURN}),
            current_turn_reference=True,
        ),
        "anysearch": _PluginHistoryPolicy(
            origin=HistoryOrigin.ANYSEARCH,
            evidence_kind=PluginEvidenceKind.GROUNDING_FACT,
            history_visibility=HistoryVisibility.CURRENT_TURN_REFERENCE,
            allowed_consumers=frozenset({EvidenceConsumer.CURRENT_TURN}),
            current_turn_reference=True,
        ),
        "meme_manager": _PluginHistoryPolicy(
            origin=HistoryOrigin.MEME_MANAGER,
            evidence_kind=PluginEvidenceKind.PRESENTATION_EFFECT,
            history_visibility=HistoryVisibility.DROP,
            allowed_consumers=frozenset({EvidenceConsumer.PRESENTATION}),
            current_turn_reference=False,
        ),
        "parser": _PluginHistoryPolicy(
            origin=HistoryOrigin.PARSER,
            evidence_kind=PluginEvidenceKind.EXTERNAL_OUTPUT,
            history_visibility=HistoryVisibility.DROP,
            allowed_consumers=frozenset(),
            current_turn_reference=False,
        ),
        "reneban": _PluginHistoryPolicy(
            origin=HistoryOrigin.RENEBAN,
            evidence_kind=PluginEvidenceKind.GATE_DECISION,
            history_visibility=HistoryVisibility.DROP,
            allowed_consumers=frozenset({EvidenceConsumer.INGRESS}),
            current_turn_reference=False,
        ),
    }
)


def _drop(origin: HistoryOrigin, *reasons: str) -> NormalizedHistoryItem:
    return NormalizedHistoryItem(
        origin=origin,
        disposition=HistoryDisposition.DROP,
        history_visibility=HistoryVisibility.DROP,
        reason_codes=tuple(reasons),
    )


def _accepted(
    *,
    origin: HistoryOrigin,
    disposition: HistoryDisposition,
    content: str,
    reason: str,
) -> NormalizedHistoryItem:
    visibility = {
        HistoryDisposition.CHARACTER_THREAD: HistoryVisibility.PERSONAL_REFERENCE,
        HistoryDisposition.CURRENT_TURN_REFERENCE: (
            HistoryVisibility.CURRENT_TURN_REFERENCE
        ),
    }[disposition]
    return NormalizedHistoryItem(
        origin=origin,
        disposition=disposition,
        history_visibility=visibility,
        allowed_consumers=(EvidenceConsumer.CURRENT_TURN,),
        content=content,
        content_digest=ledger_content_digest(content),
        reason_codes=(reason,),
    )


def _receipt_segments(
    receipts: Iterable[SentReplyRecord],
) -> dict[str, list[tuple[SentReplyRecord, SendSegmentAttempt]]]:
    index: dict[str, list[tuple[SentReplyRecord, SendSegmentAttempt]]] = {}
    for receipt in receipts or ():
        if not isinstance(receipt, SentReplyRecord):
            continue
        for segment in receipt.segments:
            if isinstance(segment, SendSegmentAttempt) and segment.segment_id:
                index.setdefault(segment.segment_id, []).append((receipt, segment))
    return index


def _normalize_assistant_record(
    record: LedgerRecord,
    *,
    segment_matches: list[tuple[SentReplyRecord, SendSegmentAttempt]],
    context: HistoryNormalizationContext,
) -> NormalizedHistoryItem:
    external = HistoryOrigin.EXTERNAL_ASSISTANT
    if (
        record.source_kind is not LedgerSourceKind.OUTBOUND
        or record.attribution_status != "verified"
        or not record.sender_key
        or not record.target_sender_key
        or not record.message_id
        or record.source_id != record.message_id
    ):
        return _drop(external, "assistant_source_unverified")
    if (
        record.scope_key != context.binding.scope_key
        or record.session_id != context.binding.session_id
    ):
        return _drop(external, "assistant_scope_mismatch")
    if record.target_sender_key != context.binding.current_sender_key:
        return _drop(external, "assistant_target_mismatch")
    if not segment_matches:
        return _drop(external, "assistant_receipt_missing")
    if len(segment_matches) != 1:
        return _drop(external, "assistant_receipt_ambiguous")

    receipt, segment = segment_matches[0]
    if (
        receipt.scope_key != record.scope_key
        or receipt.session_id != record.session_id
        or receipt.target_sender_key != record.target_sender_key
        or receipt.target_message_id != record.reply_to_message_id
        or not receipt.target_source_kind
        or receipt.reply_id != segment.internal_reply_id
        or segment.segment_id != record.message_id
    ):
        return _drop(external, "assistant_receipt_binding_mismatch")
    if segment.status is SegmentSendStatus.FAILED:
        return _drop(external, "assistant_receipt_failed")
    if segment.status is not SegmentSendStatus.SUCCEEDED or not receipt.is_terminal:
        return _drop(external, "assistant_receipt_incomplete")
    if receipt.sent_at <= 0 or segment.completed_at <= 0:
        return _drop(external, "assistant_receipt_time_invalid")
    if (
        not segment.visible_text
        or segment.visible_text != record.content
        or segment.visible_text_digest != record.content_digest
        or ledger_content_digest(record.content) != record.content_digest
    ):
        return _drop(external, "assistant_content_binding_mismatch")

    return _accepted(
        origin=HistoryOrigin.SHIO_ASSISTANT,
        disposition=HistoryDisposition.CHARACTER_THREAD,
        content=record.content,
        reason="assistant_receipt_verified",
    )


def normalize_assistant_history(
    records: Iterable[LedgerRecord],
    *,
    receipts: Iterable[SentReplyRecord],
    context: HistoryNormalizationContext,
) -> tuple[NormalizedHistoryItem, ...]:
    """Normalize assistant records without ever inspecting adjacent users."""

    if not isinstance(context, HistoryNormalizationContext):
        raise ContractViolation("history_normalization_context_required")
    receipt_index = _receipt_segments(receipts)
    normalized: list[NormalizedHistoryItem] = []
    for record in records or ():
        if not isinstance(record, LedgerRecord) or record.role is not LedgerRole.ASSISTANT:
            continue
        normalized.append(
            _normalize_assistant_record(
                record,
                segment_matches=receipt_index.get(record.message_id, []),
                context=context,
            )
        )
    return tuple(normalized)


def _plugin_contract_matches(
    evidence: ExternalPluginEvidence,
    policy: _PluginHistoryPolicy,
) -> bool:
    return (
        evidence.evidence_kind is policy.evidence_kind
        and evidence.history_visibility is policy.history_visibility
        and set(evidence.allowed_consumers).issubset(policy.allowed_consumers)
    )


def normalize_plugin_history(
    candidate: PluginHistoryCandidate,
    *,
    context: HistoryNormalizationContext,
) -> NormalizedHistoryItem:
    """Normalize one typed plugin/tool item against the closed P1 contracts."""

    if not isinstance(candidate, PluginHistoryCandidate):
        raise ContractViolation("plugin_history_candidate_required")
    if not isinstance(context, HistoryNormalizationContext):
        raise ContractViolation("history_normalization_context_required")

    evidence = candidate.evidence
    policy = _PLUGIN_POLICIES.get(evidence.plugin_id)
    if policy is None:
        return _drop(HistoryOrigin.EXTERNAL_PLUGIN, "external_plugin_history_forbidden")
    if evidence.status is not PluginEvidenceStatus.VERIFIED or not evidence.trusted:
        return _drop(policy.origin, "plugin_evidence_unverified")
    if not _plugin_contract_matches(evidence, policy):
        return _drop(policy.origin, "plugin_contract_mismatch")
    if not policy.current_turn_reference:
        return _drop(policy.origin, "plugin_history_forbidden")
    if EvidenceConsumer.CURRENT_TURN not in evidence.allowed_consumers:
        return _drop(policy.origin, "plugin_current_turn_consumer_missing")
    if evidence.binding != context.binding:
        return _drop(policy.origin, "plugin_turn_binding_mismatch")
    if evidence.target_message_id != context.binding.current_message_id:
        return _drop(policy.origin, "plugin_target_binding_missing")
    if not candidate.action_id or candidate.action_id != context.current_action_id:
        return _drop(policy.origin, "plugin_action_binding_mismatch")

    record = candidate.record
    if (
        record is None
        or record.role is not LedgerRole.TOOL
        or record.source_kind is not LedgerSourceKind.TOOL_RESULT
        or record.attribution_status != "verified"
    ):
        return _drop(policy.origin, "plugin_tool_record_unverified")
    if (
        record.scope_key != context.binding.scope_key
        or record.session_id != context.binding.session_id
    ):
        return _drop(policy.origin, "plugin_tool_scope_mismatch")
    if not candidate.tool_name or not candidate.tool_call_id:
        return _drop(policy.origin, "plugin_tool_coordinate_missing")
    expected_source_id = f"{candidate.tool_name}:{candidate.tool_call_id}"
    if record.source_id != expected_source_id:
        return _drop(policy.origin, "plugin_tool_coordinate_mismatch")
    if not record.content or ledger_content_digest(record.content) != record.content_digest:
        return _drop(policy.origin, "plugin_tool_content_invalid")

    return _accepted(
        origin=policy.origin,
        disposition=HistoryDisposition.CURRENT_TURN_REFERENCE,
        content=record.content,
        reason="plugin_current_turn_reference_verified",
    )


__all__ = [
    "HistoryDisposition",
    "HistoryNormalizationContext",
    "HistoryOrigin",
    "NormalizedHistoryItem",
    "PluginHistoryCandidate",
    "normalize_assistant_history",
    "normalize_plugin_history",
]
