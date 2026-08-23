from __future__ import annotations

import copy
import dataclasses
import json
import unittest

from astrbot_plugin_shio.core.accepted_turn_authority import (
    AcceptedTurnConsumer,
    AcceptedTurnDisposition,
)
from astrbot_plugin_shio.core.capability_policy import (
    build_guest_capability_policy,
)
from astrbot_plugin_shio.core.affect_state import (
    issue_affect_admission_evidence,
)
from astrbot_plugin_shio.core.relationship_state import (
    RelationshipBand,
    RelationshipEventKind,
    RelationshipMutationStatus,
    RelationshipStateBook,
    inspect_relationship_render_context,
    inspect_relationship_state,
)
from astrbot_plugin_shio.tests.test_affect_state import (
    _accepted_turn_authority,
    _admission,
    _sent_reply_evidence,
)
from astrbot_plugin_shio.core.conversation_event import ConversationRevisionBook
from astrbot_plugin_shio.core.ingress_admission import IngressAdmissionController


class RelationshipStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.revisions = ConversationRevisionBook()
        self.controller = IngressAdmissionController(self.revisions)
        self.authority = _accepted_turn_authority(self.controller)
        self.book = RelationshipStateBook(
            accepted_turn_authority=self.authority,
            max_subjects=16,
            max_receipts_per_scope=16,
        )

    def _record(self, *, group: str, sender: str, message: str, content: str, now: float):
        admission, _observation = _admission(
            self.controller,
            self.revisions,
            group=group,
            sender=sender,
            message=message,
            content=content,
        )
        event = admission.conversation_event
        assert event is not None
        dispatch = self.authority.dispatch(admission)
        ticket = self.authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.RELATIONSHIP_STATE,
        )
        mutation = self.book.record_human(
            ticket,
            current_message=content,
            now=now,
        )
        self.authority.finalize_unclaimed_turn(
            dispatch,
            binding=event.binding,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )
        return event, mutation

    def test_fixed_accepted_turn_fanout_contains_relationship_consumer(self):
        self.assertIn(
            AcceptedTurnConsumer.RELATIONSHIP_STATE,
            tuple(AcceptedTurnConsumer),
        )

    def test_concrete_positive_and_negative_events_are_explainable(self):
        _event, first = self._record(
            group="warm",
            sender="peer-a",
            message="warm-1",
            content="谢谢你，刚才真的帮到我了",
            now=100.0,
        )
        _event, second = self._record(
            group="warm",
            sender="peer-a",
            message="warm-2",
            content="你真靠谱，谢谢",
            now=101.0,
        )
        self.assertIs(first.status, RelationshipMutationStatus.ACCEPTED_HUMAN)
        self.assertIs(second.state.band, RelationshipBand.WARM)
        self.assertIs(inspect_relationship_state(second.state), second.state)
        with self.assertRaisesRegex(Exception, "relationship_state_not_canonical"):
            inspect_relationship_state(copy.copy(second.state))
        self.assertGreater(second.state.affinity, 0.0)
        self.assertIn(
            RelationshipEventKind.POSITIVE_INTERACTION.value,
            second.state.explanation_reason_codes,
        )

        _event, cautious = self._record(
            group="cautious",
            sender="peer-b",
            message="cautious-1",
            content="你刚才答非所问，而且把人认错了",
            now=200.0,
        )
        self.assertIs(cautious.state.band, RelationshipBand.CAUTIOUS)
        self.assertLess(cautious.state.affinity, 0.0)
        self.assertIn(
            RelationshipEventKind.NEGATIVE_INTERACTION.value,
            cautious.state.explanation_reason_codes,
        )

    def test_state_is_sender_and_scope_isolated(self):
        event_a, _ = self._record(
            group="isolation",
            sender="peer-a",
            message="a-1",
            content="谢谢你，真棒",
            now=100.0,
        )
        event_b, state_b = self._record(
            group="isolation",
            sender="peer-b",
            message="b-1",
            content="普通事实陈述",
            now=101.0,
        )
        other_a, _ = self._record(
            group="other",
            sender="peer-a",
            message="other-a-1",
            content="普通事实陈述",
            now=102.0,
        )

        state_a = self.book.get_state(
            event_a.binding.scope_key,
            event_a.binding.current_sender_key,
        )
        isolated_b = self.book.get_state(
            event_b.binding.scope_key,
            event_b.binding.current_sender_key,
        )
        isolated_other = self.book.get_state(
            other_a.binding.scope_key,
            other_a.binding.current_sender_key,
        )
        self.assertGreater(state_a.affinity, 0.0)
        self.assertEqual(isolated_b, state_b.state)
        self.assertEqual(isolated_b.affinity, 0.0)
        self.assertEqual(isolated_other.affinity, 0.0)

    def test_exact_successful_receipt_updates_only_its_target(self):
        admission, _observation = _admission(
            self.controller,
            self.revisions,
            group="receipt",
            sender="peer-a",
            message="receipt-1",
            content="普通问题",
        )
        event = admission.conversation_event
        assert event is not None
        dispatch = self.authority.dispatch(admission)
        relationship_ticket = self.authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.RELATIONSHIP_STATE,
        )
        affect_ticket = self.authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.AFFECT_STATE,
        )
        relation = self.book.record_human(
            relationship_ticket,
            current_message="普通问题",
            now=100.0,
        )
        affect_evidence = issue_affect_admission_evidence(
            affect_ticket,
            dispatch=dispatch,
            accepted_turn_authority=self.authority,
        )
        receipt_evidence = _sent_reply_evidence(affect_evidence)
        self.authority.finalize_unclaimed_turn(
            dispatch,
            binding=event.binding,
            disposition=AcceptedTurnDisposition.SKIPPED,
        )

        settled = self.book.record_shio_receipt(receipt_evidence, now=204.0)
        self.assertIs(settled.status, RelationshipMutationStatus.ACCEPTED_OUTBOUND)
        self.assertEqual(settled.state.successful_reply_count, 1)
        self.assertEqual(
            settled.state.explanation_reason_codes[-1],
            RelationshipEventKind.SUCCESSFUL_REPLY.value,
        )
        replay = self.book.record_shio_receipt(receipt_evidence, now=205.0)
        self.assertIs(replay.status, RelationshipMutationStatus.REJECTED)
        self.assertIs(replay.state, settled.state)
        self.assertEqual(relation.state.object_key, settled.state.object_key)

    def test_render_context_is_canonical_private_and_cannot_change_capability(self):
        event, mutation = self._record(
            group="render",
            sender="peer-a",
            message="render-1",
            content="谢谢你，真可靠",
            now=100.0,
        )
        context = self.book.issue_render_context(
            mutation,
            binding=event.binding,
        )
        self.assertIs(inspect_relationship_render_context(context), context)
        with self.assertRaisesRegex(Exception, "relationship_render_context_not_canonical"):
            inspect_relationship_render_context(copy.copy(context))
        original_band = context.band
        object.__setattr__(context, "band", RelationshipBand.CAUTIOUS)
        with self.assertRaisesRegex(Exception, "relationship_render_context_corrupt"):
            inspect_relationship_render_context(context)
        object.__setattr__(context, "band", original_band)
        self.assertIs(inspect_relationship_render_context(context), context)
        original_revision = context.binding.conversation_revision
        object.__setattr__(context.binding, "conversation_revision", True)
        with self.assertRaisesRegex(Exception, "relationship_render_context_corrupt"):
            inspect_relationship_render_context(context)
        object.__setattr__(
            context.binding,
            "conversation_revision",
            original_revision,
        )
        self.assertIs(inspect_relationship_render_context(context), context)

        principal = event.principal
        before = build_guest_capability_policy(
            principal,
            configured_tool_names=("web_search",),
        )
        after = build_guest_capability_policy(
            principal,
            configured_tool_names=("web_search",),
        )
        self.assertEqual(before, after)
        self.assertFalse(after.shell_exec)
        self.assertFalse(after.agent_full)

        rendered = repr(context) + json.dumps(
            context.trace_metadata(),
            ensure_ascii=False,
        )
        for forbidden in (
            event.binding.scope_key,
            event.binding.current_sender_key,
            event.binding.current_message_id,
            "谢谢你，真可靠",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertFalse(hasattr(context, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.affinity = 1.0


if __name__ == "__main__":
    unittest.main()
