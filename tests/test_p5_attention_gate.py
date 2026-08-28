from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

try:
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        FakeTool,
        FakeToolSet,
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
        main,
    )

from astrbot_plugin_shio.core.accepted_turn_authority import (
    AcceptedTurnConsumer,
)
from astrbot_plugin_shio.core.contracts import (
    ActionKind,
    AddressKind,
    ContractViolation,
)
from astrbot_plugin_shio.core.conversation_event import (
    PluginSource,
    issue_plugin_source_evidence,
)
from astrbot_plugin_shio.core.opportunity_attention import (
    OpportunityAttentionAuthority,
    OpportunityAttentionDecision,
    OpportunityAttentionLevel,
)


_SEMANTIC_REPLY = (
    '{"decision":"REPLY","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"natural_continuation",'
    '"confidence":0.9}'
)


class At:
    type = "at"

    def __init__(self, qq: str) -> None:
        self.qq = qq


class P5AttentionGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.runtime_count = 0

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _run_turn(
        self,
        *,
        message: str,
        direct: bool = False,
        components: tuple[object, ...] = (),
        prime_context: bool = False,
    ):
        self.runtime_count += 1
        FakeStarTools.data_dir = Path(self.temp.name) / f"runtime-{self.runtime_count}"
        provider = FakeProvider([_SEMANTIC_REPLY] if prime_context else [])
        plugin = main.ShioPlugin(
            FakeContext(provider, global_tools=[FakeTool("anysearch_search")]),
            {
                "persona_name": "亚托莉",
                "natural_name_wake_aliases": ["亚托莉", "萝卜子"],
                "guest_allowed_tools": ["anysearch_search"],
                "prefer_livingmemory_group_history": False,
                "natural_group_participation_enabled": True,
                "natural_group_participation_allowlist": ["p5-attention-group"],
                "natural_group_participation_min_context_messages": 2,
            },
        )
        if prime_context:
            prior = FakeEvent(
                "peer-b",
                "刚才的话题还没说完",
                group_id="p5-attention-group",
            )
            prior.message_id = f"p5-attention-prior-{self.runtime_count}"
            plugin.admit_ingress_event(prior)
        event = FakeEvent("peer-a", message, group_id="p5-attention-group")
        event.message_id = f"p5-attention-{abs(hash((message, direct))) % 1000000}"
        event.is_at_or_wake_command = direct
        event.get_messages = lambda: list(components)
        request = FakeRequest(message)
        request.func_tool = FakeToolSet([FakeTool("anysearch_search")])

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        return plugin, event, request, provider

    async def test_exact_accepted_human_address_maps_to_closed_attention_levels(self):
        cases = (
            (
                "direct",
                "你今天开心吗？",
                True,
                (),
                AddressKind.DIRECT_SELF,
                OpportunityAttentionLevel.REQUIRED,
                True,
                False,
            ),
            (
                "about-self",
                "我觉得萝卜子这个称呼很有趣",
                False,
                (),
                AddressKind.ABOUT_SELF,
                OpportunityAttentionLevel.CANDIDATE,
                False,
                True,
            ),
            (
                "open-question",
                "大家今晚吃什么？",
                False,
                (),
                AddressKind.OPEN_GROUP,
                OpportunityAttentionLevel.CANDIDATE,
                False,
                True,
            ),
            (
                "other-person",
                "小林你怎么看？",
                False,
                (At("peer-b"),),
                AddressKind.OTHER_PERSON,
                OpportunityAttentionLevel.WAIT,
                False,
                False,
            ),
            (
                "uncertain",
                "今天下雨了",
                False,
                (),
                AddressKind.UNCERTAIN,
                OpportunityAttentionLevel.CANDIDATE,
                False,
                True,
            ),
        )
        for (
            name,
            message,
            direct,
            components,
            address_kind,
            level,
            direct_required,
            candidate,
        ) in cases:
            with self.subTest(case=name):
                plugin, event, _request, _provider = await self._run_turn(
                    message=message,
                    direct=direct,
                    components=components,
                )
                attention = event.get_extra(main.SHIO_OPPORTUNITY_ATTENTION)
                address = event.get_extra(main.SHIO_ADDRESS_DECISION)
                admission = event.get_extra(main.SHIO_ADMISSION_RESULT)

                self.assertIsInstance(attention, OpportunityAttentionDecision)
                self.assertIs(address.kind, address_kind)
                self.assertIs(attention.address, address)
                self.assertIs(attention.binding, admission.decision.binding)
                self.assertIs(attention.level, level)
                self.assertIs(attention.direct_required, direct_required)
                self.assertIs(attention.participation_candidate, candidate)
                self.assertIs(
                    plugin.opportunity_attention_authority.inspect(
                        attention,
                        address=address,
                        binding=admission.decision.binding,
                    ),
                    attention,
                )
                metadata = plugin.accepted_turn_authority.trace_metadata()
                self.assertEqual(metadata["ticket_count"], 4)
                self.assertEqual(metadata["terminal_ticket_count"], 4)
                self.assertIn(
                    AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
                    tuple(AcceptedTurnConsumer),
                )

    async def test_attention_copy_cross_authority_and_mutation_are_not_canonical(self):
        plugin, event, _request, _provider = await self._run_turn(
            message="大家怎么看这个问题？",
        )
        attention = event.get_extra(main.SHIO_OPPORTUNITY_ATTENTION)
        address = event.get_extra(main.SHIO_ADDRESS_DECISION)
        binding = event.get_extra(main.SHIO_ADMISSION_RESULT).decision.binding

        copied = copy.copy(attention)
        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            plugin.opportunity_attention_authority.inspect(
                copied,
                address=address,
                binding=binding,
            )

        copied_address = copy.copy(address)
        with self.assertRaisesRegex(ContractViolation, "corrupt"):
            plugin.opportunity_attention_authority.inspect(
                attention,
                address=copied_address,
                binding=binding,
            )

        original_address_kind = address.kind
        object.__setattr__(address, "kind", AddressKind.DIRECT_SELF)
        with self.assertRaisesRegex(ContractViolation, "corrupt"):
            plugin.opportunity_attention_authority.inspect(
                attention,
                address=address,
                binding=binding,
            )
        object.__setattr__(address, "kind", original_address_kind)

        other_authority = OpportunityAttentionAuthority(
            plugin.accepted_turn_authority,
            plugin.address_resolution_authority,
        )
        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            other_authority.inspect(
                attention,
                address=address,
                binding=binding,
            )

        original = attention.level
        object.__setattr__(attention, "level", OpportunityAttentionLevel.REQUIRED)
        with self.assertRaisesRegex(ContractViolation, "corrupt"):
            plugin.opportunity_attention_authority.inspect(
                attention,
                address=address,
                binding=binding,
            )
        object.__setattr__(attention, "level", original)
        self.assertIs(
            plugin.opportunity_attention_authority.inspect(
                attention,
                address=address,
                binding=binding,
            ),
            attention,
        )

    async def test_p5_01_attention_levels_feed_p5_02_without_tool_execution(self):
        for message, expected_kind in (
            ("我觉得萝卜子这个称呼很有趣", ActionKind.REPLY),
            ("大家今晚吃什么？", ActionKind.REPLY),
            ("今天下雨了", ActionKind.NO_ACTION),
        ):
            with self.subTest(message=message):
                _plugin, event, request, provider = await self._run_turn(
                    message=message,
                    prime_context=expected_kind is ActionKind.REPLY,
                )
                planned = event.get_extra(main.SHIO_PLANNED_ACTION)
                self.assertIs(planned.kind, expected_kind)
                if expected_kind is ActionKind.NO_ACTION:
                    self.assertEqual(request.system_prompt, "")
                    self.assertEqual(request.prompt, "")
                else:
                    self.assertTrue(request.system_prompt)
                    self.assertTrue(request.prompt)
                    self.assertTrue(request.contexts)
                    self.assertTrue(
                        all(
                            context.get("role") in {"user", "assistant"}
                            and bool(context.get("content"))
                            for context in request.contexts
                        )
                    )
                    self.assertNotIn(message, repr(request.contexts))
                if expected_kind is ActionKind.NO_ACTION:
                    self.assertEqual(request.contexts, [])
                self.assertEqual(request.extra_user_content_parts, [])
                self.assertEqual(tuple(request.func_tool.tools), ())
                self.assertEqual(
                    len(provider.calls),
                    0 if expected_kind is ActionKind.NO_ACTION else 1,
                )

    async def test_dropped_self_and_known_bot_have_no_attention_or_state(self):
        cases = (
            (
                {},
                "bot-10000",
                "这是自身回声",
                "drop_self",
                None,
            ),
            (
                {"trusted_bot_identities": ["亚托莉|other-bot-20000"]},
                "other-bot-20000",
                "这是已知机器人消息",
                "drop_known_bot",
                None,
            ),
            (
                {},
                "plugin-worker",
                "这是外部插件回显",
                "drop_plugin_echo",
                PluginSource.EXTERNAL_OTHER,
            ),
        )
        for config, sender, message, disposition, plugin_source in cases:
            with self.subTest(disposition=disposition):
                self.runtime_count += 1
                FakeStarTools.data_dir = (
                    Path(self.temp.name) / f"runtime-{self.runtime_count}"
                )
                provider = FakeProvider([])
                plugin = main.ShioPlugin(FakeContext(provider), config)
                event = FakeEvent(sender, message, group_id="p5-rejected")
                event.message_id = f"p5-{disposition}"
                if plugin_source is not None:
                    event.set_extra(
                        main.SHIO_PLUGIN_SOURCE_EVIDENCE,
                        issue_plugin_source_evidence(
                            source=plugin_source,
                            source_event_digest="e" * 64,
                        ),
                    )
                request = FakeRequest(message)

                self.assertTrue(plugin.prepare_ingress_candidate(event))
                outputs = [item async for item in plugin.admit_inbound_event(event)]
                self.assertEqual(outputs, [])

                admission = event.get_extra(main.SHIO_ADMISSION_RESULT)
                self.assertEqual(admission.decision.disposition.value, disposition)
                self.assertIsNone(event.get_extra(main.SHIO_ACCEPTED_TURN_DISPATCH))
                self.assertIsNone(event.get_extra(main.SHIO_OPPORTUNITY_ATTENTION))
                self.assertEqual(
                    plugin.accepted_turn_authority.trace_metadata()["turn_count"],
                    0,
                )
                self.assertEqual(plugin.group_scenes.scope_count, 0)
                self.assertIsNone(event.get_extra(main.SHIO_AFFECT_STATE_MUTATION))
                self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
