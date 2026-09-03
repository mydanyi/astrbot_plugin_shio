"""Phase 0 regression contracts for the SYS-001 replacement architecture.

These checks deliberately inspect the published plugin boundary instead of
replaying the legacy implementation's private state machines.  They must fail
against the v0.5.x baseline and become the guardrails for the new candidate.
"""

from __future__ import annotations

import json
import importlib.util
from dataclasses import replace
import sys
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = PLUGIN_ROOT / "core" / "sys001.py"
SPEC = importlib.util.spec_from_file_location("sys001_under_test", CORE_PATH)
assert SPEC is not None and SPEC.loader is not None
SYS001 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SYS001
SPEC.loader.exec_module(SYS001)


class Sys001ReplacementContractTests(unittest.TestCase):
    def test_schema_is_new_only(self) -> None:
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        serialized = json.dumps(schema, ensure_ascii=False, sort_keys=True)

        self.assertIn("sys001", serialized)
        for retired in (
            "persona_name",
            "natural_name_wake_aliases",
            "replyer_provider_id",
        ):
            self.assertNotIn(retired, serialized)

    def test_runtime_uses_one_event_bound_standard_request_path(self) -> None:
        source = (PLUGIN_ROOT / "main.py").read_text("utf-8")

        self.assertIn("event.request_llm(", source)
        self.assertIn("@filter.on_llm_request", source)
        for retired in (
            "Context.send_message(",
            "tool_loop_agent(",
            "replyer_provider_id",
            "StarTools.create_event",
            "get_event_queue",
        ):
            self.assertNotIn(retired, source)
        self.assertIn("conversation=conversation", source)
        self.assertIn("get_curr_conversation_id", source)
        self.assertIn("get_conversation", source)
        self.assertIn("project_official_group_history", source)
        self.assertNotIn("json.loads(req.conversation.history)", source)
        self.assertIn("@filter.on_llm_tool_respond", source)
        self.assertIn("@filter.after_message_sent", source)

    def test_retired_second_owners_are_not_imported_by_the_plugin_entrypoint(self) -> None:
        source = (PLUGIN_ROOT / "main.py").read_text("utf-8")
        for retired_owner in (
            ".core.affect",
            ".core.relationship_state",
            ".core.dialogue_quality",
            ".core.astrbot_media_adapter",
            ".core.trusted_bot_registry",
        ):
            self.assertNotIn(retired_owner, source)

    def test_private_and_direct_group_events_use_one_event_bound_entry(self) -> None:
        snapshot = SYS001.TurnSnapshot(
            platform_id="qq-main", scope="qq:friend:101", account_id="qq-main",
            sender_id="101", sender_name="synthetic", self_id="999", message_id="m1",
            created_at=SYS001.datetime(2026, 1, 1, tzinfo=SYS001.UTC), message_text="hello",
            is_private=True, is_master=False, at_self=False, reply_to_self=False, origin="pending",
        )
        decision = SYS001.decide_entry(
            snapshot, native_wake=False, visible_name_wake=False, natural_enabled=False,
            allowed_group_scopes=frozenset(),
        )
        self.assertEqual(("request", "private"), (decision.disposition, decision.origin))

    def test_snapshot_keeps_real_scope_self_reply_at_and_time_facts_together(self) -> None:
        class At:
            qq = "999"

        class Reply:
            sender_id = "999"

        class Message:
            message_id = "m-42"

        class Event:
            created_at = 1_704_067_200.0
            unified_msg_origin = "qq-main:group:202"
            message_obj = Message()
            def get_platform_id(self): return "qq-main"
            def get_self_id(self): return "999"
            def get_sender_id(self): return "202"
            def get_sender_name(self): return "synthetic member"
            def get_message_str(self): return "@bot reply"
            def get_messages(self): return [At(), Reply()]
            def is_private_chat(self): return False
            def is_admin(self): return True

        snapshot = SYS001.create_snapshot(Event(), origin="direct")
        self.assertEqual("999", snapshot.account_id)
        self.assertEqual("qq-main:group:202", snapshot.scope)
        self.assertTrue(snapshot.at_self and snapshot.reply_to_self and snapshot.is_master)
        self.assertIn("time_utc=2024-01-01", snapshot.model_context())

    def test_natural_participation_requires_real_group_event_and_keeps_wait_terminal(self) -> None:
        snapshot = SYS001.TurnSnapshot(
            platform_id="qq-main", scope="qq:group:202", account_id="qq-main",
            sender_id="202", sender_name="synthetic", self_id="999", message_id="m2",
            created_at=SYS001.datetime(2026, 1, 1, tzinfo=SYS001.UTC), message_text="天气不错",
            is_private=False, is_master=False, at_self=False, reply_to_self=False, origin="pending",
        )
        decision = SYS001.decide_entry(
            snapshot, native_wake=False, visible_name_wake=False, natural_enabled=True,
            allowed_group_scopes=frozenset({"qq:group:202"}),
        )
        self.assertEqual(("request", "natural"), (decision.disposition, decision.origin))
        empty_allowlist = SYS001.decide_entry(
            snapshot, native_wake=False, visible_name_wake=False, natural_enabled=True,
            allowed_group_scopes=frozenset(),
        )
        self.assertEqual("natural_scope_not_allowed", empty_allowlist.reason)
        lifecycle = SYS001.TurnLifecycle()
        lifecycle.terminal("wait")
        self.assertEqual(("blocked", "wait"), (lifecycle.state, lifecycle.terminal_reason))

    def test_direct_and_private_precede_natural_without_a_cooldown_state(self) -> None:
        group = SYS001.TurnSnapshot(
            platform_id="qq", scope="qq:group:202", account_id="999", sender_id="202",
            sender_name="member", self_id="999", message_id="m", created_at=SYS001.datetime(2026, 1, 1, tzinfo=SYS001.UTC),
            message_text="hello", is_private=False, is_master=False, at_self=False, reply_to_self=False, origin="pending",
        )
        direct = SYS001.decide_entry(group, native_wake=True, visible_name_wake=False, natural_enabled=True, allowed_group_scopes=frozenset({"qq:group:202"}))
        private = SYS001.decide_entry(replace(group, is_private=True), native_wake=False, visible_name_wake=False, natural_enabled=True, allowed_group_scopes=frozenset({"qq:group:202"}))
        self.assertEqual(("request", "direct"), (direct.disposition, direct.origin))
        self.assertEqual(("request", "private"), (private.disposition, private.origin))

    def test_text_component_limit_never_discards_tail_text(self) -> None:
        components = SYS001.split_text_components("一。二。三。", maximum=2)
        self.assertEqual(["一。", "二。三。"], components)
        self.assertEqual("一。二。三。", "".join(components))

    def test_visible_wake_match_uses_only_official_literals_outside_url_and_code(self) -> None:
        cases = [("Bot hi", ["Bot"], "Bot"), ("hi Bot there", ["ATRI", "Bot"], "Bot"), ("ATRI Bot", ["Bot", "ATRI"], "Bot"), ("https://x/Bot", ["Bot"], None), ("`Bot`", ["Bot"], None), ("```\nBot\n```", ["Bot"], None), ("ordinary", ["Bot"], None), ("Bot", [], None)]
        for text, words, expected in cases:
            self.assertEqual(expected, SYS001.visible_wake_match(text, words))

    def test_structured_address_prioritizes_bot_over_other_targets(self) -> None:
        class At:
            def __init__(self, qq): self.qq = qq
        class Reply:
            def __init__(self, sender_id): self.sender_id = sender_id
        cases = [([At("9")], "DIRECT_BOT"), ([Reply("9")], "DIRECT_BOT"), ([At("2")], "DIRECT_OTHER"), ([Reply("2")], "DIRECT_OTHER"), ([At("2"), At("9")], "DIRECT_BOT"), ([At("2"), At("3")], "DIRECT_OTHER"), ([At("9")], "NONE")]
        for components, expected in cases:
            self.assertEqual(expected, SYS001.address_decision(components, "" if expected == "NONE" else "9"))

    def test_group_capability_projection_can_only_remove_visible_tools(self) -> None:
        class Tool:
            def __init__(self, name: str) -> None:
                self.name = name

        original = [Tool("search"), Tool("admin"), Tool("calendar")]
        projected = SYS001.project_tool_names(original, frozenset({"search", "calendar"}))
        self.assertEqual(["search", "calendar"], [tool.name for tool in projected])
        self.assertEqual([], SYS001.project_tool_names(original, frozenset()))

    def test_final_agent_observation_does_not_infer_provider_attempts(self) -> None:
        response = type("Response", (), {"role": "assistant", "completion_text": "hi"})()
        observation = SYS001.classify_final_agent_response(response)
        self.assertEqual("final_text", observation.outcome)
        self.assertTrue(observation.from_final_agent_hook)
        self.assertIs(response, observation.raw_response)

    def test_natural_pre_agent_decisions_are_strict_json_only(self) -> None:
        self.assertEqual(
            "WAIT", SYS001.parse_natural_participation_decision('{"decision":"WAIT"}').decision
        )
        for text in ("WAIT", "NO_ACTION", "WAIT please", "`WAIT`"):
            self.assertIsNone(SYS001.parse_natural_participation_decision(text))


if __name__ == "__main__":
    unittest.main()
