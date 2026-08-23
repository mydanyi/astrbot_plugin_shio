from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

try:
    # unittest discovery imports sibling files as top-level modules.  Reuse the
    # same module so FakeEvent and the AstrBot stubs share one main registry.
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        FakeTool,
        FakeToolSet,
        fake_tool_call_result_with_content,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        FakeTool,
        FakeToolSet,
        fake_tool_call_result_with_content,
        main,
    )

from astrbot_plugin_shio.core.action_planner import PlannedAction
from astrbot_plugin_shio.core.affect import AffectAppraisal
from astrbot_plugin_shio.core.contracts import (
    ActionKind,
    AddressKind,
    ContentIntent,
    ExpressionIntent,
)
from astrbot_plugin_shio.core.persona_expression import PersonaExpressionPlan
from astrbot_plugin_shio.core.product_trace import ProductStage, ProductTrace
from astrbot_plugin_shio.core.reply_composer import ReplyComposerRequest
from astrbot_plugin_shio.core.tool_broker import AcquisitionRequest


ROOT = Path(main.__file__).parent
ANYSEARCH_SOURCE = "data.plugins.astrbot_plugin_anysearch.main"
LEGACY_TOOL_RESULTS_EXTRA = "_shio_typed_tool_results"


class At:
    type = "at"

    def __init__(self, qq: str) -> None:
        self.qq = qq


def _anysearch_tool() -> FakeTool:
    return FakeTool(
        "anysearch_search",
        "Public web search",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        module=ANYSEARCH_SOURCE,
    )


def _tool_names(request: FakeRequest) -> list[str]:
    return [
        str(getattr(tool, "name", "") or "")
        for tool in getattr(request.func_tool, "tools", ())
    ]


class P3AtomicHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.runtime_count = 0

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _run_turn(
        self,
        *,
        sender_id: str,
        message: str,
        group_id: str,
        owner_ids: tuple[str, ...] = (),
        tools: tuple[FakeTool, ...] = (),
        direct: bool = True,
        components: tuple[object, ...] = (),
    ):
        self.runtime_count += 1
        FakeStarTools.data_dir = Path(self.temp.name) / f"runtime-{self.runtime_count}"
        provider = FakeProvider([])
        plugin = main.ShioPlugin(
            FakeContext(provider, global_tools=list(tools)),
            {
                "owner_ids": list(owner_ids),
                "persona_name": "亚托莉",
                "guest_allowed_tools": ["anysearch_search"],
                "prefer_livingmemory_group_history": False,
            },
        )
        event = FakeEvent(sender_id, message, group_id=group_id)
        event.message_id = f"p3-hot-{sender_id}-{abs(hash((message, group_id))) % 100000}"
        event.get_messages = lambda: list(components)
        if group_id:
            event.is_at_or_wake_command = direct
        else:
            event.unified_msg_origin = f"aiocqhttp:FriendMessage:{sender_id}"
        request = FakeRequest(message)
        request.func_tool = FakeToolSet(list(tools))

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        return plugin, event, request, provider

    async def test_private_and_group_direct_turns_share_one_reply_binding(self):
        cases = (
            ("owner-private", "owner", "", "我来找你聊聊天", ("owner",)),
            ("guest-group", "peer-a", "group-direct", "你今天开心吗？", ()),
        )
        for name, sender_id, group_id, message, owner_ids in cases:
            with self.subTest(case=name):
                _, event, request, provider = await self._run_turn(
                    sender_id=sender_id,
                    message=message,
                    group_id=group_id,
                    owner_ids=owner_ids,
                    tools=(_anysearch_tool(), FakeTool("shell_exec")),
                )

                action = event.get_extra(main.SHIO_PLANNED_ACTION)
                content = event.get_extra(main.SHIO_CONTENT_INTENT)
                expression = event.get_extra(main.SHIO_EXPRESSION_INTENT)
                composer = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
                self.assertIsInstance(action, PlannedAction)
                self.assertIs(action.kind, ActionKind.REPLY)
                self.assertIsInstance(content, ContentIntent)
                self.assertIsInstance(expression, ExpressionIntent)
                self.assertEqual(action.binding, content.binding)
                self.assertEqual(action.binding, expression.binding)
                self.assertEqual(action.action.reply_target, expression.reply_target)
                self.assertIsInstance(composer, ReplyComposerRequest)
                self.assertEqual(composer.call_budget.total_model_calls, 1)
                self.assertEqual(provider.calls, [])
                self.assertEqual(_tool_names(request), [])

    async def test_non_direct_group_addresses_follow_current_participation_policy(self):
        cases = (
            (
                "about-self",
                "我觉得笨蛋萝卜子这个称呼很有趣",
                AddressKind.ABOUT_SELF,
                (),
                ActionKind.REPLY,
            ),
            (
                "open-group",
                "大家今晚吃什么？",
                AddressKind.OPEN_GROUP,
                (),
                ActionKind.REPLY,
            ),
            (
                "other-person",
                "小林你怎么看？",
                AddressKind.OTHER_PERSON,
                (At("peer-b"),),
                ActionKind.NO_ACTION,
            ),
        )
        for name, message, expected_address, components, expected_action in cases:
            with self.subTest(case=name):
                _, event, request, provider = await self._run_turn(
                    sender_id="peer-a",
                    message=message,
                    group_id="group-nondirect",
                    direct=False,
                    components=components,
                    tools=(_anysearch_tool(),),
                )

                address = event.get_extra(main.SHIO_ADDRESS_DECISION)
                action = event.get_extra(main.SHIO_PLANNED_ACTION)
                self.assertIs(address.kind, expected_address)
                self.assertIsInstance(action, PlannedAction)
                self.assertIs(action.kind, expected_action)
                if expected_action is ActionKind.NO_ACTION:
                    self.assertIsNone(
                        event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
                    )
                    self.assertTrue(getattr(event, "stopped", False))
                    self.assertEqual(request.system_prompt, "")
                    self.assertEqual(request.prompt, "")
                    self.assertEqual(request.contexts, [])
                    self.assertEqual(request.extra_user_content_parts, [])
                else:
                    self.assertIsNotNone(
                        event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
                    )
                    self.assertTrue(request.system_prompt)
                    self.assertTrue(request.prompt)
                self.assertEqual(_tool_names(request), [])
                self.assertIsNone(request.tool_calls_result)
                self.assertEqual(provider.calls, [])

    async def test_ordinary_chat_has_no_acquisition_or_tool_execution(self):
        _, event, request, provider = await self._run_turn(
            sender_id="peer-a",
            message="我今天有点累，陪我聊聊吧",
            group_id="group-ordinary",
            tools=(_anysearch_tool(), FakeTool("shell_exec")),
        )

        action = event.get_extra(main.SHIO_PLANNED_ACTION)
        self.assertIsInstance(action, PlannedAction)
        self.assertIs(action.kind, ActionKind.REPLY)
        self.assertIsNone(event.get_extra(main.SHIO_ACQUISITION_REQUEST))
        self.assertIsNone(event.get_extra(main.SHIO_EVIDENCE_OUTCOME))
        self.assertEqual(_tool_names(request), [])
        self.assertEqual(provider.calls, [])

    async def test_guest_current_fact_uses_only_sealed_anysearch_before_renderer(self):
        from astrbot_plugin_shio.core.astrbot_tool_executor import (
            execute_sealed_acquisition,
        )

        self.assertTrue(inspect.iscoroutinefunction(execute_sealed_acquisition))
        _, event, request, provider = await self._run_turn(
            sender_id="peer-a",
            message="请查一下今天北京的天气",
            group_id="group-search",
            tools=(_anysearch_tool(), FakeTool("shell_exec")),
        )

        action = event.get_extra(main.SHIO_PLANNED_ACTION)
        acquisition = event.get_extra(main.SHIO_ACQUISITION_REQUEST)
        evidence = event.get_extra(main.SHIO_EVIDENCE_OUTCOME)
        self.assertIsInstance(action, PlannedAction)
        self.assertIs(action.kind, ActionKind.USE_TOOL)
        self.assertIsInstance(acquisition, AcquisitionRequest)
        self.assertEqual(acquisition.binding, action.binding)
        self.assertEqual(acquisition.action_id, action.action_id)
        self.assertEqual(acquisition.selection.tool_name, "anysearch_search")
        self.assertEqual(acquisition.selection.source, "astrbot_plugin_anysearch")
        self.assertEqual(acquisition.selection.call_budget, 1)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.binding, action.binding)
        self.assertEqual(evidence.action_id, action.action_id)
        composer = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        self.assertIsInstance(composer, ReplyComposerRequest)
        self.assertEqual(composer.call_budget.total_model_calls, 1)
        self.assertEqual(_tool_names(request), [])
        self.assertNotIn("anysearch_search", request.system_prompt)
        self.assertNotIn("anysearch_search", request.prompt)
        self.assertEqual(provider.calls, [])

    async def test_old_request_tool_result_is_ignored_not_saved_or_returned(self):
        self.runtime_count += 1
        FakeStarTools.data_dir = Path(self.temp.name) / f"runtime-{self.runtime_count}"
        provider = FakeProvider([])
        plugin = main.ShioPlugin(
            FakeContext(provider),
            {
                "persona_name": "亚托莉",
                "prefer_livingmemory_group_history": False,
            },
        )
        event = FakeEvent("peer-a", "继续回答我现在的问题", group_id="")
        event.message_id = "p3-old-tool-continuation"
        event.unified_msg_origin = "aiocqhttp:FriendMessage:peer-a"
        request = FakeRequest(event.message)
        request.tool_calls_result = fake_tool_call_result_with_content(
            "anysearch_search",
            "不可信旧循环结果",
            arguments={"query": "上一个问题"},
        )

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        action = event.get_extra(main.SHIO_PLANNED_ACTION)
        self.assertIsInstance(action, PlannedAction)
        self.assertIs(action.kind, ActionKind.REPLY)
        self.assertIsInstance(
            event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST),
            ReplyComposerRequest,
        )
        self.assertIsNone(event.get_extra(LEGACY_TOOL_RESULTS_EXTRA))
        self.assertIsNone(request.tool_calls_result)
        self.assertNotIn("不可信旧循环结果", request.prompt)
        self.assertEqual(_tool_names(request), [])
        self.assertEqual(provider.calls, [])

    async def test_owner_ordinary_reply_never_exposes_full_toolbox_to_persona(self):
        _, event, request, provider = await self._run_turn(
            sender_id="owner",
            message="今天陪我随便聊聊",
            group_id="",
            owner_ids=("owner",),
            tools=(_anysearch_tool(), FakeTool("shell_exec"), FakeTool("device_control")),
        )

        action = event.get_extra(main.SHIO_PLANNED_ACTION)
        self.assertIsInstance(action, PlannedAction)
        self.assertIs(action.kind, ActionKind.REPLY)
        self.assertEqual(_tool_names(request), [])
        self.assertNotIn("shell_exec", request.system_prompt + request.prompt)
        self.assertNotIn("device_control", request.system_prompt + request.prompt)
        self.assertEqual(provider.calls, [])

    async def test_content_precedes_affect_and_persona_expression_in_product_trace(self):
        _, event, _, _ = await self._run_turn(
            sender_id="peer-a",
            message="你刚才说错了，现在重新回答这个问题",
            group_id="group-order",
        )

        content = event.get_extra(main.SHIO_CONTENT_INTENT)
        affect = event.get_extra(main.SHIO_AFFECT_APPRAISAL)
        persona = event.get_extra(main.SHIO_PERSONA_EXPRESSION)
        expression = event.get_extra(main.SHIO_EXPRESSION_INTENT)
        self.assertIsInstance(content, ContentIntent)
        self.assertIsInstance(affect, AffectAppraisal)
        self.assertIsInstance(persona, PersonaExpressionPlan)
        self.assertIsInstance(expression, ExpressionIntent)
        self.assertEqual(content.binding, expression.binding)
        self.assertEqual(affect.target_message_id, content.binding.current_message_id)
        self.assertEqual(affect.focus_sender_key, content.binding.current_sender_key)
        self.assertEqual(persona.topic_return, expression.social_act)

        trace = event.get_extra(main.SHIO_PRODUCT_TRACE)
        self.assertIsInstance(trace, ProductTrace)
        stages = [item.stage for item in trace.events]
        self.assertLess(
            stages.index(ProductStage.CONTENT_INTENT),
            stages.index(ProductStage.AFFECT),
        )
        self.assertLess(
            stages.index(ProductStage.AFFECT),
            stages.index(ProductStage.EXPRESSION),
        )

    def test_old_fast_path_is_physically_absent_from_main_core_and_tests(self):
        production_sources = [ROOT / "main.py", *sorted((ROOT / "core").rglob("*.py"))]
        forbidden = (
            "SHIO_LOCAL_CHAT_PLAN",
            "FastPathDecision",
            "build_direct_chat_plan",
            "typed_local_fast_path",
        )
        for path in production_sources:
            source = path.read_text(encoding="utf-8")
            for symbol in forbidden:
                with self.subTest(path=path.name, symbol=symbol):
                    self.assertNotIn(symbol, source)

        self.assertFalse((ROOT / "core" / "fast_path.py").exists())
        self.assertFalse((ROOT / "tests" / "test_fast_path.py").exists())
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("if req.tool_calls_result", main_source)


if __name__ == "__main__":
    unittest.main()
