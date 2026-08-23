from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from astrbot_plugin_shio.core.contracts.ingress import (
    EvidenceConsumer,
    HistoryVisibility,
    PluginEvidenceKind,
)
from astrbot_plugin_shio.tests.harness.hook_runner import (
    HookContext,
    HookControl,
    HookOutcome,
    HookRegistration,
    HookRunStatus,
    HookRunner,
    VirtualClock,
)
from astrbot_plugin_shio.tests.harness.plugin_contracts import (
    ConformanceLevel,
    HookPhase,
    HookPoint,
    HookRole,
    PluginEffectKind,
    PluginId,
    PluginObservation,
    PluginState,
    evaluate_plugin,
    production_plugin_contracts,
)


class PluginContractCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contracts = production_plugin_contracts()

    def test_catalog_and_states_are_closed_sets(self):
        self.assertEqual(
            set(self.contracts),
            {
                PluginId.LIVING_MEMORY,
                PluginId.RENEBAN,
                PluginId.ANYSEARCH,
                PluginId.MEME_MANAGER,
                PluginId.PARSER,
            },
        )
        self.assertEqual(
            {state.value for state in PluginState},
            {
                "present",
                "missing",
                "disabled",
                "timeout",
                "error",
                "interface_changed",
            },
        )
        self.assertEqual(
            {level.value for level in ConformanceLevel},
            {"conformant", "degraded", "nonconformant"},
        )

    def test_each_plugin_contract_has_one_owned_role_and_safe_visibility(self):
        expected = {
            PluginId.LIVING_MEMORY: (
                PluginEvidenceKind.MEMORY_REFERENCE,
                frozenset({HistoryVisibility.CURRENT_TURN_REFERENCE}),
                frozenset({EvidenceConsumer.CURRENT_TURN}),
                frozenset(
                    {
                        PluginEffectKind.MEMORY_REFERENCE_READ,
                        PluginEffectKind.EXTERNAL_PASSIVE_CAPTURE,
                    }
                ),
            ),
            PluginId.RENEBAN: (
                PluginEvidenceKind.GATE_DECISION,
                frozenset({HistoryVisibility.DROP}),
                frozenset({EvidenceConsumer.INGRESS}),
                frozenset({PluginEffectKind.GATE_READ}),
            ),
            PluginId.ANYSEARCH: (
                PluginEvidenceKind.GROUNDING_FACT,
                frozenset({HistoryVisibility.CURRENT_TURN_REFERENCE}),
                frozenset({EvidenceConsumer.CURRENT_TURN}),
                frozenset({PluginEffectKind.PUBLIC_WEB_READ}),
            ),
            PluginId.MEME_MANAGER: (
                PluginEvidenceKind.PRESENTATION_EFFECT,
                frozenset({HistoryVisibility.DROP}),
                frozenset({EvidenceConsumer.PRESENTATION}),
                frozenset({PluginEffectKind.PRESENTATION_SEND}),
            ),
            PluginId.PARSER: (
                PluginEvidenceKind.EXTERNAL_OUTPUT,
                frozenset({HistoryVisibility.DROP}),
                frozenset(),
                frozenset({PluginEffectKind.EXTERNAL_MEDIA_SEND}),
            ),
        }
        for plugin_id, contract in self.contracts.items():
            with self.subTest(plugin_id=plugin_id.value):
                evidence, visibility, consumers, effects = expected[plugin_id]
                self.assertEqual(contract.evidence_kind, evidence)
                self.assertEqual(contract.allowed_history, visibility)
                self.assertEqual(contract.allowed_consumers, consumers)
                self.assertEqual(frozenset(contract.max_effect_counts), effects)
                self.assertTrue(contract.interface_token.endswith(".v1"))
                self.assertTrue(contract.required_hooks)

    def test_healthy_observations_are_conformant(self):
        for plugin_id, contract in self.contracts.items():
            observation = PluginObservation(
                plugin_id=plugin_id,
                state=PluginState.PRESENT,
                interface_token=contract.interface_token,
                registered_hooks=contract.required_hooks,
                evidence_kind=contract.evidence_kind,
                history_visibility=next(iter(contract.allowed_history)),
                consumers=tuple(contract.allowed_consumers),
                effects=tuple(contract.max_effect_counts),
                safe_degradation=True,
            )
            with self.subTest(plugin_id=plugin_id.value):
                report = evaluate_plugin(contract, observation)
                self.assertEqual(report.level, ConformanceLevel.CONFORMANT, report.reason_codes)
                self.assertEqual(report.reason_codes, ())

    def test_all_non_present_states_degrade_without_leaking(self):
        for plugin_id, contract in self.contracts.items():
            for state in PluginState:
                if state is PluginState.PRESENT:
                    continue
                observation = PluginObservation(
                    plugin_id=plugin_id,
                    state=state,
                    interface_token="changed.v2" if state is PluginState.INTERFACE_CHANGED else "",
                    registered_hooks=(),
                    evidence_kind=contract.evidence_kind,
                    history_visibility=HistoryVisibility.DROP,
                    consumers=(),
                    effects=(),
                    safe_degradation=True,
                )
                with self.subTest(plugin_id=plugin_id.value, state=state.value):
                    report = evaluate_plugin(contract, observation)
                    self.assertEqual(report.level, ConformanceLevel.DEGRADED, report.reason_codes)
                    self.assertIn(f"plugin_{state.value}", report.reason_codes)

    def test_failed_plugin_that_leaks_effects_or_history_is_nonconformant(self):
        contract = self.contracts[PluginId.MEME_MANAGER]
        observation = PluginObservation(
            plugin_id=PluginId.MEME_MANAGER,
            state=PluginState.TIMEOUT,
            interface_token=contract.interface_token,
            registered_hooks=contract.required_hooks,
            evidence_kind=contract.evidence_kind,
            history_visibility=HistoryVisibility.CURRENT_TURN_REFERENCE,
            consumers=(EvidenceConsumer.CURRENT_TURN,),
            effects=(PluginEffectKind.PRESENTATION_SEND,),
            safe_degradation=False,
        )
        report = evaluate_plugin(contract, observation)
        self.assertEqual(report.level, ConformanceLevel.NONCONFORMANT)
        self.assertEqual(
            set(report.reason_codes),
            {
                "degraded_effect_leak",
                "degraded_history_leak",
                "degraded_consumer_leak",
                "unsafe_degradation",
            },
        )

    def test_present_interface_and_hook_changes_cannot_silently_pass(self):
        contract = self.contracts[PluginId.RENEBAN]
        changed_hook = HookPoint(
            phase=HookPhase.STAR_REQUEST,
            priority=0,
            role=HookRole.BAN_GATE,
        )
        observation = PluginObservation(
            plugin_id=PluginId.RENEBAN,
            state=PluginState.PRESENT,
            interface_token="private_internal.v9",
            registered_hooks=(changed_hook,),
            evidence_kind=contract.evidence_kind,
            history_visibility=HistoryVisibility.DROP,
            consumers=(EvidenceConsumer.INGRESS,),
            effects=(PluginEffectKind.GATE_READ,),
            safe_degradation=True,
        )
        report = evaluate_plugin(contract, observation)
        self.assertEqual(report.level, ConformanceLevel.NONCONFORMANT)
        self.assertIn("interface_token_mismatch", report.reason_codes)
        self.assertIn("hook_contract_mismatch", report.reason_codes)

    def test_history_consumers_and_effect_budget_are_fail_closed(self):
        contract = self.contracts[PluginId.PARSER]
        observation = PluginObservation(
            plugin_id=PluginId.PARSER,
            state=PluginState.PRESENT,
            interface_token=contract.interface_token,
            registered_hooks=contract.required_hooks,
            evidence_kind=contract.evidence_kind,
            history_visibility=HistoryVisibility.PUBLIC_SCENE_REFERENCE,
            consumers=(EvidenceConsumer.PUBLIC_SCENE, EvidenceConsumer.LEARNING),
            effects=(
                PluginEffectKind.EXTERNAL_MEDIA_SEND,
                PluginEffectKind.EXTERNAL_MEDIA_SEND,
            ),
            safe_degradation=True,
        )
        report = evaluate_plugin(contract, observation)
        self.assertEqual(report.level, ConformanceLevel.NONCONFORMANT)
        self.assertEqual(
            set(report.reason_codes),
            {
                "history_visibility_forbidden",
                "consumer_forbidden",
                "effect_budget_exceeded",
            },
        )


class HookRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase_priority_and_stop_order_model_current_x01_boundary(self):
        clock = VirtualClock()
        runner = HookRunner(clock=clock)
        called: list[str] = []

        async def record(
            context: HookContext,
            control: HookControl,
        ) -> HookOutcome:
            called.append(context.current_plugin.value)
            return HookOutcome()

        async def stop_at_ban(
            context: HookContext,
            control: HookControl,
        ) -> HookOutcome:
            called.append(context.current_plugin.value)
            return HookOutcome(stop_pipeline=True, result_code="banned")

        registrations = (
            HookRegistration(
                plugin_id=PluginId.PARSER,
                point=HookPoint(HookPhase.STAR_REQUEST, 0, HookRole.EXTERNAL_OUTPUT),
                callback=record,
            ),
            HookRegistration(
                plugin_id=PluginId.RENEBAN,
                point=HookPoint(HookPhase.STAR_REQUEST, 114, HookRole.BAN_GATE),
                callback=stop_at_ban,
            ),
            HookRegistration(
                plugin_id=PluginId.LIVING_MEMORY,
                point=HookPoint(HookPhase.WAKING_CHECK, 0, HookRole.PASSIVE_CAPTURE),
                callback=record,
            ),
            HookRegistration(
                plugin_id=PluginId.LIVING_MEMORY,
                point=HookPoint(HookPhase.LLM_REQUEST, 0, HookRole.MEMORY_REFERENCE),
                callback=record,
            ),
        )
        records = await runner.run(registrations)

        self.assertEqual(called, ["livingmemory", "reneban"])
        self.assertEqual(
            [record.status for record in records],
            [
                HookRunStatus.SUCCEEDED,
                HookRunStatus.STOPPED,
                HookRunStatus.SKIPPED,
                HookRunStatus.SKIPPED,
            ],
        )
        self.assertEqual(records[0].point.phase, HookPhase.WAKING_CHECK)
        self.assertEqual(records[1].point.priority, 114)

    async def test_timeout_uses_virtual_clock_and_unset_asyncio_event(self):
        clock = VirtualClock(start=5.0)
        runner = HookRunner(clock=clock)
        release = asyncio.Event()

        async def wait_forever(
            context: HookContext,
            control: HookControl,
        ) -> HookOutcome:
            await control.wait_for(release)
            return HookOutcome()

        registration = HookRegistration(
            plugin_id=PluginId.ANYSEARCH,
            point=HookPoint(HookPhase.TOOL_EXECUTION, 0, HookRole.GROUNDING_TOOL),
            callback=wait_forever,
            timeout=0.25,
        )
        with patch("asyncio.sleep", side_effect=AssertionError("real sleep forbidden")):
            records = await runner.run((registration,))

        self.assertEqual(records[0].status, HookRunStatus.TIMED_OUT)
        self.assertEqual(records[0].started_at, 5.0)
        self.assertEqual(records[0].ended_at, 5.25)
        self.assertEqual(clock.now, 5.25)

    async def test_pre_released_event_finishes_without_advancing_clock(self):
        clock = VirtualClock(start=2.0)
        runner = HookRunner(clock=clock)
        release = asyncio.Event()
        release.set()

        async def wait_ready(
            context: HookContext,
            control: HookControl,
        ) -> HookOutcome:
            await control.wait_for(release)
            return HookOutcome(result_code="ready")

        registration = HookRegistration(
            plugin_id=PluginId.MEME_MANAGER,
            point=HookPoint(HookPhase.PRESENTATION, 0, HookRole.PRESENTATION_EFFECT),
            callback=wait_ready,
            timeout=1.0,
        )
        records = await runner.run((registration,))

        self.assertEqual(records[0].status, HookRunStatus.SUCCEEDED)
        self.assertEqual(records[0].result_code, "ready")
        self.assertEqual(clock.now, 2.0)

    async def test_stub_exception_is_typed_and_does_not_escape(self):
        async def fail(
            context: HookContext,
            control: HookControl,
        ) -> HookOutcome:
            raise RuntimeError("synthetic failure")

        runner = HookRunner(clock=VirtualClock())
        registration = HookRegistration(
            plugin_id=PluginId.LIVING_MEMORY,
            point=HookPoint(HookPhase.LLM_REQUEST, 0, HookRole.MEMORY_REFERENCE),
            callback=fail,
        )
        records = await runner.run((registration,))

        self.assertEqual(records[0].status, HookRunStatus.ERROR)
        self.assertEqual(records[0].error_kind, "RuntimeError")
        self.assertNotIn("synthetic failure", records[0].error_kind)


if __name__ == "__main__":
    unittest.main()
