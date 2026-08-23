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
    ExpressionIntent,
    ExpressionModality,
    ParticipationLevel,
)
from astrbot_plugin_shio.core.participation_reaction import (
    ParticipationReactionDecision,
)


class P5ParticipationReactionTests(unittest.IsolatedAsyncioTestCase):
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
            },
        )
        self.turn = 0

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _event(self, message: str, *, direct: bool = False) -> FakeEvent:
        self.turn += 1
        event = FakeEvent("peer-a", message, group_id=f"p5-react-{self.turn}")
        event.message_id = f"p5-react-message-{self.turn}"
        event.is_at_or_wake_command = direct
        return event

    def _admit(self, message: str, *, direct: bool = False):
        event = self._event(message, direct=direct)
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=100.0):
            result = self.plugin.admit_ingress_event(event)
        self.assertIsNotNone(result)
        reaction = event.get_extra(main.SHIO_PARTICIPATION_REACTION)
        self.assertIs(type(reaction), ParticipationReactionDecision)
        return event, reaction

    async def test_light_about_self_can_react_without_text_model_or_tool(self):
        event, reaction = self._admit("我觉得萝卜子这个表情好可爱哈哈")
        self.assertIs(reaction.decision.level, ParticipationLevel.REACT_ONLY)
        self.assertEqual(reaction.expression_intent, "light_reaction")
        self.assertTrue(event.is_at_or_wake_command)

        request = FakeRequest(event.message)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)

        planned = event.get_extra(main.SHIO_PLANNED_ACTION)
        expression = event.get_extra(main.SHIO_EXPRESSION_INTENT)
        self.assertIs(planned.kind, ActionKind.REACT)
        self.assertIs(type(expression), ExpressionIntent)
        self.assertIs(expression.modality, ExpressionModality.REACTION)
        self.assertEqual(expression.social_act, "light_reaction")
        self.assertEqual(expression.max_bubbles, 0)
        self.assertEqual(request.system_prompt, "")
        self.assertEqual(request.prompt, "")
        self.assertEqual(tuple(request.func_tool.tools), ())
        self.assertEqual(self.provider.calls, [])

    async def test_direct_serious_open_group_and_wait_never_become_react(self):
        cases = (
            ("direct", "萝卜子你好可爱哈哈", True, ParticipationLevel.MUST_REPLY),
            (
                "serious",
                "我觉得萝卜子现在出了严重错误，需要认真修复",
                False,
                ParticipationLevel.MAY_JOIN,
            ),
            ("open-group", "大家今晚吃什么？", False, ParticipationLevel.MAY_JOIN),
            ("unrelated", "今天下雨了", False, ParticipationLevel.NO_ACTION),
        )
        for name, message, direct, expected in cases:
            with self.subTest(name=name):
                FakeStarTools.data_dir = Path(self.temp.name) / f"case-{name}"
                plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {
                    "persona_name": "亚托莉",
                    "natural_name_wake_aliases": ["亚托莉", "萝卜子"],
                    "prefer_livingmemory_group_history": False,
                })
                event = FakeEvent("peer-a", message, group_id=f"p5-react-{name}")
                event.message_id = f"p5-react-{name}"
                event.is_at_or_wake_command = direct
                with patch("astrbot_plugin_shio.main.time.monotonic", return_value=100.0):
                    plugin.admit_ingress_event(event)
                reaction = event.get_extra(main.SHIO_PARTICIPATION_REACTION)
                self.assertIs(reaction.decision.level, expected)
                self.assertNotEqual(reaction.decision.level, ParticipationLevel.REACT_ONLY)

    async def test_owner_action_route_cannot_be_downgraded_to_react(self):
        FakeStarTools.data_dir = Path(self.temp.name) / "owner-action"
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "enabled": True,
                "owner_ids": ["owner"],
                "persona_name": "亚托莉",
                "prefer_livingmemory_group_history": False,
            },
        )
        event = FakeEvent(
            "owner",
            "请读取 path=/srv/safe/readme.txt",
            group_id="",
        )
        event.message_id = "p5-react-owner-action"
        event.unified_msg_origin = "aiocqhttp:FriendMessage:owner"
        request = FakeRequest(event.message)
        try:
            await plugin.enforce_agent_permission(event, request)
            await plugin.build_persona_reply(event, request)
            reaction = event.get_extra(main.SHIO_PARTICIPATION_REACTION)
            planned = event.get_extra(main.SHIO_PLANNED_ACTION)
            self.assertIs(reaction.decision.level, ParticipationLevel.MUST_REPLY)
            self.assertIs(planned.kind, ActionKind.EXECUTE_ACTION)
            self.assertIsNot(planned.kind, ActionKind.REACT)
            expression = event.get_extra(main.SHIO_EXPRESSION_INTENT)
            self.assertIs(type(expression), ExpressionIntent)
            self.assertIs(expression.modality, ExpressionModality.TEXT)
        finally:
            await plugin.terminate()

    def test_exact_cadence_only_copy_cross_runtime_and_mutation_fail_closed(self):
        _, reaction = self._admit("我觉得萝卜子这个表情好可爱哈哈")
        authority = self.plugin.participation_reaction_authority
        self.assertIs(authority.inspect(reaction), reaction)

        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            authority.inspect(copy.copy(reaction))

        original = reaction.expression_intent
        object.__setattr__(reaction, "expression_intent", "forged_reaction")
        with self.assertRaisesRegex(ContractViolation, "corrupt"):
            authority.inspect(reaction)
        object.__setattr__(reaction, "expression_intent", original)

        FakeStarTools.data_dir = Path(self.temp.name) / "cross-runtime"
        other = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            other.participation_reaction_authority.inspect(reaction)
        self.assertIs(authority.inspect(reaction), reaction)

    def test_reaction_authority_has_no_provider_tool_meme_or_owner_input(self):
        import inspect

        parameters = inspect.signature(
            self.plugin.participation_reaction_authority.issue
        ).parameters
        self.assertEqual(tuple(parameters), ("cadence", "current_message"))
        source = (
            Path(main.__file__).resolve().parent
            / "core"
            / "participation_reaction.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "MemeManager",
            "search_memes",
            "execute_sealed_acquisition",
            "OwnerAction",
            "provider",
        ):
            self.assertNotIn(forbidden, source)

    def test_reaction_ledger_is_bounded_and_trace_is_content_free(self):
        for index in range(384):
            plugin = self.plugin
            event = FakeEvent(
                f"peer-{index}",
                "今天下雨了",
                group_id=f"p5-react-bounded-{index}",
            )
            event.message_id = f"p5-react-bounded-{index}"
            with patch(
                "astrbot_plugin_shio.main.time.monotonic",
                return_value=float(index + 1),
            ):
                plugin.admit_ingress_event(event)
        metadata = self.plugin.participation_reaction_authority.trace_metadata()
        self.assertLessEqual(metadata["participation_reaction_decision_count"], 1024)
        rendered = repr(metadata)
        self.assertNotIn("peer-383", rendered)
        self.assertNotIn("今天下雨了", rendered)


if __name__ == "__main__":
    unittest.main()
