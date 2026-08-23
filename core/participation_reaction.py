from __future__ import annotations

import re
import threading
import weakref
from dataclasses import dataclass, field

from .contracts import (
    AddressKind,
    ContractViolation,
    DecisionBinding,
    ParticipationDecision,
    ParticipationLevel,
)
from .conversation_ledger import ledger_content_digest
from .participation_cadence import (
    ParticipationCadenceAuthority,
    ParticipationCadenceDecision,
)


_REACTION_SEAL = object()
_AUTHORITY_SEAL = object()
_LIGHT_REACTION_INTENT = "light_reaction"

_LIGHT_SOCIAL_RE = re.compile(
    r"(?:哈哈+|嘿嘿+|笑死|可爱|萌|好玩|好逗|表情|比心|贴贴|戳戳|摸摸)",
    re.IGNORECASE,
)
_NON_LIGHT_RE = re.compile(
    r"[？?\r\n]|"
    r"(?:严重|危险|紧急|错误|失败|崩溃|修复|排查|道歉|对不起|抱歉|"
    r"难过|伤心|害怕|焦虑|生病|不舒服|争执|冲突|隐私|权限|密钥|密码|"
    r"帮我|替我|给我|请你|能不能|可以帮忙|麻烦你|为什么|怎么|如何|"
    r"查一下|搜一下|运行|执行|打开|删除|写入|保存|提交)",
    re.IGNORECASE,
)


def _is_lightweight_social(message: str) -> bool:
    if type(message) is not str:
        raise ContractViolation("participation_reaction_message_required")
    normalized = message.strip()
    return bool(
        normalized
        and len(normalized) <= 80
        and _LIGHT_SOCIAL_RE.search(normalized)
        and not _NON_LIGHT_RE.search(normalized)
    )


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ParticipationReactionDecision:
    binding: DecisionBinding = field(repr=False)
    base: ParticipationCadenceDecision = field(repr=False)
    decision: ParticipationDecision = field(repr=False)
    reaction_selected: bool
    expression_intent: str
    reason_codes: tuple[str, ...]
    _authority_ref: weakref.ReferenceType[ParticipationReactionAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            if type(authority) is not ParticipationReactionAuthority:
                raise ContractViolation("participation_reaction_not_canonical")
            authority.inspect(self)
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "participation_reaction_level": "invalid",
                "participation_reaction_selected": False,
                "participation_reaction_reason_count": 0,
                "participation_reaction_canonical": False,
            }
        return {
            "schema_version": 1,
            "participation_reaction_level": self.decision.level.value,
            "participation_reaction_selected": self.reaction_selected,
            "participation_reaction_reason_count": len(self.reason_codes),
            "participation_reaction_canonical": True,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ParticipationReactionDecision("
            f"level={metadata['participation_reaction_level']!r}, "
            f"selected={metadata['participation_reaction_selected']!r}, "
            f"canonical={metadata['participation_reaction_canonical']!r})"
        )


def _reaction_snapshot(value: ParticipationReactionDecision) -> tuple[object, ...]:
    if type(value) is not ParticipationReactionDecision:
        raise ContractViolation("participation_reaction_required")
    try:
        decision = value.decision
        snapshot = (
            value.binding,
            value.base,
            decision,
            value.reaction_selected,
            value.expression_intent,
            value.reason_codes,
            value._authority_ref,
            value._seal,
        )
    except AttributeError as exc:
        raise ContractViolation("participation_reaction_corrupt") from exc
    if (
        type(value.binding) is not DecisionBinding
        or type(value.base) is not ParticipationCadenceDecision
        or type(decision) is not ParticipationDecision
        or decision.binding is not value.binding
        or type(decision.level) is not ParticipationLevel
        or type(decision.interruption_cost) is not float
        or type(decision.recent_presence) is not float
        or type(decision.cooldown_remaining_s) is not float
        or type(decision.reason_codes) is not tuple
        or any(type(item) is not str for item in decision.reason_codes)
        or type(value.reaction_selected) is not bool
        or type(value.expression_intent) is not str
        or value.expression_intent not in {"", _LIGHT_REACTION_INTENT}
        or type(value.reason_codes) is not tuple
        or any(type(item) is not str for item in value.reason_codes)
        or type(value._authority_ref) is not weakref.ReferenceType
        or value._seal is not _REACTION_SEAL
        or value.reason_codes != decision.reason_codes
        or (
            value.reaction_selected
            and (
                decision.level is not ParticipationLevel.REACT_ONLY
                or value.expression_intent != _LIGHT_REACTION_INTENT
            )
        )
        or (
            not value.reaction_selected
            and (
                decision.level is ParticipationLevel.REACT_ONLY
                or value.expression_intent
            )
        )
    ):
        raise ContractViolation("participation_reaction_corrupt")
    return (
        *snapshot,
        (
            decision.binding,
            decision.level,
            decision.interruption_cost,
            decision.recent_presence,
            decision.cooldown_remaining_s,
            decision.reason_codes,
        ),
    )


@dataclass(frozen=True, slots=True)
class _ReactionRecord:
    base: ParticipationCadenceDecision
    snapshot: tuple[object, ...]


@dataclass(slots=True)
class _ReactionAuthorityState:
    cadence_authority: ParticipationCadenceAuthority
    max_decisions: int
    records: weakref.WeakKeyDictionary[
        ParticipationReactionDecision,
        _ReactionRecord,
    ]
    sources: weakref.WeakKeyDictionary[
        ParticipationCadenceDecision,
        weakref.ReferenceType[ParticipationReactionDecision],
    ]


def _build_reaction_vault():
    lock = threading.RLock()
    authorities: weakref.WeakKeyDictionary[
        ParticipationReactionAuthority,
        _ReactionAuthorityState,
    ] = weakref.WeakKeyDictionary()
    runtime_authorities: weakref.WeakKeyDictionary[
        ParticipationCadenceAuthority,
        weakref.ReferenceType[ParticipationReactionAuthority],
    ] = weakref.WeakKeyDictionary()

    def state_for(
        authority: ParticipationReactionAuthority,
    ) -> _ReactionAuthorityState:
        if type(authority) is not ParticipationReactionAuthority:
            raise ContractViolation("participation_reaction_authority_required")
        state = authorities.get(authority)
        if (
            state is None
            or getattr(authority, "_seal", None) is not _AUTHORITY_SEAL
            or getattr(authority, "_cadence_authority", None)
            is not state.cadence_authority
            or runtime_authorities.get(state.cadence_authority, lambda: None)()
            is not authority
        ):
            raise ContractViolation("participation_reaction_authority_not_canonical")
        return state

    def register(
        authority: ParticipationReactionAuthority,
        cadence_authority: ParticipationCadenceAuthority,
        max_decisions: int,
    ) -> None:
        with lock:
            existing_ref = runtime_authorities.get(cadence_authority)
            if existing_ref is not None and existing_ref() is not None:
                raise ContractViolation("participation_reaction_authority_replayed")
            authorities[authority] = _ReactionAuthorityState(
                cadence_authority=cadence_authority,
                max_decisions=max_decisions,
                records=weakref.WeakKeyDictionary(),
                sources=weakref.WeakKeyDictionary(),
            )
            runtime_authorities[cadence_authority] = weakref.ref(authority)

    def issue(
        authority: ParticipationReactionAuthority,
        cadence: ParticipationCadenceDecision,
        *,
        current_message: str,
    ) -> ParticipationReactionDecision:
        if type(cadence) is not ParticipationCadenceDecision:
            raise ContractViolation("participation_reaction_cadence_required")
        if type(current_message) is not str:
            raise ContractViolation("participation_reaction_message_required")
        with lock:
            state = state_for(authority)
            state.cadence_authority.inspect(cadence)
            if ledger_content_digest(current_message) != cadence.binding.current_content_digest:
                raise ContractViolation("participation_reaction_content_mismatch")
            existing_ref = state.sources.get(cadence)
            if existing_ref is not None and existing_ref() is not None:
                raise ContractViolation("participation_reaction_cadence_replayed")
            if len(state.records) >= state.max_decisions:
                raise ContractViolation("participation_reaction_ledger_full")

            base_decision = cadence.decision
            address = cadence.base.opportunity.address
            reaction_selected = bool(
                base_decision.level is ParticipationLevel.MAY_JOIN
                and cadence.join_selected
                and address.kind is AddressKind.ABOUT_SELF
                and _is_lightweight_social(current_message)
            )
            if reaction_selected:
                final_level = ParticipationLevel.REACT_ONLY
                expression_intent = _LIGHT_REACTION_INTENT
                reasons = ("light_about_self_reaction_selected",)
            else:
                final_level = base_decision.level
                expression_intent = ""
                reasons = base_decision.reason_codes

            decision = ParticipationDecision(
                binding=cadence.binding,
                level=final_level,
                interruption_cost=base_decision.interruption_cost,
                recent_presence=base_decision.recent_presence,
                cooldown_remaining_s=base_decision.cooldown_remaining_s,
                reason_codes=reasons,
            )
            result = object.__new__(ParticipationReactionDecision)
            authority_ref = weakref.ref(authority)
            for name, field_value in (
                ("binding", cadence.binding),
                ("base", cadence),
                ("decision", decision),
                ("reaction_selected", reaction_selected),
                ("expression_intent", expression_intent),
                ("reason_codes", reasons),
                ("_authority_ref", authority_ref),
                ("_seal", _REACTION_SEAL),
            ):
                object.__setattr__(result, name, field_value)
            snapshot = _reaction_snapshot(result)
            state.records[result] = _ReactionRecord(base=cadence, snapshot=snapshot)
            state.sources[cadence] = weakref.ref(result)
            return result

    def inspect(
        authority: ParticipationReactionAuthority,
        value: ParticipationReactionDecision,
    ) -> ParticipationReactionDecision:
        if type(value) is not ParticipationReactionDecision:
            raise ContractViolation("participation_reaction_required")
        with lock:
            state = state_for(authority)
            record = state.records.get(value)
            if record is None:
                raise ContractViolation("participation_reaction_not_canonical")
            try:
                snapshot = _reaction_snapshot(value)
            except ContractViolation as exc:
                raise ContractViolation("participation_reaction_corrupt") from exc
            if (
                snapshot != record.snapshot
                or value.base is not record.base
                or value._authority_ref() is not authority
            ):
                raise ContractViolation("participation_reaction_corrupt")
            state.cadence_authority.inspect(record.base)
            return value

    def metrics(
        authority: ParticipationReactionAuthority,
    ) -> dict[str, int | bool]:
        with lock:
            state = state_for(authority)
            count = len(state.records)
            return {
                "schema_version": 1,
                "participation_reaction_decision_count": count,
                "participation_reaction_ledger_bounded": count <= state.max_decisions,
            }

    return register, issue, inspect, metrics


(
    _register_reaction_authority,
    _issue_reaction,
    _inspect_reaction,
    _reaction_metrics,
) = _build_reaction_vault()


class ParticipationReactionAuthority:
    __slots__ = ("_cadence_authority", "_seal", "__weakref__")

    def __init__(
        self,
        cadence_authority: ParticipationCadenceAuthority,
        *,
        max_decisions: int = 1024,
    ) -> None:
        if type(cadence_authority) is not ParticipationCadenceAuthority:
            raise ContractViolation("participation_reaction_cadence_authority_required")
        if type(max_decisions) is not int or max_decisions < 1:
            raise ContractViolation("participation_reaction_ledger_limit_invalid")
        self._cadence_authority = cadence_authority
        self._seal = _AUTHORITY_SEAL
        _register_reaction_authority(self, cadence_authority, max_decisions)

    def issue(
        self,
        cadence: ParticipationCadenceDecision,
        *,
        current_message: str,
    ) -> ParticipationReactionDecision:
        return _issue_reaction(self, cadence, current_message=current_message)

    def inspect(
        self,
        value: ParticipationReactionDecision,
    ) -> ParticipationReactionDecision:
        return _inspect_reaction(self, value)

    def trace_metadata(self) -> dict[str, int | bool]:
        return _reaction_metrics(self)


__all__ = ["ParticipationReactionAuthority", "ParticipationReactionDecision"]
