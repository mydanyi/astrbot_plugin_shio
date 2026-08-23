from __future__ import annotations

import dataclasses
import hashlib
import unittest

from astrbot_plugin_shio.core.capability_policy import CapabilityClass
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    ActionConfirmationPolicy,
    ActionEffectState,
    ActionReceiptStatus,
    ActionSideEffect,
    ContentIntent,
    ContentIntentKind,
    ContractViolation,
    DecisionBinding,
    GroundingFact,
    OwnerActionOperation,
    OwnerActionRequest,
)
from astrbot_plugin_shio.core.contracts import owner_action as owner_action_contracts


def _binding(message: str = "请查证当前问题", *, revision: int = 3) -> DecisionBinding:
    return DecisionBinding(
        scope_key="platform:test|bot:shio|private:owner-a",
        session_id="session-owner-a",
        current_message_id=f"message-{revision}",
        current_sender_key="platform:test|user:owner-a",
        current_content_digest=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        conversation_revision=revision,
        generation_epoch=revision + 2,
        trace_id=f"{revision:032x}",
    )


def _target(binding: DecisionBinding) -> ReplyTarget:
    return ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )


def _fact(binding: DecisionBinding, suffix: str) -> GroundingFact:
    return GroundingFact(
        binding=binding,
        fact_id=f"grounding-{suffix}",
        claim=f"公开资料结论 {suffix}",
        source_kind="public_web",
        source_digest=hashlib.sha256(f"source-{suffix}".encode()).hexdigest(),
        confidence=0.9,
        observed_at=10.0,
    )


def _intent(binding: DecisionBinding, facts: tuple[object, ...]) -> ContentIntent:
    return ContentIntent(
        binding=binding,
        reply_target=_target(binding),
        kind=ContentIntentKind.ANSWER,
        grounding_facts=facts,  # type: ignore[arg-type]
        answer_language="zh-CN",
    )


def _canonical_read_receipt(binding: DecisionBinding):
    request = OwnerActionRequest(
        binding=binding,
        action_id="1" * 64,
        adapter_id="artifact-read-exact",
        adapter_version="1.0.0",
        request_digest="2" * 64,
        parameter_digest="3" * 64,
        capability=CapabilityClass.ARTIFACT_READ,
        operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
        side_effect=ActionSideEffect.SCOPED_READ,
        issued_at=1.0,
        deadline=30.0,
        confirmation_policy=ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST,
        idempotency_key="4" * 64,
        call_budget=1,
        reason_codes=("owner_current_explicit_request",),
    )
    output = owner_action_contracts._issue_test_action_output(
        request,
        "有界 owner-only 读取结果",
    )
    return owner_action_contracts._issue_action_receipt(
        request=request,
        completion_binding=binding,
        pending_confirmation=None,
        status=ActionReceiptStatus.SUCCEEDED,
        effect_state=ActionEffectState.NO_SIDE_EFFECT,
        source_interface_digest="5" * 64,
        result_digest="6" * 64,
        output=output,
        started_at=2.0,
        completed_at=3.0,
        attempt_count=1,
        reason_codes=("owner_action_succeeded",),
    )


class DerivedGroundingFact(GroundingFact):
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class BindingCarrier:
    binding: DecisionBinding


class ContentIntentGroundingTypeGateTests(unittest.TestCase):
    def test_canonical_action_receipt_cannot_enter_grounding_facts_by_binding(self):
        binding = _binding()
        receipt = _canonical_read_receipt(binding)
        self.assertTrue(receipt.is_canonical)
        self.assertEqual(receipt.binding, binding)

        with self.assertRaisesRegex(ContractViolation, "grounding_fact_type_invalid"):
            _intent(binding, (receipt,))

    def test_subclass_and_arbitrary_binding_object_are_rejected(self):
        binding = _binding()
        exact = _fact(binding, "exact")
        derived = DerivedGroundingFact(
            binding=exact.binding,
            fact_id=exact.fact_id,
            claim=exact.claim,
            source_kind=exact.source_kind,
            source_digest=exact.source_digest,
            confidence=exact.confidence,
            observed_at=exact.observed_at,
        )
        for value in (derived, BindingCarrier(binding), object()):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ContractViolation,
                    "grounding_fact_type_invalid",
                ):
                    _intent(binding, (value,))

    def test_multiple_exact_grounding_facts_still_pass(self):
        binding = _binding()
        facts = (_fact(binding, "one"), _fact(binding, "two"))
        intent = _intent(binding, facts)
        self.assertEqual(intent.grounding_facts, facts)
        self.assertEqual(intent.trace_metadata()["grounding_fact_count"], 2)

    def test_existing_binding_mismatch_check_remains(self):
        binding = _binding(revision=3)
        other = _binding("另一轮问题", revision=4)
        with self.assertRaisesRegex(
            ContractViolation,
            "grounding_fact_binding_mismatch",
        ):
            _intent(binding, (_fact(other, "other-turn"),))


if __name__ == "__main__":
    unittest.main()
