"""Bounded shutdown of existing event tasks, without replaying any message."""
from __future__ import annotations

import asyncio
from typing import Any, Callable


class ReplyDrain:
    def __init__(self, on_retire: Callable[[Any], None] | None = None) -> None:
        self._tasks: dict[asyncio.Task, Any] = {}
        self._closed = False
        self._on_retire = on_retire

    def track(self, event: Any) -> bool:
        if self._closed:
            return False
        task = asyncio.current_task()
        if task is not None:
            if task not in self._tasks:
                task.add_done_callback(self._retire)
            self._tasks[task] = event
        return True

    def _retire(self, task: asyncio.Task) -> None:
        event = self._tasks.pop(task, None)
        if event is not None and self._on_retire is not None:
            self._on_retire(event)

    async def wait(self, seconds: float) -> tuple[Any, ...]:
        """Include arrivals during the grace period, never extend its deadline."""
        caller = asyncio.current_task()
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            active = {task: event for task, event in self._tasks.items()
                      if task is not caller and not task.done()}
            remaining = deadline - asyncio.get_running_loop().time()
            if not active or remaining <= 0:
                self._closed = True
                return tuple(active.values())
            await asyncio.wait(active, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)

    def abort(self) -> None:
        """Stop remaining official executions; the plugin fences late hooks too."""
        self._closed = True
        caller = asyncio.current_task()
        for task in tuple(self._tasks):
            if task is not caller and not task.done():
                task.cancel()
