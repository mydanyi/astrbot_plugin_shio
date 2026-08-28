from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

try:
    # unittest discovery imports sibling files as top-level modules. Reuse that
    # exact module name so test_pipeline is never executed twice with two
    # independent FakeEvent/main registries.
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
    AddressDecision,
    AddressEvidence,
    AddressKind,
)
from astrbot_plugin_shio.core.address_resolver import AddressResolutionAuthority
from astrbot_plugin_shio.core.context_assembler import (
    AssembledContext,
    FactSelection,
    ReplyTarget,
)
from astrbot_plugin_shio.core.conversation_ledger import ledger_content_digest
from astrbot_plugin_shio.core.model_input_contract import (
    project_model_identity_prompt_data,
    project_model_messages,
)


class At:
    """Minimal AstrBot structured @ component used by the pipeline fixture."""

    type = "at"

    def __init__(self, qq: str, name: str = "") -> None:
        self.qq = qq
        self.name = name


class Reply:
    """Strict production-shape AstrBot Reply used by TurnEnvelope."""

    type = "reply"

    def __init__(
        self,
        message_id: str,
        sender_id: str,
        message_str: str = "",
        sender_nickname: str = "",
    ) -> None:
        self.id = message_id
        self.sender_id = sender_id
        self.message_str = message_str
        self.sender_nickname = sender_nickname


def _address_extra_key() -> str:
    # Keep the red test useful before the production constant exists. Once wired,
    # the public main-module constant is authoritative.
    return getattr(main, "SHIO_ADDRESS_DECISION", "_shio_address_decision")


def _with_components(event: FakeEvent, *components: object) -> FakeEvent:
    event.get_messages = lambda: list(components)
    return event


class _PlatformHistoryManager:
    def __init__(self) -> None:
        self.rows: list[object] = []

    async def get(self, **_kwargs):
        return list(self.rows)


class AddressPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.runtime_count = 0

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _admit(self, plugin, event: FakeEvent):
        self.assertTrue(plugin.prepare_ingress_candidate(event))
        outputs = [item async for item in plugin.admit_inbound_event(event)]
        self.assertEqual(outputs, [])
        return event.get_extra(main.SHIO_INGRESS_DECISION)

    def _address(self, event: FakeEvent) -> AddressDecision | None:
        return event.get_extra(_address_extra_key())

    def _plugin(self, context=None, config=None):
        self.runtime_count += 1
        FakeStarTools.data_dir = Path(self.temp.name) / f"runtime-{self.runtime_count}"
        return main.ShioPlugin(
            context or FakeContext(FakeProvider([])),
            config or {},
        )

    async def test_admitted_group_turn_has_one_address_with_same_binding(self):
        plugin = self._plugin()
        event = FakeEvent("peer-a", "大家继续吧", group_id="group-address")
        event.message_id = "address-open-group"

        ingress = await self._admit(plugin, event)

        conversation_event = event.get_extra(main.SHIO_CONVERSATION_EVENT)
        address = self._address(event)
        self.assertIsInstance(address, AddressDecision)
        self.assertEqual(address.binding, conversation_event.binding)
        self.assertEqual(address.binding, ingress.binding)
        self.assertIs(address.kind, AddressKind.OPEN_GROUP)

    async def test_r13_terminal_address_release_keeps_small_authority_bounded(self):
        """Terminal turns release only their own canonical address decision.

        A capacity of two makes the previous no-release implementation fail on
        the third otherwise ordinary group turn.  The oracle is deliberately
        about admitted decisions and bounded count, not vault internals.
        """
        plugin = self._plugin()
        authority = AddressResolutionAuthority(max_decisions=2)
        plugin.address_resolution_authority = authority
        plugin.opportunity_attention_authority = main.OpportunityAttentionAuthority(
            plugin.accepted_turn_authority,
            authority,
        )
        for index in range(5):
            event = FakeEvent(
                f"peer-{index}",
                "普通群聊，不要求机器人参与。",
                group_id=f"r13-release-{index}",
            )
            event.message_id = f"r13-release-message-{index}"
            await self._admit(plugin, event)
            decision = self._address(event)
            self.assertIsInstance(decision, AddressDecision)
            self.assertTrue(authority.release(decision))
            self.assertFalse(authority.release(decision))
        metrics = authority.trace_metadata()
        self.assertEqual(metrics["resolved_address_count"], 0)
        self.assertTrue(metrics["resolved_address_bounded"])

    async def test_structured_self_mention_reply_and_natural_vocative_are_direct(self):
        cases = (
            (
                "mention-self",
                "这次请认真看",
                (At("bot-10000"),),
                True,
            ),
            (
                "reply-self",
                "接着回答这个",
                (Reply("bot-message-old", "bot-10000", "旧回复"),),
                False,
            ),
            (
                "emotional-vocative",
                "笨蛋萝卜子，你这次是大修",
                (),
                False,
            ),
        )
        for name, text, components, native_wake in cases:
            with self.subTest(case=name):
                plugin = self._plugin(
                    FakeContext(FakeProvider([])),
                    {"natural_name_wake_aliases": ["萝卜子", "亚托莉"]},
                )
                event = _with_components(
                    FakeEvent("peer-a", text, group_id="group-direct"),
                    *components,
                )
                event.message_id = f"address-{name}"
                event.is_at_or_wake_command = native_wake

                await self._admit(plugin, event)

                address = self._address(event)
                self.assertIsInstance(address, AddressDecision)
                self.assertIs(address.kind, AddressKind.DIRECT_SELF)

    async def test_structured_other_mention_and_reply_are_other_person(self):
        cases = (
            ("mention-other", "小林你怎么看", (At("peer-b"),)),
            (
                "reply-other",
                "我赞同这个说法",
                (Reply("peer-message-old", "peer-b", "另一位群友的消息"),),
            ),
        )
        for name, text, components in cases:
            with self.subTest(case=name):
                plugin = self._plugin()
                event = _with_components(
                    FakeEvent("peer-a", text, group_id="group-other"),
                    *components,
                )
                event.message_id = f"address-{name}"

                await self._admit(plugin, event)

                address = self._address(event)
                self.assertIsInstance(address, AddressDecision)
                self.assertIs(address.kind, AddressKind.OTHER_PERSON)
                record = event.get_extra(main.SHIO_TYPED_INBOUND_RECORD)
                if name == "mention-other":
                    self.assertEqual(len(record.mention_targets), 1)
                    self.assertEqual(
                        record.mention_targets[0].display.status,
                        "unavailable",
                    )
                    self.assertEqual(record.addressing_status, "mentions")
                else:
                    self.assertEqual(
                        record.identity_metadata.referenced_sender.display.status,
                        "unavailable",
                    )
                    self.assertEqual(record.addressing_status, "reply")

    async def test_real_reply_and_multiple_at_flow_to_ledger_scene_and_provider_contract(self):
        plugin = self._plugin()
        event = _with_components(
            FakeEvent("peer-a", "只保留联合正文", group_id="group-joint"),
            Reply(
                "prior-message",
                "peer-b",
                "引用正文",
                sender_nickname="引用测试者",
            ),
            At("peer-c", "提及测试甲"),
            At("peer-d", "提及测试乙"),
        )
        event.get_sender_name = lambda: "发言测试者"
        event.message_id = "address-joint"

        await self._admit(plugin, event)

        record = event.get_extra(main.SHIO_TYPED_INBOUND_RECORD)
        scene = event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT)
        topic = scene.public_topics[-1]
        self.assertEqual(record.content, "只保留联合正文")
        self.assertEqual(record.display_name, "发言测试者")
        self.assertEqual(record.referenced_display_name, "引用测试者")
        self.assertEqual(
            [target.display.value for target in record.mention_targets],
            ["提及测试甲", "提及测试乙"],
        )
        self.assertEqual(topic.identity_metadata, record.identity_metadata)
        self.assertEqual(topic.reply_to_message_id, "prior-message")

        target = ReplyTarget(
            message_id="later-message",
            sender_key=main.build_sender_key(record.scope_key, "peer-z"),
            session_id="group-joint",
            scope_key=record.scope_key,
            content_digest=ledger_content_digest("后续问题"),
            source_kind="inbound",
            referenced_message_id="",
            degradation_reasons=(),
        )
        assembled = AssembledContext(
            reply_target=target,
            reference=None,
            planner_records=(record,),
            replyer_thread=(),
            public_background=(record,),
            fact_selection=FactSelection((), (), (), ()),
        )
        messages = project_model_messages(assembled)
        provider_contexts = [message.provider_dict() for message in messages]
        identity_data = project_model_identity_prompt_data(messages)

        self.assertEqual(
            provider_contexts,
            [{"role": "user", "content": "只保留联合正文"}],
        )
        addressing = identity_data["history"][0]["addressing"]
        self.assertEqual(addressing["kind"], "reply_and_mentions")
        self.assertEqual(
            [target["display_name"] for target in addressing["mention_targets"]],
            ["提及测试甲", "提及测试乙"],
        )
        # Event admission supplies raw IDs to system-owned identity JSON only.
        for raw_id in ("peer-a", "peer-b", "peer-c", "peer-d"):
            self.assertIn(raw_id, repr(identity_data))
            self.assertNotIn(raw_id, repr(provider_contexts))
        self.assertNotIn("group-joint", repr(provider_contexts))
        self.assertNotIn("group-joint", repr(provider_contexts))

    async def test_current_reply_and_multiple_at_reach_same_turn_composer_contract(self):
        """The current turn must not borrow a later turn to expose addressing."""

        plugin = self._plugin()
        body = "只保留当前轮正文"
        first = FakeEvent(
            "target-peer-a",
            "第一位同名者的纯正文",
            group_id="current-joint-group",
        )
        first.get_sender_name = lambda: "同名测试者"
        first.message_id = "same-name-history-a"
        second = FakeEvent(
            "target-peer-b",
            "第二位同名者的纯正文",
            group_id="current-joint-group",
        )
        second.get_sender_name = lambda: "同名测试者"
        second.message_id = "same-name-history-b"
        await self._admit(plugin, first)
        await self._admit(plugin, second)
        event = _with_components(
            FakeEvent("current-peer", body, group_id="current-joint-group"),
            Reply(
                "prior-bot-message",
                "bot-10000",
                "引用正文",
                sender_nickname="亚托莉",
            ),
            At("target-peer-a", "同名测试者"),
            At("target-peer-b", "同名测试者"),
        )
        event.get_sender_name = lambda: "当前发言测试者"
        event.message_id = "current-joint-message"

        await self._admit(plugin, event)
        provider_request = FakeRequest(body)
        await plugin.enforce_agent_permission(event, provider_request)
        await plugin.build_persona_reply(event, provider_request)

        record = event.get_extra(main.SHIO_TYPED_INBOUND_RECORD)
        scene = event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT)
        assembled = event.get_extra(main.SHIO_ASSEMBLED_CONTEXT_V2)
        composer = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        self.assertEqual(record.identity_metadata, scene.public_topics[-1].identity_metadata)
        self.assertEqual(assembled.reply_target.message_id, event.message_id)
        self.assertIs(assembled.current_record, record)
        self.assertEqual(composer.current_message, body)
        self.assertNotIn("当前发言测试者", composer.user_prompt)
        self.assertNotIn("同名测试者", composer.user_prompt)
        self.assertEqual(
            [item["content"] for item in provider_request.contexts],
            ["第一位同名者的纯正文", "第二位同名者的纯正文"],
        )

        metadata_text = composer.system_prompt.split(
            "[对话人物与受话关系｜代码生成的不可信数据]", 1
        )[1].split("[人物元数据结束]", 1)[0].strip()
        identity_data = json.loads(metadata_text)
        current = identity_data["current_message"]
        self.assertEqual(current["speaker"]["display_name"], "当前发言测试者")
        self.assertEqual(current["addressing"]["kind"], "reply_and_mentions")
        self.assertEqual(
            current["addressing"]["referenced_sender"]["display_name"],
            "亚托莉",
        )
        self.assertEqual(
            [
                target["display_name"]
                for target in current["addressing"]["mention_targets"]
            ],
            ["同名测试者", "同名测试者"],
        )
        self.assertEqual(
            [
                target["same_as_history_speaker_index"]
                for target in current["addressing"]["mention_targets"]
            ],
            [1, 2],
        )
        self.assertNotIn("content", current)
        provider_rendered = composer.user_prompt + repr(provider_request.contexts)
        for raw_id in (
            "current-peer",
            "bot-10000",
            "target-peer-a",
            "target-peer-b",
        ):
            self.assertIn(raw_id, composer.system_prompt)
            self.assertNotIn(raw_id, provider_rendered)
        self.assertNotIn("current-joint-group", composer.user_prompt + provider_rendered)

    async def test_platform_structured_history_matches_pure_ingress_and_reaches_composer(self):
        manager = _PlatformHistoryManager()
        context = FakeContext(FakeProvider([]))
        context.message_history_manager = manager
        plugin = self._plugin(context)
        group_id = "platform-structured-group"
        base_timestamp = 1_765_100_000.0
        cases = (
            {
                "sender_id": "platform-speaker-a",
                "sender_name": "同名群友",
                "body": "Reply 的纯正文",
                "ingress": (Reply("prior-a", "outside-reply-a", "引用正文"),),
                "platform": (
                    {
                        "type": "reply",
                        "sender_id": "outside-reply-a",
                        "sender_name": "真实引用者甲",
                        "text": "引用正文",
                    },
                ),
            },
            {
                "sender_id": "platform-speaker-b",
                "sender_name": "第二位同名群友",
                "body": "单 At 的纯正文",
                "ingress": (At("platform-speaker-a"),),
                "platform": (
                    {
                        "type": "at",
                        "qq": "platform-speaker-a",
                        "name": "同名群友",
                    },
                ),
            },
            {
                "sender_id": "platform-speaker-c",
                "sender_name": "第三位真实群友",
                "body": "多 At 的纯正文",
                "ingress": (
                    At("platform-speaker-a"),
                    At("platform-speaker-b"),
                ),
                "platform": (
                    {
                        "type": "at",
                        "qq": "platform-speaker-b",
                        "name": "第二位同名群友",
                    },
                    {
                        "type": "at",
                        "qq": "platform-speaker-a",
                        "name": "同名群友",
                    },
                ),
            },
            {
                "sender_id": "platform-speaker-d",
                "sender_name": "第四位真实群友",
                "body": "字面 @姓名 和 [回复 普通方括号] 仍是纯正文",
                "ingress": (
                    Reply("prior-d", "platform-speaker-a", "引用正文"),
                    At("platform-speaker-b"),
                    At("platform-speaker-c"),
                ),
                "platform": (
                    {
                        "type": "reply",
                        "sender_id": "platform-speaker-a",
                        "sender_name": "同名群友",
                        "text": "引用正文",
                    },
                    {
                        "type": "at",
                        "qq": "platform-speaker-b",
                        "name": "同名群友",
                    },
                    {
                        "type": "at",
                        "qq": "platform-speaker-c",
                        "name": "第三位真实群友",
                    },
                ),
            },
        )
        accepted_message_ids = []
        for index, case in enumerate(cases):
            event = _with_components(
                FakeEvent(case["sender_id"], case["body"], group_id=group_id),
                *case["ingress"],
            )
            event.created_at = base_timestamp + index * 10
            event.message_id = f"platform-accepted-{index}"
            event.get_sender_name = lambda value=case["sender_id"]: value
            await self._admit(plugin, event)
            record = event.get_extra(main.SHIO_TYPED_INBOUND_RECORD)
            self.assertEqual(record.content, case["body"])
            accepted_message_ids.append(event.message_id)
            manager.rows.append(
                SimpleNamespace(
                    id=100 + index,
                    platform_id=event.get_platform_id(),
                    user_id=event.unified_msg_origin,
                    sender_id=case["sender_id"],
                    sender_name=case["sender_name"],
                    created_at=datetime.fromtimestamp(
                        event.created_at,
                        tz=timezone.utc,
                    ),
                    content={
                        "type": "user",
                        "message": [
                            *case["platform"],
                            {"type": "plain", "text": case["body"]},
                        ],
                    },
                )
            )

        current = FakeEvent(
            "platform-current",
            "请根据上面的纯正文回应",
            group_id=group_id,
        )
        current.created_at = base_timestamp + 50
        current.message_id = "platform-current-message"
        current.is_at_or_wake_command = True
        current.set_extra("_current_platform_message_history_id", 999)
        await self._admit(plugin, current)
        provider_request = FakeRequest(current.message)
        provider_request.contexts = []
        await plugin.enforce_agent_permission(current, provider_request)
        await plugin.build_persona_reply(current, provider_request)

        platform_history = current.get_extra(main.SHIO_PLATFORM_GROUP_HISTORY)
        assembled = current.get_extra(main.SHIO_ASSEMBLED_CONTEXT_V2)
        composer = current.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        self.assertEqual(
            list(platform_history.matched_ledger_message_ids),
            accepted_message_ids,
        )
        self.assertEqual(
            [item["content"] for item in provider_request.contexts],
            [case["body"] for case in cases],
        )
        self.assertEqual(
            [record.content for record in platform_history.records],
            [case["body"] for case in cases],
        )
        self.assertEqual(
            [record.source_id for record in assembled.planner_records if record.source_id in accepted_message_ids],
            accepted_message_ids,
        )
        metadata_text = composer.system_prompt.split(
            "[对话人物与受话关系｜代码生成的不可信数据]", 1
        )[1].split("[人物元数据结束]", 1)[0].strip()
        identity_data = json.loads(metadata_text)
        history = identity_data["history"]
        self.assertEqual(
            [item["speaker"]["display_name"] for item in history],
            [case["sender_name"] for case in cases],
        )
        self.assertEqual(
            [item["addressing"]["kind"] for item in history],
            ["reply", "mentions", "mentions", "reply_and_mentions"],
        )
        self.assertEqual(
            history[0]["addressing"]["referenced_sender"]["display_name"],
            "真实引用者甲",
        )
        self.assertEqual(
            [target["display_name"] for target in history[2]["addressing"]["mention_targets"]],
            ["同名群友", "第二位同名群友"],
        )
        self.assertEqual(
            history[3]["addressing"]["referenced_sender"]["display_name"],
            "同名群友",
        )
        self.assertEqual(composer.current_message, current.message)
        rendered = composer.system_prompt + composer.user_prompt + repr(
            provider_request.contexts
        )
        # Imported legacy platform rows have no separately verified raw ID;
        # they must remain unknown.  The current envelope does have one.
        self.assertIn("platform-current", composer.system_prompt)
        for raw_id in (
            "platform-speaker-a", "platform-speaker-b", "platform-speaker-c",
            "platform-speaker-d", "outside-reply-a",
        ):
            self.assertNotIn(raw_id, composer.user_prompt + repr(provider_request.contexts))
        self.assertNotIn(group_id, composer.user_prompt + repr(provider_request.contexts))

    async def test_protected_ids_degrade_across_ingress_history_and_composer(self):
        """Every request participant/scope ID protects every display field."""

        manager = _PlatformHistoryManager()
        context = FakeContext(FakeProvider([]))
        context.message_history_manager = manager
        plugin = self._plugin(context)
        group_id = "privacy-group-r5"
        bot_id = "bot-10000"
        peer_a = "privacy-peer-a-r5"
        peer_b = "privacy-peer-b-r5"
        current_id = "privacy-current-r5"
        base_timestamp = 1_765_200_000.0

        first = _with_components(
            FakeEvent(peer_a, "历史纯正文甲", group_id=group_id),
            Reply(
                "privacy-prior-r5",
                peer_b,
                "引用正文",
                sender_nickname=peer_b,
            ),
        )
        first.get_sender_name = lambda: peer_a
        first.message_id = "privacy-accepted-a-r5"
        first.created_at = base_timestamp
        await self._admit(plugin, first)

        second = FakeEvent(peer_b, "历史纯正文乙", group_id=group_id)
        second.get_sender_name = lambda: peer_b
        second.message_id = "privacy-accepted-b-r5"
        second.created_at = base_timestamp + 10
        await self._admit(plugin, second)

        manager.rows.extend(
            (
                SimpleNamespace(
                    id=201,
                    platform_id=first.get_platform_id(),
                    user_id=first.unified_msg_origin,
                    sender_id=peer_a,
                    sender_name=f"装饰({peer_b})",
                    created_at=datetime.fromtimestamp(
                        first.created_at,
                        tz=timezone.utc,
                    ),
                    content={
                        "type": "user",
                        "message": [
                            {
                                "type": "reply",
                                "sender_id": peer_b,
                                "sender_name": bot_id,
                                "text": "引用正文",
                            },
                            {"type": "plain", "text": first.message},
                        ],
                    },
                ),
                SimpleNamespace(
                    id=202,
                    platform_id=second.get_platform_id(),
                    user_id=second.unified_msg_origin,
                    sender_id=peer_b,
                    sender_name=group_id,
                    created_at=datetime.fromtimestamp(
                        second.created_at,
                        tz=timezone.utc,
                    ),
                    content={
                        "type": "user",
                        "message": [
                            {"type": "plain", "text": second.message},
                        ],
                    },
                ),
            )
        )

        current = _with_components(
            FakeEvent(current_id, "当前纯正文", group_id=group_id),
            Reply(
                second.message_id,
                peer_b,
                "引用正文",
                sender_nickname=group_id,
            ),
            At(peer_a, f"对象({peer_a})"),
        )
        current.get_sender_name = lambda: peer_a
        current.message_id = "privacy-current-message-r5"
        current.created_at = base_timestamp + 20
        current.is_at_or_wake_command = True
        current.set_extra("_current_platform_message_history_id", 999)
        await self._admit(plugin, current)

        current_record = current.get_extra(main.SHIO_TYPED_INBOUND_RECORD)
        self.assertEqual(current_record.content, "当前纯正文")
        self.assertFalse(current_record.identity_metadata.display.available)
        self.assertFalse(
            current_record.identity_metadata.referenced_sender.display.available
        )
        self.assertFalse(current_record.mention_targets[0].display.available)

        provider_request = FakeRequest(current.message)
        await plugin.enforce_agent_permission(current, provider_request)
        await plugin.build_persona_reply(current, provider_request)

        platform_history = current.get_extra(main.SHIO_PLATFORM_GROUP_HISTORY)
        self.assertEqual(
            list(platform_history.matched_ledger_message_ids),
            [first.message_id, second.message_id],
        )
        self.assertEqual(
            [record.content for record in platform_history.records],
            [first.message, second.message],
        )
        self.assertTrue(
            all(
                not record.identity_metadata.display.available
                for record in platform_history.records
            )
        )
        self.assertFalse(
            platform_history.records[0]
            .identity_metadata.referenced_sender.display.available
        )

        composer = current.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        payload = composer.system_prompt.split(
            "[对话人物与受话关系｜代码生成的不可信数据]\n", 1
        )[1].split("\n[人物元数据结束]", 1)[0]
        identity = json.loads(payload)
        history = identity["history"]
        current_projection = identity["current_message"]
        self.assertEqual(
            [item["speaker"]["display_name_available"] for item in history],
            [False, False],
        )
        self.assertFalse(
            history[0]["addressing"]["referenced_sender"][
                "display_name_available"
            ]
        )
        self.assertEqual(
            history[0]["addressing"]["referenced_sender"][
                "same_as_history_speaker_index"
            ],
            2,
        )
        self.assertFalse(current_projection["speaker"]["display_name_available"])
        self.assertFalse(
            current_projection["addressing"]["referenced_sender"][
                "display_name_available"
            ]
        )
        self.assertFalse(
            current_projection["addressing"]["mention_targets"][0][
                "display_name_available"
            ]
        )
        self.assertEqual(
            current_projection["addressing"]["mention_targets"][0][
                "same_as_history_speaker_index"
            ],
            1,
        )
        self.assertEqual(
            [item["content"] for item in provider_request.contexts],
            [first.message, second.message],
        )
        self.assertEqual(composer.current_message, current.message)
        rendered = composer.user_prompt + repr(provider_request.contexts)
        for raw_id in (peer_a, peer_b, current_id):
            self.assertIn(raw_id, composer.system_prompt)
            self.assertNotIn(raw_id, composer.user_prompt + repr(provider_request.contexts))
        self.assertNotIn(bot_id, rendered)
        self.assertNotIn(group_id, composer.user_prompt + repr(provider_request.contexts))
        self.assertNotIn("|user:", rendered)

    async def test_unmatched_platform_relation_ids_are_request_wide_deny_only(self):
        """Untrusted row relation IDs may deny displays, never identify people."""

        manager = _PlatformHistoryManager()
        context = FakeContext(FakeProvider([]))
        context.message_history_manager = manager
        plugin = self._plugin(context)
        group_id = "deny-only-group-r6"
        base_timestamp = 1_765_300_000.0
        reply_outsider = "deny-reply-outsider-r6"
        at_outsider = "deny-at-outsider-r6"
        at_speaker_outsider = "deny-at-speaker-r6"
        short_outsider = "q7"
        cases = (
            {
                "sender_id": "deny-speaker-reply-source-r6",
                "body": "Reply raw evidence 只提供拒绝字面",
                "ingress": (),
                "sender_name": "第一位可信发言者",
                "platform": (
                    {
                        "type": "reply",
                        "sender_id": reply_outsider,
                        "sender_name": "不得授信的 Reply 姓名",
                        "text": "引用正文",
                    },
                ),
            },
            {
                "sender_id": "deny-speaker-reply-cross-r6",
                "body": "另一行显示名也受请求级保护",
                "ingress": (),
                "sender_name": f"装饰({reply_outsider.upper()})",
                "platform": (),
            },
            {
                "sender_id": "deny-speaker-at-relation-r6",
                "body": "At raw evidence 不能成为另一目标姓名",
                "ingress": (
                    At("deny-trusted-target-r6", "可信目标"),
                    At("deny-unavailable-target-r6"),
                ),
                "sender_name": "第二位可信发言者",
                "platform": (
                    {
                        "type": "at",
                        "qq": at_outsider,
                        "name": "不得创建的 outsider",
                    },
                    {
                        "type": "at",
                        "qq": "deny-unavailable-target-r6",
                        "name": at_outsider.upper(),
                    },
                ),
            },
            {
                "sender_id": "deny-speaker-at-cross-r6",
                "body": "At raw evidence 也保护 speaker",
                "ingress": (),
                "sender_name": f"成员({at_speaker_outsider})",
                "platform": (
                    {
                        "type": "at",
                        "qq": at_speaker_outsider,
                        "name": "未匹配目标",
                    },
                ),
            },
            {
                "sender_id": "deny-speaker-short-delimited-r6",
                "body": "短 ID 有分隔符时拒绝",
                "ingress": (),
                "sender_name": f"[{short_outsider.upper()}]",
                "platform": (
                    {
                        "type": "reply",
                        "sender_id": short_outsider,
                        "sender_name": "短 ID 未匹配目标",
                        "text": "引用正文",
                    },
                ),
            },
            {
                "sender_id": "deny-speaker-short-natural-r6",
                "body": "短 ID 自然包含不能误杀",
                "ingress": (),
                "sender_name": f"{short_outsider.upper()}nova",
                "platform": (),
            },
            {
                "sender_id": "deny-speaker-duplicate-r6",
                "body": "重复 relation ID 保留 unavailable",
                "ingress": (At("deny-duplicate-target-r6"),),
                "sender_name": "重复证据发言者",
                "platform": (
                    {
                        "type": "at",
                        "qq": "deny-duplicate-target-r6",
                        "name": "重复候选甲",
                    },
                    {
                        "type": "at",
                        "qq": "deny-duplicate-target-r6",
                        "name": "重复候选乙",
                    },
                ),
            },
            {
                "sender_id": "deny-speaker-missing-r6",
                "body": "plain @字面 和 [回复 普通字面] 完整保留",
                "ingress": (At("deny-missing-target-r6"),),
                "sender_name": "缺失证据发言者",
                "platform": (
                    {
                        "type": "at",
                        "name": "缺 ID 不得按位置猜名",
                    },
                ),
            },
        )

        accepted_message_ids = []
        for index, case in enumerate(cases):
            event = _with_components(
                FakeEvent(case["sender_id"], case["body"], group_id=group_id),
                *case["ingress"],
            )
            event.created_at = base_timestamp + index * 10
            event.message_id = f"deny-only-accepted-r6-{index}"
            event.get_sender_name = lambda: ""
            await self._admit(plugin, event)
            accepted = event.get_extra(main.SHIO_TYPED_INBOUND_RECORD)
            self.assertEqual(accepted.content, case["body"])
            accepted_message_ids.append(event.message_id)
            manager.rows.append(
                SimpleNamespace(
                    id=301 + index,
                    platform_id=event.get_platform_id(),
                    user_id=event.unified_msg_origin,
                    sender_id=case["sender_id"],
                    sender_name=case["sender_name"],
                    created_at=datetime.fromtimestamp(
                        event.created_at,
                        tz=timezone.utc,
                    ),
                    content={
                        "type": "user",
                        "message": [
                            *case["platform"],
                            {"type": "plain", "text": case["body"]},
                        ],
                    },
                )
            )

        current = FakeEvent(
            "deny-current-r6",
            "请按上面的纯正文继续",
            group_id=group_id,
        )
        current.created_at = base_timestamp + 100
        current.message_id = "deny-only-current-r6"
        current.is_at_or_wake_command = True
        current.set_extra("_current_platform_message_history_id", 999)
        await self._admit(plugin, current)
        provider_request = FakeRequest(current.message)
        provider_request.contexts = []
        await plugin.enforce_agent_permission(current, provider_request)
        await plugin.build_persona_reply(current, provider_request)

        platform_history = current.get_extra(main.SHIO_PLATFORM_GROUP_HISTORY)
        assembled = current.get_extra(main.SHIO_ASSEMBLED_CONTEXT_V2)
        composer = current.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        self.assertEqual(
            list(platform_history.matched_ledger_message_ids),
            accepted_message_ids,
        )
        self.assertEqual(
            [record.content for record in platform_history.records],
            [case["body"] for case in cases],
        )
        self.assertEqual(
            [item["content"] for item in provider_request.contexts],
            [case["body"] for case in cases],
        )
        self.assertEqual(
            [
                record.source_id
                for record in assembled.planner_records
                if record.source_id in accepted_message_ids
            ],
            accepted_message_ids,
        )

        metadata_text = composer.system_prompt.split(
            "[对话人物与受话关系｜代码生成的不可信数据]", 1
        )[1].split("[人物元数据结束]", 1)[0].strip()
        history = json.loads(metadata_text)["history"]
        self.assertEqual(len(history), len(cases))
        self.assertEqual(history[0]["speaker"]["display_name"], "第一位可信发言者")
        self.assertFalse(history[1]["speaker"]["display_name_available"])
        self.assertEqual(history[2]["addressing"]["kind"], "mentions")
        relation_targets = history[2]["addressing"]["mention_targets"]
        self.assertEqual(len(relation_targets), 2)
        self.assertEqual(relation_targets[0]["display_name"], "可信目标")
        self.assertFalse(relation_targets[1]["display_name_available"])
        self.assertFalse(history[3]["speaker"]["display_name_available"])
        self.assertFalse(history[4]["speaker"]["display_name_available"])
        self.assertEqual(history[5]["speaker"]["display_name"], "Q7nova")
        self.assertFalse(
            history[6]["addressing"]["mention_targets"][0][
                "display_name_available"
            ]
        )
        self.assertFalse(
            history[7]["addressing"]["mention_targets"][0][
                "display_name_available"
            ]
        )
        self.assertEqual(
            [item["addressing"]["kind"] for item in history],
            [
                "open_group",
                "open_group",
                "mentions",
                "open_group",
                "open_group",
                "open_group",
                "mentions",
                "mentions",
            ],
        )
        self.assertEqual(composer.current_message, current.message)

        surfaces = (composer.user_prompt, repr(provider_request.contexts))
        for surface in surfaces:
            folded = surface.casefold()
            for protected in (
                reply_outsider,
                at_outsider,
                at_speaker_outsider,
                group_id,
                "|user:",
            ):
                self.assertNotIn(protected.casefold(), folded)
            self.assertNotIn(f"[{short_outsider}]", folded)
            self.assertNotIn(f'"{short_outsider}"', folded)
            self.assertNotIn(f"|user:{short_outsider}", folded)

    async def test_current_composer_addressing_variants_degrade_without_guessing(self):
        cases = (
            {
                "name": "reply",
                "sender_id": "current-reply",
                "sender_name": "回复发言者",
                "components": (
                    Reply(
                        "prior-reply",
                        "bot-10000",
                        "引用正文",
                        sender_nickname="亚托莉",
                    ),
                ),
                "native_wake": False,
                "kind": "reply",
                "referenced_name": "亚托莉",
                "mention_names": [],
                "raw_ids": ("current-reply", "bot-10000"),
            },
            {
                "name": "single_at",
                "sender_id": "current-single-at",
                "sender_name": "单提及发言者",
                "components": (At("bot-10000", "亚托莉"),),
                "native_wake": True,
                "kind": "mentions",
                "referenced_name": None,
                "mention_names": ["亚托莉"],
                "raw_ids": ("current-single-at", "bot-10000"),
            },
            {
                "name": "multiple_at",
                "sender_id": "current-multiple-at",
                "sender_name": "多提及发言者",
                "components": (
                    At("bot-10000", "亚托莉"),
                    At("multi-target-a", "多目标甲"),
                    At("multi-target-b", "多目标乙"),
                ),
                "native_wake": True,
                "kind": "mentions",
                "referenced_name": None,
                "mention_names": ["亚托莉", "多目标甲", "多目标乙"],
                "raw_ids": (
                    "current-multiple-at",
                    "bot-10000",
                    "multi-target-a",
                    "multi-target-b",
                ),
            },
            {
                "name": "reply_single_at",
                "sender_id": "current-reply-single-at",
                "sender_name": "回复单提及发言者",
                "components": (
                    Reply(
                        "prior-reply-single-at",
                        "reply-target-single-at",
                        "引用正文",
                        sender_nickname="同名受话者",
                    ),
                    At("mention-target-single-at", "同名受话者"),
                ),
                "native_wake": True,
                "kind": "reply_and_mentions",
                "referenced_name": "同名受话者",
                "mention_names": ["同名受话者"],
                "raw_ids": (
                    "current-reply-single-at",
                    "reply-target-single-at",
                    "mention-target-single-at",
                ),
            },
            {
                "name": "reply_missing_nickname",
                "sender_id": "current-reply-missing-name",
                "sender_name": "缺回复昵称发言者",
                "components": (
                    Reply(
                        "prior-reply-missing-name",
                        "reply-target-missing-name",
                        "引用正文",
                    ),
                ),
                "native_wake": True,
                "kind": "reply",
                "referenced_name": None,
                "mention_names": [],
                "raw_ids": (
                    "current-reply-missing-name",
                    "reply-target-missing-name",
                ),
                "referenced_unavailable": True,
            },
            {
                "name": "missing_reply_target",
                "sender_id": "current-missing-target",
                "sender_name": "缺目标发言者",
                "components": (Reply("prior-missing-target", "", "引用正文"),),
                "native_wake": True,
                "kind": "reply_target_unavailable",
                "referenced_name": None,
                "mention_names": [],
                "raw_ids": ("current-missing-target",),
            },
            {
                "name": "account_name_fallback",
                "sender_id": "account-current-fallback",
                "sender_name": "account-current-fallback",
                "components": (
                    Reply(
                        "prior-account-fallback",
                        "account-reply-fallback",
                        "引用正文",
                        sender_nickname="account-reply-fallback",
                    ),
                    At("account-at-fallback", "account-at-fallback"),
                ),
                "native_wake": True,
                "kind": "reply_and_mentions",
                "referenced_name": None,
                "mention_names": [],
                "raw_ids": (
                    "account-current-fallback",
                    "account-reply-fallback",
                    "account-at-fallback",
                ),
                "unavailable_account_names": True,
            },
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case["name"]):
                plugin = self._plugin()
                body = f"当前变体纯正文 {index}"
                group_id = f"current-variant-group-{index}"
                event = _with_components(
                    FakeEvent(case["sender_id"], body, group_id=group_id),
                    *case["components"],
                )
                event.get_sender_name = lambda value=case["sender_name"]: value
                event.message_id = f"current-variant-message-{index}"
                event.is_at_or_wake_command = case["native_wake"]

                await self._admit(plugin, event)
                provider_request = FakeRequest(body)
                await plugin.enforce_agent_permission(event, provider_request)
                await plugin.build_persona_reply(event, provider_request)

                composer = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
                metadata_text = composer.system_prompt.split(
                    "[对话人物与受话关系｜代码生成的不可信数据]", 1
                )[1].split("[人物元数据结束]", 1)[0].strip()
                current = json.loads(metadata_text)["current_message"]
                self.assertEqual(current["role"], "user")
                self.assertNotIn("content", current)
                self.assertEqual(current["addressing"]["kind"], case["kind"])
                self.assertEqual(composer.current_message, body)
                self.assertEqual(provider_request.contexts, [])
                self.assertNotIn(case["sender_name"], composer.user_prompt)

                referenced = current["addressing"]["referenced_sender"]
                if case["referenced_name"] is None:
                    if case.get("unavailable_account_names") or case.get(
                        "referenced_unavailable"
                    ):
                        self.assertIsNotNone(referenced)
                        self.assertFalse(referenced["display_name_available"])
                        self.assertNotIn("display_name", referenced)
                    else:
                        self.assertIsNone(referenced)
                else:
                    self.assertEqual(
                        referenced["display_name"],
                        case["referenced_name"],
                    )

                mentions = current["addressing"]["mention_targets"]
                if case.get("unavailable_account_names"):
                    self.assertEqual(len(mentions), 1)
                    self.assertFalse(mentions[0]["display_name_available"])
                    self.assertNotIn("display_name", mentions[0])
                    self.assertFalse(current["speaker"]["display_name_available"])
                    self.assertNotIn("display_name", current["speaker"])
                else:
                    self.assertEqual(
                        [target["display_name"] for target in mentions],
                        case["mention_names"],
                    )

                provider_rendered = composer.user_prompt + repr(provider_request.contexts)
                for raw_id in case["raw_ids"]:
                    self.assertIn(raw_id, composer.system_prompt)
                    self.assertNotIn(raw_id, provider_rendered)
                self.assertNotIn(group_id, provider_rendered)

    async def test_reply_nickname_priority_and_legacy_fallback_are_explicit(self):
        cases = (
            {
                "name": "production_nickname_wins",
                "sender_id": "reply-priority-a",
                "sender_nickname": "平台真实昵称",
                "sender_name": "旧 sender_name 不应覆盖",
                "legacy_name": "旧 name 不应覆盖",
                "expected": "平台真实昵称",
            },
            {
                "name": "sender_name_compatibility",
                "sender_id": "reply-priority-b",
                "sender_nickname": "",
                "sender_name": "兼容 sender_name",
                "legacy_name": "更旧 name",
                "expected": "兼容 sender_name",
            },
            {
                "name": "name_compatibility",
                "sender_id": "reply-priority-c",
                "sender_nickname": "",
                "sender_name": "",
                "legacy_name": "兼容 name",
                "expected": "兼容 name",
            },
            {
                "name": "account_literal_stops_fallback",
                "sender_id": "reply-priority-d",
                "sender_nickname": "reply-priority-d",
                "sender_name": "不得借旧字段猜名",
                "legacy_name": "也不得使用",
                "expected": None,
            },
        )
        block_start = "[对话人物与受话关系｜代码生成的不可信数据]"
        block_end = "[人物元数据结束]"
        for index, case in enumerate(cases):
            with self.subTest(case=case["name"]):
                plugin = self._plugin()
                reply = Reply(
                    f"priority-prior-{index}",
                    case["sender_id"],
                    "引用正文",
                    sender_nickname=case["sender_nickname"],
                )
                # Compatibility attributes are deliberately confined to this
                # test; the shared Reply fixture remains strict production-shape.
                reply.sender_name = case["sender_name"]
                reply.name = case["legacy_name"]
                body = f"优先级正文 {index}"
                event = _with_components(
                    FakeEvent(
                        f"priority-current-{index}",
                        body,
                        group_id=f"priority-group-{index}",
                    ),
                    reply,
                )
                event.get_sender_name = lambda: "当前优先级发言者"
                event.message_id = f"priority-current-message-{index}"
                event.is_at_or_wake_command = True

                await self._admit(plugin, event)
                provider_request = FakeRequest(body)
                await plugin.enforce_agent_permission(event, provider_request)
                await plugin.build_persona_reply(event, provider_request)

                composer = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
                payload = composer.system_prompt.split(
                    block_start + "\n", 1
                )[1].split("\n" + block_end, 1)[0]
                referenced = json.loads(payload)["current_message"][
                    "addressing"
                ]["referenced_sender"]
                if case["expected"] is None:
                    self.assertFalse(referenced["display_name_available"])
                    self.assertNotIn("display_name", referenced)
                    self.assertNotIn(case["sender_name"], composer.system_prompt)
                    self.assertNotIn(case["legacy_name"], composer.system_prompt)
                else:
                    self.assertEqual(
                        referenced["display_name"],
                        case["expected"],
                    )
                self.assertEqual(composer.current_message, body)
                self.assertEqual(provider_request.contexts, [])
                self.assertIn(case["sender_id"], composer.system_prompt)
                self.assertNotIn(case["sender_id"], composer.user_prompt)
                self.assertNotIn(f"priority-group-{index}", composer.user_prompt)

    async def test_reply_and_at_whitespace_fallback_priority_is_explicit(self):
        cases = (
            {
                "name": "whitespace_uses_next_nonempty",
                "reply_id": "fallback-reply-a-r5",
                "reply_fields": (" \t ", "Reply 有效 fallback", "Reply 旧 fallback"),
                "reply_expected": "Reply 有效 fallback",
                "at_id": "fallback-at-a-r5",
                "at_fields": (" \n ", "At 有效 fallback"),
                "at_expected": "At 有效 fallback",
            },
            {
                "name": "all_missing_stays_unavailable",
                "reply_id": "fallback-reply-b-r5",
                "reply_fields": (" \t ", "\n", ""),
                "reply_expected": None,
                "at_id": "fallback-at-b-r5",
                "at_fields": (" \t ", "\n"),
                "at_expected": None,
            },
            {
                "name": "normal_primary_wins",
                "reply_id": "fallback-reply-c-r5",
                "reply_fields": ("Reply 主字段", "Reply 次字段", "Reply 末字段"),
                "reply_expected": "Reply 主字段",
                "at_id": "fallback-at-c-r5",
                "at_fields": ("At 主字段", "At 次字段"),
                "at_expected": "At 主字段",
            },
            {
                "name": "raw_primary_stops_fallback",
                "reply_id": "fallback-reply-d-r5",
                "reply_fields": (
                    "fallback-reply-d-r5",
                    "不得使用 Reply fallback",
                    "也不得使用 Reply fallback",
                ),
                "reply_expected": None,
                "at_id": "fallback-at-d-r5",
                "at_fields": (
                    "fallback-at-d-r5",
                    "不得使用 At fallback",
                ),
                "at_expected": None,
            },
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case["name"]):
                plugin = self._plugin()
                reply = Reply(
                    f"fallback-prior-{index}",
                    case["reply_id"],
                    "引用正文",
                    sender_nickname=case["reply_fields"][0],
                )
                reply.sender_name = case["reply_fields"][1]
                reply.name = case["reply_fields"][2]
                at = At(case["at_id"], case["at_fields"][0])
                at.display_name = case["at_fields"][1]
                body = f"fallback 纯正文 {index}"
                group_id = f"fallback-group-r5-{index}"
                event = _with_components(
                    FakeEvent(
                        f"fallback-current-r5-{index}",
                        body,
                        group_id=group_id,
                    ),
                    reply,
                    at,
                )
                event.get_sender_name = lambda: "fallback 当前发言者"
                event.message_id = f"fallback-current-message-r5-{index}"
                event.is_at_or_wake_command = True

                await self._admit(plugin, event)
                provider_request = FakeRequest(body)
                await plugin.enforce_agent_permission(event, provider_request)
                await plugin.build_persona_reply(event, provider_request)

                composer = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
                payload = composer.system_prompt.split(
                    "[对话人物与受话关系｜代码生成的不可信数据]\n", 1
                )[1].split("\n[人物元数据结束]", 1)[0]
                current_projection = json.loads(payload)["current_message"]
                referenced = current_projection["addressing"][
                    "referenced_sender"
                ]
                mention = current_projection["addressing"]["mention_targets"][0]
                if case["reply_expected"] is None:
                    self.assertFalse(referenced["display_name_available"])
                    self.assertNotIn("display_name", referenced)
                else:
                    self.assertEqual(
                        referenced["display_name"],
                        case["reply_expected"],
                    )
                if case["at_expected"] is None:
                    self.assertFalse(mention["display_name_available"])
                    self.assertNotIn("display_name", mention)
                else:
                    self.assertEqual(mention["display_name"], case["at_expected"])
                self.assertEqual(composer.current_message, body)
                self.assertEqual(provider_request.contexts, [])
                self.assertIn(case["reply_id"], composer.system_prompt)
                self.assertIn(case["at_id"], composer.system_prompt)
                self.assertNotIn(case["reply_id"], composer.user_prompt)
                self.assertNotIn(case["at_id"], composer.user_prompt)
                self.assertNotIn(group_id, composer.user_prompt)

    async def test_account_id_fallbacks_degrade_instead_of_becoming_names(self):
        plugin = self._plugin()
        event = _with_components(
            FakeEvent(
                "raw-current-id-71",
                "账号回退只保留正文",
                group_id="group-id-fallback",
            ),
            Reply(
                "prior-id-fallback",
                "raw-reply-id-72",
                "引用正文",
                sender_nickname="raw-reply-id-72",
            ),
            At("raw-at-id-73", "raw-at-id-73"),
        )
        event.get_sender_name = lambda: "raw-current-id-71"
        event.message_id = "address-id-fallback"

        await self._admit(plugin, event)

        record = event.get_extra(main.SHIO_TYPED_INBOUND_RECORD)
        scene = event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT)
        messages = project_model_messages(
            AssembledContext(
                reply_target=ReplyTarget(
                    message_id="later-id-fallback",
                    sender_key=main.build_sender_key(record.scope_key, "peer-z"),
                    session_id="group-id-fallback",
                    scope_key=record.scope_key,
                    content_digest=ledger_content_digest("后续问题"),
                    source_kind="inbound",
                    referenced_message_id="",
                    degradation_reasons=(),
                ),
                reference=None,
                planner_records=(record,),
                replyer_thread=(),
                public_background=(record,),
                fact_selection=FactSelection((), (), (), ()),
            )
        )
        identity_data = project_model_identity_prompt_data(messages)
        rendered = repr(identity_data)

        self.assertEqual(record.content, "账号回退只保留正文")
        self.assertEqual(record.display_name_status, "unavailable")
        self.assertEqual(record.referenced_display_name, "")
        self.assertEqual(record.mention_targets[0].display.status, "unavailable")
        self.assertEqual(
            scene.public_topics[-1].identity_metadata,
            record.identity_metadata,
        )
        for raw_id in ("raw-current-id-71", "raw-reply-id-72", "raw-at-id-73"):
            self.assertIn(raw_id, rendered)

    async def test_direct_and_about_self_key_phrases_remain_distinct(self):
        cases = (
            (
                "direct",
                "笨蛋萝卜子，你这次是大修",
                AddressKind.DIRECT_SELF,
            ),
            (
                "about",
                "我觉得笨蛋萝卜子这个称呼很有趣",
                AddressKind.ABOUT_SELF,
            ),
        )
        for name, text, expected in cases:
            with self.subTest(case=name):
                plugin = self._plugin(
                    FakeContext(FakeProvider([])),
                    {"natural_name_wake_aliases": ["萝卜子"]},
                )
                event = FakeEvent("peer-a", text, group_id="group-key-phrases")
                event.message_id = f"address-key-{name}"

                await self._admit(plugin, event)

                address = self._address(event)
                self.assertIsInstance(address, AddressDecision)
                self.assertIs(address.kind, expected)
                if expected is AddressKind.ABOUT_SELF:
                    # Address classification is independent from natural
                    # participation.  With its switch disabled this remains
                    # non-wake while still retaining ABOUT_SELF semantics.
                    self.assertFalse(event.is_at_or_wake_command)
                    self.assertTrue(address.is_meta_discussion)

    async def test_rejected_or_degraded_ingress_never_creates_address(self):
        cases = []

        self_plugin = self._plugin()
        self_event = FakeEvent(
            "bot-10000",
            "亚托莉，这是一条自身回声",
            group_id="group-rejected",
        )
        self_event.message_id = "address-drop-self"
        cases.append(("self", self_plugin, self_event, "drop_self"))

        bot_plugin = self._plugin(
            FakeContext(FakeProvider([])),
            {"trusted_bot_identities": ["亚托莉|other-bot-20000"]},
        )
        bot_event = FakeEvent(
            "other-bot-20000",
            "亚托莉，继续循环回复",
            group_id="group-rejected",
        )
        bot_event.message_id = "address-drop-known-bot"
        cases.append(("known_bot", bot_plugin, bot_event, "drop_known_bot"))

        degraded_context = FakeContext(FakeProvider([]))
        degraded_context.reneban_metadata = None
        degraded_plugin = self._plugin(degraded_context)
        degraded_event = FakeEvent(
            "peer-a",
            "亚托莉，你在吗？",
            group_id="group-rejected",
        )
        degraded_event.message_id = "address-reneban-degraded"
        cases.append(
            (
                "reneban_degraded",
                degraded_plugin,
                degraded_event,
                "degraded_external_gate",
            )
        )

        for name, plugin, event, expected_disposition in cases:
            with self.subTest(case=name):
                ingress = await self._admit(plugin, event)
                self.assertEqual(ingress.disposition.value, expected_disposition)
                self.assertIsNone(event.get_extra(main.SHIO_CONVERSATION_EVENT))
                self.assertIsNone(self._address(event))

    async def test_private_admitted_turn_gets_code_owned_direct_address(self):
        plugin = self._plugin()
        event = FakeEvent("peer-a", "请直接回答我", group_id="")
        event.message_id = "address-private"
        event.unified_msg_origin = "aiocqhttp:FriendMessage:peer-a"

        ingress = await self._admit(plugin, event)

        self.assertEqual(ingress.disposition.value, "accept_human")
        conversation_event = event.get_extra(main.SHIO_CONVERSATION_EVENT)
        self.assertIsNotNone(conversation_event)
        self.assertIsNone(event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT))
        address = self._address(event)
        self.assertIsInstance(address, AddressDecision)
        self.assertEqual(address.binding, conversation_event.binding)
        self.assertIs(address.kind, AddressKind.DIRECT_SELF)
        self.assertEqual(address.evidence, (AddressEvidence.PRIVATE_CHANNEL,))


if __name__ == "__main__":
    unittest.main()
