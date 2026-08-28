from __future__ import annotations

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

from astrbot_plugin_shio.core.contracts import ActionKind, ParticipationLevel
from astrbot_plugin_shio.core.generation_epoch import (
    GenerationEpochSnapshot,
    event_epoch_validation,
)


_SEMANTIC_REPLY = (
    '{"decision":"REPLY","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"natural_continuation",'
    '"confidence":0.9}'
)


class P5ParticipationCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.provider = FakeProvider([])
        self.plugin = main.ShioPlugin(
            FakeContext(self.provider),
            {
                "persona_name": "亚托莉",
                "natural_name_wake_aliases": ["亚托莉", "萝卜子"],
                "prefer_livingmemory_group_history": False,
                "natural_group_participation_enabled": True,
                "natural_group_participation_allowlist": [
                    "p5-cancel-same",
                    "p5-cancel-react",
                ],
                "natural_group_participation_min_context_messages": 2,
            },
        )
        self.turn = 0

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()
        self.temp.cleanup()

    def _event(
        self,
        sender: str,
        message: str,
        *,
        group: str,
        direct: bool = False,
    ) -> FakeEvent:
        self.turn += 1
        event = FakeEvent(sender, message, group_id=group)
        event.message_id = f"p5-cancel-{self.turn}"
        event.is_at_or_wake_command = direct
        return event

    async def _prepare(self, event: FakeEvent, *, now: float):
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=now):
            self.plugin.admit_ingress_event(event)
        if event.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_REQUEST) is not None:
            self.provider.outputs.append(_SEMANTIC_REPLY)
            await self.plugin._resolve_participation_semantic(event)
        request = FakeRequest(event.message)
        await self.plugin.enforce_agent_permission(event, request)
        await self.plugin.build_persona_reply(event, request)
        return request, event.get_extra(main.SHIO_PLANNED_ACTION)

    def _prime(self, group: str, *, now: float) -> None:
        prior = self._event(
            "peer-b",
            "刚才的话题还没说完",
            group=group,
        )
        with patch("astrbot_plugin_shio.main.time.monotonic", return_value=now):
            self.plugin.admit_ingress_event(prior)

    async def test_new_same_scope_turn_drops_old_natural_join_before_guard(self):
        self._prime("p5-cancel-same", now=90.0)
        old = self._event("peer-a", "大家今晚吃什么？", group="p5-cancel-same")
        _, old_plan = await self._prepare(old, now=100.0)
        self.assertIs(old_plan.kind, ActionKind.REPLY)

        new = self._event(
            "peer-b",
            "萝卜子，你在吗？",
            group="p5-cancel-same",
            direct=True,
        )
        await self._prepare(new, now=101.0)

        response = FakeResponse("这是旧的自然接话结果。")
        await self.plugin.guard_persona_reply(old, response)
        self.assertEqual(response.completion_text, "")
        self.assertEqual(
            event_epoch_validation(old, self.plugin.generation_epochs).reason_code,
            "stale_generation_epoch",
        )

    async def test_new_same_scope_turn_invalidates_react_but_other_scope_does_not(self):
        self._prime("p5-cancel-react", now=190.0)
        reaction = self._event(
            "peer-a",
            "我觉得萝卜子这个表情好可爱哈哈",
            group="p5-cancel-react",
        )
        _, reaction_plan = await self._prepare(reaction, now=200.0)
        decision = reaction.get_extra(main.SHIO_PARTICIPATION_REACTION)
        self.assertIs(decision.decision.level, ParticipationLevel.REACT_ONLY)
        self.assertIs(reaction_plan.kind, ActionKind.REACT)

        other = self._event(
            "peer-b",
            "萝卜子，你好",
            group="p5-cancel-other",
            direct=True,
        )
        await self._prepare(other, now=201.0)
        self.assertTrue(
            event_epoch_validation(reaction, self.plugin.generation_epochs).is_current
        )

        newer = self._event(
            "peer-c",
            "萝卜子，这是同群的新问题",
            group="p5-cancel-react",
            direct=True,
        )
        await self._prepare(newer, now=202.0)
        self.assertFalse(
            event_epoch_validation(reaction, self.plugin.generation_epochs).is_current
        )

    async def test_unrelated_no_action_turn_does_not_cancel_direct_reply(self):
        direct = self._event(
            "peer-a",
            "萝卜子，他们刚才在聊什么？",
            group="p5-cancel-direct-priority",
            direct=True,
        )
        direct_request, direct_plan = await self._prepare(direct, now=250.0)
        self.assertIs(direct_plan.kind, ActionKind.REPLY)

        unrelated = self._event(
            "peer-b",
            "小林，这个问题你怎么看？",
            group="p5-cancel-direct-priority",
        )
        unrelated.get_messages = lambda: [
            type("At", (), {"type": "at", "qq": "peer-c"})()
        ]
        self.plugin.admit_ingress_event(unrelated)

        self.assertTrue(
            event_epoch_validation(direct, self.plugin.generation_epochs).is_current
        )
        self.assertIsNotNone(direct_request)

    async def test_snapshot_is_registry_owned_copy_and_mutation_fail_closed(self):
        event = self._event(
            "peer-a",
            "萝卜子，你好",
            group="p5-cancel-canonical",
            direct=True,
        )
        await self._prepare(event, now=300.0)
        snapshot = event.get_extra(main.GENERATION_EPOCH_EXTRA)
        self.assertIs(type(snapshot), GenerationEpochSnapshot)

        with self.assertRaises(TypeError):
            GenerationEpochSnapshot(
                scope_key=snapshot.scope_key,
                session_id=snapshot.session_id,
                epoch=snapshot.epoch,
                started_at=snapshot.started_at,
            )

        copied = copy.copy(snapshot)
        copied_validation = self.plugin.generation_epochs.validate(copied)
        self.assertFalse(copied_validation.is_current)
        self.assertEqual(
            copied_validation.reason_code,
            "generation_epoch_snapshot_not_canonical",
        )

        original_epoch = snapshot.epoch
        object.__setattr__(snapshot, "epoch", original_epoch + 1)
        corrupt = self.plugin.generation_epochs.validate(snapshot)
        self.assertFalse(corrupt.is_current)
        self.assertEqual(
            corrupt.reason_code,
            "generation_epoch_snapshot_corrupt",
        )
        object.__setattr__(snapshot, "epoch", original_epoch)
        self.assertTrue(self.plugin.generation_epochs.validate(snapshot).is_current)

        original_scope = snapshot.scope_key
        marker = "SENSITIVE-SCOPE-MARKER"
        object.__setattr__(snapshot, "scope_key", marker)
        self.assertEqual(
            self.plugin.generation_epochs.validate(snapshot).reason_code,
            "generation_epoch_snapshot_corrupt",
        )
        self.assertNotIn(marker, repr(snapshot))
        self.assertNotIn(marker, repr(snapshot.trace_metadata()))
        object.__setattr__(snapshot, "scope_key", original_scope)
        self.assertTrue(self.plugin.generation_epochs.validate(snapshot).is_current)

    async def test_old_snapshot_cannot_be_mutated_into_current_authority(self):
        old = self._event(
            "peer-a",
            "萝卜子，你好",
            group="p5-cancel-forge",
            direct=True,
        )
        await self._prepare(old, now=400.0)
        old_snapshot = old.get_extra(main.GENERATION_EPOCH_EXTRA)

        new = self._event(
            "peer-b",
            "萝卜子，新问题",
            group="p5-cancel-forge",
            direct=True,
        )
        await self._prepare(new, now=401.0)
        current_epoch = self.plugin.generation_epochs.current(old_snapshot.scope_key)
        self.assertGreater(current_epoch, old_snapshot.epoch)

        original_epoch = old_snapshot.epoch
        object.__setattr__(old_snapshot, "epoch", current_epoch)
        forged = self.plugin.generation_epochs.validate(old_snapshot)
        self.assertFalse(forged.is_current)
        self.assertEqual(forged.reason_code, "generation_epoch_snapshot_corrupt")
        object.__setattr__(old_snapshot, "epoch", original_epoch)


if __name__ == "__main__":
    unittest.main()
