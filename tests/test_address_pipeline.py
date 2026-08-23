from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    # unittest discovery imports sibling files as top-level modules. Reuse that
    # exact module name so test_pipeline is never executed twice with two
    # independent FakeEvent/main registries.
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeStarTools,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeStarTools,
        main,
    )

from astrbot_plugin_shio.core.contracts import (
    AddressDecision,
    AddressEvidence,
    AddressKind,
)


class At:
    """Minimal AstrBot structured @ component used by the pipeline fixture."""

    type = "at"

    def __init__(self, qq: str) -> None:
        self.qq = qq


class Reply:
    """Minimal AstrBot structured reply component used by TurnEnvelope."""

    type = "reply"

    def __init__(self, message_id: str, sender_id: str, message_str: str = "") -> None:
        self.id = message_id
        self.sender_id = sender_id
        self.message_str = message_str


def _address_extra_key() -> str:
    # Keep the red test useful before the production constant exists. Once wired,
    # the public main-module constant is authoritative.
    return getattr(main, "SHIO_ADDRESS_DECISION", "_shio_address_decision")


def _with_components(event: FakeEvent, *components: object) -> FakeEvent:
    event.get_messages = lambda: list(components)
    return event


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
                    # P5-02 may promote a canonical ABOUT_SELF candidate, but
                    # it must remain semantically distinct from DIRECT_SELF.
                    self.assertTrue(event.is_at_or_wake_command)
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
