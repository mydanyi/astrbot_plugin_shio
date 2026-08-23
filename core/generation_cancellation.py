from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, TypeVar

from .generation_epoch import (
    EpochBindingError,
    GenerationEpochRegistry,
    GenerationEpochSnapshot,
)


T = TypeVar("T")


class SupersededGeneration(RuntimeError):
    pass


@dataclass(slots=True)
class _OwnedGenerationTask:
    scope_key: str
    epoch: int
    task: asyncio.Task[Any]
    cancel_safe: bool
    superseded: bool = False


def provider_supports_cancellation(provider: Any) -> bool:
    if provider is None:
        return False
    if getattr(provider, "supports_cancellation", None) is True:
        return True
    metadata = getattr(provider, "metadata", None)
    return bool(
        isinstance(metadata, dict)
        and metadata.get("supports_cancellation") is True
    )


class GenerationTaskRegistry:
    """Own and cancel only Shio-created, explicitly cancel-safe child tasks."""

    def __init__(self, epoch_registry: GenerationEpochRegistry) -> None:
        if type(epoch_registry) is not GenerationEpochRegistry:
            raise EpochBindingError("generation task epoch registry required")
        self._epoch_registry = epoch_registry
        self._tasks: dict[asyncio.Task[Any], _OwnedGenerationTask] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _discard_awaitable(awaitable: Awaitable[Any]) -> None:
        if inspect.iscoroutine(awaitable):
            awaitable.close()

    def _require_current(self, snapshot: GenerationEpochSnapshot) -> None:
        self._epoch_registry.inspect(snapshot)
        validation = self._epoch_registry.validate(snapshot)
        if not validation.is_current:
            raise SupersededGeneration(validation.reason_code)

    def cancel_older(self, snapshot: GenerationEpochSnapshot) -> int:
        self._require_current(snapshot)
        cancelled = 0
        with self._lock:
            if self._closed:
                raise SupersededGeneration("generation_task_registry_closed")
            for entry in tuple(self._tasks.values()):
                if entry.scope_key != snapshot.scope_key or entry.epoch >= snapshot.epoch:
                    continue
                if entry.task.done() or not entry.cancel_safe:
                    continue
                entry.superseded = True
                entry.task.cancel("superseded_generation_epoch")
                cancelled += 1
        return cancelled

    async def run(
        self,
        snapshot: GenerationEpochSnapshot,
        awaitable: Awaitable[T],
        *,
        cancel_safe: bool,
    ) -> T:
        if type(cancel_safe) is not bool:
            self._discard_awaitable(awaitable)
            raise EpochBindingError("generation task cancellation flag invalid")
        try:
            self._require_current(snapshot)
        except BaseException:
            self._discard_awaitable(awaitable)
            raise
        task = asyncio.ensure_future(awaitable)
        entry = _OwnedGenerationTask(
            scope_key=snapshot.scope_key,
            epoch=snapshot.epoch,
            task=task,
            cancel_safe=cancel_safe,
        )
        rejected = False
        with self._lock:
            if self._closed:
                task.cancel("generation_task_registry_closed")
                rejected = True
            else:
                self._tasks[task] = entry
        if rejected:
            await asyncio.gather(task, return_exceptions=True)
            raise SupersededGeneration("generation_task_registry_closed")
        try:
            result = await task
            self._require_current(snapshot)
            return result
        except asyncio.CancelledError as exc:
            if entry.superseded:
                raise SupersededGeneration(
                    "generation child task was superseded by a newer epoch"
                ) from exc
            raise
        finally:
            with self._lock:
                self._tasks.pop(task, None)

    async def close(self) -> None:
        with self._lock:
            if self._closed:
                tasks = tuple(self._tasks)
            else:
                self._closed = True
                tasks = tuple(self._tasks)
                for task in tasks:
                    if not task.done():
                        task.cancel("generation_task_registry_closed")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(not entry.task.done() for entry in self._tasks.values())
