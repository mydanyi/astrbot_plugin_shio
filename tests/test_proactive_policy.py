import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_shio.core.contracts import ContractViolation
from astrbot_plugin_shio.core.proactive_policy import (
    ProactivePolicyConfig,
    ProactivePolicyDecisionKind,
    ProactivePolicyState,
)
from astrbot_plugin_shio.core.proactive_trigger import ProactiveTriggerAuthority
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


class ProactivePolicyTests(unittest.TestCase):
    @staticmethod
    def _candidate(
        authority: ProactiveTriggerAuthority,
        *,
        group_id: str = "group-a",
        observed_at: float = 1000.0,
    ):
        observation = authority.observe_group(
            platform_id="platform-a",
            bot_id="bot-a",
            group_id=group_id,
            unified_msg_origin=f"group:platform-a:{group_id}",
            observed_at=observed_at,
        )
        return authority.issue_candidate(authority.issue_source(observation))

    @staticmethod
    def _enabled_policy(**changes) -> ProactivePolicyConfig:
        values = {
            "enabled": True,
            "group_allowlist": ("group-a",),
            "active_hour_start": 0,
            "active_hour_end": 24,
            "timezone_offset_minutes": 0,
            "observation_seconds": 60,
            "idle_seconds": 60,
            "cooldown_seconds": 300,
            "daily_limit": 2,
            "max_groups": 16,
        }
        values.update(changes)
        return ProactivePolicyConfig(**values)

    def test_empty_allowlist_is_absolute_default_off_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            authority = ProactiveTriggerAuthority()
            state = ProactivePolicyState(
                Path(root),
                trigger_authority=authority,
                policy=ProactivePolicyConfig(),
            )
            candidate = self._candidate(authority)
            decision = state.evaluate(candidate, now=1000.0)

            self.assertFalse(decision.admitted)
            self.assertIs(decision.kind, ProactivePolicyDecisionKind.DISABLED)
            self.assertFalse(decision.model_authorized)
            self.assertFalse(decision.send_authorized)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_allowlist_observation_idle_cooldown_and_daily_limit(self):
        with tempfile.TemporaryDirectory() as root:
            authority = ProactiveTriggerAuthority()
            state = ProactivePolicyState(
                Path(root),
                trigger_authority=authority,
                policy=self._enabled_policy(cooldown_seconds=100),
            )
            self.assertTrue(
                state.observe_group_activity(
                    platform_id="platform-a",
                    bot_id="bot-a",
                    group_id="group-a",
                    observed_at=1000.0,
                )
            )

            too_early = state.evaluate(
                self._candidate(authority, observed_at=1030.0),
                now=1030.0,
            )
            self.assertIs(
                too_early.kind,
                ProactivePolicyDecisionKind.OBSERVATION_INCOMPLETE,
            )
            admitted = state.evaluate(
                self._candidate(authority, observed_at=1070.0),
                now=1070.0,
            )
            self.assertTrue(admitted.admitted)
            cooldown = state.evaluate(
                self._candidate(authority, observed_at=1080.0),
                now=1080.0,
            )
            self.assertIs(cooldown.kind, ProactivePolicyDecisionKind.COOLDOWN_ACTIVE)

            second = state.evaluate(
                self._candidate(authority, observed_at=1180.0),
                now=1180.0,
            )
            self.assertTrue(second.admitted)
            limited = state.evaluate(
                self._candidate(authority, observed_at=1300.0),
                now=1300.0,
            )
            self.assertIs(limited.kind, ProactivePolicyDecisionKind.DAILY_LIMIT)

    def test_group_not_allowlisted_and_outside_active_hours_are_closed(self):
        with tempfile.TemporaryDirectory() as root:
            authority = ProactiveTriggerAuthority()
            state = ProactivePolicyState(
                Path(root),
                trigger_authority=authority,
                policy=self._enabled_policy(
                    active_hour_start=9,
                    active_hour_end=18,
                    observation_seconds=0,
                    idle_seconds=0,
                ),
            )
            other = state.evaluate(
                self._candidate(authority, group_id="group-b", observed_at=36000.0),
                now=36000.0,
            )
            self.assertIs(other.kind, ProactivePolicyDecisionKind.GROUP_NOT_ALLOWED)
            outside = state.evaluate(
                self._candidate(authority, observed_at=7200.0),
                now=7200.0,
            )
            self.assertIs(outside.kind, ProactivePolicyDecisionKind.OUTSIDE_ACTIVE_HOURS)

    def test_restart_preserves_trigger_and_never_repeats_same_observation(self):
        with tempfile.TemporaryDirectory() as root:
            first_authority = ProactiveTriggerAuthority()
            policy = self._enabled_policy(
                observation_seconds=0,
                idle_seconds=0,
                cooldown_seconds=300,
            )
            first = ProactivePolicyState(
                Path(root),
                trigger_authority=first_authority,
                policy=policy,
            )
            self.assertTrue(
                first.evaluate(
                    self._candidate(first_authority, observed_at=1000.0),
                    now=1000.0,
                ).admitted
            )

            second_authority = ProactiveTriggerAuthority()
            reopened = ProactivePolicyState(
                Path(root),
                trigger_authority=second_authority,
                policy=policy,
            )
            repeated = reopened.evaluate(
                self._candidate(second_authority, observed_at=1000.0),
                now=1000.0,
            )
            self.assertFalse(repeated.admitted)
            self.assertIn(
                repeated.kind,
                {
                    ProactivePolicyDecisionKind.OBSERVATION_REPLAYED,
                    ProactivePolicyDecisionKind.COOLDOWN_ACTIVE,
                },
            )

    def test_stale_generation_and_missing_or_changed_secret_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            authority = ProactiveTriggerAuthority()
            policy = self._enabled_policy(observation_seconds=0, idle_seconds=0)
            state = ProactivePolicyState(
                root_path,
                trigger_authority=authority,
                policy=policy,
            )
            stale = self._candidate(authority, observed_at=1000.0)
            current = self._candidate(authority, observed_at=1001.0)
            stale_decision = state.evaluate(stale, now=1001.0)
            self.assertIs(stale_decision.kind, ProactivePolicyDecisionKind.CANDIDATE_STALE)
            self.assertTrue(state.evaluate(current, now=1001.0).admitted)

            secret_path = root_path / ".proactive_install_secret"
            secret_path.write_bytes(b"x" * 32)
            reopened = ProactivePolicyState(
                root_path,
                trigger_authority=ProactiveTriggerAuthority(),
                policy=policy,
            )
            self.assertFalse(reopened.operational)

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            authority = ProactiveTriggerAuthority()
            policy = self._enabled_policy(observation_seconds=0, idle_seconds=0)
            state = ProactivePolicyState(
                root_path,
                trigger_authority=authority,
                policy=policy,
            )
            state.evaluate(self._candidate(authority), now=1000.0)
            (root_path / ".proactive_install_secret").unlink()
            reopened = ProactivePolicyState(
                root_path,
                trigger_authority=ProactiveTriggerAuthority(),
                policy=policy,
            )
            self.assertFalse(reopened.operational)

    def test_persistent_state_contains_no_group_or_user_identifiers(self):
        with tempfile.TemporaryDirectory() as root:
            authority = ProactiveTriggerAuthority()
            state = ProactivePolicyState(
                Path(root),
                trigger_authority=authority,
                policy=self._enabled_policy(observation_seconds=0, idle_seconds=0),
            )
            state.evaluate(self._candidate(authority), now=1000.0)
            rendered = (Path(root) / "proactive_state.json").read_text(encoding="utf-8")

            self.assertNotIn("group-a", rendered)
            self.assertNotIn("platform-a", rendered)
            self.assertNotIn("bot-a", rendered)
            self.assertNotIn("sender", rendered.casefold())
            payload = json.loads(rendered)
            self.assertEqual(payload["schema_version"], 1)

    def test_corrupt_state_and_atomic_write_failure_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            root_path.mkdir(parents=True, exist_ok=True)
            (root_path / "proactive_state.json").write_text("{bad", encoding="utf-8")
            authority = ProactiveTriggerAuthority()
            corrupt = ProactivePolicyState(
                root_path,
                trigger_authority=authority,
                policy=self._enabled_policy(observation_seconds=0, idle_seconds=0),
            )
            decision = corrupt.evaluate(self._candidate(authority), now=1000.0)
            self.assertIs(decision.kind, ProactivePolicyDecisionKind.STATE_UNAVAILABLE)

        with tempfile.TemporaryDirectory() as root:
            authority = ProactiveTriggerAuthority()
            state = ProactivePolicyState(
                Path(root),
                trigger_authority=authority,
                policy=self._enabled_policy(observation_seconds=0, idle_seconds=0),
            )
            with patch("astrbot_plugin_shio.core.proactive_policy.os.replace", side_effect=OSError("disk")):
                decision = state.evaluate(self._candidate(authority), now=1000.0)
            self.assertFalse(decision.admitted)
            self.assertIs(decision.kind, ProactivePolicyDecisionKind.STATE_UNAVAILABLE)

    def test_candidate_copy_cross_authority_and_decision_mutation_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            authority = ProactiveTriggerAuthority()
            state = ProactivePolicyState(
                Path(root),
                trigger_authority=authority,
                policy=self._enabled_policy(observation_seconds=0, idle_seconds=0),
            )
            candidate = self._candidate(authority)
            with self.assertRaises((ContractViolation, TypeError)):
                state.evaluate(copy.copy(candidate), now=1000.0)

            decision = state.evaluate(candidate, now=1000.0)
            self.assertIs(state.inspect(decision), decision)
            original = decision.admitted
            object.__setattr__(decision, "admitted", not original)
            with self.assertRaisesRegex(ContractViolation, "proactive_policy_decision_corrupt"):
                state.inspect(decision)
            object.__setattr__(decision, "admitted", original)
            self.assertIs(state.inspect(decision), decision)

            other_authority = ProactiveTriggerAuthority()
            other_state = ProactivePolicyState(
                Path(root) / "other",
                trigger_authority=other_authority,
                policy=self._enabled_policy(observation_seconds=0, idle_seconds=0),
            )
            with self.assertRaises(ContractViolation):
                other_state.evaluate(candidate, now=1000.0)

    def test_schema_and_main_default_are_all_off_with_zero_state_write(self):
        schema = json.loads(
            (Path(main.__file__).parent / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        proactive = schema["proactive_settings"]["items"]
        self.assertFalse(proactive["proactive_initiation_enabled"]["default"])
        self.assertEqual(proactive["proactive_group_allowlist"]["default"], [])

        with tempfile.TemporaryDirectory() as root:
            FakeStarTools.data_dir = Path(root)
            plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
            event = FakeEvent("peer-a", "大家好", group_id="group-a")
            event.message_id = "proactive-default-off"
            event.created_at = 1000.0
            plugin.admit_ingress_event(event)

            self.assertFalse(plugin.proactive_policy_state.operational)
            self.assertFalse((Path(root) / "proactive").exists())

    def test_main_records_only_allowlisted_accepted_group_activity_privately(self):
        with tempfile.TemporaryDirectory() as root:
            FakeStarTools.data_dir = Path(root)
            plugin = main.ShioPlugin(
                FakeContext(FakeProvider([])),
                {
                    "proactive_initiation_enabled": True,
                    "proactive_group_allowlist": ["group-a"],
                    "proactive_active_hour_start": 0,
                    "proactive_active_hour_end": 24,
                },
            )
            event = FakeEvent("peer-a", "大家好", group_id="group-a")
            event.message_id = "proactive-activity"
            event.created_at = 1000.0
            plugin.admit_ingress_event(event)

            state_path = Path(root) / "proactive" / "proactive_state.json"
            self.assertTrue(plugin.proactive_policy_state.operational)
            self.assertTrue(state_path.exists())
            rendered = state_path.read_text(encoding="utf-8")
            self.assertNotIn("group-a", rendered)
            self.assertNotIn("peer-a", rendered)
            self.assertNotIn("大家好", rendered)


if __name__ == "__main__":
    unittest.main()
