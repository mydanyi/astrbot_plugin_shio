from __future__ import annotations

import math
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from .contracts import ContractViolation


class TemporalPeriod(str, Enum):
    LATE_NIGHT = "late_night"
    DAWN = "dawn"
    MORNING = "morning"
    NOON = "noon"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    NIGHT = "night"


_TEMPORAL_SEAL = object()
_WEEKDAY_LABELS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _period_for_hour(hour: int) -> tuple[TemporalPeriod, str]:
    if 0 <= hour < 5:
        return TemporalPeriod.LATE_NIGHT, "凌晨"
    if hour < 8:
        return TemporalPeriod.DAWN, "清晨"
    if hour < 11:
        return TemporalPeriod.MORNING, "上午"
    if hour < 14:
        return TemporalPeriod.NOON, "中午"
    if hour < 18:
        return TemporalPeriod.AFTERNOON, "下午"
    if hour < 20:
        return TemporalPeriod.EVENING, "傍晚"
    if hour < 23:
        return TemporalPeriod.NIGHT, "晚上"
    return TemporalPeriod.LATE_NIGHT, "深夜"


def _offset_text(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    absolute = abs(minutes)
    return f"{sign}{absolute // 60:02d}:{absolute % 60:02d}"


def _local_datetime(observed_at: float, offset_minutes: int) -> datetime:
    return datetime.fromtimestamp(
        observed_at,
        tz=timezone(timedelta(minutes=offset_minutes)),
    )


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class TemporalContext:
    observed_at: float = field(repr=False)
    timezone_offset_minutes: int
    local_date: str
    local_time: str
    weekday: str
    period: TemporalPeriod
    period_label: str
    utc_offset: str
    _seal: object = field(repr=False, compare=False)

    def __new__(cls):
        raise TypeError("TemporalContext is issuer-owned")

    def trace_metadata(self) -> dict[str, object]:
        try:
            inspect_temporal_context(self)
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "temporal_context_canonical": False,
            }
        return {
            "schema_version": 1,
            "temporal_context_canonical": True,
            "temporal_period": self.period.value,
            "temporal_timezone_offset_minutes": self.timezone_offset_minutes,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "TemporalContext("
            f"canonical={metadata['temporal_context_canonical']!r}, "
            f"period={metadata.get('temporal_period', 'invalid')!r})"
        )


def _snapshot(context: TemporalContext) -> tuple[object, ...]:
    if type(context) is not TemporalContext:
        raise ContractViolation("temporal_context_not_canonical")
    try:
        values = (
            context.observed_at,
            context.timezone_offset_minutes,
            context.local_date,
            context.local_time,
            context.weekday,
            context.period,
            context.period_label,
            context.utc_offset,
            context._seal,
        )
    except AttributeError as exc:
        raise ContractViolation("temporal_context_corrupt") from exc
    if (
        type(values[0]) is not float
        or not math.isfinite(values[0])
        or values[0] < 0
        or type(values[1]) is not int
        or not -720 <= values[1] <= 840
        or type(values[2]) is not str
        or type(values[3]) is not str
        or type(values[4]) is not str
        or type(values[5]) is not TemporalPeriod
        or type(values[6]) is not str
        or type(values[7]) is not str
        or values[8] is not _TEMPORAL_SEAL
    ):
        raise ContractViolation("temporal_context_corrupt")
    local = _local_datetime(values[0], values[1])
    expected_period, expected_label = _period_for_hour(local.hour)
    if (
        values[2] != local.strftime("%Y-%m-%d")
        or values[3] != local.strftime("%H:%M")
        or values[4] != _WEEKDAY_LABELS[local.weekday()]
        or values[5] is not expected_period
        or values[6] != expected_label
        or values[7] != _offset_text(values[1])
    ):
        raise ContractViolation("temporal_context_corrupt")
    return values


def _build_temporal_vault():
    lock = threading.RLock()
    records: tuple[tuple[weakref.ReferenceType[TemporalContext], tuple[object, ...]], ...] = ()

    def prune_locked() -> None:
        nonlocal records
        records = tuple(record for record in records if record[0]() is not None)

    def issue(*, now: float, timezone_offset_minutes: int) -> TemporalContext:
        nonlocal records
        local = _local_datetime(now, timezone_offset_minutes)
        period, period_label = _period_for_hour(local.hour)
        context = object.__new__(TemporalContext)
        for name, value in (
            ("observed_at", now),
            ("timezone_offset_minutes", timezone_offset_minutes),
            ("local_date", local.strftime("%Y-%m-%d")),
            ("local_time", local.strftime("%H:%M")),
            ("weekday", _WEEKDAY_LABELS[local.weekday()]),
            ("period", period),
            ("period_label", period_label),
            ("utc_offset", _offset_text(timezone_offset_minutes)),
            ("_seal", _TEMPORAL_SEAL),
        ):
            object.__setattr__(context, name, value)
        snapshot = _snapshot(context)
        with lock:
            prune_locked()
            records = (*records, (weakref.ref(context), snapshot))
        return context

    def inspect(context: TemporalContext) -> TemporalContext:
        with lock:
            prune_locked()
            matches = tuple(record for record in records if record[0]() is context)
            if len(matches) != 1:
                raise ContractViolation("temporal_context_not_canonical")
            try:
                current = _snapshot(context)
            except ContractViolation as exc:
                raise ContractViolation("temporal_context_corrupt") from exc
            if current != matches[0][1]:
                raise ContractViolation("temporal_context_corrupt")
            return context

    return issue, inspect


_issue_temporal_context, inspect_temporal_context = _build_temporal_vault()
del _build_temporal_vault


def build_temporal_context(
    *,
    now: float,
    timezone_offset_minutes: int,
) -> TemporalContext:
    if type(now) not in {int, float} or not math.isfinite(now) or now < 0:
        raise ContractViolation("temporal_clock_invalid")
    if (
        type(timezone_offset_minutes) is not int
        or not -720 <= timezone_offset_minutes <= 840
    ):
        raise ContractViolation("temporal_timezone_invalid")
    return _issue_temporal_context(
        now=float(now),
        timezone_offset_minutes=timezone_offset_minutes,
    )


def temporal_context_prompt_data(context: TemporalContext) -> dict[str, str]:
    inspect_temporal_context(context)
    return {
        "source": "server_clock",
        "local_datetime": f"{context.local_date} {context.local_time}",
        "weekday": context.weekday,
        "time_period": context.period.value,
        "time_period_label": context.period_label,
        "utc_offset": context.utc_offset,
    }


def history_temporal_marker(
    context: TemporalContext,
    *,
    observed_at: float,
) -> dict[str, str]:
    inspect_temporal_context(context)
    if (
        type(observed_at) not in {int, float}
        or not math.isfinite(observed_at)
        or observed_at <= 0
    ):
        return {"status": "unavailable"}
    observed = float(observed_at)
    age = context.observed_at - observed
    if age < -300:
        return {"status": "unavailable"}
    local = _local_datetime(observed, context.timezone_offset_minutes)
    period, label = _period_for_hour(local.hour)
    if age <= 120:
        relative_age = "just_now"
    elif age <= 900:
        relative_age = "within_15_minutes"
    elif age <= 3600:
        relative_age = "within_1_hour"
    elif age <= 10800:
        relative_age = "within_3_hours"
    else:
        current_local = _local_datetime(
            context.observed_at,
            context.timezone_offset_minutes,
        )
        day_delta = (current_local.date() - local.date()).days
        if day_delta == 0:
            relative_age = "earlier_today"
        elif day_delta == 1:
            relative_age = "yesterday"
        elif age <= 604800:
            relative_age = "within_7_days"
        else:
            relative_age = "older"
    return {
        "status": "available",
        "local_datetime": local.strftime("%Y-%m-%d %H:%M"),
        "time_period": period.value,
        "time_period_label": label,
        "relative_age": relative_age,
    }


__all__ = [
    "TemporalContext",
    "TemporalPeriod",
    "build_temporal_context",
    "history_temporal_marker",
    "inspect_temporal_context",
    "temporal_context_prompt_data",
]
