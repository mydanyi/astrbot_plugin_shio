from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from .plugin_contracts import (
    HookPhase,
    HookPoint,
    PluginEffectKind,
    PluginId,
)


_RESULT_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


class HookRunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    STOPPED = "stopped"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    ERROR = "error"


class VirtualHookTimeout(TimeoutError):
    pass


@dataclass(slots=True)
class VirtualClock:
    start: float = 0.0
    _now: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("virtual_clock_start_negative")
        self._now = float(self.start)

    @property
    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("virtual_clock_advance_negative")
        self._now += float(seconds)
        return self._now


@dataclass(frozen=True, slots=True)
class HookOutcome:
    stop_pipeline: bool = False
    result_code: str = "ok"
    effects: tuple[PluginEffectKind, ...] = ()

    def __post_init__(self) -> None:
        code = str(self.result_code or "").strip().lower()
        if not _RESULT_CODE.fullmatch(code):
            raise ValueError("hook_result_code_invalid")
        object.__setattr__(self, "result_code", code)
        object.__setattr__(self, "effects", tuple(self.effects))


@dataclass(frozen=True, slots=True)
class HookContext:
    current_plugin: PluginId
    phase: HookPhase
    pipeline_stopped: bool


@dataclass(slots=True)
class HookControl:
    clock: VirtualClock
    timeout: float

    async def wait_for(self, event: asyncio.Event) -> None:
        """Cooperative virtual wait used by scripted stubs; never sleeps."""

        if not isinstance(event, asyncio.Event):
            raise TypeError("hook_wait_event_invalid")
        if event.is_set():
            return
        self.clock.advance(self.timeout)
        raise VirtualHookTimeout("virtual_deadline_reached")


HookCallback = Callable[[HookContext, HookControl], Awaitable[HookOutcome]]


@dataclass(frozen=True, slots=True)
class HookRegistration:
    plugin_id: PluginId
    point: HookPoint
    callback: HookCallback
    timeout: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.plugin_id, PluginId):
            raise TypeError("hook_plugin_id_invalid")
        if not isinstance(self.point, HookPoint):
            raise TypeError("hook_point_invalid")
        if not callable(self.callback):
            raise TypeError("hook_callback_invalid")
        if not isinstance(self.timeout, (int, float)) or not 0 < float(self.timeout) <= 60:
            raise ValueError("hook_timeout_invalid")
        object.__setattr__(self, "timeout", float(self.timeout))


@dataclass(frozen=True, slots=True)
class HookRunRecord:
    plugin_id: PluginId
    point: HookPoint
    status: HookRunStatus
    started_at: float
    ended_at: float
    result_code: str = ""
    error_kind: str = ""
    effects: tuple[PluginEffectKind, ...] = ()


class HookRunner:
    """Deterministic hook scheduler for synthetic production-plugin stubs."""

    def __init__(self, *, clock: VirtualClock) -> None:
        self._clock = clock

    async def run(
        self,
        registrations: tuple[HookRegistration, ...],
    ) -> tuple[HookRunRecord, ...]:
        ordered = sorted(
            tuple(registrations),
            key=lambda registration: (
                int(registration.point.phase),
                -registration.point.priority,
                registration.plugin_id.value,
                registration.point.role.value,
            ),
        )
        records: list[HookRunRecord] = []
        stopped = False
        for registration in ordered:
            if stopped:
                records.append(
                    HookRunRecord(
                        plugin_id=registration.plugin_id,
                        point=registration.point,
                        status=HookRunStatus.SKIPPED,
                        started_at=self._clock.now,
                        ended_at=self._clock.now,
                        result_code="pipeline_stopped",
                    )
                )
                continue

            started_at = self._clock.now
            context = HookContext(
                current_plugin=registration.plugin_id,
                phase=registration.point.phase,
                pipeline_stopped=False,
            )
            control = HookControl(clock=self._clock, timeout=registration.timeout)
            try:
                outcome = await registration.callback(context, control)
                if not isinstance(outcome, HookOutcome):
                    raise TypeError("hook_outcome_invalid")
            except VirtualHookTimeout:
                records.append(
                    HookRunRecord(
                        plugin_id=registration.plugin_id,
                        point=registration.point,
                        status=HookRunStatus.TIMED_OUT,
                        started_at=started_at,
                        ended_at=self._clock.now,
                        result_code="timeout",
                    )
                )
                continue
            except Exception as exc:  # Synthetic failure is data, not a test crash.
                records.append(
                    HookRunRecord(
                        plugin_id=registration.plugin_id,
                        point=registration.point,
                        status=HookRunStatus.ERROR,
                        started_at=started_at,
                        ended_at=self._clock.now,
                        result_code="error",
                        error_kind=type(exc).__name__,
                    )
                )
                continue

            status = (
                HookRunStatus.STOPPED
                if outcome.stop_pipeline
                else HookRunStatus.SUCCEEDED
            )
            records.append(
                HookRunRecord(
                    plugin_id=registration.plugin_id,
                    point=registration.point,
                    status=status,
                    started_at=started_at,
                    ended_at=self._clock.now,
                    result_code=outcome.result_code,
                    effects=outcome.effects,
                )
            )
            stopped = outcome.stop_pipeline
        return tuple(records)


__all__ = [
    "HookContext",
    "HookControl",
    "HookOutcome",
    "HookRegistration",
    "HookRunRecord",
    "HookRunStatus",
    "HookRunner",
    "VirtualClock",
    "VirtualHookTimeout",
]
