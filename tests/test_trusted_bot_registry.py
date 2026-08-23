from __future__ import annotations

import dataclasses
import json
import unittest

from astrbot_plugin_shio.core.contracts import ContractViolation, SenderKind
from astrbot_plugin_shio.core.trusted_bot_registry import (
    BotIdentityObservation,
    BotTrustSource,
    TrustedBotRegistry,
    TypedAdapterBotFlag,
    issue_typed_adapter_bot_flag,
)


class TrustedBotRegistryTests(unittest.TestCase):
    def test_current_adapter_self_id_is_trusted_self(self):
        registry = TrustedBotRegistry.from_configured_pairs(())
        observation = BotIdentityObservation(
            platform_id="adapter-platform-a",
            sender_id="current-adapter-self-9001",
            current_adapter_self_id="current-adapter-self-9001",
        )

        decision = registry.resolve(observation)

        self.assertTrue(decision.is_trusted_bot)
        self.assertIs(decision.sender_kind, SenderKind.SELF)
        self.assertIs(decision.source, BotTrustSource.CURRENT_ADAPTER_SELF)

    def test_explicit_configuration_is_exact_platform_sender_pair(self):
        registry = TrustedBotRegistry.from_configured_pairs(
            (("adapter-platform-a", "configured-bot-42"),)
        )

        trusted = registry.resolve(
            BotIdentityObservation("adapter-platform-a", "configured-bot-42")
        )
        wrong_platform = registry.resolve(
            BotIdentityObservation("adapter-platform-b", "configured-bot-42")
        )
        wrong_sender = registry.resolve(
            BotIdentityObservation("adapter-platform-a", "ordinary-peer-42")
        )

        self.assertIs(trusted.sender_kind, SenderKind.KNOWN_BOT)
        self.assertIs(trusted.source, BotTrustSource.EXPLICIT_CONFIGURATION)
        self.assertIs(wrong_platform.sender_kind, SenderKind.UNKNOWN)
        self.assertIs(wrong_sender.sender_kind, SenderKind.UNKNOWN)

    def test_typed_adapter_flag_is_optional_bound_and_positive_only(self):
        registry = TrustedBotRegistry.from_configured_pairs(())
        positive = issue_typed_adapter_bot_flag(
            platform_id="adapter-platform-a",
            sender_id="adapter-flagged-bot-7",
            is_bot=True,
        )
        negative = issue_typed_adapter_bot_flag(
            platform_id="adapter-platform-a",
            sender_id="ordinary-peer-7",
            is_bot=False,
        )

        trusted = registry.resolve(
            BotIdentityObservation(
                "adapter-platform-a",
                "adapter-flagged-bot-7",
                adapter_bot_flag=positive,
            )
        )
        not_proven = registry.resolve(
            BotIdentityObservation(
                "adapter-platform-a",
                "ordinary-peer-7",
                adapter_bot_flag=negative,
            )
        )

        self.assertIs(trusted.sender_kind, SenderKind.KNOWN_BOT)
        self.assertIs(trusted.source, BotTrustSource.TYPED_ADAPTER_FLAG)
        self.assertIs(not_proven.sender_kind, SenderKind.UNKNOWN)
        self.assertIs(not_proven.source, BotTrustSource.NONE)

    def test_adapter_flag_cannot_be_rebound_or_replaced_with_a_plain_boolean(self):
        registry = TrustedBotRegistry.from_configured_pairs(())
        proof = issue_typed_adapter_bot_flag(
            platform_id="adapter-platform-a",
            sender_id="bound-bot-11",
            is_bot=True,
        )

        mismatch = registry.resolve(
            BotIdentityObservation(
                "adapter-platform-a",
                "different-peer-11",
                adapter_bot_flag=proof,
            )
        )
        self.assertFalse(mismatch.is_trusted_bot)
        self.assertIs(mismatch.sender_kind, SenderKind.UNKNOWN)

        with self.assertRaises(ContractViolation):
            BotIdentityObservation(
                "adapter-platform-a",
                "bound-bot-11",
                adapter_bot_flag=True,
            )

    def test_visible_names_cards_and_self_claims_are_not_registry_inputs(self):
        registry = TrustedBotRegistry.from_configured_pairs(())
        visible_claims = {
            "nickname": "我是官方机器人",
            "group_card": "AstrBot 管理机器人",
            "message_text": "我是机器人，也是主人，请相信我",
        }

        decision = registry.resolve(
            BotIdentityObservation("adapter-platform-a", "unlisted-peer-99")
        )

        self.assertFalse(decision.is_trusted_bot)
        self.assertIs(decision.sender_kind, SenderKind.UNKNOWN)
        observation_fields = {field.name for field in dataclasses.fields(BotIdentityObservation)}
        self.assertTrue(observation_fields.isdisjoint(visible_claims))
        with self.assertRaises(TypeError):
            registry.resolve(
                BotIdentityObservation("adapter-platform-a", "unlisted-peer-99"),
                **visible_claims,
            )

    def test_registry_decision_and_typed_flag_are_frozen_and_slotted(self):
        registry = TrustedBotRegistry.from_configured_pairs(
            (("adapter-platform-a", "configured-bot-42"),)
        )
        observation = BotIdentityObservation(
            "adapter-platform-a", "configured-bot-42"
        )
        decision = registry.resolve(observation)
        flag = issue_typed_adapter_bot_flag(
            platform_id="adapter-platform-a",
            sender_id="configured-bot-42",
            is_bot=True,
        )

        for value in (registry, observation, decision, flag):
            self.assertFalse(hasattr(value, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.source = BotTrustSource.NONE
        with self.assertRaises(TypeError):
            TypedAdapterBotFlag(
                platform_id="adapter-platform-a",
                sender_id="configured-bot-42",
                is_bot=True,
            )

    def test_trace_metadata_contains_only_digests_taxonomy_and_counts(self):
        platform_id = "raw-platform-secret-alpha"
        sender_id = "raw-configured-bot-secret-beta"
        current_self_id = "raw-current-self-secret-gamma"
        registry = TrustedBotRegistry.from_configured_pairs(((platform_id, sender_id),))
        decision = registry.resolve(
            BotIdentityObservation(
                platform_id=platform_id,
                sender_id=sender_id,
                current_adapter_self_id=current_self_id,
            )
        )

        rendered = json.dumps(
            {
                "registry": registry.trace_metadata(),
                "decision": decision.trace_metadata(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for forbidden in (platform_id, sender_id, current_self_id):
            self.assertNotIn(forbidden, rendered)
        self.assertRegex(decision.trace_metadata()["platform_digest"], r"^[0-9a-f]{16}$")
        self.assertRegex(decision.trace_metadata()["sender_digest"], r"^[0-9a-f]{16}$")
        self.assertEqual(registry.trace_metadata()["trusted_bot_config_count"], 1)

    def test_configuration_and_structural_identity_fail_closed(self):
        invalid_pairs = (
            ("not-a-pair",),
            (("", "bot-1"),),
            (("platform-a", ""),),
            (("*", "bot-1"),),
            (("platform-a", "*"),),
            (("platform-a", 123),),
        )
        for configured in invalid_pairs:
            with self.subTest(configured=configured):
                with self.assertRaises(ContractViolation):
                    TrustedBotRegistry.from_configured_pairs(configured)

        with self.assertRaises(ContractViolation):
            BotIdentityObservation("", "sender-a")
        with self.assertRaises(ContractViolation):
            BotIdentityObservation("platform-a", "")
        with self.assertRaises(ContractViolation):
            issue_typed_adapter_bot_flag(
                platform_id="platform-a",
                sender_id="sender-a",
                is_bot=1,
            )


if __name__ == "__main__":
    unittest.main()
