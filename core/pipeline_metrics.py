from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .pipeline_trace import TraceContext, TraceStage, get_trace_context


PIPELINE_METRICS_EXTRA = "_shio_pipeline_metrics_v2"


class PipelinePhase(str, Enum):
    INGRESS = "ingress"
    TARGET = "target"
    CONTEXT = "context"
    PLANNER = "planner"
    TOOL = "tool"
    REPLYER = "replyer"
    GUARD = "guard"
    BUBBLE = "bubble"
    SEND = "send"
    CONCURRENCY = "concurrency"
    OTHER = "other"


_STAGE_PHASE = {
    "input": PipelinePhase.INGRESS,
    "target": PipelinePhase.TARGET,
    "context": PipelinePhase.CONTEXT,
    "plan": PipelinePhase.PLANNER,
    "tool_result": PipelinePhase.TOOL,
    "raw_reply": PipelinePhase.REPLYER,
    "replyer_failed": PipelinePhase.REPLYER,
    "final_reply": PipelinePhase.REPLYER,
    "guard": PipelinePhase.GUARD,
    "repair": PipelinePhase.GUARD,
    "bubble_planned": PipelinePhase.BUBBLE,
    "send_attempt": PipelinePhase.SEND,
    "send_handoff": PipelinePhase.SEND,
    "send_success": PipelinePhase.SEND,
    "send_failed": PipelinePhase.SEND,
    "final_send_blocked": PipelinePhase.SEND,
    "stale_generation_drop": PipelinePhase.CONCURRENCY,
}


@dataclass(frozen=True, slots=True)
class PhaseMetric:
    phase: PipelinePhase
    stage_count: int
    duration_ms: float
    max_interval_ms: float
    call_count: int
    failure_count: int
    guard_hit_count: int
    repair_attempt_count: int
    stale_drop_count: int
    send_success_count: int
    send_failure_count: int


@dataclass(frozen=True, slots=True)
class PipelineMetricsSnapshot:
    trace_id: str
    total_elapsed_ms: float
    stage_count: int
    terminal_outcome: str
    phases: tuple[PhaseMetric, ...]

    def phase(self, value: PipelinePhase) -> PhaseMetric:
        return next(
            (metric for metric in self.phases if metric.phase == value),
            PhaseMetric(value, 0, 0.0, 0.0, 0, 0, 0, 0, 0, 0, 0),
        )

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        metadata: dict[str, str | int | float | bool] = {
            "outcome": self.terminal_outcome,
            "total_latency_ms": self.total_elapsed_ms,
            "stage_count": self.stage_count,
        }
        for metric in self.phases:
            prefix = f"phase_{metric.phase.value}"
            metadata[f"{prefix}_stage_count"] = metric.stage_count
            metadata[f"{prefix}_latency_ms"] = metric.duration_ms
            metadata[f"{prefix}_max_interval_ms"] = metric.max_interval_ms
            metadata[f"{prefix}_call_count"] = metric.call_count
            metadata[f"{prefix}_failure_count"] = metric.failure_count
            metadata[f"{prefix}_guard_hit_count"] = metric.guard_hit_count
            metadata[f"{prefix}_repair_attempt_count"] = metric.repair_attempt_count
            metadata[f"{prefix}_stale_drop_count"] = metric.stale_drop_count
            metadata[f"{prefix}_send_success_count"] = metric.send_success_count
            metadata[f"{prefix}_send_failure_count"] = metric.send_failure_count
        return metadata


def _numeric(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _stage_call_count(stage: TraceStage) -> int:
    if stage.stage == "tool_result":
        return _numeric(stage.metadata, "typed_tool_result_count")
    return sum(
        _numeric(stage.metadata, key)
        for key in stage.metadata
        if key.endswith("_call_count")
    )


def _stage_failure_count(stage: TraceStage) -> int:
    if stage.stage in {"send_failed", "final_send_blocked", "stale_generation_drop"}:
        return 1
    if stage.stage == "tool_result":
        total = _numeric(stage.metadata, "typed_tool_result_count")
        succeeded = _numeric(stage.metadata, "typed_tool_result_success_count")
        orphaned = _numeric(stage.metadata, "typed_tool_result_orphan_count")
        return max(max(0, total - succeeded), orphaned)
    return sum(
        _numeric(stage.metadata, key)
        for key in stage.metadata
        if key == "failure_count" or key.endswith("_failure_count")
    )


def _terminal_outcome(stages: Iterable[TraceStage]) -> str:
    values = list(stages)
    if any(stage.stage == "stale_generation_drop" for stage in values):
        return "stale_drop"
    if any(stage.stage == "final_send_blocked" for stage in values):
        return "send_blocked"
    send_stages = [stage.stage for stage in values if _STAGE_PHASE.get(stage.stage) == PipelinePhase.SEND]
    if "send_success" in send_stages:
        return "sent_with_failure" if "send_failed" in send_stages else "sent"
    if "send_failed" in send_stages:
        return "send_failed"
    if any(stage.stage == "final_reply" for stage in values):
        return "ready_to_send"
    if any(stage.stage == "guard" for stage in values):
        return "guarded"
    return "in_progress"


def summarize_pipeline_metrics(
    context: TraceContext | None,
) -> PipelineMetricsSnapshot:
    if context is None:
        return PipelineMetricsSnapshot("", 0.0, 0, "missing_trace", ())
    accumulators: dict[PipelinePhase, dict[str, float | int]] = {}
    previous_elapsed = 0.0
    for stage in context.stages:
        phase = _STAGE_PHASE.get(stage.stage, PipelinePhase.OTHER)
        interval = max(0.0, float(stage.elapsed_ms) - previous_elapsed)
        previous_elapsed = max(previous_elapsed, float(stage.elapsed_ms))
        values = accumulators.setdefault(
            phase,
            {
                "stage_count": 0,
                "duration_ms": 0.0,
                "max_interval_ms": 0.0,
                "call_count": 0,
                "failure_count": 0,
                "guard_hit_count": 0,
                "repair_attempt_count": 0,
                "stale_drop_count": 0,
                "send_success_count": 0,
                "send_failure_count": 0,
            },
        )
        values["stage_count"] += 1
        values["duration_ms"] += interval
        values["max_interval_ms"] = max(float(values["max_interval_ms"]), interval)
        values["call_count"] += _stage_call_count(stage)
        values["failure_count"] += _stage_failure_count(stage)
        values["guard_hit_count"] += _numeric(stage.metadata, "guard_hit_count")
        values["repair_attempt_count"] += int(
            bool(stage.metadata.get("repair_attempted", False))
        )
        values["stale_drop_count"] += int(stage.stage == "stale_generation_drop")
        values["send_success_count"] += int(stage.stage == "send_success")
        values["send_failure_count"] += int(stage.stage == "send_failed")

    phases = tuple(
        PhaseMetric(
            phase=phase,
            stage_count=int(values["stage_count"]),
            duration_ms=round(float(values["duration_ms"]), 3),
            max_interval_ms=round(float(values["max_interval_ms"]), 3),
            call_count=int(values["call_count"]),
            failure_count=int(values["failure_count"]),
            guard_hit_count=int(values["guard_hit_count"]),
            repair_attempt_count=int(values["repair_attempt_count"]),
            stale_drop_count=int(values["stale_drop_count"]),
            send_success_count=int(values["send_success_count"]),
            send_failure_count=int(values["send_failure_count"]),
        )
        for phase, values in accumulators.items()
    )
    return PipelineMetricsSnapshot(
        trace_id=context.trace_id,
        total_elapsed_ms=(context.stages[-1].elapsed_ms if context.stages else 0.0),
        stage_count=len(context.stages),
        terminal_outcome=_terminal_outcome(context.stages),
        phases=phases,
    )


def store_pipeline_metrics(event: Any) -> PipelineMetricsSnapshot:
    snapshot = summarize_pipeline_metrics(get_trace_context(event))
    event.set_extra(PIPELINE_METRICS_EXTRA, snapshot)
    return snapshot
