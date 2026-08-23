from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import weakref
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from .contracts import ContractViolation
from .proactive_trigger import ProactiveTriggerAuthority, ProactiveTriggerCandidate


_DECISION_SEAL = object()
_STATE_FILENAME = "proactive_state.json"
_SECRET_FILENAME = ".proactive_install_secret"
_MAX_STATE_BYTES = 1024 * 1024


def _exact_text(value: object, reason: str, *, maximum: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ContractViolation(reason)
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ContractViolation(reason)
    return value


def _exact_time(value: object, reason: str) -> float:
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise ContractViolation(reason)
    return value


def _exact_parts(current: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    return len(current) == len(expected) and all(
        type(left) is type(right) and left == right
        for left, right in zip(current, expected)
    )


@dataclass(frozen=True, slots=True)
class ProactivePolicyConfig:
    enabled: bool = False
    group_allowlist: tuple[str, ...] = ()
    active_hour_start: int = 9
    active_hour_end: int = 23
    timezone_offset_minutes: int = 480
    observation_seconds: int = 1800
    idle_seconds: int = 1200
    cooldown_seconds: int = 10800
    daily_limit: int = 1
    max_groups: int = 2048

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ContractViolation("proactive_policy_enabled_invalid")
        if (
            type(self.group_allowlist) is not tuple
            or any(
                type(value) is not str
                or not value
                or value != value.strip()
                or len(value) > 160
                for value in self.group_allowlist
            )
            or len(set(self.group_allowlist)) != len(self.group_allowlist)
            or len(self.group_allowlist) > 2048
        ):
            raise ContractViolation("proactive_policy_allowlist_invalid")
        if (
            type(self.active_hour_start) is not int
            or not 0 <= self.active_hour_start <= 23
            or type(self.active_hour_end) is not int
            or not 1 <= self.active_hour_end <= 24
            or self.active_hour_start == self.active_hour_end
        ):
            raise ContractViolation("proactive_policy_active_hours_invalid")
        if (
            type(self.timezone_offset_minutes) is not int
            or not -720 <= self.timezone_offset_minutes <= 840
        ):
            raise ContractViolation("proactive_policy_timezone_invalid")
        for value, reason, maximum in (
            (self.observation_seconds, "proactive_policy_observation_invalid", 604800),
            (self.idle_seconds, "proactive_policy_idle_invalid", 604800),
            (self.cooldown_seconds, "proactive_policy_cooldown_invalid", 2592000),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ContractViolation(reason)
        if type(self.daily_limit) is not int or not 1 <= self.daily_limit <= 20:
            raise ContractViolation("proactive_policy_daily_limit_invalid")
        if type(self.max_groups) is not int or not 1 <= self.max_groups <= 4096:
            raise ContractViolation("proactive_policy_group_limit_invalid")


class ProactivePolicyDecisionKind(str, Enum):
    DISABLED = "disabled"
    GROUP_NOT_ALLOWED = "group_not_allowed"
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"
    OBSERVATION_INCOMPLETE = "observation_incomplete"
    GROUP_NOT_IDLE = "group_not_idle"
    COOLDOWN_ACTIVE = "cooldown_active"
    DAILY_LIMIT = "daily_limit"
    OBSERVATION_REPLAYED = "observation_replayed"
    CLOCK_REGRESSED = "clock_regressed"
    STATE_CAPACITY = "state_capacity"
    STATE_UNAVAILABLE = "state_unavailable"
    CANDIDATE_STALE = "candidate_stale"
    ADMITTED = "admitted"


class ProactivePolicyTerminalKind(str, Enum):
    SENT = "sent"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_FAILED = "provider_failed"
    OUTPUT_REJECTED = "output_rejected"
    SEND_FAILED = "send_failed"
    SUPERSEDED = "superseded"
    STOPPED = "stopped"


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ProactivePolicyDecision:
    candidate: ProactiveTriggerCandidate = field(repr=False)
    kind: ProactivePolicyDecisionKind
    admitted: bool
    evaluated_at: float = field(repr=False)
    retry_after_seconds: float
    model_authorized: bool
    send_authorized: bool
    _state_ref: weakref.ReferenceType[ProactivePolicyState] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def __new__(cls):
        raise TypeError("ProactivePolicyDecision is issuer-owned")

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        state_ref = getattr(self, "_state_ref", None)
        state = state_ref() if type(state_ref) is weakref.ReferenceType else None
        try:
            record = state._inspect(self)  # type: ignore[union-attr]
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "proactive_policy_canonical": False,
                "proactive_policy_kind": "invalid",
                "proactive_policy_admitted": False,
                "proactive_retry_after_seconds": 0.0,
                "proactive_model_authorized": False,
                "proactive_send_authorized": False,
            }
        return {
            "schema_version": 1,
            "proactive_policy_canonical": True,
            "proactive_policy_kind": record.kind.value,
            "proactive_policy_admitted": record.admitted,
            "proactive_retry_after_seconds": record.retry_after_seconds,
            "proactive_model_authorized": False,
            "proactive_send_authorized": False,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ProactivePolicyDecision("
            f"canonical={metadata['proactive_policy_canonical']!r}, "
            f"kind={metadata['proactive_policy_kind']!r}, "
            f"admitted={metadata['proactive_policy_admitted']!r}, "
            "model_authorized=False, send_authorized=False)"
        )


@dataclass(frozen=True, slots=True)
class _GroupState:
    fingerprint: str
    first_observed_at: float
    last_activity_at: float
    last_seen_at: float
    last_trigger_at: float
    trigger_day: int
    daily_count: int
    last_trigger_observed_at: float


@dataclass(frozen=True, slots=True)
class _DecisionRecord:
    candidate: ProactiveTriggerCandidate
    kind: ProactivePolicyDecisionKind
    admitted: bool
    evaluated_at: float
    retry_after_seconds: float
    model_authorized: bool
    send_authorized: bool
    snapshot: tuple[object, ...]


def _decision_snapshot(decision: ProactivePolicyDecision) -> tuple[object, ...]:
    if type(decision) is not ProactivePolicyDecision:
        raise ContractViolation("proactive_policy_decision_not_canonical")
    try:
        values = (
            decision.candidate,
            decision.kind,
            decision.admitted,
            decision.evaluated_at,
            decision.retry_after_seconds,
            decision.model_authorized,
            decision.send_authorized,
            decision._state_ref,
            decision._seal,
        )
    except AttributeError as exc:
        raise ContractViolation("proactive_policy_decision_corrupt") from exc
    if (
        type(values[0]) is not ProactiveTriggerCandidate
        or type(values[1]) is not ProactivePolicyDecisionKind
        or type(values[2]) is not bool
        or type(values[3]) is not float
        or not math.isfinite(values[3])
        or values[3] < 0
        or type(values[4]) is not float
        or not math.isfinite(values[4])
        or values[4] < 0
        or type(values[5]) is not bool
        or type(values[6]) is not bool
        or type(values[7]) is not weakref.ReferenceType
        or values[8] is not _DECISION_SEAL
    ):
        raise ContractViolation("proactive_policy_decision_corrupt")
    return values


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


class ProactivePolicyState:
    """Persistent, privacy-minimal gate between typed candidates and P7-03."""

    __slots__ = (
        "_decisions",
        "_evaluated",
        "_failure_code",
        "_lock",
        "_policy",
        "_records",
        "_root",
        "_secret",
        "_state_path",
        "_terminals",
        "_trigger_authority",
        "__weakref__",
    )

    def __init__(
        self,
        root: Path,
        *,
        trigger_authority: ProactiveTriggerAuthority,
        policy: ProactivePolicyConfig,
    ) -> None:
        if type(root) is not type(Path()):
            raise ContractViolation("proactive_state_root_invalid")
        if type(trigger_authority) is not ProactiveTriggerAuthority:
            raise ContractViolation("proactive_trigger_authority_required")
        if type(policy) is not ProactivePolicyConfig:
            raise ContractViolation("proactive_policy_required")
        self._root = root
        self._state_path = root / _STATE_FILENAME
        self._trigger_authority = trigger_authority
        self._policy = policy
        self._secret = b""
        self._records: dict[str, _GroupState] = {}
        self._failure_code = ""
        self._decisions: weakref.WeakKeyDictionary[
            ProactivePolicyDecision,
            _DecisionRecord,
        ] = weakref.WeakKeyDictionary()
        self._evaluated: weakref.WeakKeyDictionary[
            ProactiveTriggerCandidate,
            weakref.ReferenceType[ProactivePolicyDecision],
        ] = weakref.WeakKeyDictionary()
        self._terminals: weakref.WeakKeyDictionary[
            ProactivePolicyDecision,
            ProactivePolicyTerminalKind,
        ] = weakref.WeakKeyDictionary()
        self._lock = threading.RLock()
        if policy.enabled and policy.group_allowlist:
            self._initialize()

    @property
    def operational(self) -> bool:
        with self._lock:
            return bool(
                self._policy.enabled
                and self._policy.group_allowlist
                and not self._failure_code
                and self._secret
            )

    def _initialize(self) -> None:
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            secret_path = self._root / _SECRET_FILENAME
            state_exists = self._state_path.exists()
            try:
                secret = secret_path.read_bytes()
            except FileNotFoundError:
                if state_exists:
                    raise ValueError("state_secret_missing")
                secret = secrets.token_bytes(32)
                descriptor = os.open(
                    secret_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(secret)
                    stream.flush()
                    os.fsync(stream.fileno())
            if type(secret) is not bytes or len(secret) != 32:
                raise ValueError("secret_invalid")
            self._secret = secret
            if state_exists:
                self._records = self._load_records()
        except BaseException:
            self._secret = b""
            self._records = {}
            self._failure_code = "proactive_state_unavailable"

    def _fingerprint(self, platform_id: str, bot_id: str, group_id: str) -> str:
        if not self._secret:
            raise ContractViolation("proactive_state_unavailable")
        payload = "\x1f".join((platform_id, bot_id, group_id)).encode("utf-8")
        return hmac.new(
            self._secret,
            b"proactive-group-v1\x00" + payload,
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _record_payload(record: _GroupState) -> dict[str, object]:
        return {
            "fingerprint": record.fingerprint,
            "first_observed_at": record.first_observed_at,
            "last_activity_at": record.last_activity_at,
            "last_seen_at": record.last_seen_at,
            "last_trigger_at": record.last_trigger_at,
            "trigger_day": record.trigger_day,
            "daily_count": record.daily_count,
            "last_trigger_observed_at": record.last_trigger_observed_at,
        }

    def _load_records(self) -> dict[str, _GroupState]:
        raw = self._state_path.read_bytes()
        if len(raw) > _MAX_STATE_BYTES:
            raise ValueError("state_too_large")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        if type(payload) is not dict or set(payload) != {
            "schema_version",
            "records",
            "state_mac",
        }:
            raise ValueError("state_shape")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] not in {1, 2}
        ):
            raise ValueError("state_schema")
        schema_version = payload["schema_version"]
        rows = payload["records"]
        if type(rows) is not list or len(rows) > self._policy.max_groups:
            raise ValueError("state_records")
        state_mac = payload["state_mac"]
        if (
            type(state_mac) is not str
            or len(state_mac) != 64
            or any(character not in "0123456789abcdef" for character in state_mac)
        ):
            raise ValueError("state_mac")
        rows_encoded = json.dumps(
            rows,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected_mac = hmac.new(
            self._secret,
            f"proactive-state-v{schema_version}".encode("ascii")
            + b"\x00"
            + rows_encoded,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(state_mac, expected_mac):
            raise ValueError("state_mac")
        records: dict[str, _GroupState] = {}
        expected = {
            "fingerprint",
            "first_observed_at",
            "last_activity_at",
            "last_seen_at",
            "last_trigger_at",
            "trigger_day",
            "daily_count",
            "last_trigger_observed_at",
        }
        for row in rows:
            if type(row) is not dict or set(row) != expected:
                raise ValueError("state_record_shape")
            fingerprint = row["fingerprint"]
            times = tuple(
                row[name]
                for name in (
                    "first_observed_at",
                    "last_activity_at",
                    "last_seen_at",
                    "last_trigger_at",
                    "last_trigger_observed_at",
                )
            )
            if (
                type(fingerprint) is not str
                or len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint)
                or any(type(value) is not float or not math.isfinite(value) or value < 0 for value in times)
                or type(row["trigger_day"]) is not int
                or row["trigger_day"] < -1
                or type(row["daily_count"]) is not int
                or not 0 <= row["daily_count"] <= self._policy.daily_limit
                or fingerprint in records
            ):
                raise ValueError("state_record_invalid")
            records[fingerprint] = _GroupState(
                fingerprint=fingerprint,
                first_observed_at=times[0],
                last_activity_at=times[1],
                last_seen_at=times[2],
                last_trigger_at=times[3],
                trigger_day=row["trigger_day"],
                daily_count=row["daily_count"],
                last_trigger_observed_at=times[4],
            )
        return records

    def _persist(self, records: dict[str, _GroupState]) -> bool:
        rows = [self._record_payload(records[key]) for key in sorted(records)]
        rows_encoded = json.dumps(
            rows,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        payload = {
            "schema_version": 2,
            "records": rows,
            "state_mac": hmac.new(
                self._secret,
                b"proactive-state-v2\x00" + rows_encoded,
                hashlib.sha256,
            ).hexdigest(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_STATE_BYTES:
            self._failure_code = "proactive_state_unavailable"
            return False
        temporary = self._state_path.with_suffix(".tmp")
        try:
            with open(temporary, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_path)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self._failure_code = "proactive_state_unavailable"
            return False
        return True

    def observe_group_activity(
        self,
        *,
        platform_id: str,
        bot_id: str,
        group_id: str,
        observed_at: float,
    ) -> bool:
        platform = _exact_text(platform_id, "proactive_platform_invalid", maximum=160)
        bot = _exact_text(bot_id, "proactive_bot_invalid", maximum=160)
        group = _exact_text(group_id, "proactive_group_invalid", maximum=160)
        timestamp = _exact_time(observed_at, "proactive_activity_clock_invalid")
        with self._lock:
            if not self._policy.enabled or not self._policy.group_allowlist:
                return False
            if group not in self._policy.group_allowlist or self._failure_code:
                return False
            fingerprint = self._fingerprint(platform, bot, group)
            current = self._records.get(fingerprint)
            if current is not None and timestamp < current.last_seen_at:
                return False
            if current is None and len(self._records) >= self._policy.max_groups:
                return False
            updated = _GroupState(
                fingerprint=fingerprint,
                first_observed_at=(
                    current.first_observed_at if current is not None else timestamp
                ),
                last_activity_at=timestamp,
                last_seen_at=timestamp,
                last_trigger_at=current.last_trigger_at if current is not None else 0.0,
                trigger_day=current.trigger_day if current is not None else -1,
                daily_count=current.daily_count if current is not None else 0,
                last_trigger_observed_at=(
                    current.last_trigger_observed_at if current is not None else 0.0
                ),
            )
            candidate = dict(self._records)
            candidate[fingerprint] = updated
            if not self._persist(candidate):
                return False
            self._records = candidate
            return True

    def tracks_group(self, group_id: str) -> bool:
        group = _exact_text(group_id, "proactive_group_invalid", maximum=160)
        with self._lock:
            return bool(
                self._policy.enabled
                and group in self._policy.group_allowlist
                and not self._failure_code
                and self._secret
            )

    def _active_hour(self, now: float) -> bool:
        local_seconds = now + self._policy.timezone_offset_minutes * 60
        hour = int((local_seconds % 86400) // 3600)
        start = self._policy.active_hour_start
        end = self._policy.active_hour_end
        if end == 24:
            return hour >= start
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _day(self, now: float) -> int:
        return int((now + self._policy.timezone_offset_minutes * 60) // 86400)

    def _issue_decision(
        self,
        candidate: ProactiveTriggerCandidate,
        *,
        kind: ProactivePolicyDecisionKind,
        now: float,
        retry_after: float = 0.0,
    ) -> ProactivePolicyDecision:
        admitted = kind is ProactivePolicyDecisionKind.ADMITTED
        decision = object.__new__(ProactivePolicyDecision)
        for name, value in (
            ("candidate", candidate),
            ("kind", kind),
            ("admitted", admitted),
            ("evaluated_at", now),
            ("retry_after_seconds", max(0.0, float(retry_after))),
            ("model_authorized", False),
            ("send_authorized", False),
            ("_state_ref", weakref.ref(self)),
            ("_seal", _DECISION_SEAL),
        ):
            object.__setattr__(decision, name, value)
        snapshot = _decision_snapshot(decision)
        record = _DecisionRecord(
            candidate=candidate,
            kind=kind,
            admitted=admitted,
            evaluated_at=now,
            retry_after_seconds=max(0.0, float(retry_after)),
            model_authorized=False,
            send_authorized=False,
            snapshot=snapshot,
        )
        self._decisions[decision] = record
        self._evaluated[candidate] = weakref.ref(decision)
        return decision

    def evaluate(
        self,
        candidate: ProactiveTriggerCandidate,
        *,
        now: float,
    ) -> ProactivePolicyDecision:
        timestamp = _exact_time(now, "proactive_policy_clock_invalid")
        self._trigger_authority.inspect_candidate(candidate)
        with self._lock:
            if candidate in self._evaluated:
                raise ContractViolation("proactive_candidate_evaluated")
            source = candidate.source
            observation = source.observation
            target = candidate.target
            observed_at = observation.observed_at
            if timestamp < observed_at:
                return self._issue_decision(
                    candidate,
                    kind=ProactivePolicyDecisionKind.CLOCK_REGRESSED,
                    now=timestamp,
                )
            generation_status = self._trigger_authority.validate_candidate(candidate)
            if not generation_status.is_current:
                return self._issue_decision(
                    candidate,
                    kind=ProactivePolicyDecisionKind.CANDIDATE_STALE,
                    now=timestamp,
                )
            if not self._policy.enabled or not self._policy.group_allowlist:
                return self._issue_decision(
                    candidate,
                    kind=ProactivePolicyDecisionKind.DISABLED,
                    now=timestamp,
                )
            if target.group_id not in self._policy.group_allowlist:
                return self._issue_decision(
                    candidate,
                    kind=ProactivePolicyDecisionKind.GROUP_NOT_ALLOWED,
                    now=timestamp,
                )
            if self._failure_code or not self._secret:
                return self._issue_decision(
                    candidate,
                    kind=ProactivePolicyDecisionKind.STATE_UNAVAILABLE,
                    now=timestamp,
                )
            if not self._active_hour(timestamp):
                return self._issue_decision(
                    candidate,
                    kind=ProactivePolicyDecisionKind.OUTSIDE_ACTIVE_HOURS,
                    now=timestamp,
                )
            fingerprint = self._fingerprint(
                target.platform_id,
                target.bot_id,
                target.group_id,
            )
            current = self._records.get(fingerprint)
            if current is None:
                if len(self._records) >= self._policy.max_groups:
                    return self._issue_decision(
                        candidate,
                        kind=ProactivePolicyDecisionKind.STATE_CAPACITY,
                        now=timestamp,
                    )
                current = _GroupState(
                    fingerprint=fingerprint,
                    first_observed_at=observed_at,
                    last_activity_at=observed_at,
                    last_seen_at=timestamp,
                    last_trigger_at=0.0,
                    trigger_day=-1,
                    daily_count=0,
                    last_trigger_observed_at=0.0,
                )
            if timestamp < current.last_seen_at or observed_at < current.first_observed_at:
                return self._issue_decision(
                    candidate,
                    kind=ProactivePolicyDecisionKind.CLOCK_REGRESSED,
                    now=timestamp,
                )
            day = self._day(timestamp)
            daily_count = current.daily_count if current.trigger_day == day else 0
            kind = ProactivePolicyDecisionKind.ADMITTED
            retry_after = 0.0
            if (
                current.last_trigger_observed_at > 0
                and current.last_activity_at <= current.last_trigger_observed_at
            ):
                kind = ProactivePolicyDecisionKind.OBSERVATION_REPLAYED
            elif timestamp - current.first_observed_at < self._policy.observation_seconds:
                kind = ProactivePolicyDecisionKind.OBSERVATION_INCOMPLETE
                retry_after = self._policy.observation_seconds - (
                    timestamp - current.first_observed_at
                )
            elif timestamp - current.last_activity_at < self._policy.idle_seconds:
                kind = ProactivePolicyDecisionKind.GROUP_NOT_IDLE
                retry_after = self._policy.idle_seconds - (
                    timestamp - current.last_activity_at
                )
            elif (
                current.last_trigger_at > 0
                and timestamp - current.last_trigger_at < self._policy.cooldown_seconds
            ):
                kind = ProactivePolicyDecisionKind.COOLDOWN_ACTIVE
                retry_after = self._policy.cooldown_seconds - (
                    timestamp - current.last_trigger_at
                )
            elif daily_count >= self._policy.daily_limit:
                kind = ProactivePolicyDecisionKind.DAILY_LIMIT

            updated = replace(current, last_seen_at=timestamp)
            if kind is ProactivePolicyDecisionKind.ADMITTED:
                updated = replace(
                    updated,
                    last_trigger_observed_at=current.last_activity_at,
                )
            persisted = dict(self._records)
            persisted[fingerprint] = updated
            if not self._persist(persisted):
                return self._issue_decision(
                    candidate,
                    kind=ProactivePolicyDecisionKind.STATE_UNAVAILABLE,
                    now=timestamp,
                )
            self._records = persisted
            return self._issue_decision(
                candidate,
                kind=kind,
                now=timestamp,
                retry_after=retry_after,
            )

    def record_terminal(
        self,
        decision: ProactivePolicyDecision,
        terminal: ProactivePolicyTerminalKind,
        *,
        now: float,
    ) -> None:
        timestamp = _exact_time(now, "proactive_terminal_clock_invalid")
        if type(terminal) is not ProactivePolicyTerminalKind:
            raise ContractViolation("proactive_terminal_kind_invalid")
        with self._lock:
            record = self._inspect(decision, require_current=False)
            if record.kind is not ProactivePolicyDecisionKind.ADMITTED or not record.admitted:
                raise ContractViolation("proactive_terminal_policy_not_admitted")
            if decision in self._terminals:
                raise ContractViolation("proactive_terminal_replayed")
            if terminal is ProactivePolicyTerminalKind.SENT:
                target = record.candidate.target
                fingerprint = self._fingerprint(
                    target.platform_id,
                    target.bot_id,
                    target.group_id,
                )
                current = self._records.get(fingerprint)
                if current is None:
                    raise ContractViolation("proactive_terminal_state_missing")
                day = self._day(timestamp)
                daily_count = current.daily_count if current.trigger_day == day else 0
                if daily_count >= self._policy.daily_limit:
                    raise ContractViolation("proactive_terminal_daily_limit")
                updated = replace(
                    current,
                    last_seen_at=max(current.last_seen_at, timestamp),
                    last_trigger_at=timestamp,
                    trigger_day=day,
                    daily_count=daily_count + 1,
                )
                persisted = dict(self._records)
                persisted[fingerprint] = updated
                if not self._persist(persisted):
                    raise ContractViolation("proactive_terminal_state_unavailable")
                self._records = persisted
            self._terminals[decision] = terminal

    def _inspect(
        self,
        decision: ProactivePolicyDecision,
        *,
        require_current: bool = True,
    ) -> _DecisionRecord:
        if type(decision) is not ProactivePolicyDecision:
            raise ContractViolation("proactive_policy_decision_not_canonical")
        with self._lock:
            record = self._decisions.get(decision)
            if type(record) is not _DecisionRecord:
                raise ContractViolation("proactive_policy_decision_not_canonical")
            try:
                snapshot = _decision_snapshot(decision)
            except ContractViolation as exc:
                raise ContractViolation("proactive_policy_decision_corrupt") from exc
            if (
                not _exact_parts(snapshot, record.snapshot)
                or decision.candidate is not record.candidate
                or decision._state_ref() is not self
            ):
                raise ContractViolation("proactive_policy_decision_corrupt")
            self._trigger_authority.inspect_candidate(record.candidate)
            if require_current and not self._trigger_authority.validate_candidate(
                record.candidate
            ).is_current:
                raise ContractViolation("proactive_policy_candidate_stale")
            return record

    def inspect(self, decision: ProactivePolicyDecision) -> ProactivePolicyDecision:
        self._inspect(decision)
        return decision

    def inspect_integrity(
        self,
        decision: ProactivePolicyDecision,
    ) -> ProactivePolicyDecision:
        self._inspect(decision, require_current=False)
        return decision

    def trace_metadata(self) -> dict[str, str | int | bool]:
        with self._lock:
            return {
                "schema_version": 2,
                "proactive_policy_enabled": self._policy.enabled,
                "proactive_allowlist_empty": not bool(self._policy.group_allowlist),
                "proactive_state_operational": bool(
                    self._policy.enabled
                    and self._policy.group_allowlist
                    and not self._failure_code
                    and self._secret
                ),
                "proactive_group_state_count": len(self._records),
                "proactive_decision_count": len(self._decisions),
                "proactive_terminal_count": len(self._terminals),
                "proactive_failure_code": self._failure_code,
            }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ProactivePolicyState("
            f"enabled={metadata['proactive_policy_enabled']!r}, "
            f"allowlist_empty={metadata['proactive_allowlist_empty']!r}, "
            f"operational={metadata['proactive_state_operational']!r})"
        )


__all__ = [
    "ProactivePolicyConfig",
    "ProactivePolicyDecision",
    "ProactivePolicyDecisionKind",
    "ProactivePolicyTerminalKind",
    "ProactivePolicyState",
]
