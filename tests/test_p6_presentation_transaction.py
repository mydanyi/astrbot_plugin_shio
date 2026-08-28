from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeResponse,
        FakeStarTools,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeResponse,
        FakeStarTools,
        main,
    )

from astrbot_plugin_shio.core.meme_presentation import (
    MemeExecutionAuthority,
    MemeManagerConformanceCollector,
)
from astrbot_plugin_shio.tests.test_p6_meme_presentation_contract import (
    _FakeMemeManager,
    _FakeMetadata,
    _test_profile,
)


class P6PresentationTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.manager = _FakeMemeManager()
        self.metadata = _FakeMetadata(self.manager)
        self.context = FakeContext(FakeProvider([]))
        original_get_registered = self.context.get_registered_star
        self.context.get_registered_star = (
            lambda name: (
                self.metadata
                if name == "meme_manager"
                else original_get_registered(name)
            )
        )
        self.context.get_all_stars = lambda: [self.metadata]
        self.plugin = main.ShioPlugin(
            self.context,
            {
                "persona_name": "亚托莉",
                "natural_name_wake_aliases": ["亚托莉", "萝卜子"],
                "prefer_livingmemory_group_history": False,
                "bubble_interval_min_ms": 0,
                "bubble_interval_max_ms": 0,
            },
        )
        collector = MemeManagerConformanceCollector(_test_profile())
        self.plugin.meme_manager_conformance = collector
        self.plugin.meme_execution_authority = MemeExecutionAuthority(
            planned_action_authority=self.plugin.planned_action_authority,
            expression_intent_authority=self.plugin.expression_intent_authority,
            generation_registry=self.plugin.generation_epochs,
            conformance_collector=collector,
        )

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()
        self.temp.cleanup()

    async def _prepare(self, output: str, *, message_id: str):
        event = FakeEvent(
            "peer-a",
            "萝卜子今天也太可爱了哈哈",
            group_id="p6-presentation-transaction",
        )
        event.message_id = message_id
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=200.0):
            self.plugin.admit_ingress_event(event)
            await self.plugin.enforce_agent_permission(event, request)
            await self.plugin.build_persona_reply(event, request)
        response = FakeResponse(output)
        await self.plugin.guard_persona_reply(event, response)
        event._result = types.SimpleNamespace(
            chain=[types.SimpleNamespace(text=response.completion_text)],
            is_llm_result=lambda: True,
        )
        return event, response

    async def test_text_terminal_never_duplicates_manager_hook_selection(self):
        event, response = await self._prepare(
            "哼哼，这次知道我的厉害了吧。\n被你夸，我还是有一点开心的。",
            message_id="p6-presentation-success",
        )
        await self.plugin.dispatch_chat_bubbles(event)
        self.assertEqual(event.sent, ["哼哼，这次知道我的厉害了吧。"])
        self.assertEqual(self.manager.prepare_calls, [])
        self.assertIsNone(event.get_extra(main.SHIO_MEME_PRESENTATION_RECEIPT))
        tracker = event.get_extra(main.SHIO_SEND_OBSERVATION)
        presentation = event.get_extra(main.SHIO_PRESENTATION_HANDOFF)
        with self.assertRaisesRegex(ValueError, "not_successful"):
            self.plugin.send_receipts.issue_presentation_send_terminal_evidence(
                presentation,
                internal_reply_id=tracker.internal_reply_id,
            )

        with patch("astrbot_plugin_shio.main.structured_log") as log_mock:
            await self.plugin.confirm_automatic_send_observation(event)
        receipt = event.get_extra(main.SHIO_MEME_PRESENTATION_RECEIPT)
        self.assertIsNone(receipt)
        self.assertEqual(self.manager.prepare_calls, [])
        self.assertEqual(self.manager.send_calls, [])
        terminal_logs = [
            call
            for call in log_mock.call_args_list
            if len(call.args) >= 3 and call.args[2] == "meme.presentation_terminal"
        ]
        self.assertEqual(terminal_logs, [])
        self.assertNotIn(event.message, repr(terminal_logs))

        await self.plugin.confirm_automatic_send_observation(event)
        self.assertEqual(self.manager.prepare_calls, [])
        self.assertEqual(self.manager.send_calls, [])
        sent = self.plugin.send_receipts.sent_reply_record(tracker.internal_reply_id)
        self.assertEqual(
            tuple(segment.visible_text for segment in sent.successful_segments),
            tuple(response.completion_text.splitlines()),
        )
        outbound = [
            record.content
            for record in self.plugin.ledger.records(
                main.ensure_turn_envelope(event).scope_key
            )
            if record.source_kind is main.LedgerSourceKind.OUTBOUND
        ]
        self.assertEqual(outbound, list(response.completion_text.splitlines()))
        self.assertNotIn("&&开心&&", repr(outbound))

    async def test_failed_manual_segment_is_not_retried_automatically(self):
        event, _ = await self._prepare(
            "第一句话说完了。\n第二句话也完整。",
            message_id="p6-presentation-failed",
        )

        async def fail_send(_result):
            raise RuntimeError("SENSITIVE-SEND-FAILURE")

        event.send = fail_send
        await self.plugin.dispatch_chat_bubbles(event)
        self.assertEqual(event._result.chain, [])
        tracker = event.get_extra(main.SHIO_SEND_OBSERVATION)
        record = self.plugin.send_receipts.get_reply(tracker.internal_reply_id)
        self.assertEqual([segment.status.value for segment in record.segments], [
            "failed",
            "planned",
        ])
        await self.plugin.confirm_automatic_send_observation(event)
        self.assertEqual(self.manager.prepare_calls, [])
        self.assertIsNone(event.get_extra(main.SHIO_MEME_PRESENTATION_RECEIPT))

    async def test_stale_after_text_terminal_never_sends_complement(self):
        event, _ = await self._prepare(
            "今天确实挺开心的。",
            message_id="p6-presentation-stale",
        )
        await self.plugin.dispatch_chat_bubbles(event)
        replacement = FakeEvent(
            "peer-a",
            "萝卜子，下一轮",
            group_id=event.group_id,
        )
        replacement.message_id = "p6-presentation-stale-replacement"
        self.plugin.admit_ingress_event(replacement)

        await self.plugin.confirm_automatic_send_observation(event)
        receipt = event.get_extra(main.SHIO_MEME_PRESENTATION_RECEIPT)
        self.assertIsNone(receipt)
        self.assertEqual(self.manager.prepare_calls, [])
        self.assertEqual(self.manager.send_calls, [])


if __name__ == "__main__":
    unittest.main()
