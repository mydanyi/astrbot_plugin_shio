import math
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.performance_metrics import (
    LatencyKind,
    ModelCallKind,
    PerformanceMetricsError,
    PerformanceWindow,
)


class PerformanceWindowTests(unittest.TestCase):
    def test_bounded_nearest_rank_p50_p95_and_call_counts(self):
        window = PerformanceWindow(max_samples_per_kind=4)
        for value in (10.0, 20.0, 30.0, 40.0, 50.0):
            window.observe_latency(LatencyKind.LOCAL_ORCHESTRATION, value)
        window.record_model_call(ModelCallKind.PRIMARY)
        window.record_model_call(ModelCallKind.REPAIR, failed=True)
        window.record_model_call(ModelCallKind.PROACTIVE)

        snapshot = window.snapshot()
        local = snapshot.latency(LatencyKind.LOCAL_ORCHESTRATION)
        self.assertEqual(local.sample_count, 4)
        self.assertEqual(local.p50_ms, 30.0)
        self.assertEqual(local.p95_ms, 50.0)
        self.assertEqual(local.max_ms, 50.0)
        self.assertEqual(snapshot.primary_call_count, 1)
        self.assertEqual(snapshot.repair_call_count, 1)
        self.assertEqual(snapshot.proactive_call_count, 1)
        self.assertEqual(snapshot.model_failure_count, 1)

    def test_all_required_latency_kinds_have_content_free_snapshot(self):
        window = PerformanceWindow(max_samples_per_kind=8)
        for index, kind in enumerate(LatencyKind, start=1):
            window.observe_latency(kind, float(index))
        metadata = window.snapshot().trace_metadata()
        rendered = repr(metadata)
        for kind in LatencyKind:
            self.assertEqual(window.snapshot().latency(kind).sample_count, 1)
        for marker in ("message-secret", "sender-secret", "prompt-secret"):
            self.assertNotIn(marker, rendered)
        self.assertNotIn("trace_id", metadata)
        self.assertNotIn("scope", rendered.lower())

    def test_fresh_process_window_starts_empty(self):
        previous = PerformanceWindow(max_samples_per_kind=8)
        previous.observe_latency(LatencyKind.FULL_REPLY, 12.0)
        previous.record_model_call(ModelCallKind.PRIMARY)

        reopened = PerformanceWindow(max_samples_per_kind=8)
        snapshot = reopened.snapshot()
        self.assertEqual(snapshot.latency(LatencyKind.FULL_REPLY).sample_count, 0)
        self.assertEqual(snapshot.primary_call_count, 0)
        self.assertEqual(snapshot.model_failure_count, 0)

    def test_invalid_numeric_and_enum_inputs_fail_closed(self):
        window = PerformanceWindow(max_samples_per_kind=8)
        for invalid in (True, 1, -1.0, math.inf, math.nan, "1"):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(PerformanceMetricsError):
                    window.observe_latency(LatencyKind.PRIMARY_PROVIDER, invalid)
        with self.assertRaises(PerformanceMetricsError):
            window.observe_latency("primary_provider", 1.0)  # type: ignore[arg-type]
        with self.assertRaises(PerformanceMetricsError):
            window.record_model_call("primary")  # type: ignore[arg-type]
        with self.assertRaises(PerformanceMetricsError):
            PerformanceWindow(max_samples_per_kind=True)

    def test_concurrent_observation_remains_bounded(self):
        import threading

        window = PerformanceWindow(max_samples_per_kind=16)

        def write(offset: int) -> None:
            for index in range(100):
                window.observe_latency(
                    LatencyKind.INFERENCE_QUEUE,
                    float(offset + index),
                )
                window.record_model_call(ModelCallKind.PRIMARY)

        threads = [threading.Thread(target=write, args=(1000 * index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        snapshot = window.snapshot()
        self.assertEqual(snapshot.latency(LatencyKind.INFERENCE_QUEUE).sample_count, 16)
        self.assertEqual(snapshot.primary_call_count, 800)
        self.assertTrue(snapshot.window_bounded)

    def test_main_records_primary_repair_proactive_and_send_metrics(self):
        source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("PerformanceWindow", source)
        self.assertIn("LatencyKind.LOCAL_ORCHESTRATION", source)
        self.assertIn("LatencyKind.INFERENCE_QUEUE", source)
        self.assertIn("LatencyKind.PRIMARY_PROVIDER", source)
        self.assertIn("LatencyKind.REPAIR_PROVIDER", source)
        self.assertIn("LatencyKind.PROACTIVE_PROVIDER", source)
        self.assertIn("LatencyKind.FIRST_BUBBLE", source)
        self.assertIn("LatencyKind.FULL_REPLY", source)
        self.assertIn("ModelCallKind.PRIMARY", source)
        self.assertIn("ModelCallKind.REPAIR", source)
        self.assertIn("ModelCallKind.PROACTIVE", source)


if __name__ == "__main__":
    unittest.main()
