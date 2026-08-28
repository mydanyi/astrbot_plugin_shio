from __future__ import annotations

import asyncio
import inspect
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, TypeVar

from .generation_cancellation import SupersededGeneration
from .generation_epoch import (
    EpochBindingError,
    GenerationEpochRegistry,
    GenerationEpochSnapshot,
)
from .identity import PrincipalContext


T = TypeVar("T")


class ScopeConcurrencyError(RuntimeError):
    pass


class ScopeWorkKind(str, Enum):
    DIRECT = "direct"
    REACT = "react"
    PROACTIVE = "proactive"
    ACTION = "action"


@dataclass(frozen=True, slots=True)
class _PrincipalSnapshot:
    sender_key: str
    sender_id: str
    is_owner: bool
    relationship_role: str
    verification_source: str


@dataclass(slots=True)
class _ScopeLane:
    gate: asyncio.Lock
    waiting: int = 0
    active: bool = False
    active_kind: ScopeWorkKind | None = None
    active_principal: _PrincipalSnapshot | None = None


class TurnScopeCoordinator:
    """Serialize Shio-owned async work per scope and bound cross-scope work.

    The coordinator never stores a ``PrincipalContext`` object.  It validates
    and snapshots primitive identity fields before queueing, so one scope can
    never observe a mutable principal object owned by another scope.  Work is
    created only after both the scope lane and global slot are acquired.
    """

    def __init__(
        self,
        epoch_registry: GenerationEpochRegistry,
        *,
        max_parallel_scopes: int = 4,
        max_waiters_per_scope: int = 16,
        max_waiters_total: int = 128,
        max_scope_lanes: int = 2048,
    ) -> None:
        if type(epoch_registry) is not GenerationEpochRegistry:
            raise ScopeConcurrencyError("scope_epoch_registry_required")
        for value, reason, minimum, maximum in (
            (max_parallel_scopes, "parallel_scope_limit_invalid", 1, 64),
            (max_waiters_per_scope, "scope_waiter_limit_invalid", 1, 256),
            (max_waiters_total, "total_waiter_limit_invalid", 1, 4096),
            (max_scope_lanes, "scope_lane_limit_invalid", 1, 8192),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ScopeConcurrencyError(reason)
        if max_waiters_total < max_waiters_per_scope:
            raise ScopeConcurrencyError("total_waiter_limit_invalid")
        self._epoch_registry = epoch_registry
        self._max_parallel_scopes = max_parallel_scopes
        self._max_waiters_per_scope = max_waiters_per_scope
        self._max_waiters_total = max_waiters_total
        self._max_scope_lanes = max_scope_lanes
        self._lanes: OrderedDict[str, _ScopeLane] = OrderedDict()
        self._state_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._global_gate: asyncio.Semaphore | None = None
        self._waiting_total = 0
        self._active_total = 0
        self._completed_total = 0
        self._cancelled_total = 0
        self._rejected_total = 0
        self._closed = False
        self._tasks: set[asyncio.Task[object]] = set()

    @staticmethod
    def _exact_text(value: object, reason: str, *, maximum: int = 1024) -> str:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > maximum
            or any(ord(character) < 32 for character in value)
        ):
            raise ScopeConcurrencyError(reason)
        return value

    def _ensure_loop(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._closed:
                raise ScopeConcurrencyError("scope_coordinator_closed")
            if self._loop is None:
                self._loop = loop
                self._global_gate = asyncio.Semaphore(self._max_parallel_scopes)
            elif self._loop is not loop:
                raise ScopeConcurrencyError("scope_coordinator_loop_mismatch")
            gate = self._global_gate
        if type(gate) is not asyncio.Semaphore:
            raise ScopeConcurrencyError("scope_coordinator_not_initialized")
        return gate

    def _event_scope(
        self,
        snapshot: GenerationEpochSnapshot,
        principal: PrincipalContext,
    ) -> tuple[str, _PrincipalSnapshot]:
        if type(snapshot) is not GenerationEpochSnapshot:
            raise ScopeConcurrencyError("generation_snapshot_not_canonical")
        try:
            record = self._epoch_registry.inspect(snapshot)
        except EpochBindingError as exc:
            raise ScopeConcurrencyError("generation_snapshot_not_canonical") from exc
        validation = self._epoch_registry.validate(snapshot)
        if not validation.is_current:
            raise SupersededGeneration(validation.reason_code)
        if type(principal) is not PrincipalContext:
            raise ScopeConcurrencyError("principal_not_exact")
        try:
            sender_key = principal.sender_key
            sender_id = principal.sender_id
            is_owner = principal.is_owner
            relationship_role = principal.relationship_role
            verification_source = principal.verification_source
        except AttributeError as exc:
            raise ScopeConcurrencyError("principal_corrupt") from exc
        sender_key = self._exact_text(sender_key, "principal_corrupt")
        sender_id = self._exact_text(sender_id, "principal_corrupt")
        relationship_role = self._exact_text(
            relationship_role,
            "principal_corrupt",
            maximum=128,
        )
        verification_source = self._exact_text(
            verification_source,
            "principal_corrupt",
            maximum=256,
        )
        if type(is_owner) is not bool:
            raise ScopeConcurrencyError("principal_corrupt")
        if not sender_key.startswith(f"{record.scope_key}|user:"):
            raise ScopeConcurrencyError("principal_scope_mismatch")
        return record.scope_key, _PrincipalSnapshot(
            sender_key=sender_key,
            sender_id=sender_id,
            is_owner=is_owner,
            relationship_role=relationship_role,
            verification_source=verification_source,
        )

    def _proactive_scope(self, request: object, authority: object) -> str:
        from .proactive_runtime import (
            ProactiveComposerRequest,
            ProactiveExecutionAuthority,
        )

        if (
            type(request) is not ProactiveComposerRequest
            or type(authority) is not ProactiveExecutionAuthority
        ):
            raise ScopeConcurrencyError("proactive_request_not_canonical")
        try:
            authority.inspect_request(request)
            target = request.plan.target
            scope_key = self._exact_text(
                target.scope_key,
                "proactive_scope_invalid",
            )
        except Exception as exc:
            raise ScopeConcurrencyError("proactive_request_not_canonical") from exc
        return scope_key

    def _lane_for(self, scope_key: str) -> _ScopeLane:
        with self._state_lock:
            lane = self._lanes.pop(scope_key, None)
            if lane is not None:
                self._lanes[scope_key] = lane
                return lane
            while len(self._lanes) >= self._max_scope_lanes:
                evicted = False
                for old_scope, old_lane in tuple(self._lanes.items()):
                    if (
                        not old_lane.active
                        and old_lane.waiting == 0
                        and not old_lane.gate.locked()
                    ):
                        self._lanes.pop(old_scope, None)
                        evicted = True
                        break
                if not evicted:
                    self._rejected_total += 1
                    raise ScopeConcurrencyError("scope_lane_capacity_exhausted")
            lane = _ScopeLane(gate=asyncio.Lock())
            self._lanes[scope_key] = lane
            return lane

    def _queue(self, scope_key: str) -> _ScopeLane:
        lane = self._lane_for(scope_key)
        with self._state_lock:
            if self._closed:
                raise ScopeConcurrencyError("scope_coordinator_closed")
            if lane.waiting >= self._max_waiters_per_scope:
                self._rejected_total += 1
                raise ScopeConcurrencyError("scope_waiter_limit")
            if self._waiting_total >= self._max_waiters_total:
                self._rejected_total += 1
                raise ScopeConcurrencyError("total_waiter_limit")
            lane.waiting += 1
            self._waiting_total += 1
        return lane

    def _leave_queue(self, lane: _ScopeLane) -> None:
        with self._state_lock:
            if lane.waiting < 1 or self._waiting_total < 1:
                raise ScopeConcurrencyError("scope_queue_corrupt")
            lane.waiting -= 1
            self._waiting_total -= 1

    async def _run(
        self,
        *,
        scope_key: str,
        principal_snapshot: _PrincipalSnapshot | None,
        kind: ScopeWorkKind,
        current_check: Callable[[], None],
        final_check: Callable[[], None] | None = None,
        work_factory: Callable[[], Awaitable[T]],
    ) -> T:
        if type(kind) is not ScopeWorkKind:
            raise ScopeConcurrencyError("scope_work_kind_invalid")
        if not callable(work_factory):
            raise ScopeConcurrencyError("scope_work_factory_invalid")
        current_task = asyncio.current_task()
        if type(current_task) is not asyncio.Task:
            raise ScopeConcurrencyError("scope_task_unavailable")
        with self._state_lock:
            if self._closed:
                raise ScopeConcurrencyError("scope_coordinator_closed")
            self._tasks.add(current_task)
        global_gate: asyncio.Semaphore | None = None
        lane: _ScopeLane | None = None
        lane_acquired = False
        global_acquired = False
        left_queue = False
        active = False
        try:
            global_gate = self._ensure_loop()
            lane = self._queue(scope_key)
            await lane.gate.acquire()
            lane_acquired = True
            await global_gate.acquire()
            global_acquired = True
            self._leave_queue(lane)
            left_queue = True
            with self._state_lock:
                if self._closed:
                    raise ScopeConcurrencyError("scope_coordinator_closed")
            current_check()
            with self._state_lock:
                if lane.active or lane.active_kind is not None:
                    raise ScopeConcurrencyError("scope_lane_corrupt")
                lane.active = True
                lane.active_kind = kind
                lane.active_principal = principal_snapshot
                self._active_total += 1
                active = True
            awaitable = work_factory()
            if not inspect.isawaitable(awaitable):
                raise ScopeConcurrencyError("scope_work_not_awaitable")
            result = await awaitable
            with self._state_lock:
                if self._closed:
                    raise ScopeConcurrencyError("scope_coordinator_closed")
            (final_check or current_check)()
            with self._state_lock:
                self._completed_total += 1
            return result
        except asyncio.CancelledError:
            with self._state_lock:
                self._cancelled_total += 1
            raise
        finally:
            if lane is not None and not left_queue:
                self._leave_queue(lane)
            if lane is not None and active:
                with self._state_lock:
                    lane.active = False
                    lane.active_kind = None
                    lane.active_principal = None
                    self._active_total -= 1
            if global_gate is not None and global_acquired:
                global_gate.release()
            if lane is not None and lane_acquired:
                lane.gate.release()
            with self._state_lock:
                self._tasks.discard(current_task)

    async def run_event(
        self,
        snapshot: GenerationEpochSnapshot,
        principal: PrincipalContext,
        *,
        kind: ScopeWorkKind,
        work_factory: Callable[[], Awaitable[T]],
    ) -> T:
        scope_key, principal_snapshot = self._event_scope(snapshot, principal)

        def current_check() -> None:
            validation = self._epoch_registry.validate(snapshot)
            if not validation.is_current:
                raise SupersededGeneration(validation.reason_code)

        return await self._run(
            scope_key=scope_key,
            principal_snapshot=principal_snapshot,
            kind=kind,
            current_check=current_check,
            work_factory=work_factory,
        )

    async def run_proactive(
        self,
        request: object,
        authority: object,
        *,
        work_factory: Callable[[], Awaitable[T]],
    ) -> T:
        scope_key = self._proactive_scope(request, authority)

        def current_check() -> None:
            self._proactive_scope(request, authority)

        def final_check() -> None:
            try:
                authority.inspect_request_integrity(request)
                final_scope = self._exact_text(
                    request.plan.target.scope_key,
                    "proactive_scope_invalid",
                )
            except Exception as exc:
                raise ScopeConcurrencyError(
                    "proactive_request_not_canonical"
                ) from exc
            if final_scope != scope_key:
                raise ScopeConcurrencyError("proactive_scope_changed")

        return await self._run(
            scope_key=scope_key,
            principal_snapshot=None,
            kind=ScopeWorkKind.PROACTIVE,
            current_check=current_check,
            final_check=final_check,
            work_factory=work_factory,
        )

    @property
    def active_count(self) -> int:
        with self._state_lock:
            return self._active_total

    @property
    def waiting_count(self) -> int:
        with self._state_lock:
            return self._waiting_total

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._state_lock:
            return {
                "schema_version": 1,
                "scope_concurrency_bounded": True,
                "scope_concurrency_parallel_limit": self._max_parallel_scopes,
                "scope_concurrency_lane_count": len(self._lanes),
                "scope_concurrency_active": self._active_total,
                "scope_concurrency_waiting": self._waiting_total,
                "scope_concurrency_completed": self._completed_total,
                "scope_concurrency_cancelled": self._cancelled_total,
                "scope_concurrency_rejected": self._rejected_total,
                "scope_concurrency_closed": self._closed,
            }

    async def close(self) -> None:
        current = asyncio.current_task()
        with self._state_lock:
            if not self._closed:
                self._closed = True
            tasks = tuple(
                task
                for task in self._tasks
                if task is not current and not task.done()
            )
            for task in tasks:
                task.cancel("scope_coordinator_closed")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "ScopeConcurrencyError",
    "ScopeWorkKind",
    "TurnScopeCoordinator",
]
