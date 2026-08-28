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
_PREFLIGHT_SEAL = object()
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


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ParticipationCadencePreflight:
    """A read-only cadence reservation bound to one semantic candidate."""

    binding: DecisionBinding = field(repr=False)
    base: ParticipationAssessment = field(repr=False)
    allowed: bool
    cooldown_remaining_s: float
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
                raise ContractViolation("participation_cadence_preflight_not_canonical")
            authority.inspect_preflight(self)
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "participation_cadence_preflight_allowed": False,
                "participation_cadence_preflight_open": False,
                "participation_cadence_preflight_reason_count": 0,
                "participation_cadence_preflight_canonical": False,
            }
        return {
            "schema_version": 1,
            "participation_cadence_preflight_allowed": self.allowed,
            "participation_cadence_preflight_open": authority.is_open(self),
            "participation_cadence_preflight_cooldown_remaining_s": (
                self.cooldown_remaining_s
            ),
            "participation_cadence_preflight_window_join_count": (
                self.window_join_count
            ),
            "participation_cadence_preflight_no_action_streak": (
                self.no_action_streak
            ),
            "participation_cadence_preflight_reason_count": len(self.reason_codes),
            "participation_cadence_preflight_canonical": True,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ParticipationCadencePreflight("
            f"allowed={metadata['participation_cadence_preflight_allowed']!r}, "
            f"open={metadata['participation_cadence_preflight_open']!r}, "
            f"canonical={metadata['participation_cadence_preflight_canonical']!r})"
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


def _preflight_snapshot(value: ParticipationCadencePreflight) -> tuple[object, ...]:
    if type(value) is not ParticipationCadencePreflight:
        raise ContractViolation("participation_cadence_preflight_required")
    try:
        snapshot = (
            value.binding,
            value.base,
            value.allowed,
            value.cooldown_remaining_s,
            value.window_join_count,
            value.no_action_streak,
            value.evaluated_at,
            value.reason_codes,
            value._authority_ref,
            value._seal,
        )
    except AttributeError as exc:
        raise ContractViolation("participation_cadence_preflight_corrupt") from exc
    if (
        type(value.binding) is not DecisionBinding
        or type(value.base) is not ParticipationAssessment
        or value.base.binding is not value.binding
        or type(value.allowed) is not bool
        or type(value.cooldown_remaining_s) is not float
        or not math.isfinite(value.cooldown_remaining_s)
        or value.cooldown_remaining_s < 0.0
        or type(value.window_join_count) is not int
        or value.window_join_count < 0
        or type(value.no_action_streak) is not int
        or value.no_action_streak < 0
        or type(value.evaluated_at) is not float
        or not math.isfinite(value.evaluated_at)
        or value.evaluated_at < 0.0
        or type(value.reason_codes) is not tuple
        or not value.reason_codes
        or any(type(item) is not str or not item for item in value.reason_codes)
        or type(value._authority_ref) is not weakref.ReferenceType
        or value._seal is not _PREFLIGHT_SEAL
    ):
        raise ContractViolation("participation_cadence_preflight_corrupt")
    return snapshot


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


@dataclass(frozen=True, slots=True)
class _PreflightRecord:
    base: ParticipationAssessment
    key: tuple[str, str]
    temporal: _TemporalState | None
    normalized_join_times: tuple[float, ...]
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
    preflights: weakref.WeakKeyDictionary[
        ParticipationCadencePreflight,
        _PreflightRecord,
    ]
    preflight_sources: weakref.WeakKeyDictionary[
        ParticipationAssessment,
        weakref.ReferenceType[ParticipationCadencePreflight],
    ]
    finalized_preflights: weakref.WeakKeyDictionary[
        ParticipationCadencePreflight,
        bool,
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
                preflights=weakref.WeakKeyDictionary(),
                preflight_sources=weakref.WeakKeyDictionary(),
                finalized_preflights=weakref.WeakKeyDictionary(),
            )
            authorities[authority] = state
            runtime_authorities[participation_authority] = weakref.ref(authority)

    def read_temporal(
        state: _CadenceAuthorityState,
        key: tuple[str, str],
        *,
        now: float,
    ) -> tuple[_TemporalState | None, bool]:
        temporal = state.subjects.get(key)
        if state.continuity_store is None:
            return temporal, False
        if not state.continuity_store.enabled:
            return temporal, True
        if temporal is not None:
            return temporal, False
        try:
            restored = state.continuity_store.cadence_for_subject(
                key[0],
                key[1],
                now=now,
            )
        except RuntimeContinuityError:
            return None, True
        if restored is None:
            return None, False
        if len(state.subjects) >= state.max_subjects:
            return None, False
        temporal = _TemporalState(
            last_evaluated_at=restored.last_evaluated_at,
            join_times=restored.join_times,
            no_action_streak=restored.no_action_streak,
            backoff_until=restored.backoff_until,
        )
        state.subjects[key] = temporal
        return temporal, False

    def preflight(
        authority: ParticipationCadenceAuthority,
        base: ParticipationAssessment,
        *,
        now: float,
    ) -> ParticipationCadencePreflight:
        if type(base) is not ParticipationAssessment:
            raise ContractViolation("participation_cadence_base_required")
        if type(now) is not float or not math.isfinite(now) or now < 0:
            raise ContractViolation("participation_cadence_clock_invalid")
        with lock:
            state = state_for(authority)
            state.participation_authority.inspect(base)
            if base.decision.level is not ParticipationLevel.MAY_JOIN:
                raise ContractViolation("participation_cadence_candidate_required")
            existing_ref = state.preflight_sources.get(base)
            if existing_ref is not None and existing_ref() is not None:
                raise ContractViolation("participation_cadence_base_replayed")
            decision_ref = state.sources.get(base)
            if decision_ref is not None and decision_ref() is not None:
                raise ContractViolation("participation_cadence_base_replayed")
            if len(state.preflights) >= state.max_decisions:
                raise ContractViolation("participation_cadence_preflight_ledger_full")

            key = (base.binding.scope_key, base.binding.current_sender_key)
            temporal, persistence_unavailable = read_temporal(state, key, now=now)
            cutoff = now - state.window_seconds
            join_times = tuple(
                value
                for value in (temporal.join_times if temporal is not None else ())
                if value > cutoff
            )
            no_action_streak = temporal.no_action_streak if temporal is not None else 0
            cooldown_remaining = 0.0
            allowed = True
            reasons = ("participation_cadence_preflight_ready",)
            if persistence_unavailable:
                allowed = False
                cooldown_remaining = state.no_action_backoff_max_seconds
                reasons = ("participation_continuity_unavailable",)
            elif temporal is not None and now < temporal.last_evaluated_at:
                allowed = False
                reasons = ("participation_cadence_clock_regressed",)
            elif temporal is None and len(state.subjects) >= state.max_subjects:
                allowed = False
                cooldown_remaining = state.no_action_backoff_max_seconds
                reasons = ("participation_cadence_capacity",)
            else:
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
                backoff_until = temporal.backoff_until if temporal is not None else 0.0
                blocked_until = max(backoff_until, cooldown_until, window_until)
                if blocked_until > now:
                    allowed = False
                    cooldown_remaining = round(blocked_until - now, 3)
                    if window_until == blocked_until:
                        reasons = ("participation_window_limit",)
                    elif backoff_until == blocked_until:
                        reasons = ("participation_no_action_backoff",)
                    else:
                        reasons = ("participation_join_cooldown",)

            value = object.__new__(ParticipationCadencePreflight)
            authority_ref = weakref.ref(authority)
            for name, field_value in (
                ("binding", base.binding),
                ("base", base),
                ("allowed", allowed),
                ("cooldown_remaining_s", float(cooldown_remaining)),
                ("window_join_count", len(join_times)),
                ("no_action_streak", no_action_streak),
                ("evaluated_at", now),
                ("reason_codes", reasons),
                ("_authority_ref", authority_ref),
                ("_seal", _PREFLIGHT_SEAL),
            ):
                object.__setattr__(value, name, field_value)
            snapshot = _preflight_snapshot(value)
            state.preflights[value] = _PreflightRecord(
                base=base,
                key=key,
                temporal=temporal,
                normalized_join_times=join_times,
                snapshot=snapshot,
            )
            state.preflight_sources[base] = weakref.ref(value)
            state.finalized_preflights[value] = False
            return value

    def inspect_preflight(
        authority: ParticipationCadenceAuthority,
        value: ParticipationCadencePreflight,
    ) -> ParticipationCadencePreflight:
        if type(value) is not ParticipationCadencePreflight:
            raise ContractViolation("participation_cadence_preflight_required")
        with lock:
            state = state_for(authority)
            record = state.preflights.get(value)
            if record is None:
                raise ContractViolation("participation_cadence_preflight_not_canonical")
            try:
                snapshot = _preflight_snapshot(value)
            except ContractViolation as exc:
                raise ContractViolation("participation_cadence_preflight_corrupt") from exc
            if (
                snapshot != record.snapshot
                or value.base is not record.base
                or value._authority_ref() is not authority
            ):
                raise ContractViolation("participation_cadence_preflight_corrupt")
            state.participation_authority.inspect(record.base)
            return value

    def is_open(
        authority: ParticipationCadenceAuthority,
        value: ParticipationCadencePreflight,
    ) -> bool:
        inspect_preflight(authority, value)
        with lock:
            state = state_for(authority)
            return state.finalized_preflights.get(value) is False

    def finalize(
        authority: ParticipationCadenceAuthority,
        value: ParticipationCadencePreflight,
        *,
        outcome: ParticipationLevel,
        reason_code: str,
    ) -> ParticipationCadenceDecision:
        if type(outcome) is not ParticipationLevel or outcome not in {
            ParticipationLevel.MAY_JOIN,
            ParticipationLevel.WAIT,
            ParticipationLevel.NO_ACTION,
        }:
            raise ContractViolation("participation_cadence_outcome_invalid")
        if type(reason_code) is not str or not reason_code or reason_code != reason_code.strip():
            raise ContractViolation("participation_cadence_reason_invalid")
        with lock:
            state = state_for(authority)
            inspect_preflight(authority, value)
            record = state.preflights.get(value)
            if record is None:
                raise ContractViolation("participation_cadence_preflight_not_canonical")
            if state.finalized_preflights.get(value) is not False:
                raise ContractViolation("participation_cadence_preflight_replayed")
            if len(state.records) >= state.max_decisions:
                raise ContractViolation("participation_cadence_decision_ledger_full")

            final_level = outcome
            join_selected = False
            cooldown_applied = False
            cooldown_remaining = 0.0
            join_times = record.normalized_join_times
            no_action_streak = value.no_action_streak
            reasons = (reason_code,)
            next_temporal: _TemporalState | None = None
            current_temporal, persistence_unavailable = read_temporal(
                state,
                record.key,
                now=value.evaluated_at,
            )
            if persistence_unavailable:
                final_level = ParticipationLevel.WAIT
                cooldown_applied = True
                cooldown_remaining = state.no_action_backoff_max_seconds
                reasons = ("participation_continuity_unavailable",)
            elif current_temporal != record.temporal:
                final_level = ParticipationLevel.WAIT
                reasons = ("participation_cadence_preflight_stale",)
            elif not value.allowed:
                final_level = ParticipationLevel.WAIT
                cooldown_applied = value.cooldown_remaining_s > 0.0
                cooldown_remaining = value.cooldown_remaining_s
                reasons = value.reason_codes
            elif outcome is ParticipationLevel.MAY_JOIN:
                join_times = (*join_times, value.evaluated_at)
                no_action_streak = 0
                next_temporal = _TemporalState(
                    last_evaluated_at=value.evaluated_at,
                    join_times=join_times,
                    no_action_streak=0,
                    backoff_until=0.0,
                )
                join_selected = True
            elif outcome is ParticipationLevel.NO_ACTION:
                no_action_streak = min(16, no_action_streak + 1)
                backoff_seconds = min(
                    state.no_action_backoff_max_seconds,
                    state.no_action_backoff_base_seconds
                    * (2 ** min(no_action_streak - 1, 8)),
                )
                prior_backoff = (
                    record.temporal.backoff_until
                    if record.temporal is not None
                    else 0.0
                )
                next_temporal = _TemporalState(
                    last_evaluated_at=value.evaluated_at,
                    join_times=join_times,
                    no_action_streak=no_action_streak,
                    backoff_until=max(
                        prior_backoff,
                        value.evaluated_at + backoff_seconds,
                    ),
                )

            if next_temporal is not None:
                committed = True
                if state.continuity_store is not None:
                    try:
                        state.continuity_store.commit_cadence(
                            record.key[0],
                            record.key[1],
                            state=CadenceContinuity(
                                last_evaluated_at=next_temporal.last_evaluated_at,
                                join_times=next_temporal.join_times,
                                no_action_streak=next_temporal.no_action_streak,
                                backoff_until=next_temporal.backoff_until,
                            ),
                            now=value.evaluated_at,
                        )
                    except RuntimeContinuityError:
                        committed = False
                if committed:
                    state.subjects[record.key] = next_temporal
                else:
                    final_level = ParticipationLevel.WAIT
                    join_selected = False
                    cooldown_applied = True
                    cooldown_remaining = state.no_action_backoff_max_seconds
                    join_times = record.normalized_join_times
                    no_action_streak = value.no_action_streak
                    reasons = ("participation_continuity_unavailable",)

            base_decision = value.base.decision
            decision = ParticipationDecision(
                binding=value.binding,
                level=final_level,
                interruption_cost=base_decision.interruption_cost,
                recent_presence=base_decision.recent_presence,
                cooldown_remaining_s=float(cooldown_remaining),
                reason_codes=reasons,
            )
            result = object.__new__(ParticipationCadenceDecision)
            authority_ref = weakref.ref(authority)
            for name, field_value in (
                ("binding", value.binding),
                ("base", value.base),
                ("decision", decision),
                ("join_selected", join_selected),
                ("cooldown_applied", cooldown_applied),
                ("window_join_count", len(join_times)),
                ("no_action_streak", no_action_streak),
                ("evaluated_at", value.evaluated_at),
                ("reason_codes", reasons),
                ("_authority_ref", authority_ref),
                ("_seal", _CADENCE_SEAL),
            ):
                object.__setattr__(result, name, field_value)
            snapshot = _decision_snapshot(result)
            state.records[result] = _CadenceRecord(base=value.base, snapshot=snapshot)
            state.sources[value.base] = weakref.ref(result)
            state.finalized_preflights[value] = True
            return result

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
            if base.decision.level is ParticipationLevel.MAY_JOIN:
                raise ContractViolation("participation_cadence_preflight_required")
            preflight_ref = state.preflight_sources.get(base)
            if preflight_ref is not None and preflight_ref() is not None:
                raise ContractViolation("participation_cadence_base_replayed")
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
            elif base_decision.level is ParticipationLevel.NO_ACTION:
                final_level = ParticipationLevel.NO_ACTION
                cooldown_remaining = 0.0
                join_selected = False
                cooldown_applied = False
                join_times = temporal.join_times if temporal is not None else ()
                no_action_streak = temporal.no_action_streak if temporal is not None else 0
                reasons = base_decision.reason_codes
            else:
                # MAY_JOIN must use the two-phase preflight/finalize contract;
                # canonical assessments currently expose no other base level.
                raise ContractViolation("participation_cadence_base_level_invalid")

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
                "participation_cadence_preflight_count": len(state.preflights),
                "participation_cadence_open_preflight_count": sum(
                    1
                    for value in state.finalized_preflights.values()
                    if value is False
                ),
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

    return (
        register,
        issue,
        preflight,
        inspect_preflight,
        is_open,
        finalize,
        inspect,
        metrics,
    )


(
    _register_cadence_authority,
    _issue_cadence,
    _preflight_cadence,
    _inspect_cadence_preflight,
    _cadence_preflight_is_open,
    _finalize_cadence,
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

    def preflight(
        self,
        base: ParticipationAssessment,
        *,
        now: float,
    ) -> ParticipationCadencePreflight:
        return _preflight_cadence(self, base, now=now)

    def inspect_preflight(
        self,
        value: ParticipationCadencePreflight,
    ) -> ParticipationCadencePreflight:
        return _inspect_cadence_preflight(self, value)

    def is_open(self, value: ParticipationCadencePreflight) -> bool:
        return _cadence_preflight_is_open(self, value)

    def finalize(
        self,
        value: ParticipationCadencePreflight,
        *,
        outcome: ParticipationLevel,
        reason_code: str,
    ) -> ParticipationCadenceDecision:
        return _finalize_cadence(
            self,
            value,
            outcome=outcome,
            reason_code=reason_code,
        )

    def inspect(
        self,
        value: ParticipationCadenceDecision,
    ) -> ParticipationCadenceDecision:
        return _inspect_cadence(self, value)

    def trace_metadata(self) -> dict[str, int | bool]:
        return _cadence_metrics(self)


__all__ = [
    "ParticipationCadenceAuthority",
    "ParticipationCadenceDecision",
    "ParticipationCadencePreflight",
]
