import json
import re
import unittest

from astrbot_plugin_shio.core.pipeline_trace import (
    PIPELINE_TRACE_CONTEXT_EXTRA,
    PIPELINE_TRACE_EXTRA,
    TraceContext,
    get_pipeline_trace,
    get_trace_context,
    get_trace_id,
    record_pipeline_stage,
    start_pipeline_trace,
)


class FakeEvent:
    def __init__(self):
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class PipelineTraceTests(unittest.TestCase):
    @staticmethod
    def start(event, *, message="不能出现在 trace 的正文", sender="real-user-42"):
        start_pipeline_trace(
            event,
            current_message=message,
            sender_id=sender,
            group_id="real-group-7",
            chat_type="group",
            is_owner=False,
            envelope_metadata={"source_kind": "direct_event"},
        )

    def test_each_event_gets_a_distinct_random_typed_trace_context(self):
        first = FakeEvent()
        second = FakeEvent()
        self.start(first)
        self.start(second)

        first_context = get_trace_context(first)
        self.assertIsInstance(first_context, TraceContext)
        self.assertIs(
            first.get_extra(PIPELINE_TRACE_CONTEXT_EXTRA),
            first_context,
        )
        self.assertRegex(get_trace_id(first), re.compile(r"^[0-9a-f]{32}$"))
        self.assertRegex(get_trace_id(second), re.compile(r"^[0-9a-f]{32}$"))
        self.assertNotEqual(get_trace_id(first), get_trace_id(second))

    def test_legacy_snapshot_tracks_typed_stages_without_plaintext_or_real_ids(self):
        event = FakeEvent()
        self.start(event)
        record_pipeline_stage(
            event,
            "send_success",
            automatic=True,
            visible_chars=12,
            sender_id="real-user-42",
            prompt="secret prompt",
            authorization="Bearer private",
        )

        snapshot = get_pipeline_trace(event)
        self.assertEqual(snapshot, event.get_extra(PIPELINE_TRACE_EXTRA))
        self.assertEqual(snapshot["trace_id"], get_trace_id(event))
        self.assertEqual(
            [stage["stage"] for stage in snapshot["stages"]],
            ["input", "send_success"],
        )
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("不能出现在 trace 的正文", serialized)
        self.assertNotIn("real-user-42", serialized)
        self.assertNotIn("real-group-7", serialized)
        self.assertNotIn("secret prompt", serialized)
        self.assertNotIn("Bearer private", serialized)
        self.assertIn("message_fingerprint", serialized)
        self.assertIn("sender_fingerprint", serialized)


if __name__ == "__main__":
    unittest.main()
