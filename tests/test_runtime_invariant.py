from __future__ import annotations

import unittest
from dataclasses import replace

from astrbot_plugin_shio.core.action_planner import PlannedAction, StructuralOutcome
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import ActionDecision, ActionKind, DecisionBinding

from astrbot_plugin_shio.core.identity import (
    TurnEnvelope,
    build_scope_key,
    build_sender_key,
    resolve_principal,
)
from astrbot_plugin_shio.core.runtime_invariant import (
    typed_runtime_decision,
    validate_typed_turn,
)


def private_envelope(sender_id: str = "owner-a") -> TurnEnvelope:
    scope_key = build_scope_key(
        platform_id="test-platform",
        bot_id="test-bot",
        chat_type="private",
        session_id="private-session",
    )
    return TurnEnvelope(
        session_id="private-session",
        message_id="message-current",
        scope_key=scope_key,
        sender_key=build_sender_key(scope_key, sender_id),
        sender_id=sender_id,
        platform_id="test-platform",
        bot_id="test-bot",
        chat_type="private",
        group_id="",
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=1_765_000_000.0,
        timestamp_source="fixture",
        source_kind="inbound",
        degradation_reasons=(),
    )


def group_envelope(sender_id: str) -> TurnEnvelope:
    private = private_envelope(sender_id)
    scope_key = build_scope_key(
        platform_id="test-platform",
        bot_id="test-bot",
        chat_type="group",
        group_id="test-group",
    )
    return replace(
        private,
        session_id="test-group",
        scope_key=scope_key,
        sender_key=build_sender_key(scope_key, sender_id),
        chat_type="group",
        group_id="test-group",
    )


def principal_for(envelope: TurnEnvelope, *, owner_ids=("owner-a",)):
    return resolve_principal(
        sender_id=envelope.sender_id,
        sender_key=envelope.sender_key,
        chat_type=envelope.chat_type,
        owner_ids=owner_ids,
        verification_source="astrbot_event_sender_id",
        identity_verified=True,
    )


def planned_action(kind: ActionKind) -> PlannedAction:
    envelope = private_envelope("guest-a")
    binding = DecisionBinding(
        scope_key=envelope.scope_key,
        session_id=envelope.session_id,
        current_message_id=envelope.message_id,
        current_sender_key=envelope.sender_key,
        current_content_digest="a" * 64,
        conversation_revision=1,
        generation_epoch=1,
        trace_id="b" * 32,
    )
    target = ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    kwargs = {"binding": binding, "kind": kind}
    outcome = StructuralOutcome.NO_ACTION
    if kind in {ActionKind.REPLY, ActionKind.USE_TOOL, ActionKind.REACT}:
        kwargs["reply_target"] = target
        outcome = (
            StructuralOutcome.REACT
            if kind is ActionKind.REACT
            else StructuralOutcome.CONTINUE
        )
    if kind is ActionKind.USE_TOOL:
        kwargs["capability_intent"] = "public_web_read"
    if kind is ActionKind.REACT:
        kwargs["expression_intent"] = "light_reaction"
    if kind is ActionKind.WAIT:
        kwargs["wait_until"] = 10.0
        outcome = StructuralOutcome.WAIT
    return PlannedAction(
        action=ActionDecision(**kwargs),
        structural_outcome=outcome,
        planner_reason_codes=("fixture",),
    )


class TypedRuntimeInvariantTests(unittest.TestCase):
    def test_activation_requires_a_fully_bound_turn(self):
        blocked = typed_runtime_decision(activation_ready=False)
        awaiting = typed_runtime_decision(activation_ready=True)
        ready = typed_runtime_decision(
            activation_ready=True,
            planned_action=planned_action(ActionKind.REPLY),
        )

        self.assertFalse(blocked.activate_typed_pipeline)
        self.assertEqual(blocked.activation_status, "typed_fail_closed")
        self.assertFalse(awaiting.activate_typed_pipeline)
        self.assertEqual(awaiting.activation_status, "typed_awaiting_action")
        self.assertTrue(ready.activate_typed_pipeline)
        self.assertEqual(ready.activation_status, "typed_ready")

    def test_execution_budgets_are_derived_from_action(self):
        expected = {
            ActionKind.REPLY: (0, 1, 1),
            ActionKind.USE_TOOL: (1, 1, 1),
            ActionKind.REACT: (0, 0, 0),
            ActionKind.WAIT: (0, 0, 0),
            ActionKind.NO_ACTION: (0, 0, 0),
        }
        for kind, budgets in expected.items():
            with self.subTest(kind=kind.value):
                decision = typed_runtime_decision(
                    activation_ready=True,
                    planned_action=planned_action(kind),
                )
                self.assertEqual(
                    (
                        decision.acquisition_call_budget,
                        decision.final_generation_budget,
                        decision.final_send_budget,
                    ),
                    budgets,
                )
                self.assertEqual(decision.action_kind, kind.value)
                self.assertEqual(
                    decision.trace_metadata()["typed_runtime"],
                    "typed_only",
                )

    def test_all_trusted_direct_identities_are_accepted(self):
        for envelope in (
            private_envelope("owner-a"),
            private_envelope("guest-a"),
            group_envelope("owner-a"),
            group_envelope("guest-a"),
        ):
            with self.subTest(
                chat_type=envelope.chat_type,
                sender_id=envelope.sender_id,
            ):
                gate = validate_typed_turn(
                    envelope=envelope,
                    principal=principal_for(envelope),
                )
                self.assertTrue(gate.activation_ready)
                self.assertEqual(gate.status, "typed_identity_ready")
                self.assertEqual(gate.reason_codes, ())

    def test_untrusted_identity_fails_closed(self):
        envelope = private_envelope()
        verified = principal_for(envelope)
        forged = type(verified)(
            sender_key=verified.sender_key,
            sender_id=verified.sender_id,
            is_owner=True,
            relationship_role="owner",
            verification_source="message_text:configured_owner_id",
        )
        mismatched = replace(verified, sender_key=verified.sender_key + "-other")
        for name, principal in (
            ("forged", forged),
            ("mismatched", mismatched),
        ):
            with self.subTest(case=name):
                gate = validate_typed_turn(
                    envelope=envelope,
                    principal=principal,
                )
                self.assertFalse(gate.activation_ready)
                self.assertEqual(gate.status, "typed_identity_denied")
                self.assertTrue(gate.reason_codes)

    def test_missing_structural_target_fails_closed(self):
        envelope = private_envelope()
        degraded = replace(envelope, message_id="")
        gate = validate_typed_turn(
            envelope=degraded,
            principal=principal_for(degraded),
        )

        self.assertFalse(gate.activation_ready)
        self.assertIn("target_identity_incomplete", gate.reason_codes)


if __name__ == "__main__":
    unittest.main()
