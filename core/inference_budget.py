from __future__ import annotations

import asyncio
import inspect
import math
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic as _monotonic
from typing import Awaitable, Callable, TypeVar

from .action_planner import PlannedAction, PlannedActionAuthority
from .contracts import ActionKind, ContractViolation
from .generation_epoch import (
    EpochBindingError,
    GenerationEpochRegistry,
    GenerationEpochSnapshot,
)
from .identity import PrincipalContext


T = TypeVar("T")
_PERMIT_SEAL = object()


class InferenceBudgetError(RuntimeError):
    pass


class InferencePriority(str, Enum):
    DIRECT = "direct"
    PARTICIPATION = "participation"
    PROACTIVE = "proactive"


class InferencePurpose(str, Enum):
    PRIMARY = "primary"
    REPAIR = "repair"
    PROACTIVE = "proactive"


_PRIORITY_RANK = {
    InferencePriority.DIRECT: 0,
    InferencePriority.PARTICIPATION: 1,
    InferencePriority.PROACTIVE: 2,
}


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class InferencePermit:
    priority: InferencePriority
    purpose: InferencePurpose
    wait_seconds: float
    _authority_ref: weakref.ReferenceType[InferenceBudgetAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | float | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            record = authority._inspect_record(self)  # type: ignore[union-attr]
        except (InferenceBudgetError, AttributeError, TypeError):
            return {
                "inference_permit_canonical": False,
                "inference_priority": "invalid",
                "inference_purpose": "invalid",
                "inference_wait_seconds": 0.0,
                "inference_permit_active": False,
            }
        return {
            "inference_permit_canonical": True,
            "inference_priority": record.priority.value,
            "inference_purpose": record.purpose.value,
            "inference_wait_seconds": record.wait_seconds,
            "inference_permit_active": record.state == "active",
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "InferencePermit("
            f"canonical={metadata['inference_permit_canonical']!r}, "
            f"priority={metadata['inference_priority']!r}, "
            f"purpose={metadata['inference_purpose']!r}, "
            f"active={metadata['inference_permit_active']!r})"
        )


@dataclass(frozen=True, slots=True)
class _PrincipalSnapshot:
    sender_key: str
    sender_id: str
    is_owner: bool
    relationship_role: str
    verification_source: str


@dataclass(frozen=True, slots=True)
class _PermitRecord:
    priority: InferencePriority
    purpose: InferencePurpose
    wait_seconds: float
    acquired_at: float
    state: str
    snapshot: tuple[object, ...]
    generation_snapshot: GenerationEpochSnapshot | None
    planned_action: PlannedAction | None
    proactive_request: object | None
    proactive_authority: object | None


@dataclass(slots=True)
class _Waiter:
    sequence: int
    priority: InferencePriority
    purpose: InferencePurpose
    enqueued_at: float
    future: asyncio.Future[InferencePermit]
    call_key: tuple[str, int] | None
    generation_snapshot: GenerationEpochSnapshot | None = None
    principal_snapshot: _PrincipalSnapshot | None = None
    planned_action: PlannedAction | None = None
    proactive_request: object | None = None
    proactive_authority: object | None = None


class InferenceBudgetAuthority:
    """One global, priority-aware owner for all model calls made by Shio."""

    def __init__(
        self,
        epoch_registry: GenerationEpochRegistry,
        planned_action_authority: PlannedActionAuthority,
        *,
        max_active: int = 4,
        max_waiters: int = 128,
        queue_timeout_seconds: float = 30.0,
        active_timeout_seconds: float = 300.0,
        max_history: int = 4096,
    ) -> None:
        if type(epoch_registry) is not GenerationEpochRegistry:
            raise InferenceBudgetError("inference_epoch_registry_required")
        if type(planned_action_authority) is not PlannedActionAuthority:
            raise InferenceBudgetError("inference_plan_authority_required")
        for value, reason, maximum in (
            (max_active, "inference_active_limit_invalid", 64),
            (max_waiters, "inference_waiter_limit_invalid", 4096),
            (max_history, "inference_history_limit_invalid", 16384),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise InferenceBudgetError(reason)
        if (
            type(queue_timeout_seconds) is not float
            or not math.isfinite(queue_timeout_seconds)
            or not 0.001 <= queue_timeout_seconds <= 300.0
        ):
            raise InferenceBudgetError("inference_queue_timeout_invalid")
        if (
            type(active_timeout_seconds) is not float
            or not math.isfinite(active_timeout_seconds)
            or not 0.001 <= active_timeout_seconds <= 3600.0
        ):
            raise InferenceBudgetError("inference_active_timeout_invalid")
        self._epoch_registry = epoch_registry
        self._planned_action_authority = planned_action_authority
        self._max_active = max_active
        self._max_waiters = max_waiters
        self._queue_timeout_seconds = queue_timeout_seconds
        self._active_timeout_seconds = active_timeout_seconds
        self._max_history = max_history
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_lock: asyncio.Lock | None = None
        self._permit_lock = threading.RLock()
        self._permit_records: OrderedDict[InferencePermit, _PermitRecord] = OrderedDict()
        self._waiters: list[_Waiter] = []
        self._issued_event_calls: OrderedDict[
            tuple[str, int], set[InferencePurpose]
        ] = OrderedDict()
        self._issued_proactive: weakref.WeakKeyDictionary[object, bool] = (
            weakref.WeakKeyDictionary()
        )
        self._active_count = 0
        self._sequence = 0
        self._closed = False
        self._completed_count = 0
        self._timeout_count = 0
        self._rejected_count = 0
        self._expired_count = 0
        self._expired_active_count = 0

    @staticmethod
    def _exact_text(value: object, reason: str, *, maximum: int = 1024) -> str:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > maximum
            or any(ord(character) < 32 for character in value)
        ):
            raise InferenceBudgetError(reason)
        return value

    def _lock_for_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._async_lock = asyncio.Lock()
        elif self._loop is not loop:
            raise InferenceBudgetError("inference_budget_loop_mismatch")
        lock = self._async_lock
        if type(lock) is not asyncio.Lock:
            raise InferenceBudgetError("inference_budget_not_initialized")
        return lock

    def _principal_snapshot(
        self,
        principal: PrincipalContext,
        *,
        scope_key: str,
        sender_key: str,
    ) -> _PrincipalSnapshot:
        if type(principal) is not PrincipalContext:
            raise InferenceBudgetError("inference_principal_not_exact")
        try:
            values = (
                principal.sender_key,
                principal.sender_id,
                principal.is_owner,
                principal.relationship_role,
                principal.verification_source,
            )
        except AttributeError as exc:
            raise InferenceBudgetError("inference_principal_corrupt") from exc
        if type(values[2]) is not bool:
            raise InferenceBudgetError("inference_principal_corrupt")
        sender = self._exact_text(values[0], "inference_principal_corrupt")
        sender_id = self._exact_text(values[1], "inference_principal_corrupt")
        role = self._exact_text(
            values[3], "inference_principal_corrupt", maximum=128
        )
        source = self._exact_text(
            values[4], "inference_principal_corrupt", maximum=256
        )
        if sender != sender_key or not sender.startswith(f"{scope_key}|user:"):
            raise InferenceBudgetError("inference_principal_scope_mismatch")
        return _PrincipalSnapshot(sender, sender_id, values[2], role, source)

    def _event_inputs(
        self,
        snapshot: GenerationEpochSnapshot,
        principal: PrincipalContext,
        planned_action: PlannedAction,
        purpose: InferencePurpose,
    ) -> tuple[
        tuple[str, int], InferencePriority, _PrincipalSnapshot
    ]:
        if type(purpose) is not InferencePurpose or purpose not in {
            InferencePurpose.PRIMARY,
            InferencePurpose.REPAIR,
        }:
            raise InferenceBudgetError("inference_event_purpose_invalid")
        if type(snapshot) is not GenerationEpochSnapshot:
            raise InferenceBudgetError("inference_generation_not_canonical")
        try:
            epoch_record = self._epoch_registry.inspect(snapshot)
            self._planned_action_authority.inspect_plan(planned_action)
        except (EpochBindingError, ContractViolation) as exc:
            raise InferenceBudgetError("inference_event_not_canonical") from exc
        validation = self._epoch_registry.validate(snapshot)
        if not validation.is_current:
            raise InferenceBudgetError("inference_generation_superseded")
        binding = planned_action.binding
        if (
            binding.scope_key != epoch_record.scope_key
            or binding.session_id != epoch_record.session_id
            or binding.generation_epoch != epoch_record.epoch
            or planned_action.kind
            not in {ActionKind.REPLY, ActionKind.USE_TOOL, ActionKind.EXECUTE_ACTION}
        ):
            raise InferenceBudgetError("inference_event_binding_mismatch")
        principal_snapshot = self._principal_snapshot(
            principal,
            scope_key=epoch_record.scope_key,
            sender_key=binding.current_sender_key,
        )
        priority = (
            InferencePriority.DIRECT
            if (
                planned_action.kind is ActionKind.EXECUTE_ACTION
                or "direct_force_must_reply" in planned_action.planner_reason_codes
            )
            else InferencePriority.PARTICIPATION
        )
        return (epoch_record.scope_key, epoch_record.epoch), priority, principal_snapshot

    @staticmethod
    def _permit_snapshot(
        permit: InferencePermit,
    ) -> tuple[object, ...]:
        try:
            priority = permit.priority
            purpose = permit.purpose
            wait_seconds = permit.wait_seconds
            authority_ref = permit._authority_ref
            seal = permit._seal
        except AttributeError as exc:
            raise InferenceBudgetError("inference_permit_corrupt") from exc
        if (
            type(priority) is not InferencePriority
            or type(purpose) is not InferencePurpose
            or type(wait_seconds) is not float
            or not math.isfinite(wait_seconds)
            or wait_seconds < 0
            or type(authority_ref) is not weakref.ReferenceType
            or seal is not _PERMIT_SEAL
        ):
            raise InferenceBudgetError("inference_permit_corrupt")
        return priority, purpose, wait_seconds, authority_ref, seal

    def _mint_permit(
        self,
        waiter: _Waiter,
        *,
        wait_seconds: float,
        acquired_at: float,
    ) -> InferencePermit:
        permit = object.__new__(InferencePermit)
        authority_ref = weakref.ref(self)
        for name, value in (
            ("priority", waiter.priority),
            ("purpose", waiter.purpose),
            ("wait_seconds", wait_seconds),
            ("_authority_ref", authority_ref),
            ("_seal", _PERMIT_SEAL),
        ):
            object.__setattr__(permit, name, value)
        snapshot = self._permit_snapshot(permit)
        record = _PermitRecord(
            priority=waiter.priority,
            purpose=waiter.purpose,
            wait_seconds=wait_seconds,
            acquired_at=acquired_at,
            state="active",
            snapshot=snapshot,
            generation_snapshot=waiter.generation_snapshot,
            planned_action=waiter.planned_action,
            proactive_request=waiter.proactive_request,
            proactive_authority=waiter.proactive_authority,
        )
        with self._permit_lock:
            self._permit_records[permit] = record
            self._trim_permit_history_locked()
        return permit

    def _trim_permit_history_locked(self) -> None:
        while len(self._permit_records) > self._max_history:
            removed = False
            for permit, record in tuple(self._permit_records.items()):
                if record.state in {"released", "closed"}:
                    self._permit_records.pop(permit, None)
                    removed = True
                    break
            if not removed:
                raise InferenceBudgetError("inference_permit_history_full")

    def _inspect_record(
        self,
        permit: InferencePermit,
        *,
        validate_source: bool = True,
    ) -> _PermitRecord:
        if type(permit) is not InferencePermit:
            raise InferenceBudgetError("inference_permit_not_canonical")
        with self._permit_lock:
            record = self._permit_records.get(permit)
            if type(record) is not _PermitRecord:
                raise InferenceBudgetError("inference_permit_not_canonical")
            current = self._permit_snapshot(permit)
            if (
                current != record.snapshot
                or permit._authority_ref() is not self
                or permit.priority is not record.priority
                or permit.purpose is not record.purpose
                or permit.wait_seconds != record.wait_seconds
                or record.state not in {"active", "expired", "released", "closed"}
            ):
                raise InferenceBudgetError("inference_permit_corrupt")
            if not validate_source:
                return record
            if record.planned_action is not None:
                try:
                    self._planned_action_authority.inspect_plan(record.planned_action)
                except ContractViolation as exc:
                    raise InferenceBudgetError("inference_permit_corrupt") from exc
            elif record.proactive_request is not None:
                try:
                    record.proactive_authority.inspect_request(record.proactive_request)
                except Exception as exc:
                    raise InferenceBudgetError("inference_permit_corrupt") from exc
            else:
                raise InferenceBudgetError("inference_permit_corrupt")
            return record

    def inspect(self, permit: InferencePermit) -> InferencePermit:
        self._inspect_record(permit)
        return permit

    def _event_current(self, waiter: _Waiter) -> bool:
        snapshot = waiter.generation_snapshot
        plan = waiter.planned_action
        if snapshot is None or plan is None:
            return False
        try:
            self._epoch_registry.inspect(snapshot)
            self._planned_action_authority.inspect_plan(plan)
        except (EpochBindingError, ContractViolation):
            return False
        return self._epoch_registry.validate(snapshot).is_current

    @staticmethod
    def _proactive_current(waiter: _Waiter) -> bool:
        try:
            waiter.proactive_authority.inspect_request(waiter.proactive_request)
        except Exception:
            return False
        return True

    def _rollback_waiter_reservation(self, waiter: _Waiter) -> None:
        if waiter.call_key is not None:
            purposes = self._issued_event_calls.get(waiter.call_key)
            if purposes is not None:
                purposes.discard(waiter.purpose)
                if not purposes:
                    self._issued_event_calls.pop(waiter.call_key, None)
        elif waiter.proactive_request is not None:
            self._issued_proactive.pop(waiter.proactive_request, None)

    def _dispatch_locked(self, now: float) -> None:
        self._waiters.sort(
            key=lambda waiter: (_PRIORITY_RANK[waiter.priority], waiter.sequence)
        )
        if self._expired_active_count:
            return
        while not self._closed and self._active_count < self._max_active and self._waiters:
            waiter = self._waiters.pop(0)
            current = (
                self._event_current(waiter)
                if waiter.generation_snapshot is not None
                else self._proactive_current(waiter)
            )
            if not current:
                self._rollback_waiter_reservation(waiter)
                if not waiter.future.done():
                    waiter.future.set_exception(
                        InferenceBudgetError("inference_waiter_superseded")
                    )
                continue
            wait_seconds = max(0.0, float(now - waiter.enqueued_at))
            permit = self._mint_permit(
                waiter,
                wait_seconds=wait_seconds,
                acquired_at=now,
            )
            self._active_count += 1
            if not waiter.future.done():
                waiter.future.set_result(permit)

    def _trim_call_history(self) -> None:
        while len(self._issued_event_calls) > self._max_history:
            self._issued_event_calls.popitem(last=False)

    def _expire_active_locked(self, now: float) -> int:
        expired = 0
        with self._permit_lock:
            for permit, record in tuple(self._permit_records.items()):
                if (
                    record.state != "active"
                    or now - record.acquired_at < self._active_timeout_seconds
                ):
                    continue
                self._permit_records[permit] = _PermitRecord(
                    priority=record.priority,
                    purpose=record.purpose,
                    wait_seconds=record.wait_seconds,
                    acquired_at=record.acquired_at,
                    state="expired",
                    snapshot=record.snapshot,
                    generation_snapshot=record.generation_snapshot,
                    planned_action=record.planned_action,
                    proactive_request=record.proactive_request,
                    proactive_authority=record.proactive_authority,
                )
                expired += 1
        self._expired_count += expired
        self._expired_active_count += expired
        return expired

    async def _enqueue(self, waiter: _Waiter) -> InferencePermit:
        lock = self._lock_for_loop()
        async with lock:
            if self._closed:
                raise InferenceBudgetError("inference_budget_closed")
            self._expire_active_locked(_monotonic())
            if self._expired_active_count:
                self._rollback_waiter_reservation(waiter)
                self._rejected_count += 1
                raise InferenceBudgetError("inference_active_timeout")
            if len(self._waiters) >= self._max_waiters:
                self._rejected_count += 1
                self._rollback_waiter_reservation(waiter)
                raise InferenceBudgetError("inference_waiter_limit")
            self._waiters.append(waiter)
            self._dispatch_locked(_monotonic())
        try:
            return await asyncio.wait_for(
                asyncio.shield(waiter.future),
                timeout=self._queue_timeout_seconds,
            )
        except TimeoutError as exc:
            async with lock:
                if waiter.future.done():
                    return waiter.future.result()
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._rollback_waiter_reservation(waiter)
                self._timeout_count += 1
            raise InferenceBudgetError("inference_queue_timeout") from exc
        except asyncio.CancelledError:
            async with lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._rollback_waiter_reservation(waiter)
                elif waiter.future.done() and not waiter.future.cancelled():
                    try:
                        permit = waiter.future.result()
                    except Exception:
                        permit = None
                    if type(permit) is InferencePermit:
                        self._release_locked(permit, state="released")
                self._dispatch_locked(_monotonic())
            raise

    async def acquire_event(
        self,
        snapshot: GenerationEpochSnapshot,
        principal: PrincipalContext,
        planned_action: PlannedAction,
        *,
        purpose: InferencePurpose,
    ) -> InferencePermit:
        call_key, priority, principal_snapshot = self._event_inputs(
            snapshot,
            principal,
            planned_action,
            purpose,
        )
        lock = self._lock_for_loop()
        loop = asyncio.get_running_loop()
        async with lock:
            if self._closed:
                raise InferenceBudgetError("inference_budget_closed")
            self._expire_active_locked(_monotonic())
            if self._expired_active_count:
                self._rejected_count += 1
                raise InferenceBudgetError("inference_active_timeout")
            purposes = self._issued_event_calls.setdefault(call_key, set())
            if purpose in purposes or len(purposes) >= 2:
                self._rejected_count += 1
                raise InferenceBudgetError("inference_call_budget_exhausted")
            if purpose is InferencePurpose.REPAIR and InferencePurpose.PRIMARY not in purposes:
                self._rejected_count += 1
                raise InferenceBudgetError("inference_primary_call_required")
            purposes.add(purpose)
            self._issued_event_calls.move_to_end(call_key)
            self._trim_call_history()
            self._sequence += 1
            waiter = _Waiter(
                sequence=self._sequence,
                priority=priority,
                purpose=purpose,
                enqueued_at=_monotonic(),
                future=loop.create_future(),
                call_key=call_key,
                generation_snapshot=snapshot,
                principal_snapshot=principal_snapshot,
                planned_action=planned_action,
            )
        return await self._enqueue(waiter)

    async def acquire_proactive(
        self,
        request: object,
        authority: object,
    ) -> InferencePermit:
        from .proactive_runtime import (
            ProactiveComposerRequest,
            ProactiveExecutionAuthority,
        )

        if (
            type(request) is not ProactiveComposerRequest
            or type(authority) is not ProactiveExecutionAuthority
        ):
            raise InferenceBudgetError("inference_proactive_not_canonical")
        try:
            authority.inspect_request(request)
        except Exception as exc:
            raise InferenceBudgetError("inference_proactive_not_canonical") from exc
        lock = self._lock_for_loop()
        loop = asyncio.get_running_loop()
        async with lock:
            if self._closed:
                raise InferenceBudgetError("inference_budget_closed")
            self._expire_active_locked(_monotonic())
            if self._expired_active_count:
                self._rejected_count += 1
                raise InferenceBudgetError("inference_active_timeout")
            if self._issued_proactive.get(request, False):
                self._rejected_count += 1
                raise InferenceBudgetError("inference_call_budget_exhausted")
            self._issued_proactive[request] = True
            self._sequence += 1
            waiter = _Waiter(
                sequence=self._sequence,
                priority=InferencePriority.PROACTIVE,
                purpose=InferencePurpose.PROACTIVE,
                enqueued_at=_monotonic(),
                future=loop.create_future(),
                call_key=None,
                proactive_request=request,
                proactive_authority=authority,
            )
        return await self._enqueue(waiter)

    def _release_locked(self, permit: InferencePermit, *, state: str) -> bool:
        with self._permit_lock:
            record = self._permit_records.get(permit)
            if type(record) is not _PermitRecord or record.state not in {
                "active",
                "expired",
            }:
                return False
            timely = record.state == "active"
            self._permit_records[permit] = _PermitRecord(
                priority=record.priority,
                purpose=record.purpose,
                wait_seconds=record.wait_seconds,
                acquired_at=record.acquired_at,
                state=state,
                snapshot=record.snapshot,
                generation_snapshot=record.generation_snapshot,
                planned_action=record.planned_action,
                proactive_request=record.proactive_request,
                proactive_authority=record.proactive_authority,
            )
            self._permit_records.move_to_end(permit)
            if not timely:
                self._expired_active_count -= 1
        self._active_count -= 1
        self._completed_count += 1
        return timely

    async def release(self, permit: InferencePermit) -> bool:
        lock = self._lock_for_loop()
        async with lock:
            if self._closed:
                return False
            # Releasing capacity is cleanup, not a new authorization decision.
            # The source plan may have been superseded or evicted while the
            # external model call was in flight; an exact active permit must
            # still be able to free its global slot.
            self._expire_active_locked(_monotonic())
            self._inspect_record(permit, validate_source=False)
            released = self._release_locked(permit, state="released")
            if self._expired_active_count == 0:
                self._dispatch_locked(_monotonic())
            return released

    async def _release_after_work(self, permit: InferencePermit) -> bool:
        task = asyncio.create_task(self.release(permit))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def sweep_expired(self) -> int:
        lock = self._lock_for_loop()
        async with lock:
            if self._closed:
                return 0
            return self._expire_active_locked(_monotonic())

    async def run_event_call(
        self,
        snapshot: GenerationEpochSnapshot,
        principal: PrincipalContext,
        planned_action: PlannedAction,
        *,
        purpose: InferencePurpose,
        work_factory: Callable[[], Awaitable[T]],
        permit_observer: Callable[[InferencePermit], None] | None = None,
    ) -> T:
        if not callable(work_factory):
            raise InferenceBudgetError("inference_work_factory_invalid")
        if permit_observer is not None and not callable(permit_observer):
            raise InferenceBudgetError("inference_permit_observer_invalid")
        permit = await self.acquire_event(
            snapshot,
            principal,
            planned_action,
            purpose=purpose,
        )
        try:
            if permit_observer is not None:
                permit_observer(permit)
            awaitable = work_factory()
            if not inspect.isawaitable(awaitable):
                raise InferenceBudgetError("inference_work_not_awaitable")
            result = await awaitable
        except BaseException:
            await self._release_after_work(permit)
            raise
        if not await self._release_after_work(permit):
            raise InferenceBudgetError("inference_active_timeout")
        return result

    async def run_proactive_call(
        self,
        request: object,
        authority: object,
        *,
        work_factory: Callable[[], Awaitable[T]],
        permit_observer: Callable[[InferencePermit], None] | None = None,
    ) -> T:
        if not callable(work_factory):
            raise InferenceBudgetError("inference_work_factory_invalid")
        if permit_observer is not None and not callable(permit_observer):
            raise InferenceBudgetError("inference_permit_observer_invalid")
        permit = await self.acquire_proactive(request, authority)
        try:
            if permit_observer is not None:
                permit_observer(permit)
            awaitable = work_factory()
            if not inspect.isawaitable(awaitable):
                raise InferenceBudgetError("inference_work_not_awaitable")
            result = await awaitable
        except BaseException:
            await self._release_after_work(permit)
            raise
        if not await self._release_after_work(permit):
            raise InferenceBudgetError("inference_active_timeout")
        return result

    async def close(self) -> None:
        lock = self._lock_for_loop()
        async with lock:
            if self._closed:
                return
            self._closed = True
            for waiter in tuple(self._waiters):
                self._rollback_waiter_reservation(waiter)
                if not waiter.future.done():
                    waiter.future.set_exception(
                        InferenceBudgetError("inference_budget_closed")
                    )
            self._waiters.clear()
            with self._permit_lock:
                for permit, record in tuple(self._permit_records.items()):
                    if record.state in {"active", "expired"}:
                        self._permit_records[permit] = _PermitRecord(
                            priority=record.priority,
                            purpose=record.purpose,
                            wait_seconds=record.wait_seconds,
                            acquired_at=record.acquired_at,
                            state="closed",
                            snapshot=record.snapshot,
                            generation_snapshot=record.generation_snapshot,
                            planned_action=record.planned_action,
                            proactive_request=record.proactive_request,
                            proactive_authority=record.proactive_authority,
                        )
            self._active_count = 0
            self._expired_active_count = 0

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def waiting_count(self) -> int:
        return len(self._waiters)

    def trace_metadata(self) -> dict[str, int | float | bool]:
        return {
            "schema_version": 1,
            "inference_budget_bounded": True,
            "inference_budget_closed": self._closed,
            "inference_active_limit": self._max_active,
            "inference_waiter_limit": self._max_waiters,
            "inference_active_timeout_seconds": self._active_timeout_seconds,
            "inference_active_count": self._active_count,
            "inference_expired_active_count": self._expired_active_count,
            "inference_expired_count": self._expired_count,
            "inference_waiting_count": len(self._waiters),
            "inference_completed_count": self._completed_count,
            "inference_timeout_count": self._timeout_count,
            "inference_rejected_count": self._rejected_count,
        }


__all__ = [
    "InferenceBudgetAuthority",
    "InferenceBudgetError",
    "InferencePermit",
    "InferencePriority",
    "InferencePurpose",
]
