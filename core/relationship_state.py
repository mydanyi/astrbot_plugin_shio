from __future__ import annotations

import math
import threading
import weakref
from dataclasses import dataclass, field
from enum import Enum

from .accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnTicket,
)
from .affect import AffectTrigger, appraise_affect
from .affect_state import ShioReceiptEvidence, inspect_shio_receipt_evidence
from .context_assembler import ReplyTarget
from .contracts import ContractViolation, DecisionBinding
from .conversation_ledger import ledger_content_digest
from .send_receipt import SentReplyRecord


class RelationshipBand(str, Enum):
    NEUTRAL = "neutral"
    WARM = "warm"
    CAUTIOUS = "cautious"


class RelationshipEventKind(str, Enum):
    INTERACTION = "interaction"
    POSITIVE_INTERACTION = "positive_interaction"
    NEGATIVE_INTERACTION = "negative_interaction"
    SUCCESSFUL_REPLY = "successful_reply"


class RelationshipMutationStatus(str, Enum):
    ACCEPTED_HUMAN = "accepted_human"
    ACCEPTED_OUTBOUND = "accepted_outbound"
    REJECTED = "rejected"


class RelationshipRejectReason(str, Enum):
    BINDING_MISMATCH = "binding_mismatch"
    MESSAGE_DIGEST_MISMATCH = "message_digest_mismatch"
    REVISION_STALE = "revision_stale"
    RECEIPT_UNVERIFIED = "receipt_unverified"
    RECEIPT_TARGET_MISMATCH = "receipt_target_mismatch"
    RECEIPT_REPLAYED = "receipt_replayed"
    CLOCK_STALE = "clock_stale"


_POSITIVE_TRIGGERS = {
    AffectTrigger.PRAISE,
    AffectTrigger.CONCERN_FOR_AGENT,
    AffectTrigger.APOLOGY,
    AffectTrigger.GRATITUDE,
}
_NEGATIVE_TRIGGERS = {
    AffectTrigger.CORRECTION_OR_MISTAKE,
    AffectTrigger.DISAGREEMENT,
}


def _clock(value: object) -> float:
    if type(value) not in {int, float}:
        raise ContractViolation("relationship_clock_invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ContractViolation("relationship_clock_invalid")
    return parsed


def _band(affinity: float) -> RelationshipBand:
    if affinity >= 0.25:
        return RelationshipBand.WARM
    if affinity <= -0.15:
        return RelationshipBand.CAUTIOUS
    return RelationshipBand.NEUTRAL


def _state_snapshot(state: "RelationshipState") -> tuple[object, ...]:
    if type(state) is not RelationshipState:
        raise ContractViolation("relationship_state_corrupt")
    try:
        snapshot = (
            state.scope_key,
            state.object_key,
            state.affinity,
            state.band,
            state.interaction_count,
            state.positive_event_count,
            state.negative_event_count,
            state.successful_reply_count,
            state.explanation_reason_codes,
            state.updated_at,
            state.version,
            state.conversation_revision,
        )
    except (AttributeError, TypeError) as exc:
        raise ContractViolation("relationship_state_corrupt") from exc
    if (
        type(snapshot[0]) is not str
        or type(snapshot[1]) is not str
        or type(snapshot[2]) is not float
        or type(snapshot[3]) is not RelationshipBand
        or any(type(value) is not int for value in snapshot[4:8])
        or type(snapshot[8]) is not tuple
        or any(type(value) is not str for value in snapshot[8])
        or type(snapshot[9]) is not float
        or type(snapshot[10]) is not int
        or type(snapshot[11]) is not int
    ):
        raise ContractViolation("relationship_state_corrupt")
    return snapshot


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class RelationshipState:
    scope_key: str = field(repr=False)
    object_key: str = field(repr=False)
    affinity: float
    band: RelationshipBand
    interaction_count: int
    positive_event_count: int
    negative_event_count: int
    successful_reply_count: int
    explanation_reason_codes: tuple[str, ...]
    updated_at: float
    version: int
    conversation_revision: int

    def __post_init__(self) -> None:
        if type(self.scope_key) is not str or not self.scope_key:
            raise ContractViolation("relationship_scope_invalid")
        if type(self.object_key) is not str or not self.object_key:
            raise ContractViolation("relationship_object_invalid")
        if type(self.affinity) is not float or not -1.0 <= self.affinity <= 1.0:
            raise ContractViolation("relationship_affinity_invalid")
        if type(self.band) is not RelationshipBand or self.band is not _band(self.affinity):
            raise ContractViolation("relationship_band_invalid")
        for name in (
            "interaction_count",
            "positive_event_count",
            "negative_event_count",
            "successful_reply_count",
            "version",
            "conversation_revision",
        ):
            value = object.__getattribute__(self, name)
            if type(value) is not int or value < 0:
                raise ContractViolation(f"relationship_{name}_invalid")
        if self.version < 1 or self.conversation_revision < 1:
            raise ContractViolation("relationship_version_invalid")
        if (
            type(self.explanation_reason_codes) is not tuple
            or not self.explanation_reason_codes
            or len(self.explanation_reason_codes) > 4
            or any(
                type(reason) is not str
                or reason not in {item.value for item in RelationshipEventKind}
                for reason in self.explanation_reason_codes
            )
        ):
            raise ContractViolation("relationship_explanation_invalid")
        _clock(self.updated_at)

    def trace_metadata(self) -> dict[str, str | int | float]:
        inspect_relationship_state(self)
        return {
            "relationship_band": self.band.value,
            "relationship_affinity": round(self.affinity, 3),
            "relationship_interaction_count": self.interaction_count,
            "relationship_positive_event_count": self.positive_event_count,
            "relationship_negative_event_count": self.negative_event_count,
            "relationship_successful_reply_count": self.successful_reply_count,
            "relationship_reason_count": len(self.explanation_reason_codes),
            "relationship_version": self.version,
        }

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except Exception:
            return "RelationshipState(canonical=False)"
        return (
            "RelationshipState("
            f"band={metadata['relationship_band']!r}, "
            f"version={metadata['relationship_version']}, "
            f"events={metadata['relationship_interaction_count']})"
        )


def _build_state_vault():
    lock = threading.RLock()
    records: tuple[
        tuple[weakref.ReferenceType[RelationshipState], tuple[object, ...]],
        ...,
    ] = ()

    def mint(**values: object) -> RelationshipState:
        nonlocal records
        state = object.__new__(RelationshipState)
        for name in (
            "scope_key",
            "object_key",
            "affinity",
            "band",
            "interaction_count",
            "positive_event_count",
            "negative_event_count",
            "successful_reply_count",
            "explanation_reason_codes",
            "updated_at",
            "version",
            "conversation_revision",
        ):
            object.__setattr__(state, name, values[name])
        state.__post_init__()
        snapshot = _state_snapshot(state)
        with lock:
            records = tuple(record for record in records if record[0]() is not None)
            if len(records) >= 2048:
                raise ContractViolation("relationship_state_ledger_full")
            records = (*records, (weakref.ref(state), snapshot))
        return state

    def inspect(state: RelationshipState) -> RelationshipState:
        nonlocal records
        current = _state_snapshot(state)
        with lock:
            records = tuple(record for record in records if record[0]() is not None)
            matches = tuple(record for record in records if record[0]() is state)
            if len(matches) != 1:
                raise ContractViolation("relationship_state_not_canonical")
            if matches[0][1] != current:
                raise ContractViolation("relationship_state_corrupt")
            return state

    return mint, inspect


_mint_relationship_state, inspect_relationship_state = _build_state_vault()
del _build_state_vault


@dataclass(frozen=True, slots=True, repr=False)
class RelationshipMutationResult:
    status: RelationshipMutationStatus
    reason: RelationshipRejectReason | None
    state_changed: bool
    state: RelationshipState = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not RelationshipMutationStatus:
            raise ContractViolation("relationship_mutation_status_invalid")
        if self.reason is not None and type(self.reason) is not RelationshipRejectReason:
            raise ContractViolation("relationship_mutation_reason_invalid")
        if type(self.state_changed) is not bool or type(self.state) is not RelationshipState:
            raise ContractViolation("relationship_mutation_state_invalid")
        inspect_relationship_state(self.state)
        if self.status is RelationshipMutationStatus.REJECTED:
            if self.reason is None or self.state_changed:
                raise ContractViolation("relationship_rejection_invalid")
        elif self.reason is not None or not self.state_changed:
            raise ContractViolation("relationship_acceptance_invalid")

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        return {
            "relationship_mutation_status": self.status.value,
            "relationship_reject_reason": self.reason.value if self.reason else "none",
            "relationship_state_changed": self.state_changed,
            **self.state.trace_metadata(),
        }


@dataclass(frozen=True, slots=True, repr=False)
class _AcceptedTarget:
    scope_key: str
    session_id: str
    message_id: str
    object_key: str
    content_digest: str
    trace_id: str
    conversation_revision: int


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False, weakref_slot=True)
class RelationshipRenderContext:
    binding: DecisionBinding = field(repr=False)
    band: RelationshipBand
    affinity: float
    explanation_reason_codes: tuple[str, ...]
    interaction_count: int
    successful_reply_count: int

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        try:
            inspect_relationship_render_context(self)
        except Exception:
            return {"relationship_render_canonical": False}
        return {
            "relationship_render_canonical": True,
            "relationship_band": self.band.value,
            "relationship_affinity": round(self.affinity, 3),
            "relationship_reason_count": len(self.explanation_reason_codes),
            "relationship_interaction_count": self.interaction_count,
            "relationship_successful_reply_count": self.successful_reply_count,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        if not metadata.get("relationship_render_canonical", False):
            return "RelationshipRenderContext(canonical=False)"
        return (
            "RelationshipRenderContext("
            f"band={metadata['relationship_band']!r}, "
            f"events={metadata['relationship_interaction_count']})"
        )


def _binding_snapshot(binding: DecisionBinding) -> tuple[object, ...]:
    if type(binding) is not DecisionBinding:
        raise ContractViolation("relationship_render_context_corrupt")
    try:
        snapshot = (
            binding.scope_key,
            binding.session_id,
            binding.current_message_id,
            binding.current_sender_key,
            binding.current_content_digest,
            binding.conversation_revision,
            binding.generation_epoch,
            binding.trace_id,
        )
    except (AttributeError, TypeError) as exc:
        raise ContractViolation("relationship_render_context_corrupt") from exc
    if (
        any(type(value) is not str for value in (*snapshot[:5], snapshot[7]))
        or type(snapshot[5]) is not int
        or type(snapshot[6]) is not int
    ):
        raise ContractViolation("relationship_render_context_corrupt")
    return snapshot


def _render_snapshot(context: RelationshipRenderContext) -> tuple[object, ...]:
    if type(context) is not RelationshipRenderContext:
        raise ContractViolation("relationship_render_context_not_canonical")
    try:
        snapshot = (
            context.binding,
            _binding_snapshot(context.binding),
            context.band,
            context.affinity,
            context.explanation_reason_codes,
            context.interaction_count,
            context.successful_reply_count,
        )
    except (AttributeError, TypeError) as exc:
        raise ContractViolation("relationship_render_context_corrupt") from exc
    if (
        type(snapshot[0]) is not DecisionBinding
        or type(snapshot[1]) is not tuple
        or type(snapshot[2]) is not RelationshipBand
        or type(snapshot[3]) is not float
        or type(snapshot[4]) is not tuple
        or any(type(value) is not str for value in snapshot[4])
        or type(snapshot[5]) is not int
        or type(snapshot[6]) is not int
    ):
        raise ContractViolation("relationship_render_context_corrupt")
    return snapshot


def _build_render_vault():
    lock = threading.RLock()
    records: tuple[
        tuple[
            weakref.ReferenceType[RelationshipRenderContext],
            tuple[object, ...],
            weakref.ReferenceType[RelationshipStateBook],
            RelationshipState,
            tuple[object, ...],
        ],
        ...,
    ] = ()

    def mint(
        *,
        binding: DecisionBinding,
        state: RelationshipState,
        book: "RelationshipStateBook",
    ) -> RelationshipRenderContext:
        nonlocal records
        context = object.__new__(RelationshipRenderContext)
        object.__setattr__(context, "binding", binding)
        object.__setattr__(context, "band", state.band)
        object.__setattr__(context, "affinity", state.affinity)
        object.__setattr__(
            context,
            "explanation_reason_codes",
            state.explanation_reason_codes,
        )
        object.__setattr__(context, "interaction_count", state.interaction_count)
        object.__setattr__(
            context,
            "successful_reply_count",
            state.successful_reply_count,
        )
        snapshot = _render_snapshot(context)
        state_snapshot = _state_snapshot(state)
        with lock:
            records = tuple(record for record in records if record[0]() is not None)
            if len(records) >= 512:
                raise ContractViolation("relationship_render_ledger_full")
            records = (
                *records,
                (weakref.ref(context), snapshot, weakref.ref(book), state, state_snapshot),
            )
        return context

    def inspect(context: RelationshipRenderContext) -> RelationshipRenderContext:
        current = _render_snapshot(context)
        with lock:
            live = tuple(record for record in records if record[0]() is not None)
            matches = tuple(record for record in live if record[0]() is context)
            if len(matches) != 1:
                raise ContractViolation("relationship_render_context_not_canonical")
            record = matches[0]
            if current != record[1]:
                raise ContractViolation("relationship_render_context_corrupt")
            book = record[2]()
            if book is None:
                raise ContractViolation("relationship_render_context_not_canonical")
            book._inspect_render_source(record[3], record[4], context.binding)
            return context

    return mint, inspect


_mint_relationship_render_context, inspect_relationship_render_context = (
    _build_render_vault()
)


class RelationshipStateBook:
    """Sender-isolated relationship history driven only by concrete events."""

    __slots__ = (
        "__weakref__",
        "_accepted_turn_authority",
        "_lock",
        "_max_receipts_per_scope",
        "_max_subjects",
        "_receipt_ids",
        "_state_snapshots",
        "_states",
        "_targets",
    )

    def __init__(
        self,
        *,
        accepted_turn_authority: AcceptedTurnAuthority,
        max_subjects: int = 512,
        max_receipts_per_scope: int = 64,
    ) -> None:
        if type(accepted_turn_authority) is not AcceptedTurnAuthority:
            raise ContractViolation("relationship_turn_authority_required")
        if type(max_subjects) is not int or not 1 <= max_subjects <= 10_000:
            raise ContractViolation("relationship_subject_limit_invalid")
        if (
            type(max_receipts_per_scope) is not int
            or not 1 <= max_receipts_per_scope <= 10_000
        ):
            raise ContractViolation("relationship_receipt_limit_invalid")
        self._accepted_turn_authority = accepted_turn_authority
        self._max_subjects = max_subjects
        self._max_receipts_per_scope = max_receipts_per_scope
        self._states: dict[tuple[str, str], RelationshipState] = {}
        self._state_snapshots: dict[tuple[str, str], tuple[object, ...]] = {}
        self._targets: dict[str, tuple[_AcceptedTarget, ...]] = {}
        self._receipt_ids: dict[str, tuple[str, ...]] = {}
        self._lock = threading.RLock()

    @property
    def state_count(self) -> int:
        with self._lock:
            return len(self._states)

    def get_state(self, scope_key: str, object_key: str) -> RelationshipState | None:
        if type(scope_key) is not str or not scope_key:
            raise ContractViolation("relationship_scope_invalid")
        if type(object_key) is not str or not object_key:
            raise ContractViolation("relationship_object_invalid")
        with self._lock:
            state = self._states.get((scope_key, object_key))
            if state is not None and self._state_snapshots.get(
                (scope_key, object_key)
            ) != _state_snapshot(state):
                raise ContractViolation("relationship_state_corrupt")
            return state

    def _inspect_render_source(
        self,
        state: RelationshipState,
        snapshot: tuple[object, ...],
        binding: DecisionBinding,
    ) -> None:
        with self._lock:
            key = (binding.scope_key, binding.current_sender_key)
            if (
                self._states.get(key) is not state
                or self._state_snapshots.get(key) != snapshot
                or _state_snapshot(state) != snapshot
                or state.conversation_revision != binding.conversation_revision
            ):
                raise ContractViolation("relationship_render_source_not_current")

    @staticmethod
    def _event_for_trigger(trigger: AffectTrigger) -> RelationshipEventKind:
        if trigger in _POSITIVE_TRIGGERS:
            return RelationshipEventKind.POSITIVE_INTERACTION
        if trigger in _NEGATIVE_TRIGGERS:
            return RelationshipEventKind.NEGATIVE_INTERACTION
        return RelationshipEventKind.INTERACTION

    @staticmethod
    def _next_state(
        current: RelationshipState | None,
        *,
        scope_key: str,
        object_key: str,
        event_kind: RelationshipEventKind,
        now: float,
        revision: int,
    ) -> RelationshipState:
        affinity = current.affinity if current is not None else 0.0
        interaction_count = current.interaction_count if current is not None else 0
        positive = current.positive_event_count if current is not None else 0
        negative = current.negative_event_count if current is not None else 0
        replies = current.successful_reply_count if current is not None else 0
        reasons = current.explanation_reason_codes if current is not None else ()
        if event_kind is RelationshipEventKind.POSITIVE_INTERACTION:
            affinity = min(1.0, affinity + 0.18)
            positive += 1
            interaction_count += 1
        elif event_kind is RelationshipEventKind.NEGATIVE_INTERACTION:
            affinity = max(-1.0, affinity - 0.25)
            negative += 1
            interaction_count += 1
        elif event_kind is RelationshipEventKind.INTERACTION:
            interaction_count += 1
        elif event_kind is RelationshipEventKind.SUCCESSFUL_REPLY:
            replies += 1
        reason_codes = (*reasons, event_kind.value)[-4:]
        return _mint_relationship_state(
            scope_key=scope_key,
            object_key=object_key,
            affinity=float(affinity),
            band=_band(float(affinity)),
            interaction_count=interaction_count,
            positive_event_count=positive,
            negative_event_count=negative,
            successful_reply_count=replies,
            explanation_reason_codes=reason_codes,
            updated_at=now,
            version=(current.version + 1 if current is not None else 1),
            conversation_revision=revision,
        )

    def record_human(
        self,
        ticket: AcceptedTurnTicket,
        *,
        current_message: str,
        now: float,
    ) -> RelationshipMutationResult:
        if type(ticket) is not AcceptedTurnTicket:
            raise ContractViolation("relationship_ticket_required")
        current_time = _clock(now)
        message = current_message if type(current_message) is str else ""
        context = self._accepted_turn_authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.RELATIONSHIP_STATE,
            binding=ticket.binding,
        )
        if ledger_content_digest(message) != context.binding.current_content_digest:
            raise ContractViolation("relationship_message_digest_mismatch")
        envelope = context.envelope
        target = ReplyTarget(
            message_id=envelope.message_id,
            sender_key=envelope.sender_key,
            session_id=envelope.session_id,
            scope_key=envelope.scope_key,
            content_digest=context.content_digest,
            source_kind="current_inbound",
            referenced_message_id=envelope.reply_to_message_id,
            degradation_reasons=(),
        )
        appraisal = appraise_affect(
            principal=context.principal,
            reply_target=target,
            current_message=message,
            conversation_mode="direct_reply",
        )
        if not appraisal.is_actionable:
            raise ContractViolation("relationship_appraisal_unavailable")
        event_kind = self._event_for_trigger(appraisal.trigger)
        key = (context.binding.scope_key, context.binding.current_sender_key)
        with self._lock:
            current = self._states.get(key)
            if current is not None:
                if current_time < current.updated_at:
                    return RelationshipMutationResult(
                        RelationshipMutationStatus.REJECTED,
                        RelationshipRejectReason.CLOCK_STALE,
                        False,
                        current,
                    )
                if context.binding.conversation_revision <= current.conversation_revision:
                    return RelationshipMutationResult(
                        RelationshipMutationStatus.REJECTED,
                        RelationshipRejectReason.REVISION_STALE,
                        False,
                        current,
                    )
            if current is None and len(self._states) >= self._max_subjects:
                raise ContractViolation("relationship_state_ledger_full")
            next_state = self._next_state(
                current,
                scope_key=key[0],
                object_key=key[1],
                event_kind=event_kind,
                now=current_time,
                revision=context.binding.conversation_revision,
            )
            self._accepted_turn_authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.RELATIONSHIP_STATE,
                binding=context.binding,
                context=context,
            )
            self._states[key] = next_state
            self._state_snapshots[key] = _state_snapshot(next_state)
            target_record = _AcceptedTarget(
                scope_key=key[0],
                session_id=context.binding.session_id,
                message_id=context.binding.current_message_id,
                object_key=key[1],
                content_digest=context.binding.current_content_digest,
                trace_id=context.binding.trace_id,
                conversation_revision=context.binding.conversation_revision,
            )
            self._targets[key[0]] = (
                *self._targets.get(key[0], ()),
                target_record,
            )[-self._max_subjects :]
            return RelationshipMutationResult(
                RelationshipMutationStatus.ACCEPTED_HUMAN,
                None,
                True,
                next_state,
            )

    @staticmethod
    def _successful_receipt(receipt: SentReplyRecord) -> bool:
        return bool(
            type(receipt) is SentReplyRecord
            and receipt.is_terminal
            and receipt.segments
            and len(receipt.successful_segments) == len(receipt.segments)
            and receipt.sent_at > 0
            and receipt.target_source_kind == "current_inbound"
        )

    def record_shio_receipt(
        self,
        evidence: ShioReceiptEvidence,
        *,
        now: float,
    ) -> RelationshipMutationResult:
        if type(evidence) is not ShioReceiptEvidence:
            raise ContractViolation("relationship_receipt_evidence_required")
        inspect_shio_receipt_evidence(evidence)
        receipt = evidence.receipt
        current_time = _clock(now)
        with self._lock:
            scope = receipt.scope_key
            reply_ids = self._receipt_ids.get(scope, ())
            if receipt.reply_id in reply_ids:
                state = self._states.get((scope, receipt.target_sender_key))
                if state is None:
                    raise ContractViolation("relationship_receipt_state_missing")
                return RelationshipMutationResult(
                    RelationshipMutationStatus.REJECTED,
                    RelationshipRejectReason.RECEIPT_REPLAYED,
                    False,
                    state,
                )
            if not self._successful_receipt(receipt):
                raise ContractViolation("relationship_receipt_unverified")
            target = next(
                (
                    candidate
                    for candidate in reversed(self._targets.get(scope, ()))
                    if candidate.session_id == receipt.session_id
                    and candidate.message_id == receipt.target_message_id
                    and candidate.object_key == receipt.target_sender_key
                    and candidate.content_digest == receipt.target_content_digest
                    and candidate.trace_id == receipt.trace_id
                ),
                None,
            )
            if target is None:
                raise ContractViolation("relationship_receipt_target_mismatch")
            key = (scope, target.object_key)
            current = self._states.get(key)
            if current is None or current.conversation_revision != target.conversation_revision:
                raise ContractViolation("relationship_receipt_target_mismatch")
            if current_time < current.updated_at or current_time < receipt.sent_at:
                return RelationshipMutationResult(
                    RelationshipMutationStatus.REJECTED,
                    RelationshipRejectReason.CLOCK_STALE,
                    False,
                    current,
                )
            next_state = self._next_state(
                current,
                scope_key=key[0],
                object_key=key[1],
                event_kind=RelationshipEventKind.SUCCESSFUL_REPLY,
                now=current_time,
                revision=current.conversation_revision,
            )
            self._states[key] = next_state
            self._state_snapshots[key] = _state_snapshot(next_state)
            self._receipt_ids[scope] = (*reply_ids, receipt.reply_id)[
                -self._max_receipts_per_scope :
            ]
            return RelationshipMutationResult(
                RelationshipMutationStatus.ACCEPTED_OUTBOUND,
                None,
                True,
                next_state,
            )

    def issue_render_context(
        self,
        mutation: RelationshipMutationResult,
        *,
        binding: DecisionBinding,
    ) -> RelationshipRenderContext:
        if type(mutation) is not RelationshipMutationResult:
            raise ContractViolation("relationship_mutation_required")
        if type(binding) is not DecisionBinding:
            raise ContractViolation("relationship_render_binding_required")
        if (
            mutation.status is not RelationshipMutationStatus.ACCEPTED_HUMAN
            or not mutation.state_changed
            or mutation.reason is not None
        ):
            raise ContractViolation("relationship_mutation_not_renderable")
        state = mutation.state
        key = (binding.scope_key, binding.current_sender_key)
        with self._lock:
            snapshot = _state_snapshot(state)
            if (
                self._states.get(key) is not state
                or self._state_snapshots.get(key) != snapshot
                or state.conversation_revision != binding.conversation_revision
            ):
                raise ContractViolation("relationship_render_source_not_current")
            return _mint_relationship_render_context(
                binding=binding,
                state=state,
                book=self,
            )

    def trace_metadata(self) -> dict[str, int]:
        with self._lock:
            return {
                "relationship_state_count": len(self._states),
                "relationship_target_count": sum(
                    len(values) for values in self._targets.values()
                ),
                "relationship_receipt_count": sum(
                    len(values) for values in self._receipt_ids.values()
                ),
            }


__all__ = [
    "RelationshipBand",
    "RelationshipEventKind",
    "RelationshipMutationResult",
    "RelationshipMutationStatus",
    "RelationshipRejectReason",
    "RelationshipRenderContext",
    "RelationshipState",
    "RelationshipStateBook",
    "inspect_relationship_render_context",
    "inspect_relationship_state",
]
