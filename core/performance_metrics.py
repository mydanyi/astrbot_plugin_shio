from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum


class PerformanceMetricsError(ValueError):
    pass


class LatencyKind(str, Enum):
    LOCAL_ORCHESTRATION = "local_orchestration"
    INFERENCE_QUEUE = "inference_queue"
    PRIMARY_PROVIDER = "primary_provider"
    REPAIR_PROVIDER = "repair_provider"
    PROACTIVE_PROVIDER = "proactive_provider"
    FIRST_BUBBLE = "first_bubble"
    FULL_REPLY = "full_reply"


class ModelCallKind(str, Enum):
    PRIMARY = "primary"
    REPAIR = "repair"
    PROACTIVE = "proactive"


@dataclass(frozen=True, slots=True)
class LatencySummary:
    kind: LatencyKind
    sample_count: int
    p50_ms: float
    p95_ms: float
    max_ms: float


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    latencies: tuple[LatencySummary, ...]
    primary_call_count: int
    repair_call_count: int
    proactive_call_count: int
    model_failure_count: int
    max_samples_per_kind: int
    window_bounded: bool

    def latency(self, kind: LatencyKind) -> LatencySummary:
        if type(kind) is not LatencyKind:
            raise PerformanceMetricsError("performance_latency_kind_invalid")
        return next(
            (
                summary
                for summary in self.latencies
                if summary.kind is kind
            ),
            LatencySummary(kind, 0, 0.0, 0.0, 0.0),
        )

    def trace_metadata(self) -> dict[str, int | float | bool]:
        metadata: dict[str, int | float | bool] = {
            "performance_schema_version": 1,
            "performance_window_bounded": self.window_bounded,
            "performance_max_samples_per_kind": self.max_samples_per_kind,
            "model_primary_call_count": self.primary_call_count,
            "model_repair_call_count": self.repair_call_count,
            "model_proactive_call_count": self.proactive_call_count,
            "model_failure_count": self.model_failure_count,
        }
        for summary in self.latencies:
            prefix = f"latency_{summary.kind.value}"
            metadata[f"{prefix}_samples"] = summary.sample_count
            metadata[f"{prefix}_p50_ms"] = summary.p50_ms
            metadata[f"{prefix}_p95_ms"] = summary.p95_ms
            metadata[f"{prefix}_max_ms"] = summary.max_ms
        return metadata


def _nearest_rank(values: tuple[float, ...], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(float(ordered[index]), 3)


class PerformanceWindow:
    """Bounded, content-free latency and model-call aggregate."""

    __slots__ = (
        "_lock",
        "_max_samples_per_kind",
        "_samples",
        "_call_counts",
        "_failure_count",
    )

    def __init__(self, *, max_samples_per_kind: int = 512) -> None:
        if (
            type(max_samples_per_kind) is not int
            or not 1 <= max_samples_per_kind <= 8192
        ):
            raise PerformanceMetricsError("performance_window_limit_invalid")
        self._lock = threading.RLock()
        self._max_samples_per_kind = max_samples_per_kind
        self._samples = {
            kind: deque(maxlen=max_samples_per_kind)
            for kind in LatencyKind
        }
        self._call_counts = {kind: 0 for kind in ModelCallKind}
        self._failure_count = 0

    def observe_latency(self, kind: LatencyKind, value_ms: float) -> None:
        if type(kind) is not LatencyKind:
            raise PerformanceMetricsError("performance_latency_kind_invalid")
        if (
            type(value_ms) is not float
            or not math.isfinite(value_ms)
            or value_ms < 0.0
            or value_ms > 86_400_000.0
        ):
            raise PerformanceMetricsError("performance_latency_value_invalid")
        with self._lock:
            self._samples[kind].append(round(value_ms, 3))

    def record_model_call(
        self,
        kind: ModelCallKind,
        *,
        failed: bool = False,
    ) -> None:
        if type(kind) is not ModelCallKind:
            raise PerformanceMetricsError("performance_model_call_kind_invalid")
        if type(failed) is not bool:
            raise PerformanceMetricsError("performance_model_call_failure_invalid")
        with self._lock:
            self._call_counts[kind] += 1
            self._failure_count += int(failed)

    def record_model_failure(self) -> None:
        with self._lock:
            self._failure_count += 1

    def snapshot(self) -> PerformanceSnapshot:
        with self._lock:
            latencies = tuple(
                LatencySummary(
                    kind=kind,
                    sample_count=len(values),
                    p50_ms=_nearest_rank(tuple(values), 0.50),
                    p95_ms=_nearest_rank(tuple(values), 0.95),
                    max_ms=(round(max(values), 3) if values else 0.0),
                )
                for kind, values in self._samples.items()
            )
            return PerformanceSnapshot(
                latencies=latencies,
                primary_call_count=self._call_counts[ModelCallKind.PRIMARY],
                repair_call_count=self._call_counts[ModelCallKind.REPAIR],
                proactive_call_count=self._call_counts[ModelCallKind.PROACTIVE],
                model_failure_count=self._failure_count,
                max_samples_per_kind=self._max_samples_per_kind,
                window_bounded=all(
                    len(values) <= self._max_samples_per_kind
                    for values in self._samples.values()
                ),
            )


__all__ = [
    "LatencyKind",
    "LatencySummary",
    "ModelCallKind",
    "PerformanceMetricsError",
    "PerformanceSnapshot",
    "PerformanceWindow",
]
