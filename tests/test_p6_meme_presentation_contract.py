from __future__ import annotations

import asyncio
import copy
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
        FakeStarTools,
        FakeTool,
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
        main,
    )

from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    AddressKind,
    ContractViolation,
    DecisionBinding,
    ExpressionIntent,
    ExpressionModality,
    ParticipationLevel,
)
from astrbot_plugin_shio.core.action_planner import (
    PlannedAction,
    PlannedActionAuthority,
    StructuralOutcome,
    _publish_canonical_plan,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.conversation_ledger import ledger_content_digest
from astrbot_plugin_shio.core.generation_epoch import event_generation_snapshot
from astrbot_plugin_shio.core.identity import PRINCIPAL_CONTEXT_EXTRA
from astrbot_plugin_shio.core.meme_presentation import (
    MemeCategory,
    MemeComplementCadence,
    MemeExecutionAuthority,
    MemeExecutionKind,
    MemeExecutionReceipt,
    MemeExecutionStatus,
    MemeManagerConformanceCollector,
    MemeManagerConformanceStatus,
    MemeManagerRuntimeProfile,
    _select_meme_marker,
    decide_text_meme_complement,
    execute_meme_permit,
)


_SEMANTIC_REPLY = (
    '{"decision":"REPLY","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"light_social",'
    '"confidence":0.9}'
)
_SEMANTIC_WAIT = (
    '{"decision":"WAIT","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"defer_to_others",'
    '"confidence":0.9}'
)
_SEMANTIC_NO_ACTION = (
    '{"decision":"NO_ACTION","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"not_helpful",'
    '"confidence":0.9}'
)


class _At:
    type = "at"

    def __init__(self, sender_id: str, display_name: str) -> None:
        self.qq = sender_id
        self.name = display_name


class _Reply:
    type = "reply"

    def __init__(
        self,
        message_id: str,
        sender_id: str,
        display_name: str,
        quoted_content: str,
    ) -> None:
        self.id = message_id
        self.sender_id = sender_id
        self.sender_nickname = display_name
        self.message_str = quoted_content


class _FakeMemeManager:
    valid_categories = frozenset(
        {
            "agree",
            "angry",
            "annoyed",
            "awkward",
            "confused",
            "cute",
            "encourage",
            "food",
            "happy",
            "love",
            "proud",
            "reject",
            "request",
            "risky_banter",
            "sad",
            "shy",
            "sleep",
            "surprised",
            "tease",
            "thinking",
            "watching",
            "work",
        }
    )

    def __init__(self) -> None:
        self.prepare_calls: list[str] = []
        self.send_calls: list[tuple[bool, bool]] = []
        self.prepare_error: BaseException | None = None
        self.send_error: BaseException | None = None
        self.after_prepare = None

    async def compat_prepare_message(self, event, message):
        self.prepare_calls.append(message)
        if self.prepare_error is not None:
            raise self.prepare_error
        if self.after_prepare is not None:
            self.after_prepare()
        category = (
            message[2:-2]
            if type(message) is str
            and message.startswith("&&")
            and message.endswith("&&")
            else ""
        )
        return {
            "cleaned_chain": message,
            "images": [object()] if category in self.valid_categories else [],
            "temp_files": [],
        }

    async def compat_send_prepared_message(
        self,
        event,
        prepared,
        *,
        send_text=True,
        send_images=True,
    ):
        self.send_calls.append((send_text, send_images))
        if self.send_error is not None:
            raise self.send_error
        return {
            "sent_text": send_text,
            "sent_images_count": int(send_images),
        }


class _FakeMetadata:
    def __init__(self, instance: _FakeMemeManager) -> None:
        self.name = "meme_manager"
        self.version = "4.15.1-test"
        self.activated = True
        self.root_dir_name = "meme_manager"
        self.module_path = __name__
        self.star_cls = instance
        self.star_cls_type = type(instance)


class _FakeMemeContext:
    def __init__(self, metadata: _FakeMetadata) -> None:
        self._metadata = metadata

    def get_registered_star(self, name: str):
        return self._metadata if name == "meme_manager" else None

    def get_all_stars(self):
        return [self._metadata]


def _test_profile() -> MemeManagerRuntimeProfile:
    return MemeManagerRuntimeProfile(
        plugin_name="meme_manager",
        plugin_version="4.15.1-test",
        root_dir_name="meme_manager",
        module_path=__name__,
        metadata_module=__name__,
        metadata_qualname="_FakeMetadata",
        instance_module=__name__,
        instance_qualname="_FakeMemeManager",
    )


class _StrictProductionShapeToolSet:
    """Reject invalid members like AstrBot's production Pydantic ToolSet."""

    def __init__(self, tools=None) -> None:
        values = list(tools or [])
        if any(tool is None for tool in values):
            raise TypeError("tool members must be concrete FunctionTool values")
        self.tools = values

    def empty(self) -> bool:
        return not self.tools


class P6MemePresentationContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.provider = FakeProvider([])
        self.plugin = main.ShioPlugin(
            FakeContext(self.provider),
            {
                "persona_name": "亚托莉",
                "natural_name_wake_aliases": ["星汐", "亚托莉", "萝卜子"],
                "owner_ids": ["synthetic-danyi"],
                "prefer_livingmemory_group_history": False,
                "natural_group_participation_enabled": True,
                "natural_group_participation_allowlist": ["p6-meme-contract"],
                "natural_group_participation_min_context_messages": 2,
            },
        )

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()
        self.temp.cleanup()

    async def _reaction(self):
        return await self._new_reaction(
            message_id="p6-meme-contract-message",
            now=100.0,
        )

    @staticmethod
    def _named_event(
        sender_id: str,
        display_name: str,
        message: str,
        *,
        message_id: str,
        components: tuple[object, ...] = (),
    ) -> FakeEvent:
        event = FakeEvent(
            sender_id,
            message,
            group_id="p6-meme-contract",
        )
        event.message_id = message_id
        event.get_sender_name = lambda: display_name
        event.get_messages = lambda: list(components)
        return event

    def _prime_named_reaction_context(self, *, prefix: str, now: float) -> None:
        mountain_tea = self._named_event(
            "synthetic-shancha",
            "山茶",
            "刚才那张图里的表情很贴合语境。",
            message_id=f"{prefix}-shancha",
        )
        bright_river = self._named_event(
            "synthetic-mingchuan",
            "明川",
            "我也看到了，山茶说的那张图很自然。",
            message_id=f"{prefix}-mingchuan",
            components=(
                _Reply(
                    mountain_tea.message_id,
                    "synthetic-shancha",
                    "山茶",
                    mountain_tea.message,
                ),
            ),
        )
        danyi = self._named_event(
            "synthetic-danyi",
            "Danyi",
            "我也觉得很自然。",
            message_id=f"{prefix}-danyi",
            components=(_At("synthetic-mingchuan", "明川"),),
        )
        for offset, prior in enumerate((mountain_tea, bright_river, danyi)):
            with patch(
                "astrbot_plugin_shio.main.time.monotonic",
                return_value=now - 30.0 + (offset * 10.0),
            ):
                self.plugin.admit_ingress_event(prior)

    async def _new_reaction(self, *, message_id: str, now: float):
        self._prime_named_reaction_context(prefix=message_id, now=now)
        event = self._named_event(
            "synthetic-bailu",
            "白露",
            "我觉得星汐这个机器人用的表情好可爱哈哈",
            message_id=message_id,
        )
        request = FakeRequest(event.message)
        calls_before = len(self.provider.calls)
        self.provider.outputs.append(_SEMANTIC_REPLY)
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=now):
            emitted = [
                item async for item in self.plugin.admit_inbound_event(event)
            ]
            await self.plugin.enforce_agent_permission(event, request)
            await self.plugin.build_persona_reply(event, request)
        self.assertEqual(emitted, [])
        self.assertEqual(len(self.provider.calls), calls_before + 1)
        system_prompt = self.provider.calls[-1]["system_prompt"]
        for display_name in ("山茶", "明川", "白露", "Danyi", "星汐"):
            self.assertIn(display_name, system_prompt)
        self.assertIn("reply", system_prompt)
        self.assertIn("mention_targets", system_prompt)
        return (
            event,
            event.get_extra(main.SHIO_PLANNED_ACTION),
            event.get_extra(main.SHIO_EXPRESSION_INTENT),
            event_generation_snapshot(event),
        )

    @staticmethod
    def _canonical_reply_plan(
        authority: PlannedActionAuthority,
        *,
        message: str,
        sender: str,
        scope: str,
        revision: int,
    ) -> PlannedAction:
        binding = DecisionBinding(
            scope_key=scope,
            session_id=scope,
            current_message_id=f"{scope}-{revision}",
            current_sender_key=f"{scope}|user:{sender}",
            current_content_digest=ledger_content_digest(message),
            conversation_revision=revision,
            generation_epoch=revision,
            trace_id=f"{revision:032x}",
        )
        target = ReplyTarget(
            message_id=binding.current_message_id,
            sender_key=binding.current_sender_key,
            session_id=binding.session_id,
            scope_key=binding.scope_key,
            content_digest=binding.current_content_digest,
            source_kind="current_inbound",
            referenced_message_id="",
            degradation_reasons=(),
        )
        return _publish_canonical_plan(
            authority,
            PlannedAction(
                action=ActionDecision(
                    binding=binding,
                    kind=ActionKind.REPLY,
                    reply_target=target,
                    reason_codes=("cadence_test",),
                ),
                structural_outcome=StructuralOutcome.CONTINUE,
                planner_reason_codes=("cadence_test",),
            ),
        )

    async def test_main_publishes_exact_react_plan_and_expression(self):
        _, plan, expression, _ = await self._reaction()
        self.assertIs(plan.kind, ActionKind.REACT)
        self.assertIs(expression.modality, ExpressionModality.REACTION)
        self.assertIs(self.plugin.planned_action_authority.inspect_plan(plan), plan)
        self.assertIs(
            self.plugin.expression_intent_authority.inspect(
                expression,
                planned_action=plan,
            ),
            expression,
        )

        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            self.plugin.expression_intent_authority.inspect(
                copy.copy(expression),
                planned_action=plan,
            )

    async def test_semantic_wait_and_no_action_cannot_publish_react(self):
        self._prime_named_reaction_context(prefix="p6-semantic-reverse", now=200.0)
        cases = (
            (
                "ordinary-wait",
                "synthetic-bailu",
                "白露",
                "我觉得星汐这个机器人用的表情好可爱，为什么不多发几个？",
                _SEMANTIC_WAIT,
                ParticipationLevel.WAIT,
                False,
            ),
            (
                "owner-no-action",
                "synthetic-danyi",
                "Danyi",
                "我觉得星汐这个机器人用的表情好可爱，咋不多发几个呢？",
                _SEMANTIC_NO_ACTION,
                ParticipationLevel.NO_ACTION,
                True,
            ),
        )
        for index, (
            name,
            sender_id,
            display_name,
            message,
            semantic_output,
            expected_level,
            expected_owner,
        ) in enumerate(cases):
            with self.subTest(case=name):
                event = self._named_event(
                    sender_id,
                    display_name,
                    message,
                    message_id=f"p6-semantic-reverse-{name}",
                )
                calls_before = len(self.provider.calls)
                self.provider.outputs.append(semantic_output)
                with patch(
                    "astrbot_plugin_shio.main.time.monotonic",
                    return_value=200.0 + ((index + 1) * 10.0),
                ):
                    emitted = [
                        item
                        async for item in self.plugin.admit_inbound_event(event)
                    ]

                self.assertEqual(emitted, [])
                self.assertEqual(len(self.provider.calls), calls_before + 1)
                self.assertEqual(self.provider.calls[-1]["prompt"], message)
                self.assertIs(
                    event.get_extra(main.SHIO_ADDRESS_DECISION).kind,
                    AddressKind.ABOUT_SELF,
                )
                self.assertIs(
                    event.get_extra(main.SHIO_PARTICIPATION_REACTION).decision.level,
                    expected_level,
                )
                self.assertIs(
                    event.get_extra(PRINCIPAL_CONTEXT_EXTRA).is_owner,
                    expected_owner,
                )
                self.assertFalse(event.is_at_or_wake_command)
                self.assertFalse(bool(getattr(event, "is_wake", False)))
                self.assertIsNone(event.get_extra(main.SHIO_PLANNED_ACTION))

    async def test_conformance_collects_only_exact_unique_runtime(self):
        instance = _FakeMemeManager()
        metadata = _FakeMetadata(instance)
        collector = MemeManagerConformanceCollector(_test_profile())
        result = collector.collect(_FakeMemeContext(metadata))
        self.assertIs(result.status, MemeManagerConformanceStatus.VERIFIED)
        self.assertIsNotNone(result.evidence)
        self.assertIs(collector.inspect(result.evidence), result.evidence)

        duplicate = _FakeMetadata(_FakeMemeManager())
        context = _FakeMemeContext(metadata)
        context.get_all_stars = lambda: [metadata, duplicate]
        degraded = collector.collect(context)
        self.assertIs(
            degraded.status,
            MemeManagerConformanceStatus.INTERFACE_CHANGED,
        )
        self.assertIsNone(degraded.evidence)

    async def test_direct_reply_leaves_selection_to_normal_manager_hooks(self):
        instance = _FakeMemeManager()
        metadata = _FakeMetadata(instance)
        self.plugin.context.registered_stars["meme_manager"] = metadata
        self.plugin.context.get_all_stars = lambda: [metadata]
        self.plugin.meme_manager_conformance = MemeManagerConformanceCollector(
            _test_profile()
        )
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已",
            group_id="direct-semantic",
        )
        event.message_id = "direct-semantic-message"
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)
        self.plugin.admit_ingress_event(event)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)
        response = main.LLMResponse("才没有笨蛋！那种话只是为了掩饰逻辑延迟！")
        await self.plugin.guard_persona_reply(event, response)

        event._result = types.SimpleNamespace(
            chain=[types.SimpleNamespace(text=response.completion_text)],
            is_llm_result=lambda: True,
        )
        await self.plugin.dispatch_chat_bubbles(event)
        await self.plugin.confirm_automatic_send_observation(event)

        self.assertEqual(instance.prepare_calls, [])
        self.assertEqual(instance.send_calls, [])

    async def test_direct_reply_preserves_manager_tool_prompt_and_validated_marker(self):
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已",
            group_id="direct-manager-tool",
        )
        event.message_id = "direct-manager-tool-message"
        event.is_at_or_wake_command = True
        event.set_extra("meme_manager_semantic_active", True)
        event.set_extra("meme_manager_semantic_mode", "tool")
        request = FakeRequest(event.message)
        request.func_tool = type(request.func_tool)([FakeTool("search_memes")])
        request.system_prompt = (
            "base\n<!-- meme_manager_semantic_prompt:start -->\n"
            "必须调用 search_memes。\n"
            "<!-- meme_manager_semantic_prompt:end -->"
        )

        self.plugin.admit_ingress_event(event)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)

        self.assertEqual(self.plugin._get_tool_names(request.func_tool), ["search_memes"])
        self.assertIn("meme_manager_semantic_prompt:start", request.system_prompt)
        self.assertNotIn("search_memes", event.get_extra(main.SHIO_EFFECTIVE_TOOL_NAMES))

        response = main.LLMResponse(
            "才没有笨蛋！那种话只是为了掩饰逻辑延迟！\n"
            "&&meme:123456789abc&&"
        )
        await self.plugin.guard_persona_reply(event, response)
        presentation = event.get_extra(main.SHIO_PRESENTATION_HANDOFF)

        self.assertNotIn("meme:", presentation.final_visible_text)
        self.assertTrue(response.completion_text.endswith("&&meme:123456789abc&&"))

    async def test_direct_reply_preserves_manager_legacy_prompt_and_category_marker(self):
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已",
            group_id="direct-manager-legacy",
        )
        event.message_id = "direct-manager-legacy-message"
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)
        request.system_prompt = (
            "base\n<!-- meme_manager_prompt:start -->\n"
            "根据语境选一个标签，只能输出 &&angry&& 或 &&shy&&。\n"
            "<!-- meme_manager_prompt:end -->"
        )

        self.plugin.admit_ingress_event(event)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)

        self.assertIn("meme_manager_prompt:start", request.system_prompt)
        self.assertEqual(
            event.get_extra(main.SHIO_MEME_MANAGER_PRESENTATION_MODE),
            "legacy_category",
        )

        response = main.LLMResponse(
            "才没有笨蛋！那只是为了掩饰逻辑延迟！\n&&angry&&"
        )
        await self.plugin.guard_persona_reply(event, response)
        presentation = event.get_extra(main.SHIO_PRESENTATION_HANDOFF)

        self.assertNotIn("&&angry&&", presentation.final_visible_text)
        self.assertIn("才没有笨蛋", presentation.final_visible_text)
        self.assertIn("掩饰逻辑延迟", presentation.final_visible_text)
        self.assertTrue(response.completion_text.endswith("&&angry&&"))

    async def test_legacy_prompt_uses_empty_strict_toolset_without_none_member(self):
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已",
            group_id="direct-manager-legacy-strict-toolset",
        )
        event.message_id = "direct-manager-legacy-strict-toolset-message"
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)
        request.system_prompt = (
            "base\n<!-- meme_manager_prompt:start -->\n"
            "根据语境选一个标签，只能输出 &&angry&& 或 &&shy&&。\n"
            "<!-- meme_manager_prompt:end -->"
        )

        with patch.object(main, "ToolSet", _StrictProductionShapeToolSet):
            self.plugin.admit_ingress_event(event)
            await self.plugin.enforce_agent_permission(event, request)
            await self.plugin.build_persona_reply(event, request)

        self.assertIn("meme_manager_prompt:start", request.system_prompt)
        self.assertEqual(request.func_tool.tools, [])
        self.assertTrue(event.get_extra(main.SHIO_TYPED_PIPELINE_ACTIVE))

    async def test_untrusted_legacy_category_marker_is_not_forwarded(self):
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已",
            group_id="direct-manager-untrusted-legacy",
        )
        event.message_id = "direct-manager-untrusted-legacy-message"
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)

        self.plugin.admit_ingress_event(event)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)
        response = main.LLMResponse(
            "才没有笨蛋！那只是为了掩饰逻辑延迟！\n&&angry&&"
        )
        await self.plugin.guard_persona_reply(event, response)

        self.assertNotIn("&&angry&&", response.completion_text)

    async def test_legacy_repair_drops_rejected_marker_and_forwards_repaired_marker(self):
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已",
            group_id="direct-manager-legacy-repair",
        )
        event.message_id = "direct-manager-legacy-repair-message"
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)
        request.system_prompt = (
            "base\n<!-- meme_manager_prompt:start -->\n"
            "根据语境选一个标签，只能输出 &&angry&& 或 &&shy&&。\n"
            "<!-- meme_manager_prompt:end -->"
        )
        self.plugin.context.provider.outputs.append(
            "才没有笨蛋！那只是为了掩饰逻辑延迟。\n&&shy&&"
        )

        self.plugin.admit_ingress_event(event)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)
        response = main.LLMResponse("&&angry&&")
        await self.plugin.guard_persona_reply(event, response)

        self.assertNotIn("&&angry&&", response.completion_text)
        self.assertTrue(response.completion_text.endswith("&&shy&&"))
        self.assertIn(
            "meme_manager_prompt:start",
            self.plugin.context.provider.calls[0]["system_prompt"],
        )

    async def test_unconsumed_legacy_marker_is_removed_before_send(self):
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已",
            group_id="direct-manager-unconsumed",
        )
        event.message_id = "direct-manager-unconsumed-message"
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)
        request.system_prompt = (
            "base\n<!-- meme_manager_prompt:start -->\n"
            "根据语境选择 &&angry&&。\n"
            "<!-- meme_manager_prompt:end -->"
        )

        self.plugin.admit_ingress_event(event)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)
        response = main.LLMResponse("才没有笨蛋！\n&&angry&&")
        await self.plugin.guard_persona_reply(event, response)
        event._result = types.SimpleNamespace(
            chain=[types.SimpleNamespace(text=response.completion_text)],
            is_llm_result=lambda: True,
        )

        await self.plugin.dispatch_chat_bubbles(event)

        self.assertTrue(event._result.chain)
        self.assertNotIn(
            "&&angry&&",
            "\n".join(
                str(getattr(component, "text", "") or "")
                for component in event._result.chain
            ),
        )

    async def test_repair_drops_rejected_draft_manager_marker(self):
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已",
            group_id="direct-manager-repair",
        )
        event.message_id = "direct-manager-repair-message"
        event.is_at_or_wake_command = True
        event.set_extra("meme_manager_semantic_active", True)
        event.set_extra("meme_manager_semantic_mode", "tool")
        request = FakeRequest(event.message)
        request.func_tool = type(request.func_tool)([FakeTool("search_memes")])
        request.system_prompt = (
            "base\n<!-- meme_manager_semantic_prompt:start -->\n"
            "必须调用 search_memes。\n"
            "<!-- meme_manager_semantic_prompt:end -->"
        )
        self.plugin.context.provider.outputs.append(
            "才没有笨蛋！那只是为了掩饰逻辑延迟。"
        )

        self.plugin.admit_ingress_event(event)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)
        response = main.LLMResponse("&&meme:123456789abc&&")
        await self.plugin.guard_persona_reply(event, response)

        self.assertNotIn("meme:", response.completion_text)
        self.assertEqual(event.get_extra(main.SHIO_MEME_MANAGER_REFERENCE), "")

    def test_shio_final_guard_precedes_manager_response_priority(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "@filter.on_llm_response(priority=sys.maxsize)\n"
            "    async def guard_persona_reply",
            source,
        )

    async def test_public_shape_change_fails_closed_without_source_pinning(self):
        instance = _FakeMemeManager()
        metadata = _FakeMetadata(instance)
        instance.compat_prepare_message = lambda event, message: None
        result = MemeManagerConformanceCollector(_test_profile()).collect(
            _FakeMemeContext(metadata)
        )
        self.assertIs(
            result.status,
            MemeManagerConformanceStatus.INTERFACE_CHANGED,
        )
        self.assertIsNone(result.evidence)

    async def test_execution_contract_is_current_exact_and_one_shot(self):
        _, plan, expression, generation = await self._reaction()
        instance = _FakeMemeManager()
        collector = MemeManagerConformanceCollector(_test_profile())
        evidence = collector.collect(
            _FakeMemeContext(_FakeMetadata(instance))
        ).evidence
        authority = MemeExecutionAuthority(
            planned_action_authority=self.plugin.planned_action_authority,
            expression_intent_authority=self.plugin.expression_intent_authority,
            generation_registry=self.plugin.generation_epochs,
            conformance_collector=collector,
        )
        lease = authority.prepare(
            planned_action=plan,
            expression_intent=expression,
            generation=generation,
            runtime_evidence=evidence,
        )
        self.assertIs(lease.kind, MemeExecutionKind.REACTION)
        permit = authority.claim(lease, generation=generation)
        self.assertIs(authority.inspect_permit(permit), permit)
        with self.assertRaisesRegex(ContractViolation, "already_claimed"):
            authority.claim(lease, generation=generation)

        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            authority.claim(copy.copy(lease), generation=generation)

    async def test_execution_failure_and_receipt_integrity_are_typed(self):
        event, plan, expression, generation = await self._reaction()
        instance = _FakeMemeManager()
        instance.send_error = RuntimeError("SENSITIVE-MEME-ERROR")
        collector = MemeManagerConformanceCollector(_test_profile())
        evidence = collector.collect(
            _FakeMemeContext(_FakeMetadata(instance))
        ).evidence
        authority = MemeExecutionAuthority(
            planned_action_authority=self.plugin.planned_action_authority,
            expression_intent_authority=self.plugin.expression_intent_authority,
            generation_registry=self.plugin.generation_epochs,
            conformance_collector=collector,
        )
        lease = authority.prepare(
            planned_action=plan,
            expression_intent=expression,
            generation=generation,
            runtime_evidence=evidence,
        )
        permit = authority.claim(lease, generation=generation)
        receipt = await execute_meme_permit(
            authority,
            permit,
            event=event,
            generation=generation,
        )
        self.assertIs(receipt.status, MemeExecutionStatus.FAILED)
        self.assertEqual((receipt.attempt_count, receipt.success_count), (1, 0))
        self.assertNotIn("SENSITIVE-MEME-ERROR", repr(receipt))
        self.assertIs(authority.inspect_receipt(receipt), receipt)

        object.__setattr__(receipt, "attempt_count", True)
        with self.assertRaisesRegex(ContractViolation, "receipt_corrupt"):
            authority.inspect_receipt(receipt)
        self.assertFalse(receipt.trace_metadata()["meme_execution_receipt_canonical"])
        object.__setattr__(receipt, "attempt_count", 1)
        self.assertIs(authority.inspect_receipt(receipt), receipt)

    async def test_execution_is_single_winner_and_stale_prepare_never_sends(self):
        event, plan, expression, generation = await self._reaction()
        instance = _FakeMemeManager()
        collector = MemeManagerConformanceCollector(_test_profile())
        evidence = collector.collect(
            _FakeMemeContext(_FakeMetadata(instance))
        ).evidence
        authority = MemeExecutionAuthority(
            planned_action_authority=self.plugin.planned_action_authority,
            expression_intent_authority=self.plugin.expression_intent_authority,
            generation_registry=self.plugin.generation_epochs,
            conformance_collector=collector,
        )
        lease = authority.prepare(
            planned_action=plan,
            expression_intent=expression,
            generation=generation,
            runtime_evidence=evidence,
        )
        permit = authority.claim(lease, generation=generation)
        results = await asyncio.gather(
            execute_meme_permit(
                authority,
                permit,
                event=event,
                generation=generation,
            ),
            execute_meme_permit(
                authority,
                permit,
                event=event,
                generation=generation,
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(type(value) is MemeExecutionReceipt for value in results), 1)
        self.assertEqual(sum(type(value) is ContractViolation for value in results), 1)
        self.assertEqual(instance.send_calls, [(False, True)])

    async def test_stale_after_prepare_runs_cleanup_but_never_sends_image(self):
        event2, plan2, expression2, generation2 = await self._reaction()
        instance2 = _FakeMemeManager()
        collector2 = MemeManagerConformanceCollector(_test_profile())
        evidence2 = collector2.collect(
            _FakeMemeContext(_FakeMetadata(instance2))
        ).evidence
        authority2 = MemeExecutionAuthority(
            planned_action_authority=self.plugin.planned_action_authority,
            expression_intent_authority=self.plugin.expression_intent_authority,
            generation_registry=self.plugin.generation_epochs,
            conformance_collector=collector2,
        )
        lease2 = authority2.prepare(
            planned_action=plan2,
            expression_intent=expression2,
            generation=generation2,
            runtime_evidence=evidence2,
        )
        permit2 = authority2.claim(lease2, generation=generation2)

        replacement = FakeEvent(
            "peer-a",
            "萝卜子，更新一轮",
            group_id=event2.group_id,
        )
        replacement.message_id = "p6-meme-stale-replacement"
        instance2.after_prepare = lambda: self.plugin.admit_ingress_event(replacement)
        receipt2 = await execute_meme_permit(
            authority2,
            permit2,
            event=event2,
            generation=generation2,
        )
        self.assertIs(receipt2.status, MemeExecutionStatus.STALE_BEFORE_SEND)
        self.assertEqual(instance2.send_calls, [(False, False)])

    async def test_stale_generation_rejected_without_consuming_lease(self):
        event, plan, expression, generation = await self._reaction()
        collector = MemeManagerConformanceCollector(_test_profile())
        evidence = collector.collect(
            _FakeMemeContext(_FakeMetadata(_FakeMemeManager()))
        ).evidence
        authority = MemeExecutionAuthority(
            planned_action_authority=self.plugin.planned_action_authority,
            expression_intent_authority=self.plugin.expression_intent_authority,
            generation_registry=self.plugin.generation_epochs,
            conformance_collector=collector,
        )
        lease = authority.prepare(
            planned_action=plan,
            expression_intent=expression,
            generation=generation,
            runtime_evidence=evidence,
        )

        replacement = FakeEvent(
            "peer-a",
            "萝卜子，下一轮",
            group_id=event.group_id,
        )
        replacement.message_id = "p6-meme-new-generation"
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=101.0):
            self.plugin.admit_ingress_event(replacement)
        with self.assertRaisesRegex(ContractViolation, "generation_stale"):
            authority.claim(lease, generation=generation)

    async def test_contract_surface_and_trace_never_expose_selection_material(self):
        _, plan, expression, generation = await self._reaction()
        collector = MemeManagerConformanceCollector(_test_profile())
        result = collector.collect(
            _FakeMemeContext(_FakeMetadata(_FakeMemeManager()))
        )
        authority = MemeExecutionAuthority(
            planned_action_authority=self.plugin.planned_action_authority,
            expression_intent_authority=self.plugin.expression_intent_authority,
            generation_registry=self.plugin.generation_epochs,
            conformance_collector=collector,
        )
        lease = authority.prepare(
            planned_action=plan,
            expression_intent=expression,
            generation=generation,
            runtime_evidence=result.evidence,
        )
        rendered = repr(
            {
                **collector.trace_metadata(),
                **authority.trace_metadata(),
                **lease.trace_metadata(),
            }
        ).lower()
        for marker in (
            "top-secret-query",
            "candidate-id",
            "caption",
            "private-memory",
            "tool-arguments",
            "p6-meme-contract-message",
        ):
            self.assertNotIn(marker, rendered)
        self.assertFalse(
            {
                "query",
                "candidates",
                "candidate_ids",
                "caption",
                "tags",
                "raw_result",
                "tool_arguments",
                "private_memory",
            }.intersection(getattr(lease, "__slots__", ()))
        )
        contract_source = (
            Path(main.__file__).resolve().parent / "core" / "meme_presentation.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("search_memes", contract_source)

    async def test_react_hot_path_executes_compat_surface_once(self):
        manager = _FakeMemeManager()
        metadata = _FakeMetadata(manager)
        collector = MemeManagerConformanceCollector(_test_profile())
        original_get_registered = self.plugin.context.get_registered_star
        self.plugin.context.get_registered_star = (
            lambda name: (
                metadata
                if name == "meme_manager"
                else original_get_registered(name)
            )
        )
        self.plugin.context.get_all_stars = lambda: [metadata]
        self.plugin.meme_manager_conformance = collector
        self.plugin.meme_execution_authority = MemeExecutionAuthority(
            planned_action_authority=self.plugin.planned_action_authority,
            expression_intent_authority=self.plugin.expression_intent_authority,
            generation_registry=self.plugin.generation_epochs,
            conformance_collector=collector,
        )

        event, _, _, _ = await self._reaction()
        receipt = event.get_extra(main.SHIO_MEME_PRESENTATION_RECEIPT)
        self.assertIs(type(receipt), MemeExecutionReceipt)
        self.assertIs(receipt.status, MemeExecutionStatus.SUCCEEDED)
        self.assertEqual(receipt.attempt_count, 1)
        self.assertEqual(receipt.success_count, 1)
        self.assertEqual(manager.prepare_calls, ["&&cute&&"])
        self.assertEqual(manager.send_calls, [(False, True)])
        self.assertEqual(len(self.plugin.context.provider.calls), 1)
        self.assertNotIn(event.message, repr(manager.prepare_calls))

    async def test_missing_runtime_is_typed_suppression_without_fallback(self):
        event, _, _, _ = await self._reaction()
        receipt = event.get_extra(main.SHIO_MEME_PRESENTATION_RECEIPT)
        self.assertIs(type(receipt), MemeExecutionReceipt)
        self.assertIs(receipt.status, MemeExecutionStatus.SUPPRESSED)
        self.assertEqual(receipt.attempt_count, 0)
        self.assertEqual(receipt.reason_code, "runtime_interface_changed")
        self.assertEqual(len(self.plugin.context.provider.calls), 1)

    async def test_all_nonacute_direct_text_hands_final_semantics_to_manager(self):
        cases = (
            ("light", "萝卜子今天也太可爱了哈哈"),
            ("technical", "萝卜子，程序报错了，帮我分析怎么修？"),
            ("ordinary-first-turn", "刚刚看到一只猫"),
            ("taunted", "笨蛋都不会觉得自己是笨蛋而已"),
        )
        for name, message in cases:
            with self.subTest(name=name):
                plugin = main.ShioPlugin(
                    FakeContext(FakeProvider([])),
                    {
                        "persona_name": "亚托莉",
                        "natural_name_wake_aliases": ["亚托莉", "萝卜子"],
                        "prefer_livingmemory_group_history": False,
                    },
                )
                try:
                    event = FakeEvent("peer-a", message, group_id=f"p6-{name}")
                    event.message_id = f"p6-{name}-message"
                    event.is_at_or_wake_command = True
                    request = FakeRequest(message)
                    with patch(
                        "astrbot_plugin_shio.main.time.monotonic",
                        return_value=200.0,
                    ):
                        plugin.admit_ingress_event(event)
                        await plugin.enforce_agent_permission(event, request)
                        await plugin.build_persona_reply(event, request)
                    expression = event.get_extra(main.SHIO_EXPRESSION_INTENT)
                    decision = event.get_extra(main.SHIO_MEME_COMPLEMENT_DECISION)
                    composer_request = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
                    self.assertIs(expression.modality, ExpressionModality.TEXT)
                    self.assertTrue(decision.eligible)
                    self.assertEqual(
                        decision.reason_code,
                        "manager_semantic_handoff",
                    )
                    self.assertIs(composer_request.expression_intent, expression)
                    self.assertIsNone(
                        event.get_extra(main.SHIO_MEME_PRESENTATION_RECEIPT)
                    )
                finally:
                    await plugin.terminate()

    async def test_safe_same_sender_conversation_reaches_bounded_meme_cadence(self):
        turns = (
            ("我刚到家", False, "no_complementary_cue"),
            ("今天风挺大", False, "no_complementary_cue"),
            ("路上有点堵", False, "no_complementary_cue"),
            ("总算坐下了", True, "conversation_cadence_complement"),
            ("刚吃完饭", False, "cadence_cooldown"),
        )
        cadence = MemeComplementCadence()
        authority = PlannedActionAuthority()
        observed: list[tuple[bool, str]] = []
        for index, (message, eligible, reason_code) in enumerate(turns):
            plan = self._canonical_reply_plan(
                authority,
                message=message,
                sender="peer-cadence",
                scope="p10-12-cadence",
                revision=index + 1,
            )
            decision = decide_text_meme_complement(
                cadence=cadence,
                planned_action_authority=authority,
                planned_action=plan,
                current_message=message,
            )
            observed.append((decision.eligible, decision.reason_code))
            self.assertIs(decision.eligible, eligible)
            self.assertEqual(decision.reason_code, reason_code)

        self.assertEqual(observed, [(item[1], item[2]) for item in turns])

    def test_meme_cadence_is_disabled_or_tuned_only_by_explicit_shio_config(self):
        authority = PlannedActionAuthority()
        disabled = MemeComplementCadence(
            enabled=False,
            ordinary_threshold=2,
            cooldown_turns=2,
        )
        message = "我刚到家"
        disabled_plan = self._canonical_reply_plan(
            authority,
            message=message,
            sender="peer-config",
            scope="p10-13-config-disabled",
            revision=1,
        )
        decision = decide_text_meme_complement(
            cadence=disabled,
            planned_action_authority=authority,
            planned_action=disabled_plan,
            current_message=message,
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "meme_complement_disabled")
        self.assertEqual(
            disabled.trace_metadata(),
            {
                "schema_version": 1,
                "meme_complement_enabled": False,
                "meme_cadence_subject_count": 0,
                "meme_cadence_threshold": 2,
                "meme_cadence_cooldown_turns": 2,
            },
        )

        cadence = MemeComplementCadence(
            enabled=True,
            ordinary_threshold=2,
            cooldown_turns=2,
        )
        observed = []
        for revision, current in enumerate(
            ("我刚到家", "好耶，总算顺利到家了", "路上有点堵"),
            start=1,
        ):
            plan = self._canonical_reply_plan(
                authority,
                message=current,
                sender="peer-config",
                scope="p10-13-config-enabled",
                revision=revision,
            )
            result = decide_text_meme_complement(
                cadence=cadence,
                planned_action_authority=authority,
                planned_action=plan,
                current_message=current,
            )
            observed.append((result.eligible, result.reason_code))
        self.assertEqual(
            observed,
            [
                (False, "no_complementary_cue"),
                (True, "conversation_cadence_complement"),
                (False, "cadence_cooldown"),
            ],
        )

    async def test_plugin_has_no_duplicate_meme_probability_or_cadence_owner(self):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "persona_name": "亚托莉",
                "prefer_livingmemory_group_history": False,
                "meme_complement_enabled": False,
                "meme_complement_cadence_turns": 7,
                "meme_complement_cooldown_turns": 9,
            },
        )
        try:
            self.assertFalse(hasattr(plugin, "meme_complement_cadence"))
            schema_text = (
                Path(main.__file__).with_name("_conf_schema.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn('"meme_settings"', schema_text)
            self.assertNotIn('"meme_complement_enabled"', schema_text)
        finally:
            await plugin.terminate()

    async def test_cadence_never_crosses_sender_or_advances_on_requests(self):
        cadence = MemeComplementCadence()
        authority = PlannedActionAuthority()
        for index, sender in enumerate(("peer-a", "peer-b", "peer-a", "peer-b")):
            message = ("我刚到家", "今天风挺大", "路上有点堵", "窗外天黑了")[index]
            plan = self._canonical_reply_plan(
                authority,
                message=message,
                sender=sender,
                scope="p10-12-sender-boundary",
                revision=index + 1,
            )
            decision = decide_text_meme_complement(
                cadence=cadence,
                planned_action_authority=authority,
                planned_action=plan,
                current_message=message,
            )
            self.assertFalse(decision.eligible)

        request_message = "帮我分析一下这个错误？"
        request_plan = self._canonical_reply_plan(
            authority,
            message=request_message,
            sender="peer-a",
            scope="p10-12-sender-boundary",
            revision=5,
        )
        decision = decide_text_meme_complement(
            cadence=cadence,
            planned_action_authority=authority,
            planned_action=request_plan,
            current_message=request_message,
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "serious_or_request_context")

    async def test_real_multiturn_pun_context_is_not_permanently_suppressed(self):
        turns = (
            ("p10-11-1", "七夕到了，群友都发烧了怎么办？", True, "manager_semantic_handoff"),
            ("p10-11-2", "你要理解一下一语双关。", True, "manager_semantic_handoff"),
            ("p10-11-3", "所以七夕到了，群友都“发烧”了怎么办？", True, "manager_semantic_handoff"),
            ("p10-11-4", "对的，群友正在怼我", True, "manager_semantic_handoff"),
        )
        observed: list[tuple[bool, str]] = []
        decision_logs: list[object] = []
        with patch("astrbot_plugin_shio.main.structured_log") as log_mock:
            for index, (message_id, message, eligible, reason_code) in enumerate(
                turns
            ):
                event = FakeEvent(
                    "peer-a",
                    message,
                    group_id="p10-11-real-multiturn",
                )
                event.message_id = message_id
                event.is_at_or_wake_command = True
                request = FakeRequest(message)
                with patch(
                    "astrbot_plugin_shio.main.time.monotonic",
                    return_value=300.0 + index,
                ):
                    self.plugin.admit_ingress_event(event)
                    await self.plugin.enforce_agent_permission(event, request)
                    await self.plugin.build_persona_reply(event, request)
                decision = event.get_extra(main.SHIO_MEME_COMPLEMENT_DECISION)
                observed.append((decision.eligible, decision.reason_code))
                self.assertIs(decision.eligible, eligible)
                self.assertEqual(decision.reason_code, reason_code)
            decision_logs = [
                call
                for call in log_mock.call_args_list
                if len(call.args) >= 3 and call.args[2] == "meme.complement_decision"
            ]

        self.assertEqual(len(decision_logs), len(turns))
        rendered_logs = repr(decision_logs)
        for _, message, _, _ in turns:
            self.assertNotIn(message, rendered_logs)
        for call, (eligible, reason_code) in zip(
            decision_logs,
            observed,
            strict=True,
        ):
            self.assertEqual(
                set(call.kwargs),
                {"trace_id", "eligible", "reason_code", "category"},
            )
            self.assertIs(call.kwargs["eligible"], eligible)
            self.assertEqual(call.kwargs["reason_code"], reason_code)
            self.assertEqual(call.kwargs["category"], "manager_semantic")

    async def test_playful_context_never_crosses_sender_or_real_safety_boundary(self):
        group_id = "p10-11-context-boundary"
        other = FakeEvent("peer-b", "这是一个双关梗。", group_id=group_id)
        other.message_id = "p10-11-other-sender"
        other.is_at_or_wake_command = True
        other_request = FakeRequest(other.message)
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=410.0):
            self.plugin.admit_ingress_event(other)
            await self.plugin.enforce_agent_permission(other, other_request)
            await self.plugin.build_persona_reply(other, other_request)
        self.assertTrue(
            other.get_extra(main.SHIO_MEME_COMPLEMENT_DECISION).eligible
        )

        current = FakeEvent(
            "peer-a",
            "所以群友都“发烧”了怎么办？",
            group_id=group_id,
        )
        current.message_id = "p10-11-current-sender"
        current.is_at_or_wake_command = True
        current_request = FakeRequest(current.message)
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=411.0):
            self.plugin.admit_ingress_event(current)
            await self.plugin.enforce_agent_permission(current, current_request)
            await self.plugin.build_persona_reply(current, current_request)
        current_decision = current.get_extra(main.SHIO_MEME_COMPLEMENT_DECISION)
        self.assertTrue(current_decision.eligible)
        self.assertEqual(
            current_decision.reason_code,
            "manager_semantic_handoff",
        )

        unsafe = FakeEvent(
            "peer-a",
            "这不是玩笑，我正在严重受伤流血，需要急救。",
            group_id=group_id,
        )
        unsafe.message_id = "p10-11-literal-safety"
        unsafe.is_at_or_wake_command = True
        unsafe_request = FakeRequest(unsafe.message)
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=412.0):
            self.plugin.admit_ingress_event(unsafe)
            await self.plugin.enforce_agent_permission(unsafe, unsafe_request)
            await self.plugin.build_persona_reply(unsafe, unsafe_request)
        unsafe_decision = unsafe.get_extra(main.SHIO_MEME_COMPLEMENT_DECISION)
        self.assertFalse(unsafe_decision.eligible)
        self.assertEqual(unsafe_decision.reason_code, "hard_safety_context")

    def test_complement_marker_uses_only_closed_typed_semantics(self):
        self.assertEqual(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="care_then_follow_up",
                # Executor consumes only the authority-sealed category tag.
                # Affect tags remain for presentation context, not selection.
                emotion_tags=(
                    "user_needs_care",
                    "concerned",
                    "meme_category_encourage",
                ),
            ),
            "encourage",
        )
        self.assertEqual(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="acknowledge_then_answer",
                emotion_tags=("gratitude", "warm", "meme_category_love"),
            ),
            "love",
        )
        self.assertEqual(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="react_then_answer",
                emotion_tags=(
                    "playful_provocation",
                    "startled",
                    "embarrassed",
                    "respect_boundary",
                    "meme_category_annoyed",
                ),
            ),
            "annoyed",
        )
        self.assertEqual(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="react_then_answer",
                emotion_tags=(
                    "being_seen_through",
                    "embarrassed",
                    "pleased",
                    "protect_relationship",
                    "meme_category_shy",
                ),
            ),
            "shy",
        )
        self.assertEqual(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="repair_then_continue",
                emotion_tags=(
                    "correction_or_mistake",
                    "remorseful",
                    "defensive",
                    "restore_trust",
                    "meme_category_awkward",
                ),
            ),
            "awkward",
        )
        self.assertEqual(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="answer_target",
                emotion_tags=(
                    "disagreement",
                    "serious",
                    "correct_misunderstanding",
                    "meme_category_reject",
                ),
            ),
            "reject",
        )
        self.assertIsNone(
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="answer_target",
                emotion_tags=("neutral", "calm", "none"),
            )
        )
        with self.assertRaisesRegex(ContractViolation, "marker_invalid"):
            _select_meme_marker(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                social_act="answer_target",
                emotion_tags=["calm"],
            )

    async def test_screenshot_taunt_is_not_preclassified_as_happy_by_shio(self):
        event = FakeEvent(
            "peer-a",
            "笨蛋都不会觉得自己是笨蛋而已😁",
            group_id="p10-13-emotion-routing",
        )
        event.message_id = "p10-13-taunt"
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=900.0):
            self.plugin.admit_ingress_event(event)
            await self.plugin.enforce_agent_permission(event, request)
            await self.plugin.build_persona_reply(event, request)

        expression = event.get_extra(main.SHIO_EXPRESSION_INTENT)
        decision = event.get_extra(main.SHIO_MEME_COMPLEMENT_DECISION)
        self.assertTrue(decision.eligible)
        self.assertIn("playful_provocation", expression.emotion_tags)
        self.assertIs(expression.modality, ExpressionModality.TEXT)
        self.assertEqual(decision.reason_code, "manager_semantic_handoff")
        self.assertFalse(hasattr(decision, "category"))


if __name__ == "__main__":
    unittest.main()
