from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        main,
    )

from astrbot_plugin_shio.core.contracts import (
    ActionKind,
    ContractViolation,
    ParticipationLevel,
)
from astrbot_plugin_shio.core.participation_cadence import (
    ParticipationCadenceDecision,
)


_SEMANTIC_REPLY = (
    '{"decision":"REPLY","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"natural_continuation",'
    '"confidence":0.9}'
)
_SEMANTIC_NO_ACTION = (
    '{"decision":"NO_ACTION","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"not_helpful",'
    '"confidence":0.9}'
)


class P5ParticipationCadenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.provider = FakeProvider([])
        self.plugin = main.ShioPlugin(
            FakeContext(self.provider),
            {
                "persona_name": "亚托莉",
                "natural_name_wake_aliases": ["亚托莉", "萝卜子"],
                "prefer_livingmemory_group_history": False,
                "natural_group_participation_enabled": True,
                "natural_group_participation_allowlist": ["p5-cadence-group"],
                "natural_group_participation_min_context_messages": 2,
            },
        )
        self.turn = 0
        prior = FakeEvent(
            "peer-b",
            "刚才的话题还没说完",
            group_id="p5-cadence-group",
        )
        prior.message_id = "p5-cadence-prior"
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=0.0):
            self.plugin.admit_ingress_event(prior)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _event(self, message: str, *, direct: bool = False) -> FakeEvent:
        self.turn += 1
        event = FakeEvent("peer-a", message, group_id="p5-cadence-group")
        event.message_id = f"p5-cadence-{self.turn}"
        event.is_at_or_wake_command = direct
        return event

    async def _admit(
        self,
        message: str,
        *,
        now: float,
        direct: bool = False,
        semantic_output: str = _SEMANTIC_REPLY,
    ):
        event = self._event(message, direct=direct)
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=now):
            result = self.plugin.admit_ingress_event(event)
        self.assertIsNotNone(result)
        if event.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_REQUEST) is not None:
            self.provider.outputs.append(semantic_output)
            await self.plugin._resolve_participation_semantic(event)
        cadence = event.get_extra(main.SHIO_PARTICIPATION_CADENCE)
        self.assertIs(type(cadence), ParticipationCadenceDecision)
        return event, cadence

    async def test_join_cooldown_and_window_limit_prevent_consecutive_interruption(self):
        first_event, first = await self._admit("大家今晚吃什么？", now=100.0)
        self.assertIs(first.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertTrue(first_event.is_at_or_wake_command)

        blocked_event, blocked = await self._admit("大家晚饭怎么选？", now=110.0)
        self.assertIs(blocked.base.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertIs(blocked.decision.level, ParticipationLevel.WAIT)
        self.assertGreater(blocked.decision.cooldown_remaining_s, 0.0)
        self.assertFalse(blocked_event.is_at_or_wake_command)

        second_event, second = await self._admit("大家吃什么料理？", now=146.0)
        self.assertIs(second.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertTrue(second_event.is_at_or_wake_command)

        limited_event, limited = await self._admit("大家晚餐吃什么？", now=192.0)
        self.assertIs(limited.decision.level, ParticipationLevel.WAIT)
        self.assertIn("participation_window_limit", limited.decision.reason_codes)
        self.assertFalse(limited_event.is_at_or_wake_command)

        resumed_event, resumed = await self._admit("大家晚餐怎么选？", now=401.0)
        self.assertIs(resumed.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertTrue(resumed_event.is_at_or_wake_command)

    async def test_no_action_backoff_blocks_candidate_but_never_direct(self):
        _, no_action = await self._admit(
            "今天下雨了",
            now=10.0,
            semantic_output=_SEMANTIC_NO_ACTION,
        )
        self.assertIs(no_action.decision.level, ParticipationLevel.NO_ACTION)

        candidate_event, candidate = await self._admit(
            "我觉得萝卜子这个称呼很有趣",
            now=11.0,
        )
        self.assertIs(candidate.base.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertIs(candidate.decision.level, ParticipationLevel.WAIT)
        self.assertIn("participation_no_action_backoff", candidate.decision.reason_codes)
        self.assertFalse(candidate_event.is_at_or_wake_command)

        direct_event, direct = await self._admit(
            "萝卜子，你在吗？",
            now=11.5,
            direct=True,
        )
        self.assertIs(direct.decision.level, ParticipationLevel.MUST_REPLY)
        self.assertEqual(direct.decision.cooldown_remaining_s, 0.0)
        self.assertTrue(direct_event.is_at_or_wake_command)

        resumed_event, resumed = await self._admit(
            "我觉得萝卜子这个称呼很有趣",
            now=13.0,
        )
        self.assertIs(resumed.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertTrue(resumed_event.is_at_or_wake_command)

    async def test_wait_is_terminal_zero_model_zero_tool(self):
        await self._admit("大家今晚吃什么？", now=20.0)
        semantic_calls_before_wait = len(self.provider.calls)
        event, cadence = await self._admit("大家晚饭吃什么？", now=21.0)
        request = FakeRequest(event.message)

        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)

        planned = event.get_extra(main.SHIO_PLANNED_ACTION)
        self.assertIs(cadence.decision.level, ParticipationLevel.WAIT)
        self.assertIs(planned.kind, ActionKind.WAIT)
        self.assertEqual(request.system_prompt, "")
        self.assertEqual(request.prompt, "")
        self.assertEqual(tuple(request.func_tool.tools), ())
        self.assertEqual(semantic_calls_before_wait, 1)
        self.assertEqual(len(self.provider.calls), semantic_calls_before_wait)

    async def test_cadence_copy_cross_runtime_and_mutation_fail_closed(self):
        event, cadence = await self._admit("大家今晚吃什么？", now=30.0)
        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            self.plugin.participation_cadence_authority.inspect(copy.copy(cadence))

        original = cadence.decision.level
        object.__setattr__(cadence.decision, "level", ParticipationLevel.MUST_REPLY)
        with self.assertRaisesRegex(ContractViolation, "corrupt"):
            self.plugin.participation_cadence_authority.inspect(cadence)
        object.__setattr__(cadence.decision, "level", original)

        other = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            other.participation_cadence_authority.inspect(cadence)
        self.assertIs(
            self.plugin.participation_cadence_authority.inspect(cadence),
            cadence,
        )
        self.assertIsNotNone(event.get_extra(main.SHIO_PARTICIPATION_ASSESSMENT))

    def test_temporal_state_is_bounded_and_trace_is_content_free(self):
        for index in range(384):
            event = FakeEvent(
                f"peer-{index}",
                "今天下雨了",
                group_id=f"p5-cadence-bounded-{index}",
            )
            event.message_id = f"p5-cadence-bounded-{index}"
            with patch(
                "astrbot_plugin_shio.main.time.monotonic",
                return_value=float(index + 1),
            ):
                self.plugin.admit_ingress_event(event)
        metadata = self.plugin.participation_cadence_authority.trace_metadata()
        self.assertLessEqual(metadata["participation_cadence_subject_count"], 256)
        rendered = repr(metadata)
        self.assertNotIn("peer-383", rendered)
        self.assertNotIn("今天下雨了", rendered)


if __name__ == "__main__":
    unittest.main()
