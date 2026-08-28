from __future__ import annotations

import asyncio
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
        FakeResponse,
        FakeStarTools,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeResponse,
        FakeStarTools,
        main,
    )

from astrbot_plugin_shio.core.contracts import AddressKind, ContractViolation, ParticipationLevel
from astrbot_plugin_shio.core.participation_cadence import (
    ParticipationCadenceAuthority,
    ParticipationCadenceDecision,
    ParticipationCadencePreflight,
)
from astrbot_plugin_shio.core.participation_reaction import ParticipationReactionAuthority
from astrbot_plugin_shio.core.participation_engine import ParticipationAuthority
from astrbot_plugin_shio.core.participation_semantic import (
    ParticipationSemanticOutcome,
    ParticipationSemanticRequest,
    ParticipationSemanticStatus,
)
from astrbot_plugin_shio.core.performance_metrics import LatencyKind
from astrbot_plugin_shio.core.runtime_continuity import RuntimeContinuityStore


_GROUP_ID = "synthetic-semantic-group"
_REPLY = (
    '{"decision":"REPLY","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"helpful_context",'
    '"confidence":0.91}'
)
_WAIT = (
    '{"decision":"WAIT","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"defer_to_others",'
    '"confidence":0.83}'
)
_NO_ACTION = (
    '{"decision":"NO_ACTION","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"not_helpful",'
    '"confidence":0.88}'
)


class At:
    type = "at"

    def __init__(self, sender_id: str, display_name: str) -> None:
        self.qq = sender_id
        self.name = display_name


class Reply:
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


class ExactProvider:
    def __init__(self, outputs) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        if value is None or hasattr(value, "completion_text"):
            return value
        return FakeResponse(value)


class BlockingProvider:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        return FakeResponse(self.output)


class RoutedContext(FakeContext):
    def __init__(self, current_provider, configured_provider) -> None:
        super().__init__(current_provider)
        self.configured_provider = configured_provider
        self.requested_provider_ids: list[str] = []

    def get_provider_by_id(self, provider_id):
        self.requested_provider_ids.append(provider_id)
        return self.configured_provider


class P5ParticipationSemanticIncidentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.runtime_count = 0

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(
        display_name: str,
        sender_id: str,
        message: str,
        suffix: str,
        *,
        components: tuple[object, ...] = (),
        direct: bool = False,
    ):
        event = FakeEvent(sender_id, message, group_id=_GROUP_ID)
        event.message_id = f"synthetic-semantic-{suffix}"
        event.get_sender_name = lambda: display_name
        event.get_messages = lambda: list(components)
        event.is_at_or_wake_command = direct
        return event

    def _plugin(self, provider, *, config=None, context=None):
        self.runtime_count += 1
        FakeStarTools.data_dir = (
            Path(self.temp.name) / f"semantic-runtime-{self.runtime_count}"
        )
        values = {
            # 星汐是测试中的机器人显示名；既有下游 Persona 仍由亚托莉包提供。
            "persona_name": "亚托莉",
            "natural_name_wake_aliases": ["星汐"],
            "prefer_livingmemory_group_history": False,
            "natural_group_participation_enabled": True,
            "natural_group_participation_allowlist": [_GROUP_ID],
            "natural_group_participation_min_context_messages": 2,
            "natural_group_participation_cooldown_seconds": 1,
            "natural_group_participation_window_minutes": 5,
            "natural_group_participation_max_joins_per_window": 4,
        }
        values.update(config or {})
        return main.ShioPlugin(
            context or FakeContext(provider),
            values,
        )

    @staticmethod
    def _prime_named_context(plugin) -> None:
        mountain_tea = P5ParticipationSemanticIncidentTests._event(
            "山茶",
            "synthetic-shancha",
            "说明书第一行是处理器型号，后面才是重量。",
            "shancha-context",
        )
        bright_river = P5ParticipationSemanticIncidentTests._event(
            "明川",
            "synthetic-mingchuan",
            "我看到了重量，处理器那行字有点小。",
            "mingchuan-context",
            components=(
                Reply(
                    mountain_tea.message_id,
                    "synthetic-shancha",
                    "山茶",
                    mountain_tea.message,
                ),
            ),
        )
        plugin.admit_ingress_event(mountain_tea)
        plugin.admit_ingress_event(bright_river)

    @staticmethod
    async def _admit_async(plugin, event) -> None:
        outputs = [item async for item in plugin.admit_inbound_event(event)]
        if outputs:
            raise AssertionError("admission filter must not emit events")

    @staticmethod
    async def _admit_at(plugin, event, now: float) -> None:
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=now):
            plugin.admit_ingress_event(event)
        await plugin._resolve_participation_semantic(event)

    async def test_named_plain_incident_reaches_semantic_provider_before_reply(self):
        provider = FakeProvider(
            [_REPLY]
        )
        plugin = self._plugin(provider)
        self._prime_named_context(plugin)
        white_dew = self._event(
            "白露",
            "synthetic-bailu",
            "第一行写的啥",
            "bailu-plain-incident",
        )

        await self._admit_async(plugin, white_dew)

        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(white_dew.is_at_or_wake_command)
        self.assertTrue(white_dew.is_wake)

    async def test_question_words_cannot_auto_reply_against_semantic_no_action(self):
        provider = FakeProvider(
            [_NO_ACTION]
        )
        plugin = self._plugin(provider)
        self._prime_named_context(plugin)
        white_dew = self._event(
            "白露",
            "synthetic-bailu",
            "大家为什么要换轻薄本？",
            "bailu-question-negative",
        )

        await self._admit_async(plugin, white_dew)

        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(white_dew.is_at_or_wake_command)
        self.assertFalse(getattr(white_dew, "is_wake", False))

    async def test_plain_short_and_natural_topic_shift_all_reach_semantic(self):
        cases = (
            ("感觉除了358H也没啥轻薄本", _WAIT, False),
            ("但是价格顶不住", _WAIT, False),
            ("嗯有点悬", _WAIT, False),
            ("外面雨突然变大了", _REPLY, True),
        )
        for index, (message, output, should_wake) in enumerate(cases, start=1):
            with self.subTest(message=message):
                provider = ExactProvider([output])
                plugin = self._plugin(provider)
                self._prime_named_context(plugin)
                event = self._event(
                    "白露",
                    "synthetic-bailu",
                    message,
                    f"plain-variant-{index}",
                )

                await self._admit_async(plugin, event)

                self.assertEqual(len(provider.calls), 1)
                self.assertIs(event.is_at_or_wake_command, should_wake)
                cadence = event.get_extra(main.SHIO_PARTICIPATION_CADENCE)
                self.assertIs(type(cadence), ParticipationCadenceDecision)
                self.assertIs(
                    cadence.decision.level,
                    ParticipationLevel.MAY_JOIN
                    if should_wake
                    else ParticipationLevel.WAIT,
                )

    async def test_every_invalid_or_failed_provider_shape_fails_closed_once(self):
        valid_without_confidence = (
            '{"decision":"REPLY","target":"current_message",'
            '"topic_anchor":"current_message","reason_code":"helpful_context"}'
        )
        cases = (
            ("provider-exception", RuntimeError("synthetic-provider-failure")),
            ("missing-response", None),
            ("empty-completion", FakeResponse("")),
            (
                "reasoning-only",
                types.SimpleNamespace(
                    role="assistant",
                    completion_text="",
                    reasoning_content="internal synthetic reasoning",
                ),
            ),
            ("error-role", FakeResponse(_REPLY, role="err")),
            ("malformed-json", "{"),
            (
                "duplicate-key",
                '{"decision":"REPLY","decision":"WAIT",'
                '"target":"current_message","topic_anchor":"current_message",'
                '"reason_code":"helpful_context","confidence":0.9}',
            ),
            ("missing-key", valid_without_confidence),
            (
                "extra-key",
                _REPLY[:-1] + ',"visible_reply":"不得生成"}',
            ),
            (
                "invalid-decision",
                _REPLY.replace('"REPLY"', '"ANSWER"', 1),
            ),
            (
                "invalid-target",
                _REPLY.replace('"current_message"', '"message:1"', 1),
            ),
            (
                "invalid-anchor",
                _REPLY.replace(
                    '"topic_anchor":"current_message"',
                    '"topic_anchor":"message:99"',
                ),
            ),
            (
                "invalid-reason",
                _REPLY.replace('"helpful_context"', '"keyword_matched"'),
            ),
            ("invalid-confidence-type", _REPLY.replace("0.91", '"high"')),
            ("invalid-confidence-bool", _REPLY.replace("0.91", "true")),
            ("invalid-confidence-range", _REPLY.replace("0.91", "1.2")),
            ("invalid-confidence-nan", _REPLY.replace("0.91", "NaN")),
        )
        for index, (name, output) in enumerate(cases, start=1):
            with self.subTest(case=name):
                provider = ExactProvider([output])
                plugin = self._plugin(provider)
                self._prime_named_context(plugin)
                event = self._event(
                    "白露",
                    "synthetic-bailu",
                    "但是价格顶不住",
                    f"invalid-{index}",
                )

                await self._admit_async(plugin, event)

                self.assertEqual(len(provider.calls), 1)
                outcome = event.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_OUTCOME)
                self.assertIs(type(outcome), ParticipationSemanticOutcome)
                self.assertIsNot(outcome.status, ParticipationSemanticStatus.DECIDED)
                cadence = event.get_extra(main.SHIO_PARTICIPATION_CADENCE)
                self.assertIs(cadence.decision.level, ParticipationLevel.WAIT)
                self.assertFalse(event.is_at_or_wake_command)
                self.assertEqual(
                    plugin.generation_epochs.current(outcome.request.binding.scope_key),
                    0,
                )
                performance = plugin.performance_window.snapshot()
                self.assertEqual(performance.participation_call_count, 1)
                self.assertEqual(performance.participation_failure_count, 1)
                self.assertEqual(
                    performance.latency(
                        LatencyKind.PARTICIPATION_PROVIDER
                    ).sample_count,
                    1,
                )

    async def test_current_provider_gets_pure_content_and_real_relationship_metadata(self):
        current_provider = ExactProvider([_WAIT])
        configured_provider = ExactProvider([_REPLY])
        context = RoutedContext(current_provider, configured_provider)
        plugin = self._plugin(
            current_provider,
            context=context,
            config={"replyer_provider_id": "synthetic-configured-provider"},
        )
        self._prime_named_context(plugin)
        danyi = self._event(
            "Danyi",
            "synthetic-danyi",
            "我先把两台机器的重量放在一起看。",
            "danyi-context",
            components=(
                At("synthetic-mingchuan", "明川"),
                At("synthetic-bailu", "白露"),
            ),
        )
        plugin.admit_ingress_event(danyi)
        white_dew = self._event(
            "白露",
            "synthetic-bailu",
            "外面雨突然变大了",
            "provider-contract",
        )

        await self._admit_async(plugin, white_dew)

        self.assertEqual(len(current_provider.calls), 1)
        self.assertEqual(configured_provider.calls, [])
        self.assertEqual(context.requested_provider_ids, [])
        call = current_provider.calls[0]
        self.assertEqual(call["prompt"], white_dew.message)
        self.assertEqual(call["image_urls"], [])
        self.assertEqual(call["audio_urls"], [])
        self.assertIsNone(call["func_tool"])
        self.assertIsNone(call["tool_calls_result"])
        self.assertEqual(call["request_max_retries"], 0)
        contexts = call["contexts"]
        self.assertTrue(contexts)
        self.assertTrue(
            all(
                set(item) == {"role", "content"}
                and item["content"]
                in {
                    "说明书第一行是处理器型号，后面才是重量。",
                    "我看到了重量，处理器那行字有点小。",
                    "我先把两台机器的重量放在一起看。",
                }
                for item in contexts
            )
        )
        system_prompt = call["system_prompt"]
        for display_name in ("山茶", "明川", "白露", "Danyi", "星汐"):
            self.assertIn(display_name, system_prompt)
        self.assertIn("reply", system_prompt)
        self.assertIn("mention_targets", system_prompt)
        rendered = system_prompt + repr(contexts) + str(call["prompt"])
        for private_literal in (
            "synthetic-shancha",
            "synthetic-mingchuan",
            "synthetic-bailu",
            "synthetic-danyi",
            _GROUP_ID,
            "bot-10000",
            "synthetic-configured-provider",
        ):
            self.assertNotIn(private_literal, rendered)
        for forbidden_alias in (
            "路人 A",
            "路人 B",
            "群友 1",
            "同群成员 1",
        ):
            self.assertNotIn(forbidden_alias, rendered)

    async def test_direct_star_and_explicit_real_people_never_call_semantic_provider(self):
        cases = (
            (
                "mention-star",
                self._event(
                    "山茶",
                    "synthetic-shancha",
                    "这次请认真看",
                    "mention-star",
                    components=(At("bot-10000", "星汐"),),
                    direct=True,
                ),
                AddressKind.DIRECT_SELF,
            ),
            (
                "reply-star",
                self._event(
                    "山茶",
                    "synthetic-shancha",
                    "接着回答这个",
                    "reply-star",
                    components=(
                        Reply(
                            "synthetic-star-message",
                            "bot-10000",
                            "星汐",
                            "已验证的机器人旧回复",
                        ),
                    ),
                    direct=True,
                ),
                AddressKind.DIRECT_SELF,
            ),
            (
                "reply-mingchuan",
                self._event(
                    "山茶",
                    "synthetic-shancha",
                    "我赞同这个说法",
                    "reply-mingchuan",
                    components=(
                        Reply(
                            "synthetic-mingchuan-message",
                            "synthetic-mingchuan",
                            "明川",
                            "白露刚才问的是第一行",
                        ),
                    ),
                ),
                AddressKind.OTHER_PERSON,
            ),
            (
                "mention-mingchuan-and-bailu",
                self._event(
                    "Danyi",
                    "synthetic-danyi",
                    "你们两位先核对原文",
                    "mention-two-real-people",
                    components=(
                        At("synthetic-mingchuan", "明川"),
                        At("synthetic-bailu", "白露"),
                    ),
                ),
                AddressKind.OTHER_PERSON,
            ),
        )
        for name, event, expected_address in cases:
            with self.subTest(case=name):
                provider = ExactProvider([])
                plugin = self._plugin(provider)

                await self._admit_async(plugin, event)

                self.assertIs(
                    event.get_extra(main.SHIO_ADDRESS_DECISION).kind,
                    expected_address,
                )
                self.assertEqual(provider.calls, [])
                self.assertIsNone(
                    event.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_REQUEST)
                )
                if expected_address is AddressKind.DIRECT_SELF:
                    self.assertTrue(event.is_at_or_wake_command)
                else:
                    self.assertFalse(event.is_at_or_wake_command)
                    record = event.get_extra(main.SHIO_TYPED_INBOUND_RECORD)
                    displayed = {
                        target.display.value
                        for target in record.mention_targets
                        if target.display.available
                    }
                    if name == "reply-mingchuan":
                        self.assertEqual(record.referenced_display_name, "明川")
                    else:
                        self.assertEqual(displayed, {"明川", "白露"})

    async def test_owner_and_ordinary_member_have_equal_semantic_qualification(self):
        observations = []
        for display_name, sender_id, owner_ids in (
            ("Danyi", "synthetic-danyi", ["synthetic-danyi"]),
            ("明川", "synthetic-mingchuan", ["synthetic-danyi"]),
        ):
            provider = ExactProvider([_REPLY])
            plugin = self._plugin(provider, config={"owner_ids": owner_ids})
            self._prime_named_context(plugin)
            event = self._event(
                display_name,
                sender_id,
                "但是价格顶不住",
                f"parity-{display_name}",
            )

            await self._admit_async(plugin, event)

            assessment = event.get_extra(main.SHIO_PARTICIPATION_ASSESSMENT)
            cadence = event.get_extra(main.SHIO_PARTICIPATION_CADENCE)
            observations.append(
                (
                    assessment.decision.level,
                    assessment.interest_relevance,
                    assessment.relationship_affinity,
                    cadence.decision.level,
                    cadence.join_selected,
                    len(provider.calls),
                    event.is_at_or_wake_command,
                )
            )
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(
            observations[0],
            (
                ParticipationLevel.MAY_JOIN,
                0.0,
                0.0,
                ParticipationLevel.MAY_JOIN,
                True,
                1,
                True,
            ),
        )

    async def test_disabled_allowlist_and_shallow_context_close_before_provider(self):
        cases = (
            (
                "disabled",
                {
                    "natural_group_participation_enabled": False,
                    "natural_group_participation_allowlist": [_GROUP_ID],
                },
                True,
            ),
            (
                "outside-allowlist",
                {
                    "natural_group_participation_enabled": True,
                    "natural_group_participation_allowlist": [
                        "synthetic-other-group"
                    ],
                },
                True,
            ),
            (
                "shallow-context",
                {
                    "natural_group_participation_enabled": True,
                    "natural_group_participation_allowlist": [_GROUP_ID],
                    "natural_group_participation_min_context_messages": 3,
                },
                False,
            ),
        )
        for index, (name, config, prime) in enumerate(cases, start=1):
            with self.subTest(case=name):
                provider = ExactProvider([])
                plugin = self._plugin(provider, config=config)
                if prime:
                    self._prime_named_context(plugin)
                event = self._event(
                    "白露",
                    "synthetic-bailu",
                    "但是价格顶不住",
                    f"deterministic-gate-{index}",
                )

                await self._admit_async(plugin, event)

                self.assertEqual(provider.calls, [])
                self.assertFalse(event.is_at_or_wake_command)
                self.assertIsNone(
                    event.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_REQUEST)
                )

    async def test_wait_does_not_backoff_but_no_action_and_join_limits_gate_before_provider(self):
        wait_provider = ExactProvider([_WAIT, _REPLY])
        wait_plugin = self._plugin(wait_provider)
        self._prime_named_context(wait_plugin)
        wait_event = self._event(
            "白露",
            "synthetic-bailu",
            "但是价格顶不住",
            "wait-no-backoff-first",
        )
        await self._admit_at(wait_plugin, wait_event, 100.0)
        retry_event = self._event(
            "白露",
            "synthetic-bailu",
            "外面雨突然变大了",
            "wait-no-backoff-second",
        )
        await self._admit_at(wait_plugin, retry_event, 100.5)
        self.assertEqual(len(wait_provider.calls), 2)
        self.assertFalse(wait_event.is_at_or_wake_command)
        self.assertTrue(retry_event.is_at_or_wake_command)

        no_action_provider = ExactProvider([_NO_ACTION])
        no_action_plugin = self._plugin(no_action_provider)
        self._prime_named_context(no_action_plugin)
        first_no_action = self._event(
            "白露",
            "synthetic-bailu",
            "大家为什么要换轻薄本？",
            "no-action-backoff-first",
        )
        await self._admit_at(no_action_plugin, first_no_action, 200.0)
        backoff_blocked = self._event(
            "白露",
            "synthetic-bailu",
            "外面雨突然变大了",
            "no-action-backoff-second",
        )
        await self._admit_at(no_action_plugin, backoff_blocked, 201.0)
        self.assertEqual(len(no_action_provider.calls), 1)
        blocked_cadence = backoff_blocked.get_extra(main.SHIO_PARTICIPATION_CADENCE)
        self.assertIs(blocked_cadence.decision.level, ParticipationLevel.WAIT)
        self.assertIn(
            "participation_no_action_backoff",
            blocked_cadence.reason_codes,
        )

        cooldown_provider = ExactProvider([_REPLY])
        cooldown_plugin = self._plugin(cooldown_provider)
        self._prime_named_context(cooldown_plugin)
        first_join = self._event(
            "白露",
            "synthetic-bailu",
            "但是价格顶不住",
            "cooldown-first",
        )
        await self._admit_at(cooldown_plugin, first_join, 300.0)
        cooldown_blocked = self._event(
            "白露",
            "synthetic-bailu",
            "嗯有点悬",
            "cooldown-second",
        )
        await self._admit_at(cooldown_plugin, cooldown_blocked, 300.5)
        self.assertEqual(len(cooldown_provider.calls), 1)
        self.assertIn(
            "participation_join_cooldown",
            cooldown_blocked.get_extra(
                main.SHIO_PARTICIPATION_CADENCE
            ).reason_codes,
        )

        window_provider = ExactProvider([_REPLY])
        window_plugin = self._plugin(
            window_provider,
            config={
                "natural_group_participation_cooldown_seconds": 1,
                "natural_group_participation_max_joins_per_window": 1,
            },
        )
        self._prime_named_context(window_plugin)
        window_first = self._event(
            "白露",
            "synthetic-bailu",
            "但是价格顶不住",
            "window-first",
        )
        await self._admit_at(window_plugin, window_first, 400.0)
        window_blocked = self._event(
            "白露",
            "synthetic-bailu",
            "外面雨突然变大了",
            "window-second",
        )
        await self._admit_at(window_plugin, window_blocked, 402.0)
        self.assertEqual(len(window_provider.calls), 1)
        self.assertIn(
            "participation_window_limit",
            window_blocked.get_extra(
                main.SHIO_PARTICIPATION_CADENCE
            ).reason_codes,
        )

    async def test_capacity_and_continuity_unavailable_gate_before_provider(self):
        capacity_provider = ExactProvider([_NO_ACTION])
        capacity_plugin = self._plugin(capacity_provider)
        capacity_plugin.participation_authority = ParticipationAuthority(
            capacity_plugin.opportunity_attention_authority,
            capacity_plugin.group_scenes,
        )
        capacity_plugin.participation_cadence_authority = (
            ParticipationCadenceAuthority(
                capacity_plugin.participation_authority,
                continuity_store=capacity_plugin.runtime_continuity,
                join_cooldown_seconds=1.0,
                window_seconds=300.0,
                max_joins_per_window=4,
                max_subjects=1,
            )
        )
        capacity_plugin.participation_semantic_authority = (
            main.ParticipationSemanticAuthority(
                capacity_plugin.participation_authority,
                capacity_plugin.participation_cadence_authority,
                capacity_plugin.group_scenes,
            )
        )
        capacity_plugin.participation_reaction_authority = (
            ParticipationReactionAuthority(
                capacity_plugin.participation_cadence_authority
            )
        )
        self._prime_named_context(capacity_plugin)
        white_dew = self._event(
            "白露",
            "synthetic-bailu",
            "但是价格顶不住",
            "capacity-first-subject",
        )
        await self._admit_async(capacity_plugin, white_dew)
        danyi = self._event(
            "Danyi",
            "synthetic-danyi",
            "外面雨突然变大了",
            "capacity-second-subject",
        )
        await self._admit_async(capacity_plugin, danyi)
        self.assertEqual(len(capacity_provider.calls), 1)
        self.assertIn(
            "participation_cadence_capacity",
            danyi.get_extra(main.SHIO_PARTICIPATION_CADENCE).reason_codes,
        )

        continuity_provider = ExactProvider([])
        continuity_plugin = self._plugin(continuity_provider)
        closed_store = RuntimeContinuityStore(
            Path(self.temp.name) / "closed-semantic-continuity"
        )
        closed_store.close()
        continuity_plugin.participation_authority = ParticipationAuthority(
            continuity_plugin.opportunity_attention_authority,
            continuity_plugin.group_scenes,
        )
        continuity_plugin.participation_cadence_authority = (
            ParticipationCadenceAuthority(
                continuity_plugin.participation_authority,
                continuity_store=closed_store,
            )
        )
        continuity_plugin.participation_semantic_authority = (
            main.ParticipationSemanticAuthority(
                continuity_plugin.participation_authority,
                continuity_plugin.participation_cadence_authority,
                continuity_plugin.group_scenes,
            )
        )
        continuity_plugin.participation_reaction_authority = (
            ParticipationReactionAuthority(
                continuity_plugin.participation_cadence_authority
            )
        )
        self._prime_named_context(continuity_plugin)
        continuity_event = self._event(
            "白露",
            "synthetic-bailu",
            "但是价格顶不住",
            "continuity-unavailable",
        )
        await self._admit_async(continuity_plugin, continuity_event)
        self.assertEqual(continuity_provider.calls, [])
        self.assertIn(
            "participation_continuity_unavailable",
            continuity_event.get_extra(
                main.SHIO_PARTICIPATION_CADENCE
            ).reason_codes,
        )

    async def test_stale_before_and_after_provider_never_promotes_or_commits_join(self):
        stale_before_provider = ExactProvider([_REPLY])
        before_plugin = self._plugin(stale_before_provider)
        self._prime_named_context(before_plugin)
        white_dew_before = self._event(
            "白露",
            "synthetic-bailu",
            "但是价格顶不住",
            "stale-before",
        )
        before_plugin.admit_ingress_event(white_dew_before)
        mountain_tea_update = self._event(
            "山茶",
            "synthetic-shancha",
            "先看明川刚贴的第二页。",
            "stale-before-update",
        )
        before_plugin.admit_ingress_event(mountain_tea_update)
        await before_plugin._resolve_participation_semantic(white_dew_before)
        self.assertEqual(stale_before_provider.calls, [])
        before_outcome = white_dew_before.get_extra(
            main.SHIO_PARTICIPATION_SEMANTIC_OUTCOME
        )
        self.assertIs(before_outcome.status, ParticipationSemanticStatus.STALE)
        self.assertFalse(white_dew_before.is_at_or_wake_command)
        self.assertFalse(
            white_dew_before.get_extra(
                main.SHIO_PARTICIPATION_CADENCE
            ).join_selected
        )

        blocking_provider = BlockingProvider(_REPLY)
        after_plugin = self._plugin(blocking_provider)
        self._prime_named_context(after_plugin)
        white_dew_after = self._event(
            "白露",
            "synthetic-bailu",
            "第一行写的啥",
            "stale-after",
        )
        task = asyncio.create_task(
            self._admit_async(after_plugin, white_dew_after)
        )
        await blocking_provider.started.wait()
        mountain_tea_after = self._event(
            "山茶",
            "synthetic-shancha",
            "明川，我又核对了一遍第二页。",
            "stale-after-update",
            components=(At("synthetic-mingchuan", "明川"),),
        )
        after_plugin.admit_ingress_event(mountain_tea_after)
        blocking_provider.release.set()
        await task
        self.assertEqual(len(blocking_provider.calls), 1)
        after_outcome = white_dew_after.get_extra(
            main.SHIO_PARTICIPATION_SEMANTIC_OUTCOME
        )
        self.assertIs(after_outcome.status, ParticipationSemanticStatus.STALE)
        self.assertFalse(white_dew_after.is_at_or_wake_command)
        self.assertFalse(
            white_dew_after.get_extra(
                main.SHIO_PARTICIPATION_CADENCE
            ).join_selected
        )

    async def test_request_preflight_and_outcome_are_one_shot(self):
        provider = ExactProvider([_REPLY])
        plugin = self._plugin(provider)
        self._prime_named_context(plugin)
        event = self._event(
            "白露",
            "synthetic-bailu",
            "第一行写的啥",
            "one-shot",
        )
        await self._admit_async(plugin, event)
        request = event.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_REQUEST)
        preflight = event.get_extra(main.SHIO_PARTICIPATION_CADENCE_PREFLIGHT)
        outcome = event.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_OUTCOME)
        self.assertIs(type(request), ParticipationSemanticRequest)
        self.assertIs(type(preflight), ParticipationCadencePreflight)
        with self.assertRaisesRegex(ContractViolation, "replayed"):
            await plugin.participation_semantic_authority.evaluate(
                request,
                provider=provider,
                inference_budget=plugin.inference_budget,
                performance_window=plugin.performance_window,
            )
        with self.assertRaisesRegex(ContractViolation, "replayed"):
            plugin.participation_cadence_authority.finalize(
                preflight,
                outcome=ParticipationLevel.MAY_JOIN,
                reason_code="helpful_context",
            )
        with self.assertRaisesRegex(ContractViolation, "not_decided"):
            plugin.participation_semantic_authority.claim_decision(outcome)
        self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
