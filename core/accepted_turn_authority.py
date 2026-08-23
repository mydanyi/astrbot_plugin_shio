from __future__ import annotations

import hashlib
import math
import secrets
import threading
from dataclasses import dataclass, field
from enum import Enum

from .contracts import ContractViolation, DecisionBinding
from .conversation_event import ConversationEvent, PluginSource, SenderKind
from .identity import PrincipalContext, TurnEnvelope
from .ingress_admission import (
    AdmissionProof,
    AdmissionResult,
    IngressAdmissionController,
)


_TICKET_SEAL = object()
_DISPATCH_SEAL = object()
_CONTEXT_SEAL = object()
_FINALIZATION_SEAL = object()
_DEFAULT_MAX_TURNS = 256


class AcceptedTurnConsumer(str, Enum):
    """Closed code-owned consumers of one accepted ingress turn."""

    OWNER_ACTION = "owner_action"
    AFFECT_STATE = "affect_state"
    RELATIONSHIP_STATE = "relationship_state"
    OPPORTUNITY_ATTENTION = "opportunity_attention"


class AcceptedTurnDisposition(str, Enum):
    """Code-owned terminal reasons for a consumer that never claimed its ticket."""

    SKIPPED = "skipped"
    ABORTED = "aborted"
    REJECTED = "rejected"


_FIXED_CONSUMERS: tuple[AcceptedTurnConsumer, ...] = (
    AcceptedTurnConsumer.OWNER_ACTION,
    AcceptedTurnConsumer.AFFECT_STATE,
    AcceptedTurnConsumer.RELATIONSHIP_STATE,
    AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
)
ACCEPTED_TURN_CONSUMERS: tuple[AcceptedTurnConsumer, ...] = _FIXED_CONSUMERS


def _digest_parts(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _exact_parts_match(
    current: tuple[object, ...],
    snapshot: tuple[object, ...],
) -> bool:
    """Compare issuer snapshots without Python's cross-type equality aliases."""

    return len(current) == len(snapshot) and all(
        type(current_part) is type(snapshot_part)
        and current_part == snapshot_part
        for current_part, snapshot_part in zip(current, snapshot)
    )


def _exact_value_matches(current: object, snapshot: object) -> bool:
    return type(current) is type(snapshot) and current == snapshot


def _binding_parts(binding: DecisionBinding) -> tuple[object, ...]:
    if type(binding) is not DecisionBinding:
        raise ContractViolation("accepted_turn_binding_invalid")
    try:
        parts = (
            binding.scope_key,
            binding.session_id,
            binding.current_message_id,
            binding.current_sender_key,
            binding.current_content_digest,
            binding.conversation_revision,
            binding.generation_epoch,
            binding.trace_id,
        )
    except AttributeError as exc:
        raise ContractViolation("accepted_turn_binding_invalid") from exc
    if (
        any(type(value) is not str for value in (*parts[:5], parts[7]))
        or type(parts[5]) is not int
        or type(parts[6]) is not int
    ):
        raise ContractViolation("accepted_turn_binding_invalid")
    return parts


def _envelope_parts(envelope: TurnEnvelope) -> tuple[object, ...]:
    if type(envelope) is not TurnEnvelope:
        raise ContractViolation("accepted_turn_envelope_invalid")
    try:
        parts = (
            envelope.session_id,
            envelope.message_id,
            envelope.scope_key,
            envelope.sender_key,
            envelope.sender_id,
            envelope.platform_id,
            envelope.bot_id,
            envelope.chat_type,
            envelope.group_id,
            envelope.reply_to_message_id,
            envelope.reply_to_sender_id,
            envelope.timestamp,
            envelope.timestamp_source,
            envelope.source_kind,
            envelope.degradation_reasons,
        )
    except AttributeError as exc:
        raise ContractViolation("accepted_turn_envelope_invalid") from exc
    if (
        any(type(value) is not str for value in (*parts[:11], *parts[12:14]))
        or type(parts[11]) not in (int, float)
        or (type(parts[11]) is float and not math.isfinite(parts[11]))
        or type(parts[14]) is not tuple
        or any(type(reason) is not str for reason in parts[14])
    ):
        raise ContractViolation("accepted_turn_envelope_invalid")
    return parts


def _principal_parts(principal: PrincipalContext) -> tuple[object, ...]:
    if type(principal) is not PrincipalContext:
        raise ContractViolation("accepted_turn_principal_invalid")
    try:
        parts = (
            principal.sender_key,
            principal.sender_id,
            principal.is_owner,
            principal.relationship_role,
            principal.verification_source,
        )
    except AttributeError as exc:
        raise ContractViolation("accepted_turn_principal_invalid") from exc
    if (
        type(parts[0]) is not str
        or type(parts[1]) is not str
        or type(parts[2]) is not bool
        or type(parts[3]) is not str
        or type(parts[4]) is not str
    ):
        raise ContractViolation("accepted_turn_principal_invalid")
    return parts


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class AcceptedTurnContext:
    """Content-free canonical projection of one controller-accepted human turn.

    Consumers never authenticate a caller-provided ``ConversationEvent`` or
    ``AdmissionResult`` by replaying public field checks.  This object is built
    once from the controller-canonical admission before its proof is consumed,
    then exact-object bound to every child ticket.
    """

    binding: DecisionBinding = field(repr=False)
    conversation_event: ConversationEvent = field(repr=False)
    envelope: TurnEnvelope = field(repr=False)
    principal: PrincipalContext = field(repr=False)
    sender_kind: SenderKind
    plugin_source: PluginSource
    content_digest: str = field(repr=False)
    _issuer_seal: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        sender_kind = getattr(self, "sender_kind", None)
        plugin_source = getattr(self, "plugin_source", None)
        binding = getattr(self, "binding", None)
        revision = (
            getattr(binding, "conversation_revision", None)
            if type(binding) is DecisionBinding
            else None
        )
        return {
            "schema_version": 1,
            "accepted_turn_context_bound": True,
            "sender_kind": (
                sender_kind.value
                if type(sender_kind) is SenderKind
                else "invalid"
            ),
            "plugin_source": (
                plugin_source.value
                if type(plugin_source) is PluginSource
                else "invalid"
            ),
            "conversation_revision": (
                revision
                if type(revision) is int and revision >= 1
                else 0
            ),
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "AcceptedTurnContext("
            f"sender_kind={metadata['sender_kind']!r}, "
            f"plugin_source={metadata['plugin_source']!r}, "
            f"conversation_revision={metadata['conversation_revision']}, "
            "binding_bound=True)"
        )


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class AcceptedTurnTicket:
    """Opaque one-consumer authority token; only its issuer may claim it."""

    binding: DecisionBinding = field(repr=False)
    consumer: AcceptedTurnConsumer
    conversation_revision: int
    dispatch_digest: str = field(repr=False)
    ticket_digest: str = field(repr=False)
    _issuer_seal: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        current_consumer = getattr(self, "consumer", None)
        current_revision = getattr(self, "conversation_revision", None)
        dispatch_digest = getattr(self, "dispatch_digest", None)
        ticket_digest = getattr(self, "ticket_digest", None)
        consumer = (
            current_consumer.value
            if type(current_consumer) is AcceptedTurnConsumer
            else "invalid"
        )
        revision = (
            current_revision
            if type(current_revision) is int and current_revision >= 1
            else 0
        )
        return {
            "schema_version": 1,
            "accepted_turn_consumer": consumer,
            "accepted_turn_binding_bound": True,
            "accepted_turn_dispatch_bound": (
                type(dispatch_digest) is str and bool(dispatch_digest)
            ),
            "accepted_turn_ticket_bound": (
                type(ticket_digest) is str and bool(ticket_digest)
            ),
            "conversation_revision": revision,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "AcceptedTurnTicket("
            f"consumer={metadata['accepted_turn_consumer']!r}, "
            f"conversation_revision={metadata['conversation_revision']}, "
            "binding_bound=True)"
        )


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class AcceptedTurnDispatch:
    """Atomic, complete ticket set for one controller-canonical accepted turn."""

    binding: DecisionBinding = field(repr=False)
    tickets: tuple[AcceptedTurnTicket, ...] = field(repr=False)
    conversation_revision: int
    dispatch_digest: str = field(repr=False)
    _issuer_seal: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, int | bool]:
        current_tickets = getattr(self, "tickets", None)
        current_revision = getattr(self, "conversation_revision", None)
        dispatch_digest = getattr(self, "dispatch_digest", None)
        ticket_count = (
            len(current_tickets)
            if type(current_tickets) is tuple
            and all(type(ticket) is AcceptedTurnTicket for ticket in current_tickets)
            else 0
        )
        revision = (
            current_revision
            if type(current_revision) is int
            and current_revision >= 1
            else 0
        )
        return {
            "schema_version": 1,
            "accepted_turn_binding_bound": True,
            "accepted_turn_dispatch_bound": (
                type(dispatch_digest) is str and bool(dispatch_digest)
            ),
            "accepted_turn_ticket_count": ticket_count,
            "conversation_revision": revision,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "AcceptedTurnDispatch("
            f"ticket_count={metadata['accepted_turn_ticket_count']}, "
            f"conversation_revision={metadata['conversation_revision']}, "
            "binding_bound=True)"
        )


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class AcceptedTurnFinalization:
    """Opaque receipt proving one exact unclaimed consumer ticket was terminalized."""

    consumer: AcceptedTurnConsumer
    disposition: AcceptedTurnDisposition
    conversation_revision: int
    finalization_digest: str = field(repr=False)
    _issuer_seal: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        current_consumer = getattr(self, "consumer", None)
        current_disposition = getattr(self, "disposition", None)
        current_revision = getattr(self, "conversation_revision", None)
        finalization_digest = getattr(self, "finalization_digest", None)
        return {
            "schema_version": 1,
            "accepted_turn_consumer": (
                current_consumer.value
                if type(current_consumer) is AcceptedTurnConsumer
                else "invalid"
            ),
            "accepted_turn_disposition": (
                current_disposition.value
                if type(current_disposition) is AcceptedTurnDisposition
                else "invalid"
            ),
            "accepted_turn_finalized": (
                type(finalization_digest) is str and bool(finalization_digest)
            ),
            "conversation_revision": (
                current_revision
                if type(current_revision) is int
                and current_revision >= 1
                else 0
            ),
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "AcceptedTurnFinalization("
            f"consumer={metadata['accepted_turn_consumer']!r}, "
            f"disposition={metadata['accepted_turn_disposition']!r}, "
            f"conversation_revision={metadata['conversation_revision']})"
        )


@dataclass(slots=True, repr=False)
class _TicketRecord:
    ticket: AcceptedTurnTicket
    consumer: AcceptedTurnConsumer
    binding: DecisionBinding
    binding_parts: tuple[object, ...]
    context: AcceptedTurnContext
    context_event: ConversationEvent
    context_ingress: object
    context_envelope: TurnEnvelope
    context_envelope_parts: tuple[object, ...]
    context_principal: PrincipalContext
    context_principal_parts: tuple[object, ...]
    context_sender_kind: SenderKind
    context_plugin_source: PluginSource
    context_content_digest: str
    dispatch_digest: str
    ticket_digest: str
    claimed: bool = False
    finalization: AcceptedTurnFinalization | None = None

    @property
    def terminal(self) -> bool:
        return self.claimed or self.finalization is not None


@dataclass(slots=True, repr=False)
class _TurnRecord:
    dispatch: AcceptedTurnDispatch
    binding: DecisionBinding
    binding_parts: tuple[object, ...]
    context: AcceptedTurnContext
    dispatch_digest: str
    ticket_records: tuple[_TicketRecord, ...]

    @property
    def complete(self) -> bool:
        return all(record.terminal for record in self.ticket_records)


@dataclass(slots=True, repr=False)
class _FinalizationRecord:
    finalization: AcceptedTurnFinalization
    ticket_record: _TicketRecord
    consumer: AcceptedTurnConsumer
    disposition: AcceptedTurnDisposition
    conversation_revision: int
    finalization_digest: str


class AcceptedTurnAuthority:
    """Claim one admission proof and fan it out to a fixed consumer set.

    This layer decides neither identity nor policy.  It only transforms one active,
    controller-canonical ``AdmissionProof`` into a complete fixed set of exact,
    independently one-shot tickets.
    """

    __slots__ = (
        "_admission_controller",
        "_authority_nonce",
        "_dispatches",
        "_finalizations",
        "_issuer_seal",
        "_lock",
        "_max_turns",
        "_tickets",
    )

    def __init__(
        self,
        admission_controller: IngressAdmissionController,
        *,
        max_turns: int = _DEFAULT_MAX_TURNS,
    ) -> None:
        if type(admission_controller) is not IngressAdmissionController:
            raise ContractViolation("accepted_turn_controller_required")
        if type(max_turns) is not int or max_turns < 1:
            raise ContractViolation("accepted_turn_ledger_limit_invalid")
        self._admission_controller = admission_controller
        self._max_turns = max_turns
        self._authority_nonce = secrets.token_hex(32)
        self._issuer_seal = object()
        self._dispatches: dict[int, _TurnRecord] = {}
        self._tickets: dict[int, _TicketRecord] = {}
        self._finalizations: dict[int, _FinalizationRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _accepted_binding(
        admission: AdmissionResult,
        proof: AdmissionProof,
    ) -> DecisionBinding:
        if type(proof) is not AdmissionProof:
            raise ContractViolation("accepted_turn_admission_invalid")
        try:
            event = admission.conversation_event
            event_binding = event.binding if event is not None else None
            decision_binding = admission.decision.binding
            event_binding_parts = _binding_parts(event_binding)
            decision_binding_parts = _binding_parts(decision_binding)
            proof_revision = proof.conversation_revision
            allows_state_mutation = admission.decision.allows_state_mutation
        except (AttributeError, ContractViolation) as exc:
            raise ContractViolation("accepted_turn_admission_invalid") from exc
        if (
            admission.admission_proof is not proof
            or event is None
            or event.ingress is not admission.ingress_event
            or not _exact_parts_match(
                event_binding_parts,
                decision_binding_parts,
            )
            or not _exact_value_matches(
                proof_revision,
                event_binding_parts[5],
            )
            or allows_state_mutation is not True
        ):
            raise ContractViolation("accepted_turn_admission_invalid")
        return event_binding

    def _eviction_keys_for_new_turn(self) -> tuple[int, ...]:
        projected = len(self._dispatches)
        keys: list[int] = []
        for key, record in self._dispatches.items():
            if projected < self._max_turns:
                break
            if record.complete:
                keys.append(key)
                projected -= 1
        if projected >= self._max_turns:
            raise ContractViolation("accepted_turn_ledger_full")
        return tuple(keys)

    def _build_turn(
        self,
        *,
        admission: AdmissionResult,
        binding: DecisionBinding,
        proof: AdmissionProof,
    ) -> _TurnRecord:
        binding_parts = _binding_parts(binding)
        event = admission.conversation_event
        if event is None:
            raise ContractViolation("accepted_turn_event_required")
        try:
            envelope = event.envelope
            principal = event.principal
            sender_kind = event.sender_kind
            plugin_source = event.plugin_source
            content_digest = event.content_digest
            proof_commit_digest = proof.commit_digest
        except AttributeError as exc:
            raise ContractViolation("accepted_turn_event_invalid") from exc
        envelope_parts = _envelope_parts(envelope)
        principal_parts = _principal_parts(principal)
        if type(sender_kind) is not SenderKind:
            raise ContractViolation("accepted_turn_sender_kind_invalid")
        if type(plugin_source) is not PluginSource:
            raise ContractViolation("accepted_turn_plugin_source_invalid")
        if (
            type(content_digest) is not str
            or not content_digest
            or not _exact_value_matches(content_digest, binding_parts[4])
        ):
            raise ContractViolation("accepted_turn_content_digest_invalid")
        if type(proof_commit_digest) is not str or not proof_commit_digest:
            raise ContractViolation("accepted_turn_proof_digest_invalid")
        context = object.__new__(AcceptedTurnContext)
        object.__setattr__(context, "binding", binding)
        object.__setattr__(context, "conversation_event", event)
        object.__setattr__(context, "envelope", envelope)
        object.__setattr__(context, "principal", principal)
        object.__setattr__(context, "sender_kind", sender_kind)
        object.__setattr__(context, "plugin_source", plugin_source)
        object.__setattr__(context, "content_digest", content_digest)
        object.__setattr__(context, "_issuer_seal", self._issuer_seal)
        object.__setattr__(context, "_seal", _CONTEXT_SEAL)
        dispatch_digest = _digest_parts(
            "accepted-turn-dispatch-v1",
            self._authority_nonce,
            proof_commit_digest,
            *binding_parts,
            *(consumer.value for consumer in _FIXED_CONSUMERS),
        )
        ticket_records: list[_TicketRecord] = []
        tickets: list[AcceptedTurnTicket] = []
        for consumer in _FIXED_CONSUMERS:
            ticket_digest = _digest_parts(
                "accepted-turn-ticket-v1",
                self._authority_nonce,
                dispatch_digest,
                consumer.value,
                *binding_parts,
            )
            ticket = object.__new__(AcceptedTurnTicket)
            object.__setattr__(ticket, "binding", binding)
            object.__setattr__(ticket, "consumer", consumer)
            object.__setattr__(
                ticket,
                "conversation_revision",
                binding_parts[5],
            )
            object.__setattr__(ticket, "dispatch_digest", dispatch_digest)
            object.__setattr__(ticket, "ticket_digest", ticket_digest)
            object.__setattr__(ticket, "_issuer_seal", self._issuer_seal)
            object.__setattr__(ticket, "_seal", _TICKET_SEAL)
            tickets.append(ticket)
            ticket_records.append(
                _TicketRecord(
                    ticket=ticket,
                    consumer=consumer,
                    binding=binding,
                    binding_parts=binding_parts,
                    context=context,
                    context_event=event,
                    context_ingress=event.ingress,
                    context_envelope=envelope,
                    context_envelope_parts=envelope_parts,
                    context_principal=principal,
                    context_principal_parts=principal_parts,
                    context_sender_kind=sender_kind,
                    context_plugin_source=plugin_source,
                    context_content_digest=content_digest,
                    dispatch_digest=dispatch_digest,
                    ticket_digest=ticket_digest,
                )
            )
        dispatch = object.__new__(AcceptedTurnDispatch)
        object.__setattr__(dispatch, "binding", binding)
        object.__setattr__(dispatch, "tickets", tuple(tickets))
        object.__setattr__(
            dispatch,
            "conversation_revision",
            binding_parts[5],
        )
        object.__setattr__(dispatch, "dispatch_digest", dispatch_digest)
        object.__setattr__(dispatch, "_issuer_seal", self._issuer_seal)
        object.__setattr__(dispatch, "_seal", _DISPATCH_SEAL)
        return _TurnRecord(
            dispatch=dispatch,
            binding=binding,
            binding_parts=binding_parts,
            context=context,
            dispatch_digest=dispatch_digest,
            ticket_records=tuple(ticket_records),
        )

    def dispatch(self, admission: AdmissionResult) -> AcceptedTurnDispatch:
        """Atomically consume one proof and publish the full fixed ticket set."""

        if type(admission) is not AdmissionResult:
            raise ContractViolation("accepted_turn_admission_required")
        with self._lock:
            proof = self._admission_controller.inspect_admission_proof(admission)
            binding = self._accepted_binding(admission, proof)
            eviction_keys = self._eviction_keys_for_new_turn()
            prepared = self._build_turn(
                admission=admission,
                binding=binding,
                proof=proof,
            )

            # No authority state is published before this exact one-time claim.
            self._admission_controller.claim_admission_proof(admission)

            # Every remaining operation is private, deterministic registry mutation
            # under the same lock; callers can observe neither a partial eviction nor
            # a partial new ticket set.
            for key in eviction_keys:
                evicted = self._dispatches.pop(key)
                for ticket_record in evicted.ticket_records:
                    self._tickets.pop(id(ticket_record.ticket), None)
                    if ticket_record.finalization is not None:
                        self._finalizations.pop(
                            id(ticket_record.finalization),
                            None,
                        )
            self._dispatches[id(prepared.dispatch)] = prepared
            for ticket_record in prepared.ticket_records:
                self._tickets[id(ticket_record.ticket)] = ticket_record
            return prepared.dispatch

    def _canonical_dispatch(
        self,
        dispatch: AcceptedTurnDispatch,
    ) -> _TurnRecord:
        if type(dispatch) is not AcceptedTurnDispatch:
            raise ContractViolation("accepted_turn_dispatch_required")
        record = self._dispatches.get(id(dispatch))
        if record is None or record.dispatch is not dispatch:
            raise ContractViolation("accepted_turn_dispatch_not_canonical")
        expected_tickets = tuple(
            ticket_record.ticket for ticket_record in record.ticket_records
        )
        try:
            current_binding = getattr(dispatch, "binding", None)
            current_binding_parts = _binding_parts(current_binding)
        except ContractViolation as exc:
            raise ContractViolation("accepted_turn_dispatch_corrupt") from exc
        current_tickets = getattr(dispatch, "tickets", None)
        if (
            getattr(dispatch, "_issuer_seal", None) is not self._issuer_seal
            or getattr(dispatch, "_seal", None) is not _DISPATCH_SEAL
            or current_binding is not record.binding
            or not _exact_parts_match(
                current_binding_parts,
                record.binding_parts,
            )
            or not _exact_value_matches(
                getattr(dispatch, "dispatch_digest", None),
                record.dispatch_digest,
            )
            or not _exact_value_matches(
                getattr(dispatch, "conversation_revision", None),
                record.binding_parts[5],
            )
            or type(current_tickets) is not tuple
            or len(current_tickets) != len(expected_tickets)
            or any(
                current is not expected
                for current, expected in zip(current_tickets, expected_tickets)
            )
            or len(expected_tickets) != len(_FIXED_CONSUMERS)
        ):
            raise ContractViolation("accepted_turn_dispatch_corrupt")
        return record

    @staticmethod
    def _consumer(value: AcceptedTurnConsumer) -> AcceptedTurnConsumer:
        if type(value) is not AcceptedTurnConsumer:
            raise ContractViolation("accepted_turn_consumer_invalid")
        return value

    def ticket_for(
        self,
        dispatch: AcceptedTurnDispatch,
        consumer: AcceptedTurnConsumer,
    ) -> AcceptedTurnTicket:
        """Return one exact ticket from one exact complete dispatch object."""

        expected_consumer = self._consumer(consumer)
        with self._lock:
            record = self._canonical_dispatch(dispatch)
            matches = tuple(
                ticket_record.ticket
                for ticket_record in record.ticket_records
                if ticket_record.consumer is expected_consumer
            )
            if len(matches) != 1:
                raise ContractViolation("accepted_turn_ticket_set_corrupt")
            return matches[0]

    def _canonical_ticket_record(
        self,
        ticket: AcceptedTurnTicket,
        *,
        consumer: AcceptedTurnConsumer,
    ) -> _TicketRecord:
        expected_consumer = self._consumer(consumer)
        if type(ticket) is not AcceptedTurnTicket:
            raise ContractViolation("accepted_turn_ticket_required")
        record = self._tickets.get(id(ticket))
        if record is None or record.ticket is not ticket:
            raise ContractViolation("accepted_turn_ticket_not_canonical")
        if expected_consumer is not record.consumer:
            raise ContractViolation("accepted_turn_ticket_consumer_mismatch")
        context = record.context
        try:
            ticket_binding = getattr(ticket, "binding", None)
            context_binding = getattr(context, "binding", None)
            context_event = getattr(context, "conversation_event", None)
            context_envelope = getattr(context, "envelope", None)
            context_principal = getattr(context, "principal", None)
            ticket_binding_parts = _binding_parts(ticket_binding)
            context_binding_parts = _binding_parts(context_binding)
            context_envelope_parts = _envelope_parts(context_envelope)
            context_principal_parts = _principal_parts(context_principal)
        except ContractViolation as exc:
            raise ContractViolation("accepted_turn_ticket_corrupt") from exc
        if (
            getattr(ticket, "_issuer_seal", None) is not self._issuer_seal
            or getattr(ticket, "_seal", None) is not _TICKET_SEAL
            or getattr(ticket, "consumer", None) is not record.consumer
            or ticket_binding is not record.binding
            or not _exact_parts_match(
                ticket_binding_parts,
                record.binding_parts,
            )
            or not _exact_value_matches(
                getattr(ticket, "dispatch_digest", None),
                record.dispatch_digest,
            )
            or not _exact_value_matches(
                getattr(ticket, "ticket_digest", None),
                record.ticket_digest,
            )
            or not _exact_value_matches(
                getattr(ticket, "conversation_revision", None),
                record.binding_parts[5],
            )
            or type(context) is not AcceptedTurnContext
            or getattr(context, "_issuer_seal", None) is not self._issuer_seal
            or getattr(context, "_seal", None) is not _CONTEXT_SEAL
            or context_binding is not record.binding
            or not _exact_parts_match(
                context_binding_parts,
                record.binding_parts,
            )
            or type(context_event) is not ConversationEvent
            or context_event is not record.context_event
            or getattr(context_event, "ingress", None) is not record.context_ingress
            or getattr(context_event, "binding", None) is not record.binding
            or getattr(context_event, "envelope", None) is not record.context_envelope
            or getattr(context_event, "principal", None) is not record.context_principal
            or getattr(context_event, "sender_kind", None)
            is not record.context_sender_kind
            or getattr(context_event, "plugin_source", None)
            is not record.context_plugin_source
            or not _exact_value_matches(
                getattr(context_event, "content_digest", None),
                record.context_content_digest,
            )
            or context_envelope is not record.context_envelope
            or not _exact_parts_match(
                context_envelope_parts,
                record.context_envelope_parts,
            )
            or context_principal is not record.context_principal
            or not _exact_parts_match(
                context_principal_parts,
                record.context_principal_parts,
            )
            or getattr(context, "sender_kind", None)
            is not record.context_sender_kind
            or getattr(context, "plugin_source", None)
            is not record.context_plugin_source
            or not _exact_value_matches(
                getattr(context, "content_digest", None),
                record.context_content_digest,
            )
            or not _exact_value_matches(
                getattr(context, "content_digest", None),
                record.binding_parts[4],
            )
        ):
            raise ContractViolation("accepted_turn_ticket_corrupt")
        return record

    def context_for(
        self,
        ticket: AcceptedTurnTicket,
        *,
        consumer: AcceptedTurnConsumer,
        binding: DecisionBinding,
    ) -> AcceptedTurnContext:
        """Return the issuer-owned identity projection for one exact ticket."""

        try:
            expected_binding_parts = _binding_parts(binding)
        except ContractViolation as exc:
            raise ContractViolation("accepted_turn_ticket_binding_mismatch") from exc
        with self._lock:
            record = self._canonical_ticket_record(ticket, consumer=consumer)
            if (
                binding is not record.binding
                or not _exact_parts_match(
                    expected_binding_parts,
                    record.binding_parts,
                )
            ):
                raise ContractViolation("accepted_turn_ticket_binding_mismatch")
            return record.context

    def claim_ticket(
        self,
        ticket: AcceptedTurnTicket,
        *,
        consumer: AcceptedTurnConsumer,
        binding: DecisionBinding,
        context: AcceptedTurnContext,
    ) -> AcceptedTurnTicket:
        """Claim one exact consumer ticket once for the same turn binding."""

        try:
            expected_binding_parts = _binding_parts(binding)
        except ContractViolation as exc:
            raise ContractViolation("accepted_turn_ticket_binding_mismatch") from exc
        if type(context) is not AcceptedTurnContext:
            raise ContractViolation("accepted_turn_context_required")
        with self._lock:
            record = self._canonical_ticket_record(ticket, consumer=consumer)
            if (
                binding is not record.binding
                or not _exact_parts_match(
                    expected_binding_parts,
                    record.binding_parts,
                )
            ):
                raise ContractViolation("accepted_turn_ticket_binding_mismatch")
            if context is not record.context:
                raise ContractViolation("accepted_turn_context_mismatch")
            if record.terminal:
                raise ContractViolation("accepted_turn_ticket_replayed")
            record.claimed = True
            return ticket

    @staticmethod
    def _disposition(
        value: AcceptedTurnDisposition,
    ) -> AcceptedTurnDisposition:
        if type(value) is not AcceptedTurnDisposition:
            raise ContractViolation("accepted_turn_disposition_invalid")
        return value

    def _build_finalization(
        self,
        record: _TicketRecord,
        disposition: AcceptedTurnDisposition,
    ) -> _FinalizationRecord:
        finalization_digest = _digest_parts(
            "accepted-turn-finalization-v1",
            self._authority_nonce,
            record.ticket_digest,
            disposition.value,
            *record.binding_parts,
        )
        finalization = object.__new__(AcceptedTurnFinalization)
        object.__setattr__(finalization, "consumer", record.consumer)
        object.__setattr__(finalization, "disposition", disposition)
        object.__setattr__(
            finalization,
            "conversation_revision",
            record.binding_parts[5],
        )
        object.__setattr__(
            finalization,
            "finalization_digest",
            finalization_digest,
        )
        object.__setattr__(finalization, "_issuer_seal", self._issuer_seal)
        object.__setattr__(finalization, "_seal", _FINALIZATION_SEAL)
        return _FinalizationRecord(
            finalization=finalization,
            ticket_record=record,
            consumer=record.consumer,
            disposition=disposition,
            conversation_revision=record.binding_parts[5],
            finalization_digest=finalization_digest,
        )

    def _publish_finalization(self, record: _FinalizationRecord) -> None:
        ticket_record = record.ticket_record
        if ticket_record.terminal:
            raise ContractViolation("accepted_turn_ticket_replayed")
        ticket_record.finalization = record.finalization
        self._finalizations[id(record.finalization)] = record

    def _rollback_finalizations(
        self,
        records: tuple[_FinalizationRecord, ...],
    ) -> None:
        for record in records:
            ticket_record = record.ticket_record
            if ticket_record.finalization is record.finalization:
                ticket_record.finalization = None
            retained = self._finalizations.get(id(record.finalization))
            if retained is record:
                self._finalizations.pop(id(record.finalization), None)

    def finalize_unclaimed_ticket(
        self,
        ticket: AcceptedTurnTicket,
        *,
        consumer: AcceptedTurnConsumer,
        binding: DecisionBinding,
        context: AcceptedTurnContext,
        disposition: AcceptedTurnDisposition,
    ) -> AcceptedTurnFinalization:
        """Terminalize one exact ticket only while it remains unclaimed."""

        terminal_disposition = self._disposition(disposition)
        try:
            expected_binding_parts = _binding_parts(binding)
        except ContractViolation as exc:
            raise ContractViolation("accepted_turn_ticket_binding_mismatch") from exc
        if type(context) is not AcceptedTurnContext:
            raise ContractViolation("accepted_turn_context_required")
        with self._lock:
            ticket_record = self._canonical_ticket_record(ticket, consumer=consumer)
            if (
                binding is not ticket_record.binding
                or not _exact_parts_match(
                    expected_binding_parts,
                    ticket_record.binding_parts,
                )
            ):
                raise ContractViolation("accepted_turn_ticket_binding_mismatch")
            if context is not ticket_record.context:
                raise ContractViolation("accepted_turn_context_mismatch")
            if ticket_record.terminal:
                raise ContractViolation("accepted_turn_ticket_replayed")
            prepared = self._build_finalization(
                ticket_record,
                terminal_disposition,
            )
            try:
                self._publish_finalization(prepared)
            except BaseException:
                self._rollback_finalizations((prepared,))
                raise
            return prepared.finalization

    def finalize_unclaimed_turn(
        self,
        dispatch: AcceptedTurnDispatch,
        *,
        binding: DecisionBinding,
        disposition: AcceptedTurnDisposition,
    ) -> tuple[AcceptedTurnFinalization, ...]:
        """Atomically terminalize every still-unclaimed ticket in one exact turn."""

        terminal_disposition = self._disposition(disposition)
        try:
            expected_binding_parts = _binding_parts(binding)
        except ContractViolation as exc:
            raise ContractViolation("accepted_turn_dispatch_binding_mismatch") from exc
        with self._lock:
            turn_record = self._canonical_dispatch(dispatch)
            if (
                binding is not turn_record.binding
                or not _exact_parts_match(
                    expected_binding_parts,
                    turn_record.binding_parts,
                )
            ):
                raise ContractViolation("accepted_turn_dispatch_binding_mismatch")
            unclaimed: list[_TicketRecord] = []
            for ticket_record in turn_record.ticket_records:
                canonical = self._canonical_ticket_record(
                    ticket_record.ticket,
                    consumer=ticket_record.consumer,
                )
                if canonical is not ticket_record:
                    raise ContractViolation("accepted_turn_ticket_set_corrupt")
                if not ticket_record.terminal:
                    unclaimed.append(ticket_record)
            if not unclaimed:
                raise ContractViolation("accepted_turn_turn_already_terminal")
            prepared = tuple(
                self._build_finalization(ticket_record, terminal_disposition)
                for ticket_record in unclaimed
            )
            try:
                for finalization_record in prepared:
                    self._publish_finalization(finalization_record)
            except BaseException:
                # Whole-turn finalization is a transaction.  Roll back every
                # prepared receipt, including the receipt whose publisher may
                # have failed after mutating one of the two registries.
                self._rollback_finalizations(prepared)
                raise
            return tuple(record.finalization for record in prepared)

    def inspect_finalization(
        self,
        finalization: AcceptedTurnFinalization,
    ) -> AcceptedTurnFinalization:
        """Authenticate one exact still-retained finalization receipt."""

        if type(finalization) is not AcceptedTurnFinalization:
            raise ContractViolation("accepted_turn_finalization_required")
        with self._lock:
            record = self._finalizations.get(id(finalization))
            if record is None or record.finalization is not finalization:
                raise ContractViolation("accepted_turn_finalization_not_canonical")
            ticket_record = record.ticket_record
            if (
                getattr(finalization, "_issuer_seal", None) is not self._issuer_seal
                or getattr(finalization, "_seal", None) is not _FINALIZATION_SEAL
                or ticket_record.finalization is not finalization
                or getattr(finalization, "consumer", None) is not record.consumer
                or getattr(finalization, "disposition", None)
                is not record.disposition
                or not _exact_value_matches(
                    getattr(finalization, "conversation_revision", None),
                    record.conversation_revision,
                )
                or not _exact_value_matches(
                    getattr(finalization, "finalization_digest", None),
                    record.finalization_digest,
                )
                or not ticket_record.terminal
                or ticket_record.claimed
            ):
                raise ContractViolation("accepted_turn_finalization_corrupt")
            return finalization

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            records = tuple(self._dispatches.values())
            ticket_records = tuple(self._tickets.values())
            completed = sum(record.complete for record in records)
            claimed = sum(record.claimed for record in ticket_records)
            finalized = sum(
                record.finalization is not None for record in ticket_records
            )
            return {
                "schema_version": 1,
                "consumer_count": len(_FIXED_CONSUMERS),
                "max_turns": self._max_turns,
                "turn_count": len(records),
                "active_turn_count": len(records) - completed,
                "completed_turn_count": completed,
                "ticket_count": len(ticket_records),
                "claimed_ticket_count": claimed,
                "finalized_ticket_count": finalized,
                "terminal_ticket_count": claimed + finalized,
                "ledger_bounded": len(records) <= self._max_turns,
            }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "AcceptedTurnAuthority("
            f"max_turns={metadata['max_turns']}, "
            f"turn_count={metadata['turn_count']}, "
            f"ticket_count={metadata['ticket_count']})"
        )


__all__ = [
    "AcceptedTurnContext",
    "ACCEPTED_TURN_CONSUMERS",
    "AcceptedTurnAuthority",
    "AcceptedTurnConsumer",
    "AcceptedTurnDispatch",
    "AcceptedTurnDisposition",
    "AcceptedTurnFinalization",
    "AcceptedTurnTicket",
]
