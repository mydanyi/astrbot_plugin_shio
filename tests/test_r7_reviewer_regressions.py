"""R7 regressions for the R6 review findings.

These tests reuse the public-API recorder installed by the routing suite.  They
exercise the plugin boundary rather than reproducing its routing algorithms.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

try:
    from test_name_semantic_provider_routing import (  # type: ignore
        FakeContext,
        FakeEvent,
        FakeProvider,
        MAIN,
        SYS001,
    )
except ModuleNotFoundError:  # pragma: no cover - package invocation
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        MAIN,
        SYS001,
    )


def _snapshot(*, origin: str = "natural", sender: str = "202"):
    return SYS001.TurnSnapshot(
        platform_id="qq", scope="qq:group:202", account_id="999",
        sender_id=sender, sender_name="member", self_id="999",
        message_id="r7-message", created_at=datetime(2024, 1, 1, tzinfo=UTC),
        message_text="same words", is_private=False, is_master=False,
        at_self=False, reply_to_self=False, origin=origin,
        at_targets=("998",), reply_message_id="reply-1",
        reply_sender_id="998", reply_time_utc="2024-01-01T00:00:00+00:00",
    )


class _Event(FakeEvent):
    def __init__(self, text="same words", *, components=()):
        super().__init__(text)
        self.extras = {}
        self.is_at_or_wake_command = False
        self._components = list(components)
        self.requests = []

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_messages(self):
        return self._components

    def should_call_llm(self, _value):
        return None

    def request_llm(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(**kwargs)


def _plugin(context, *, group=None, final_review=None, presentation=None):
    plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
    plugin.context = context
    plugin.config = {"sys001": {
        "group": group or {}, "final_review": final_review or {},
        "presentation": presentation or {}, "ingress": {
            "group_allowed_scopes": ["qq:group:202"],
        },
    }}
    plugin._auxiliary_epoch = 1
    plugin._auxiliary_terminated = False
    plugin._natural_generations = {"qq:group:202": 1}
    return plugin


class R7ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_fixed_timestampmixin_sqlmodel_sqlite_round_trip_is_naive_utc(self):
        """Execute fixed public Field shape with an existing dependency only."""
        root = Path(__file__).resolve().parents[2]
        interpreter = root / ".worktrees" / "ASTR-IF-001" / ".venv" / "Scripts" / "python.exe"
        if not interpreter.is_file():
            self.skipTest("existing sqlmodel interpreter is unavailable")
        source = """
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field, Session, create_engine, select, JSON
class TimestampMixin(SQLModel):
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
class PlatformMessageHistory(TimestampMixin, SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    platform_id: str
    user_id: str
    content: dict = Field(sa_type=JSON)
e = create_engine('sqlite://')
SQLModel.metadata.create_all(e)
with Session(e) as session:
    session.add(PlatformMessageHistory(platform_id='qq', user_id='group', content={'type':'user'}))
    session.commit()
    value = session.exec(select(PlatformMessageHistory)).one().created_at
    print(value.tzinfo is None)
"""
        encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
        completed = subprocess.run(
            [str(interpreter), "-B", "-c", f"import base64; exec(base64.b64decode('{encoded}'))"],
            check=True, text=True, encoding="utf-8", capture_output=True,
        )
        self.assertEqual("True", completed.stdout.strip())

    def test_sqlite_naive_utc_history_row_is_projected_and_current_row_stays_excluded(self):
        # AstrBot's SQLModel/SQLite TimestampMixin round-trips this supported
        # datetime without tzinfo.  It means UTC, not an unusable row.
        row = lambda ident: SimpleNamespace(
            id=ident, created_at=datetime(2024, 1, 1), sender_id="202",
            sender_name="member", content={"type": "user", "message": [
                {"type": "plain", "text": "past" if ident == 1 else "current"}
            ]},
        )
        contexts = SYS001.project_official_group_history(
            [row(1), row(2)], blocked_sender_ids=frozenset(),
            current_history_row_id=2,
        )
        self.assertEqual(1, len(contexts))
        self.assertIn('"created_at_utc":"2024-01-01T00:00:00+00:00"', contexts[0]["content"])
        self.assertIn("past", contexts[0]["content"])

    def test_history_rejects_non_datetime_or_uninterpretable_timezone_values(self):
        base = dict(id=1, sender_id="202", sender_name="member", content={
            "type": "user", "message": [{"type": "plain", "text": "text"}],
        })
        invalid = [
            SimpleNamespace(**base, created_at="2024-01-01"),
            SimpleNamespace(**base, created_at=object()),
        ]
        self.assertEqual([], SYS001.project_official_group_history(
            invalid, blocked_sender_ids=frozenset(),
        ))

    def test_auxiliary_prompt_has_structured_turn_and_history_not_just_words(self):
        prompt = SYS001.name_semantic_prompt(
            "rule", "Shio", _snapshot(), "DIRECT_OTHER",
            history_contexts=[{"role": "user", "content": "official past"}],
        )
        for expected in (
            "sender_id=202", "scope=qq:group:202", "bot_self_id=999",
            "reply_sender_id=998", "at_targets=998", "official_history=",
            "DIRECT_OTHER",
        ):
            self.assertIn(expected, prompt)

    async def test_natural_provider_lookup_error_continues_to_healthy_fallback(self):
        healthy = FakeProvider('{"decision":"REPLY"}')
        context = FakeContext(by_id={"healthy": healthy}, lookup_errors={"broken"})
        plugin = _plugin(context, group={
            "natural_decision_provider_id": "broken",
            "natural_decision_fallback_provider_ids": ["healthy"],
            "natural_decision_timeout_seconds": 1,
        })
        event = _Event()
        event.set_extra("shio.sys001.generation", 1)
        self.assertEqual("REPLY", await plugin._decide_natural_participation(event, _snapshot()))
        self.assertEqual(["broken", "healthy"], context.lookup_ids)
        self.assertEqual(1, len(healthy.calls))

    async def test_current_auxiliary_provider_survives_a_bad_fallback_lookup(self):
        current = FakeProvider('{"decision":"REPLY"}')
        context = FakeContext(current=current, lookup_errors={"broken"})
        plugin = _plugin(context, group={
            "natural_decision_provider_id": "",
            "natural_decision_fallback_provider_ids": ["broken"],
            "natural_decision_timeout_seconds": 1,
        })
        event = _Event(components=[SimpleNamespace(qq="998")])
        event.set_extra("shio.sys001.generation", 1)
        self.assertEqual("REPLY", await plugin._decide_natural_participation(event, _snapshot()))
        self.assertEqual(1, len(current.calls))
        self.assertEqual(["broken"], context.lookup_ids)

    async def test_auxiliary_lookup_cancel_propagates_without_late_state_change(self):
        class CancelContext(FakeContext):
            def get_provider_by_id(self, provider_id):
                raise asyncio.CancelledError()

        plugin = _plugin(CancelContext(), group={
            "natural_decision_provider_id": "cancel", "natural_decision_fallback_provider_ids": [],
        })
        event = _Event()
        event.set_extra("shio.sys001.generation", 1)
        with self.assertRaises(asyncio.CancelledError):
            await plugin._decide_natural_participation(event, _snapshot())

    async def test_review_and_model_lookup_error_continue_to_healthy_fallback(self):
        review_provider = FakeProvider('{"action":"keep"}')
        context = FakeContext(by_id={"healthy": review_provider}, lookup_errors={"broken"})
        plugin = _plugin(context, final_review={
            "mode": "core", "timeout_seconds": 1, "max_repair_attempts": 0,
            "provider_id": "broken", "fallback_provider_ids": ["healthy"],
        })
        event = _Event()
        outcome = await plugin._review_final_text(event, _snapshot(origin="direct"), "safe")
        self.assertFalse(outcome.exhausted)

        segment_provider = FakeProvider('{"segments":["hello","world"]}')
        segment_context = FakeContext(by_id={"healthy": segment_provider}, lookup_errors={"broken"})
        plugin = _plugin(segment_context, presentation={
            "model_segment_provider_id": "broken",
            "model_segment_fallback_provider_ids": ["healthy"],
            "model_segment_timeout_seconds": 1, "text_component_min_segments": 1,
            "text_component_max_segments": 2,
        })
        self.assertEqual(["hello", "world"], await plugin._model_text_components(
            _Event(), "helloworld", plugin._presentation_settings(),
            snapshot=_snapshot(origin="direct"),
        ))

    async def test_natural_main_request_never_receives_silence_protocol(self):
        classifier = FakeProvider('{"decision":"REPLY"}')
        plugin = _plugin(FakeContext(current=classifier), group={
            "natural_participation_enabled": True,
            "natural_group_scopes": ["qq:group:202"],
            "natural_participation_prompt": "Return WAIT or NO_ACTION when quiet.",
            "continuous_window_enabled": False,
        })
        plugin._natural_cadence = {"qq:group:202": SYS001.NaturalCadence.empty()}
        plugin._natural_ready = {"qq:group:202": True}
        plugin._natural_bindings = {}
        plugin._natural_locks = {}
        plugin._natural_terminated = False
        plugin._continuous_scopes = {}
        plugin._continuous_locks = {}
        plugin._continuous_terminated = False
        plugin.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=lambda _scope: asyncio.sleep(0, result="c"),
            get_conversation=lambda *_args: asyncio.sleep(0, result=object()),
            new_conversation=lambda *_args, **_kwargs: asyncio.sleep(0, result="c"),
        )
        event = _Event()
        request = await anext(plugin.request_event_bound_reply(event))
        self.assertEqual("same words", request.prompt)
        self.assertNotIn("WAIT", request.prompt)

    async def test_tool_then_marker_like_main_result_remains_visible_and_uncommitted(self):
        plugin = _plugin(FakeContext())
        event = _Event()
        snapshot = _snapshot(origin="natural")
        lifecycle = SYS001.TurnLifecycle()
        event.set_extra(MAIN.SYS001_TURN_EXTRA, snapshot)
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, lifecycle)
        event.set_extra(MAIN.SYS001_TOOL_OBSERVATIONS_EXTRA, [object()])
        response = SimpleNamespace(role="assistant", completion_text="NO_ACTION", reasoning_content="")

        await plugin.observe_final_agent_response(event, response)

        self.assertEqual("NO_ACTION", response.completion_text)
        self.assertEqual("captured", lifecycle.state)
        self.assertFalse(event.get_extra("shio.sys001.natural_no_action_committed", False))

    async def test_direct_other_reaches_only_the_structured_cautious_natural_gate(self):
        classifier = FakeProvider('{"decision":"REPLY"}')
        plugin = _plugin(FakeContext(current=classifier), group={
            "natural_participation_enabled": True,
            "natural_group_scopes": ["qq:group:202"],
        })
        plugin._natural_ready = {"qq:group:202": True}
        plugin._natural_cadence = {"qq:group:202": SYS001.NaturalCadence.empty()}
        plugin._natural_locks = {}
        plugin._natural_terminated = False
        plugin._continuous_scopes = {}
        plugin._continuous_locks = {}
        plugin._continuous_terminated = False
        event = _Event(components=[SimpleNamespace(qq="998")])
        plugin.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=lambda _scope: asyncio.sleep(0, result="c"),
            get_conversation=lambda *_args: asyncio.sleep(0, result=object()),
            new_conversation=lambda *_args, **_kwargs: asyncio.sleep(0, result="c"),
        )
        request = await anext(plugin.request_event_bound_reply(event))
        self.assertEqual("same words", request.prompt)
        self.assertEqual(1, len(classifier.calls))
        self.assertIn("STRUCTURED_ADDRESS=DIRECT_OTHER", classifier.calls[0]["prompt"])

    async def test_natural_classifier_does_not_read_persistent_group_history(self):
        history = [SimpleNamespace(
            id=7, created_at=datetime(2024, 1, 1), sender_id="303", sender_name="past",
            content={"type": "user", "message": [{"type": "plain", "text": "past context"}]},
        )]

        class HistoryContext(FakeContext):
            def get_config(self, *, umo):
                return {"provider_ltm_settings": {
                    "group_message_history_enable": True, "group_icl_enable": False,
                    "group_message_history_max_cnt": 50,
                }}

        provider = FakeProvider('{"decision":"WAIT"}')
        context = HistoryContext(current=provider)
        context.message_history_manager = SimpleNamespace(
            get=lambda **_kwargs: asyncio.sleep(0, result=history)
        )
        plugin = _plugin(context)
        event = _Event(components=[SimpleNamespace(qq="998")])
        event.set_extra("shio.sys001.generation", 1)
        self.assertEqual("WAIT", await plugin._decide_natural_participation(event, _snapshot()))
        prompt = provider.calls[0]["prompt"]
        self.assertIn("sender_id=202", prompt)
        self.assertIn("DIRECT_OTHER", prompt)
        self.assertNotIn("past context", prompt)
        self.assertNotIn("OFFICIAL_HISTORY=", prompt)

    async def test_continuous_missing_terminal_fails_closed_in_bounded_time(self):
        plugin = _plugin(FakeContext(), group={
            "continuous_window_enabled": True, "continuous_window_seconds": 1,
        })
        plugin._continuous_scopes = {}
        plugin._continuous_locks = {}
        plugin._continuous_terminated = False
        self.assertEqual("immediate", await plugin._continuous_begin("scope", "first"))
        self.assertEqual("wait", await plugin._continuous_begin("scope", "winner"))
        # The first turn never reaches response/after-send.  The winner must
        # not wait forever for a terminal hook that AstrBot may not emit.
        self.assertFalse(await asyncio.wait_for(
            plugin._continuous_wait_for_turn("scope", "winner"), 2.5
        ))
        # The late first turn may still return through AstrBot, but cannot
        # mutate this released scope or supersede a fresh generation.
        await plugin._continuous_mark_terminal("scope", "first")
        self.assertEqual("immediate", await plugin._continuous_begin("scope", "fresh"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
