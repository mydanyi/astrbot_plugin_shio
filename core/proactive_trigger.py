from __future__ import annotations

import hashlib
import math
import secrets
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import Enum

from .contracts import ContractViolation
from .observability import diagnostic_digest


_TARGET_SEAL = object()
_OBSERVATION_SEAL = object()
_SOURCE_SEAL = object()
_CANDIDATE_SEAL = object()


def _digest_parts(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _exact_text(value: object, reason: str, *, maximum: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ContractViolation(reason)
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ContractViolation(reason)
    return value


def _exact_float(value: object, reason: str) -> float:
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise ContractViolation(reason)
    return value


def _parts_match(current: tuple[object, ...], snapshot: tuple[object, ...]) -> bool:
    return len(current) == len(snapshot) and all(
        type(left) is type(right) and left == right
        for left, right in zip(current, snapshot)
    )


class ProactiveCandidateDisposition(str, Enum):
    """P7-01 deliberately cannot authorize generation or delivery."""

    TYPED_ONLY = "typed_only"


class ProactiveSourceKind(str, Enum):
    SCHEDULER_OBSERVATION = "proactive_scheduler"


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ProactiveGroupTarget:
    platform_id: str = field(repr=False)
    bot_id: str = field(repr=False)
    group_id: str = field(repr=False)
    unified_msg_origin: str = field(repr=False)
    scope_key: str = field(repr=False)
    _authority_ref: weakref.ReferenceType[ProactiveTriggerAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def __new__(cls):
        raise TypeError("ProactiveGroupTarget is issuer-owned")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = (
            authority_ref()
            if type(authority_ref) is weakref.ReferenceType
            else None
        )
        try:
            record = authority._inspect_target(self)  # type: ignore[union-attr]
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "proactive_target_canonical": False,
                "proactive_target_kind": "invalid",
                "proactive_scope_digest": "",
            }
        return {
            "schema_version": 1,
            "proactive_target_canonical": True,
            "proactive_target_kind": "group",
            "proactive_scope_digest": diagnostic_digest(record.scope_key),
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ProactiveGroupTarget("
            f"canonical={metadata['proactive_target_canonical']!r}, "
            f"kind={metadata['proactive_target_kind']!r})"
        )


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ProactiveSchedulerObservation:
    target: ProactiveGroupTarget = field(repr=False)
    observed_at: float
    scheduler_tick: int
    observation_digest: str = field(repr=False)
    _authority_ref: weakref.ReferenceType[ProactiveTriggerAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def __new__(cls):
        raise TypeError("ProactiveSchedulerObservation is issuer-owned")

    def trace_metadata(self) -> dict[str, int | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            record = authority._inspect_observation(self)  # type: ignore[union-attr]
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "proactive_observation_canonical": False,
                "proactive_scheduler_tick": 0,
            }
        return {
            "schema_version": 1,
            "proactive_observation_canonical": True,
            "proactive_scheduler_tick": record.scheduler_tick,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ProactiveSchedulerObservation("
            f"canonical={metadata['proactive_observation_canonical']!r}, "
            f"scheduler_tick={metadata['proactive_scheduler_tick']})"
        )


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ProactiveTurnSource:
    target: ProactiveGroupTarget = field(repr=False)
    observation: ProactiveSchedulerObservation = field(repr=False)
    source_kind: ProactiveSourceKind
    source_digest: str = field(repr=False)
    _authority_ref: weakref.ReferenceType[ProactiveTriggerAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def __new__(cls):
        raise TypeError("ProactiveTurnSource is issuer-owned")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            record = authority._inspect_source(self)  # type: ignore[union-attr]
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "proactive_source_canonical": False,
                "proactive_source_kind": "invalid",
                "proactive_scheduler_tick": 0,
            }
        return {
            "schema_version": 1,
            "proactive_source_canonical": True,
            "proactive_source_kind": record.source_kind.value,
            "proactive_scheduler_tick": record.scheduler_tick,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ProactiveTurnSource("
            f"canonical={metadata['proactive_source_canonical']!r}, "
            f"source_kind={metadata['proactive_source_kind']!r})"
        )


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ProactiveTriggerCandidate:
    target: ProactiveGroupTarget = field(repr=False)
    source: ProactiveTurnSource = field(repr=False)
    proactive_generation: int
    disposition: ProactiveCandidateDisposition
    model_authorized: bool
    send_authorized: bool
    candidate_digest: str = field(repr=False)
    _authority_ref: weakref.ReferenceType[ProactiveTriggerAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def __new__(cls):
        raise TypeError("ProactiveTriggerCandidate is issuer-owned")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            record = authority._inspect_candidate(self)  # type: ignore[union-attr]
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "proactive_candidate_canonical": False,
                "proactive_generation": 0,
                "proactive_disposition": "invalid",
                "model_authorized": False,
                "send_authorized": False,
            }
        return {
            "schema_version": 1,
            "proactive_candidate_canonical": True,
            "proactive_generation": record.proactive_generation,
            "proactive_disposition": record.disposition.value,
            "model_authorized": False,
            "send_authorized": False,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ProactiveTriggerCandidate("
            f"canonical={metadata['proactive_candidate_canonical']!r}, "
            f"generation={metadata['proactive_generation']}, "
            f"disposition={metadata['proactive_disposition']!r}, "
            "model_authorized=False, send_authorized=False)"
        )


@dataclass(frozen=True, slots=True)
class ProactiveCandidateValidation:
    is_current: bool
    reason_code: str
    current_generation: int

    def __post_init__(self) -> None:
        if type(self.is_current) is not bool:
            raise ContractViolation("proactive_validation_invalid")
        _exact_text(self.reason_code, "proactive_validation_invalid", maximum=80)
        if type(self.current_generation) is not int or self.current_generation < 0:
            raise ContractViolation("proactive_validation_invalid")


@dataclass(frozen=True, slots=True)
class _TargetRecord:
    platform_id: str
    bot_id: str
    group_id: str
    unified_msg_origin: str
    scope_key: str


@dataclass(frozen=True, slots=True)
class _ObservationRecord:
    target: ProactiveGroupTarget
    target_parts: tuple[object, ...]
    observed_at: float
    scheduler_tick: int
    observation_digest: str
    source_issued: bool


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    target: ProactiveGroupTarget
    observation: ProactiveSchedulerObservation
    source_kind: ProactiveSourceKind
    source_digest: str
    scheduler_tick: int
    candidate_issued: bool


@dataclass(frozen=True, slots=True)
class _CandidateRecord:
    target: ProactiveGroupTarget
    source: ProactiveTurnSource
    proactive_generation: int
    disposition: ProactiveCandidateDisposition
    model_authorized: bool
    send_authorized: bool
    candidate_digest: str
    scope_key: str


class ProactiveTriggerAuthority:
    """Mint a group-only proactive source without creating an inbound turn.

    P7-01 has no claim or execution API.  Every candidate is deliberately
    typed-only; P7-02 must add a separate closed policy decision before any
    caller may plan, invoke a model, or send.
    """

    __slots__ = (
        "_authority_nonce",
        "_candidates",
        "_generations",
        "_lock",
        "_max_groups",
        "_max_records",
        "_observations",
        "_scheduler_tick",
        "_sources",
        "_targets",
        "__weakref__",
    )

    def __init__(self, *, max_groups: int = 2048, max_records: int = 4096) -> None:
        if type(max_groups) is not int or not 1 <= max_groups <= 4096:
            raise ContractViolation("proactive_group_limit_invalid")
        if type(max_records) is not int or not 1 <= max_records <= 8192:
            raise ContractViolation("proactive_record_limit_invalid")
        self._max_groups = max_groups
        self._max_records = max_records
        self._authority_nonce = secrets.token_hex(32)
        self._scheduler_tick = 0
        self._generations: OrderedDict[str, int] = OrderedDict()
        self._targets: weakref.WeakKeyDictionary[
            ProactiveGroupTarget,
            _TargetRecord,
        ] = weakref.WeakKeyDictionary()
        self._observations: weakref.WeakKeyDictionary[
            ProactiveSchedulerObservation,
            _ObservationRecord,
        ] = weakref.WeakKeyDictionary()
        self._sources: weakref.WeakKeyDictionary[
            ProactiveTurnSource,
            _SourceRecord,
        ] = weakref.WeakKeyDictionary()
        self._candidates: weakref.WeakKeyDictionary[
            ProactiveTriggerCandidate,
            _CandidateRecord,
        ] = weakref.WeakKeyDictionary()
        self._lock = threading.RLock()

    def _check_record_capacity(self) -> None:
        if (
            len(self._observations) >= self._max_records
            or len(self._sources) >= self._max_records
            or len(self._candidates) >= self._max_records
        ):
            raise ContractViolation("proactive_ledger_full")

    @staticmethod
    def _target_parts(target: ProactiveGroupTarget) -> tuple[object, ...]:
        try:
            return (
                target.platform_id,
                target.bot_id,
                target.group_id,
                target.unified_msg_origin,
                target.scope_key,
            )
        except AttributeError as exc:
            raise ContractViolation("proactive_target_corrupt") from exc

    def _inspect_target(self, target: ProactiveGroupTarget) -> _TargetRecord:
        if type(target) is not ProactiveGroupTarget:
            raise ContractViolation("proactive_target_not_canonical")
        with self._lock:
            record = self._targets.get(target)
            if type(record) is not _TargetRecord:
                raise ContractViolation("proactive_target_not_canonical")
            try:
                authority_ref = target._authority_ref
                seal = target._seal
                parts = self._target_parts(target)
            except AttributeError as exc:
                raise ContractViolation("proactive_target_corrupt") from exc
            if (
                type(authority_ref) is not weakref.ReferenceType
                or authority_ref() is not self
                or seal is not _TARGET_SEAL
                or not _parts_match(parts, (
                    record.platform_id,
                    record.bot_id,
                    record.group_id,
                    record.unified_msg_origin,
                    record.scope_key,
                ))
            ):
                raise ContractViolation("proactive_target_corrupt")
            return record

    def inspect_target(self, target: ProactiveGroupTarget) -> ProactiveGroupTarget:
        self._inspect_target(target)
        return target

    def observe_group(
        self,
        *,
        platform_id: str,
        bot_id: str,
        group_id: str,
        unified_msg_origin: str,
        observed_at: float,
    ) -> ProactiveSchedulerObservation:
        platform = _exact_text(platform_id, "proactive_platform_invalid", maximum=160)
        bot = _exact_text(bot_id, "proactive_bot_invalid", maximum=160)
        group = _exact_text(group_id, "proactive_group_invalid", maximum=160)
        origin = _exact_text(
            unified_msg_origin,
            "proactive_origin_invalid",
            maximum=512,
        )
        timestamp = _exact_float(observed_at, "proactive_observed_at_invalid")
        scope_key = f"platform:{platform}|bot:{bot}|group:{group}"
        with self._lock:
            self._check_record_capacity()
            self._scheduler_tick += 1
            tick = self._scheduler_tick
            authority_ref = weakref.ref(self)
            target = object.__new__(ProactiveGroupTarget)
            for name, value in (
                ("platform_id", platform),
                ("bot_id", bot),
                ("group_id", group),
                ("unified_msg_origin", origin),
                ("scope_key", scope_key),
                ("_authority_ref", authority_ref),
                ("_seal", _TARGET_SEAL),
            ):
                object.__setattr__(target, name, value)
            target_record = _TargetRecord(platform, bot, group, origin, scope_key)
            observation_digest = _digest_parts(
                "proactive-observation-v1",
                self._authority_nonce,
                scope_key,
                origin,
                timestamp,
                tick,
            )
            observation = object.__new__(ProactiveSchedulerObservation)
            for name, value in (
                ("target", target),
                ("observed_at", timestamp),
                ("scheduler_tick", tick),
                ("observation_digest", observation_digest),
                ("_authority_ref", authority_ref),
                ("_seal", _OBSERVATION_SEAL),
            ):
                object.__setattr__(observation, name, value)
            observation_record = _ObservationRecord(
                target=target,
                target_parts=(platform, bot, group, origin, scope_key),
                observed_at=timestamp,
                scheduler_tick=tick,
                observation_digest=observation_digest,
                source_issued=False,
            )
            try:
                self._targets[target] = target_record
                self._observations[observation] = observation_record
            except BaseException:
                self._targets.pop(target, None)
                self._observations.pop(observation, None)
                raise
            return observation

    def _inspect_observation(
        self,
        observation: ProactiveSchedulerObservation,
    ) -> _ObservationRecord:
        if type(observation) is not ProactiveSchedulerObservation:
            raise ContractViolation("proactive_observation_not_canonical")
        with self._lock:
            record = self._observations.get(observation)
            if type(record) is not _ObservationRecord:
                raise ContractViolation("proactive_observation_not_canonical")
            try:
                target = observation.target
                observed_at = observation.observed_at
                scheduler_tick = observation.scheduler_tick
                digest = observation.observation_digest
                authority_ref = observation._authority_ref
                seal = observation._seal
                target_record = self._inspect_target(target)
            except (AttributeError, ContractViolation) as exc:
                raise ContractViolation("proactive_observation_corrupt") from exc
            if (
                target is not record.target
                or not _parts_match(record.target_parts, (
                    target_record.platform_id,
                    target_record.bot_id,
                    target_record.group_id,
                    target_record.unified_msg_origin,
                    target_record.scope_key,
                ))
                or type(observed_at) is not float
                or observed_at != record.observed_at
                or type(scheduler_tick) is not int
                or scheduler_tick != record.scheduler_tick
                or type(digest) is not str
                or digest != record.observation_digest
                or type(authority_ref) is not weakref.ReferenceType
                or authority_ref() is not self
                or seal is not _OBSERVATION_SEAL
            ):
                raise ContractViolation("proactive_observation_corrupt")
            return record

    def inspect_observation(
        self,
        observation: ProactiveSchedulerObservation,
    ) -> ProactiveSchedulerObservation:
        self._inspect_observation(observation)
        return observation

    def issue_source(
        self,
        observation: ProactiveSchedulerObservation,
    ) -> ProactiveTurnSource:
        with self._lock:
            self._check_record_capacity()
            record = self._inspect_observation(observation)
            if record.source_issued:
                raise ContractViolation("proactive_observation_consumed")
            source_digest = _digest_parts(
                "proactive-source-v1",
                self._authority_nonce,
                record.observation_digest,
            )
            source = object.__new__(ProactiveTurnSource)
            for name, value in (
                ("target", record.target),
                ("observation", observation),
                ("source_kind", ProactiveSourceKind.SCHEDULER_OBSERVATION),
                ("source_digest", source_digest),
                ("_authority_ref", weakref.ref(self)),
                ("_seal", _SOURCE_SEAL),
            ):
                object.__setattr__(source, name, value)
            source_record = _SourceRecord(
                target=record.target,
                observation=observation,
                source_kind=ProactiveSourceKind.SCHEDULER_OBSERVATION,
                source_digest=source_digest,
                scheduler_tick=record.scheduler_tick,
                candidate_issued=False,
            )
            try:
                self._sources[source] = source_record
                self._observations[observation] = replace(record, source_issued=True)
            except BaseException:
                self._sources.pop(source, None)
                self._observations[observation] = record
                raise
            return source

    def _inspect_source(self, source: ProactiveTurnSource) -> _SourceRecord:
        if type(source) is not ProactiveTurnSource:
            raise ContractViolation("proactive_source_not_canonical")
        with self._lock:
            record = self._sources.get(source)
            if type(record) is not _SourceRecord:
                raise ContractViolation("proactive_source_not_canonical")
            try:
                target = source.target
                observation = source.observation
                source_kind = source.source_kind
                source_digest = source.source_digest
                authority_ref = source._authority_ref
                seal = source._seal
                self._inspect_target(target)
                observation_record = self._inspect_observation(observation)
            except (AttributeError, ContractViolation) as exc:
                raise ContractViolation("proactive_source_corrupt") from exc
            if (
                target is not record.target
                or observation is not record.observation
                or observation_record.target is not target
                or type(source_kind) is not ProactiveSourceKind
                or source_kind is not record.source_kind
                or type(source_digest) is not str
                or source_digest != record.source_digest
                or type(authority_ref) is not weakref.ReferenceType
                or authority_ref() is not self
                or seal is not _SOURCE_SEAL
            ):
                raise ContractViolation("proactive_source_corrupt")
            return record

    def inspect_source(self, source: ProactiveTurnSource) -> ProactiveTurnSource:
        self._inspect_source(source)
        return source

    def issue_candidate(self, source: ProactiveTurnSource) -> ProactiveTriggerCandidate:
        with self._lock:
            self._check_record_capacity()
            record = self._inspect_source(source)
            if record.candidate_issued:
                raise ContractViolation("proactive_source_consumed")
            target_record = self._inspect_target(record.target)
            generations_before = tuple(self._generations.items())
            previous = self._generations.pop(target_record.scope_key, 0)
            generation = previous + 1
            self._generations[target_record.scope_key] = generation
            while len(self._generations) > self._max_groups:
                self._generations.popitem(last=False)
            candidate_digest = _digest_parts(
                "proactive-candidate-v1",
                self._authority_nonce,
                record.source_digest,
                generation,
                ProactiveCandidateDisposition.TYPED_ONLY.value,
                False,
                False,
            )
            candidate = object.__new__(ProactiveTriggerCandidate)
            for name, value in (
                ("target", record.target),
                ("source", source),
                ("proactive_generation", generation),
                ("disposition", ProactiveCandidateDisposition.TYPED_ONLY),
                ("model_authorized", False),
                ("send_authorized", False),
                ("candidate_digest", candidate_digest),
                ("_authority_ref", weakref.ref(self)),
                ("_seal", _CANDIDATE_SEAL),
            ):
                object.__setattr__(candidate, name, value)
            candidate_record = _CandidateRecord(
                target=record.target,
                source=source,
                proactive_generation=generation,
                disposition=ProactiveCandidateDisposition.TYPED_ONLY,
                model_authorized=False,
                send_authorized=False,
                candidate_digest=candidate_digest,
                scope_key=target_record.scope_key,
            )
            try:
                self._candidates[candidate] = candidate_record
                self._sources[source] = replace(record, candidate_issued=True)
            except BaseException:
                self._candidates.pop(candidate, None)
                self._sources[source] = record
                self._generations.clear()
                self._generations.update(generations_before)
                raise
            return candidate

    def _inspect_candidate(
        self,
        candidate: ProactiveTriggerCandidate,
    ) -> _CandidateRecord:
        if type(candidate) is not ProactiveTriggerCandidate:
            raise ContractViolation("proactive_candidate_not_canonical")
        with self._lock:
            record = self._candidates.get(candidate)
            if type(record) is not _CandidateRecord:
                raise ContractViolation("proactive_candidate_not_canonical")
            try:
                target = candidate.target
                source = candidate.source
                generation = candidate.proactive_generation
                disposition = candidate.disposition
                model_authorized = candidate.model_authorized
                send_authorized = candidate.send_authorized
                candidate_digest = candidate.candidate_digest
                authority_ref = candidate._authority_ref
                seal = candidate._seal
                self._inspect_target(target)
                source_record = self._inspect_source(source)
            except (AttributeError, ContractViolation) as exc:
                raise ContractViolation("proactive_candidate_corrupt") from exc
            if (
                target is not record.target
                or source is not record.source
                or source_record.target is not target
                or type(generation) is not int
                or generation != record.proactive_generation
                or type(disposition) is not ProactiveCandidateDisposition
                or disposition is not record.disposition
                or type(model_authorized) is not bool
                or model_authorized is not record.model_authorized
                or type(send_authorized) is not bool
                or send_authorized is not record.send_authorized
                or type(candidate_digest) is not str
                or candidate_digest != record.candidate_digest
                or type(authority_ref) is not weakref.ReferenceType
                or authority_ref() is not self
                or seal is not _CANDIDATE_SEAL
            ):
                raise ContractViolation("proactive_candidate_corrupt")
            return record

    def inspect_candidate(
        self,
        candidate: ProactiveTriggerCandidate,
    ) -> ProactiveTriggerCandidate:
        self._inspect_candidate(candidate)
        return candidate

    def validate_candidate(
        self,
        candidate: ProactiveTriggerCandidate,
    ) -> ProactiveCandidateValidation:
        try:
            record = self._inspect_candidate(candidate)
        except ContractViolation as exc:
            reason = str(exc)
            return ProactiveCandidateValidation(
                False,
                (
                    "proactive_candidate_corrupt"
                    if reason.endswith("corrupt")
                    else "proactive_candidate_not_canonical"
                ),
                0,
            )
        with self._lock:
            current = self._generations.get(record.scope_key, 0)
        if current == 0:
            return ProactiveCandidateValidation(
                False,
                "proactive_generation_evicted",
                0,
            )
        if current != record.proactive_generation:
            return ProactiveCandidateValidation(
                False,
                "proactive_candidate_stale",
                current,
            )
        return ProactiveCandidateValidation(
            True,
            "proactive_candidate_current",
            current,
        )

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "schema_version": 1,
                "proactive_group_count": len(self._generations),
                "proactive_observation_count": len(self._observations),
                "proactive_source_count": len(self._sources),
                "proactive_candidate_count": len(self._candidates),
                "proactive_model_authorized": False,
                "proactive_send_authorized": False,
            }


__all__ = [
    "ProactiveCandidateDisposition",
    "ProactiveCandidateValidation",
    "ProactiveGroupTarget",
    "ProactiveSchedulerObservation",
    "ProactiveSourceKind",
    "ProactiveTriggerAuthority",
    "ProactiveTriggerCandidate",
    "ProactiveTurnSource",
]
