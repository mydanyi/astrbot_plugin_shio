from __future__ import annotations

import math
import threading
import weakref
from dataclasses import dataclass, field

from .contracts import (
    ContractViolation,
    DecisionBinding,
    ParticipationDecision,
    ParticipationLevel,
)
from .participation_engine import ParticipationAssessment, ParticipationAuthority
from .runtime_continuity import (
    CadenceContinuity,
    RuntimeContinuityError,
    RuntimeContinuityStore,
)


_CADENCE_SEAL = object()
_AUTHORITY_SEAL = object()


def _positive_float(value: float, name: str, *, allow_zero: bool = False) -> float:
    if type(value) not in {int, float} or type(value) is bool:
        raise ContractViolation(f"{name}_invalid")
    rendered = float(value)
    if not math.isfinite(rendered) or rendered < 0 or (not allow_zero and rendered == 0):
        raise ContractViolation(f"{name}_invalid")
    return rendered


def _positive_int(value: int, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ContractViolation(f"{name}_invalid")
    return value


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ParticipationCadenceDecision:
    binding: DecisionBinding = field(repr=False)
    base: ParticipationAssessment = field(repr=False)
    decision: ParticipationDecision = field(repr=False)
    join_selected: bool
    cooldown_applied: bool
    window_join_count: int
    no_action_streak: int
    evaluated_at: float = field(repr=False)
    reason_codes: tuple[str, ...]
    _authority_ref: weakref.ReferenceType[ParticipationCadenceAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            if type(authority) is not ParticipationCadenceAuthority:
                raise ContractViolation("participation_cadence_not_canonical")
            authority.inspect(self)
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "participation_cadence_level": "invalid",
                "participation_cadence_canonical": False,
                "participation_cadence_reason_count": 0,
            }
        return {
            "schema_version": 1,
            "participation_cadence_level": self.decision.level.value,
            "participation_cadence_join_selected": self.join_selected,
            "participation_cadence_cooldown_applied": self.cooldown_applied,
            "participation_cadence_window_join_count": self.window_join_count,
            "participation_cadence_no_action_streak": self.no_action_streak,
            "participation_cadence_cooldown_remaining_s": (
                self.decision.cooldown_remaining_s
            ),
            "participation_cadence_reason_count": len(self.reason_codes),
            "participation_cadence_canonical": True,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ParticipationCadenceDecision("
            f"level={metadata['participation_cadence_level']!r}, "
            f"selected={metadata.get('participation_cadence_join_selected', False)!r}, "
            f"canonical={metadata['participation_cadence_canonical']!r})"
        )


def _decision_snapshot(value: ParticipationCadenceDecision) -> tuple[object, ...]:
    if type(value) is not ParticipationCadenceDecision:
        raise ContractViolation("participation_cadence_required")
    try:
        decision = value.decision
        snapshot = (
            value.binding,
            value.base,
            decision,
            value.join_selected,
            value.cooldown_applied,
            value.window_join_count,
            value.no_action_streak,
            value.evaluated_at,
            value.reason_codes,
            value._authority_ref,
            value._seal,
        )
    except AttributeError as exc:
        raise ContractViolation("participation_cadence_corrupt") from exc
    if (
        type(value.binding) is not DecisionBinding
        or type(value.base) is not ParticipationAssessment
        or type(decision) is not ParticipationDecision
        or decision.binding is not value.binding
        or type(decision.level) is not ParticipationLevel
        or type(decision.interruption_cost) is not float
        or type(decision.recent_presence) is not float
        or type(decision.cooldown_remaining_s) is not float
        or type(decision.reason_codes) is not tuple
        or any(type(item) is not str for item in decision.reason_codes)
        or type(value.join_selected) is not bool
        or type(value.cooldown_applied) is not bool
        or type(value.window_join_count) is not int
        or value.window_join_count < 0
        or type(value.no_action_streak) is not int
        or value.no_action_streak < 0
        or type(value.evaluated_at) is not float
        or not math.isfinite(value.evaluated_at)
        or value.evaluated_at < 0
        or type(value.reason_codes) is not tuple
        or any(type(item) is not str for item in value.reason_codes)
        or type(value._authority_ref) is not weakref.ReferenceType
        or value._seal is not _CADENCE_SEAL
    ):
        raise ContractViolation("participation_cadence_corrupt")
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
class _TemporalState:
    last_evaluated_at: float
    join_times: tuple[float, ...]
    no_action_streak: int
    backoff_until: float


@dataclass(frozen=True, slots=True)
class _CadenceRecord:
    base: ParticipationAssessment
    snapshot: tuple[object, ...]


@dataclass(slots=True)
class _CadenceAuthorityState:
    participation_authority: ParticipationAuthority
    continuity_store: RuntimeContinuityStore | None
    join_cooldown_seconds: float
    window_seconds: float
    max_joins_per_window: int
    no_action_backoff_base_seconds: float
    no_action_backoff_max_seconds: float
    max_subjects: int
    max_decisions: int
    subjects: dict[tuple[str, str], _TemporalState]
    records: weakref.WeakKeyDictionary[
        ParticipationCadenceDecision,
        _CadenceRecord,
    ]
    sources: weakref.WeakKeyDictionary[
        ParticipationAssessment,
        weakref.ReferenceType[ParticipationCadenceDecision],
    ]


def _build_cadence_vault():
    lock = threading.RLock()
    authorities: weakref.WeakKeyDictionary[
        ParticipationCadenceAuthority,
        _CadenceAuthorityState,
    ] = weakref.WeakKeyDictionary()
    runtime_authorities: weakref.WeakKeyDictionary[
        ParticipationAuthority,
        weakref.ReferenceType[ParticipationCadenceAuthority],
    ] = weakref.WeakKeyDictionary()

    def state_for(authority: ParticipationCadenceAuthority) -> _CadenceAuthorityState:
        if type(authority) is not ParticipationCadenceAuthority:
            raise ContractViolation("participation_cadence_authority_required")
        state = authorities.get(authority)
        if (
            state is None
            or getattr(authority, "_seal", None) is not _AUTHORITY_SEAL
            or getattr(authority, "_participation_authority", None)
            is not state.participation_authority
            or runtime_authorities.get(state.participation_authority, lambda: None)()
            is not authority
        ):
            raise ContractViolation("participation_cadence_authority_not_canonical")
        return state

    def register(
        authority: ParticipationCadenceAuthority,
        participation_authority: ParticipationAuthority,
        *,
        join_cooldown_seconds: float,
        window_seconds: float,
        max_joins_per_window: int,
        no_action_backoff_base_seconds: float,
        no_action_backoff_max_seconds: float,
        max_subjects: int,
        max_decisions: int,
        continuity_store: RuntimeContinuityStore | None,
    ) -> None:
        with lock:
            existing_ref = runtime_authorities.get(participation_authority)
            if existing_ref is not None and existing_ref() is not None:
                raise ContractViolation("participation_cadence_authority_replayed")
            state = _CadenceAuthorityState(
                participation_authority=participation_authority,
                continuity_store=continuity_store,
                join_cooldown_seconds=join_cooldown_seconds,
                window_seconds=window_seconds,
                max_joins_per_window=max_joins_per_window,
                no_action_backoff_base_seconds=no_action_backoff_base_seconds,
                no_action_backoff_max_seconds=no_action_backoff_max_seconds,
                max_subjects=max_subjects,
                max_decisions=max_decisions,
                subjects={},
                records=weakref.WeakKeyDictionary(),
                sources=weakref.WeakKeyDictionary(),
            )
            authorities[authority] = state
            runtime_authorities[participation_authority] = weakref.ref(authority)

    def issue(
        authority: ParticipationCadenceAuthority,
        base: ParticipationAssessment,
        *,
        now: float,
    ) -> ParticipationCadenceDecision:
        if type(base) is not ParticipationAssessment:
            raise ContractViolation("participation_cadence_base_required")
        if type(now) is not float or not math.isfinite(now) or now < 0:
            raise ContractViolation("participation_cadence_clock_invalid")
        with lock:
            state = state_for(authority)
            state.participation_authority.inspect(base)
            existing_ref = state.sources.get(base)
            if existing_ref is not None and existing_ref() is not None:
                raise ContractViolation("participation_cadence_base_replayed")
            if len(state.records) >= state.max_decisions:
                raise ContractViolation("participation_cadence_decision_ledger_full")

            base_decision = base.decision
            key = (base.binding.scope_key, base.binding.current_sender_key)
            temporal = state.subjects.get(key)
            persistence_unavailable = bool(
                state.continuity_store is not None
                and not state.continuity_store.enabled
            )
            if (
                temporal is None
                and state.continuity_store is not None
                and not persistence_unavailable
            ):
                try:
                    restored = state.continuity_store.cadence_for_subject(
                        key[0],
                        key[1],
                        now=now,
                    )
                except RuntimeContinuityError:
                    persistence_unavailable = True
                else:
                    if restored is not None:
                        temporal = _TemporalState(
                            last_evaluated_at=restored.last_evaluated_at,
                            join_times=restored.join_times,
                            no_action_streak=restored.no_action_streak,
                            backoff_until=restored.backoff_until,
                        )
                        state.subjects[key] = temporal
            if (
                persistence_unavailable
                and base_decision.level is not ParticipationLevel.MUST_REPLY
            ):
                final_level = ParticipationLevel.WAIT
                cooldown_remaining = state.no_action_backoff_max_seconds
                join_selected = False
                cooldown_applied = True
                join_times = temporal.join_times if temporal is not None else ()
                no_action_streak = temporal.no_action_streak if temporal is not None else 0
                reasons = ("participation_continuity_unavailable",)
            elif temporal is not None and now < temporal.last_evaluated_at:
                final_level = (
                    ParticipationLevel.MUST_REPLY
                    if base_decision.level is ParticipationLevel.MUST_REPLY
                    else ParticipationLevel.WAIT
                )
                cooldown_remaining = 0.0
                join_selected = False
                cooldown_applied = final_level is ParticipationLevel.WAIT
                join_times = temporal.join_times
                no_action_streak = temporal.no_action_streak
                reasons = ("participation_cadence_clock_regressed",)
            elif base_decision.level is ParticipationLevel.MUST_REPLY:
                final_level = ParticipationLevel.MUST_REPLY
                cooldown_remaining = 0.0
                join_selected = False
                cooldown_applied = False
                join_times = temporal.join_times if temporal is not None else ()
                no_action_streak = temporal.no_action_streak if temporal is not None else 0
                reasons = ("direct_must_reply_cadence_bypass",)
            elif temporal is None and len(state.subjects) >= state.max_subjects:
                final_level = ParticipationLevel.WAIT
                cooldown_remaining = state.no_action_backoff_max_seconds
                join_selected = False
                cooldown_applied = True
                join_times = ()
                no_action_streak = 0
                reasons = ("participation_cadence_capacity",)
            else:
                temporal = temporal or _TemporalState(
                    last_evaluated_at=now,
                    join_times=(),
                    no_action_streak=0,
                    backoff_until=0.0,
                )
                cutoff = now - state.window_seconds
                join_times = tuple(value for value in temporal.join_times if value > cutoff)
                no_action_streak = temporal.no_action_streak
                backoff_until = temporal.backoff_until
                final_level = base_decision.level
                cooldown_remaining = 0.0
                join_selected = False
                cooldown_applied = False
                reasons = base_decision.reason_codes

                if base_decision.level is ParticipationLevel.MAY_JOIN:
                    cooldown_until = (
                        join_times[-1] + state.join_cooldown_seconds
                        if join_times
                        else 0.0
                    )
                    window_until = (
                        join_times[0] + state.window_seconds
                        if len(join_times) >= state.max_joins_per_window
                        else 0.0
                    )
                    blocked_until = max(backoff_until, cooldown_until, window_until)
                    if blocked_until > now:
                        final_level = ParticipationLevel.WAIT
                        cooldown_remaining = round(blocked_until - now, 3)
                        cooldown_applied = True
                        if window_until == blocked_until:
                            reasons = ("participation_window_limit",)
                        elif backoff_until == blocked_until:
                            reasons = ("participation_no_action_backoff",)
                        else:
                            reasons = ("participation_join_cooldown",)
                    else:
                        join_times = (*join_times, now)
                        no_action_streak = 0
                        backoff_until = 0.0
                        join_selected = True
                        reasons = ("participation_cadence_join_selected",)
                elif base_decision.level is ParticipationLevel.NO_ACTION:
                    no_action_streak = min(16, no_action_streak + 1)
                    backoff_seconds = min(
                        state.no_action_backoff_max_seconds,
                        state.no_action_backoff_base_seconds
                        * (2 ** min(no_action_streak - 1, 8)),
                    )
                    backoff_until = max(backoff_until, now + backoff_seconds)
                    reasons = (
                        *base_decision.reason_codes,
                        "participation_no_action_retreat",
                    )
                next_temporal = _TemporalState(
                    last_evaluated_at=now,
                    join_times=join_times,
                    no_action_streak=no_action_streak,
                    backoff_until=backoff_until,
                )
                persistence_committed = True
                if state.continuity_store is not None:
                    try:
                        state.continuity_store.commit_cadence(
                            key[0],
                            key[1],
                            state=CadenceContinuity(
                                last_evaluated_at=next_temporal.last_evaluated_at,
                                join_times=next_temporal.join_times,
                                no_action_streak=next_temporal.no_action_streak,
                                backoff_until=next_temporal.backoff_until,
                            ),
                            now=now,
                        )
                    except RuntimeContinuityError:
                        persistence_committed = False
                        final_level = ParticipationLevel.WAIT
                        cooldown_remaining = state.no_action_backoff_max_seconds
                        join_selected = False
                        cooldown_applied = True
                        reasons = ("participation_continuity_unavailable",)
                if persistence_committed:
                    state.subjects[key] = next_temporal

            decision = ParticipationDecision(
                binding=base.binding,
                level=final_level,
                interruption_cost=base_decision.interruption_cost,
                recent_presence=base_decision.recent_presence,
                cooldown_remaining_s=cooldown_remaining,
                reason_codes=reasons,
            )
            result = object.__new__(ParticipationCadenceDecision)
            authority_ref = weakref.ref(authority)
            for name, value in (
                ("binding", base.binding),
                ("base", base),
                ("decision", decision),
                ("join_selected", join_selected),
                ("cooldown_applied", cooldown_applied),
                ("window_join_count", len(join_times)),
                ("no_action_streak", no_action_streak),
                ("evaluated_at", now),
                ("reason_codes", reasons),
                ("_authority_ref", authority_ref),
                ("_seal", _CADENCE_SEAL),
            ):
                object.__setattr__(result, name, value)
            snapshot = _decision_snapshot(result)
            state.records[result] = _CadenceRecord(base=base, snapshot=snapshot)
            state.sources[base] = weakref.ref(result)
            return result

    def inspect(
        authority: ParticipationCadenceAuthority,
        value: ParticipationCadenceDecision,
    ) -> ParticipationCadenceDecision:
        if type(value) is not ParticipationCadenceDecision:
            raise ContractViolation("participation_cadence_required")
        with lock:
            state = state_for(authority)
            record = state.records.get(value)
            if record is None:
                raise ContractViolation("participation_cadence_not_canonical")
            try:
                snapshot = _decision_snapshot(value)
            except ContractViolation as exc:
                raise ContractViolation("participation_cadence_corrupt") from exc
            if (
                snapshot != record.snapshot
                or value.base is not record.base
                or value._authority_ref() is not authority
            ):
                raise ContractViolation("participation_cadence_corrupt")
            state.participation_authority.inspect(record.base)
            return value

    def metrics(
        authority: ParticipationCadenceAuthority,
    ) -> dict[str, int | bool]:
        with lock:
            state = state_for(authority)
            return {
                "schema_version": 1,
                "participation_cadence_subject_count": len(state.subjects),
                "participation_cadence_decision_count": len(state.records),
                "participation_cadence_subjects_bounded": (
                    len(state.subjects) <= state.max_subjects
                ),
                "participation_cadence_decisions_bounded": (
                    len(state.records) <= state.max_decisions
                ),
                "participation_cadence_persistence_available": bool(
                    state.continuity_store is None or state.continuity_store.enabled
                ),
            }

    return register, issue, inspect, metrics


(
    _register_cadence_authority,
    _issue_cadence,
    _inspect_cadence,
    _cadence_metrics,
) = _build_cadence_vault()


class ParticipationCadenceAuthority:
    __slots__ = ("_participation_authority", "_seal", "__weakref__")

    def __init__(
        self,
        participation_authority: ParticipationAuthority,
        *,
        join_cooldown_seconds: float = 45.0,
        window_seconds: float = 300.0,
        max_joins_per_window: int = 2,
        no_action_backoff_base_seconds: float = 2.0,
        no_action_backoff_max_seconds: float = 30.0,
        max_subjects: int = 256,
        max_decisions: int = 1024,
        continuity_store: RuntimeContinuityStore | None = None,
    ) -> None:
        if type(participation_authority) is not ParticipationAuthority:
            raise ContractViolation("participation_authority_required")
        if (
            continuity_store is not None
            and type(continuity_store) is not RuntimeContinuityStore
        ):
            raise ContractViolation("participation_continuity_store_invalid")
        cooldown = _positive_float(
            join_cooldown_seconds,
            "participation_join_cooldown",
            allow_zero=True,
        )
        window = _positive_float(window_seconds, "participation_window")
        max_joins = _positive_int(
            max_joins_per_window,
            "participation_max_joins_per_window",
        )
        backoff_base = _positive_float(
            no_action_backoff_base_seconds,
            "participation_no_action_backoff_base",
        )
        backoff_max = _positive_float(
            no_action_backoff_max_seconds,
            "participation_no_action_backoff_max",
        )
        if backoff_base > backoff_max:
            raise ContractViolation("participation_no_action_backoff_order_invalid")
        subjects = _positive_int(max_subjects, "participation_max_subjects")
        decisions = _positive_int(max_decisions, "participation_max_decisions")
        self._participation_authority = participation_authority
        self._seal = _AUTHORITY_SEAL
        _register_cadence_authority(
            self,
            participation_authority,
            join_cooldown_seconds=cooldown,
            window_seconds=window,
            max_joins_per_window=max_joins,
            no_action_backoff_base_seconds=backoff_base,
            no_action_backoff_max_seconds=backoff_max,
            max_subjects=subjects,
            max_decisions=decisions,
            continuity_store=continuity_store,
        )

    def issue(
        self,
        base: ParticipationAssessment,
        *,
        now: float,
    ) -> ParticipationCadenceDecision:
        return _issue_cadence(self, base, now=now)

    def inspect(
        self,
        value: ParticipationCadenceDecision,
    ) -> ParticipationCadenceDecision:
        return _inspect_cadence(self, value)

    def trace_metadata(self) -> dict[str, int | bool]:
        return _cadence_metrics(self)


__all__ = ["ParticipationCadenceAuthority", "ParticipationCadenceDecision"]
