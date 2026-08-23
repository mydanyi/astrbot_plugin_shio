from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from time import time as _wall_time


_SCHEMA_VERSION = 1
_STATE_FILENAME = "runtime_continuity.json"
_SECRET_FILENAME = ".runtime_continuity_secret"
_MAX_STATE_BYTES = 1024 * 1024
_SECRET_BYTES = 32
_FAILURE_CODES = frozenset(
    {
        "",
        "continuity_closed",
        "continuity_root_invalid",
        "continuity_secret_invalid",
        "continuity_state_invalid",
        "continuity_state_changed",
        "continuity_persist_failed",
        "continuity_store_in_use",
    }
)
_PROCESS_LOCK = threading.RLock()
_PROCESS_STORES: weakref.WeakValueDictionary[str, RuntimeContinuityStore] = (
    weakref.WeakValueDictionary()
)


class RuntimeContinuityError(RuntimeError):
    pass


def _exact_text(value: object, reason: str, *, maximum: int = 1024) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise RuntimeContinuityError(reason)
    return value


def _exact_nonnegative_float(value: object, reason: str) -> float:
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise RuntimeContinuityError(reason)
    return value


def _exact_positive_int(value: object, reason: str, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise RuntimeContinuityError(reason)
    return value


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate_or_invalid_key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CadenceContinuity:
    last_evaluated_at: float
    join_times: tuple[float, ...]
    no_action_streak: int
    backoff_until: float

    def __post_init__(self) -> None:
        _exact_nonnegative_float(
            self.last_evaluated_at,
            "continuity_cadence_invalid",
        )
        if (
            type(self.join_times) is not tuple
            or len(self.join_times) > 16
            or any(
                type(value) is not float
                or not math.isfinite(value)
                or value < 0
                or value > self.last_evaluated_at
                for value in self.join_times
            )
        ):
            raise RuntimeContinuityError("continuity_cadence_invalid")
        if (
            type(self.no_action_streak) is not int
            or not 0 <= self.no_action_streak <= 16
            or type(self.backoff_until) is not float
            or not math.isfinite(self.backoff_until)
            or self.backoff_until < 0
        ):
            raise RuntimeContinuityError("continuity_cadence_invalid")


@dataclass(frozen=True, slots=True)
class _StoredCadence:
    saved_at: float
    join_ages: tuple[float, ...]
    no_action_streak: int
    backoff_remaining: float


class RuntimeContinuityStore:
    """Atomic, bounded, privacy-minimal restart state for revision/cadence."""

    __slots__ = (
        "_closed",
        "_failure_code",
        "_lock",
        "_max_scopes",
        "_max_subjects",
        "_path_key",
        "_records_cadence",
        "_records_revision",
        "_root",
        "_secret",
        "_secret_path",
        "_state_digest",
        "_state_path",
        "__weakref__",
    )

    def __init__(
        self,
        root: Path,
        *,
        max_scopes: int = 2048,
        max_subjects: int = 256,
    ) -> None:
        if not isinstance(root, Path):
            raise RuntimeContinuityError("continuity_root_invalid")
        self._max_scopes = _exact_positive_int(
            max_scopes,
            "continuity_scope_limit_invalid",
            maximum=8192,
        )
        self._max_subjects = _exact_positive_int(
            max_subjects,
            "continuity_subject_limit_invalid",
            maximum=8192,
        )
        self._lock = threading.RLock()
        self._failure_code = ""
        self._closed = False
        self._root = root.resolve()
        self._state_path = self._root / _STATE_FILENAME
        self._secret_path = self._root / _SECRET_FILENAME
        self._path_key = str(self._root).casefold()
        self._secret = b""
        self._records_revision: dict[str, int] = {}
        self._records_cadence: dict[str, _StoredCadence] = {}
        self._state_digest = ""

        with _PROCESS_LOCK:
            existing = _PROCESS_STORES.get(self._path_key)
            if (
                existing is not None
                and not existing._closed
                and not existing._failure_code
            ):
                self._disable("continuity_store_in_use")
                return
            _PROCESS_STORES[self._path_key] = self
        try:
            self._initialize()
        except BaseException:
            self._disable(
                self._failure_code
                if self._failure_code in _FAILURE_CODES and self._failure_code
                else "continuity_state_invalid"
            )

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _disable(self, code: str) -> None:
        self._failure_code = code if code in _FAILURE_CODES and code else "continuity_state_invalid"

    def _initialize(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir() or self._root.is_symlink():
            self._failure_code = "continuity_root_invalid"
            raise RuntimeContinuityError("continuity_root_invalid")
        try:
            os.chmod(self._root, 0o700)
        except OSError as exc:
            self._failure_code = "continuity_root_invalid"
            raise RuntimeContinuityError("continuity_root_invalid") from exc
        self._secret = self._load_or_create_secret()
        if self._state_path.exists():
            raw = self._read_state_bytes()
            revisions, cadence = self._decode_state(raw)
            self._records_revision = revisions
            self._records_cadence = cadence
            self._state_digest = hashlib.sha256(raw).hexdigest()
        else:
            self._persist_candidate({}, {})

    def _load_or_create_secret(self) -> bytes:
        if self._secret_path.exists():
            try:
                details = self._secret_path.stat()
                raw = self._secret_path.read_bytes()
            except OSError as exc:
                self._failure_code = "continuity_secret_invalid"
                raise RuntimeContinuityError("continuity_secret_invalid") from exc
            if (
                not stat.S_ISREG(details.st_mode)
                or self._secret_path.is_symlink()
                or details.st_nlink != 1
                or len(raw) != _SECRET_BYTES
                or (
                    os.name != "nt"
                    and stat.S_IMODE(details.st_mode) & 0o077 != 0
                )
            ):
                self._failure_code = "continuity_secret_invalid"
                raise RuntimeContinuityError("continuity_secret_invalid")
            return raw
        if self._state_path.exists():
            self._failure_code = "continuity_secret_invalid"
            raise RuntimeContinuityError("continuity_secret_invalid")
        raw = secrets.token_bytes(_SECRET_BYTES)
        try:
            descriptor = os.open(
                self._secret_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
        except BaseException as exc:
            self._failure_code = "continuity_secret_invalid"
            raise RuntimeContinuityError("continuity_secret_invalid") from exc
        return raw

    def _read_state_bytes(self) -> bytes:
        try:
            details = self._state_path.stat()
            if (
                not stat.S_ISREG(details.st_mode)
                or self._state_path.is_symlink()
                or details.st_nlink != 1
                or details.st_size > _MAX_STATE_BYTES
                or (
                    os.name != "nt"
                    and stat.S_IMODE(details.st_mode) & 0o077 != 0
                )
            ):
                raise ValueError("state_shape")
            raw = self._state_path.read_bytes()
        except (OSError, ValueError) as exc:
            raise RuntimeContinuityError("continuity_state_invalid") from exc
        if len(raw) > _MAX_STATE_BYTES:
            raise RuntimeContinuityError("continuity_state_invalid")
        return raw

    @staticmethod
    def _revision_entry(value: object) -> tuple[str, int]:
        if type(value) is not dict or set(value) != {"key", "revision"}:
            raise RuntimeContinuityError("continuity_state_invalid")
        key = value["key"]
        revision = value["revision"]
        if (
            type(key) is not str
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or type(revision) is not int
            or not 1 <= revision <= 2**63 - 1
        ):
            raise RuntimeContinuityError("continuity_state_invalid")
        return key, revision

    @staticmethod
    def _cadence_entry(value: object) -> tuple[str, _StoredCadence]:
        expected = {
            "key",
            "saved_at",
            "join_ages",
            "no_action_streak",
            "backoff_remaining",
        }
        if type(value) is not dict or set(value) != expected:
            raise RuntimeContinuityError("continuity_state_invalid")
        key = value["key"]
        saved_at = value["saved_at"]
        join_ages = value["join_ages"]
        streak = value["no_action_streak"]
        remaining = value["backoff_remaining"]
        if (
            type(key) is not str
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or type(saved_at) is not float
            or not math.isfinite(saved_at)
            or saved_at < 0
            or type(join_ages) is not list
            or len(join_ages) > 16
            or any(
                type(age) is not float or not math.isfinite(age) or age < 0
                for age in join_ages
            )
            or type(streak) is not int
            or not 0 <= streak <= 16
            or type(remaining) is not float
            or not math.isfinite(remaining)
            or remaining < 0
        ):
            raise RuntimeContinuityError("continuity_state_invalid")
        return key, _StoredCadence(
            saved_at=saved_at,
            join_ages=tuple(join_ages),
            no_action_streak=streak,
            backoff_remaining=remaining,
        )

    def _decode_state(
        self,
        raw: bytes,
    ) -> tuple[dict[str, int], dict[str, _StoredCadence]]:
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("nonfinite")
                ),
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeContinuityError("continuity_state_invalid") from exc
        if (
            type(payload) is not dict
            or set(payload) != {"schema_version", "revisions", "cadence", "mac"}
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != _SCHEMA_VERSION
            or type(payload["revisions"]) is not list
            or type(payload["cadence"]) is not list
            or type(payload["mac"]) is not str
            or len(payload["mac"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in payload["mac"]
            )
            or len(payload["revisions"]) > self._max_scopes
            or len(payload["cadence"]) > self._max_subjects
        ):
            raise RuntimeContinuityError("continuity_state_invalid")
        body = {
            "schema_version": payload["schema_version"],
            "revisions": payload["revisions"],
            "cadence": payload["cadence"],
        }
        body_raw = json.dumps(
            body,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected_mac = hmac.new(
            self._secret,
            b"continuity-state-v1\x00" + body_raw,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(payload["mac"], expected_mac):
            raise RuntimeContinuityError("continuity_state_invalid")
        revisions = dict(self._revision_entry(item) for item in payload["revisions"])
        cadence = dict(self._cadence_entry(item) for item in payload["cadence"])
        if (
            len(revisions) != len(payload["revisions"])
            or len(cadence) != len(payload["cadence"])
        ):
            raise RuntimeContinuityError("continuity_state_invalid")
        return revisions, cadence

    def _encode_state(
        self,
        revisions: dict[str, int],
        cadence: dict[str, _StoredCadence],
    ) -> bytes:
        body = {
            "schema_version": _SCHEMA_VERSION,
            "revisions": [
                {"key": key, "revision": revisions[key]}
                for key in sorted(revisions)
            ],
            "cadence": [
                {
                    "key": key,
                    "saved_at": cadence[key].saved_at,
                    "join_ages": list(cadence[key].join_ages),
                    "no_action_streak": cadence[key].no_action_streak,
                    "backoff_remaining": cadence[key].backoff_remaining,
                }
                for key in sorted(cadence)
            ],
        }
        body_raw = json.dumps(
            body,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        payload = {
            **body,
            "mac": hmac.new(
                self._secret,
                b"continuity-state-v1\x00" + body_raw,
                hashlib.sha256,
            ).hexdigest(),
        }
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > _MAX_STATE_BYTES:
            raise RuntimeContinuityError("continuity_state_invalid")
        return raw

    def _persist_candidate(
        self,
        revisions: dict[str, int],
        cadence: dict[str, _StoredCadence],
    ) -> None:
        raw = self._encode_state(revisions, cadence)
        temporary = self._root / (
            f".{_STATE_FILENAME}.{secrets.token_hex(8)}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_path)
            if os.name != "nt":
                directory = os.open(self._root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except BaseException as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self._disable("continuity_persist_failed")
            raise RuntimeContinuityError("continuity_persist_failed") from exc
        self._records_revision = revisions
        self._records_cadence = cadence
        self._state_digest = hashlib.sha256(raw).hexdigest()

    def _health_locked(self) -> None:
        if self._closed:
            raise RuntimeContinuityError("continuity_state_unavailable")
        if self._failure_code == "continuity_store_in_use":
            with _PROCESS_LOCK:
                existing = _PROCESS_STORES.get(self._path_key)
                if (
                    existing is not None
                    and existing is not self
                    and not existing._closed
                    and not existing._failure_code
                ):
                    raise RuntimeContinuityError("continuity_state_unavailable")
                _PROCESS_STORES[self._path_key] = self
            self._failure_code = ""
            try:
                self._initialize()
            except BaseException as exc:
                self._disable("continuity_state_invalid")
                raise RuntimeContinuityError("continuity_state_unavailable") from exc
        if self._failure_code:
            raise RuntimeContinuityError("continuity_state_unavailable")
        try:
            raw = self._read_state_bytes()
        except RuntimeContinuityError as exc:
            self._disable("continuity_state_changed")
            raise RuntimeContinuityError("continuity_state_unavailable") from exc
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), self._state_digest):
            self._disable("continuity_state_changed")
            raise RuntimeContinuityError("continuity_state_unavailable")

    def _fingerprint(self, domain: bytes, *parts: str) -> str:
        material = "\x1f".join(
            _exact_text(part, "continuity_key_invalid") for part in parts
        ).encode("utf-8")
        return hmac.new(self._secret, domain + b"\x00" + material, hashlib.sha256).hexdigest()

    @property
    def enabled(self) -> bool:
        with self._lock:
            try:
                self._health_locked()
            except RuntimeContinuityError:
                return False
            return True

    @property
    def failure_code(self) -> str:
        with self._lock:
            if not self._failure_code and not self._closed:
                try:
                    self._health_locked()
                except RuntimeContinuityError:
                    pass
            if self._closed:
                return "continuity_closed"
            return self._failure_code

    def revision_for_scope(self, scope_key: str) -> int:
        with self._lock:
            self._health_locked()
            key = self._fingerprint(b"revision-v1", scope_key)
            return self._records_revision.get(key, 0)

    def commit_revision(
        self,
        scope_key: str,
        *,
        base_revision: int,
        next_revision: int,
    ) -> None:
        if (
            type(base_revision) is not int
            or base_revision < 0
            or type(next_revision) is not int
            or next_revision != base_revision + 1
        ):
            raise RuntimeContinuityError("continuity_revision_invalid")
        with self._lock:
            self._health_locked()
            key = self._fingerprint(b"revision-v1", scope_key)
            current = self._records_revision.get(key, 0)
            if current != base_revision:
                raise RuntimeContinuityError("continuity_revision_stale")
            if key not in self._records_revision and len(self._records_revision) >= self._max_scopes:
                raise RuntimeContinuityError("continuity_scope_capacity")
            revisions = dict(self._records_revision)
            revisions[key] = next_revision
            self._persist_candidate(revisions, dict(self._records_cadence))

    def cadence_for_subject(
        self,
        scope_key: str,
        sender_key: str,
        *,
        now: float,
    ) -> CadenceContinuity | None:
        current = _exact_nonnegative_float(now, "continuity_cadence_clock_invalid")
        with self._lock:
            self._health_locked()
            key = self._fingerprint(b"cadence-v1", scope_key, sender_key)
            stored = self._records_cadence.get(key)
            if stored is None:
                return None
            elapsed = max(0.0, float(_wall_time() - stored.saved_at))
            joins = tuple(
                max(0.0, current - (age + elapsed)) for age in stored.join_ages
            )
            return CadenceContinuity(
                last_evaluated_at=current,
                join_times=joins,
                no_action_streak=stored.no_action_streak,
                backoff_until=current
                + max(0.0, stored.backoff_remaining - elapsed),
            )

    def commit_cadence(
        self,
        scope_key: str,
        sender_key: str,
        *,
        state: CadenceContinuity,
        now: float,
    ) -> None:
        current = _exact_nonnegative_float(now, "continuity_cadence_clock_invalid")
        if type(state) is not CadenceContinuity:
            raise RuntimeContinuityError("continuity_cadence_invalid")
        state.__post_init__()
        if state.last_evaluated_at != current:
            raise RuntimeContinuityError("continuity_cadence_clock_mismatch")
        with self._lock:
            self._health_locked()
            key = self._fingerprint(b"cadence-v1", scope_key, sender_key)
            if key not in self._records_cadence and len(self._records_cadence) >= self._max_subjects:
                raise RuntimeContinuityError("continuity_subject_capacity")
            stored = _StoredCadence(
                saved_at=float(_wall_time()),
                join_ages=tuple(max(0.0, current - value) for value in state.join_times),
                no_action_streak=state.no_action_streak,
                backoff_remaining=max(0.0, state.backoff_until - current),
            )
            cadence = dict(self._records_cadence)
            cadence[key] = stored
            self._persist_candidate(dict(self._records_revision), cadence)

    def trace_metadata(self) -> dict[str, int | bool | str]:
        with self._lock:
            available = self.enabled
            return {
                "schema_version": _SCHEMA_VERSION,
                "continuity_state_available": available,
                "continuity_state_bounded": bool(
                    len(self._records_revision) <= self._max_scopes
                    and len(self._records_cadence) <= self._max_subjects
                ),
                "continuity_revision_count": len(self._records_revision),
                "continuity_cadence_count": len(self._records_cadence),
                "continuity_failure_code": (
                    "" if available else self.failure_code
                ),
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with _PROCESS_LOCK:
            if _PROCESS_STORES.get(self._path_key) is self:
                _PROCESS_STORES.pop(self._path_key, None)

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "RuntimeContinuityStore("
            f"available={metadata['continuity_state_available']!r}, "
            f"bounded={metadata['continuity_state_bounded']!r}, "
            f"revision_count={metadata['continuity_revision_count']!r}, "
            f"cadence_count={metadata['continuity_cadence_count']!r})"
        )


__all__ = [
    "CadenceContinuity",
    "RuntimeContinuityError",
    "RuntimeContinuityStore",
]
