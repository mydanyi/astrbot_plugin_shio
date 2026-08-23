import asyncio
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

from astrbot_plugin_shio.core.contracts import ContractViolation
from astrbot_plugin_shio.core.proactive_runtime import (
    ProactiveComposerRequest,
    ProactiveExecutionAuthority,
    ProactiveExecutionStatus,
    ProactivePresentation,
    ProactiveSchedulerRuntime,
)


class ProactiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.context = FakeContext(FakeProvider([]))
        self.plugin = main.ShioPlugin(
            self.context,
            {
                "persona_name": "亚托莉",
                "proactive_initiation_enabled": True,
                "proactive_group_allowlist": ["group-a"],
                "proactive_active_hour_start": 0,
                "proactive_active_hour_end": 24,
                "proactive_timezone_offset_minutes": 0,
                "proactive_observation_minutes": 0,
                "proactive_idle_minutes": 0,
                "proactive_cooldown_minutes": 0,
                "proactive_daily_limit": 16,
                "proactive_scheduler_interval_seconds": 15,
            },
        )
        self.turn = 0

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()
        self.temp.cleanup()

    def _human(self, message: str, *, created_at: float = 1000.0):
        if self.turn == 0:
            self.turn += 1
            prior = FakeEvent("peer-b", "刚才的话题还没聊完", group_id="group-a")
            prior.message_id = f"p7-runtime-{self.turn}"
            prior.created_at = created_at + self.turn
            admission = self.plugin.admit_ingress_event(prior)
            self.assertTrue(admission.decision.allows_state_mutation)
        self.turn += 1
        event = FakeEvent("peer-a", message, group_id="group-a")
        event.message_id = f"p7-runtime-{self.turn}"
        event.created_at = created_at + self.turn
        admission = self.plugin.admit_ingress_event(event)
        self.assertTrue(admission.decision.allows_state_mutation)
        scene = event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT)
        self.assertIsNotNone(scene)
        return scene

    def _request(self, *, now: float = 2000.0) -> ProactiveComposerRequest:
        scene = self._human("大家晚饭吃什么？")
        observation = self.plugin.proactive_trigger_authority.observe_group(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="group-a",
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            observed_at=now,
        )
        candidate = self.plugin.proactive_trigger_authority.issue_candidate(
            self.plugin.proactive_trigger_authority.issue_source(observation)
        )
        decision = self.plugin.proactive_policy_state.evaluate(candidate, now=now)
        self.assertTrue(decision.admitted)
        persona = self.plugin._configured_persona_package()
        plan = self.plugin.proactive_topic_authority.select(
            decision,
            scene=scene,
            persona=persona,
        )
        return self.plugin.proactive_execution_authority.prepare(
            plan,
            persona=persona,
            temporal_context=self.plugin._build_temporal_context(now=now),
        )

    async def test_exact_plan_becomes_zero_tool_request_and_single_segment_send(self):
        self._human("大家晚饭吃什么？")
        self.context.provider.outputs.append("天气这么好，大家想吃点什么？")

        self.assertEqual(
            await self.plugin._run_proactive_scheduler_once(now=2000.0),
            1,
        )
        await self.plugin.proactive_scheduler_runtime.wait_idle()

        self.assertEqual(len(self.context.provider.calls), 1)
        call = self.context.provider.calls[0]
        self.assertIsNone(call["func_tool"])
        self.assertEqual(call["contexts"], [])
        self.assertEqual(call["request_max_retries"], 1)
        self.assertNotIn("peer-a", call["system_prompt"] + call["prompt"])
        self.assertNotIn("peer-b", call["system_prompt"] + call["prompt"])
        self.assertIn("刚才的话题还没聊完", call["prompt"])
        self.assertIn("大家晚饭吃什么", call["prompt"])
        self.assertIn("server_clock", call["system_prompt"])
        self.assertEqual(len(self.context.proactive_messages), 1)
        session, chain = self.context.proactive_messages[0]
        self.assertEqual(session, "aiocqhttp:GroupMessage:123")
        self.assertEqual(
            "".join(component.text for component in chain.chain),
            "天气这么好，大家想吃点什么？",
        )
        self.assertEqual(
            self.plugin.proactive_scheduler_runtime.trace_metadata()[
                "proactive_scheduler_waiting_human_count"
            ],
            1,
        )
        scene = self.plugin.group_scenes.snapshot(
            "platform:亚托莉|bot:bot-10000|group:group-a"
        )
        self.assertEqual(scene.public_topics[-1].content, "天气这么好，大家想吃点什么？")
        self.assertEqual(
            scene.public_topics[-1].target_sender_key,
            "platform:亚托莉|bot:bot-10000|group:group-a|user:bot-10000",
        )
        performance = self.plugin.performance_window.snapshot()
        self.assertEqual(performance.primary_call_count, 0)
        self.assertEqual(performance.repair_call_count, 0)
        self.assertEqual(performance.proactive_call_count, 1)
        self.assertEqual(
            performance.latency(main.LatencyKind.PROACTIVE_PROVIDER).sample_count,
            1,
        )

    async def test_terminal_log_keeps_exact_sent_outcome_after_scene_revision_moves(self):
        self._human("大家晚饭吃什么？")
        self.context.provider.outputs.append("天气这么好，大家想吃点什么？")

        with patch("astrbot_plugin_shio.main.structured_log") as log_mock:
            self.assertEqual(
                await self.plugin._run_proactive_scheduler_once(now=2000.0),
                1,
            )
            await self.plugin.proactive_scheduler_runtime.wait_idle()

        terminal = [
            call
            for call in log_mock.call_args_list
            if len(call.args) >= 3 and call.args[2] == "proactive.terminal"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].kwargs["outcome"], "sent")
        self.assertTrue(terminal[0].kwargs["proactive_presentation_canonical"])
        self.assertTrue(terminal[0].kwargs["proactive_send_authorized"])
        self.assertEqual(terminal[0].kwargs["proactive_segment_count"], 1)

    async def test_no_consecutive_monologue_until_a_new_human_message(self):
        self._human("大家晚饭吃什么？")
        self.context.provider.outputs.extend(("要不要聊聊晚饭？", "今天想吃什么？"))
        await self.plugin._run_proactive_scheduler_once(now=2000.0)
        await self.plugin.proactive_scheduler_runtime.wait_idle()
        self.assertEqual(await self.plugin._run_proactive_scheduler_once(now=2001.0), 0)
        self.assertEqual(len(self.context.proactive_messages), 1)

        self._human("我想吃面", created_at=2002.0)
        self.assertEqual(await self.plugin._run_proactive_scheduler_once(now=2100.0), 1)
        await self.plugin.proactive_scheduler_runtime.wait_idle()
        self.assertEqual(len(self.context.proactive_messages), 2)

    async def test_ungrounded_scene_is_silent_without_spending_policy_cooldown(self):
        self._human("今天天气真不错")
        self.assertEqual(await self.plugin._run_proactive_scheduler_once(now=2000.0), 0)
        self.assertEqual(self.context.provider.calls, [])
        self.assertEqual(self.context.proactive_messages, [])
        self.assertEqual(
            self.plugin.proactive_scheduler_runtime.trace_metadata()[
                "proactive_scheduler_terminal_error_count"
            ],
            0,
        )

        self.context.provider.outputs.append("说到晚饭，我忽然也有点想吃面了。")
        self._human("大家晚饭吃什么？", created_at=2001.0)
        self.assertEqual(await self.plugin._run_proactive_scheduler_once(now=2100.0), 1)
        await self.plugin.proactive_scheduler_runtime.wait_idle()
        self.assertEqual(len(self.context.proactive_messages), 1)

    async def test_new_human_message_cancels_cancel_safe_provider(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowProvider:
            supports_cancellation = True

            def __init__(self):
                self.calls = []

            async def text_chat(provider_self, **kwargs):
                provider_self.calls.append(kwargs)
                started.set()
                await release.wait()
                return main.LLMResponse("不该发送")

        provider = SlowProvider()
        self.context.provider = provider
        self._human("大家晚饭吃什么？")
        self.assertEqual(await self.plugin._run_proactive_scheduler_once(now=2000.0), 1)
        await started.wait()
        self._human("有人回来了", created_at=2001.0)
        release.set()
        await self.plugin.proactive_scheduler_runtime.wait_idle()
        self.assertEqual(self.context.proactive_messages, [])

    async def test_unsafe_provider_result_is_dropped_after_new_message(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowProvider:
            def __init__(self):
                self.calls = []

            async def text_chat(provider_self, **kwargs):
                provider_self.calls.append(kwargs)
                started.set()
                await release.wait()
                return main.LLMResponse("这个结果已经过时")

        self.context.provider = SlowProvider()
        self._human("大家晚饭吃什么？")
        await self.plugin._run_proactive_scheduler_once(now=2000.0)
        await started.wait()
        self._human("换个话题", created_at=2001.0)
        release.set()
        await self.plugin.proactive_scheduler_runtime.wait_idle()
        self.assertEqual(self.context.proactive_messages, [])

    async def test_provider_or_output_failure_never_sends_or_retries(self):
        self._human("大家晚饭吃什么？")
        self.context.provider.outputs.append(RuntimeError("provider marker"))
        await self.plugin._run_proactive_scheduler_once(now=2000.0)
        await self.plugin.proactive_scheduler_runtime.wait_idle()
        self.assertEqual(self.context.proactive_messages, [])

    async def test_non_exact_send_success_is_terminal_failure_without_retry(self):
        self._human("大家晚饭吃什么？")
        self.context.provider.outputs.append("大家想聊聊晚饭吗？")

        async def ambiguous_send(session, chain):
            return 1

        self.context.send_message = ambiguous_send
        await self.plugin._run_proactive_scheduler_once(now=2000.0)
        await self.plugin.proactive_scheduler_runtime.wait_idle()
        replies = tuple(self.plugin.send_receipts._replies.values())
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].segments[0].status.value, "failed")
        self.assertEqual(await self.plugin._run_proactive_scheduler_once(now=2001.0), 0)
        self.assertEqual(await self.plugin._run_proactive_scheduler_once(now=2001.0), 0)

        self._human("再说点什么", created_at=2002.0)
        self.context.provider.outputs.append("```json\n{\"tool\":\"shell\"}\n```")
        await self.plugin._run_proactive_scheduler_once(now=2100.0)
        await self.plugin.proactive_scheduler_runtime.wait_idle()
        self.assertEqual(self.context.proactive_messages, [])

    async def test_request_presentation_copy_mutation_and_replay_fail_closed(self):
        request = self._request()
        self.assertIs(type(request), ProactiveComposerRequest)
        self.assertTrue(request.model_authorized)
        self.assertFalse(request.send_authorized)
        self.assertNotIn("大家晚饭", repr(request) + repr(request.trace_metadata()))
        with self.assertRaises((ContractViolation, TypeError)):
            self.plugin.proactive_execution_authority.inspect_request(copy.copy(request))

        presentation = self.plugin.proactive_execution_authority.validate_output(
            request,
            "大家想聊点轻松的吗？",
        )
        self.assertIs(type(presentation), ProactivePresentation)
        self.assertTrue(presentation.send_authorized)
        with self.assertRaisesRegex(ContractViolation, "proactive_request_terminal"):
            self.plugin.proactive_execution_authority.validate_output(
                request,
                "第二次输出",
            )
        with self.assertRaises((ContractViolation, TypeError)):
            self.plugin.send_receipts.begin_proactive_presentation_reply(
                copy.copy(presentation)
            )
        original = presentation.final_visible_text
        object.__setattr__(presentation, "final_visible_text", "PRIVATE-MARKER")
        with self.assertRaisesRegex(ContractViolation, "proactive_presentation_corrupt"):
            self.plugin.proactive_execution_authority.inspect_presentation(presentation)
        self.assertNotIn("PRIVATE-MARKER", repr(presentation) + repr(presentation.trace_metadata()))
        object.__setattr__(presentation, "final_visible_text", original)

        reply = self.plugin.send_receipts.begin_proactive_presentation_reply(presentation)
        segment = reply.segments[0]
        self.plugin.send_receipts.mark_attempted(segment.segment_id)
        self.plugin.send_receipts.mark_succeeded(segment.segment_id)
        self.assertIs(
            self.plugin.proactive_execution_authority.complete_send(
                presentation,
                ledger=self.plugin.send_receipts,
            ),
            ProactiveExecutionStatus.SENT,
        )
        with self.assertRaises(ContractViolation):
            self.plugin.proactive_execution_authority.complete_send(
                presentation,
                ledger=self.plugin.send_receipts,
            )

    async def test_execution_authority_is_unique_and_send_claim_survives_later_human(self):
        with self.assertRaises(TypeError):
            ProactiveExecutionAuthority(self.plugin.proactive_topic_authority)
        with self.assertRaisesRegex(
            ContractViolation,
            "proactive_execution_authority_exists",
        ):
            ProactiveExecutionAuthority.issue_for_runtime(
                self.plugin.proactive_topic_authority
            )
        with self.assertRaises(TypeError):
            ProactiveSchedulerRuntime(
                self.plugin.proactive_trigger_authority,
                self.plugin.proactive_policy_state,
                self.plugin.proactive_topic_authority,
                self.plugin.proactive_execution_authority,
            )
        with self.assertRaisesRegex(ContractViolation, "proactive_scheduler_exists"):
            ProactiveSchedulerRuntime.issue_for_runtime(
                self.plugin.proactive_execution_authority,
                self.plugin.proactive_trigger_authority,
                self.plugin.proactive_policy_state,
                self.plugin.proactive_topic_authority,
            )

        request = self._request()
        presentation = self.plugin.proactive_execution_authority.validate_output(
            request,
            "要不要聊聊今晚想吃什么？",
        )
        reply = self.plugin.send_receipts.begin_proactive_presentation_reply(
            presentation
        )
        segment = reply.segments[0]
        self.plugin.send_receipts.mark_attempted(segment.segment_id)
        self._human("有人在发送开始后回来了", created_at=2001.0)
        self.plugin.send_receipts.mark_succeeded(segment.segment_id)
        self.assertIs(
            self.plugin.proactive_execution_authority.complete_send(
                presentation,
                ledger=self.plugin.send_receipts,
            ),
            ProactiveExecutionStatus.SENT,
        )

    async def test_default_off_starts_no_task_and_creates_no_message(self):
        await self.plugin.terminate()
        FakeStarTools.data_dir = Path(self.temp.name) / "disabled"
        self.context = FakeContext(FakeProvider(["不该调用"]))
        self.plugin = main.ShioPlugin(self.context, {"persona_name": "亚托莉"})
        await self.plugin.start_proactive_scheduler()
        self.assertIsNone(self.plugin._proactive_scheduler_task)
        self.assertFalse(self.plugin.proactive_scheduler_runtime.operational)
        self.assertEqual(self.context.proactive_messages, [])

    async def test_scheduler_start_is_single_and_terminate_stops_it(self):
        await self.plugin.start_proactive_scheduler()
        task = self.plugin._proactive_scheduler_task
        self.assertIsNotNone(task)
        await self.plugin.start_proactive_scheduler()
        self.assertIs(self.plugin._proactive_scheduler_task, task)
        await self.plugin.terminate()
        self.assertTrue(task.done())
        self.assertTrue(
            self.plugin.proactive_scheduler_runtime.trace_metadata()[
                "proactive_scheduler_stopped"
            ]
        )
        self.assertEqual(self.plugin.proactive_scheduler_runtime.trace_metadata()[
            "proactive_scheduler_active_count"
        ], 0)
        self.assertTrue(self.plugin.generation_tasks.closed)
        self.assertTrue(
            self.plugin.scope_concurrency.trace_metadata()["scope_concurrency_closed"]
        )
        self.assertTrue(self.plugin.inference_budget.trace_metadata()[
            "inference_budget_closed"
        ])
        await self.plugin.terminate()

    async def test_hot_reload_instance_self_starts_without_global_loaded_hook(self):
        await self.plugin.terminate()
        FakeStarTools.data_dir = Path(self.temp.name) / "hot-reload"
        self.context = FakeContext(FakeProvider([]))
        self.plugin = main.ShioPlugin(
            self.context,
            {
                "persona_name": "亚托莉",
                "proactive_initiation_enabled": True,
                "proactive_group_allowlist": ["group-a"],
                "proactive_active_hour_start": 0,
                "proactive_active_hour_end": 24,
                "proactive_timezone_offset_minutes": 0,
                "proactive_observation_minutes": 0,
                "proactive_idle_minutes": 0,
                "proactive_cooldown_minutes": 0,
                "proactive_daily_limit": 16,
                "proactive_scheduler_interval_seconds": 3600,
            },
        )

        await asyncio.sleep(0)
        task = self.plugin._proactive_scheduler_task
        self.assertIsNotNone(task)
        self.assertFalse(task.done())

        await self.plugin.start_proactive_scheduler()
        self.assertIs(self.plugin._proactive_scheduler_task, task)

    async def test_runtime_source_never_imports_inbound_or_private_memory(self):
        source = (
            Path(main.__file__).parent / "core" / "proactive_runtime.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "ConversationEvent",
            "AdmissionResult",
            "PrincipalContext",
            "AcceptedTurn",
            "livingmemory",
            "personal_facts",
        ):
            self.assertNotIn(forbidden.casefold(), source.casefold())
        self.assertIs(ProactiveExecutionAuthority, type(self.plugin.proactive_execution_authority))
        self.assertIs(ProactiveSchedulerRuntime, type(self.plugin.proactive_scheduler_runtime))


if __name__ == "__main__":
    unittest.main()
