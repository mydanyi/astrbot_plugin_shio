import json
import unittest

from astrbot_plugin_shio.core.pipeline_metrics import (
    PIPELINE_METRICS_EXTRA,
    PipelinePhase,
    store_pipeline_metrics,
    summarize_pipeline_metrics,
)
from astrbot_plugin_shio.core.pipeline_trace import (
    PIPELINE_TRACE_CONTEXT_EXTRA,
    TraceContext,
    TraceStage,
)


def stage(name, elapsed, **metadata):
    return TraceStage(name, elapsed, metadata)


class FakeEvent:
    def __init__(self, context):
        self.extras = {PIPELINE_TRACE_CONTEXT_EXTRA: context}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class PipelineMetricsTests(unittest.TestCase):
    def test_success_trace_is_split_into_actionable_phase_metrics(self):
        context = TraceContext(
            trace_id="0123456789abcdef0123456789abcdef",
            started_at=0.0,
            stages=[
                stage("input", 0.2),
                stage("target", 1.0),
                stage("context", 4.0),
                stage("plan", 10.0),
                stage(
                    "tool_result",
                    15.0,
                    typed_tool_result_count=2,
                    typed_tool_result_success_count=2,
                    typed_tool_result_orphan_count=0,
                ),
                stage("raw_reply", 20.0),
                stage(
                    "guard",
                    22.0,
                    guard_hit_count=1,
                    repair_attempted=True,
                    called_tool_count=2,
                ),
                stage("final_reply", 24.0),
                stage("bubble_planned", 25.0),
                stage("send_attempt", 26.0),
                stage("send_success", 30.0),
            ],
        )

        snapshot = summarize_pipeline_metrics(context)

        self.assertEqual(snapshot.terminal_outcome, "sent")
        self.assertEqual(snapshot.stage_count, 11)
        self.assertEqual(snapshot.phase(PipelinePhase.TARGET).duration_ms, 0.8)
        self.assertEqual(snapshot.phase(PipelinePhase.CONTEXT).duration_ms, 3.0)
        self.assertEqual(snapshot.phase(PipelinePhase.PLANNER).duration_ms, 6.0)
        self.assertEqual(snapshot.phase(PipelinePhase.TOOL).call_count, 2)
        self.assertEqual(snapshot.phase(PipelinePhase.TOOL).failure_count, 0)
        self.assertEqual(snapshot.phase(PipelinePhase.GUARD).guard_hit_count, 1)
        self.assertEqual(snapshot.phase(PipelinePhase.GUARD).repair_attempt_count, 1)
        self.assertEqual(snapshot.phase(PipelinePhase.SEND).send_success_count, 1)

    def test_tool_failure_guard_repair_stale_and_send_failure_are_distinct(self):
        tool = summarize_pipeline_metrics(
            TraceContext(
                "a" * 32,
                0.0,
                [
                    stage("input", 0.1),
                    stage(
                        "tool_result",
                        1.0,
                        typed_tool_result_count=2,
                        typed_tool_result_success_count=1,
                        typed_tool_result_orphan_count=1,
                    ),
                    stage("send_failed", 2.0),
                ],
            )
        )
        self.assertEqual(tool.phase(PipelinePhase.TOOL).failure_count, 1)
        self.assertEqual(tool.terminal_outcome, "send_failed")
        self.assertEqual(tool.phase(PipelinePhase.SEND).send_failure_count, 1)

        stale = summarize_pipeline_metrics(
            TraceContext(
                "b" * 32,
                0.0,
                [stage("input", 0.1), stage("stale_generation_drop", 4.0)],
            )
        )
        self.assertEqual(stale.terminal_outcome, "stale_drop")
        self.assertEqual(stale.phase(PipelinePhase.CONCURRENCY).stale_drop_count, 1)

    def test_metrics_snapshot_is_stored_and_contains_no_stage_content(self):
        secret = "private chat body"
        context = TraceContext(
            "c" * 32,
            0.0,
            [
                stage("input", 0.1, message_fingerprint="1" * 16),
                stage("final_reply", 2.0, content_fingerprint="2" * 16),
            ],
        )
        event = FakeEvent(context)

        snapshot = store_pipeline_metrics(event)

        self.assertIs(event.get_extra(PIPELINE_METRICS_EXTRA), snapshot)
        rendered = json.dumps(snapshot.trace_metadata(), ensure_ascii=False)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("message_fingerprint", rendered)
        self.assertNotIn("content_fingerprint", rendered)
        self.assertEqual(snapshot.terminal_outcome, "ready_to_send")

    def test_final_send_blocked_is_not_reported_as_ready_to_send(self):
        snapshot = summarize_pipeline_metrics(
            TraceContext(
                "d" * 32,
                0.0,
                [
                    stage("input", 0.1),
                    stage("final_reply", 1.0),
                    stage("final_send_blocked", 1.2, reason_code="guard.protocol"),
                ],
            )
        )
        self.assertEqual(snapshot.terminal_outcome, "send_blocked")
        self.assertEqual(snapshot.phase(PipelinePhase.SEND).failure_count, 1)


if __name__ == "__main__":
    unittest.main()
