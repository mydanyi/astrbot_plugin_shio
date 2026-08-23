from __future__ import annotations

import hashlib
import math
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnContext,
    AcceptedTurnDispatch,
    AcceptedTurnTicket,
)
from .contracts import (
    ContractViolation,
    DecisionBinding,
    SenderKind,
)
from .contracts._validation import require_text
from .conversation_event import ConversationRevisionBook, PluginSource
from .identity import build_scope_key, build_sender_key
from .send_receipt import (
    InternalSendReceiptLedger,
    PlatformReceiptSource,
    SendSegmentAttempt,
    SegmentSendStatus,
    SentReplyRecord,
)


AFFECT_DECAY_INTERVAL_S = 300.0
MAX_AFFECT_INTENSITY = 0.85
MAX_AFFECT_AROUSAL = 0.95
MAX_ABS_VALENCE = 0.95
DEFAULT_AFFECT_INERTIA = 0.65
MIN_AFFECT_INERTIA = 0.05
MAX_AFFECT_INERTIA = 0.90
MODEL_APPRAISAL_MIN_CONFIDENCE = 0.60
_VALUE_EPSILON = 1e-6
_ADMISSION_EVIDENCE_SEAL = object()
_AFFECT_RENDER_CONTEXT_SEAL = object()


class AffectAppraisalKind(str, Enum):
    NEUTRAL_FACT = "neutral_fact"
    POSITIVE_SOCIAL = "positive_social"
    NEGATIVE_SOCIAL = "negative_social"
    PLAYFUL = "playful"
    CARE = "care"
    REPAIR = "repair"


class AffectCause(str, Enum):
    BASELINE = "baseline"
    POSITIVE_SOCIAL = "positive_social"
    NEGATIVE_SOCIAL = "negative_social"
    PLAYFUL = "playful"
    CARE = "care"
    REPAIR = "repair"
    SHIO_OUTBOUND_SETTLE = "shio_outbound_settle"


class AffectMutationStatus(str, Enum):
    ACCEPTED_HUMAN = "accepted_human"
    ACCEPTED_SHIO_OUTBOUND = "accepted_shio_outbound"
    REJECTED = "rejected"


class AffectRejectReason(str, Enum):
    BINDING_MISMATCH = "binding_mismatch"
    INGRESS_NOT_ACCEPTED = "ingress_not_accepted"
    EVENT_SOURCE_UNVERIFIED = "event_source_unverified"
    REVISION_STALE = "revision_stale"
    REVISION_GAP = "revision_gap"
    CLOCK_STALE = "clock_stale"
    OUTBOUND_SOURCE_UNVERIFIED = "outbound_source_unverified"
    RECEIPT_NOT_SUCCESSFUL = "receipt_not_successful"
    OUTBOUND_TARGET_MISMATCH = "outbound_target_mismatch"
    OUTBOUND_STALE = "outbound_stale"
    OUTBOUND_DUPLICATE = "outbound_duplicate"


_MODEL_ALLOWED_KEYS = frozenset({"appraisal_hint", "confidence"})


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ContractViolation(f"{field_name}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(f"{field_name}_invalid") from exc
    if not math.isfinite(parsed):
        raise ContractViolation(f"{field_name}_invalid")
    return parsed


def _clock(value: Any, field_name: str = "affect_clock") -> float:
    parsed = _finite_number(value, field_name)
    if parsed < 0:
        raise ContractViolation(f"{field_name}_invalid")
    return parsed


def _bounded(
    value: Any,
    field_name: str,
    *,
    lower: float,
    upper: float,
) -> float:
    parsed = _finite_number(value, field_name)
    if parsed < lower or parsed > upper:
        raise ContractViolation(f"{field_name}_out_of_range")
    return parsed


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractViolation(f"{field_name}_invalid")
    return value


def _positive_limit(value: Any, field_name: str) -> int:
    return _positive_int(value, field_name)


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _zero_small(value: float) -> float:
    return 0.0 if abs(value) <= _VALUE_EPSILON else value


def _content_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _positive_finite(value: Any) -> float | None:
    try:
        parsed = _finite_number(value, "affect_receipt_time")
    except ContractViolation:
        return None
    return parsed if parsed > 0 else None


def _subject_key(context: AcceptedTurnContext) -> str:
    scope = require_text(context.envelope.scope_key, "affect_scope_key")
    bot_id = require_text(context.envelope.bot_id, "affect_bot_id")
    return f"{scope}|agent:{bot_id}"


def _context_identity_verified(context: AcceptedTurnContext) -> bool:
    if type(context) is not AcceptedTurnContext:
        return False
    envelope = context.envelope
    principal = context.principal
    expected_scope = build_scope_key(
        platform_id=envelope.platform_id,
        bot_id=envelope.bot_id,
        chat_type=envelope.chat_type,
        group_id=envelope.group_id,
        session_id=envelope.session_id,
    )
    expected_sender = build_sender_key(expected_scope, envelope.sender_id)
    return bool(
        expected_scope
        and expected_sender
        and envelope.scope_key == expected_scope
        and envelope.sender_key == expected_sender
        and principal.sender_key == expected_sender
        and principal.sender_id == envelope.sender_id
        and context.binding.scope_key == expected_scope
        and context.binding.session_id == envelope.session_id
        and context.binding.current_message_id == envelope.message_id
        and context.binding.current_sender_key == expected_sender
        and context.binding.current_content_digest == context.content_digest
        and context.sender_kind is SenderKind.HUMAN
        and context.plugin_source is PluginSource.NONE
        and envelope.source_kind == "inbound"
        and not envelope.is_degraded
        and principal.verification_source.startswith("astrbot_event_sender_id:")
    )


@dataclass(frozen=True, slots=True)
class ModelAffectAppraisal:
    appraisal_hint: AffectAppraisalKind
    confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.appraisal_hint, AffectAppraisalKind):
            raise ContractViolation("affect_appraisal_hint_invalid")
        object.__setattr__(
            self,
            "confidence",
            _bounded(
                self.confidence,
                "affect_appraisal_confidence",
                lower=0.0,
                upper=1.0,
            ),
        )

    def trace_metadata(self) -> dict[str, str | float]:
        return {
            "affect_appraisal_hint": self.appraisal_hint.value,
            "affect_appraisal_confidence": self.confidence,
        }


def parse_model_affect_appraisal(
    payload: Mapping[str, Any],
) -> ModelAffectAppraisal:
    if not isinstance(payload, Mapping):
        raise ContractViolation("affect_model_appraisal_invalid")
    raw_keys = tuple(payload.keys())
    if (
        any(
            not isinstance(key, str)
            or not key
            or key != key.strip()
            for key in raw_keys
        )
        or len(raw_keys) != len(set(raw_keys))
        or set(raw_keys) != _MODEL_ALLOWED_KEYS
    ):
        raise ContractViolation("affect_model_authority_field_forbidden")
    try:
        hint = AffectAppraisalKind(
            str(payload.get("appraisal_hint", "")).strip().lower()
        )
    except ValueError as exc:
        raise ContractViolation("affect_appraisal_hint_invalid") from exc
    return ModelAffectAppraisal(
        appraisal_hint=hint,
        confidence=payload.get("confidence", 0.0),
    )


def _coerce_appraisal(
    value: Mapping[str, Any] | ModelAffectAppraisal | None,
) -> ModelAffectAppraisal:
    if value is None:
        return ModelAffectAppraisal(
            appraisal_hint=AffectAppraisalKind.NEUTRAL_FACT,
            confidence=1.0,
        )
    if isinstance(value, ModelAffectAppraisal):
        return value
    if isinstance(value, Mapping):
        return parse_model_affect_appraisal(value)
    raise ContractViolation("affect_model_appraisal_invalid")


@dataclass(frozen=True, slots=True, init=False)
class AffectAdmissionEvidence:
    """Opaque handle to one exact, still-unclaimed affect consumer ticket."""

    context: AcceptedTurnContext = field(repr=False)
    ticket: AcceptedTurnTicket = field(repr=False)
    _authority: AcceptedTurnAuthority = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    @property
    def binding(self):
        return self.context.binding

    @property
    def envelope(self):
        return self.context.envelope

    @property
    def content_digest(self) -> str:
        return self.context.content_digest

    @property
    def sender_kind(self) -> SenderKind:
        return self.context.sender_kind

    def trace_metadata(self) -> dict[str, str | bool]:
        return {
            "affect_ticket_verified": True,
            "affect_ticket_consumer": AcceptedTurnConsumer.AFFECT_STATE.value,
            "affect_ticket_binding_bound": True,
        }


def issue_affect_admission_evidence(
    ticket: AcceptedTurnTicket,
    *,
    dispatch: AcceptedTurnDispatch,
    accepted_turn_authority: AcceptedTurnAuthority,
) -> AffectAdmissionEvidence:
    if type(ticket) is not AcceptedTurnTicket:
        raise ContractViolation("affect_ticket_required")
    if type(accepted_turn_authority) is not AcceptedTurnAuthority:
        raise ContractViolation("affect_ticket_authority_required")
    if ticket.consumer is not AcceptedTurnConsumer.AFFECT_STATE:
        raise ContractViolation("affect_ticket_consumer_required")
    canonical_ticket = accepted_turn_authority.ticket_for(
        dispatch,
        AcceptedTurnConsumer.AFFECT_STATE,
    )
    if canonical_ticket is not ticket:
        raise ContractViolation("affect_ticket_not_canonical")
    context = accepted_turn_authority.context_for(
        ticket,
        consumer=AcceptedTurnConsumer.AFFECT_STATE,
        binding=dispatch.binding,
    )
    if not _context_identity_verified(context):
        raise ContractViolation("affect_context_identity_unverified")
    if (
        ticket.binding is not context.binding
        or dispatch.binding is not context.binding
        or ticket.conversation_revision != context.binding.conversation_revision
        or dispatch.conversation_revision != context.binding.conversation_revision
        or context.plugin_source is not PluginSource.NONE
    ):
        raise ContractViolation("affect_ticket_binding_mismatch")
    evidence = object.__new__(AffectAdmissionEvidence)
    object.__setattr__(evidence, "context", context)
    object.__setattr__(evidence, "ticket", ticket)
    object.__setattr__(evidence, "_authority", accepted_turn_authority)
    object.__setattr__(evidence, "_seal", _ADMISSION_EVIDENCE_SEAL)
    return evidence


def _receipt_successful(receipt: SentReplyRecord) -> bool:
    if (
        not isinstance(receipt.segments, tuple)
        or not receipt.segments
        or any(
            not isinstance(segment, SendSegmentAttempt)
            for segment in receipt.segments
        )
    ):
        return False
    successful = tuple(
        segment
        for segment in receipt.segments
        if segment.status is SegmentSendStatus.SUCCEEDED
    )
    created_at = _positive_finite(receipt.created_at)
    sent_at = _positive_finite(receipt.sent_at)
    return bool(
        receipt.is_terminal
        and successful
        and created_at is not None
        and sent_at is not None
        and all(
            segment.status is SegmentSendStatus.SUCCEEDED
            and segment.internal_reply_id == receipt.reply_id
            and _positive_finite(segment.completed_at) is not None
            and isinstance(segment.visible_text, str)
            and bool(segment.visible_text)
            and isinstance(segment.visible_text_digest, str)
            and segment.visible_text_digest == _content_digest(segment.visible_text)
            for segment in successful
        )
    )


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class ShioReceiptEvidence:
    """Opaque canonical lookup result from the internal send ledger."""

    receipt: SentReplyRecord = field(repr=False)
    def trace_metadata(self) -> dict[str, int | bool]:
        try:
            evidence = inspect_shio_receipt_evidence(self)
            metadata = evidence.receipt.trace_metadata()
        except Exception:
            return {"affect_receipt_verified": False}
        return {
            "affect_receipt_verified": True,
            "affect_receipt_segment_count": int(metadata["segment_count"]),
            "affect_receipt_succeeded_count": int(metadata["succeeded_count"]),
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ShioReceiptEvidence(verified="
            f"{metadata.get('affect_receipt_verified', False)!r})"
        )


def _sent_reply_snapshot(receipt: SentReplyRecord) -> tuple[object, ...]:
    if type(receipt) is not SentReplyRecord:
        raise ContractViolation("affect_receipt_evidence_corrupt")
    try:
        segments = object.__getattribute__(receipt, "segments")
        expression_ids = object.__getattribute__(receipt, "expression_ids")
        values = (
            object.__getattribute__(receipt, "reply_id"),
            object.__getattribute__(receipt, "target_message_id"),
            object.__getattribute__(receipt, "target_sender_key"),
            object.__getattribute__(receipt, "session_id"),
            object.__getattribute__(receipt, "scope_key"),
            object.__getattribute__(receipt, "target_content_digest"),
            object.__getattribute__(receipt, "target_source_kind"),
            object.__getattribute__(receipt, "target_referenced_message_id"),
            segments,
            expression_ids,
            object.__getattribute__(receipt, "trace_id"),
            object.__getattribute__(receipt, "created_at"),
            object.__getattribute__(receipt, "sent_at"),
        )
    except (AttributeError, TypeError) as exc:
        raise ContractViolation("affect_receipt_evidence_corrupt") from exc
    if (
        any(type(value) is not str for value in (*values[:8], values[10]))
        or type(segments) is not tuple
        or type(expression_ids) is not tuple
        or any(type(value) is not str for value in expression_ids)
        or type(values[11]) is not float
        or type(values[12]) is not float
    ):
        raise ContractViolation("affect_receipt_evidence_corrupt")
    segment_snapshots: list[tuple[object, ...]] = []
    for segment in segments:
        if type(segment) is not SendSegmentAttempt:
            raise ContractViolation("affect_receipt_evidence_corrupt")
        try:
            snapshot = (
                object.__getattribute__(segment, "internal_reply_id"),
                object.__getattribute__(segment, "segment_id"),
                object.__getattribute__(segment, "segment_index"),
                object.__getattribute__(segment, "visible_text"),
                object.__getattribute__(segment, "visible_text_digest"),
                object.__getattribute__(segment, "visible_text_length"),
                object.__getattribute__(segment, "status"),
                object.__getattribute__(segment, "attempted_at"),
                object.__getattribute__(segment, "completed_at"),
                object.__getattribute__(segment, "platform_message_id"),
                object.__getattribute__(segment, "platform_receipt_source"),
                object.__getattribute__(segment, "failure_kind"),
            )
        except (AttributeError, TypeError) as exc:
            raise ContractViolation("affect_receipt_evidence_corrupt") from exc
        if (
            any(
                type(value) is not str
                for value in (
                    *snapshot[:2],
                    *snapshot[3:5],
                    *snapshot[9:10],
                    *snapshot[11:12],
                )
            )
            or type(snapshot[2]) is not int
            or type(snapshot[5]) is not int
            or type(snapshot[6]) is not SegmentSendStatus
            or type(snapshot[7]) is not float
            or type(snapshot[8]) is not float
            or (
                snapshot[10] is not None
                and type(snapshot[10]) is not PlatformReceiptSource
            )
        ):
            raise ContractViolation("affect_receipt_evidence_corrupt")
        segment_snapshots.append(snapshot)
    return (*values[:8], tuple(segment_snapshots), expression_ids, *values[10:])


def _build_receipt_evidence_vault():
    lock = threading.RLock()
    records: tuple[
        tuple[
            weakref.ReferenceType[ShioReceiptEvidence],
            SentReplyRecord,
            tuple[object, ...],
        ],
        ...,
    ] = ()

    def mint(receipt: SentReplyRecord) -> ShioReceiptEvidence:
        nonlocal records
        snapshot = _sent_reply_snapshot(receipt)
        evidence = object.__new__(ShioReceiptEvidence)
        object.__setattr__(evidence, "receipt", receipt)
        with lock:
            records = tuple(record for record in records if record[0]() is not None)
            if len(records) >= 1024:
                raise ContractViolation("affect_receipt_evidence_ledger_full")
            records = (*records, (weakref.ref(evidence), receipt, snapshot))
        return evidence

    def inspect(evidence: ShioReceiptEvidence) -> ShioReceiptEvidence:
        nonlocal records
        if type(evidence) is not ShioReceiptEvidence:
            raise ContractViolation("affect_receipt_evidence_required")
        try:
            receipt = object.__getattribute__(evidence, "receipt")
        except AttributeError as exc:
            raise ContractViolation("affect_receipt_evidence_corrupt") from exc
        current = _sent_reply_snapshot(receipt)
        with lock:
            records = tuple(record for record in records if record[0]() is not None)
            matches = tuple(record for record in records if record[0]() is evidence)
            if len(matches) != 1:
                raise ContractViolation("affect_receipt_evidence_required")
            if matches[0][1] is not receipt or matches[0][2] != current:
                raise ContractViolation("affect_receipt_evidence_corrupt")
            return evidence

    return mint, inspect


_mint_shio_receipt_evidence, inspect_shio_receipt_evidence = (
    _build_receipt_evidence_vault()
)
del _build_receipt_evidence_vault


def issue_shio_receipt_evidence(
    ledger: InternalSendReceiptLedger,
    *,
    internal_reply_id: str,
) -> ShioReceiptEvidence:
    if not isinstance(ledger, InternalSendReceiptLedger):
        raise ContractViolation("affect_send_ledger_required")
    reply_id = require_text(internal_reply_id, "affect_internal_reply_id")
    receipt = ledger.sent_reply_record(reply_id)
    if (
        receipt is None
        or receipt.reply_id != reply_id
        or not reply_id.startswith("shio-")
        or receipt.target_source_kind != "current_inbound"
        or not receipt.trace_id
        or not _receipt_successful(receipt)
    ):
        raise ContractViolation("affect_receipt_evidence_unverified")
    return _mint_shio_receipt_evidence(receipt)


@dataclass(frozen=True, slots=True)
class AffectState:
    scope_key: str = field(repr=False)
    subject_key: str = field(repr=False)
    object_key: str = field(repr=False)
    valence: float
    arousal: float
    intensity: float
    cause: AffectCause
    inertia: float
    updated_at: float
    version: int
    conversation_revision: int

    def __post_init__(self) -> None:
        scope = require_text(self.scope_key, "affect_scope_key")
        subject = require_text(self.subject_key, "affect_subject_key")
        object_key = require_text(self.object_key, "affect_object_key")
        if not subject.startswith(f"{scope}|agent:") or not subject.removeprefix(
            f"{scope}|agent:"
        ):
            raise ContractViolation("affect_subject_scope_mismatch")
        if not object_key.startswith(f"{scope}|user:") or not object_key.removeprefix(
            f"{scope}|user:"
        ):
            raise ContractViolation("affect_object_scope_mismatch")
        object.__setattr__(
            self,
            "valence",
            _bounded(
                self.valence,
                "affect_valence",
                lower=-MAX_ABS_VALENCE,
                upper=MAX_ABS_VALENCE,
            ),
        )
        object.__setattr__(
            self,
            "arousal",
            _bounded(
                self.arousal,
                "affect_arousal",
                lower=0.0,
                upper=MAX_AFFECT_AROUSAL,
            ),
        )
        object.__setattr__(
            self,
            "intensity",
            _bounded(
                self.intensity,
                "affect_intensity",
                lower=0.0,
                upper=MAX_AFFECT_INTENSITY,
            ),
        )
        if not isinstance(self.cause, AffectCause):
            raise ContractViolation("affect_cause_invalid")
        object.__setattr__(
            self,
            "inertia",
            _bounded(
                self.inertia,
                "affect_inertia",
                lower=MIN_AFFECT_INERTIA,
                upper=MAX_AFFECT_INERTIA,
            ),
        )
        object.__setattr__(self, "updated_at", _clock(self.updated_at))
        _positive_int(self.version, "affect_version")
        _positive_int(
            self.conversation_revision,
            "affect_conversation_revision",
        )

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        return {
            "affect_valence": self.valence,
            "affect_arousal": self.arousal,
            "affect_intensity": self.intensity,
            "affect_cause": self.cause.value,
            "affect_inertia": self.inertia,
            "affect_updated_at": self.updated_at,
            "affect_version": self.version,
            "affect_conversation_revision": self.conversation_revision,
            "affect_subject_object_bound": True,
        }


def _affect_state_snapshot(state: AffectState) -> tuple[object, ...]:
    if type(state) is not AffectState:
        raise ContractViolation("affect_state_corrupt")
    try:
        values = (
            object.__getattribute__(state, "scope_key"),
            object.__getattribute__(state, "subject_key"),
            object.__getattribute__(state, "object_key"),
            object.__getattribute__(state, "valence"),
            object.__getattribute__(state, "arousal"),
            object.__getattribute__(state, "intensity"),
            object.__getattribute__(state, "cause"),
            object.__getattribute__(state, "inertia"),
            object.__getattribute__(state, "updated_at"),
            object.__getattribute__(state, "version"),
            object.__getattribute__(state, "conversation_revision"),
        )
    except (AttributeError, TypeError) as exc:
        raise ContractViolation("affect_state_corrupt") from exc
    expected_types = (
        str,
        str,
        str,
        float,
        float,
        float,
        AffectCause,
        float,
        float,
        int,
        int,
    )
    if any(type(value) is not expected for value, expected in zip(values, expected_types)):
        raise ContractViolation("affect_state_corrupt")
    return tuple((type(value), value) for value in values)


@dataclass(frozen=True, slots=True, init=False, eq=False, weakref_slot=True)
class AffectRenderContext:
    """Prompt-safe continuous affect bound to one exact current DecisionBinding."""

    cause: AffectCause
    valence: float
    arousal: float
    intensity: float
    inertia: float
    version: int
    conversation_revision: int
    carryover_active: bool
    _binding: DecisionBinding = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        inspect_affect_render_context(self, self._binding)
        return {
            "continuous_affect_bound": True,
            "continuous_affect_cause": self.cause.value,
            "continuous_affect_valence": self.valence,
            "continuous_affect_arousal": self.arousal,
            "continuous_affect_intensity": self.intensity,
            "continuous_affect_inertia": self.inertia,
            "continuous_affect_version": self.version,
            "continuous_affect_revision": self.conversation_revision,
            "continuous_affect_carryover": self.carryover_active,
        }


def _affect_render_context_snapshot(
    context: AffectRenderContext,
) -> tuple[object, ...]:
    if type(context) is not AffectRenderContext:
        raise ContractViolation("affect_render_context_required")
    try:
        values = (
            object.__getattribute__(context, "cause"),
            object.__getattribute__(context, "valence"),
            object.__getattribute__(context, "arousal"),
            object.__getattribute__(context, "intensity"),
            object.__getattribute__(context, "inertia"),
            object.__getattribute__(context, "version"),
            object.__getattribute__(context, "conversation_revision"),
            object.__getattribute__(context, "carryover_active"),
            object.__getattribute__(context, "_binding"),
            object.__getattribute__(context, "_seal"),
        )
    except (AttributeError, TypeError) as exc:
        raise ContractViolation("affect_render_context_corrupt") from exc
    expected_types = (
        AffectCause,
        float,
        float,
        float,
        float,
        int,
        int,
        bool,
        DecisionBinding,
        object,
    )
    if any(
        type(value) is not expected
        for value, expected in zip(values[:-1], expected_types[:-1])
    ) or values[-1] is not _AFFECT_RENDER_CONTEXT_SEAL:
        raise ContractViolation("affect_render_context_corrupt")
    return (
        *( (type(value), value) for value in values[:-2] ),
        (DecisionBinding, values[-2]),
        (object, _AFFECT_RENDER_CONTEXT_SEAL),
    )


def _affect_binding_snapshot(binding: DecisionBinding) -> tuple[object, ...]:
    if type(binding) is not DecisionBinding:
        raise ContractViolation("affect_render_binding_required")
    try:
        values = (
            object.__getattribute__(binding, "scope_key"),
            object.__getattribute__(binding, "session_id"),
            object.__getattribute__(binding, "current_message_id"),
            object.__getattribute__(binding, "current_sender_key"),
            object.__getattribute__(binding, "current_content_digest"),
            object.__getattribute__(binding, "conversation_revision"),
            object.__getattribute__(binding, "generation_epoch"),
            object.__getattribute__(binding, "trace_id"),
        )
    except (AttributeError, TypeError) as exc:
        raise ContractViolation("affect_render_binding_corrupt") from exc
    expected = (str, str, str, str, str, int, int, str)
    if any(type(value) is not kind for value, kind in zip(values, expected)):
        raise ContractViolation("affect_render_binding_corrupt")
    return tuple((type(value), value) for value in values)


def _build_affect_render_context_vault():
    lock = threading.RLock()
    records: tuple[tuple[object, ...], ...] = ()
    max_records = 512

    def mint(
        *,
        binding: DecisionBinding,
        source_state: AffectState,
        projected_state: AffectState,
        source_book: object | None,
    ) -> AffectRenderContext:
        nonlocal records
        if type(binding) is not DecisionBinding:
            raise ContractViolation("affect_render_binding_required")
        binding_snapshot = _affect_binding_snapshot(binding)
        source_snapshot = _affect_state_snapshot(source_state)
        _affect_state_snapshot(projected_state)
        context = object.__new__(AffectRenderContext)
        object.__setattr__(context, "cause", projected_state.cause)
        object.__setattr__(context, "valence", projected_state.valence)
        object.__setattr__(context, "arousal", projected_state.arousal)
        object.__setattr__(context, "intensity", projected_state.intensity)
        object.__setattr__(context, "inertia", projected_state.inertia)
        object.__setattr__(context, "version", projected_state.version)
        object.__setattr__(
            context,
            "conversation_revision",
            projected_state.conversation_revision,
        )
        object.__setattr__(
            context,
            "carryover_active",
            bool(source_state.version > 1 and projected_state.intensity > _VALUE_EPSILON),
        )
        object.__setattr__(context, "_binding", binding)
        object.__setattr__(context, "_seal", _AFFECT_RENDER_CONTEXT_SEAL)
        snapshot = _affect_render_context_snapshot(context)
        with lock:
            live = tuple(record for record in records if record[0]() is not None)
            if len(live) >= max_records:
                raise ContractViolation("affect_render_context_capacity")
            records = (
                *live,
                (
                    weakref.ref(context),
                    snapshot,
                    binding,
                    source_state,
                    source_snapshot,
                    source_book,
                    binding_snapshot,
                ),
            )
        return context

    def inspect(
        context: AffectRenderContext,
        binding: DecisionBinding,
    ) -> AffectRenderContext:
        nonlocal records
        if type(binding) is not DecisionBinding:
            raise ContractViolation("affect_render_binding_required")
        binding_snapshot = _affect_binding_snapshot(binding)
        snapshot = _affect_render_context_snapshot(context)
        with lock:
            live = tuple(record for record in records if record[0]() is not None)
            records = live
            matches = tuple(record for record in live if record[0]() is context)
        if len(matches) != 1:
            raise ContractViolation("affect_render_context_not_canonical")
        record = matches[0]
        if (
            record[1] != snapshot
            or record[2] is not binding
            or object.__getattribute__(context, "_binding") is not binding
            or record[6] != binding_snapshot
        ):
            raise ContractViolation("affect_render_context_binding_mismatch")
        if _affect_state_snapshot(record[3]) != record[4]:
            raise ContractViolation("affect_render_source_corrupt")
        source_book = record[5]
        if source_book is not None:
            source_book._inspect_render_source(
                record[3],
                record[4],
                binding,
            )
        return context

    return mint, inspect


_mint_affect_render_context, inspect_affect_render_context = (
    _build_affect_render_context_vault()
)


@dataclass(frozen=True, slots=True)
class AffectMutationResult:
    status: AffectMutationStatus
    reason: AffectRejectReason | None
    state_changed: bool
    model_appraisal_applied: bool
    state: AffectState | None = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, AffectMutationStatus):
            raise ContractViolation("affect_mutation_status_invalid")
        if type(self.state_changed) is not bool:
            raise ContractViolation("affect_mutation_changed_invalid")
        if type(self.model_appraisal_applied) is not bool:
            raise ContractViolation("affect_model_appraisal_flag_invalid")
        if self.state is not None and not isinstance(self.state, AffectState):
            raise ContractViolation("affect_mutation_state_invalid")
        if self.status is AffectMutationStatus.REJECTED:
            if (
                not isinstance(self.reason, AffectRejectReason)
                or self.state_changed
                or self.model_appraisal_applied
            ):
                raise ContractViolation("affect_rejection_shape_invalid")
        elif self.reason is not None or not self.state_changed or self.state is None:
            raise ContractViolation("affect_acceptance_shape_invalid")

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        metadata: dict[str, str | int | float | bool] = {
            "affect_mutation_status": self.status.value,
            "affect_reject_reason": self.reason.value if self.reason else "none",
            "affect_state_changed": self.state_changed,
            "affect_model_appraisal_applied": self.model_appraisal_applied,
            "affect_has_state": self.state is not None,
        }
        if self.state is not None:
            metadata.update(self.state.trace_metadata())
        return metadata


@dataclass(frozen=True, slots=True)
class _AcceptedTarget:
    scope_key: str = field(repr=False)
    session_id: str = field(repr=False)
    message_id: str = field(repr=False)
    object_key: str = field(repr=False)
    subject_key: str = field(repr=False)
    content_digest: str = field(repr=False)
    trace_id: str = field(repr=False)
    referenced_message_id: str = field(repr=False)
    conversation_revision: int


_IMPULSES: dict[
    AffectAppraisalKind,
    tuple[float, float, float, float, AffectCause],
] = {
    AffectAppraisalKind.POSITIVE_SOCIAL: (
        0.72,
        0.45,
        0.62,
        0.72,
        AffectCause.POSITIVE_SOCIAL,
    ),
    AffectAppraisalKind.NEGATIVE_SOCIAL: (
        -0.72,
        0.68,
        0.70,
        0.64,
        AffectCause.NEGATIVE_SOCIAL,
    ),
    AffectAppraisalKind.PLAYFUL: (
        0.25,
        0.78,
        0.60,
        0.58,
        AffectCause.PLAYFUL,
    ),
    AffectAppraisalKind.CARE: (
        0.42,
        0.50,
        0.68,
        0.78,
        AffectCause.CARE,
    ),
    AffectAppraisalKind.REPAIR: (
        0.18,
        0.35,
        0.45,
        0.62,
        AffectCause.REPAIR,
    ),
}


def _decayed_state(state: AffectState, now: float) -> AffectState:
    if now < state.updated_at:
        raise ContractViolation("affect_clock_stale")
    elapsed = now - state.updated_at
    if elapsed <= 0:
        return state
    retention = state.inertia ** (elapsed / AFFECT_DECAY_INTERVAL_S)
    valence = _zero_small(state.valence * retention)
    arousal = _zero_small(state.arousal * (retention**1.10))
    intensity = _zero_small(state.intensity * retention)
    cause = state.cause
    if not valence and not arousal and not intensity:
        cause = AffectCause.BASELINE
    return AffectState(
        scope_key=state.scope_key,
        subject_key=state.subject_key,
        object_key=state.object_key,
        valence=valence,
        arousal=arousal,
        intensity=intensity,
        cause=cause,
        inertia=state.inertia,
        updated_at=now,
        version=state.version,
        conversation_revision=state.conversation_revision,
    )


def _next_human_state(
    *,
    current: AffectState | None,
    scope_key: str,
    subject_key: str,
    object_key: str,
    conversation_revision: int,
    appraisal: ModelAffectAppraisal,
    now: float,
) -> tuple[AffectState, bool]:
    if current is None:
        valence = 0.0
        arousal = 0.0
        intensity = 0.0
        inertia = DEFAULT_AFFECT_INERTIA
        cause = AffectCause.BASELINE
        version = 1
    else:
        decayed = _decayed_state(current, now)
        valence = decayed.valence
        arousal = decayed.arousal
        intensity = decayed.intensity
        inertia = decayed.inertia
        cause = decayed.cause
        version = current.version + 1

    applied = bool(
        appraisal.confidence >= MODEL_APPRAISAL_MIN_CONFIDENCE
        and appraisal.appraisal_hint in _IMPULSES
    )
    if applied:
        (
            target_valence,
            target_arousal,
            target_intensity,
            target_inertia,
            cause,
        ) = _IMPULSES[appraisal.appraisal_hint]
        response = min(0.75, appraisal.confidence * 0.65)
        valence += response * (target_valence - valence)
        arousal += response * (target_arousal - arousal)
        intensity += response * (target_intensity - intensity)
        inertia += response * (target_inertia - inertia)

    return (
        AffectState(
            scope_key=scope_key,
            subject_key=subject_key,
            object_key=object_key,
            valence=_clamp(valence, -MAX_ABS_VALENCE, MAX_ABS_VALENCE),
            arousal=_clamp(arousal, 0.0, MAX_AFFECT_AROUSAL),
            intensity=_clamp(intensity, 0.0, MAX_AFFECT_INTENSITY),
            cause=cause,
            inertia=_clamp(
                inertia,
                MIN_AFFECT_INERTIA,
                MAX_AFFECT_INERTIA,
            ),
            updated_at=now,
            version=version,
            conversation_revision=conversation_revision,
        ),
        applied,
    )


class AffectStateBook:
    """Bounded, sender-isolated state updated only by admitted typed inputs."""

    __slots__ = (
        "_accepted_turn_authority",
        "_lock",
        "_max_receipts_per_scope",
        "_max_targets_per_scope",
        "_receipt_ids",
        "_revision_book",
        "_scope_revisions",
        "_state_snapshots",
        "_states",
        "_targets",
    )

    def __init__(
        self,
        *,
        accepted_turn_authority: AcceptedTurnAuthority,
        max_targets_per_scope: int = 64,
        max_receipts_per_scope: int = 64,
        revision_book: ConversationRevisionBook | None = None,
    ) -> None:
        if type(accepted_turn_authority) is not AcceptedTurnAuthority:
            raise ContractViolation("affect_ticket_authority_required")
        if revision_book is not None and type(revision_book) is not ConversationRevisionBook:
            raise ContractViolation("affect_revision_book_invalid")
        self._accepted_turn_authority = accepted_turn_authority
        self._revision_book = revision_book
        self._max_targets_per_scope = _positive_limit(
            max_targets_per_scope,
            "affect_max_targets_per_scope",
        )
        self._max_receipts_per_scope = _positive_limit(
            max_receipts_per_scope,
            "affect_max_receipts_per_scope",
        )
        self._states: dict[tuple[str, str], AffectState] = {}
        self._state_snapshots: dict[tuple[str, str], tuple[object, ...]] = {}
        self._scope_revisions: dict[str, int] = {}
        self._targets: dict[str, tuple[_AcceptedTarget, ...]] = {}
        self._receipt_ids: dict[str, tuple[str, ...]] = {}
        self._lock = threading.RLock()

    @property
    def state_count(self) -> int:
        with self._lock:
            return len(self._states)

    def scope_revision(self, scope_key: str) -> int:
        scope = require_text(scope_key, "affect_scope_key")
        with self._lock:
            return self._scope_revisions.get(scope, 0)

    def get_state(self, scope_key: str, object_key: str) -> AffectState | None:
        scope = require_text(scope_key, "affect_scope_key")
        object_value = require_text(object_key, "affect_object_key")
        with self._lock:
            state = self._states.get((scope, object_value))
            if state is not None and self._state_snapshots.get(
                (scope, object_value)
            ) != _affect_state_snapshot(state):
                raise ContractViolation("affect_state_corrupt")
            return state

    def _inspect_render_source(
        self,
        state: AffectState,
        snapshot: tuple[object, ...],
        binding: DecisionBinding,
    ) -> None:
        if type(binding) is not DecisionBinding:
            raise ContractViolation("affect_render_binding_required")
        with self._lock:
            key = (binding.scope_key, binding.current_sender_key)
            if (
                self._states.get(key) is not state
                or self._state_snapshots.get(key) != snapshot
                or _affect_state_snapshot(state) != snapshot
                or state.conversation_revision != binding.conversation_revision
            ):
                raise ContractViolation("affect_render_source_not_current")

    def issue_render_context(
        self,
        mutation: AffectMutationResult,
        *,
        binding: DecisionBinding,
        now: float,
    ) -> AffectRenderContext:
        if type(mutation) is not AffectMutationResult:
            raise ContractViolation("affect_mutation_required")
        if type(binding) is not DecisionBinding:
            raise ContractViolation("affect_render_binding_required")
        current_time = _clock(now)
        try:
            status = object.__getattribute__(mutation, "status")
            reason = object.__getattribute__(mutation, "reason")
            state_changed = object.__getattribute__(mutation, "state_changed")
            model_applied = object.__getattribute__(
                mutation,
                "model_appraisal_applied",
            )
            state = object.__getattribute__(mutation, "state")
        except (AttributeError, TypeError) as exc:
            raise ContractViolation("affect_mutation_corrupt") from exc
        if (
            status is not AffectMutationStatus.ACCEPTED_HUMAN
            or reason is not None
            or type(state_changed) is not bool
            or not state_changed
            or type(model_applied) is not bool
            or type(state) is not AffectState
        ):
            raise ContractViolation("affect_mutation_not_renderable")
        with self._lock:
            key = (binding.scope_key, binding.current_sender_key)
            snapshot = _affect_state_snapshot(state)
            if (
                self._states.get(key) is not state
                or self._state_snapshots.get(key) != snapshot
                or state.scope_key != binding.scope_key
                or state.object_key != binding.current_sender_key
                or state.conversation_revision != binding.conversation_revision
            ):
                raise ContractViolation("affect_render_source_not_current")
            projected = _decayed_state(state, current_time)
            return _mint_affect_render_context(
                binding=binding,
                source_state=state,
                projected_state=projected,
                source_book=self,
            )

    def project_state(
        self,
        scope_key: str,
        object_key: str,
        *,
        now: float,
    ) -> AffectState | None:
        current_time = _clock(now)
        state = self.get_state(scope_key, object_key)
        if state is None:
            return None
        return _decayed_state(state, current_time)

    def _reject(
        self,
        *,
        scope_key: str,
        object_key: str,
        reason: AffectRejectReason,
    ) -> AffectMutationResult:
        return AffectMutationResult(
            status=AffectMutationStatus.REJECTED,
            reason=reason,
            state_changed=False,
            model_appraisal_applied=False,
            state=self._states.get((scope_key, object_key)),
        )

    def record_human(
        self,
        admission: AffectAdmissionEvidence,
        *,
        appraisal: Mapping[str, Any] | ModelAffectAppraisal | None,
        now: float,
    ) -> AffectMutationResult:
        if (
            type(admission) is not AffectAdmissionEvidence
            or getattr(admission, "_seal", None) is not _ADMISSION_EVIDENCE_SEAL
        ):
            raise ContractViolation("affect_admission_evidence_required")
        if admission._authority is not self._accepted_turn_authority:
            raise ContractViolation("affect_ticket_authority_mismatch")
        context = admission.context
        ticket = admission.ticket
        scope = context.envelope.scope_key
        object_key = context.envelope.sender_key
        current_time = _clock(now)

        with self._lock:
            if (
                ticket.binding is not context.binding
                or ticket.consumer is not AcceptedTurnConsumer.AFFECT_STATE
                or ticket.conversation_revision
                != context.binding.conversation_revision
            ):
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.BINDING_MISMATCH,
                )
            if (
                not _context_identity_verified(context)
            ):
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.EVENT_SOURCE_UNVERIFIED,
                )

            subject_key = _subject_key(context)

            revision = context.binding.conversation_revision
            current_revision = self._scope_revisions.get(scope, 0)
            if current_revision == 0 and scope not in self._scope_revisions:
                restored = (
                    self._revision_book.restored_revision(scope)
                    if self._revision_book is not None
                    else 0
                )
                if revision == restored + 1:
                    current_revision = restored
            if revision <= current_revision:
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.REVISION_STALE,
                )
            if revision != current_revision + 1:
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.REVISION_GAP,
                )

            current = self._states.get((scope, object_key))
            if current is not None and current_time < current.updated_at:
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.CLOCK_STALE,
                )
            soft_appraisal = _coerce_appraisal(appraisal)
            state, applied = _next_human_state(
                current=current,
                scope_key=scope,
                subject_key=subject_key,
                object_key=object_key,
                conversation_revision=revision,
                appraisal=soft_appraisal,
                now=current_time,
            )

            targets = (
                *self._targets.get(scope, ()),
                _AcceptedTarget(
                    scope_key=scope,
                    session_id=context.envelope.session_id,
                    message_id=context.envelope.message_id,
                    object_key=object_key,
                    subject_key=subject_key,
                    content_digest=context.content_digest,
                    trace_id=context.binding.trace_id,
                    referenced_message_id=context.envelope.reply_to_message_id,
                    conversation_revision=revision,
                ),
            )[-self._max_targets_per_scope :]
            self._accepted_turn_authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.AFFECT_STATE,
                binding=context.binding,
                context=context,
            )
            self._states[(scope, object_key)] = state
            self._state_snapshots[(scope, object_key)] = _affect_state_snapshot(state)
            self._scope_revisions[scope] = revision
            self._targets[scope] = targets
            return AffectMutationResult(
                status=AffectMutationStatus.ACCEPTED_HUMAN,
                reason=None,
                state_changed=True,
                model_appraisal_applied=applied,
                state=state,
            )

    @staticmethod
    def _target_for_receipt(
        targets: tuple[_AcceptedTarget, ...],
        receipt: SentReplyRecord,
    ) -> _AcceptedTarget | None:
        matches = tuple(
            target
            for target in targets
            if receipt.scope_key == target.scope_key
            and receipt.session_id == target.session_id
            and receipt.target_message_id == target.message_id
            and receipt.target_sender_key == target.object_key
            and receipt.target_content_digest == target.content_digest
            and receipt.trace_id == target.trace_id
            and receipt.target_referenced_message_id
            == target.referenced_message_id
        )
        return matches[0] if len(matches) == 1 else None

    def record_shio_receipt(
        self,
        evidence: ShioReceiptEvidence,
        *,
        now: float,
    ) -> AffectMutationResult:
        inspect_shio_receipt_evidence(evidence)
        receipt = evidence.receipt
        scope = require_text(receipt.scope_key, "affect_scope_key")
        object_key = str(receipt.target_sender_key or "").strip()
        reply_id = str(receipt.reply_id or "").strip()
        current_time = _clock(now)

        with self._lock:
            if (
                receipt.target_source_kind != "current_inbound"
                or not reply_id.startswith("shio-")
                or not receipt.trace_id
            ):
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.OUTBOUND_SOURCE_UNVERIFIED,
                )
            if reply_id in self._receipt_ids.get(scope, ()):
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.OUTBOUND_DUPLICATE,
                )
            if not _receipt_successful(receipt):
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.RECEIPT_NOT_SUCCESSFUL,
                )

            target = self._target_for_receipt(self._targets.get(scope, ()), receipt)
            if target is None:
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.OUTBOUND_TARGET_MISMATCH,
                )
            current = self._states.get((scope, target.object_key))
            if current is None or current.subject_key != target.subject_key:
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.OUTBOUND_TARGET_MISMATCH,
                )
            if current.conversation_revision != target.conversation_revision:
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.OUTBOUND_STALE,
                )
            sent_at = _positive_finite(receipt.sent_at)
            if (
                sent_at is None
                or current_time < current.updated_at
                or current_time < sent_at
            ):
                return self._reject(
                    scope_key=scope,
                    object_key=object_key,
                    reason=AffectRejectReason.CLOCK_STALE,
                )

            decayed = _decayed_state(current, current_time)
            state = AffectState(
                scope_key=decayed.scope_key,
                subject_key=decayed.subject_key,
                object_key=decayed.object_key,
                valence=_zero_small(decayed.valence * 0.95),
                arousal=_zero_small(decayed.arousal * 0.70),
                intensity=_zero_small(decayed.intensity * 0.75),
                cause=AffectCause.SHIO_OUTBOUND_SETTLE,
                inertia=decayed.inertia,
                updated_at=current_time,
                version=current.version + 1,
                conversation_revision=current.conversation_revision,
            )
            self._states[(scope, target.object_key)] = state
            self._state_snapshots[(scope, target.object_key)] = _affect_state_snapshot(
                state
            )
            self._receipt_ids[scope] = (
                *self._receipt_ids.get(scope, ()),
                reply_id,
            )[-self._max_receipts_per_scope :]
            return AffectMutationResult(
                status=AffectMutationStatus.ACCEPTED_SHIO_OUTBOUND,
                reason=None,
                state_changed=True,
                model_appraisal_applied=False,
                state=state,
            )


def _issue_test_affect_render_context(
    binding: DecisionBinding,
    *,
    cause: AffectCause = AffectCause.BASELINE,
    valence: float = 0.0,
    arousal: float = 0.0,
    intensity: float = 0.0,
    inertia: float = DEFAULT_AFFECT_INERTIA,
    version: int = 1,
) -> AffectRenderContext:
    """Test-only canonical issuer; production must use AffectStateBook."""

    if type(binding) is not DecisionBinding or type(cause) is not AffectCause:
        raise ContractViolation("affect_test_render_input_invalid")
    state = AffectState(
        scope_key=binding.scope_key,
        subject_key=f"{binding.scope_key}|agent:test-renderer",
        object_key=f"{binding.scope_key}|user:test-renderer",
        valence=valence,
        arousal=arousal,
        intensity=intensity,
        cause=cause,
        inertia=inertia,
        updated_at=0.0,
        version=version,
        conversation_revision=binding.conversation_revision,
    )
    return _mint_affect_render_context(
        binding=binding,
        source_state=state,
        projected_state=state,
        source_book=None,
    )


__all__ = [
    "AFFECT_DECAY_INTERVAL_S",
    "DEFAULT_AFFECT_INERTIA",
    "MAX_ABS_VALENCE",
    "MAX_AFFECT_AROUSAL",
    "MAX_AFFECT_INTENSITY",
    "MODEL_APPRAISAL_MIN_CONFIDENCE",
    "AffectAdmissionEvidence",
    "AffectAppraisalKind",
    "AffectCause",
    "AffectMutationResult",
    "AffectMutationStatus",
    "AffectRenderContext",
    "AffectRejectReason",
    "AffectState",
    "AffectStateBook",
    "ModelAffectAppraisal",
    "ShioReceiptEvidence",
    "issue_affect_admission_evidence",
    "inspect_affect_render_context",
    "inspect_shio_receipt_evidence",
    "issue_shio_receipt_evidence",
    "parse_model_affect_appraisal",
]
