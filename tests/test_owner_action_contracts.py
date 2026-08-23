from __future__ import annotations

import copy
import dataclasses
import json
import unittest

from astrbot_plugin_shio.core.capability_policy import CapabilityClass
from astrbot_plugin_shio.core.contracts import (
    ActionConfirmationPolicy,
    ActionEffectState,
    ActionOutput,
    ActionOutputKind,
    ActionOutputVisibility,
    ActionReceipt,
    ActionReceiptStatus,
    ActionSideEffect,
    ContractViolation,
    DecisionBinding,
    OwnerActionOperation,
    OwnerActionProposal,
    OwnerActionRequest,
    PendingConfirmation,
    parse_owner_action_proposal,
)
from astrbot_plugin_shio.core.contracts import owner_action as owner_action_contracts


def binding(
    *,
    group: str = "group-a",
    sender: str = "owner-a",
    session: str = "session-a",
    message: str = "msg-a",
    revision: int = 3,
    epoch: int = 5,
    content_digest: str = "a" * 64,
) -> DecisionBinding:
    scope = f"platform:test|bot:shio|group:{group}"
    return DecisionBinding(
        scope_key=scope,
        session_id=session,
        current_message_id=message,
        current_sender_key=f"{scope}|user:{sender}",
        current_content_digest=content_digest,
        conversation_revision=revision,
        generation_epoch=epoch,
        trace_id="b" * 32,
    )


def request(
    *,
    bound: DecisionBinding | None = None,
    operation: OwnerActionOperation = OwnerActionOperation.ARTIFACT_READ_EXACT,
    capability: CapabilityClass = CapabilityClass.ARTIFACT_READ,
    side_effect: ActionSideEffect = ActionSideEffect.SCOPED_READ,
    confirmation_policy: ActionConfirmationPolicy = (
        ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST
    ),
    issued_at: float = 10.0,
    deadline: float = 120.0,
) -> OwnerActionRequest:
    return OwnerActionRequest(
        binding=bound or binding(),
        action_id="1" * 64,
        adapter_id="artifact-read-exact",
        adapter_version="1.0.0",
        request_digest="2" * 64,
        parameter_digest="3" * 64,
        capability=capability,
        operation=operation,
        side_effect=side_effect,
        issued_at=issued_at,
        deadline=deadline,
        confirmation_policy=confirmation_policy,
        idempotency_key="4" * 64,
        call_budget=1,
        reason_codes=("owner_current_explicit_request",),
    )


def shell_request(*, bound: DecisionBinding | None = None) -> OwnerActionRequest:
    return OwnerActionRequest(
        binding=bound or binding(),
        action_id="5" * 64,
        adapter_id="sandbox-shell-once",
        adapter_version="1.0.0",
        request_digest="6" * 64,
        parameter_digest="7" * 64,
        capability=CapabilityClass.SHELL_EXEC,
        operation=OwnerActionOperation.SANDBOX_SHELL_ONCE,
        side_effect=ActionSideEffect.CODE_EXECUTION,
        issued_at=20.0,
        deadline=200.0,
        confirmation_policy=ActionConfirmationPolicy.SECOND_TURN_REQUIRED,
        idempotency_key="8" * 64,
        call_budget=1,
        reason_codes=("owner_current_explicit_request",),
    )


def pending_for(
    action_request: OwnerActionRequest,
    *,
    consumed: bool = False,
) -> PendingConfirmation:
    pending = PendingConfirmation.from_request(
        action_request,
        pending_id="a" * 64,
        issued_at=30.0,
        expires_at=90.0,
    )
    if not consumed:
        return pending
    return pending.consume_confirmation(
        confirmation_binding=binding(
            message="msg-confirm",
            revision=4,
            epoch=6,
            content_digest="c" * 64,
        ),
        request_digest=action_request.request_digest,
        parameter_digest=action_request.parameter_digest,
        confirmation_proof_digest="d" * 64,
        now=50.0,
    )


def text_output(
    action_request: OwnerActionRequest,
    text: str,
    *,
    bound: DecisionBinding | None = None,
) -> ActionOutput:
    if bound is not None and bound is not action_request.binding:
        return ActionOutput.from_safe_text(
            binding=bound,
            request_digest=action_request.request_digest,
            text=text,
        )
    return owner_action_contracts._issue_test_action_output(action_request, text)


def issue_receipt(
    action_request: OwnerActionRequest,
    *,
    status: ActionReceiptStatus,
    effect_state: ActionEffectState,
    completion_binding: DecisionBinding | None = None,
    pending_confirmation: PendingConfirmation | None = None,
    output: ActionOutput | None = None,
    result_digest: str = "",
    confirmation_proof_digest: str = "",
    source_interface_digest: str | None = None,
    attempt_count: int | None = None,
    started_at: float = 30.0,
    completed_at: float = 31.0,
    reason_codes: tuple[str, ...] = ("action_contract_test",),
) -> ActionReceipt:
    attempted = status in {
        ActionReceiptStatus.SUCCEEDED,
        ActionReceiptStatus.FAILED,
        ActionReceiptStatus.TIMED_OUT,
        ActionReceiptStatus.EFFECT_UNKNOWN,
    } or (status is ActionReceiptStatus.STALE and effect_state is not ActionEffectState.NOT_STARTED)
    return owner_action_contracts._issue_action_receipt(
        request=action_request,
        completion_binding=completion_binding or action_request.binding,
        pending_confirmation=pending_confirmation,
        status=status,
        effect_state=effect_state,
        source_interface_digest=(
            ("9" * 64 if attempted else "")
            if source_interface_digest is None
            else source_interface_digest
        ),
        result_digest=result_digest,
        confirmation_proof_digest=confirmation_proof_digest,
        output=output,
        started_at=started_at,
        completed_at=completed_at,
        attempt_count=(1 if attempted else 0) if attempt_count is None else attempt_count,
        reason_codes=reason_codes,
    )


class OwnerActionProposalContractTests(unittest.TestCase):
    def test_closed_operations_bind_to_broad_capability_only(self):
        cases = (
            (
                OwnerActionOperation.ARTIFACT_READ_EXACT,
                CapabilityClass.ARTIFACT_READ,
            ),
            (OwnerActionOperation.ARTIFACT_GREP, CapabilityClass.ARTIFACT_READ),
            (OwnerActionOperation.MEMORY_WRITE_LITERAL, CapabilityClass.MEMORY_WRITE),
            (OwnerActionOperation.SANDBOX_SHELL_ONCE, CapabilityClass.SHELL_EXEC),
        )
        for operation, capability in cases:
            with self.subTest(operation=operation):
                proposal = OwnerActionProposal(
                    binding=binding(),
                    capability=capability,
                    operation=operation,
                    confidence=0.95,
                    reason_codes=("current_explicit_action",),
                )
                self.assertEqual(proposal.capability, capability)
                self.assertEqual(proposal.operation, operation)

        with self.assertRaises(ContractViolation):
            OwnerActionProposal(
                binding=binding(),
                capability=CapabilityClass.SHELL_EXEC,
                operation=OwnerActionOperation.ARTIFACT_GREP,
                confidence=0.95,
            )

    def test_model_payload_has_only_broad_capability_and_closed_operation(self):
        proposal = parse_owner_action_proposal(
            binding(),
            {
                "capability_intent": "artifact_read",
                "operation_intent": "artifact_grep",
                "confidence": 0.91,
                "reason_codes": ["explicit_current_request"],
            },
        )
        self.assertEqual(proposal.capability, CapabilityClass.ARTIFACT_READ)
        self.assertEqual(proposal.operation, OwnerActionOperation.ARTIFACT_GREP)
        fields = {field.name for field in dataclasses.fields(proposal)}
        self.assertEqual(
            fields,
            {"binding", "capability", "operation", "confidence", "reason_codes"},
        )

    def test_model_payload_rejects_every_authority_or_parameter_key(self):
        base = {
            "capability_intent": "artifact_read",
            "operation_intent": "artifact_read_exact",
            "confidence": 0.9,
        }
        authority_values = {
            "tool": "forbidden",
            "tool_name": "forbidden",
            "exact_tool": "forbidden",
            "source": "forbidden",
            "plugin_source": "forbidden",
            "args": {},
            "arguments": {},
            "parameters": {},
            "path": "forbidden",
            "command": "forbidden",
            "sender": "forbidden",
            "sender_id": "forbidden",
            "sender_key": "forbidden",
            "principal": "forbidden",
            "owner": True,
            "is_owner": True,
            "binding": {},
            "scope_key": "forbidden",
            "session_id": "forbidden",
            "message_id": "forbidden",
            "deadline": 100.0,
            "timeout": 10.0,
            "budget": 1,
            "call_budget": 1,
            "confirmation": True,
            "confirmation_policy": "forbidden",
            "confirmation_token": "forbidden",
            "idempotency": "forbidden",
            "idempotency_key": "forbidden",
            "adapter_id": "forbidden",
            "adapter_version": "forbidden",
            "request_digest": "0" * 64,
            "parameter_digest": "0" * 64,
            "action_id": "0" * 64,
        }
        for key, value in authority_values.items():
            with self.subTest(key=key), self.assertRaisesRegex(
                ContractViolation,
                "model_authority_field_forbidden",
            ):
                parse_owner_action_proposal(binding(), {**base, key: value})

    def test_model_payload_rejects_unknown_and_normalized_duplicate_keys(self):
        with self.assertRaises(ContractViolation):
            parse_owner_action_proposal(
                binding(),
                {
                    "capability_intent": "artifact_read",
                    "operation_intent": "artifact_read_exact",
                    "confidence": 0.9,
                    "style": "forbidden",
                },
            )
        with self.assertRaises(ContractViolation):
            parse_owner_action_proposal(
                binding(),
                {
                    "capability_intent": "artifact_read",
                    " Capability_Intent ": "artifact_read",
                    "operation_intent": "artifact_read_exact",
                    "confidence": 0.9,
                },
            )

    def test_proposal_is_frozen_binding_exact_and_repr_safe(self):
        proposal = OwnerActionProposal(
            binding=binding(),
            capability=CapabilityClass.ARTIFACT_READ,
            operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            confidence=0.9,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            proposal.confidence = 0.1
        proposal.assert_current_binding(binding())
        for changed in (
            binding(sender="owner-b"),
            binding(group="group-b"),
            binding(epoch=6),
        ):
            with self.subTest(changed=changed), self.assertRaises(ContractViolation):
                proposal.assert_current_binding(changed)
        rendered = repr(proposal) + json.dumps(proposal.trace_metadata())
        self.assertNotIn("owner-a", rendered)
        self.assertNotIn("group-a", rendered)
        self.assertNotIn("msg-a", rendered)


class OwnerActionRequestContractTests(unittest.TestCase):
    def test_request_has_no_generic_parameter_mapping_or_exact_tool_fields(self):
        value = request()
        fields = {field.name for field in dataclasses.fields(value)}
        self.assertIn("parameter_digest", fields)
        for forbidden in (
            "tool",
            "tool_name",
            "source",
            "args",
            "arguments",
            "parameters",
            "path",
            "command",
        ):
            self.assertNotIn(forbidden, fields)
        self.assertFalse(hasattr(value, "materialize_arguments"))

    def test_request_rejects_capability_effect_policy_and_deadline_mismatch(self):
        invalid = (
            {"capability": CapabilityClass.SHELL_EXEC},
            {"side_effect": ActionSideEffect.STATE_WRITE},
            {"confirmation_policy": ActionConfirmationPolicy.SECOND_TURN_REQUIRED},
            {"deadline": 10.0},
            {"deadline": float("inf")},
            {"request_digest": "raw"},
            {"parameter_digest": "raw"},
            {"idempotency_key": "raw"},
            {"action_id": "raw"},
            {"call_budget": 0},
            {"call_budget": 2},
            {"call_budget": True},
        )
        current = request()
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                dataclasses.replace(current, **change)

        valid_memory = request(
            operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
            capability=CapabilityClass.MEMORY_WRITE,
            side_effect=ActionSideEffect.STATE_WRITE,
            confirmation_policy=ActionConfirmationPolicy.SECOND_TURN_REQUIRED,
        )
        self.assertEqual(valid_memory.operation, OwnerActionOperation.MEMORY_WRITE_LITERAL)
        self.assertEqual(
            shell_request().confirmation_policy,
            ActionConfirmationPolicy.SECOND_TURN_REQUIRED,
        )

    def test_request_is_frozen_exact_bound_expiring_and_repr_safe(self):
        value = request()
        self.assertFalse(value.is_expired(119.9999))
        self.assertTrue(value.is_expired(120.0))
        self.assertTrue(value.is_expired(120.0001))
        value.assert_current_binding(binding())
        for changed in (
            binding(sender="owner-b"),
            binding(group="group-b"),
            binding(epoch=6),
        ):
            with self.subTest(changed=changed), self.assertRaises(ContractViolation):
                value.assert_current_binding(changed)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.deadline = 999.0

        rendered = repr(value) + json.dumps(value.trace_metadata())
        for secret in (
            value.binding.current_sender_key,
            value.binding.scope_key,
            value.action_id,
            value.request_digest,
            value.parameter_digest,
            value.idempotency_key,
        ):
            self.assertNotIn(secret, rendered)


class PendingConfirmationContractTests(unittest.TestCase):
    def pending(self) -> PendingConfirmation:
        return PendingConfirmation.from_request(
            shell_request(),
            pending_id="a" * 64,
            issued_at=30.0,
            expires_at=90.0,
        )

    def confirmation_binding(self, **changes) -> DecisionBinding:
        values = {
            "message": "msg-confirm",
            "revision": 4,
            "epoch": 6,
            "content_digest": "c" * 64,
        }
        values.update(changes)
        return binding(**values)

    def test_confirmation_requires_same_sender_scope_session_digest_and_ttl(self):
        pending = self.pending()
        consumed = pending.consume_confirmation(
            confirmation_binding=self.confirmation_binding(),
            request_digest=pending.request_digest,
            parameter_digest=pending.parameter_digest,
            confirmation_proof_digest="d" * 64,
            now=50.0,
        )
        self.assertTrue(consumed.consumed)
        self.assertEqual(consumed.confirmation_binding.generation_epoch, 6)

        failures = (
            {"confirmation_binding": self.confirmation_binding(sender="owner-b")},
            {"confirmation_binding": self.confirmation_binding(group="group-b")},
            {"confirmation_binding": self.confirmation_binding(session="session-b")},
            {"confirmation_binding": self.confirmation_binding(epoch=5)},
            {"confirmation_binding": self.confirmation_binding(revision=3)},
            {"confirmation_binding": binding()},
            {"request_digest": "e" * 64},
            {"parameter_digest": "e" * 64},
            {"confirmation_proof_digest": "raw"},
            {"now": 90.0},
            {"now": 90.0001},
        )
        for change in failures:
            kwargs = {
                "confirmation_binding": self.confirmation_binding(),
                "request_digest": pending.request_digest,
                "parameter_digest": pending.parameter_digest,
                "confirmation_proof_digest": "d" * 64,
                "now": 50.0,
            }
            kwargs.update(change)
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                pending.consume_confirmation(**kwargs)

    def test_confirmation_shape_rejects_replay_and_invalid_ttl_or_request(self):
        pending = self.pending().consume_confirmation(
            confirmation_binding=self.confirmation_binding(),
            request_digest="6" * 64,
            parameter_digest="7" * 64,
            confirmation_proof_digest="d" * 64,
            now=50.0,
        )
        with self.assertRaisesRegex(ContractViolation, "already_consumed"):
            pending.consume_confirmation(
                confirmation_binding=self.confirmation_binding(
                    message="msg-confirm-again",
                    revision=5,
                    epoch=7,
                    content_digest="e" * 64,
                ),
                request_digest="6" * 64,
                parameter_digest="7" * 64,
                confirmation_proof_digest="f" * 64,
                now=60.0,
            )

        with self.assertRaises(ContractViolation):
            PendingConfirmation.from_request(
                request(),
                pending_id="a" * 64,
                issued_at=30.0,
                expires_at=60.0,
            )
        with self.assertRaises(ContractViolation):
            PendingConfirmation.from_request(
                shell_request(),
                pending_id="a" * 64,
                issued_at=30.0,
                expires_at=30.0,
            )
        with self.assertRaises(ContractViolation):
            PendingConfirmation.from_request(
                shell_request(),
                pending_id="a" * 64,
                issued_at=30.0,
                expires_at=200.0001,
            )
        original = self.pending()
        with self.assertRaises(ContractViolation):
            dataclasses.replace(
                original,
                request=dataclasses.replace(
                    original.request,
                    parameter_digest="e" * 64,
                ),
            )

    def test_confirmation_repr_and_trace_hide_identity_digests_and_timestamps(self):
        pending = self.pending()
        rendered = repr(pending) + json.dumps(pending.trace_metadata())
        for secret in (
            pending.sender_key,
            pending.scope_key,
            pending.request_digest,
            pending.pending_id,
            str(pending.issued_at),
            str(pending.expires_at),
        ):
            self.assertNotIn(secret, rendered)


class ActionOutputContractTests(unittest.TestCase):
    def test_public_output_factories_create_shapes_not_canonical_authority(self):
        action_request = request()
        output = ActionOutput.from_safe_text(
            binding=action_request.binding,
            request_digest=action_request.request_digest,
            text="public shape only",
        )
        self.assertFalse(output.is_canonical)

    def test_output_authority_rejects_copy_mutation_and_cross_request_splice(self):
        action_request = request()
        canonical = text_output(action_request, "sealed output")
        self.assertTrue(canonical.is_canonical)

        copied = copy.copy(canonical)
        self.assertIsNot(copied, canonical)
        self.assertFalse(copied.is_canonical)
        with self.assertRaises(ContractViolation):
            owner_action_contracts._open_action_output(
                copied,
                request=action_request,
            )

        cross_request = dataclasses.replace(action_request)
        self.assertIsNot(cross_request, action_request)
        with self.assertRaises(ContractViolation):
            owner_action_contracts._open_action_output(
                canonical,
                request=cross_request,
            )
        with self.assertRaises(ContractViolation):
            issue_receipt(
                cross_request,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                output=canonical,
                result_digest="a" * 64,
            )

        mutated = text_output(action_request, "mutable target")
        object.__setattr__(mutated, "safe_text", "tampered output")
        self.assertFalse(mutated.is_canonical)
        with self.assertRaises(ContractViolation):
            owner_action_contracts._open_action_output(
                mutated,
                request=action_request,
            )

    def test_safe_text_is_bounded_owner_only_and_repr_private(self):
        action_request = request()
        secret = "private owner-only result"
        output = text_output(action_request, secret)
        self.assertEqual(output.kind, ActionOutputKind.SAFE_TEXT)
        self.assertEqual(output.visibility, ActionOutputVisibility.OWNER_ONLY)
        self.assertEqual(output.safe_text, secret)
        self.assertEqual(len(output.output_digest), 64)
        rendered = repr(output) + json.dumps(output.trace_metadata())
        self.assertNotIn(secret, rendered)
        self.assertNotIn(output.output_digest, rendered)

        text_output(
            action_request,
            "x" * owner_action_contracts.MAX_ACTION_OUTPUT_TEXT_CHARS,
        )
        with self.assertRaises(ContractViolation):
            text_output(
                action_request,
                "x" * (owner_action_contracts.MAX_ACTION_OUTPUT_TEXT_CHARS + 1),
            )
        for invalid in ("", "contains\x00nul"):
            with self.subTest(invalid=invalid), self.assertRaises(ContractViolation):
                text_output(action_request, invalid)

    def test_artifact_reference_is_opaque_and_output_shape_is_exclusive(self):
        action_request = request()
        output = ActionOutput.from_artifact_ref(
            binding=action_request.binding,
            request_digest=action_request.request_digest,
            artifact_ref="e" * 64,
        )
        self.assertEqual(output.kind, ActionOutputKind.ARTIFACT_REF)
        self.assertFalse(output.safe_text)
        self.assertEqual(output.artifact_ref, "e" * 64)
        rendered = repr(output) + json.dumps(output.trace_metadata())
        self.assertNotIn(output.artifact_ref, rendered)

        with self.assertRaises(ContractViolation):
            ActionOutput.from_artifact_ref(
                binding=action_request.binding,
                request_digest=action_request.request_digest,
                artifact_ref="not-an-opaque-ref",
            )
        with self.assertRaises(ContractViolation):
            ActionOutput(
                binding=action_request.binding,
                request_digest=action_request.request_digest,
                kind=ActionOutputKind.SAFE_TEXT,
                visibility=ActionOutputVisibility.OWNER_ONLY,
                safe_text="safe",
                artifact_ref="e" * 64,
            )
        with self.assertRaises(ContractViolation):
            ActionOutput(
                binding=action_request.binding,
                request_digest=action_request.request_digest,
                kind=ActionOutputKind.SAFE_TEXT,
                visibility=ActionOutputVisibility.PUBLIC,
                safe_text="safe",
            )

    def test_output_lineage_rejects_cross_binding_or_request_splice(self):
        action_request = request()
        output = text_output(action_request, "safe")
        cross_binding_output = dataclasses.replace(
            output,
            binding=binding(group="group-b"),
        )
        with self.assertRaises(ContractViolation):
            issue_receipt(
                action_request,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                output=cross_binding_output,
                result_digest="a" * 64,
            )
        with self.assertRaises(ContractViolation):
            issue_receipt(
                action_request,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                output=dataclasses.replace(
                    output,
                    request_digest="e" * 64,
                ),
                result_digest="a" * 64,
            )


class ActionReceiptContractTests(unittest.TestCase):
    def test_valid_status_effect_combinations_are_closed(self):
        read_request = request()
        read_output = text_output(read_request, "bounded result")
        read_success = issue_receipt(
            read_request,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            output=read_output,
            result_digest="a" * 64,
        )
        self.assertTrue(read_success.succeeded)

        memory = request(
            operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
            capability=CapabilityClass.MEMORY_WRITE,
            side_effect=ActionSideEffect.STATE_WRITE,
        )
        write_success = issue_receipt(
            memory,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.COMMITTED,
            result_digest="b" * 64,
        )
        self.assertTrue(write_success.succeeded)

        shell = shell_request()
        waiting_pending = pending_for(shell)
        waiting = issue_receipt(
            shell,
            status=ActionReceiptStatus.CONFIRMATION_REQUIRED,
            effect_state=ActionEffectState.NOT_STARTED,
            pending_confirmation=waiting_pending,
            started_at=0.0,
            completed_at=0.0,
        )
        self.assertFalse(waiting.succeeded)
        self.assertFalse(waiting.trace_metadata()["has_source_attestation"])

        consumed_pending = pending_for(shell, consumed=True)
        shell_success = issue_receipt(
            shell,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.COMMITTED,
            completion_binding=consumed_pending.confirmation_binding,
            pending_confirmation=consumed_pending,
            result_digest="c" * 64,
            confirmation_proof_digest=consumed_pending.confirmation_proof_digest,
        )
        self.assertTrue(shell_success.succeeded)

        unknown = issue_receipt(
            memory,
            status=ActionReceiptStatus.EFFECT_UNKNOWN,
            effect_state=ActionEffectState.UNKNOWN,
            result_digest="e" * 64,
        )
        self.assertFalse(unknown.succeeded)

        partial_failure = issue_receipt(
            memory,
            status=ActionReceiptStatus.FAILED,
            effect_state=ActionEffectState.PARTIAL,
            result_digest="f" * 64,
        )
        self.assertFalse(partial_failure.succeeded)

        stale_after_start = issue_receipt(
            memory,
            status=ActionReceiptStatus.STALE,
            effect_state=ActionEffectState.COMMITTED,
            result_digest="0" * 64,
        )
        self.assertEqual(stale_after_start.attempt_count, 1)

        stale_before_start = issue_receipt(
            read_request,
            status=ActionReceiptStatus.STALE,
            effect_state=ActionEffectState.NOT_STARTED,
            started_at=0.0,
            completed_at=0.0,
        )
        self.assertEqual(stale_before_start.attempt_count, 0)

    def test_invalid_status_effect_output_and_confirmation_combinations_fail(self):
        read = request()
        memory = request(
            operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
            capability=CapabilityClass.MEMORY_WRITE,
            side_effect=ActionSideEffect.STATE_WRITE,
        )
        shell = shell_request()
        output = text_output(read, "safe")
        invalid = (
            (read, ActionReceiptStatus.SUCCEEDED, ActionEffectState.COMMITTED, output, "a" * 64, ""),
            (memory, ActionReceiptStatus.SUCCEEDED, ActionEffectState.NOT_COMMITTED, None, "a" * 64, ""),
            (read, ActionReceiptStatus.SUCCEEDED, ActionEffectState.NO_SIDE_EFFECT, None, "a" * 64, ""),
            (read, ActionReceiptStatus.FAILED, ActionEffectState.NO_SIDE_EFFECT, output, "", ""),
            (read, ActionReceiptStatus.DENIED, ActionEffectState.NOT_COMMITTED, None, "", ""),
            (read, ActionReceiptStatus.TIMED_OUT, ActionEffectState.UNKNOWN, None, "a" * 64, ""),
            (read, ActionReceiptStatus.EFFECT_UNKNOWN, ActionEffectState.UNKNOWN, None, "a" * 64, ""),
            (shell, ActionReceiptStatus.CONFIRMATION_REQUIRED, ActionEffectState.NOT_STARTED, None, "a" * 64, ""),
            (shell, ActionReceiptStatus.SUCCEEDED, ActionEffectState.COMMITTED, None, "a" * 64, ""),
            (read, ActionReceiptStatus.SUCCEEDED, ActionEffectState.NOT_COMMITTED, output, "a" * 64, "b" * 64),
        )
        for (
            action_request,
            status,
            effect_state,
            receipt_output,
            result_digest,
            proof_digest,
        ) in invalid:
            with self.subTest(status=status, effect=effect_state), self.assertRaises(
                ContractViolation
            ):
                issue_receipt(
                    action_request,
                    status=status,
                    effect_state=effect_state,
                    output=receipt_output,
                    result_digest=result_digest,
                    confirmation_proof_digest=proof_digest,
                )

    def test_attempt_and_attestation_shape_is_truthful(self):
        action_request = request()
        output = text_output(action_request, "safe")
        with self.assertRaises(ContractViolation):
            issue_receipt(
                action_request,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                output=output,
                result_digest="a" * 64,
                attempt_count=2,
            )
        with self.assertRaises(ContractViolation):
            issue_receipt(
                action_request,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                output=output,
                result_digest="a" * 64,
                source_interface_digest="",
            )
        with self.assertRaises(ContractViolation):
            issue_receipt(
                action_request,
                status=ActionReceiptStatus.DENIED,
                effect_state=ActionEffectState.NOT_STARTED,
                source_interface_digest="9" * 64,
                attempt_count=0,
                started_at=0.0,
                completed_at=0.0,
            )

    def test_second_turn_pre_execution_terminal_can_bind_later_owner_message(self):
        action_request = shell_request()
        pending = pending_for(action_request)
        later_binding = binding(
            message="msg-cancel",
            revision=4,
            epoch=6,
            content_digest="e" * 64,
        )
        for status, reason in (
            (ActionReceiptStatus.CANCELLED, "owner_cancelled"),
            (ActionReceiptStatus.DENIED, "policy_denied"),
            (ActionReceiptStatus.STALE, "confirmation_expired"),
        ):
            with self.subTest(status=status):
                receipt = issue_receipt(
                    action_request,
                    status=status,
                    effect_state=ActionEffectState.NOT_STARTED,
                    completion_binding=later_binding,
                    pending_confirmation=pending,
                    started_at=0.0,
                    completed_at=0.0,
                    reason_codes=(reason,),
                )
                self.assertEqual(receipt.binding, later_binding)
                self.assertFalse(receipt.trace_metadata()["has_confirmation_proof"])

    def test_public_constructor_and_dataclass_replace_cannot_forge_canonical_receipt(self):
        action_request = request()
        with self.assertRaisesRegex(ContractViolation, "issuer_seal_invalid"):
            ActionReceipt(
                binding=action_request.binding,
                action_id=action_request.action_id,
                request_digest=action_request.request_digest,
                adapter_id=action_request.adapter_id,
                adapter_version=action_request.adapter_version,
                capability=action_request.capability,
                operation=action_request.operation,
                side_effect=action_request.side_effect,
                confirmation_policy=action_request.confirmation_policy,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                idempotency_key=action_request.idempotency_key,
                source_interface_digest="9" * 64,
                result_digest="a" * 64,
                output=text_output(action_request, "safe"),
                started_at=30.0,
                completed_at=31.0,
                attempt_count=1,
                reason_codes=("forged",),
                _issuer_seal=object(),
            )

        canonical = issue_receipt(
            action_request,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            output=text_output(action_request, "safe"),
            result_digest="a" * 64,
        )
        self.assertTrue(canonical.is_canonical)
        with self.assertRaisesRegex(ContractViolation, "issuer_seal_invalid"):
            dataclasses.replace(canonical, status=ActionReceiptStatus.FAILED)

    def test_receipt_is_exact_bound_safe_to_repr_and_has_no_grounding_conversion(self):
        action_request = request()
        output = text_output(action_request, "private receipt output")
        receipt = issue_receipt(
            action_request,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            output=output,
            result_digest="a" * 64,
        )
        receipt.assert_current_binding(binding())
        for changed in (
            binding(sender="owner-b"),
            binding(group="group-b"),
            binding(epoch=6),
        ):
            with self.subTest(changed=changed), self.assertRaises(ContractViolation):
                receipt.assert_current_binding(changed)

        rendered = repr(receipt) + json.dumps(receipt.trace_metadata())
        for secret in (
            action_request.binding.current_sender_key,
            action_request.binding.scope_key,
            action_request.action_id,
            action_request.request_digest,
            action_request.idempotency_key,
            receipt.source_interface_digest,
            receipt.result_digest,
            output.safe_text,
        ):
            self.assertNotIn(secret, rendered)
        self.assertFalse(hasattr(receipt, "to_grounding_fact"))
        self.assertFalse(hasattr(receipt, "as_grounding_fact"))
        self.assertNotIn("GroundingFact", owner_action_contracts.__all__)

    def test_canonical_receipt_snapshot_rejects_field_and_nested_output_mutation(self):
        action_request = request()
        output = text_output(action_request, "snapshot protected output")
        receipt = issue_receipt(
            action_request,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            output=output,
            result_digest="a" * 64,
        )
        self.assertTrue(receipt.is_canonical)

        object.__setattr__(receipt, "status", ActionReceiptStatus.DENIED)
        object.__setattr__(receipt, "effect_state", ActionEffectState.NOT_STARTED)
        self.assertFalse(receipt.is_canonical)

        object.__setattr__(receipt, "status", ActionReceiptStatus.SUCCEEDED)
        object.__setattr__(receipt, "effect_state", ActionEffectState.NO_SIDE_EFFECT)
        object.__setattr__(output, "safe_text", "tampered output")
        self.assertFalse(receipt.is_canonical)

    def test_all_public_contract_dataclasses_are_frozen_and_slotted(self):
        receipt_request = request()
        instances = (
            OwnerActionProposal(
                binding=binding(),
                capability=CapabilityClass.ARTIFACT_READ,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
                confidence=0.9,
            ),
            request(),
            PendingConfirmation.from_request(
                shell_request(),
                pending_id="a" * 64,
                issued_at=30.0,
                expires_at=90.0,
            ),
            text_output(request(), "safe"),
            issue_receipt(
                receipt_request,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                output=text_output(receipt_request, "safe"),
                result_digest="a" * 64,
            ),
        )
        for value in instances:
            with self.subTest(value=type(value).__name__):
                self.assertTrue(hasattr(type(value), "__slots__"))
                first_field = dataclasses.fields(value)[0].name
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(value, first_field, getattr(value, first_field))


if __name__ == "__main__":
    unittest.main()
