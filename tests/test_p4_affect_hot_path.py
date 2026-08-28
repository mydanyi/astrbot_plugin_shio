from __future__ import annotations

import copy
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    # Discovery imports sibling tests as top-level modules. Reuse the exact
    # pipeline harness so its AstrBot stubs and main module are not duplicated.
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeResponse,
        FakeStarTools,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeResponse,
        FakeStarTools,
        main,
    )

from astrbot_plugin_shio.core.affect_state import (
    AffectCause,
    AffectMutationResult,
    AffectMutationStatus,
    AffectRenderContext,
    inspect_affect_render_context,
)
from astrbot_plugin_shio.core.affect import AffectTrigger
from astrbot_plugin_shio.core.contracts import ContractViolation


class P4AffectHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(sender_id: str, message: str, message_id: str) -> FakeEvent:
        event = FakeEvent(sender_id, message, group_id="group-affect")
        event.message_id = message_id
        event.is_at_or_wake_command = True
        return event

    def _admit(self, plugin, event: FakeEvent, *, now: float):
        with patch("astrbot_plugin_shio.main.time.time", return_value=now):
            result = plugin.admit_ingress_event(event)
        self.assertIsNotNone(result)
        self.assertTrue(result.decision.allows_state_mutation)
        return result

    def test_admitted_human_claims_affect_ticket_and_records_bounded_state(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = self._event("peer-a", "你今天真的很厉害", "affect-positive-1")

        self._admit(plugin, event, now=1_000.0)

        self.assertTrue(hasattr(plugin, "affect_states"))
        envelope = main.ensure_turn_envelope(event)
        mutation = event.get_extra(main.SHIO_AFFECT_STATE_MUTATION)
        self.assertIsInstance(mutation, AffectMutationResult)
        self.assertIs(mutation.status, AffectMutationStatus.ACCEPTED_HUMAN)
        state = plugin.affect_states.get_state(
            envelope.scope_key,
            envelope.sender_key,
        )
        self.assertIs(state, mutation.state)
        self.assertIs(state.cause, AffectCause.POSITIVE_SOCIAL)
        self.assertGreater(state.intensity, 0.0)
        self.assertLessEqual(state.intensity, 0.85)
        self.assertEqual(state.version, 1)
        authority = plugin.accepted_turn_authority.trace_metadata()
        self.assertEqual(authority["claimed_ticket_count"], 4)
        self.assertEqual(authority["finalized_ticket_count"], 0)
        rendered = repr(mutation) + repr(mutation.trace_metadata())
        self.assertNotIn("peer-a", rendered)
        self.assertNotIn("你今天真的很厉害", rendered)

    def test_state_persists_per_sender_and_projects_to_baseline(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        first = self._event("peer-a", "你今天真的很厉害", "affect-a-1")
        second = self._event("peer-a", "继续说吧", "affect-a-2")
        other = self._event("peer-b", "继续说吧", "affect-b-1")

        self._admit(plugin, first, now=1_000.0)
        envelope_a = main.ensure_turn_envelope(first)
        first_state = plugin.affect_states.get_state(
            envelope_a.scope_key,
            envelope_a.sender_key,
        )
        self._admit(plugin, second, now=1_060.0)
        second_state = plugin.affect_states.get_state(
            envelope_a.scope_key,
            envelope_a.sender_key,
        )
        self.assertEqual(second_state.version, first_state.version + 1)
        self.assertGreater(second_state.intensity, 0.0)
        self.assertLess(second_state.intensity, first_state.intensity)

        self._admit(plugin, other, now=1_120.0)
        envelope_b = main.ensure_turn_envelope(other)
        other_state = plugin.affect_states.get_state(
            envelope_b.scope_key,
            envelope_b.sender_key,
        )
        self.assertEqual(other_state.version, 1)
        self.assertEqual(other_state.intensity, 0.0)
        self.assertIs(
            plugin.affect_states.get_state(
                envelope_a.scope_key,
                envelope_a.sender_key,
            ),
            second_state,
        )

        projected = plugin.affect_states.project_state(
            envelope_a.scope_key,
            envelope_a.sender_key,
            now=100_000.0,
        )
        self.assertEqual(projected.intensity, 0.0)
        self.assertIs(projected.cause, AffectCause.BASELINE)

    async def test_only_terminal_actual_send_receipt_settles_affect(self):
        with patch("astrbot_plugin_shio.main.time.time", return_value=1_999.0):
            plugin = main.ShioPlugin(
                FakeContext(FakeProvider([])),
                {
                    "bubble_interval_min_ms": 0,
                    "bubble_interval_max_ms": 0,
                },
            )
        event = self._event("peer-a", "你今天真的很厉害", "affect-send-1")
        request = FakeRequest(event.message)

        with patch("astrbot_plugin_shio.main.time.time", return_value=2_000.0):
            await plugin.enforce_agent_permission(event, request)
            await plugin.build_persona_reply(event, request)
        response = FakeResponse("被你这么认真地夸，我当然会开心呀。")
        await plugin.guard_persona_reply(event, response)
        self.assertTrue(response.completion_text)

        envelope = main.ensure_turn_envelope(event)
        before = plugin.affect_states.get_state(
            envelope.scope_key,
            envelope.sender_key,
        )
        self.assertIs(before.cause, AffectCause.POSITIVE_SOCIAL)

        class Result:
            def __init__(self, text: str) -> None:
                self.chain = [types.SimpleNamespace(text=text)]

            @staticmethod
            def is_llm_result() -> bool:
                return True

        event._result = Result(response.completion_text)
        await plugin.dispatch_chat_bubbles(event)
        # Planning/attempting a send is not delivery evidence.
        self.assertIs(
            plugin.affect_states.get_state(
                envelope.scope_key,
                envelope.sender_key,
            ),
            before,
        )

        with patch("astrbot_plugin_shio.main.time.time", return_value=2_001.0):
            await plugin.confirm_automatic_send_observation(event)

        outbound = event.get_extra(main.SHIO_AFFECT_OUTBOUND_MUTATION)
        self.assertIsInstance(outbound, AffectMutationResult)
        self.assertIs(
            outbound.status,
            AffectMutationStatus.ACCEPTED_SHIO_OUTBOUND,
        )
        after = plugin.affect_states.get_state(
            envelope.scope_key,
            envelope.sender_key,
        )
        self.assertIs(after, outbound.state)
        self.assertIs(after.cause, AffectCause.SHIO_OUTBOUND_SETTLE)
        self.assertEqual(after.version, before.version + 1)
        self.assertLess(after.intensity, before.intensity)

    async def test_exact_continuous_state_reaches_the_persona_renderer(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = self._event("peer-a", "你今天真的很厉害", "affect-render-1")
        request = FakeRequest(event.message)

        with patch("astrbot_plugin_shio.main.time.time", return_value=3_000.0):
            await plugin.enforce_agent_permission(event, request)
            await plugin.build_persona_reply(event, request)

        composer = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        context = composer.continuous_affect
        self.assertIs(type(context), AffectRenderContext)
        self.assertIs(
            inspect_affect_render_context(context, composer.planned_action.binding),
            context,
        )
        self.assertIs(context.cause, AffectCause.POSITIVE_SOCIAL)
        self.assertGreater(context.intensity, 0.0)
        self.assertIn('"continuous_affect"', composer.system_prompt)
        self.assertIn('"cause":"positive_social"', composer.system_prompt)
        self.assertNotIn("peer-a", repr(context) + repr(context.trace_metadata()))

    async def test_neutral_turn_keeps_bounded_carryover_but_current_trigger(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        first = self._event("peer-a", "你今天真的很厉害", "affect-carry-1")
        second = self._event("peer-a", "继续说吧", "affect-carry-2")

        with patch("astrbot_plugin_shio.main.time.time", return_value=4_000.0):
            await plugin.enforce_agent_permission(first, FakeRequest(first.message))
            await plugin.build_persona_reply(first, FakeRequest(first.message))
        with patch("astrbot_plugin_shio.main.time.time", return_value=4_060.0):
            await plugin.enforce_agent_permission(second, FakeRequest(second.message))
            await plugin.build_persona_reply(second, FakeRequest(second.message))

        composer = second.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        appraisal = second.get_extra(main.SHIO_AFFECT_APPRAISAL)
        self.assertIs(appraisal.trigger, AffectTrigger.NEUTRAL)
        self.assertEqual(composer.continuous_affect.version, 2)
        self.assertIs(composer.continuous_affect.cause, AffectCause.POSITIVE_SOCIAL)
        self.assertGreater(composer.continuous_affect.intensity, 0.0)
        self.assertLessEqual(composer.continuous_affect.intensity, 0.85)

    async def test_render_context_copy_cross_binding_and_mutation_fail_closed(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        first = self._event("peer-a", "你今天真的很厉害", "affect-seal-1")
        second = self._event("peer-a", "继续说吧", "affect-seal-2")
        with patch("astrbot_plugin_shio.main.time.time", return_value=5_000.0):
            await plugin.enforce_agent_permission(first, FakeRequest(first.message))
            await plugin.build_persona_reply(first, FakeRequest(first.message))
        with patch("astrbot_plugin_shio.main.time.time", return_value=5_010.0):
            await plugin.enforce_agent_permission(second, FakeRequest(second.message))
            await plugin.build_persona_reply(second, FakeRequest(second.message))

        first_request = first.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        second_request = second.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
        context = second_request.continuous_affect
        with self.assertRaises(ContractViolation):
            inspect_affect_render_context(
                copy.copy(context),
                second_request.planned_action.binding,
            )
        with self.assertRaises(ContractViolation):
            inspect_affect_render_context(
                context,
                first_request.planned_action.binding,
            )
        current_binding = second_request.planned_action.binding
        with self.assertRaises(ContractViolation):
            inspect_affect_render_context(context, copy.copy(current_binding))
        original = context.intensity
        object.__setattr__(context, "intensity", 0.0)
        with self.assertRaises(ContractViolation):
            inspect_affect_render_context(
                context,
                second_request.planned_action.binding,
            )
        object.__setattr__(context, "intensity", original)
        original_message_id = current_binding.current_message_id
        object.__setattr__(current_binding, "current_message_id", "mutated-message")
        with self.assertRaises(ContractViolation):
            inspect_affect_render_context(context, current_binding)
        object.__setattr__(current_binding, "current_message_id", original_message_id)
        self.assertIs(
            inspect_affect_render_context(
                context,
                second_request.planned_action.binding,
            ),
            context,
        )


if __name__ == "__main__":
    unittest.main()
