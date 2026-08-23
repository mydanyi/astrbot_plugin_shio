from __future__ import annotations

import threading
import time
import math
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from .identity import TurnEnvelope


GENERATION_EPOCH_EXTRA = "_shio_generation_epoch_v2"


class EpochBindingError(ValueError):
    pass


_EPOCH_SNAPSHOT_SEAL = object()


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class GenerationEpochSnapshot:
    scope_key: str
    session_id: str
    epoch: int
    started_at: float
    _registry_ref: weakref.ReferenceType[GenerationEpochRegistry]
    _seal: object

    def trace_metadata(self) -> dict[str, int | bool]:
        registry_ref = getattr(self, "_registry_ref", None)
        registry = registry_ref() if type(registry_ref) is weakref.ReferenceType else None
        try:
            if type(registry) is not GenerationEpochRegistry:
                raise EpochBindingError("generation epoch snapshot not canonical")
            record = registry.inspect(self)
        except (EpochBindingError, AttributeError, TypeError):
            return {
                "generation_epoch": 0,
                "has_scope_key": False,
                "has_session_id": False,
                "generation_epoch_canonical": False,
            }
        return {
            "generation_epoch": record.epoch,
            "has_scope_key": True,
            "has_session_id": True,
            "generation_epoch_canonical": True,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "GenerationEpochSnapshot("
            f"epoch={metadata['generation_epoch']!r}, "
            f"canonical={metadata['generation_epoch_canonical']!r})"
        )


@dataclass(frozen=True, slots=True)
class EpochValidation:
    is_current: bool
    reason_code: str
    current_epoch: int


@dataclass(frozen=True, slots=True)
class _EpochSnapshotRecord:
    scope_key: str
    session_id: str
    epoch: int
    started_at: float
    advances_scope: bool


class GenerationEpochRegistry:
    def __init__(
        self,
        *,
        max_scopes: int = 2048,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        if type(max_scopes) is not int or max_scopes < 1:
            raise EpochBindingError("generation epoch scope limit invalid")
        if now_fn is not None and not callable(now_fn):
            raise EpochBindingError("generation epoch clock invalid")
        self.max_scopes = max(64, max_scopes)
        self.now_fn = now_fn or time.time
        self._epochs: OrderedDict[str, int] = OrderedDict()
        self._snapshots: weakref.WeakKeyDictionary[
            GenerationEpochSnapshot,
            _EpochSnapshotRecord,
        ] = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

    @staticmethod
    def _binding(envelope: TurnEnvelope) -> tuple[str, str]:
        if type(envelope) is not TurnEnvelope:
            raise EpochBindingError("generation epoch requires exact turn envelope")
        scope_key = envelope.scope_key.strip()
        session_id = envelope.session_id.strip()
        if not scope_key or not session_id:
            raise EpochBindingError("generation epoch requires typed scope and session")
        return scope_key, session_id

    def current(self, scope_key: str) -> int:
        if type(scope_key) is not str:
            raise EpochBindingError("generation epoch requires typed scope and session")
        scope = scope_key.strip()
        if not scope:
            raise EpochBindingError("generation epoch requires typed scope and session")
        with self._lock:
            return int(self._epochs.get(scope, 0))

    def next_epoch(self, envelope: TurnEnvelope) -> int:
        """Return the next epoch without mutating the registry."""

        scope_key, _ = self._binding(envelope)
        with self._lock:
            return int(self._epochs.get(scope_key, 0)) + 1

    def advance(
        self,
        envelope: TurnEnvelope,
        *,
        expected_epoch: int | None = None,
    ) -> GenerationEpochSnapshot:
        scope_key, session_id = self._binding(envelope)
        if expected_epoch is not None and (
            type(expected_epoch) is not int or expected_epoch < 1
        ):
            raise EpochBindingError("generation epoch candidate invalid")
        now = self.now_fn()
        if type(now) not in {int, float} or type(now) is bool:
            raise EpochBindingError("generation epoch clock invalid")
        started_at = float(now)
        if not math.isfinite(started_at) or started_at < 0:
            raise EpochBindingError("generation epoch clock invalid")
        with self._lock:
            epoch = self._epochs.pop(scope_key, 0) + 1
            if expected_epoch is not None and epoch != expected_epoch:
                # Restore the original LRU entry because a failed admission is
                # not allowed to advance generation state.
                if epoch > 1:
                    self._epochs[scope_key] = epoch - 1
                raise EpochBindingError("generation epoch candidate stale")
            self._epochs[scope_key] = epoch
            while len(self._epochs) > self.max_scopes:
                self._epochs.popitem(last=False)
            snapshot = object.__new__(GenerationEpochSnapshot)
            registry_ref = weakref.ref(self)
            for name, value in (
                ("scope_key", scope_key),
                ("session_id", session_id),
                ("epoch", epoch),
                ("started_at", started_at),
                ("_registry_ref", registry_ref),
                ("_seal", _EPOCH_SNAPSHOT_SEAL),
            ):
                object.__setattr__(snapshot, name, value)
            self._snapshots[snapshot] = _EpochSnapshotRecord(
                scope_key=scope_key,
                session_id=session_id,
                epoch=epoch,
                started_at=started_at,
                advances_scope=True,
            )
            return snapshot

    def issue_passive(
        self,
        envelope: TurnEnvelope,
        *,
        expected_epoch: int,
    ) -> GenerationEpochSnapshot:
        """Bind a zero-execution turn without superseding active generation."""

        scope_key, session_id = self._binding(envelope)
        if type(expected_epoch) is not int or expected_epoch < 1:
            raise EpochBindingError("generation epoch candidate invalid")
        now = self.now_fn()
        if type(now) not in {int, float} or type(now) is bool:
            raise EpochBindingError("generation epoch clock invalid")
        started_at = float(now)
        if not math.isfinite(started_at) or started_at < 0:
            raise EpochBindingError("generation epoch clock invalid")
        with self._lock:
            if expected_epoch != int(self._epochs.get(scope_key, 0)) + 1:
                raise EpochBindingError("generation epoch candidate stale")
            snapshot = object.__new__(GenerationEpochSnapshot)
            registry_ref = weakref.ref(self)
            for name, value in (
                ("scope_key", scope_key),
                ("session_id", session_id),
                ("epoch", expected_epoch),
                ("started_at", started_at),
                ("_registry_ref", registry_ref),
                ("_seal", _EPOCH_SNAPSHOT_SEAL),
            ):
                object.__setattr__(snapshot, name, value)
            self._snapshots[snapshot] = _EpochSnapshotRecord(
                scope_key=scope_key,
                session_id=session_id,
                epoch=expected_epoch,
                started_at=started_at,
                advances_scope=False,
            )
            return snapshot

    def inspect(self, snapshot: GenerationEpochSnapshot) -> _EpochSnapshotRecord:
        if type(snapshot) is not GenerationEpochSnapshot:
            raise EpochBindingError("generation epoch snapshot not canonical")
        with self._lock:
            record = self._snapshots.get(snapshot)
            if record is None:
                raise EpochBindingError("generation epoch snapshot not canonical")
            try:
                scope_key = snapshot.scope_key
                session_id = snapshot.session_id
                epoch = snapshot.epoch
                started_at = snapshot.started_at
                registry_ref = snapshot._registry_ref
                seal = snapshot._seal
            except AttributeError as exc:
                raise EpochBindingError("generation epoch snapshot corrupt") from exc
            if (
                type(scope_key) is not str
                or type(session_id) is not str
                or type(epoch) is not int
                or epoch < 1
                or type(started_at) is not float
                or not math.isfinite(started_at)
                or started_at < 0
                or type(registry_ref) is not weakref.ReferenceType
                or registry_ref() is not self
                or seal is not _EPOCH_SNAPSHOT_SEAL
                or scope_key != record.scope_key
                or session_id != record.session_id
                or epoch != record.epoch
                or started_at != record.started_at
            ):
                raise EpochBindingError("generation epoch snapshot corrupt")
            return record

    def validate(self, snapshot: GenerationEpochSnapshot | None) -> EpochValidation:
        if snapshot is None:
            return EpochValidation(False, "missing_generation_epoch", 0)
        try:
            record = self.inspect(snapshot)
        except EpochBindingError as exc:
            reason = str(exc)
            return EpochValidation(
                False,
                (
                    "generation_epoch_snapshot_corrupt"
                    if reason.endswith("corrupt")
                    else "generation_epoch_snapshot_not_canonical"
                ),
                0,
            )
        with self._lock:
            current = int(self._epochs.get(record.scope_key, 0))
        if not record.advances_scope:
            return EpochValidation(False, "passive_generation_epoch", current)
        if current <= 0:
            return EpochValidation(False, "generation_scope_evicted", 0)
        if current != record.epoch:
            return EpochValidation(False, "stale_generation_epoch", current)
        return EpochValidation(True, "current_generation_epoch", current)


def ensure_event_generation_epoch(
    event: Any,
    registry: GenerationEpochRegistry,
    envelope: TurnEnvelope,
) -> GenerationEpochSnapshot:
    get_extra = getattr(event, "get_extra", None)
    if callable(get_extra):
        existing = get_extra(GENERATION_EPOCH_EXTRA, None)
        if type(existing) is GenerationEpochSnapshot:
            registry.inspect(existing)
            return existing
    snapshot = registry.advance(envelope)
    set_extra = getattr(event, "set_extra", None)
    if callable(set_extra):
        set_extra(GENERATION_EPOCH_EXTRA, snapshot)
    return snapshot


def event_epoch_validation(
    event: Any,
    registry: GenerationEpochRegistry,
) -> EpochValidation:
    get_extra = getattr(event, "get_extra", None)
    snapshot = (
        get_extra(GENERATION_EPOCH_EXTRA, None)
        if callable(get_extra)
        else None
    )
    return registry.validate(
        snapshot if type(snapshot) is GenerationEpochSnapshot else None
    )


def event_generation_snapshot(event: Any) -> GenerationEpochSnapshot | None:
    get_extra = getattr(event, "get_extra", None)
    snapshot = (
        get_extra(GENERATION_EPOCH_EXTRA, None)
        if callable(get_extra)
        else None
    )
    return snapshot if type(snapshot) is GenerationEpochSnapshot else None
