from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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

from astrbot_plugin_shio.core.affect_state import AffectMutationStatus
from astrbot_plugin_shio.core.contracts import ParticipationLevel, SenderKind
from astrbot_plugin_shio.core.conversation_event import (
    ConversationRevisionBook,
    build_ingress_event,
)
from astrbot_plugin_shio.core.group_scene import SceneMutationStatus
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.runtime_continuity import (
    CadenceContinuity,
    RuntimeContinuityError,
    RuntimeContinuityStore,
)


SCOPE = "platform:test|bot:shio|group:continuity-private-marker"
SENDER = f"{SCOPE}|user:continuity-user-marker"


def _envelope(message: str) -> TurnEnvelope:
    return TurnEnvelope(
        session_id="continuity-session-marker",
        message_id=message,
        scope_key=SCOPE,
        sender_key=SENDER,
        sender_id="continuity-user-marker",
        platform_id="test",
        bot_id="shio",
        chat_type="group",
        group_id="continuity-private-marker",
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=1.0,
        timestamp_source="message",
        source_kind="inbound",
        degradation_reasons=(),
    )


def _principal() -> PrincipalContext:
    return PrincipalContext(
        sender_key=SENDER,
        sender_id="continuity-user-marker",
        is_owner=False,
        relationship_role="group_peer",
        verification_source="synthetic_structured_sender",
    )


def _commit(book: ConversationRevisionBook, message: str):
    turn = _envelope(message)
    ingress = build_ingress_event(
        envelope=turn,
        principal=_principal(),
        sender_kind=SenderKind.HUMAN,
        content=f"private-content-{message}",
        revision_candidate=book.peek(SCOPE),
    )
    return book.commit(ingress, accepted=True, scope_key=SCOPE)


class RuntimeContinuityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "continuity"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_revision_and_cadence_survive_restart_without_plain_identifiers(self):
        first = RuntimeContinuityStore(
            self.root,
            max_scopes=8,
            max_subjects=8,
        )
        first.commit_revision(SCOPE, base_revision=0, next_revision=1)
        first.commit_cadence(
            SCOPE,
            SENDER,
            state=CadenceContinuity(
                last_evaluated_at=100.0,
                join_times=(100.0,),
                no_action_streak=0,
                backoff_until=0.0,
            ),
            now=100.0,
        )
        state_bytes = first.state_path.read_bytes()
        for forbidden in (
            SCOPE.encode(),
            SENDER.encode(),
            b"continuity-private-marker",
            b"continuity-user-marker",
        ):
            self.assertNotIn(forbidden, state_bytes)
        first.close()

        second = RuntimeContinuityStore(
            self.root,
            max_scopes=8,
            max_subjects=8,
        )
        self.assertEqual(second.revision_for_scope(SCOPE), 1)
        cadence = second.cadence_for_subject(SCOPE, SENDER, now=110.0)
        self.assertIs(type(cadence), CadenceContinuity)
        self.assertEqual(len(cadence.join_times), 1)
        self.assertGreater(cadence.join_times[0], 109.0)
        self.assertLessEqual(cadence.join_times[0], 110.0)
        self.assertEqual(cadence.no_action_streak, 0)
        self.assertNotIn(SCOPE, repr(second.trace_metadata()))
        self.assertNotIn(str(self.root), repr(second))
        second.close()

    def test_atomic_revision_commit_is_single_winner_and_bounded(self):
        store = RuntimeContinuityStore(
            self.root,
            max_scopes=2,
            max_subjects=2,
        )

        def commit_once() -> str:
            try:
                store.commit_revision(SCOPE, base_revision=0, next_revision=1)
            except RuntimeContinuityError as exc:
                return str(exc)
            return "committed"

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: commit_once(), range(32)))
        self.assertEqual(results.count("committed"), 1)
        self.assertEqual(store.revision_for_scope(SCOPE), 1)

        store.commit_revision("scope-two", base_revision=0, next_revision=1)
        with self.assertRaisesRegex(RuntimeContinuityError, "scope_capacity"):
            store.commit_revision("scope-three", base_revision=0, next_revision=1)
        metadata = store.trace_metadata()
        self.assertEqual(metadata["continuity_revision_count"], 2)
        self.assertTrue(metadata["continuity_state_bounded"])
        store.close()

    def test_corrupt_state_disables_persistence_without_leaking_path(self):
        healthy = RuntimeContinuityStore(self.root)
        healthy.commit_revision(SCOPE, base_revision=0, next_revision=1)
        state_path = healthy.state_path
        healthy.close()
        state_path.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")

        broken = RuntimeContinuityStore(self.root)
        self.assertFalse(broken.enabled)
        self.assertRegex(broken.failure_code, r"^continuity_[a-z_]+$")
        self.assertNotIn(str(self.root), broken.failure_code)
        self.assertNotIn(str(self.root), repr(broken.trace_metadata()))
        with self.assertRaisesRegex(RuntimeContinuityError, "unavailable"):
            broken.revision_for_scope(SCOPE)
        broken.close()

        isolated_root = Path(self.temp.name) / "secret-mismatch"
        sealed = RuntimeContinuityStore(isolated_root)
        sealed.commit_revision(SCOPE, base_revision=0, next_revision=1)
        sealed.close()
        (isolated_root / ".runtime_continuity_secret").write_bytes(b"x" * 32)
        mismatched = RuntimeContinuityStore(isolated_root)
        self.assertFalse(mismatched.enabled)
        self.assertEqual(mismatched.failure_code, "continuity_state_invalid")
        mismatched.close()

    def test_hot_reload_standby_recovers_after_previous_store_closes(self):
        first = RuntimeContinuityStore(self.root)
        first.commit_revision(SCOPE, base_revision=0, next_revision=1)
        standby = RuntimeContinuityStore(self.root)
        self.assertFalse(standby.enabled)
        self.assertEqual(standby.failure_code, "continuity_store_in_use")

        first.close()
        self.assertTrue(standby.enabled)
        self.assertEqual(standby.revision_for_scope(SCOPE), 1)
        standby.commit_revision(SCOPE, base_revision=1, next_revision=2)
        self.assertEqual(standby.revision_for_scope(SCOPE), 2)
        standby.close()

    def test_persist_fault_is_atomic_and_late_replace_recovers_without_replay(self):
        store = RuntimeContinuityStore(self.root)
        with patch(
            "astrbot_plugin_shio.core.runtime_continuity.os.replace",
            side_effect=RuntimeError("PRIVATE-PATH-MARKER"),
        ):
            with self.assertRaisesRegex(RuntimeContinuityError, "persist_failed"):
                store.commit_revision(SCOPE, base_revision=0, next_revision=1)
        self.assertFalse(store.enabled)
        self.assertNotIn("PRIVATE-PATH-MARKER", repr(store))
        store.close()
        unchanged = RuntimeContinuityStore(self.root)
        self.assertEqual(unchanged.revision_for_scope(SCOPE), 0)
        unchanged.close()

        late = RuntimeContinuityStore(self.root)
        real_replace = os.replace

        def replace_then_interrupt(source, target):
            real_replace(source, target)
            raise KeyboardInterrupt("PRIVATE-LATE-MARKER")

        with patch(
            "astrbot_plugin_shio.core.runtime_continuity.os.replace",
            side_effect=replace_then_interrupt,
        ):
            with self.assertRaisesRegex(RuntimeContinuityError, "persist_failed"):
                late.commit_revision(SCOPE, base_revision=0, next_revision=1)
        self.assertFalse(late.enabled)
        self.assertNotIn("PRIVATE-LATE-MARKER", repr(late))
        late.close()
        recovered = RuntimeContinuityStore(self.root)
        self.assertEqual(recovered.revision_for_scope(SCOPE), 1)
        with self.assertRaisesRegex(RuntimeContinuityError, "revision_stale"):
            recovered.commit_revision(SCOPE, base_revision=0, next_revision=1)
        recovered.close()

    def test_live_state_replacement_fails_closed_and_512_churn_stays_bounded(self):
        store = RuntimeContinuityStore(
            self.root,
            max_scopes=512,
            max_subjects=256,
        )
        for index in range(512):
            store.commit_revision(
                f"scope-{index}",
                base_revision=0,
                next_revision=1,
            )
        for index in range(256):
            now = float(index + 1)
            store.commit_cadence(
                f"scope-{index}",
                f"sender-{index}",
                state=CadenceContinuity(
                    last_evaluated_at=now,
                    join_times=(now,),
                    no_action_streak=0,
                    backoff_until=0.0,
                ),
                now=now,
            )
        metadata = store.trace_metadata()
        self.assertEqual(metadata["continuity_revision_count"], 512)
        self.assertEqual(metadata["continuity_cadence_count"], 256)
        self.assertTrue(metadata["continuity_state_bounded"])

        store.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "revisions": [],
                    "cadence": [],
                    "mac": "0" * 64,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        self.assertFalse(store.enabled)
        self.assertEqual(store.failure_code, "continuity_state_changed")
        store.close()


class RuntimeContinuityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _plugin(self):
        return main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "persona_name": "亚托莉",
                "natural_name_wake_aliases": ["亚托莉", "萝卜子"],
                "prefer_livingmemory_group_history": False,
            },
        )

    @staticmethod
    def _admit(plugin, *, turn: int, now: float, direct: bool = False):
        event = FakeEvent(
            "peer-a",
            "大家今晚吃什么？" if not direct else "萝卜子，你在吗？",
            group_id="p9-continuity-group",
        )
        event.message_id = f"p9-continuity-{turn}"
        event.is_at_or_wake_command = direct
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=now):
            result = plugin.admit_ingress_event(event)
        return event, result

    async def test_plugin_restart_restores_revision_cadence_affect_and_scene_continuity(self):
        first = self._plugin()
        event1, result1 = self._admit(first, turn=1, now=100.0)
        self.assertEqual(result1.conversation_event.binding.conversation_revision, 1)
        self.assertIs(
            event1.get_extra(main.SHIO_PARTICIPATION_CADENCE).decision.level,
            ParticipationLevel.MAY_JOIN,
        )
        await first.terminate()

        second = self._plugin()
        event2, result2 = self._admit(second, turn=2, now=110.0)
        self.assertEqual(result2.conversation_event.binding.conversation_revision, 2)
        self.assertEqual(second.conversation_revisions.restored_revision(
            result2.conversation_event.envelope.scope_key
        ), 1)
        self.assertIs(
            event2.get_extra(main.SHIO_PARTICIPATION_CADENCE).decision.level,
            ParticipationLevel.WAIT,
        )
        self.assertIs(
            event2.get_extra(main.SHIO_AFFECT_STATE_MUTATION).status,
            AffectMutationStatus.ACCEPTED_HUMAN,
        )
        self.assertIs(
            event2.get_extra(main.SHIO_GROUP_SCENE_MUTATION).status,
            SceneMutationStatus.ACCEPTED_HUMAN,
        )
        await second.terminate()

    async def test_corrupt_continuity_blocks_unsolicited_join_but_direct_reply_survives(self):
        plugin = self._plugin()
        plugin.runtime_continuity.state_path.write_text("not-json", encoding="utf-8")
        await plugin.terminate()

        degraded = self._plugin()
        self.assertFalse(degraded.runtime_continuity.enabled)
        passive_event, passive = self._admit(degraded, turn=1, now=200.0)
        self.assertIsNotNone(passive)
        self.assertIs(
            passive_event.get_extra(main.SHIO_PARTICIPATION_CADENCE).decision.level,
            ParticipationLevel.WAIT,
        )
        direct_event, direct = self._admit(
            degraded,
            turn=2,
            now=201.0,
            direct=True,
        )
        self.assertIsNotNone(direct)
        self.assertIs(
            direct_event.get_extra(main.SHIO_PARTICIPATION_CADENCE).decision.level,
            ParticipationLevel.MUST_REPLY,
        )
        await degraded.terminate()


if __name__ == "__main__":
    unittest.main()
