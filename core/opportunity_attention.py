from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field
from enum import Enum

from .accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnContext,
    AcceptedTurnTicket,
)
from .address_resolver import AddressResolutionAuthority
from .contracts import (
    AddressDecision,
    AddressKind,
    ContractViolation,
    DecisionBinding,
    SenderKind,
)
from .conversation_event import PluginSource


_OPPORTUNITY_SEAL = object()
_OPPORTUNITY_AUTHORITY_SEAL = object()
_DEFAULT_MAX_OPPORTUNITIES = 256


class OpportunityAttentionLevel(str, Enum):
    """P5-01 attention only; it is not a participation decision."""

    REQUIRED = "required"
    CANDIDATE = "candidate"
    WAIT = "wait"


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class OpportunityAttentionDecision:
    binding: DecisionBinding = field(repr=False)
    address: AddressDecision = field(repr=False)
    level: OpportunityAttentionLevel
    direct_required: bool
    participation_candidate: bool
    reason_codes: tuple[str, ...]
    _authority_ref: weakref.ReferenceType[OpportunityAttentionAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            if type(authority) is not OpportunityAttentionAuthority:
                raise ContractViolation("opportunity_attention_not_canonical")
            authority.inspect(
                self,
                address=getattr(self, "address", None),
                binding=getattr(self, "binding", None),
            )
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "opportunity_attention_level": "invalid",
                "opportunity_attention_direct_required": False,
                "opportunity_attention_candidate": False,
                "opportunity_attention_reason_count": 0,
                "opportunity_attention_canonical": False,
            }
        return {
            "schema_version": 1,
            "opportunity_attention_level": self.level.value,
            "opportunity_attention_direct_required": self.direct_required,
            "opportunity_attention_candidate": self.participation_candidate,
            "opportunity_attention_reason_count": len(self.reason_codes),
            "opportunity_attention_canonical": True,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "OpportunityAttentionDecision("
            f"level={metadata['opportunity_attention_level']!r}, "
            f"direct_required={metadata['opportunity_attention_direct_required']!r}, "
            f"candidate={metadata['opportunity_attention_candidate']!r}, "
            f"canonical={metadata['opportunity_attention_canonical']!r})"
        )


def _decision_snapshot(decision: OpportunityAttentionDecision) -> tuple[object, ...]:
    if type(decision) is not OpportunityAttentionDecision:
        raise ContractViolation("opportunity_attention_required")
    try:
        binding = decision.binding
        address = decision.address
        level = decision.level
        direct_required = decision.direct_required
        candidate = decision.participation_candidate
        reason_codes = decision.reason_codes
        authority_ref = decision._authority_ref
        seal = decision._seal
    except AttributeError as exc:
        raise ContractViolation("opportunity_attention_corrupt") from exc
    if (
        type(binding) is not DecisionBinding
        or type(address) is not AddressDecision
        or type(level) is not OpportunityAttentionLevel
        or type(direct_required) is not bool
        or type(candidate) is not bool
        or type(reason_codes) is not tuple
        or any(type(value) is not str for value in reason_codes)
        or type(authority_ref) is not weakref.ReferenceType
        or seal is not _OPPORTUNITY_SEAL
    ):
        raise ContractViolation("opportunity_attention_corrupt")
    return (
        binding,
        address,
        level,
        direct_required,
        candidate,
        reason_codes,
    )


def _classification(
    kind: AddressKind,
) -> tuple[OpportunityAttentionLevel, bool, bool, tuple[str, ...]]:
    if kind is AddressKind.DIRECT_SELF:
        return (
            OpportunityAttentionLevel.REQUIRED,
            True,
            False,
            ("direct_self_attention_required",),
        )
    if kind is AddressKind.ABOUT_SELF:
        return (
            OpportunityAttentionLevel.CANDIDATE,
            False,
            True,
            ("about_self_attention_candidate",),
        )
    if kind is AddressKind.OPEN_GROUP:
        return (
            OpportunityAttentionLevel.CANDIDATE,
            False,
            True,
            ("open_group_attention_candidate",),
        )
    if kind is AddressKind.OTHER_PERSON:
        return (
            OpportunityAttentionLevel.WAIT,
            False,
            False,
            ("other_person_attention_wait",),
        )
    if kind is AddressKind.UNCERTAIN:
        return (
            OpportunityAttentionLevel.CANDIDATE,
            False,
            True,
            ("untargeted_uncertain_attention_candidate",),
        )
    raise ContractViolation("opportunity_attention_address_kind_invalid")


@dataclass(frozen=True, slots=True)
class _OpportunityRecord:
    context: AcceptedTurnContext
    binding: DecisionBinding
    address: AddressDecision
    authority_ref: weakref.ReferenceType[OpportunityAttentionAuthority]
    snapshot: tuple[object, ...]


@dataclass(slots=True)
class _OpportunityAuthorityState:
    accepted_turn_authority: AcceptedTurnAuthority
    address_resolution_authority: AddressResolutionAuthority
    max_opportunities: int
    records: weakref.WeakKeyDictionary[
        OpportunityAttentionDecision,
        _OpportunityRecord,
    ]


def _build_opportunity_vault():
    lock = threading.RLock()
    authorities: weakref.WeakKeyDictionary[
        OpportunityAttentionAuthority,
        _OpportunityAuthorityState,
    ] = weakref.WeakKeyDictionary()

    def state_for(
        authority: OpportunityAttentionAuthority,
    ) -> _OpportunityAuthorityState:
        if type(authority) is not OpportunityAttentionAuthority:
            raise ContractViolation("opportunity_attention_authority_required")
        state = authorities.get(authority)
        if (
            state is None
            or getattr(authority, "_seal", None) is not _OPPORTUNITY_AUTHORITY_SEAL
            or getattr(authority, "_accepted_turn_authority", None)
            is not state.accepted_turn_authority
            or getattr(authority, "_address_resolution_authority", None)
            is not state.address_resolution_authority
        ):
            raise ContractViolation("opportunity_attention_authority_not_canonical")
        return state

    def register(
        authority: OpportunityAttentionAuthority,
        accepted_turn_authority: AcceptedTurnAuthority,
        address_resolution_authority: AddressResolutionAuthority,
        max_opportunities: int,
    ) -> None:
        with lock:
            if authority in authorities:
                raise ContractViolation("opportunity_attention_authority_replayed")
            authorities[authority] = _OpportunityAuthorityState(
                accepted_turn_authority=accepted_turn_authority,
                address_resolution_authority=address_resolution_authority,
                max_opportunities=max_opportunities,
                records=weakref.WeakKeyDictionary(),
            )

    def issue(
        authority: OpportunityAttentionAuthority,
        ticket: AcceptedTurnTicket,
        address: AddressDecision,
    ) -> OpportunityAttentionDecision:
        if type(ticket) is not AcceptedTurnTicket:
            raise ContractViolation("opportunity_attention_ticket_required")
        if type(address) is not AddressDecision:
            raise ContractViolation("opportunity_attention_address_required")
        binding = address.binding
        with lock:
            state = state_for(authority)
            context = state.accepted_turn_authority.context_for(
                ticket,
                consumer=AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
                binding=binding,
            )
            if (
                context.sender_kind is not SenderKind.HUMAN
                or context.plugin_source is not PluginSource.NONE
                or context.binding is not binding
            ):
                raise ContractViolation("opportunity_attention_ingress_not_human")
            state.address_resolution_authority.inspect(
                address,
                context=context,
                binding=binding,
            )
            if len(state.records) >= state.max_opportunities:
                raise ContractViolation("opportunity_attention_ledger_full")
            level, direct_required, candidate, reasons = _classification(address.kind)
            decision = object.__new__(OpportunityAttentionDecision)
            authority_ref = weakref.ref(authority)
            object.__setattr__(decision, "binding", binding)
            object.__setattr__(decision, "address", address)
            object.__setattr__(decision, "level", level)
            object.__setattr__(decision, "direct_required", direct_required)
            object.__setattr__(decision, "participation_candidate", candidate)
            object.__setattr__(decision, "reason_codes", reasons)
            object.__setattr__(decision, "_authority_ref", authority_ref)
            object.__setattr__(decision, "_seal", _OPPORTUNITY_SEAL)
            snapshot = _decision_snapshot(decision)
            state.accepted_turn_authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
                binding=binding,
                context=context,
            )
            state.records[decision] = _OpportunityRecord(
                context=context,
                binding=binding,
                address=address,
                authority_ref=authority_ref,
                snapshot=snapshot,
            )
            return decision

    def inspect(
        authority: OpportunityAttentionAuthority,
        decision: OpportunityAttentionDecision,
        *,
        address: AddressDecision,
        binding: DecisionBinding,
    ) -> OpportunityAttentionDecision:
        if type(decision) is not OpportunityAttentionDecision:
            raise ContractViolation("opportunity_attention_required")
        with lock:
            state = state_for(authority)
            record = state.records.get(decision)
            if record is None:
                raise ContractViolation("opportunity_attention_not_canonical")
            try:
                snapshot = _decision_snapshot(decision)
            except ContractViolation as exc:
                raise ContractViolation("opportunity_attention_corrupt") from exc
            if (
                type(address) is not AddressDecision
                or address is not record.address
                or type(binding) is not DecisionBinding
                or binding is not record.binding
                or decision.binding is not record.binding
                or decision.address is not record.address
                or decision._authority_ref is not record.authority_ref
                or decision._authority_ref() is not authority
                or snapshot != record.snapshot
            ):
                raise ContractViolation("opportunity_attention_corrupt")
            state.address_resolution_authority.inspect(
                address,
                context=record.context,
                binding=binding,
            )
            return decision

    def metrics(authority: OpportunityAttentionAuthority) -> dict[str, int | bool]:
        with lock:
            state = state_for(authority)
            count = len(state.records)
            return {
                "schema_version": 1,
                "opportunity_attention_count": count,
                "opportunity_attention_bounded": count <= state.max_opportunities,
            }

    def context_for(
        authority: OpportunityAttentionAuthority,
        decision: OpportunityAttentionDecision,
    ) -> AcceptedTurnContext:
        try:
            address = decision.address
            binding = decision.binding
        except AttributeError as exc:
            raise ContractViolation("opportunity_attention_corrupt") from exc
        inspect(
            authority,
            decision,
            address=address,
            binding=binding,
        )
        with lock:
            state = state_for(authority)
            record = state.records.get(decision)
            if record is None:
                raise ContractViolation("opportunity_attention_not_canonical")
            return record.context

    return register, issue, inspect, context_for, metrics


(
    _register_opportunity_authority,
    _issue_opportunity_attention,
    _inspect_opportunity_attention,
    _opportunity_context_for,
    _opportunity_authority_metrics,
) = _build_opportunity_vault()


class OpportunityAttentionAuthority:
    """Issue P5-01 attention from one exact accepted-human consumer ticket."""

    __slots__ = (
        "_accepted_turn_authority",
        "_address_resolution_authority",
        "_seal",
        "__weakref__",
    )

    def __init__(
        self,
        accepted_turn_authority: AcceptedTurnAuthority,
        address_resolution_authority: AddressResolutionAuthority,
        *,
        max_opportunities: int = _DEFAULT_MAX_OPPORTUNITIES,
    ) -> None:
        if type(accepted_turn_authority) is not AcceptedTurnAuthority:
            raise ContractViolation("opportunity_attention_accepted_authority_required")
        if type(address_resolution_authority) is not AddressResolutionAuthority:
            raise ContractViolation("opportunity_attention_address_authority_required")
        if type(max_opportunities) is not int or max_opportunities < 1:
            raise ContractViolation("opportunity_attention_ledger_limit_invalid")
        self._accepted_turn_authority = accepted_turn_authority
        self._address_resolution_authority = address_resolution_authority
        self._seal = _OPPORTUNITY_AUTHORITY_SEAL
        _register_opportunity_authority(
            self,
            accepted_turn_authority,
            address_resolution_authority,
            max_opportunities,
        )

    def issue(
        self,
        ticket: AcceptedTurnTicket,
        address: AddressDecision,
    ) -> OpportunityAttentionDecision:
        return _issue_opportunity_attention(self, ticket, address)

    def inspect(
        self,
        decision: OpportunityAttentionDecision,
        *,
        address: AddressDecision,
        binding: DecisionBinding,
    ) -> OpportunityAttentionDecision:
        return _inspect_opportunity_attention(
            self,
            decision,
            address=address,
            binding=binding,
        )

    def context_for(
        self,
        decision: OpportunityAttentionDecision,
    ) -> AcceptedTurnContext:
        """Return the exact accepted context only after canonical inspection."""

        return _opportunity_context_for(self, decision)

    def trace_metadata(self) -> dict[str, int | bool]:
        return _opportunity_authority_metrics(self)


__all__ = [
    "OpportunityAttentionAuthority",
    "OpportunityAttentionDecision",
    "OpportunityAttentionLevel",
]
