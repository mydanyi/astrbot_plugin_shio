"""Minimal durable lifecycle tombstones for owner-action integration.

This module is deliberately separate from :mod:`owner_action_controller`.  It
does not issue a request, execution lease, receipt, or ``ActionOutcome`` and it
does not retain Controller-owned parameter material.  The later integration
seam may project an exact Controller transition into this store, but a
``LifecycleHandle`` is never authority to execute an action.

The journal contains only a secret-derived request fingerprint, closed
operation/status/effect values, an attempted bit, and lifecycle timestamps.
No request digest, path, command, memory literal, tool output, or real platform
identifier is persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import socket
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .contracts import (
    ActionEffectState,
    ActionReceiptStatus,
    OwnerActionOperation,
)


_SCHEMA_VERSION = 1
_JOURNAL_NAME = "owner_action_lifecycle.json"
_LOCK_NAME = ".owner_action_lifecycle.lock"
_ANCHOR_NAME = ".owner_action_lifecycle.anchor"
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024
_ANCHOR_VERSION = 1
_MAX_ANCHOR_BYTES = 512
_MIN_SECRET_BYTES = 32
_MAX_SECRET_BYTES = 256
_DEFAULT_MAX_RECORDS = 256
_MAX_RECORDS = 4096
_MIN_TOMBSTONE_TTL_SECONDS = 900.0
_MAX_TOMBSTONE_TTL_SECONDS = 31_536_000.0
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_STORE_KEYS = frozenset(
    {"schema_version", "secret_verifier", "generation", "records"}
)
_RECORD_KEYS = frozenset(
    {
        "attempted",
        "created_at",
        "delivery_ack_at",
        "effect",
        "fingerprint",
        "operation",
        "phase",
        "result_status",
        "terminal_at",
        "updated_at",
    }
)
_READ_OPERATIONS = frozenset(
    {
        OwnerActionOperation.ARTIFACT_READ_EXACT,
        OwnerActionOperation.ARTIFACT_GREP,
    }
)
_MUTATION_OPERATIONS = frozenset(
    {
        OwnerActionOperation.MEMORY_WRITE_LITERAL,
        OwnerActionOperation.SANDBOX_SHELL_ONCE,
    }
)
_FAILURE_CODES = frozenset(
    {
        "active_attempted_invalid",
        "active_effect_invalid",
        "active_record_shape_invalid",
        "atomic_write_failed",
        "atomic_write_interrupted",
        "created_at_invalid",
        "delivery_ack_at_invalid",
        "delivery_ack_time_invalid",
        "install_secret_mismatch",
        "journal_corrupt",
        "journal_duplicate_key",
        "journal_file_changed",
        "journal_generation_invalid",
        "journal_load_failed",
        "journal_nonfinite_number",
        "journal_not_canonical",
        "journal_not_regular",
        "journal_permissions_invalid",
        "journal_shape_invalid",
        "journal_size_invalid",
        "journal_symlink_rejected",
        "journal_anchor_invalid",
        "lifecycle_fail_closed",
        "lifecycle_initialization_interrupted",
        "lifecycle_lock_identity_changed",
        "lifecycle_lock_not_regular",
        "lifecycle_lock_permissions_invalid",
        "lifecycle_lock_symlink_rejected",
        "lifecycle_lock_validation_interrupted",
        "lifecycle_root_identity_invalid",
        "lifecycle_root_invalid",
        "lifecycle_root_permissions_invalid",
        "lifecycle_root_symlink_rejected",
        "lifecycle_store_closed",
        "lifecycle_store_locked",
        "record_attempted_invalid",
        "record_count_exceeds_bound",
        "record_enum_invalid",
        "record_fingerprint_duplicate",
        "record_fingerprint_invalid",
        "record_shape_invalid",
        "record_status_invalid",
        "record_time_order_invalid",
        "terminal_ack_invalid",
        "terminal_at_invalid",
        "terminal_record_shape_invalid",
        "terminal_semantics_invalid",
        "unknown_schema",
        "updated_at_invalid",
    }
)

_ANCHOR_KEYS = frozenset(
    {"anchor_version", "generation", "journal_digest", "mac"}
)


def _closed_failure_code(code: Any, fallback: str) -> str:
    safe_fallback = (
        fallback
        if type(fallback) is str and fallback in _FAILURE_CODES
        else "lifecycle_fail_closed"
    )
    return code if type(code) is str and code in _FAILURE_CODES else safe_fallback


def _exception_failure_code(exc: BaseException, fallback: str) -> str:
    try:
        args = exc.args
    except BaseException:
        return _closed_failure_code(None, fallback)
    candidate = args[0] if type(args) is tuple and len(args) == 1 else None
    return _closed_failure_code(candidate, fallback)


def _raise_safe_interruption(code: str, exc: BaseException) -> None:
    """Preserve process-control semantics without re-emitting attacker text."""

    if type(exc) is KeyboardInterrupt:
        raise KeyboardInterrupt() from None
    if type(exc) is SystemExit:
        raise SystemExit() from None
    if type(exc) is RuntimeError:
        raise RuntimeError() from None
    raise LifecycleStoreDisabled(_closed_failure_code(code, code)) from None


def _anchor_mac(
    secret: bytes,
    generation: int,
    journal_digest: str,
) -> str:
    payload = (
        f"shio-owner-action-lifecycle-anchor-v1\0{generation}\0"
        f"{journal_digest}"
    ).encode("ascii")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _anchor_bytes(
    secret: bytes,
    generation: int,
    journal_digest: str,
) -> bytes:
    return _canonical_json_bytes(
        {
            "anchor_version": _ANCHOR_VERSION,
            "generation": generation,
            "journal_digest": journal_digest,
            "mac": _anchor_mac(secret, generation, journal_digest),
        }
    )


def _parse_anchor(
    raw: bytes,
    secret: bytes,
) -> tuple[int, str] | None:
    if raw in {b"", b"\x00"}:
        return None
    if not (0 < len(raw) <= _MAX_ANCHOR_BYTES):
        raise ValueError("journal_anchor_invalid")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("journal_anchor_invalid") from None
    if raw != _canonical_json_bytes(payload):
        raise ValueError("journal_anchor_invalid")
    if type(payload) is not dict or frozenset(payload) != _ANCHOR_KEYS:
        raise ValueError("journal_anchor_invalid")
    version = payload["anchor_version"]
    generation = payload["generation"]
    digest = payload["journal_digest"]
    mac = payload["mac"]
    if (
        type(version) is not int
        or version != _ANCHOR_VERSION
        or type(generation) is not int
        or generation < 1
        or type(digest) is not str
        or _HEX_64.fullmatch(digest) is None
        or type(mac) is not str
        or _HEX_64.fullmatch(mac) is None
    ):
        raise ValueError("journal_anchor_invalid")
    if not hmac.compare_digest(mac, _anchor_mac(secret, generation, digest)):
        raise ValueError("install_secret_mismatch")
    return generation, digest


class LifecycleStoreDisabled(RuntimeError):
    """The durable boundary is all-off and must not authorize work."""


class LifecycleTransitionRejected(RuntimeError):
    """A lifecycle mutation was non-canonical or contradicted durable state."""


class LifecyclePhase(str, Enum):
    RESERVED = "reserved"
    IN_PROGRESS = "in_progress"
    TERMINAL = "terminal"
    DELIVERY_ACK = "delivery_ack"


class LifecycleReserveDisposition(str, Enum):
    RESERVED = "reserved"
    EXISTING_ACTIVE = "existing_active"
    EXISTING_TERMINAL = "existing_terminal"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class LifecycleHandle:
    """Opaque store-local identity; explicitly not an execution capability."""

    operation: OwnerActionOperation
    _request_fingerprint: str
    _store_nonce: object

    def __repr__(self) -> str:
        return (
            "LifecycleHandle("
            "operation_bound=True, canonical_bound=True, "
            "execution_authority=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LifecycleSnapshot:
    operation: OwnerActionOperation
    phase: LifecyclePhase
    result_status: ActionReceiptStatus | None
    effect_state: ActionEffectState
    attempted: bool
    created_at: float
    updated_at: float
    terminal_at: float
    delivery_ack_at: float

    @property
    def may_execute(self) -> bool:
        """Always false: Controller and its exact lease remain authoritative."""

        return False

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "operation": self.operation.value,
            "phase": self.phase.value,
            "result_status": (
                "none" if self.result_status is None else self.result_status.value
            ),
            "effect": self.effect_state.value,
            "attempted": self.attempted,
            "request_bound": True,
            "request_material_visible": False,
            "execution_authority": False,
        }

    def __repr__(self) -> str:
        return (
            "LifecycleSnapshot("
            f"operation={self.operation.value!r}, phase={self.phase.value!r}, "
            f"result_status={self.trace_metadata()['result_status']!r}, "
            f"effect={self.effect_state.value!r}, attempted={self.attempted!r}, "
            "request_bound=True, request_material_visible=False, "
            "execution_authority=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LifecycleReservation:
    handle: LifecycleHandle
    disposition: LifecycleReserveDisposition
    snapshot: LifecycleSnapshot

    def __repr__(self) -> str:
        return (
            "LifecycleReservation("
            f"disposition={self.disposition.value!r}, "
            f"phase={self.snapshot.phase.value!r}, request_bound=True, "
            "request_material_visible=False, execution_authority=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LifecycleStartClaim:
    claimed: bool
    snapshot: LifecycleSnapshot

    def __repr__(self) -> str:
        return (
            "LifecycleStartClaim("
            f"claimed={self.claimed!r}, phase={self.snapshot.phase.value!r}, "
            "request_material_visible=False, execution_authority=False)"
        )


@dataclass(frozen=True, slots=True)
class _Record:
    fingerprint: str
    operation: OwnerActionOperation
    phase: LifecyclePhase
    result_status: ActionReceiptStatus | None
    effect_state: ActionEffectState
    attempted: bool
    created_at: float
    updated_at: float
    terminal_at: float
    delivery_ack_at: float


def _require_time(value: Any, field_name: str) -> float:
    if type(value) not in {int, float}:
        raise LifecycleTransitionRejected(f"{field_name}_invalid")
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise LifecycleTransitionRejected(f"{field_name}_invalid") from None
    if not math.isfinite(normalized) or normalized < 0.0:
        raise LifecycleTransitionRejected(f"{field_name}_invalid")
    return normalized


def _parse_stored_time(value: Any, field_name: str) -> float:
    if type(value) is not float:
        raise ValueError(f"{field_name}_invalid")
    try:
        return _require_time(value, field_name)
    except LifecycleTransitionRejected:
        raise ValueError(f"{field_name}_invalid") from None


def _require_request_digest(value: Any) -> str:
    if type(value) is not str or _HEX_64.fullmatch(value) is None:
        raise LifecycleTransitionRejected("request_digest_invalid")
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("journal_duplicate_key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("journal_nonfinite_number")


def _canonical_json_bytes(payload: Any) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("journal_not_canonical") from None


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(file_attributes & reparse_flag)


def _path_has_link_or_reparse(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            break
        if _is_link_or_reparse(current):
            return True
    return False


def _secret_verifier(secret: bytes) -> str:
    return hmac.new(
        secret,
        b"astrbot-plugin-shio/owner-action-lifecycle/install-v1",
        hashlib.sha256,
    ).hexdigest()


def _request_fingerprint(
    secret: bytes,
    operation: OwnerActionOperation,
    request_digest: str,
) -> str:
    material = (
        b"astrbot-plugin-shio/owner-action-lifecycle/request-v1\x00"
        + operation.value.encode("ascii")
        + b"\x00"
        + request_digest.encode("ascii")
    )
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _snapshot(record: _Record) -> LifecycleSnapshot:
    return LifecycleSnapshot(
        operation=record.operation,
        phase=record.phase,
        result_status=record.result_status,
        effect_state=record.effect_state,
        attempted=record.attempted,
        created_at=record.created_at,
        updated_at=record.updated_at,
        terminal_at=record.terminal_at,
        delivery_ack_at=record.delivery_ack_at,
    )


def _record_payload(record: _Record) -> dict[str, str | float | bool]:
    return {
        "attempted": record.attempted,
        "created_at": record.created_at,
        "delivery_ack_at": record.delivery_ack_at,
        "effect": record.effect_state.value,
        "fingerprint": record.fingerprint,
        "operation": record.operation.value,
        "phase": record.phase.value,
        "result_status": (
            "" if record.result_status is None else record.result_status.value
        ),
        "terminal_at": record.terminal_at,
        "updated_at": record.updated_at,
    }


def _validate_terminal_semantics(
    operation: OwnerActionOperation,
    status: ActionReceiptStatus,
    effect: ActionEffectState,
    attempted: bool,
) -> None:
    if status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
        raise LifecycleTransitionRejected("terminal_status_invalid")

    if status in {ActionReceiptStatus.DENIED, ActionReceiptStatus.CANCELLED}:
        valid = not attempted and effect is ActionEffectState.NOT_STARTED
    elif status is ActionReceiptStatus.STALE:
        if not attempted:
            valid = effect is ActionEffectState.NOT_STARTED
        elif operation in _READ_OPERATIONS:
            valid = effect is ActionEffectState.NO_SIDE_EFFECT
        else:
            valid = effect in {
                ActionEffectState.NOT_COMMITTED,
                ActionEffectState.COMMITTED,
                ActionEffectState.PARTIAL,
                ActionEffectState.UNKNOWN,
            }
    elif status is ActionReceiptStatus.SUCCEEDED:
        valid = attempted and (
            (operation in _READ_OPERATIONS and effect is ActionEffectState.NO_SIDE_EFFECT)
            or (
                operation in _MUTATION_OPERATIONS
                and effect is ActionEffectState.COMMITTED
            )
        )
    elif status is ActionReceiptStatus.FAILED:
        valid = attempted and (
            (operation in _READ_OPERATIONS and effect is ActionEffectState.NO_SIDE_EFFECT)
            or (
                operation in _MUTATION_OPERATIONS
                and effect
                in {ActionEffectState.NOT_COMMITTED, ActionEffectState.PARTIAL}
            )
        )
    elif status is ActionReceiptStatus.TIMED_OUT:
        valid = attempted and (
            (operation in _READ_OPERATIONS and effect is ActionEffectState.NO_SIDE_EFFECT)
            or (
                operation in _MUTATION_OPERATIONS
                and effect
                in {
                    ActionEffectState.NOT_COMMITTED,
                    ActionEffectState.PARTIAL,
                    ActionEffectState.UNKNOWN,
                }
            )
        )
    elif status is ActionReceiptStatus.EFFECT_UNKNOWN:
        valid = (
            attempted
            and operation in _MUTATION_OPERATIONS
            and effect is ActionEffectState.UNKNOWN
        )
    else:  # closed Enum, retained as a defensive fail-closed branch
        valid = False
    if not valid:
        raise LifecycleTransitionRejected("terminal_semantics_invalid")


def _parse_record(value: Any) -> _Record:
    if type(value) is not dict or frozenset(value) != _RECORD_KEYS:
        raise ValueError("record_shape_invalid")
    fingerprint = value["fingerprint"]
    if type(fingerprint) is not str or _HEX_64.fullmatch(fingerprint) is None:
        raise ValueError("record_fingerprint_invalid")
    if any(
        type(value[field_name]) is not str
        for field_name in ("operation", "phase", "effect", "result_status")
    ):
        raise ValueError("record_enum_invalid")
    try:
        operation = OwnerActionOperation(value["operation"])
        phase = LifecyclePhase(value["phase"])
        effect = ActionEffectState(value["effect"])
    except (TypeError, ValueError):
        raise ValueError("record_enum_invalid") from None
    attempted = value["attempted"]
    if type(attempted) is not bool:
        raise ValueError("record_attempted_invalid")
    raw_status = value["result_status"]
    if raw_status == "":
        result_status = None
    else:
        try:
            result_status = ActionReceiptStatus(raw_status)
        except (TypeError, ValueError):
            raise ValueError("record_status_invalid") from None
    created_at = _parse_stored_time(value["created_at"], "created_at")
    updated_at = _parse_stored_time(value["updated_at"], "updated_at")
    terminal_at = _parse_stored_time(value["terminal_at"], "terminal_at")
    delivery_ack_at = _parse_stored_time(
        value["delivery_ack_at"], "delivery_ack_at"
    )
    if updated_at < created_at:
        raise ValueError("record_time_order_invalid")
    if phase in {LifecyclePhase.RESERVED, LifecyclePhase.IN_PROGRESS}:
        if result_status is not None or terminal_at != 0.0 or delivery_ack_at != 0.0:
            raise ValueError("active_record_shape_invalid")
        expected_attempted = phase is LifecyclePhase.IN_PROGRESS
        if attempted is not expected_attempted:
            raise ValueError("active_attempted_invalid")
        expected_effect = (
            ActionEffectState.NOT_STARTED
            if phase is LifecyclePhase.RESERVED
            else (
                ActionEffectState.NO_SIDE_EFFECT
                if operation in _READ_OPERATIONS
                else ActionEffectState.UNKNOWN
            )
        )
        if effect is not expected_effect:
            raise ValueError("active_effect_invalid")
    else:
        if result_status is None or terminal_at < created_at or updated_at < terminal_at:
            raise ValueError("terminal_record_shape_invalid")
        if phase is LifecyclePhase.TERMINAL and delivery_ack_at != 0.0:
            raise ValueError("terminal_ack_invalid")
        if phase is LifecyclePhase.DELIVERY_ACK and (
            delivery_ack_at < terminal_at or updated_at < delivery_ack_at
        ):
            raise ValueError("delivery_ack_time_invalid")
        try:
            _validate_terminal_semantics(
                operation,
                result_status,
                effect,
                attempted,
            )
        except LifecycleTransitionRejected:
            raise ValueError("terminal_semantics_invalid") from None
    return _Record(
        fingerprint=fingerprint,
        operation=operation,
        phase=phase,
        result_status=result_status,
        effect_state=effect,
        attempted=attempted,
        created_at=created_at,
        updated_at=updated_at,
        terminal_at=terminal_at,
        delivery_ack_at=delivery_ack_at,
    )


class OwnerActionLifecycleStore:
    """Crash-safe minimal journal; never an action or outcome authority."""

    __slots__ = (
        "_enabled",
        "_failure_code",
        "_generation",
        "_handles",
        "_install_secret",
        "_anchor_path",
        "_journal_digest",
        "_journal_identity",
        "_journal_path",
        "_lock",
        "_lock_fd",
        "_lock_path",
        "_max_records",
        "_namespace_lock",
        "_records",
        "_root",
        "_root_fd",
        "_store_nonce",
        "_tombstone_ttl_seconds",
    )

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        install_secret: bytes,
        recovery_now: float,
        max_records: int = _DEFAULT_MAX_RECORDS,
        tombstone_ttl_seconds: float = _MIN_TOMBSTONE_TTL_SECONDS,
    ) -> None:
        if not isinstance(root, (str, os.PathLike)):
            raise LifecycleTransitionRejected("lifecycle_root_invalid")
        if type(install_secret) is not bytes or not (
            _MIN_SECRET_BYTES <= len(install_secret) <= _MAX_SECRET_BYTES
        ):
            raise LifecycleTransitionRejected("install_secret_invalid")
        if (
            type(max_records) is not int
            or not (1 <= max_records <= _MAX_RECORDS)
        ):
            raise LifecycleTransitionRejected("max_records_invalid")
        ttl = _require_time(tombstone_ttl_seconds, "tombstone_ttl_seconds")
        if not (
            _MIN_TOMBSTONE_TTL_SECONDS
            <= ttl
            <= _MAX_TOMBSTONE_TTL_SECONDS
        ):
            raise LifecycleTransitionRejected("tombstone_ttl_seconds_invalid")
        recovered_at = _require_time(recovery_now, "recovery_now")

        requested_root = Path(root)
        self._root = Path(os.path.abspath(os.fspath(requested_root)))
        self._journal_path = self._root / _JOURNAL_NAME
        self._lock_path = self._root / _LOCK_NAME
        self._anchor_path = self._root / _ANCHOR_NAME
        self._lock_fd = -1
        self._root_fd = -1
        self._namespace_lock: socket.socket | None = None
        self._install_secret = bytes(install_secret)
        self._max_records = max_records
        self._tombstone_ttl_seconds = ttl
        self._generation = 0
        self._journal_digest = ""
        self._journal_identity: tuple[int, int, int, int, int] | None = None
        self._records: dict[str, _Record] = {}
        self._handles: dict[str, LifecycleHandle] = {}
        self._store_nonce = object()
        self._lock = threading.RLock()
        self._enabled = True
        self._failure_code = ""

        initialization_succeeded = False
        try:
            if _path_has_link_or_reparse(requested_root):
                raise ValueError("lifecycle_root_symlink_rejected")
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_metadata = self._root.lstat()
            if (
                _is_link_or_reparse(self._root)
                or not stat.S_ISDIR(root_metadata.st_mode)
            ):
                raise ValueError("lifecycle_root_invalid")
            if (
                os.name != "nt"
                and stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                raise ValueError("lifecycle_root_permissions_invalid")
            self._root = self._root.resolve(strict=True)
            self._journal_path = self._root / _JOURNAL_NAME
            self._lock_path = self._root / _LOCK_NAME
            self._anchor_path = self._root / _ANCHOR_NAME
            self._acquire_namespace_lock()
            self._acquire_process_lock()
            anchor = self._read_anchor_locked()
            try:
                self._journal_path.lstat()
                journal_exists = True
            except FileNotFoundError:
                journal_exists = False
            if journal_exists:
                self._load_existing()
                if anchor is None:
                    raise ValueError("journal_anchor_invalid")
                if anchor != (self._generation, self._journal_digest):
                    raise ValueError("journal_anchor_invalid")
                self._recover_interrupted(recovered_at)
            else:
                if anchor is not None:
                    raise ValueError("journal_file_changed")
                self._persist_candidate({})
            initialization_succeeded = True
        except LifecycleStoreDisabled:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._disable(_exception_failure_code(exc, "journal_load_failed"))
        except BaseException as exc:
            self._enabled = False
            self._failure_code = "lifecycle_initialization_interrupted"
            self._records = {}
            self._handles = {}
            _raise_safe_interruption(
                "lifecycle_initialization_interrupted",
                exc,
            )
        finally:
            if not initialization_succeeded:
                self._release_process_lock()

    @property
    def enabled(self) -> bool:
        with self._lock:
            self._refresh_health()
            return self._enabled

    @property
    def failure_code(self) -> str:
        with self._lock:
            self._refresh_health()
            return self._failure_code

    def _refresh_health(self) -> None:
        if self._enabled:
            try:
                self._validate_process_lock()
            except LifecycleStoreDisabled:
                pass

    def _disable(self, code: Any) -> str:
        safe_code = _closed_failure_code(code, "lifecycle_fail_closed")
        try:
            self._enabled = False
            self._failure_code = safe_code
            self._records = {}
            self._handles = {}
        finally:
            # A replaced root must remain leased until explicit close/GC so a
            # new inode at the same configured path cannot become a second
            # writer.  Other failures release the logical lease after the
            # instance has irreversibly failed closed, permitting clean reopen.
            self._release_process_lock(
                release_namespace=(
                    safe_code != "lifecycle_root_identity_invalid"
                )
            )
        return safe_code

    def _acquire_namespace_lock(self) -> None:
        """Acquire a non-filesystem logical-path lease on Linux.

        ``flock`` protects an inode, not a pathname.  An abstract AF_UNIX name
        keeps the configured logical root exclusive even if its directory is
        renamed and a new inode is created at the old path.
        """

        if os.name == "nt" or not sys.platform.startswith("linux"):
            return
        normalized = os.path.normcase(os.path.abspath(os.fspath(self._root)))
        digest = hashlib.sha256(
            normalized.encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        lease = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            lease.bind("\x00shio-owner-lifecycle-" + digest)
        except OSError:
            lease.close()
            code = self._disable("lifecycle_store_locked")
            raise LifecycleStoreDisabled(code) from None
        self._namespace_lock = lease

    def _acquire_process_lock(self) -> None:
        if os.name != "nt":
            root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            root_flags |= getattr(os, "O_CLOEXEC", 0)
            root_flags |= getattr(os, "O_NOFOLLOW", 0)
            root_descriptor = -1
            try:
                root_descriptor = os.open(self._root, root_flags)
                root_metadata = os.fstat(root_descriptor)
                root_path_metadata = self._root.lstat()
                if (
                    not stat.S_ISDIR(root_metadata.st_mode)
                    or root_metadata.st_nlink < 1
                    or root_metadata.st_dev != root_path_metadata.st_dev
                    or root_metadata.st_ino != root_path_metadata.st_ino
                    or root_metadata.st_nlink != root_path_metadata.st_nlink
                    or stat.S_IMODE(root_metadata.st_mode) != 0o700
                    or stat.S_IMODE(root_path_metadata.st_mode) != 0o700
                    or _is_link_or_reparse(self._root)
                ):
                    raise ValueError("lifecycle_root_identity_invalid")
                import fcntl

                fcntl.flock(root_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked_metadata = os.fstat(root_descriptor)
                locked_path_metadata = self._root.lstat()
                if (
                    not stat.S_ISDIR(locked_metadata.st_mode)
                    or locked_metadata.st_nlink < 1
                    or locked_metadata.st_dev != locked_path_metadata.st_dev
                    or locked_metadata.st_ino != locked_path_metadata.st_ino
                    or locked_metadata.st_nlink != locked_path_metadata.st_nlink
                    or stat.S_IMODE(locked_metadata.st_mode) != 0o700
                    or stat.S_IMODE(locked_path_metadata.st_mode) != 0o700
                    or _is_link_or_reparse(self._root)
                ):
                    raise ValueError("lifecycle_root_identity_invalid")
            except (OSError, ValueError) as exc:
                if root_descriptor >= 0:
                    try:
                        os.close(root_descriptor)
                    except BaseException:
                        pass
                code = self._disable(
                    _exception_failure_code(exc, "lifecycle_store_locked")
                )
                raise LifecycleStoreDisabled(code) from None
            except BaseException:
                if root_descriptor >= 0:
                    try:
                        os.close(root_descriptor)
                    except BaseException:
                        pass
                raise
            self._root_fd = root_descriptor

        if _is_link_or_reparse(self._lock_path):
            self._disable("lifecycle_lock_symlink_rejected")
            raise LifecycleStoreDisabled("lifecycle_lock_symlink_rejected")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            path_metadata = self._lock_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != path_metadata.st_dev
                or metadata.st_ino != path_metadata.st_ino
                or metadata.st_nlink != path_metadata.st_nlink
                or _is_link_or_reparse(self._lock_path)
                or (os.name == "nt" and metadata.st_size not in {0, 1})
                or (os.name != "nt" and metadata.st_size != 0)
            ):
                raise ValueError("lifecycle_lock_not_regular")
            if os.name != "nt":
                if (
                    stat.S_IMODE(metadata.st_mode) != 0o600
                    or stat.S_IMODE(path_metadata.st_mode) != 0o600
                ):
                    raise ValueError("lifecycle_lock_permissions_invalid")
            if os.name == "nt":
                import msvcrt

                if metadata.st_size == 0:
                    os.write(descriptor, b"\x00")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked_metadata = os.fstat(descriptor)
            locked_path_metadata = self._lock_path.lstat()
            if (
                not stat.S_ISREG(locked_metadata.st_mode)
                or locked_metadata.st_nlink != 1
                or locked_metadata.st_dev != locked_path_metadata.st_dev
                or locked_metadata.st_ino != locked_path_metadata.st_ino
                or locked_metadata.st_nlink != locked_path_metadata.st_nlink
                or _is_link_or_reparse(self._lock_path)
                or (
                    os.name != "nt"
                    and (
                        stat.S_IMODE(locked_metadata.st_mode) != 0o600
                        or stat.S_IMODE(locked_path_metadata.st_mode) != 0o600
                    )
                )
            ):
                raise ValueError("lifecycle_lock_identity_changed")
        except (OSError, ValueError) as exc:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            code = self._disable(
                _exception_failure_code(exc, "lifecycle_store_locked")
            )
            raise LifecycleStoreDisabled(code) from None
        except BaseException:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            raise
        self._lock_fd = descriptor

    def _release_process_lock(self, *, release_namespace: bool = True) -> None:
        descriptor = self._lock_fd
        if descriptor >= 0:
            self._lock_fd = -1
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except BaseException:
                pass
            finally:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
        root_descriptor = self._root_fd
        if root_descriptor >= 0:
            self._root_fd = -1
            try:
                import fcntl

                fcntl.flock(root_descriptor, fcntl.LOCK_UN)
            except BaseException:
                pass
            finally:
                try:
                    os.close(root_descriptor)
                except BaseException:
                    pass
        if release_namespace:
            namespace_lock = self._namespace_lock
            self._namespace_lock = None
            if namespace_lock is not None:
                try:
                    namespace_lock.close()
                except BaseException:
                    pass

    def _read_anchor_locked(self) -> tuple[int, str] | None:
        try:
            metadata = self._anchor_path.lstat()
        except FileNotFoundError:
            return None
        if (
            _is_link_or_reparse(self._anchor_path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not (0 < metadata.st_size <= _MAX_ANCHOR_BYTES)
            or (
                os.name != "nt"
                and stat.S_IMODE(metadata.st_mode) != 0o600
            )
        ):
            raise ValueError("journal_anchor_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._anchor_path, flags)
        try:
            opened = os.fstat(descriptor)
            raw = os.read(descriptor, _MAX_ANCHOR_BYTES + 1)
            final_metadata = os.fstat(descriptor)
            path_metadata = self._anchor_path.lstat()
            if (
                self._journal_stat_identity(opened)
                != self._journal_stat_identity(final_metadata)
                or final_metadata.st_dev != path_metadata.st_dev
                or final_metadata.st_ino != path_metadata.st_ino
                or final_metadata.st_nlink != path_metadata.st_nlink
                or len(raw) != opened.st_size
                or _is_link_or_reparse(self._anchor_path)
            ):
                raise ValueError("journal_anchor_invalid")
        finally:
            os.close(descriptor)
        return _parse_anchor(raw, self._install_secret)

    def _write_anchor_locked(
        self,
        generation: int,
        journal_digest: str,
    ) -> None:
        encoded = _anchor_bytes(
            self._install_secret,
            generation,
            journal_digest,
        )
        if len(encoded) > _MAX_ANCHOR_BYTES:
            raise OSError("journal_anchor_invalid")
        descriptor = -1
        temporary_path = ""
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".owner_action_lifecycle.anchor.",
                suffix=".tmp",
                dir=self._root,
            )
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._anchor_path)
            temporary_path = ""
            if os.name != "nt":
                os.chmod(self._anchor_path, 0o600)
                if self._root_fd < 0:
                    raise OSError("lifecycle_root_identity_invalid")
                os.fsync(self._root_fd)
            if self._read_anchor_locked() != (generation, journal_digest):
                raise OSError("journal_anchor_invalid")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _journal_stat_identity(
        metadata: os.stat_result,
    ) -> tuple[int, int, int, int, int]:
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_nlink),
            int(metadata.st_size),
            int(stat.S_IMODE(metadata.st_mode)),
        )

    def _read_journal_state(
        self,
    ) -> tuple[tuple[int, int, int, int, int], str]:
        metadata = self._journal_path.lstat()
        if (
            _is_link_or_reparse(self._journal_path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not (0 < metadata.st_size <= _MAX_JOURNAL_BYTES)
            or (
                os.name != "nt"
                and stat.S_IMODE(metadata.st_mode) != 0o600
            )
        ):
            raise ValueError("journal_file_changed")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._journal_path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
            ):
                raise ValueError("journal_file_changed")
            digest = hashlib.sha256()
            remaining = _MAX_JOURNAL_BYTES + 1
            total = 0
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                remaining -= len(chunk)
            final_metadata = os.fstat(descriptor)
            final_path_metadata = self._journal_path.lstat()
            if (
                total != opened.st_size
                or self._journal_stat_identity(final_metadata)
                != self._journal_stat_identity(opened)
                or final_metadata.st_dev != final_path_metadata.st_dev
                or final_metadata.st_ino != final_path_metadata.st_ino
                or final_metadata.st_nlink != final_path_metadata.st_nlink
                or _is_link_or_reparse(self._journal_path)
            ):
                raise ValueError("journal_file_changed")
            return self._journal_stat_identity(final_metadata), digest.hexdigest()
        finally:
            os.close(descriptor)

    def _validate_durable_state(self) -> None:
        if self._generation == 0:
            return
        anchor = self._read_anchor_locked()
        if anchor != (self._generation, self._journal_digest):
            raise ValueError("journal_anchor_invalid")
        identity, digest = self._read_journal_state()
        if (
            identity != self._journal_identity
            or digest != self._journal_digest
        ):
            raise ValueError("journal_file_changed")

    def _validate_process_lock(self) -> None:
        try:
            descriptor = self._lock_fd
            if descriptor < 0:
                raise ValueError("lifecycle_lock_identity_changed")
            metadata = os.fstat(descriptor)
            path_metadata = self._lock_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != path_metadata.st_dev
                or metadata.st_ino != path_metadata.st_ino
                or metadata.st_nlink != path_metadata.st_nlink
                or _is_link_or_reparse(self._lock_path)
                or (os.name == "nt" and metadata.st_size not in {0, 1})
                or (os.name != "nt" and metadata.st_size != 0)
                or (
                    os.name != "nt"
                    and (
                        stat.S_IMODE(metadata.st_mode) != 0o600
                        or stat.S_IMODE(path_metadata.st_mode) != 0o600
                    )
                )
            ):
                raise ValueError("lifecycle_lock_identity_changed")

            if os.name != "nt":
                root_descriptor = self._root_fd
                if root_descriptor < 0:
                    raise ValueError("lifecycle_root_identity_invalid")
                root_metadata = os.fstat(root_descriptor)
                root_path_metadata = self._root.lstat()
                if (
                    not stat.S_ISDIR(root_metadata.st_mode)
                    or root_metadata.st_nlink < 1
                    or root_metadata.st_dev != root_path_metadata.st_dev
                    or root_metadata.st_ino != root_path_metadata.st_ino
                    or root_metadata.st_nlink != root_path_metadata.st_nlink
                    or stat.S_IMODE(root_metadata.st_mode) != 0o700
                    or stat.S_IMODE(root_path_metadata.st_mode) != 0o700
                    or _is_link_or_reparse(self._root)
                ):
                    raise ValueError("lifecycle_root_identity_invalid")
            self._validate_durable_state()
        except (OSError, ValueError) as exc:
            code = self._disable(
                _exception_failure_code(exc, "lifecycle_lock_identity_changed")
            )
            raise LifecycleStoreDisabled(code) from None
        except BaseException as exc:
            code = self._disable("lifecycle_lock_validation_interrupted")
            _raise_safe_interruption(code, exc)

    def close(self) -> None:
        with self._lock:
            if (
                self._lock_fd < 0
                and self._root_fd < 0
                and self._namespace_lock is None
            ):
                return
            try:
                self._enabled = False
                if not self._failure_code:
                    self._failure_code = "lifecycle_store_closed"
                self._records = {}
                self._handles = {}
            finally:
                self._release_process_lock()

    def __enter__(self) -> "OwnerActionLifecycleStore":
        with self._lock:
            self._require_enabled()
            return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._release_process_lock()
        except BaseException:
            pass

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise LifecycleStoreDisabled(
                _closed_failure_code(
                    self._failure_code,
                    "lifecycle_fail_closed",
                )
            )
        self._validate_process_lock()

    def _load_existing(self) -> None:
        if _is_link_or_reparse(self._journal_path):
            self._disable("journal_symlink_rejected")
            raise LifecycleStoreDisabled("journal_symlink_rejected")
        try:
            metadata = self._journal_path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("journal_not_regular")
            if (
                os.name != "nt"
                and stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError("journal_permissions_invalid")
            if not (0 < metadata.st_size <= _MAX_JOURNAL_BYTES):
                raise ValueError("journal_size_invalid")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._journal_path, flags)
            try:
                opened_metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened_metadata.st_mode)
                    or opened_metadata.st_nlink != 1
                    or opened_metadata.st_size != metadata.st_size
                    or opened_metadata.st_dev != metadata.st_dev
                    or opened_metadata.st_ino != metadata.st_ino
                    or opened_metadata.st_size > _MAX_JOURNAL_BYTES
                ):
                    raise ValueError(
                        "journal_not_regular"
                        if opened_metadata.st_nlink != 1
                        else "journal_file_changed"
                    )
                if (
                    os.name != "nt"
                    and stat.S_IMODE(opened_metadata.st_mode) != 0o600
                ):
                    raise ValueError("journal_permissions_invalid")
                chunks: list[bytes] = []
                remaining = _MAX_JOURNAL_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                final_metadata = os.fstat(descriptor)
                final_path_metadata = self._journal_path.lstat()
                if (
                    len(raw) != opened_metadata.st_size
                    or final_metadata.st_size != opened_metadata.st_size
                    or not stat.S_ISREG(final_metadata.st_mode)
                    or final_metadata.st_nlink != 1
                    or final_metadata.st_dev != opened_metadata.st_dev
                    or final_metadata.st_ino != opened_metadata.st_ino
                    or final_metadata.st_dev != final_path_metadata.st_dev
                    or final_metadata.st_ino != final_path_metadata.st_ino
                    or final_metadata.st_nlink != final_path_metadata.st_nlink
                    or _is_link_or_reparse(self._journal_path)
                ):
                    raise ValueError("journal_file_changed")
                if (
                    os.name != "nt"
                    and (
                        stat.S_IMODE(final_metadata.st_mode) != 0o600
                        or stat.S_IMODE(final_path_metadata.st_mode) != 0o600
                    )
                ):
                    raise ValueError("journal_permissions_invalid")
            finally:
                os.close(descriptor)
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
            if raw != _canonical_json_bytes(payload):
                raise ValueError("journal_not_canonical")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self._disable("journal_corrupt")
            raise LifecycleStoreDisabled("journal_corrupt") from None
        except ValueError as exc:
            code = self._disable(
                _exception_failure_code(exc, "journal_corrupt")
            )
            raise LifecycleStoreDisabled(code) from None
        if type(payload) is not dict or frozenset(payload) != _STORE_KEYS:
            self._disable("journal_shape_invalid")
            raise LifecycleStoreDisabled("journal_shape_invalid")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != _SCHEMA_VERSION
        ):
            self._disable("unknown_schema")
            raise LifecycleStoreDisabled("unknown_schema")
        expected_verifier = _secret_verifier(self._install_secret)
        stored_verifier = payload["secret_verifier"]
        if (
            type(stored_verifier) is not str
            or _HEX_64.fullmatch(stored_verifier) is None
            or not hmac.compare_digest(stored_verifier, expected_verifier)
        ):
            self._disable("install_secret_mismatch")
            raise LifecycleStoreDisabled("install_secret_mismatch")
        generation = payload["generation"]
        if (
            type(generation) is not int
            or generation < 1
        ):
            self._disable("journal_generation_invalid")
            raise LifecycleStoreDisabled("journal_generation_invalid")
        values = payload["records"]
        if type(values) is not list or len(values) > self._max_records:
            self._disable("record_count_exceeds_bound")
            raise LifecycleStoreDisabled("record_count_exceeds_bound")
        records: dict[str, _Record] = {}
        try:
            for value in values:
                record = _parse_record(value)
                if record.fingerprint in records:
                    raise ValueError("record_fingerprint_duplicate")
                records[record.fingerprint] = record
        except ValueError as exc:
            code = self._disable(
                _exception_failure_code(exc, "journal_corrupt")
            )
            raise LifecycleStoreDisabled(code) from None
        self._generation = generation
        self._journal_identity = self._journal_stat_identity(final_metadata)
        self._journal_digest = hashlib.sha256(raw).hexdigest()
        self._records = records
        self._handles = {
            fingerprint: self._new_handle(fingerprint, record)
            for fingerprint, record in records.items()
        }

    def _recover_interrupted(self, recovered_at: float) -> None:
        candidate = dict(self._records)
        changed = False
        for fingerprint, record in tuple(candidate.items()):
            terminal_at = max(recovered_at, record.updated_at)
            if record.phase is LifecyclePhase.RESERVED:
                candidate[fingerprint] = replace(
                    record,
                    phase=LifecyclePhase.TERMINAL,
                    result_status=ActionReceiptStatus.STALE,
                    effect_state=ActionEffectState.NOT_STARTED,
                    attempted=False,
                    updated_at=terminal_at,
                    terminal_at=terminal_at,
                )
                changed = True
            elif record.phase is LifecyclePhase.IN_PROGRESS:
                if record.operation in _READ_OPERATIONS:
                    status = ActionReceiptStatus.FAILED
                    effect = ActionEffectState.NO_SIDE_EFFECT
                else:
                    status = ActionReceiptStatus.EFFECT_UNKNOWN
                    effect = ActionEffectState.UNKNOWN
                candidate[fingerprint] = replace(
                    record,
                    phase=LifecyclePhase.TERMINAL,
                    result_status=status,
                    effect_state=effect,
                    attempted=True,
                    updated_at=terminal_at,
                    terminal_at=terminal_at,
                )
                changed = True
        if changed:
            self._persist_candidate(candidate)

    def _payload_for(self, records: dict[str, _Record]) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "secret_verifier": _secret_verifier(self._install_secret),
            "generation": self._generation + 1,
            "records": [
                _record_payload(records[key]) for key in sorted(records)
            ],
        }

    def _validate_journal_path(self, *, required: bool) -> None:
        try:
            metadata = self._journal_path.lstat()
        except FileNotFoundError:
            if required:
                raise OSError("journal_file_changed") from None
            return
        if not required:
            raise OSError("journal_file_changed")
        if (
            _is_link_or_reparse(self._journal_path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (
                os.name != "nt"
                and stat.S_IMODE(metadata.st_mode) != 0o600
            )
        ):
            raise OSError("journal_file_changed")

    def _atomic_write(
        self,
        payload: dict[str, Any],
    ) -> tuple[tuple[int, int, int, int, int], str]:
        encoded = _canonical_json_bytes(payload)
        if len(encoded) > _MAX_JOURNAL_BYTES:
            raise OSError("journal_size_invalid")
        descriptor = -1
        temporary_path = ""
        temporary_metadata: os.stat_result | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".owner_action_lifecycle.",
                suffix=".tmp",
                dir=self._root,
            )
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
                if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                    raise OSError("journal_temp_permissions_invalid")
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                try:
                    os.chmod(temporary_path, 0o600)
                except OSError:
                    pass
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
                temporary_metadata = os.fstat(stream.fileno())
            os.replace(temporary_path, self._journal_path)
            temporary_path = ""
            journal_metadata = self._journal_path.lstat()
            if (
                temporary_metadata is None
                or _is_link_or_reparse(self._journal_path)
                or not stat.S_ISREG(journal_metadata.st_mode)
                or journal_metadata.st_nlink != 1
                or journal_metadata.st_dev != temporary_metadata.st_dev
                or journal_metadata.st_ino != temporary_metadata.st_ino
                or journal_metadata.st_size != temporary_metadata.st_size
                or (
                    os.name != "nt"
                    and stat.S_IMODE(journal_metadata.st_mode) != 0o600
                )
            ):
                raise OSError("journal_replace_identity_invalid")
            if os.name != "nt":
                if self._root_fd < 0:
                    raise OSError("lifecycle_root_identity_invalid")
                os.fsync(self._root_fd)
            return (
                self._journal_stat_identity(journal_metadata),
                hashlib.sha256(encoded).hexdigest(),
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def _persist_candidate(self, candidate: dict[str, _Record]) -> None:
        if len(candidate) > self._max_records:
            raise LifecycleTransitionRejected("lifecycle_record_limit_reached")
        payload = self._payload_for(candidate)
        self._validate_process_lock()
        try:
            self._validate_journal_path(required=self._generation > 0)
            journal_identity, journal_digest = self._atomic_write(payload)
            generation = int(payload["generation"])
            self._write_anchor_locked(generation, journal_digest)
            self._generation = generation
            self._journal_identity = journal_identity
            self._journal_digest = journal_digest
            self._validate_process_lock()
            self._validate_journal_path(required=True)
            self._records = candidate
            self._handles = {
                key: self._handles[key]
                for key in candidate
                if key in self._handles
            }
        except LifecycleStoreDisabled:
            raise
        except OSError:
            self._disable("atomic_write_failed")
            raise LifecycleStoreDisabled("atomic_write_failed") from None
        except BaseException as exc:
            # ``os.replace`` may have committed before an injected process
            # interruption surfaced.  Reconcile only when the exact canonical
            # candidate bytes are already durable; this preserves no-retry on
            # reopen without treating an arbitrary disk file as authoritative.
            try:
                expected = _canonical_json_bytes(payload)
                expected_digest = hashlib.sha256(expected).hexdigest()
                _identity, actual_digest = self._read_journal_state()
                if actual_digest == expected_digest:
                    self._write_anchor_locked(
                        int(payload["generation"]),
                        expected_digest,
                    )
            except BaseException:
                pass
            code = self._disable("atomic_write_interrupted")
            _raise_safe_interruption(code, exc)

    def _new_handle(self, fingerprint: str, record: _Record) -> LifecycleHandle:
        return LifecycleHandle(
            operation=record.operation,
            _request_fingerprint=fingerprint,
            _store_nonce=self._store_nonce,
        )

    def _handle_for(self, fingerprint: str, record: _Record) -> LifecycleHandle:
        handle = self._handles.get(fingerprint)
        try:
            valid = (
                type(handle) is LifecycleHandle
                and type(handle.operation) is OwnerActionOperation
                and handle.operation is record.operation
                and type(handle._request_fingerprint) is str
                and handle._request_fingerprint == fingerprint
                and handle._store_nonce is self._store_nonce
            )
        except AttributeError:
            valid = False
        if not valid:
            handle = self._new_handle(fingerprint, record)
            self._handles[fingerprint] = handle
        return handle

    def _repair_exposed_handle(self, handle: LifecycleHandle) -> None:
        for fingerprint, candidate in tuple(self._handles.items()):
            if candidate is handle:
                record = self._records.get(fingerprint)
                if record is not None:
                    self._handles[fingerprint] = self._new_handle(
                        fingerprint, record
                    )
                return

    def _canonical(
        self, handle: LifecycleHandle
    ) -> tuple[str, _Record]:
        if type(handle) is not LifecycleHandle:
            raise LifecycleTransitionRejected("lifecycle_handle_required")
        try:
            fingerprint = handle._request_fingerprint
            operation = handle.operation
            store_nonce = handle._store_nonce
        except AttributeError:
            self._repair_exposed_handle(handle)
            raise LifecycleTransitionRejected("lifecycle_handle_not_canonical") from None
        if (
            type(fingerprint) is not str
            or _HEX_64.fullmatch(fingerprint) is None
            or type(operation) is not OwnerActionOperation
            or store_nonce is not self._store_nonce
        ):
            self._repair_exposed_handle(handle)
            raise LifecycleTransitionRejected("lifecycle_handle_not_canonical")
        canonical = self._handles.get(fingerprint)
        record = self._records.get(fingerprint)
        if (
            canonical is not handle
            or store_nonce is not self._store_nonce
            or record is None
            or operation is not record.operation
        ):
            self._repair_exposed_handle(handle)
            raise LifecycleTransitionRejected("lifecycle_handle_not_canonical")
        return fingerprint, record

    def _prunable(self, record: _Record, now: float) -> bool:
        from .owner_action_durable_finalize import _lifecycle_record_is_held

        return (
            record.phase is LifecyclePhase.DELIVERY_ACK
            and not _lifecycle_record_is_held(self, record.fingerprint)
            and now >= record.delivery_ack_at + self._tombstone_ttl_seconds
            and now >= record.created_at + self._tombstone_ttl_seconds
        )

    def inspect_handle(self, handle: LifecycleHandle) -> LifecycleSnapshot:
        """Inspect one exact store-local handle without granting execution."""

        with self._lock:
            self._require_enabled()
            _fingerprint, record = self._canonical(handle)
            return _snapshot(record)

    def reserve(
        self,
        operation: OwnerActionOperation,
        *,
        request_digest: str,
        now: float,
    ) -> LifecycleReservation:
        with self._lock:
            self._require_enabled()
            if type(operation) is not OwnerActionOperation:
                raise LifecycleTransitionRejected("operation_invalid")
            digest = _require_request_digest(request_digest)
            reserved_at = _require_time(now, "now")
            fingerprint = _request_fingerprint(
                self._install_secret,
                operation,
                digest,
            )
            existing = self._records.get(fingerprint)
            if existing is not None:
                disposition = (
                    LifecycleReserveDisposition.EXISTING_ACTIVE
                    if existing.phase
                    in {LifecyclePhase.RESERVED, LifecyclePhase.IN_PROGRESS}
                    else LifecycleReserveDisposition.EXISTING_TERMINAL
                )
                return LifecycleReservation(
                    handle=self._handle_for(fingerprint, existing),
                    disposition=disposition,
                    snapshot=_snapshot(existing),
                )

            candidate = {
                key: record
                for key, record in self._records.items()
                if not self._prunable(record, reserved_at)
            }
            if len(candidate) >= self._max_records:
                raise LifecycleTransitionRejected("lifecycle_record_limit_reached")
            record = _Record(
                fingerprint=fingerprint,
                operation=operation,
                phase=LifecyclePhase.RESERVED,
                result_status=None,
                effect_state=ActionEffectState.NOT_STARTED,
                attempted=False,
                created_at=reserved_at,
                updated_at=reserved_at,
                terminal_at=0.0,
                delivery_ack_at=0.0,
            )
            candidate[fingerprint] = record
            self._persist_candidate(candidate)
            self._handles = {
                key: self._handles[key]
                for key in candidate
                if key in self._handles
            }
            handle = self._handle_for(fingerprint, record)
            return LifecycleReservation(
                handle=handle,
                disposition=LifecycleReserveDisposition.RESERVED,
                snapshot=_snapshot(record),
            )

    def mark_in_progress(
        self,
        handle: LifecycleHandle,
        *,
        now: float,
    ) -> LifecycleStartClaim:
        with self._lock:
            self._require_enabled()
            started_at = _require_time(now, "now")
            fingerprint, record = self._canonical(handle)
            if record.phase in {LifecyclePhase.TERMINAL, LifecyclePhase.DELIVERY_ACK}:
                return LifecycleStartClaim(claimed=False, snapshot=_snapshot(record))
            if record.phase is LifecyclePhase.IN_PROGRESS:
                return LifecycleStartClaim(claimed=False, snapshot=_snapshot(record))
            if started_at < record.updated_at:
                raise LifecycleTransitionRejected("lifecycle_time_regressed")
            updated = replace(
                record,
                phase=LifecyclePhase.IN_PROGRESS,
                effect_state=(
                    ActionEffectState.NO_SIDE_EFFECT
                    if record.operation in _READ_OPERATIONS
                    else ActionEffectState.UNKNOWN
                ),
                attempted=True,
                updated_at=started_at,
            )
            candidate = dict(self._records)
            candidate[fingerprint] = updated
            self._persist_candidate(candidate)
            return LifecycleStartClaim(claimed=True, snapshot=_snapshot(updated))

    def mark_terminal(
        self,
        handle: LifecycleHandle,
        *,
        status: ActionReceiptStatus,
        effect_state: ActionEffectState,
        attempted: bool,
        now: float,
    ) -> LifecycleSnapshot:
        with self._lock:
            self._require_enabled()
            if type(status) is not ActionReceiptStatus:
                raise LifecycleTransitionRejected("terminal_status_invalid")
            if type(effect_state) is not ActionEffectState:
                raise LifecycleTransitionRejected("terminal_effect_invalid")
            if type(attempted) is not bool:
                raise LifecycleTransitionRejected("terminal_attempted_invalid")
            terminal_at = _require_time(now, "now")
            fingerprint, record = self._canonical(handle)
            _validate_terminal_semantics(
                record.operation,
                status,
                effect_state,
                attempted,
            )
            if record.phase in {LifecyclePhase.TERMINAL, LifecyclePhase.DELIVERY_ACK}:
                if (
                    record.result_status is status
                    and record.effect_state is effect_state
                    and record.attempted is attempted
                ):
                    return _snapshot(record)
                raise LifecycleTransitionRejected("terminal_fact_conflict")
            if attempted and record.phase is not LifecyclePhase.IN_PROGRESS:
                raise LifecycleTransitionRejected("terminal_attempt_without_start")
            if not attempted and record.phase is not LifecyclePhase.RESERVED:
                raise LifecycleTransitionRejected("terminal_not_started_conflict")
            if terminal_at < record.updated_at:
                raise LifecycleTransitionRejected("lifecycle_time_regressed")
            updated = replace(
                record,
                phase=LifecyclePhase.TERMINAL,
                result_status=status,
                effect_state=effect_state,
                attempted=attempted,
                updated_at=terminal_at,
                terminal_at=terminal_at,
            )
            candidate = dict(self._records)
            candidate[fingerprint] = updated
            self._persist_candidate(candidate)
            return _snapshot(updated)

    def acknowledge_delivery(
        self,
        handle: LifecycleHandle,
        *,
        now: float,
    ) -> LifecycleSnapshot:
        with self._lock:
            self._require_enabled()
            acknowledged_at = _require_time(now, "now")
            fingerprint, record = self._canonical(handle)
            if record.phase is LifecyclePhase.DELIVERY_ACK:
                return _snapshot(record)
            if record.phase is not LifecyclePhase.TERMINAL:
                raise LifecycleTransitionRejected("terminal_required_before_delivery_ack")
            if acknowledged_at < record.updated_at:
                raise LifecycleTransitionRejected("lifecycle_time_regressed")
            updated = replace(
                record,
                phase=LifecyclePhase.DELIVERY_ACK,
                updated_at=acknowledged_at,
                delivery_ack_at=acknowledged_at,
            )
            candidate = dict(self._records)
            candidate[fingerprint] = updated
            self._persist_candidate(candidate)
            return _snapshot(updated)

    def snapshot_for(
        self,
        operation: OwnerActionOperation,
        *,
        request_digest: str,
    ) -> LifecycleSnapshot | None:
        with self._lock:
            self._require_enabled()
            if type(operation) is not OwnerActionOperation:
                raise LifecycleTransitionRejected("operation_invalid")
            digest = _require_request_digest(request_digest)
            fingerprint = _request_fingerprint(
                self._install_secret,
                operation,
                digest,
            )
            record = self._records.get(fingerprint)
            return None if record is None else _snapshot(record)

    def prune(self, *, now: float) -> int:
        with self._lock:
            self._require_enabled()
            pruned_at = _require_time(now, "now")
            candidate = {
                key: record
                for key, record in self._records.items()
                if not self._prunable(record, pruned_at)
            }
            removed = len(self._records) - len(candidate)
            if removed:
                self._persist_candidate(candidate)
                self._handles = {
                    key: self._handles[key]
                    for key in candidate
                    if key in self._handles
                }
            return removed

    def trace_metadata(self) -> dict[str, str | int | bool]:
        with self._lock:
            self._refresh_health()
            phases = tuple(record.phase for record in self._records.values())
            return {
                "schema_version": _SCHEMA_VERSION,
                "enabled": self._enabled,
                "failure_code": self._failure_code or "none",
                "record_count": len(self._records),
                "reserved_count": sum(
                    phase is LifecyclePhase.RESERVED for phase in phases
                ),
                "in_progress_count": sum(
                    phase is LifecyclePhase.IN_PROGRESS for phase in phases
                ),
                "terminal_count": sum(
                    phase is LifecyclePhase.TERMINAL for phase in phases
                ),
                "delivery_ack_count": sum(
                    phase is LifecyclePhase.DELIVERY_ACK for phase in phases
                ),
                "max_records": self._max_records,
                "request_material_visible": False,
                "install_secret_visible": False,
                "execution_authority": False,
                "action_outcome_authority": False,
            }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "OwnerActionLifecycleStore("
            f"enabled={metadata['enabled']!r}, "
            f"record_count={metadata['record_count']}, "
            f"failure_code={metadata['failure_code']!r}, "
            "request_material_visible=False, install_secret_visible=False, "
            "execution_authority=False, action_outcome_authority=False)"
        )


__all__ = [
    "LifecycleHandle",
    "LifecyclePhase",
    "LifecycleReservation",
    "LifecycleReserveDisposition",
    "LifecycleSnapshot",
    "LifecycleStartClaim",
    "LifecycleStoreDisabled",
    "LifecycleTransitionRejected",
    "OwnerActionLifecycleStore",
]
