from __future__ import annotations

import dataclasses
import copy
import gc
import hashlib
import inspect
import json
import re
import threading
import types
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from astrbot_plugin_shio.core.capability_policy import (
    CapabilityClass,
    build_owner_capability_policy,
)
from astrbot_plugin_shio.core.accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnTicket,
)
from astrbot_plugin_shio.core import owner_action_controller as controller_module
from astrbot_plugin_shio.core import owner_action_executor as executor_module
from astrbot_plugin_shio.core.action_planner import (
    PlannedAction,
    PlannedActionAuthority,
    StructuralOutcome,
    reconcile_action,
    structural_action_gate,
)
from astrbot_plugin_shio.core.action_outcome import ActionOutcomeAuthority
from astrbot_plugin_shio.core import owner_action_adapters as adapters
from astrbot_plugin_shio.core import owner_action_runtime_collector as runtime_collector
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionConfirmationPolicy,
    ActionEffectState,
    ActionOutput,
    ActionReceipt,
    ActionReceiptStatus,
    ActionSideEffect,
    ActionKind,
    AddressDecision,
    AddressEvidence,
    AddressKind,
    AttentionDecision,
    AttentionLevel,
    ContractViolation,
    IngressDisposition,
    IngressDecision,
    KnowledgeGapDecision,
    KnowledgeNeed,
    OwnerActionOperation,
    OwnerActionProposal,
    OwnerActionRequest,
    PendingConfirmation,
    ParticipationDecision,
    ParticipationLevel,
    PluginEvidenceStatus,
    SenderKind,
)
from astrbot_plugin_shio.core.contracts import owner_action as owner_action_contracts
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.conversation_event import (
    ConversationRevisionBook,
    build_ingress_event,
)
from astrbot_plugin_shio.core.identity import PrincipalContext, TurnEnvelope
from astrbot_plugin_shio.core.ingress_admission import (
    AdmissionResult,
    IngressAdmissionController,
)
from astrbot_plugin_shio.core.owner_action_controller import (
    ActionClaim,
    ActionClaimStatus,
    ExecutionLease,
    OwnerActionController,
    OwnerActionContinuationInspection,
    OwnerActionContinuationLineage,
    OwnerActionDenial,
    OwnerActionDenialInspection,
    OwnerActionInspection,
    OwnerActionLedgerState,
    OwnerActionReceiptInspection,
    OwnerActionRequestLineageInspection,
    PendingDecision,
    PendingMatch,
    PendingMatchStatus,
    PendingResolutionMatch,
    PendingResolutionRoute,
    PendingResolutionStatus,
    PlannedOwnerActionRoute,
)
from astrbot_plugin_shio.tests.harness.owner_action_durable import (
    DurableFinalizeHarness,
)
from astrbot_plugin_shio.core.owner_action_router import (
    OwnerActionRouteDecision,
    OwnerActionRouter,
    route_owner_action,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def second_turn_message(label: str) -> str:
    safe_label = re.sub(r"[^a-z0-9-]", "-", label.casefold())
    return f"请记住：{safe_label} 的控制器二轮确认测试。"


FILE_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path of the file to read. If relative, will be in workspace root.",
        },
        "offset": {
            "type": "integer",
            "description": "Optional line offset to start reading from. 0-based index.",
            "minimum": 0,
        },
        "limit": {
            "type": "integer",
            "description": "Optional maximum number of lines to read.",
            "minimum": 1,
        },
    },
    "required": ["path"],
}

MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "memory": {
            "type": "string",
            "description": "Concise factual long-term memory to save. Do not copy the full conversation.",
        },
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional short topic tags for this memory, up to 5.",
            "default": [],
        },
        "key_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional key facts supporting the memory, up to 5.",
            "default": [],
        },
        "sentiment": {
            "type": "string",
            "description": "Sentiment of the memory: positive, neutral, or negative.",
            "default": "neutral",
        },
        "importance": {
            "type": "number",
            "description": "Importance from 0.0 to 1.0. Use higher values for durable preferences, commitments, or identity facts.",
            "default": 0.7,
        },
        "reason": {
            "type": "string",
            "description": "Optional short reason why this information should be remembered.",
            "default": "",
        },
    },
    "required": ["memory"],
}


FileReadTool = type(
    "FileReadTool",
    (),
    {"__module__": "astrbot.core.tools.computer_tools.fs"},
)
MemoryMemorizeTool = type(
    "MemoryMemorizeTool",
    (),
    {
        "__module__": (
            "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool"
        )
    },
)
PermissionGuardedTool = type(
    "_PermissionGuardedTool",
    (),
    {"__module__": "astrbot.core.provider.func_tool_manager"},
)


class _ControllerFileReadManager:
    def __init__(self) -> None:
        tool = FileReadTool()
        tool.name = "astrbot_file_read_tool"
        tool.parameters = copy.deepcopy(FILE_READ_SCHEMA)
        tool.active = True
        tool.handler = None
        tool.handler_module_path = type(tool).__module__
        tool.is_background_task = False
        self.tool = tool
        self.func_list = [tool]

    def get_builtin_tool(self, name: str):
        if name != self.tool.name:
            raise KeyError(name)
        return self.tool


def _controller_tool(tool_type, name: str, schema: dict):
    tool = tool_type()
    tool.name = name
    tool.parameters = copy.deepcopy(schema)
    tool.active = True
    tool.handler = None
    tool.handler_module_path = type(tool).__module__
    tool.is_background_task = False
    return tool


class _ControllerToolSet:
    def __init__(self, tools) -> None:
        self.tools = list(tools)

    def get_tool(self, name: str):
        return next((tool for tool in self.tools if tool.name == name), None)


class _ControllerAdminEvent:
    def __init__(self, admin: bool) -> None:
        self._admin = admin

    def is_admin(self) -> bool:
        return self._admin

    def get_sender_id(self) -> str:
        return "redacted"


class _ControllerPermissionContext:
    def __init__(self, admin: bool) -> None:
        self.context = types.SimpleNamespace(event=_ControllerAdminEvent(admin))


class _ControllerMemoryManager:
    def __init__(self) -> None:
        raw = _controller_tool(
            MemoryMemorizeTool,
            "memorize_long_term_memory",
            MEMORY_SCHEMA,
        )
        wrapper = _controller_tool(
            PermissionGuardedTool,
            "memorize_long_term_memory",
            MEMORY_SCHEMA,
        )
        wrapper._wrapped = raw
        wrapper.handler_module_path = type(raw).__module__
        self.raw = raw
        self.wrapper = wrapper
        self.func_list = [raw]

    def get_full_tool_set(self):
        return _ControllerToolSet([self.wrapper])

    def _check_tool_permission(
        self,
        name: str,
        context: _ControllerPermissionContext,
    ):
        if name != self.raw.name:
            return "error"
        return None if context.context.event.is_admin() else "error: permission denied"


def attestation(
    *,
    operation: OwnerActionOperation = OwnerActionOperation.ARTIFACT_READ_EXACT,
) -> adapters.AdapterDescriptor:
    return adapters.OWNER_ACTION_ADAPTER_REGISTRY[operation]


class AdmissionFixture:
    def __init__(self) -> None:
        self.book = ConversationRevisionBook()
        self.admission_controller = IngressAdmissionController(
            self.book,
            max_proofs=64,
        )
        self.accepted_turn_authority = AcceptedTurnAuthority(
            self.admission_controller,
            max_turns=256,
        )
        self.owner_action_router = OwnerActionRouter(
            self.accepted_turn_authority,
            max_routes=256,
        )
        self._epoch = 0
        self._contents: dict[int, str] = {}
        self._tickets: dict[tuple[int, AcceptedTurnConsumer], AcceptedTurnTicket] = {}

    def admitted(
        self,
        *,
        sender: str = "owner-a",
        session: str = "private-a",
        message: str = "message-a",
        content: str | None = None,
        owner: bool = True,
        chat_type: str = "private",
        group_id: str = "",
        reply_to_message_id: str = "",
        verification_source: str = "astrbot_event_sender_id:configured_owner_id",
        relationship_role: str | None = None,
    ) -> AdmissionResult:
        conversation_id = group_id if chat_type == "group" else session
        scope = f"platform:test|bot:shio|{chat_type}:{conversation_id}"
        envelope = TurnEnvelope(
            session_id=session,
            message_id=message,
            scope_key=scope,
            sender_key=f"{scope}|user:{sender}",
            sender_id=sender,
            platform_id="test",
            bot_id="shio",
            chat_type=chat_type,
            group_id=group_id,
            reply_to_message_id=reply_to_message_id,
            reply_to_sender_id="",
            timestamp=100.0 + self._epoch,
            timestamp_source="message",
            source_kind="inbound",
            degradation_reasons=(),
        )
        principal = PrincipalContext(
            sender_key=envelope.sender_key,
            sender_id=envelope.sender_id,
            is_owner=owner,
            relationship_role=(
                relationship_role
                if relationship_role is not None
                else ("owner" if owner else "private_peer")
            ),
            verification_source=verification_source,
        )
        self._epoch += 1
        current_content = (
            f'请读取文件 path="C:\\trusted\\{message}.txt" offset=0 limit=20'
            if content is None
            else content
        )
        ingress = build_ingress_event(
            envelope=envelope,
            principal=principal,
            sender_kind=SenderKind.HUMAN,
            content=current_content,
            revision_candidate=self.book.peek(scope),
            generation_epoch=self._epoch,
        )
        gate = self.admission_controller.issue_gate_observation(
            ingress,
            status=PluginEvidenceStatus.VERIFIED,
            banned=False,
        )
        result = self.admission_controller.admit(
            ingress,
            gate_observation=gate,
        )
        self._contents[id(result)] = current_content
        self.assert_admitted(result)
        dispatch = self.accepted_turn_authority.dispatch(result)
        for consumer in AcceptedTurnConsumer:
            self._tickets[(id(result), consumer)] = (
                self.accepted_turn_authority.ticket_for(dispatch, consumer)
            )
        return result

    def content_for(self, admission: AdmissionResult) -> str:
        return self._contents[id(admission)]

    def ticket_for(
        self,
        admission: AdmissionResult,
        consumer: AcceptedTurnConsumer = AcceptedTurnConsumer.OWNER_ACTION,
    ) -> AcceptedTurnTicket:
        return self._tickets[(id(admission), consumer)]

    def assert_ticket_claimed(
        self,
        admission: AdmissionResult,
        consumer: AcceptedTurnConsumer = AcceptedTurnConsumer.OWNER_ACTION,
    ) -> None:
        ticket = self.ticket_for(admission, consumer)
        context = self.accepted_turn_authority.context_for(
            ticket,
            consumer=consumer,
            binding=ticket.binding,
        )
        with self.assert_contract_violation("accepted_turn_ticket_replayed"):
            self.accepted_turn_authority.claim_ticket(
                ticket,
                consumer=consumer,
                binding=ticket.binding,
                context=context,
            )

    def claim_ticket_directly(
        self,
        admission: AdmissionResult,
        consumer: AcceptedTurnConsumer = AcceptedTurnConsumer.OWNER_ACTION,
    ) -> AcceptedTurnTicket:
        ticket = self.ticket_for(admission, consumer)
        context = self.accepted_turn_authority.context_for(
            ticket,
            consumer=consumer,
            binding=ticket.binding,
        )
        return self.accepted_turn_authority.claim_ticket(
            ticket,
            consumer=consumer,
            binding=ticket.binding,
            context=context,
        )

    @staticmethod
    def assert_contract_violation(pattern: str):
        return unittest.TestCase().assertRaisesRegex(ContractViolation, pattern)

    @staticmethod
    def assert_admitted(result: AdmissionResult) -> None:
        if result.decision.disposition is not IngressDisposition.ACCEPT_HUMAN:
            raise AssertionError("fixture admission was not accepted")


class OwnerActionControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AdmissionFixture()
        self.read_attestation = attestation()
        self.second_turn_attestation = dataclasses.replace(
            attestation(operation=OwnerActionOperation.MEMORY_WRITE_LITERAL),
            confirmation_policy=ActionConfirmationPolicy.SECOND_TURN_REQUIRED,
        )
        self.plan_authority = PlannedActionAuthority(max_plans=256)
        self.controller = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            self.plan_authority,
            max_ledger_entries=32,
        )
        self._resolution_tickets: dict[int, AcceptedTurnTicket] = {}
        self._resolution_plans: dict[int, PlannedAction] = {}
        self._durable_harness: DurableFinalizeHarness | None = None

    def durable_harness(self) -> DurableFinalizeHarness:
        if self._durable_harness is None:
            self._durable_harness = DurableFinalizeHarness(
                self.controller,
                ActionOutcomeAuthority.issue_for_runtime(
                    self.controller,
                    self.plan_authority,
                    max_outcomes=256,
                ),
            )
            self.addCleanup(self._durable_harness.close)
        return self._durable_harness

    def durable_abort(
        self,
        request: OwnerActionRequest,
        *,
        aborted_at: float,
    ) -> controller_module.OwnerActionLifecycleTombstone:
        durable = self.durable_harness()
        _handle, ticket = durable.prepared_abort_ticket(
            request,
            now=aborted_at - 1.0,
        )
        return self.controller.abort_prepared(
            request,
            ticket=ticket,
            durable_authority=durable.authority,
            aborted_at=aborted_at,
        )

    def test_completion_authority_surface_is_lease_plus_sealed_blueprint_only(self):
        self.assertEqual(
            tuple(inspect.signature(OwnerActionController.complete_execution).parameters),
            ("self", "lease", "blueprint"),
        )

    def completion_blueprint(
        self,
        request: OwnerActionRequest,
        lease: ExecutionLease,
        *,
        status: ActionReceiptStatus,
        effect_state: ActionEffectState,
        result_digest: str,
        completed_at: float,
        output: ActionOutput | None = None,
        reason_codes: tuple[str, ...] = (),
    ):
        if output is not None and not output.is_canonical:
            output = owner_action_contracts._issue_test_action_output(
                request,
                output.safe_text,
                truncated=output.truncated,
            )
        return executor_module._issue_test_execution_blueprint(
            self.controller,
            request,
            lease,
            status=status,
            effect_state=effect_state,
            result_digest=result_digest,
            completed_at=completed_at,
            output=output,
            reason_codes=reason_codes,
        )

    def complete_fixture(
        self,
        request: OwnerActionRequest,
        lease: ExecutionLease,
        **completion,
    ):
        blueprint = self.completion_blueprint(request, lease, **completion)
        return self.controller.complete_execution(lease, blueprint)

    def runtime_evidence(
        self,
        admission: AdmissionResult,
        operation: OwnerActionOperation,
        *,
        artifact_path: adapters.ArtifactPathConformance | None = None,
        memory_scope: adapters.MemoryScopeConformance | None = None,
    ) -> adapters.RuntimeConformanceEvidence:
        conversation = admission.conversation_event
        assert conversation is not None
        spec = adapters.OWNER_ACTION_ADAPTER_REGISTRY[operation]
        if operation is OwnerActionOperation.ARTIFACT_READ_EXACT:
            manager = _ControllerFileReadManager()
            scope_probe = None
        elif operation is OwnerActionOperation.MEMORY_WRITE_LITERAL:
            manager = _ControllerMemoryManager()
            scope_probe = lambda: "private_user"
        else:
            raise AssertionError("controller fixture operation has no canonical evidence")
        collector = runtime_collector._build_test_runtime_collector(
            manager,
            operation=operation,
            implementation_version=spec.implementation_version,
            memory_scope_probe=scope_probe,
        )
        candidate = collector.collect(conversation.binding, operation)
        proof = runtime_collector._issue_test_operation_proof(
            artifact_path=artifact_path,
            memory_scope=memory_scope,
        )
        return collector.issue_evidence(candidate, proof)

    def adapter_draft(
        self,
        admission: AdmissionResult,
        proposal: OwnerActionProposal,
        *,
        confirmation_policy: ActionConfirmationPolicy | None = None,
    ) -> adapters.AdapterDraft:
        conversation = admission.conversation_event
        assert conversation is not None
        message = self.fixture.content_for(admission)
        operation = proposal.operation
        if operation is OwnerActionOperation.ARTIFACT_READ_EXACT:
            path_match = re.search(r'path="(?P<path>[^"]+)"', message)
            if path_match is None:
                raise AssertionError("artifact fixture path missing")
            path = path_match.group("path")
            root = "C:\\trusted"
            evidence = self.runtime_evidence(
                admission,
                operation,
                artifact_path=adapters.ArtifactPathConformance(
                    root_digest=digest(root),
                    path_digest=digest(path),
                    preflight_token_digest=digest("artifact-preflight"),
                    binding_revision=conversation.binding.conversation_revision,
                    target_kind=adapters.ArtifactTargetKind.PLAIN_TEXT_FILE,
                    target_size_bytes=128,
                    tree_file_count=1,
                    tree_total_bytes=128,
                    exists=True,
                    realpath_within_root=True,
                    components_lstat_verified=True,
                    symlink_free=True,
                    reparse_free=True,
                    mount_escape_free=True,
                    hardlink_count=1,
                    root_exclusive_trusted_writers=True,
                    replacement_protected=True,
                    plain_text_magic=True,
                    allowed_extensions_only=True,
                ),
            )
            config = adapters.AdapterConfig(
                artifact_read_exact_enabled=True,
                artifact_root=root,
                path_flavor=adapters.ArtifactPathFlavor.WINDOWS,
            )
            return adapters.compile_owner_action_draft(
                proposal,
                message,
                config,
                evidence,
            )
        if operation is OwnerActionOperation.MEMORY_WRITE_LITERAL:
            scope_token = "owner-private-scope-token"
            evidence = self.runtime_evidence(
                admission,
                operation,
                memory_scope=adapters.MemoryScopeConformance(
                    mode=adapters.MemoryScopeMode.PRIVATE_USER,
                    expected_scope_token=scope_token,
                    resolved_scope_token=scope_token,
                    private_chat=True,
                    current_owner_subject=True,
                    identity_degraded=False,
                    global_or_shared_alias_disabled=True,
                    dashboard_permission_admin=True,
                    owner_is_astrbot_admin=True,
                    permission_wrapper_preserved=True,
                ),
            )
            config = adapters.AdapterConfig(memory_write_literal_enabled=True)
            descriptor = adapters.OWNER_ACTION_ADAPTER_REGISTRY[operation]
            selected_policy = confirmation_policy or descriptor.confirmation_policy
            original_policy = descriptor.confirmation_policy
            object.__setattr__(descriptor, "confirmation_policy", selected_policy)
            try:
                # The operation contract already permits either current-turn or
                # second-turn confirmation for private literal memory writes.
                return adapters.compile_owner_action_draft(
                    proposal,
                    message,
                    config,
                    evidence,
                )
            finally:
                object.__setattr__(descriptor, "confirmation_policy", original_policy)
        raise AssertionError(f"unsupported controller fixture operation: {operation}")

    def proposal(
        self,
        admission: AdmissionResult,
        *,
        operation: OwnerActionOperation = OwnerActionOperation.ARTIFACT_READ_EXACT,
    ) -> OwnerActionProposal:
        conversation = admission.conversation_event
        assert conversation is not None
        capability = {
            OwnerActionOperation.ARTIFACT_READ_EXACT: CapabilityClass.ARTIFACT_READ,
            OwnerActionOperation.ARTIFACT_GREP: CapabilityClass.ARTIFACT_READ,
            OwnerActionOperation.MEMORY_WRITE_LITERAL: CapabilityClass.MEMORY_WRITE,
            OwnerActionOperation.SANDBOX_SHELL_ONCE: CapabilityClass.SHELL_EXEC,
        }[operation]
        return OwnerActionProposal(
            binding=conversation.binding,
            capability=capability,
            operation=operation,
            confidence=0.99,
            reason_codes=("current_explicit_action",),
        )

    def route_decision(
        self,
        admission: AdmissionResult,
    ) -> OwnerActionRouteDecision:
        decision = route_owner_action(
            self.fixture.owner_action_router,
            self.fixture.ticket_for(admission),
            current_message=self.fixture.content_for(admission),
        )
        if decision.proposal is None:
            raise AssertionError("controller fixture expected a matched owner route")
        return decision

    def planned_action(
        self,
        admission: AdmissionResult,
        *,
        capability: CapabilityClass = CapabilityClass.ARTIFACT_READ,
        operation: OwnerActionOperation = OwnerActionOperation.ARTIFACT_READ_EXACT,
        kind: ActionKind = ActionKind.EXECUTE_ACTION,
    ) -> PlannedAction:
        conversation = admission.conversation_event
        assert conversation is not None
        ticket = self.fixture.ticket_for(admission)
        binding = ticket.binding
        target = ReplyTarget(
            message_id=binding.current_message_id,
            sender_key=binding.current_sender_key,
            session_id=binding.session_id,
            scope_key=binding.scope_key,
            content_digest=binding.current_content_digest,
            source_kind="current_inbound",
            referenced_message_id="",
            degradation_reasons=(),
        )
        decision = route_owner_action(
            self.fixture.owner_action_router,
            ticket,
            current_message=self.fixture.content_for(admission),
        )
        proposal = decision.proposal
        if (
            kind is ActionKind.EXECUTE_ACTION
            and proposal is not None
            and capability is proposal.capability
            and operation is proposal.operation
        ):
            ingress = IngressDecision(
                binding=binding,
                sender_kind=SenderKind.HUMAN,
                disposition=IngressDisposition.ACCEPT_HUMAN,
            )
            address = AddressDecision(
                binding=binding,
                kind=AddressKind.DIRECT_SELF,
                evidence=(AddressEvidence.STRUCTURED_MENTION,),
                confidence=1.0,
            )
            attention = AttentionDecision(
                binding=binding,
                level=AttentionLevel.FORCE,
                self_relevance=1.0,
                response_value=1.0,
            )
            participation = ParticipationDecision(
                binding=binding,
                level=ParticipationLevel.MUST_REPLY,
            )
            knowledge_gap = KnowledgeGapDecision(
                binding=binding,
                need=KnowledgeNeed.NONE,
                requires_evidence=False,
                requested_capability=None,
                max_tool_calls=0,
            )
            policy = build_owner_capability_policy(
                conversation.principal,
                conversation_mode="direct_reply",
            )
            gate = structural_action_gate(
                ingress=ingress,
                address=address,
                attention=attention,
                participation=participation,
                reply_target=target,
                now=100.0,
            )
            return reconcile_action(
                gate=gate,
                ingress=ingress,
                address=address,
                attention=attention,
                participation=participation,
                reply_target=target,
                knowledge_gap=knowledge_gap,
                capability_policy=policy,
                owner_action_router=self.fixture.owner_action_router,
                owner_action_ticket=ticket,
                owner_action_route=decision,
                planned_action_authority=self.plan_authority,
            )

        is_execution = kind is ActionKind.EXECUTE_ACTION
        return PlannedAction(
            action=ActionDecision(
                binding=binding,
                kind=kind,
                reply_target=(
                    target
                    if kind
                    in {
                        ActionKind.EXECUTE_ACTION,
                        ActionKind.USE_TOOL,
                        ActionKind.REPLY,
                    }
                    else None
                ),
                capability_intent=(
                    capability.value
                    if kind in {ActionKind.EXECUTE_ACTION, ActionKind.USE_TOOL}
                    else ""
                ),
                operation_intent=operation if is_execution else None,
                reason_codes=("noncanonical_controller_negative_fixture",),
            ),
            structural_outcome=StructuralOutcome.CONTINUE,
            model_hint_applied=False,
            planner_reason_codes=("noncanonical_controller_negative_fixture",),
        )

    def route(
        self,
        admission: AdmissionResult,
        *,
        adapter: adapters.AdapterDescriptor | None = None,
    ) -> PlannedOwnerActionRoute:
        selected = adapter or self.read_attestation
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        if proposal.operation is not selected.operation:
            raise AssertionError("controller fixture adapter/route mismatch")
        return self.controller.seal_action_route(
            self.fixture.ticket_for(admission),
            self.planned_action(
                admission,
                capability=proposal.capability,
                operation=proposal.operation,
            ),
            decision,
        )

    def issue(
        self,
        admission: AdmissionResult,
        *,
        adapter: adapters.AdapterDescriptor | None = None,
        idempotency_key: str | None = None,
        issued_at: float = 100.0,
        deadline: float = 200.0,
    ) -> OwnerActionRequest:
        selected = adapter or self.read_attestation
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        if proposal.operation is not selected.operation:
            raise AssertionError("controller fixture adapter/route mismatch")
        route = self.controller.seal_action_route(
            self.fixture.ticket_for(admission),
            self.planned_action(
                admission,
                capability=proposal.capability,
                operation=proposal.operation,
            ),
            decision,
        )
        draft = self.adapter_draft(
            admission,
            proposal,
            confirmation_policy=selected.confirmation_policy,
        )
        return self.controller.issue_request(
            self.fixture.ticket_for(admission),
            route,
            adapter_draft=draft,
            issued_at=issued_at,
            deadline=deadline,
            idempotency_key=idempotency_key or digest("idempotency-a"),
            reason_codes=("owner_current_explicit_request",),
        )

    def resolution_route(
        self,
        pending: PendingConfirmation,
        admission: AdmissionResult,
        *,
        now: float,
        expected: PendingResolutionStatus,
    ) -> PendingResolutionRoute:
        matched = self.controller.route_pending_resolution(
            pending,
            self.fixture.ticket_for(admission),
            current_message=self.fixture.content_for(admission),
            now=now,
        )
        self.assertIsInstance(matched, PendingResolutionMatch)
        self.assertIs(matched.status, expected)
        self.assertIsInstance(matched.route, PendingResolutionRoute)
        assert matched.route is not None
        self._resolution_tickets[id(matched.route)] = self.fixture.ticket_for(admission)
        self._resolution_plans[id(matched.route)] = self.continuation_plan(matched.route)
        return matched.route

    def continuation_plan(self, route: PendingResolutionRoute) -> PlannedAction:
        ticket = self._resolution_tickets[id(route)]
        context = self.fixture.accepted_turn_authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=ticket.binding,
        )
        binding = ticket.binding
        target = ReplyTarget(
            message_id=binding.current_message_id,
            sender_key=binding.current_sender_key,
            session_id=binding.session_id,
            scope_key=binding.scope_key,
            content_digest=binding.current_content_digest,
            source_kind="current_inbound",
            referenced_message_id="",
            degradation_reasons=(),
        )
        ingress = IngressDecision(
            binding=binding,
            sender_kind=SenderKind.HUMAN,
            disposition=IngressDisposition.ACCEPT_HUMAN,
        )
        address = AddressDecision(
            binding=binding,
            kind=AddressKind.DIRECT_SELF,
            evidence=(AddressEvidence.STRUCTURED_MENTION,),
            confidence=1.0,
        )
        attention = AttentionDecision(
            binding=binding,
            level=AttentionLevel.FORCE,
            self_relevance=1.0,
            response_value=1.0,
        )
        participation = ParticipationDecision(
            binding=binding,
            level=ParticipationLevel.MUST_REPLY,
        )
        gap = KnowledgeGapDecision(
            binding=binding,
            need=KnowledgeNeed.NONE,
            requires_evidence=False,
            requested_capability=None,
            max_tool_calls=0,
        )
        policy = build_owner_capability_policy(context.principal)
        gate = structural_action_gate(
            ingress=ingress,
            address=address,
            attention=attention,
            participation=participation,
            reply_target=target,
            now=100.0,
        )
        return reconcile_action(
            gate=gate,
            ingress=ingress,
            address=address,
            attention=attention,
            participation=participation,
            reply_target=target,
            knowledge_gap=gap,
            capability_policy=policy,
            pending_resolution_controller=self.controller,
            pending_resolution_route=route,
            planned_action_authority=self.plan_authority,
        )

    def resolve(
        self,
        route: PendingResolutionRoute,
        *,
        now: float,
    ) -> PendingConfirmation | object:
        return self.controller.resolve_pending(
            route,
            self._resolution_tickets[id(route)],
            current_planned_action=self._resolution_plans[id(route)],
            now=now,
        )

    def test_exact_private_configured_owner_admission_issues_canonical_request(self):
        admission = self.fixture.admitted()
        with mock.patch.object(
            IngressAdmissionController,
            "inspect_admission_proof",
            side_effect=AssertionError("controller must not inspect raw proof"),
        ), mock.patch.object(
            IngressAdmissionController,
            "claim_admission_proof",
            side_effect=AssertionError("controller must not claim raw proof"),
        ):
            request = self.issue(admission)

        self.assertIs(
            self.controller.inspect_request(request).request,
            request,
        )
        self.assertIs(
            self.controller.lookup_idempotent_request(request.idempotency_key),
            request,
        )
        self.assertEqual(
            self.controller.inspect_request(request).state,
            OwnerActionLedgerState.PREPARED,
        )
        self.fixture.assert_ticket_claimed(admission)

        copied = dataclasses.replace(request)
        with self.assertRaisesRegex(ContractViolation, "request_not_canonical"):
            self.controller.inspect_request(copied)
        other = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            self.plan_authority,
        )
        with self.assertRaisesRegex(ContractViolation, "request_not_canonical"):
            other.inspect_request(request)

    def test_request_action_id_is_inherited_from_exact_controller_sealed_plan_route(self):
        admission = self.fixture.admitted(message="planned-lineage")
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        plan = self.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        )
        ticket = self.fixture.ticket_for(admission)
        route = self.controller.seal_action_route(ticket, plan, decision)
        draft = self.adapter_draft(admission, proposal)
        self.assertIsInstance(route, PlannedOwnerActionRoute)
        copied = dataclasses.replace(route)
        with self.assertRaisesRegex(ContractViolation, "action_route_not_canonical"):
            self.controller.issue_request(
                ticket,
                copied,
                adapter_draft=draft,
                issued_at=100.0,
                deadline=200.0,
                idempotency_key=digest("copied-route-idempotency"),
            )
        request = self.controller.issue_request(
            ticket,
            route,
            adapter_draft=draft,
            issued_at=100.0,
            deadline=200.0,
            idempotency_key=digest("planned-lineage-idempotency"),
        )
        self.assertEqual(request.action_id, plan.action_id)
        self.assertNotIn(request.action_id, repr(route))
        self.assertNotIn(request.action_id, json.dumps(route.trace_metadata()))

    def test_controller_seal_requires_exact_unmutated_planner_plan(self):
        admission = self.fixture.admitted(message="plan-authority-copy")
        ticket = self.fixture.ticket_for(admission)
        decision = self.route_decision(admission)
        plan = self.planned_action(admission)
        with self.assertRaisesRegex(ContractViolation, "planned_action_not_canonical"):
            self.controller.seal_action_route(ticket, copy.copy(plan), decision)

        foreign_controller = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            PlannedActionAuthority(),
        )
        with self.assertRaisesRegex(ContractViolation, "planned_action_not_canonical"):
            foreign_controller.seal_action_route(ticket, plan, decision)
        self.assertIsInstance(
            self.controller.seal_action_route(ticket, plan, decision),
            PlannedOwnerActionRoute,
        )

        mutated_admission = self.fixture.admitted(message="plan-authority-mutation")
        mutated_ticket = self.fixture.ticket_for(mutated_admission)
        mutated_decision = self.route_decision(mutated_admission)
        mutated_plan = self.planned_action(mutated_admission)
        object.__setattr__(mutated_plan.action, "operation_intent", OwnerActionOperation.ARTIFACT_GREP)
        with self.assertRaisesRegex(ContractViolation, "planned_action_corrupt"):
            self.controller.seal_action_route(
                mutated_ticket,
                mutated_plan,
                mutated_decision,
            )
        # Failed plan integrity cannot consume either current-turn authority.
        self.assertIs(
            self.fixture.owner_action_router.inspect_route(
                mutated_decision,
                ticket=mutated_ticket,
            ),
            mutated_decision,
        )
    def test_route_denial_claims_after_validation_and_exposes_no_adapter_material(self):
        admission = self.fixture.admitted(message="route-claim-boundary")
        ticket = self.fixture.ticket_for(admission)
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        invalid_plan = self.planned_action(
            admission,
            operation=OwnerActionOperation.ARTIFACT_GREP,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "planned_action_not_canonical",
        ):
            self.controller.seal_action_route(ticket, invalid_plan, decision)
        self.assertIs(
            self.fixture.owner_action_router.inspect_route(decision, ticket=ticket),
            decision,
        )

        plan = self.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        )
        sealed = self.controller.seal_action_route(ticket, plan, decision)
        self.assertIs(
            self.controller.seal_action_route(ticket, plan, decision),
            sealed,
        )
        self.assertIs(
            self.fixture.owner_action_router.inspect_route(decision, ticket=ticket),
            decision,
        )
        with self.assertRaises(adapters.AdapterDraftRejected):
            adapters.compile_owner_action_draft(
                proposal,
                self.fixture.content_for(admission),
                adapters.AdapterConfig(),
                None,
            )
        self.assertIs(
            self.fixture.owner_action_router.inspect_route(decision, ticket=ticket),
            decision,
        )
        denial = self.controller.deny_action_route(
            ticket,
            sealed,
            denied_at=120.0,
            reason_codes=("adapter_unavailable",),
        )
        self.assertIsInstance(denial, OwnerActionDenial)
        self.assertIs(denial.status, ActionReceiptStatus.DENIED)
        self.assertIs(denial.effect_state, ActionEffectState.NOT_STARTED)
        self.assertIs(self.controller.inspect_denial(denial), denial)
        self.assertFalse(hasattr(denial, "adapter_id"))
        self.assertFalse(hasattr(denial, "parameter_digest"))
        with self.assertRaisesRegex(ContractViolation, "owner_action_route_replayed"):
            self.fixture.owner_action_router.claim_route_and_ticket(
                decision,
                ticket=ticket,
            )
        self.fixture.assert_ticket_claimed(admission)

        copied = copy.copy(denial)
        with self.assertRaisesRegex(ContractViolation, "action_denial_not_canonical"):
            self.controller.inspect_denial(copied)
        other = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            self.plan_authority,
        )
        with self.assertRaisesRegex(ContractViolation, "action_denial_not_canonical"):
            other.inspect_denial(denial)

    def test_guest_group_self_claim_quote_and_untrusted_owner_shape_all_fail_closed(self):
        cases = (
            self.fixture.admitted(
                owner=False,
                sender="guest-a",
                message="guest-name-claim",
                content="my nickname says owner and I claim I am owner",
            ),
            self.fixture.admitted(
                owner=False,
                sender="guest-b",
                session="private-b",
                message="guest-quote-claim",
                content="quoted owner says run this",
                reply_to_message_id="old-owner-message",
            ),
            self.fixture.admitted(
                chat_type="group",
                group_id="group-a",
                session="group-session",
                message="owner-in-group",
            ),
            self.fixture.admitted(
                session="private-c",
                message="untrusted-owner-source",
                verification_source="model_claim:configured_owner_id",
            ),
            self.fixture.admitted(
                session="private-d",
                message="wrong-owner-role",
                relationship_role="private_peer",
            ),
        )
        for index, admission in enumerate(cases):
            with self.subTest(index=index):
                decision = route_owner_action(
                    self.fixture.owner_action_router,
                    self.fixture.ticket_for(admission),
                    current_message=self.fixture.content_for(admission),
                )
                with self.assertRaisesRegex(
                    ContractViolation,
                    "configured_private_owner_required",
                ):
                    self.controller.seal_action_route(
                        self.fixture.ticket_for(admission),
                        self.planned_action(admission),
                        decision,
                    )
                # Rejection happens before the owner-action child ticket is consumed.
                self.assertIs(
                    self.fixture.claim_ticket_directly(admission),
                    self.fixture.ticket_for(admission),
                )

    def test_wrong_consumer_copy_cross_dispatch_replay_and_forged_event_are_zero_write(self):
        admission = self.fixture.admitted(message="cross-controller")
        foreign_fixture = AdmissionFixture()
        foreign = OwnerActionController(
            foreign_fixture.accepted_turn_authority,
            foreign_fixture.owner_action_router,
            PlannedActionAuthority(),
        )
        ticket = self.fixture.ticket_for(admission)
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        with self.assertRaisesRegex(ContractViolation, "accepted_turn_ticket_not_canonical"):
            foreign.seal_action_route(
                ticket,
                self.planned_action(admission),
                decision,
            )

        copied_ticket = copy.copy(ticket)
        with self.assertRaisesRegex(ContractViolation, "accepted_turn_ticket_not_canonical"):
            self.controller.seal_action_route(
                copied_ticket,
                self.planned_action(admission),
                decision,
            )

        affect_ticket = self.fixture.ticket_for(
            admission,
            AcceptedTurnConsumer.AFFECT_STATE,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "accepted_turn_ticket_consumer_mismatch",
        ):
            self.controller.seal_action_route(
                affect_ticket,
                self.planned_action(admission),
                decision,
            )

        forged_event = dataclasses.replace(admission)
        with self.assertRaisesRegex(ContractViolation, "accepted_turn_ticket_required"):
            self.controller.seal_action_route(
                forged_event,
                self.planned_action(admission),
                decision,
            )
        self.assertEqual(self.controller.ledger_size, 0)

        other_admission = self.fixture.admitted(message="cross-dispatch")
        other_ticket = self.fixture.ticket_for(other_admission)
        route = self.controller.seal_action_route(
            ticket,
            self.planned_action(admission),
            decision,
        )
        with self.assertRaisesRegex(ContractViolation, "action_route_ticket_mismatch"):
            self.controller.issue_request(
                other_ticket,
                route,
                adapter_draft=self.adapter_draft(
                    admission,
                    proposal,
                ),
                issued_at=100.0,
                deadline=200.0,
                idempotency_key=digest("cross-dispatch"),
            )
        self.assertEqual(self.controller.ledger_size, 0)
        self.assertIs(
            self.fixture.claim_ticket_directly(other_admission),
            other_ticket,
        )

        consumed_admission = self.fixture.admitted(message="consumed-ticket")
        consumed_decision = self.route_decision(consumed_admission)
        consumed_proposal = consumed_decision.proposal
        assert consumed_proposal is not None
        self.fixture.claim_ticket_directly(consumed_admission)
        route = self.controller.seal_action_route(
            self.fixture.ticket_for(consumed_admission),
            self.planned_action(consumed_admission),
            consumed_decision,
        )
        with self.assertRaisesRegex(ContractViolation, "accepted_turn_ticket_replayed"):
            self.controller.issue_request(
                self.fixture.ticket_for(consumed_admission),
                route,
                adapter_draft=self.adapter_draft(
                    consumed_admission,
                    consumed_proposal,
                ),
                issued_at=100.0,
                deadline=200.0,
                idempotency_key=digest("consumed"),
            )
        self.assertEqual(self.controller.ledger_size, 0)

    def test_controller_boundary_has_no_raw_admission_proof_surface(self):
        source = inspect.getsource(controller_module)
        for forbidden in (
            "AdmissionResult",
            "IngressAdmissionController",
            "inspect_admission_proof",
            "claim_admission_proof",
        ):
            self.assertNotIn(forbidden, source)

        for method in (
            OwnerActionController.seal_action_route,
            OwnerActionController.issue_request,
            OwnerActionController.match_pending,
            OwnerActionController.route_pending_resolution,
            OwnerActionController.resolve_pending,
        ):
            parameters = inspect.signature(method).parameters
            self.assertNotIn("admission", parameters)
            self.assertIn("ticket", parameters)
        for method in (
            OwnerActionController.issue_request,
            OwnerActionController.deny_action_route,
        ):
            method_source = inspect.getsource(method)
            self.assertIn("claim_route_and_ticket", method_source)
            self.assertNotIn("_accepted_turn_authority.claim_ticket", method_source)
        self.assertFalse(hasattr(self.fixture.owner_action_router, "claim_route"))

    def test_public_controller_shapes_reject_bare_string_enums(self):
        admission = self.fixture.admitted(message="shape-enum")
        binding = admission.conversation_event.binding
        request = self.issue(admission)
        hex_a = "a" * 64
        hex_b = "b" * 64

        cases = (
            lambda: PlannedOwnerActionRoute(
                binding=binding,
                action_id=hex_a,
                route_digest=hex_b,
                capability=CapabilityClass.ARTIFACT_READ.value,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            ),
            lambda: PlannedOwnerActionRoute(
                binding=binding,
                action_id=hex_a,
                route_digest=hex_b,
                capability=CapabilityClass.ARTIFACT_READ,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT.value,
            ),
            lambda: PendingMatch(status=PendingMatchStatus.NONE.value),
            lambda: PendingResolutionRoute(
                binding=binding,
                route_digest=hex_a,
                request_digest=hex_b,
                parameter_digest=hex_a,
                decision=PendingDecision.CONFIRM.value,
                capability=CapabilityClass.ARTIFACT_READ,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            ),
            lambda: PendingResolutionRoute(
                binding=binding,
                route_digest=hex_a,
                request_digest=hex_b,
                parameter_digest=hex_a,
                decision=PendingDecision.CONFIRM,
                capability=CapabilityClass.ARTIFACT_READ.value,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            ),
            lambda: PendingResolutionRoute(
                binding=binding,
                route_digest=hex_a,
                request_digest=hex_b,
                parameter_digest=hex_a,
                decision=PendingDecision.CONFIRM,
                capability=CapabilityClass.ARTIFACT_READ,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT.value,
            ),
            lambda: PendingResolutionMatch(
                status=PendingResolutionStatus.UNCLEAR.value,
            ),
            lambda: ExecutionLease(
                binding=binding,
                request_digest=hex_a,
                lease_digest=hex_b,
                capability=CapabilityClass.ARTIFACT_READ.value,
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
                issued_at=1.0,
                deadline=2.0,
            ),
            lambda: OwnerActionInspection(
                request=request,
                state=OwnerActionLedgerState.PREPARED.value,
                has_pending=False,
                has_lease=False,
                has_receipt=False,
            ),
            lambda: ActionClaim(status=ActionClaimStatus.CLAIMED.value),
        )
        for index, constructor in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ContractViolation):
                    constructor()

    def test_public_proposal_route_copy_action_mismatch_and_forged_draft_are_rejected(self):
        admission = self.fixture.admitted(message="shape-mismatch")
        conversation = admission.conversation_event
        assert conversation is not None
        ticket = self.fixture.ticket_for(admission)
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        wrong_binding = dataclasses.replace(
            conversation.binding,
            current_content_digest="f" * 64,
        )
        wrong_proposal = OwnerActionProposal(
            binding=wrong_binding,
            capability=CapabilityClass.ARTIFACT_READ,
            operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
            confidence=0.99,
        )
        with self.assertRaisesRegex(ContractViolation, "owner_action_route_required"):
            self.controller.seal_action_route(
                ticket,
                self.planned_action(admission),
                wrong_proposal,
            )

        copied_decision = copy.copy(decision)
        with self.assertRaisesRegex(
            ContractViolation,
            "owner_action_route_not_canonical",
        ):
            self.controller.seal_action_route(
                ticket,
                self.planned_action(admission),
                copied_decision,
            )

        other_router = OwnerActionRouter(
            self.fixture.accepted_turn_authority,
            max_routes=8,
        )
        foreign_decision = route_owner_action(
            other_router,
            ticket,
            current_message=self.fixture.content_for(admission),
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "owner_action_route_not_canonical",
        ):
            self.controller.seal_action_route(
                ticket,
                self.planned_action(admission),
                foreign_decision,
            )

        wrong_action = self.planned_action(
            admission,
            capability=CapabilityClass.SHELL_EXEC,
            operation=OwnerActionOperation.SANDBOX_SHELL_ONCE,
        )
        with self.assertRaisesRegex(ContractViolation, "planned_action_not_canonical"):
            self.controller.seal_action_route(
                ticket,
                wrong_action,
                decision,
            )

        wrong_kind = self.planned_action(admission, kind=ActionKind.USE_TOOL)
        with self.assertRaisesRegex(ContractViolation, "planned_action_not_canonical"):
            self.controller.seal_action_route(
                ticket,
                wrong_kind,
                decision,
            )

        wrong_operation = self.planned_action(
            admission,
            operation=OwnerActionOperation.ARTIFACT_GREP,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "planned_action_not_canonical",
        ):
            self.controller.seal_action_route(
                ticket,
                wrong_operation,
                decision,
            )

        route = self.controller.seal_action_route(
            ticket,
            self.planned_action(admission),
            decision,
        )
        valid_draft = self.adapter_draft(admission, proposal)
        copied_draft = object.__new__(adapters.AdapterDraft)
        for draft_field in dataclasses.fields(valid_draft):
            object.__setattr__(
                copied_draft,
                draft_field.name,
                getattr(valid_draft, draft_field.name),
            )
        self.assertFalse(copied_draft.is_canonical)
        with self.assertRaisesRegex(ContractViolation, "adapter_draft_not_canonical"):
            self.controller.issue_request(
                self.fixture.ticket_for(admission),
                route,
                adapter_draft=copied_draft,
                issued_at=100.0,
                deadline=200.0,
                idempotency_key=digest("copied-draft-idem"),
            )

        class CrossModuleDraft:
            pass

        cross_module_draft = CrossModuleDraft()
        for draft_field in dataclasses.fields(valid_draft):
            setattr(
                cross_module_draft,
                draft_field.name,
                getattr(valid_draft, draft_field.name),
            )
        self.assertNotIsInstance(cross_module_draft, adapters.AdapterDraft)
        with self.assertRaisesRegex(ContractViolation, "adapter_draft_not_canonical"):
            self.controller.issue_request(
                self.fixture.ticket_for(admission),
                route,
                adapter_draft=cross_module_draft,
                issued_at=100.0,
                deadline=200.0,
                idempotency_key=digest("cross-module-draft-idem"),
            )

        request = self.controller.issue_request(
            self.fixture.ticket_for(admission),
            route,
            adapter_draft=valid_draft,
            issued_at=100.0,
            deadline=200.0,
            idempotency_key=digest("valid-after-forgeries-idem"),
        )
        self.assertIs(self.controller.inspect_request(request).request, request)

    def test_parameter_record_is_typed_opaque_and_never_leaks(self):
        admission = self.fixture.admitted(message="redaction")
        secret_path = "C:\\trusted\\redaction.txt"
        request = self.issue(admission)
        snapshot = self.controller.inspect_request(request)

        for public in (request, snapshot, self.controller):
            rendered = repr(public)
            trace = getattr(public, "trace_metadata", lambda: {})()
            rendered += json.dumps(trace, ensure_ascii=False)
            for forbidden in (
                secret_path,
                admission.ingress_event.envelope.sender_id,
                admission.ingress_event.envelope.scope_key,
                admission.ingress_event.envelope.session_id,
                admission.ingress_event.content_digest,
            ):
                self.assertNotIn(forbidden, rendered)
        self.assertFalse(hasattr(request, "parameter_record"))
        self.assertFalse(hasattr(request, "adapter_draft"))
        self.assertFalse(hasattr(snapshot, "parameter_record"))
        public_parameters = inspect.signature(
            OwnerActionController.issue_request
        ).parameters
        self.assertNotIn("parameter_record", public_parameters)
        self.assertNotIn("parameter_digest", public_parameters)
        self.assertNotIn("adapter_attestation", public_parameters)

    def test_canonical_draft_must_match_binding_and_sealed_proposal_operation(self):
        admission = self.fixture.admitted(message="draft-binding-origin")
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        route = self.controller.seal_action_route(
            self.fixture.ticket_for(admission),
            self.planned_action(admission),
            decision,
        )
        other_admission = self.fixture.admitted(message="draft-binding-other")
        other_decision = self.route_decision(other_admission)
        other_proposal = other_decision.proposal
        assert other_proposal is not None
        other_draft = self.adapter_draft(other_admission, other_proposal)
        with self.assertRaisesRegex(
            ContractViolation,
            "adapter_draft_binding_mismatch",
        ):
            self.controller.issue_request(
                self.fixture.ticket_for(admission),
                route,
                adapter_draft=other_draft,
                issued_at=100.0,
                deadline=200.0,
                idempotency_key=digest("wrong-draft-binding-idem"),
            )
        self.assertIs(
            self.fixture.claim_ticket_directly(admission),
            self.fixture.ticket_for(admission),
        )

    def test_atomic_execution_claim_is_single_use_under_concurrency(self):
        admission = self.fixture.admitted(message="concurrent-claim")
        request = self.issue(admission)
        binding = request.binding
        barrier = threading.Barrier(12)

        def claim():
            barrier.wait()
            return self.controller.claim_execution(
                request,
                current_binding=binding,
                now=120.0,
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(lambda _: claim(), range(12)))

        claimed = [outcome for outcome in outcomes if outcome.status is ActionClaimStatus.CLAIMED]
        in_progress = [
            outcome
            for outcome in outcomes
            if outcome.status is ActionClaimStatus.IN_PROGRESS
        ]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(len(in_progress), 11)
        self.assertIsInstance(claimed[0].lease, ExecutionLease)
        self.assertTrue(all(outcome.lease is None for outcome in in_progress))
        self.assertEqual(
            self.controller.inspect_request(request).state,
            OwnerActionLedgerState.IN_PROGRESS,
        )

    def test_wrong_binding_cross_epoch_and_stale_before_start_never_issue_a_lease(self):
        admission = self.fixture.admitted(message="wrong-epoch")
        request = self.issue(admission)
        wrong_epoch = dataclasses.replace(
            request.binding,
            generation_epoch=request.binding.generation_epoch + 1,
        )
        with self.assertRaisesRegex(ContractViolation, "execution_binding_mismatch"):
            self.controller.claim_execution(
                request,
                current_binding=wrong_epoch,
                now=120.0,
            )

        stale = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
            stale=True,
        )
        self.assertIs(stale.status, ActionClaimStatus.TERMINAL)
        self.assertIsNone(stale.lease)
        assert stale.receipt is not None
        self.assertIs(stale.receipt.status, ActionReceiptStatus.STALE)
        self.assertIs(stale.receipt.effect_state, ActionEffectState.NOT_STARTED)
        self.assertEqual(stale.receipt.attempt_count, 0)
        replay = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=121.0,
        )
        self.assertIs(replay.status, ActionClaimStatus.TERMINAL)
        self.assertIs(replay.receipt, stale.receipt)

    def test_second_turn_confirmation_is_same_sender_scope_session_ttl_and_single_use(self):
        origin = self.fixture.admitted(
            message="memory-origin",
            content=second_turn_message("memory-origin"),
        )
        request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("memory-idempotency"),
            issued_at=100.0,
            deadline=190.0,
        )
        pending = self.controller.start_confirmation(request, now=110.0, ttl=60.0)
        self.assertIsInstance(pending, PendingConfirmation)
        confirmation_receipt = self.controller.canonical_receipt_for(request)
        assert confirmation_receipt is not None
        self.assertIs(
            confirmation_receipt.status,
            ActionReceiptStatus.CONFIRMATION_REQUIRED,
        )

        confirmed_admission = self.fixture.admitted(
            message="memory-confirm",
            content="确认保存",
        )
        resolution_route = self.resolution_route(
            pending,
            confirmed_admission,
            now=130.0,
            expected=PendingResolutionStatus.CONFIRM,
        )
        current_plan = self._resolution_plans[id(resolution_route)]
        self.assertIs(current_plan.kind, ActionKind.EXECUTE_ACTION)
        self.assertIs(current_plan.binding, resolution_route.binding)
        self.assertIs(
            current_plan.action.operation_intent,
            OwnerActionOperation.MEMORY_WRITE_LITERAL,
        )
        self.assertNotEqual(current_plan.action_id, request.action_id)
        confirmed = self.resolve(resolution_route, now=130.0)
        self.assertTrue(confirmed.consumed)
        lineage = self.controller.continuation_lineage_for(resolution_route)
        self.assertIsInstance(lineage, OwnerActionContinuationLineage)
        inspection = self.controller.inspect_continuation_lineage(lineage)
        self.assertIsInstance(inspection, OwnerActionContinuationInspection)
        self.assertIs(inspection.current_planned_action, current_plan)
        self.assertIs(inspection.origin_request, request)
        self.assertIs(inspection.resolution_route, resolution_route)
        self.assertIsNone(inspection.receipt)
        with self.assertRaisesRegex(
            ContractViolation,
            "continuation_lineage_not_canonical",
        ):
            self.controller.inspect_continuation_lineage(copy.copy(lineage))
        other_controller = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            self.plan_authority,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "continuation_lineage_not_canonical",
        ):
            other_controller.inspect_continuation_lineage(lineage)
        self.assertEqual(
            self.controller.inspect_request(request).state,
            OwnerActionLedgerState.READY,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "pending_resolution_route_replayed",
        ):
            self.resolve(resolution_route, now=131.0)

        claim = self.controller.claim_execution(
            request,
            current_binding=confirmed.confirmation_binding,
            pending_confirmation=confirmed,
            now=131.0,
        )
        self.assertIs(claim.status, ActionClaimStatus.CLAIMED)
        self.assertIsNotNone(claim.lease)
        assert claim.lease is not None
        completed = self.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.COMMITTED,
            result_digest=digest("confirmation-lineage-completed"),
            completed_at=140.0,
        )
        completed_inspection = self.controller.inspect_continuation_lineage(lineage)
        self.assertIs(completed_inspection.receipt, completed)
        self.assertEqual(completed.request_digest, request.request_digest)
        self.assertEqual(completed.origin_binding, request.binding)
        self.assertEqual(completed.binding, current_plan.binding)
        self.assertEqual(completed.action_id, request.action_id)
        rendered_lineage = repr(lineage) + json.dumps(
            completed_inspection.trace_metadata(),
            ensure_ascii=False,
        )
        for hidden in (
            request.action_id,
            request.request_digest,
            current_plan.action_id,
            current_plan.binding.current_message_id,
            current_plan.binding.current_sender_key,
        ):
            self.assertNotIn(hidden, rendered_lineage)
        object.__setattr__(lineage, "decision", PendingDecision.CANCEL)
        with self.assertRaisesRegex(
            ContractViolation,
            "continuation_lineage_corrupt",
        ):
            self.controller.inspect_continuation_lineage(lineage)

    def test_pending_resolution_parser_is_closed_and_unclear_keeps_proof(self):
        cases = (
            ("other", "好啊，那就这样吧", ""),
            ("negated", "不确认保存", ""),
            ("question", "确认保存吗", ""),
            ("ambiguous", "确认保存 取消保存", ""),
            ("extra", "确认保存，不过先解释一下", ""),
            ("quoted", "确认保存", "quoted-old-message"),
        )
        for index, (label, content, reply_to) in enumerate(cases):
            with self.subTest(label=label):
                origin = self.fixture.admitted(
                    session=f"closed-{label}",
                    message=f"closed-origin-{label}",
                    content=second_turn_message(f"closed-origin-{label}"),
                )
                request = self.issue(
                    origin,
                    adapter=self.second_turn_attestation,
                    idempotency_key=digest(f"closed-idem-{label}"),
                    issued_at=100.0,
                    deadline=240.0,
                )
                pending = self.controller.start_confirmation(
                    request,
                    now=110.0,
                    ttl=100.0,
                )
                followup = self.fixture.admitted(
                    session=f"closed-{label}",
                    message=f"closed-followup-{label}",
                    content=content,
                    reply_to_message_id=reply_to,
                )
                matched = self.controller.route_pending_resolution(
                    pending,
                    self.fixture.ticket_for(followup),
                    current_message=self.fixture.content_for(followup),
                    now=130.0 + index,
                )
                self.assertIs(matched.status, PendingResolutionStatus.UNCLEAR)
                self.assertIsNone(matched.route)
                rendered = repr(matched) + json.dumps(
                    matched.trace_metadata(),
                    ensure_ascii=False,
                )
                self.assertNotIn(content, rendered)
                self.assertIs(
                    self.fixture.claim_ticket_directly(followup),
                    self.fixture.ticket_for(followup),
                )
                self.assertIs(
                    self.controller.inspect_request(request).state,
                    OwnerActionLedgerState.CONFIRMATION_REQUIRED,
                )

    def test_pending_resolution_route_copy_cross_controller_and_changed_body_fail(self):
        origin = self.fixture.admitted(
            session="resolution-authority",
            message="resolution-authority-origin",
            content=second_turn_message("resolution-authority-origin"),
        )
        request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("resolution-authority-idem"),
            issued_at=100.0,
            deadline=240.0,
        )
        pending = self.controller.start_confirmation(request, now=110.0, ttl=100.0)
        followup = self.fixture.admitted(
            session="resolution-authority",
            message="resolution-authority-followup",
            content="确认保存",
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "current_message_binding_mismatch",
        ):
            self.controller.route_pending_resolution(
                pending,
                self.fixture.ticket_for(followup),
                current_message="取消保存",
                now=130.0,
            )
        route = self.resolution_route(
            pending,
            followup,
            now=130.0,
            expected=PendingResolutionStatus.CONFIRM,
        )
        current_plan = self._resolution_plans[id(route)]
        with self.assertRaisesRegex(
            ContractViolation,
            "planned_action_not_canonical",
        ):
            self.controller.resolve_pending(
                route,
                self.fixture.ticket_for(followup),
                current_planned_action=copy.copy(current_plan),
                now=130.0,
            )

        copied = object.__new__(PendingResolutionRoute)
        for route_field in dataclasses.fields(route):
            object.__setattr__(
                copied,
                route_field.name,
                getattr(route, route_field.name),
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "pending_resolution_route_not_canonical",
        ):
            self.controller.resolve_pending(
                copied,
                self.fixture.ticket_for(followup),
                current_planned_action=self._resolution_plans[id(route)],
                now=130.0,
            )

        other = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            self.plan_authority,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "pending_resolution_route_not_canonical",
        ):
            other.resolve_pending(
                route,
                self.fixture.ticket_for(followup),
                current_planned_action=self._resolution_plans[id(route)],
                now=130.0,
            )

        wrong_followup = self.fixture.admitted(
            session="resolution-authority",
            message="resolution-authority-wrong-turn",
            content="确认保存",
        )
        with self.assertRaises(ContractViolation):
            self.controller.resolve_pending(
                route,
                self.fixture.ticket_for(wrong_followup),
                current_planned_action=self._resolution_plans[id(route)],
                now=130.0,
            )
        self.assertIs(
            self.fixture.claim_ticket_directly(wrong_followup),
            self.fixture.ticket_for(wrong_followup),
        )
        self.assertIs(
            self.controller.inspect_request(request).state,
            OwnerActionLedgerState.CONFIRMATION_REQUIRED,
        )

        confirmed = self.resolve(route, now=130.0)
        self.assertTrue(confirmed.consumed)
        public_parameters = inspect.signature(
            OwnerActionController.resolve_pending
        ).parameters
        self.assertNotIn("admission", public_parameters)
        self.assertNotIn("decision", public_parameters)
        self.assertNotIn("request_digest", public_parameters)
        self.assertNotIn("parameter_digest", public_parameters)

    def test_pending_resolution_requires_exact_current_plan_binding_and_operation(self):
        def prepared(label: str):
            origin = self.fixture.admitted(
                session=f"current-plan-{label}",
                message=f"current-plan-origin-{label}",
                content=second_turn_message(f"current-plan-origin-{label}"),
            )
            request = self.issue(
                origin,
                adapter=self.second_turn_attestation,
                idempotency_key=digest(f"current-plan-idem-{label}"),
                issued_at=100.0,
                deadline=240.0,
            )
            pending = self.controller.start_confirmation(
                request,
                now=110.0,
                ttl=100.0,
            )
            followup = self.fixture.admitted(
                session=f"current-plan-{label}",
                message=f"current-plan-followup-{label}",
                content="确认保存",
            )
            route = self.resolution_route(
                pending,
                followup,
                now=130.0,
                expected=PendingResolutionStatus.CONFIRM,
            )
            return request, route, followup

        request_a, route_a, followup_a = prepared("a")
        _request_b, route_b, _followup_b = prepared("b")
        with self.assertRaisesRegex(
            ContractViolation,
            "pending_resolution_plan_mismatch",
        ):
            self.controller.resolve_pending(
                route_a,
                self.fixture.ticket_for(followup_a),
                current_planned_action=self._resolution_plans[id(route_b)],
                now=130.0,
            )
        confirmed_a = self.resolve(route_a, now=130.0)
        self.assertTrue(confirmed_a.consumed)
        lineage_a = self.controller.continuation_lineage_for(route_a)
        self.assertIs(
            self.controller.inspect_continuation_lineage(lineage_a).origin_request,
            request_a,
        )

        _request_c, route_c, followup_c = prepared("c")
        corrupted_plan = self._resolution_plans[id(route_c)]
        object.__setattr__(
            corrupted_plan.action,
            "operation_intent",
            OwnerActionOperation.ARTIFACT_GREP,
        )
        with self.assertRaisesRegex(ContractViolation, "planned_action_corrupt"):
            self.controller.resolve_pending(
                route_c,
                self.fixture.ticket_for(followup_c),
                current_planned_action=corrupted_plan,
                now=130.0,
            )
        self.assertIs(
            self.fixture.claim_ticket_directly(followup_c),
            self.fixture.ticket_for(followup_c),
        )

    def test_a_to_b_to_a_does_not_steal_or_destroy_pending(self):
        origin = self.fixture.admitted(
            message="a-origin",
            content=second_turn_message("a-origin"),
        )
        request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("aba-idempotency"),
            issued_at=100.0,
            deadline=220.0,
        )
        pending = self.controller.start_confirmation(request, now=105.0, ttl=100.0)

        user_b = self.fixture.admitted(
            sender="owner-b",
            message="b-confirm",
            content="确认保存",
        )
        with self.assertRaisesRegex(ContractViolation, "confirmation_sender_mismatch"):
            self.controller.route_pending_resolution(
                pending,
                self.fixture.ticket_for(user_b),
                current_message=self.fixture.content_for(user_b),
                now=120.0,
            )
        self.assertEqual(
            self.controller.inspect_request(request).state,
            OwnerActionLedgerState.CONFIRMATION_REQUIRED,
        )
        self.assertIs(
            self.fixture.claim_ticket_directly(user_b),
            self.fixture.ticket_for(user_b),
        )

        user_a = self.fixture.admitted(message="a-confirm", content="确认保存")
        resolution_route = self.resolution_route(
            pending,
            user_a,
            now=130.0,
            expected=PendingResolutionStatus.CONFIRM,
        )
        confirmed = self.resolve(resolution_route, now=130.0)
        self.assertTrue(confirmed.consumed)

    def test_match_pending_uses_only_current_canonical_sender_scope_session(self):
        empty_admission = self.fixture.admitted(
            session="match-empty",
            message="match-empty-current",
        )
        empty = self.controller.match_pending(
            self.fixture.ticket_for(empty_admission),
            now=120.0,
        )
        self.assertIs(empty.status, PendingMatchStatus.NONE)
        self.assertIsNone(empty.pending)
        self.assertIs(
            self.fixture.claim_ticket_directly(empty_admission),
            self.fixture.ticket_for(empty_admission),
        )

        origin = self.fixture.admitted(
            session="match-aba",
            message="match-origin",
            content=second_turn_message("match-origin"),
        )
        request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("match-idempotency"),
            issued_at=100.0,
            deadline=220.0,
        )
        pending = self.controller.start_confirmation(request, now=110.0, ttl=80.0)

        user_b = self.fixture.admitted(
            sender="owner-b",
            session="match-aba",
            message="match-user-b",
        )
        b_match = self.controller.match_pending(
            self.fixture.ticket_for(user_b),
            now=125.0,
        )
        self.assertIs(b_match.status, PendingMatchStatus.NONE)
        self.assertIsNone(b_match.pending)

        user_a = self.fixture.admitted(
            session="match-aba",
            message="match-user-a",
            content="确认保存",
        )
        a_match = self.controller.match_pending(
            self.fixture.ticket_for(user_a),
            now=130.0,
        )
        self.assertIs(a_match.status, PendingMatchStatus.UNIQUE)
        self.assertIs(a_match.pending, pending)
        resolution_route = self.resolution_route(
            a_match.pending,
            user_a,
            now=130.0,
            expected=PendingResolutionStatus.CONFIRM,
        )
        confirmed = self.resolve(resolution_route, now=130.0)
        self.assertTrue(confirmed.consumed)

        group_owner = self.fixture.admitted(
            session="match-group-session",
            chat_type="group",
            group_id="match-group",
            message="match-group-owner",
        )
        with self.assertRaisesRegex(ContractViolation, "configured_private_owner_required"):
            self.controller.match_pending(
                self.fixture.ticket_for(group_owner),
                now=135.0,
            )

    def test_match_pending_multiple_active_actions_is_ambiguous_and_selects_none(self):
        pendings: list[PendingConfirmation] = []
        for index in range(2):
            origin = self.fixture.admitted(
                session="match-ambiguous",
                message=f"ambiguous-origin-{index}",
                content=second_turn_message(f"ambiguous-origin-{index}"),
            )
            request = self.issue(
                origin,
                adapter=self.second_turn_attestation,
                idempotency_key=digest(f"ambiguous-idempotency-{index}"),
                issued_at=100.0,
                deadline=240.0,
            )
            pendings.append(
                self.controller.start_confirmation(
                    request,
                    now=110.0 + index,
                    ttl=100.0,
                )
            )

        current = self.fixture.admitted(
            session="match-ambiguous",
            message="ambiguous-current",
            content="确认保存",
        )
        matched = self.controller.match_pending(
            self.fixture.ticket_for(current),
            now=130.0,
        )
        self.assertIs(matched.status, PendingMatchStatus.AMBIGUOUS)
        self.assertIsNone(matched.pending)
        self.assertEqual(matched.candidate_count, 2)
        self.assertNotIn("pending_id", repr(matched))
        self.assertNotIn("pending_id", json.dumps(matched.trace_metadata()))
        with self.assertRaisesRegex(
            ContractViolation,
            "pending_resolution_not_unique",
        ):
            self.controller.route_pending_resolution(
                pendings[0],
                self.fixture.ticket_for(current),
                current_message=self.fixture.content_for(current),
                now=130.0,
            )
        self.assertIs(
            self.fixture.claim_ticket_directly(current),
            self.fixture.ticket_for(current),
        )

    def test_other_scope_unclear_deny_cancel_and_expiry_are_fail_closed(self):
        def pending_case(label: str):
            origin = self.fixture.admitted(
                session=f"origin-{label}",
                message=f"origin-{label}",
                content=second_turn_message(f"origin-{label}"),
            )
            request = self.issue(
                origin,
                adapter=self.second_turn_attestation,
                idempotency_key=digest(f"idem-{label}"),
                issued_at=100.0,
                deadline=210.0,
            )
            return request, self.controller.start_confirmation(
                request,
                now=110.0,
                ttl=60.0,
            )

        request, pending = pending_case("scope")
        other_scope = self.fixture.admitted(
            session="other-private",
            message="other-scope-confirm",
            content="确认保存",
        )
        with self.assertRaisesRegex(ContractViolation, "confirmation_scope_mismatch"):
            self.controller.route_pending_resolution(
                pending,
                self.fixture.ticket_for(other_scope),
                current_message=self.fixture.content_for(other_scope),
                now=120.0,
            )

        unclear_admission = self.fixture.admitted(
            session="origin-scope",
            message="unclear-confirm",
            content="确认保存，不过先解释一下",
        )
        unclear = self.controller.route_pending_resolution(
            pending,
            self.fixture.ticket_for(unclear_admission),
            current_message=self.fixture.content_for(unclear_admission),
            now=125.0,
        )
        self.assertIs(unclear.status, PendingResolutionStatus.UNCLEAR)
        self.assertIsNone(unclear.route)
        self.assertIs(
            self.fixture.claim_ticket_directly(unclear_admission),
            self.fixture.ticket_for(unclear_admission),
        )

        request_deny, pending_deny = pending_case("deny")
        deny_admission = self.fixture.admitted(
            session="origin-deny",
            message="deny-followup",
            content="拒绝保存",
        )
        deny_route = self.resolution_route(
            pending_deny,
            deny_admission,
            now=130.0,
            expected=PendingResolutionStatus.DENY,
        )
        self.assertIs(
            self._resolution_plans[id(deny_route)].kind,
            ActionKind.REPLY,
        )
        self.assertIsNone(
            self._resolution_plans[id(deny_route)].action.operation_intent
        )
        denied = self.resolve(deny_route, now=130.0)
        self.assertIs(denied.status, ActionReceiptStatus.DENIED)
        self.assertIs(denied.effect_state, ActionEffectState.NOT_STARTED)
        self.assertEqual(denied.attempt_count, 0)
        deny_lineage = self.controller.continuation_lineage_for(deny_route)
        deny_inspection = self.controller.inspect_continuation_lineage(deny_lineage)
        self.assertIs(deny_inspection.receipt, denied)
        self.assertIs(deny_inspection.origin_request, request_deny)
        self.assertEqual(denied.action_id, request_deny.action_id)

        request_cancel, pending_cancel = pending_case("cancel")
        cancel_admission = self.fixture.admitted(
            session="origin-cancel",
            message="cancel-followup",
            content="取消保存",
        )
        cancel_route = self.resolution_route(
            pending_cancel,
            cancel_admission,
            now=130.0,
            expected=PendingResolutionStatus.CANCEL,
        )
        self.assertIs(
            self._resolution_plans[id(cancel_route)].kind,
            ActionKind.REPLY,
        )
        cancelled = self.resolve(cancel_route, now=130.0)
        self.assertIs(cancelled.status, ActionReceiptStatus.CANCELLED)
        self.assertEqual(cancelled.attempt_count, 0)
        cancel_lineage = self.controller.continuation_lineage_for(cancel_route)
        cancel_inspection = self.controller.inspect_continuation_lineage(
            cancel_lineage
        )
        self.assertIs(cancel_inspection.receipt, cancelled)
        self.assertIs(cancel_inspection.origin_request, request_cancel)

        request_expire, _pending_expire = pending_case("expire")
        expiry_admission = self.fixture.admitted(
            session="origin-expire",
            message="expiry-followup",
        )
        expired_receipts = self.controller.sweep_expired(now=175.0)
        self.assertEqual(len(expired_receipts), 2)
        expired = next(
            receipt
            for receipt in expired_receipts
            if receipt.request_digest == request_expire.request_digest
        )
        self.assertIs(expired.status, ActionReceiptStatus.STALE)
        self.assertIs(expired.effect_state, ActionEffectState.NOT_STARTED)
        self.assertEqual(expired.attempt_count, 0)
        self.assertEqual(expired.binding, request_expire.binding)
        followup_request = self.issue(
            expiry_admission,
            idempotency_key=digest("expiry-followup-new-action"),
            issued_at=175.0,
            deadline=220.0,
        )
        self.assertIs(
            self.controller.inspect_request(followup_request).request,
            followup_request,
        )
        self.assertFalse(hasattr(self.controller, "expire_pending"))

    def test_completion_is_once_canonical_and_timeout_never_retries(self):
        admission = self.fixture.admitted(message="timeout")
        request = self.issue(admission)
        claim = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        timeout_blueprint = self.completion_blueprint(
            request,
            claim.lease,
            status=ActionReceiptStatus.TIMED_OUT,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            result_digest=digest("bounded-timeout-result"),
            completed_at=150.0,
            reason_codes=("executor_timeout",),
        )
        timed_out = self.controller.complete_execution(claim.lease, timeout_blueprint)
        self.assertTrue(timed_out.is_canonical)
        self.assertIs(self.controller.inspect_receipt(timed_out), timed_out)

        replay = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=151.0,
        )
        self.assertIs(replay.status, ActionClaimStatus.TERMINAL)
        self.assertIs(replay.receipt, timed_out)
        with self.assertRaisesRegex(ContractViolation, "execution_lease_consumed"):
            self.controller.complete_execution(claim.lease, timeout_blueprint)

    def test_mutation_timeout_can_preserve_unknown_or_partial_effect(self):
        for index, (status, effect) in enumerate(
            (
                (ActionReceiptStatus.TIMED_OUT, ActionEffectState.PARTIAL),
                (ActionReceiptStatus.TIMED_OUT, ActionEffectState.UNKNOWN),
                (ActionReceiptStatus.EFFECT_UNKNOWN, ActionEffectState.UNKNOWN),
            )
        ):
            with self.subTest(status=status, effect=effect):
                admission = self.fixture.admitted(
                    session=f"mutation-{index}",
                    message=f"mutation-{index}",
                    content=second_turn_message(f"mutation-{index}"),
                )
                request = self.issue(
                    admission,
                    adapter=self.second_turn_attestation,
                    idempotency_key=digest(f"mutation-idempotency-{index}"),
                    issued_at=100.0,
                    deadline=230.0,
                )
                pending = self.controller.start_confirmation(
                    request,
                    now=110.0,
                    ttl=80.0,
                )
                confirm = self.fixture.admitted(
                    session=f"mutation-{index}",
                    message=f"mutation-confirm-{index}",
                    content="确认保存",
                )
                resolution_route = self.resolution_route(
                    pending,
                    confirm,
                    now=130.0,
                    expected=PendingResolutionStatus.CONFIRM,
                )
                confirmed = self.resolve(resolution_route, now=130.0)
                claim = self.controller.claim_execution(
                    request,
                    pending_confirmation=confirmed,
                    current_binding=confirmed.confirmation_binding,
                    now=131.0,
                )
                assert claim.lease is not None
                receipt = self.complete_fixture(
                    request,
                    claim.lease,
                    status=status,
                    effect_state=effect,
                    result_digest=digest(f"mutation-result-{index}"),
                    completed_at=150.0,
                    reason_codes=("effect_not_proven_absent",),
                )
                self.assertIs(receipt.effect_state, effect)
                self.assertEqual(receipt.attempt_count, 1)

    def test_receipt_forgery_copy_and_cross_controller_are_rejected(self):
        admission = self.fixture.admitted(message="receipt-authority")
        request = self.issue(admission)
        claim = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        output = ActionOutput.from_safe_text(
            binding=request.binding,
            request_digest=request.request_digest,
            text="bounded synthetic output",
        )
        receipt = self.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            result_digest=digest("success-result"),
            output=output,
            completed_at=125.0,
        )

        with self.assertRaises(ContractViolation):
            dataclasses.replace(receipt)

        copied = object.__new__(type(receipt))
        for field in dataclasses.fields(receipt):
            object.__setattr__(copied, field.name, getattr(receipt, field.name))
        self.assertFalse(copied.is_canonical)
        with self.assertRaisesRegex(ContractViolation, "receipt_not_canonical"):
            self.controller.inspect_receipt(copied)

        other = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            self.plan_authority,
        )
        with self.assertRaisesRegex(ContractViolation, "receipt_not_canonical"):
            other.inspect_receipt(receipt)

    def test_request_pending_lease_receipt_repr_and_trace_are_redacted(self):
        origin = self.fixture.admitted(
            message="repr-origin",
            content=second_turn_message("repr-origin"),
        )
        request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("repr-idempotency"),
            issued_at=100.0,
            deadline=220.0,
        )
        pending = self.controller.start_confirmation(request, now=110.0, ttl=80.0)
        confirm = self.fixture.admitted(
            message="repr-confirm",
            content="确认保存",
        )
        resolution_route = self.resolution_route(
            pending,
            confirm,
            now=130.0,
            expected=PendingResolutionStatus.CONFIRM,
        )
        confirmed = self.resolve(resolution_route, now=130.0)
        claim = self.controller.claim_execution(
            request,
            pending_confirmation=confirmed,
            current_binding=confirmed.confirmation_binding,
            now=131.0,
        )
        assert claim.lease is not None
        receipt = self.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.EFFECT_UNKNOWN,
            effect_state=ActionEffectState.UNKNOWN,
            result_digest=digest("repr-result"),
            completed_at=150.0,
            reason_codes=("effect_unknown",),
        )

        rendered = ""
        for value in (
            request,
            pending,
            resolution_route,
            confirmed,
            claim.lease,
            receipt,
        ):
            rendered += repr(value)
            rendered += json.dumps(value.trace_metadata(), ensure_ascii=False)
        for forbidden in (
            "C:/private/owner-only.txt",
            "never echo this command",
            "private literal body",
            "确认保存",
            request.action_id,
            request.request_digest,
            request.parameter_digest,
            request.idempotency_key,
            origin.ingress_event.envelope.sender_key,
            origin.ingress_event.envelope.scope_key,
            origin.ingress_event.envelope.session_id,
            origin.ingress_event.content_digest,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_idempotency_duplicate_and_bounded_ledger_fail_closed(self):
        controller = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            self.plan_authority,
            max_ledger_entries=2,
        )

        first_admission = self.fixture.admitted(
            session="bounded-first",
            message="bounded-first",
        )
        first_decision = self.route_decision(first_admission)
        first_proposal = first_decision.proposal
        assert first_proposal is not None
        first_ticket = self.fixture.ticket_for(first_admission)
        first = controller.issue_request(
            first_ticket,
            controller.seal_action_route(
                first_ticket,
                self.planned_action(first_admission),
                first_decision,
            ),
            adapter_draft=self.adapter_draft(first_admission, first_proposal),
            issued_at=100.0,
            deadline=200.0,
            idempotency_key=digest("bounded-shared-idempotency"),
        )
        duplicate_admission = self.fixture.admitted(
            session="bounded-duplicate",
            message="bounded-duplicate",
        )
        duplicate_decision = self.route_decision(duplicate_admission)
        duplicate_proposal = duplicate_decision.proposal
        assert duplicate_proposal is not None
        duplicate_ticket = self.fixture.ticket_for(duplicate_admission)
        with self.assertRaisesRegex(ContractViolation, "idempotency_key_replayed"):
            controller.issue_request(
                duplicate_ticket,
                controller.seal_action_route(
                    duplicate_ticket,
                    self.planned_action(duplicate_admission),
                    duplicate_decision,
                ),
                adapter_draft=self.adapter_draft(
                    duplicate_admission,
                    duplicate_proposal,
                ),
                issued_at=100.0,
                deadline=200.0,
                idempotency_key=first.idempotency_key,
            )
        self.assertIs(
            controller.lookup_idempotent_request(first.idempotency_key),
            first,
        )
        self.assertIs(
            self.fixture.claim_ticket_directly(duplicate_admission),
            duplicate_ticket,
        )

        second_admission = self.fixture.admitted(
            session="bounded-second",
            message="bounded-second",
        )
        second_decision = self.route_decision(second_admission)
        second_proposal = second_decision.proposal
        assert second_proposal is not None
        second_ticket = self.fixture.ticket_for(second_admission)
        controller.issue_request(
            second_ticket,
            controller.seal_action_route(
                second_ticket,
                self.planned_action(second_admission),
                second_decision,
            ),
            adapter_draft=self.adapter_draft(second_admission, second_proposal),
            issued_at=100.0,
            deadline=200.0,
            idempotency_key=digest("bounded-second-idempotency"),
        )
        third_admission = self.fixture.admitted(
            session="bounded-third",
            message="bounded-third",
        )
        third_decision = self.route_decision(third_admission)
        third_proposal = third_decision.proposal
        assert third_proposal is not None
        third_ticket = self.fixture.ticket_for(third_admission)
        with self.assertRaisesRegex(ContractViolation, "owner_action_ledger_full"):
            controller.issue_request(
                third_ticket,
                controller.seal_action_route(
                    third_ticket,
                    self.planned_action(third_admission),
                    third_decision,
                ),
                adapter_draft=self.adapter_draft(third_admission, third_proposal),
                issued_at=100.0,
                deadline=200.0,
                idempotency_key=digest("bounded-third-idempotency"),
            )
        self.assertEqual(controller.ledger_size, 2)
        self.assertIs(
            self.fixture.claim_ticket_directly(third_admission),
            third_ticket,
        )

    def test_sealed_route_snapshot_rejects_action_id_and_digest_mutation(self):
        for index, field_name in enumerate(("action_id", "route_digest")):
            with self.subTest(field_name=field_name):
                admission = self.fixture.admitted(
                    session=f"route-snapshot-{index}",
                    message=f"route-snapshot-{index}",
                )
                ticket = self.fixture.ticket_for(admission)
                decision = self.route_decision(admission)
                proposal = decision.proposal
                assert proposal is not None
                route = self.controller.seal_action_route(
                    ticket,
                    self.planned_action(admission),
                    decision,
                )
                draft = self.adapter_draft(admission, proposal)
                object.__setattr__(route, field_name, "f" * 64)
                with self.assertRaisesRegex(ContractViolation, "action_route_corrupt"):
                    self.controller.issue_request(
                        ticket,
                        route,
                        adapter_draft=draft,
                        issued_at=100.0,
                        deadline=200.0,
                        idempotency_key=digest(f"route-snapshot-{index}"),
                    )
                self.assertIs(
                    self.fixture.owner_action_router.inspect_route(
                        decision,
                        ticket=ticket,
                    ),
                    decision,
                )

    def test_request_snapshot_blocks_deadline_extension_and_nested_binding_mutation(self):
        admission = self.fixture.admitted(
            session="request-deadline-snapshot",
            message="request-deadline-snapshot",
        )
        request = self.issue(
            admission,
            idempotency_key=digest("request-deadline-snapshot"),
            issued_at=100.0,
            deadline=200.0,
        )
        object.__setattr__(request, "deadline", 10000.0)
        with self.assertRaisesRegex(ContractViolation, "request_corrupt"):
            self.controller.claim_execution(
                request,
                current_binding=request.binding,
                now=500.0,
            )
        with self.assertRaisesRegex(ContractViolation, "request_corrupt"):
            self.controller.inspect_request(request)

        binding_admission = self.fixture.admitted(
            session="request-binding-snapshot",
            message="request-binding-snapshot",
        )
        bound_request = self.issue(
            binding_admission,
            idempotency_key=digest("request-binding-snapshot"),
        )
        object.__setattr__(
            bound_request.binding,
            "generation_epoch",
            bound_request.binding.generation_epoch + 1,
        )
        with self.assertRaisesRegex(ContractViolation, "request_corrupt"):
            self.controller.inspect_request(bound_request)

    def test_denial_snapshot_rejects_status_and_effect_mutation(self):
        admission = self.fixture.admitted(
            session="denial-snapshot",
            message="denial-snapshot",
        )
        ticket = self.fixture.ticket_for(admission)
        route = self.route(admission)
        denial = self.controller.deny_action_route(
            ticket,
            route,
            denied_at=120.0,
            reason_codes=("adapter_unavailable",),
        )
        object.__setattr__(denial, "status", ActionReceiptStatus.SUCCEEDED)
        object.__setattr__(denial, "effect_state", ActionEffectState.COMMITTED)
        with self.assertRaisesRegex(ContractViolation, "action_denial_corrupt"):
            self.controller.inspect_denial(denial)

    def test_resolution_route_snapshot_survives_replay_state_and_guards_lineage(self):
        origin = self.fixture.admitted(
            session="resolution-snapshot",
            message="resolution-snapshot-origin",
            content=second_turn_message("resolution-snapshot"),
        )
        request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("resolution-snapshot"),
            issued_at=100.0,
            deadline=220.0,
        )
        pending = self.controller.start_confirmation(request, now=110.0, ttl=80.0)
        confirmation = self.fixture.admitted(
            session="resolution-snapshot",
            message="resolution-snapshot-confirm",
            content="确认保存",
        )
        route = self.resolution_route(
            pending,
            confirmation,
            now=130.0,
            expected=PendingResolutionStatus.CONFIRM,
        )
        original_digest = route.route_digest
        object.__setattr__(route, "route_digest", "f" * 64)
        with self.assertRaisesRegex(
            ContractViolation,
            "pending_resolution_route_corrupt",
        ):
            self.resolve(route, now=130.0)

        object.__setattr__(route, "route_digest", original_digest)
        confirmed = self.resolve(route, now=130.0)
        self.assertTrue(confirmed.consumed)
        lineage = self.controller.continuation_lineage_for(route)
        object.__setattr__(route, "decision", PendingDecision.DENY)
        with self.assertRaisesRegex(
            ContractViolation,
            "pending_resolution_route_corrupt",
        ):
            self.controller.continuation_lineage_for(route)
        with self.assertRaisesRegex(
            ContractViolation,
            "pending_resolution_route_corrupt",
        ):
            self.controller.inspect_continuation_lineage(lineage)

    def test_continuation_rechecks_origin_request_and_eventual_receipt_snapshots(self):
        origin = self.fixture.admitted(
            session="lineage-snapshot",
            message="lineage-snapshot-origin",
            content=second_turn_message("lineage-snapshot"),
        )
        request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("lineage-snapshot"),
            issued_at=100.0,
            deadline=220.0,
        )
        pending = self.controller.start_confirmation(request, now=110.0, ttl=80.0)
        denial_turn = self.fixture.admitted(
            session="lineage-snapshot",
            message="lineage-snapshot-deny",
            content="拒绝保存",
        )
        route = self.resolution_route(
            pending,
            denial_turn,
            now=130.0,
            expected=PendingResolutionStatus.DENY,
        )
        receipt = self.resolve(route, now=130.0)
        lineage = self.controller.continuation_lineage_for(route)
        self.assertIs(
            self.controller.inspect_continuation_lineage(lineage).receipt,
            receipt,
        )

        object.__setattr__(request, "operation", OwnerActionOperation.ARTIFACT_GREP)
        with self.assertRaisesRegex(ContractViolation, "request_corrupt"):
            self.controller.inspect_continuation_lineage(lineage)
        object.__setattr__(request, "operation", OwnerActionOperation.MEMORY_WRITE_LITERAL)

        object.__setattr__(receipt, "status", ActionReceiptStatus.SUCCEEDED)
        object.__setattr__(receipt, "effect_state", ActionEffectState.COMMITTED)
        self.assertFalse(receipt.is_canonical)
        with self.assertRaisesRegex(ContractViolation, "receipt_not_canonical"):
            self.controller.inspect_receipt(receipt)
        with self.assertRaisesRegex(ContractViolation, "receipt_not_canonical"):
            self.controller.inspect_continuation_lineage(lineage)

    def test_pending_snapshot_rejects_expiry_and_parameter_rebinding(self):
        for index, field_name in enumerate(("expires_at", "parameter_digest")):
            with self.subTest(field_name=field_name):
                origin = self.fixture.admitted(
                    session=f"pending-snapshot-{index}",
                    message=f"pending-snapshot-origin-{index}",
                    content=second_turn_message(f"pending-snapshot-{index}"),
                )
                request = self.issue(
                    origin,
                    adapter=self.second_turn_attestation,
                    idempotency_key=digest(f"pending-snapshot-{index}"),
                    issued_at=100.0,
                    deadline=220.0,
                )
                pending = self.controller.start_confirmation(
                    request,
                    now=110.0,
                    ttl=80.0,
                )
                object.__setattr__(
                    pending,
                    field_name,
                    10000.0 if field_name == "expires_at" else "f" * 64,
                )
                current = self.fixture.admitted(
                    session=f"pending-snapshot-{index}",
                    message=f"pending-snapshot-current-{index}",
                    content="确认保存",
                )
                with self.assertRaisesRegex(ContractViolation, "pending_corrupt"):
                    self.controller.match_pending(
                        self.fixture.ticket_for(current),
                        now=500.0,
                    )
                self.assertIs(
                    self.fixture.claim_ticket_directly(current),
                    self.fixture.ticket_for(current),
                )

    def test_execution_lease_snapshot_rejects_deadline_and_binding_mutation(self):
        for index, field_name in enumerate(("deadline", "binding")):
            with self.subTest(field_name=field_name):
                admission = self.fixture.admitted(
                    session=f"lease-snapshot-{index}",
                    message=f"lease-snapshot-{index}",
                )
                request = self.issue(
                    admission,
                    idempotency_key=digest(f"lease-snapshot-{index}"),
                )
                claim = self.controller.claim_execution(
                    request,
                    current_binding=request.binding,
                    now=120.0,
                )
                assert claim.lease is not None
                blueprint = self.completion_blueprint(
                    request,
                    claim.lease,
                    status=ActionReceiptStatus.SUCCEEDED,
                    effect_state=ActionEffectState.NO_SIDE_EFFECT,
                    result_digest=digest(f"lease-snapshot-result-{index}"),
                    output=ActionOutput.from_safe_text(
                        binding=request.binding,
                        request_digest=request.request_digest,
                        text="must not be published",
                    ),
                    completed_at=130.0,
                )
                if field_name == "deadline":
                    object.__setattr__(claim.lease, field_name, 10000.0)
                else:
                    object.__setattr__(
                        claim.lease,
                        field_name,
                        dataclasses.replace(
                            request.binding,
                            generation_epoch=request.binding.generation_epoch + 1,
                        ),
                    )
                with self.assertRaisesRegex(
                    ContractViolation,
                    "execution_lease_corrupt",
                ):
                    self.controller.complete_execution(claim.lease, blueprint)

    def test_public_origin_lineage_inspectors_return_exact_plan_and_route(self):
        admission = self.fixture.admitted(
            session="origin-lineage-request",
            message="origin-lineage-request",
        )
        ticket = self.fixture.ticket_for(admission)
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        origin_plan = self.planned_action(admission)
        same_field_plan = self.planned_action(admission)
        self.assertIsNot(origin_plan, same_field_plan)
        self.assertEqual(origin_plan.action_id, same_field_plan.action_id)
        route = self.controller.seal_action_route(
            ticket,
            origin_plan,
            decision,
        )
        request = self.controller.issue_request(
            ticket,
            route,
            adapter_draft=self.adapter_draft(admission, proposal),
            issued_at=100.0,
            deadline=200.0,
            idempotency_key=digest("origin-lineage-request"),
        )
        request_lineage = self.controller.inspect_request_lineage(request)
        self.assertIsInstance(
            request_lineage,
            OwnerActionRequestLineageInspection,
        )
        self.assertIs(request_lineage.request, request)
        self.assertIs(request_lineage.planned_action, origin_plan)
        self.assertIsNot(request_lineage.planned_action, same_field_plan)
        self.assertIs(request_lineage.action_route, route)

        claim = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        receipt = self.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            result_digest=digest("origin-lineage-result"),
            output=ActionOutput.from_safe_text(
                binding=request.binding,
                request_digest=request.request_digest,
                text="bounded output",
            ),
            completed_at=130.0,
        )
        receipt_lineage = self.controller.inspect_receipt_lineage(receipt)
        self.assertIsInstance(receipt_lineage, OwnerActionReceiptInspection)
        self.assertIs(receipt_lineage.receipt, receipt)
        self.assertIs(receipt_lineage.request, request)
        self.assertIs(receipt_lineage.planned_action, origin_plan)
        self.assertIs(receipt_lineage.action_route, route)

        denial_admission = self.fixture.admitted(
            session="origin-lineage-denial",
            message="origin-lineage-denial",
        )
        denial_ticket = self.fixture.ticket_for(denial_admission)
        denial_decision = self.route_decision(denial_admission)
        denial_plan = self.planned_action(denial_admission)
        denial_substitute = self.planned_action(denial_admission)
        denial_route = self.controller.seal_action_route(
            denial_ticket,
            denial_plan,
            denial_decision,
        )
        denial = self.controller.deny_action_route(
            denial_ticket,
            denial_route,
            denied_at=120.0,
            reason_codes=("adapter_unavailable",),
        )
        denial_lineage = self.controller.inspect_denial_lineage(denial)
        self.assertIsInstance(denial_lineage, OwnerActionDenialInspection)
        self.assertIs(denial_lineage.denial, denial)
        self.assertIs(denial_lineage.planned_action, denial_plan)
        self.assertIsNot(denial_lineage.planned_action, denial_substitute)
        self.assertIs(denial_lineage.action_route, denial_route)

        with self.assertRaisesRegex(
            ContractViolation,
            "action_denial_not_canonical",
        ):
            self.controller.inspect_denial_lineage(copy.copy(denial))
        object.__setattr__(denial_route, "route_digest", "f" * 64)
        with self.assertRaisesRegex(ContractViolation, "action_route_corrupt"):
            self.controller.inspect_denial_lineage(denial)

    def test_prepared_abort_is_atomic_typed_and_releases_sensitive_graph(self):
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_constructor_forbidden",
        ):
            controller_module.OwnerActionLifecycleTombstone()
        admission = self.fixture.admitted(
            session="prepared-abort",
            message="prepared-abort",
        )
        ticket = self.fixture.ticket_for(admission)
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        plan = self.planned_action(admission)
        route = self.controller.seal_action_route(ticket, plan, decision)
        draft = self.adapter_draft(admission, proposal)
        draft_ref = weakref.ref(draft)
        request = self.controller.issue_request(
            ticket,
            route,
            adapter_draft=draft,
            issued_at=100.0,
            deadline=200.0,
            idempotency_key=digest("prepared-abort"),
        )
        durable = self.durable_harness()
        _handle, abort_ticket = durable.prepared_abort_ticket(request, now=109.0)

        copied = copy.copy(request)
        with self.assertRaisesRegex(
            ContractViolation,
            "durable_abort_ticket_lineage_mismatch",
        ):
            self.controller.abort_prepared(
                copied,
                ticket=abort_ticket,
                durable_authority=durable.authority,
                aborted_at=110.0,
            )
        self.assertIs(self.controller.inspect_request(request).request, request)

        other = OwnerActionController(
            self.fixture.accepted_turn_authority,
            self.fixture.owner_action_router,
            self.plan_authority,
        )
        with self.assertRaises(ContractViolation):
            other.abort_prepared(
                request,
                ticket=abort_ticket,
                durable_authority=durable.authority,
                aborted_at=110.0,
            )

        def abort_once():
            try:
                return self.controller.abort_prepared(
                    request,
                    ticket=abort_ticket,
                    durable_authority=durable.authority,
                    aborted_at=110.0,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: abort_once(), range(8)))
        tombstones = tuple(
            value
            for value in results
            if type(value) is controller_module.OwnerActionLifecycleTombstone
        )
        failures = tuple(value for value in results if type(value) is str)
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(failures, ("durable_abort_ticket_state_invalid",) * 7)
        tombstone = tombstones[0]
        self.assertIs(
            self.controller.inspect_lifecycle_tombstone(tombstone),
            tombstone,
        )
        self.assertIs(tombstone.operation, OwnerActionOperation.ARTIFACT_READ_EXACT)
        self.assertIs(tombstone.status, ActionReceiptStatus.CANCELLED)
        self.assertIs(tombstone.effect_state, ActionEffectState.NOT_STARTED)
        self.assertFalse(tombstone.attempted)
        self.assertTrue(tombstone.retry_blocked)
        self.assertEqual(self.controller.ledger_size, 0)
        self.assertEqual(len(self.controller._routes), 0)
        self.assertEqual(len(self.controller._route_by_ticket), 0)
        with self.assertRaisesRegex(
            ContractViolation,
            "idempotency_key_reclaimed",
        ):
            self.controller.lookup_idempotent_request(request.idempotency_key)
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_not_canonical",
        ):
            self.controller.inspect_lifecycle_tombstone(copy.copy(tombstone))
        with self.assertRaises(ContractViolation):
            other.inspect_lifecycle_tombstone(tombstone)
        object.__setattr__(tombstone, "attempted", True)
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_corrupt",
        ):
            self.controller.inspect_lifecycle_tombstone(tombstone)
        object.__setattr__(tombstone, "attempted", False)
        self.assertIs(
            self.controller.inspect_lifecycle_tombstone(tombstone),
            tombstone,
        )

        rendered = repr(tombstone) + json.dumps(tombstone.trace_metadata())
        for forbidden in (
            request.action_id,
            request.request_digest,
            request.parameter_digest,
            request.idempotency_key,
            "C:\\trusted\\prepared-abort.txt",
        ):
            self.assertNotIn(forbidden, rendered)

        durable.authority.complete_prepared_abort(abort_ticket, now=111.0)
        self.assertEqual(self.controller.trace_metadata()["lifecycle_tombstone_count"], 0)
        del copied, request, draft, route, tombstone, tombstones, results
        gc.collect()
        self.assertIsNone(draft_ref())

    def test_lifecycle_tombstone_rendering_requires_exact_canonical_snapshot(self):
        class Marker:
            value = "SENSITIVE-LIFECYCLE-MARKER"

            def __bool__(self):
                return True

            def __eq__(self, other):
                del other
                return True

        admission = self.fixture.admitted(
            session="tombstone-rendering",
            message="tombstone-rendering",
        )
        request = self.issue(
            admission,
            idempotency_key=digest("tombstone-rendering"),
        )
        tombstone = self.durable_abort(request, aborted_at=110.0)
        canonical_trace = tombstone.trace_metadata()
        self.assertEqual(canonical_trace["kind"], "prepared_aborted")
        self.assertIs(canonical_trace["attempted"], False)
        self.assertIs(canonical_trace["retry_blocked"], True)
        copied = copy.copy(tombstone)
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_corrupt",
        ):
            copied.trace_metadata()
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_corrupt",
        ):
            repr(copied)

        originals = {
            name: object.__getattribute__(tombstone, name)
            for name in (
                "kind",
                "operation",
                "status",
                "effect_state",
                "attempted",
                "retry_blocked",
                "reclaimed_at",
                "lifecycle_fingerprint",
            )
        }
        for field_name, replacement in (
            ("kind", Marker()),
            (
                "kind",
                controller_module.OwnerActionLifecycleKind.TERMINAL_RELEASED,
            ),
            ("operation", Marker()),
            ("status", Marker()),
            ("effect_state", Marker()),
            ("attempted", 0),
            ("retry_blocked", 1),
            ("reclaimed_at", 110),
            ("reclaimed_at", 111.0),
            (
                "lifecycle_fingerprint",
                type("DigestSubclass", (str,), {})(
                    originals["lifecycle_fingerprint"]
                ),
            ),
            ("lifecycle_fingerprint", "f" * 64),
        ):
            with self.subTest(field=field_name):
                object.__setattr__(tombstone, field_name, replacement)
                try:
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "lifecycle_tombstone_corrupt",
                    ):
                        tombstone.trace_metadata()
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "lifecycle_tombstone_corrupt",
                    ):
                        repr(tombstone)
                finally:
                    object.__setattr__(
                        tombstone,
                        field_name,
                        originals[field_name],
                    )
                self.assertIs(
                    self.controller.inspect_lifecycle_tombstone(tombstone),
                    tombstone,
                )

        for field_name in ("kind", "attempted", "lifecycle_fingerprint"):
            with self.subTest(deleted_field=field_name):
                original = originals[field_name]
                object.__delattr__(tombstone, field_name)
                try:
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "lifecycle_tombstone_corrupt",
                    ):
                        self.controller.inspect_lifecycle_tombstone(tombstone)
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "lifecycle_tombstone_corrupt",
                    ):
                        tombstone.trace_metadata()
                    with self.assertRaisesRegex(
                        ContractViolation,
                        "lifecycle_tombstone_corrupt",
                    ):
                        repr(tombstone)
                finally:
                    object.__setattr__(tombstone, field_name, original)
                self.assertIs(
                    self.controller.inspect_lifecycle_tombstone(tombstone),
                    tombstone,
                )

        tombstone_key = id(tombstone)
        canonical_record = self.controller._tombstones[tombstone_key]

        class TruthyEquivalentRecord:
            def __init__(self):
                self.tombstone = tombstone
                self.tombstone_snapshot = canonical_record.tombstone_snapshot

            def __bool__(self):
                return True

            def __eq__(self, other):
                del other
                return True

        self.controller._tombstones[tombstone_key] = TruthyEquivalentRecord()
        try:
            with self.assertRaisesRegex(
                ContractViolation,
                "lifecycle_tombstone_corrupt",
            ):
                self.controller.inspect_lifecycle_tombstone(tombstone)
            with self.assertRaisesRegex(
                ContractViolation,
                "lifecycle_tombstone_corrupt",
            ):
                tombstone.trace_metadata()
            with self.assertRaisesRegex(
                ContractViolation,
                "lifecycle_tombstone_corrupt",
            ):
                repr(tombstone)
        finally:
            self.controller._tombstones[tombstone_key] = canonical_record
        self.assertIs(
            self.controller.inspect_lifecycle_tombstone(tombstone),
            tombstone,
        )

        rendered = repr(tombstone) + json.dumps(tombstone.trace_metadata())
        self.assertNotIn("SENSITIVE-LIFECYCLE-MARKER", rendered)

    def test_canonical_registry_records_require_exact_type_and_complete_fields(self):
        class TruthyImpostor:
            def __init__(self, target):
                self.target = target

            def __getattr__(self, name):
                return object.__getattribute__(self.target, name)

            def __bool__(self):
                return True

            def __eq__(self, other):
                del other
                return True

        def exercise_record(mapping, key, field_name, inspect_record):
            record = mapping[key]
            mapping[key] = TruthyImpostor(record)
            try:
                with self.assertRaises(ContractViolation):
                    inspect_record()
            finally:
                mapping[key] = record
            inspect_record()

            original = object.__getattribute__(record, field_name)
            object.__setattr__(record, field_name, TruthyImpostor(original))
            try:
                with self.assertRaises(ContractViolation):
                    inspect_record()
            finally:
                object.__setattr__(record, field_name, original)
            inspect_record()

            object.__delattr__(record, field_name)
            try:
                with self.assertRaises(ContractViolation):
                    inspect_record()
            finally:
                object.__setattr__(record, field_name, original)
            inspect_record()
            return record

        admission = self.fixture.admitted(
            session="registry-records",
            message="registry-action",
        )
        request = self.issue(
            admission,
            idempotency_key=digest("registry-action"),
        )
        inspect_request = lambda: self.controller.inspect_request(request)
        action_record = exercise_record(
            self.controller._ledger,
            id(request),
            "request_snapshot",
            inspect_request,
        )
        canonical_idempotency = self.controller._idempotency[request.idempotency_key]
        self.controller._idempotency[request.idempotency_key] = TruthyImpostor(
            canonical_idempotency
        )
        try:
            with self.assertRaises(ContractViolation):
                self.controller.lookup_idempotent_request(request.idempotency_key)
        finally:
            self.controller._idempotency[request.idempotency_key] = (
                canonical_idempotency
            )
        self.assertIs(
            self.controller.lookup_idempotent_request(request.idempotency_key),
            request,
        )
        exercise_record(
            self.controller._routes,
            id(action_record.action_route),
            "route_snapshot",
            inspect_request,
        )

        claim = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        inspect_lease = lambda: self.controller._inspect_exact_execution_lease(
            request,
            claim.lease,
        )
        exercise_record(
            self.controller._leases,
            id(claim.lease),
            "lease_snapshot",
            inspect_lease,
        )
        receipt = self.controller.recover_interrupted_execution(
            claim.lease,
            recovered_at=130.0,
        )
        exercise_record(
            self.controller._receipts,
            id(receipt),
            "receipt_snapshot",
            lambda: self.controller.inspect_receipt(receipt),
        )

        denial_admission = self.fixture.admitted(
            session="registry-records",
            message="registry-denial",
        )
        denial_ticket = self.fixture.ticket_for(denial_admission)
        denial_route = self.route(denial_admission)
        denial = self.controller.deny_action_route(
            denial_ticket,
            denial_route,
            denied_at=140.0,
            reason_codes=("adapter_unavailable",),
        )
        exercise_record(
            self.controller._denials,
            id(denial),
            "denial_snapshot",
            lambda: self.controller.inspect_denial(denial),
        )

        origin = self.fixture.admitted(
            session="registry-continuation",
            message="registry-continuation-origin",
            content=second_turn_message("registry-continuation"),
        )
        continued_request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("registry-continuation"),
            issued_at=100.0,
            deadline=220.0,
        )
        pending = self.controller.start_confirmation(
            continued_request,
            now=110.0,
            ttl=80.0,
        )
        denial_turn = self.fixture.admitted(
            session="registry-continuation",
            message="registry-continuation-deny",
            content="拒绝保存",
        )
        resolution_route = self.resolution_route(
            pending,
            denial_turn,
            now=130.0,
            expected=PendingResolutionStatus.DENY,
        )
        exercise_record(
            self.controller._resolution_routes,
            id(resolution_route),
            "route_snapshot",
            lambda: self.controller.inspect_pending_resolution_route(
                resolution_route
            ),
        )
        resolution_ticket = self._resolution_tickets[id(resolution_route)]
        resolution_pair = self.controller._resolution_route_by_ticket[
            id(resolution_ticket)
        ]
        self.controller._resolution_route_by_ticket[id(resolution_ticket)] = (
            TruthyImpostor(resolution_pair)
        )
        try:
            with self.assertRaises(ContractViolation):
                self.controller.route_pending_resolution(
                    pending,
                    resolution_ticket,
                    current_message=self.fixture.content_for(denial_turn),
                    now=130.0,
                )
        finally:
            self.controller._resolution_route_by_ticket[id(resolution_ticket)] = (
                resolution_pair
            )
        self.assertIs(
            self.controller.inspect_pending_resolution_route(resolution_route),
            resolution_route,
        )
        self.resolve(resolution_route, now=130.0)
        lineage = self.controller.continuation_lineage_for(resolution_route)
        continuation_entry = self.controller._continuations[id(lineage)]
        continuation_record = continuation_entry[1]
        self.controller._continuations[id(lineage)] = TruthyImpostor(
            continuation_entry
        )
        try:
            with self.assertRaises(ContractViolation):
                self.controller.inspect_continuation_lineage(lineage)
        finally:
            self.controller._continuations[id(lineage)] = continuation_entry
        self.controller.inspect_continuation_lineage(lineage)
        self.controller._continuations[id(lineage)] = (
            lineage,
            TruthyImpostor(continuation_record),
        )
        try:
            with self.assertRaises(ContractViolation):
                self.controller.inspect_continuation_lineage(lineage)
        finally:
            self.controller._continuations[id(lineage)] = continuation_entry
        self.controller.inspect_continuation_lineage(lineage)
        original_lineage_snapshot = continuation_record.lineage_snapshot
        object.__setattr__(
            continuation_record,
            "lineage_snapshot",
            TruthyImpostor(original_lineage_snapshot),
        )
        try:
            with self.assertRaises(ContractViolation):
                self.controller.inspect_continuation_lineage(lineage)
        finally:
            object.__setattr__(
                continuation_record,
                "lineage_snapshot",
                original_lineage_snapshot,
            )
        self.controller.inspect_continuation_lineage(lineage)
        object.__delattr__(continuation_record, "lineage_snapshot")
        try:
            with self.assertRaises(ContractViolation):
                self.controller.inspect_continuation_lineage(lineage)
        finally:
            object.__setattr__(
                continuation_record,
                "lineage_snapshot",
                original_lineage_snapshot,
            )
        self.controller.inspect_continuation_lineage(lineage)

        aborted_admission = self.fixture.admitted(
            session="registry-records",
            message="registry-tombstone",
        )
        aborted_request = self.issue(
            aborted_admission,
            idempotency_key=digest("registry-tombstone"),
        )
        aborted_record = self.controller._ledger[id(aborted_request)]
        aborted_route_record = self.controller._routes[id(aborted_record.action_route)]
        route_pair_key = id(aborted_route_record.ticket)
        route_pair = self.controller._route_by_ticket[route_pair_key]
        self.controller._route_by_ticket[route_pair_key] = TruthyImpostor(route_pair)
        try:
            with self.assertRaises(ContractViolation):
                self.durable_abort(
                    aborted_request,
                    aborted_at=149.0,
                )
        finally:
            self.controller._route_by_ticket[route_pair_key] = route_pair
        self.assertIs(
            self.controller.inspect_request(aborted_request).request,
            aborted_request,
        )
        tombstone = self.durable_abort(
            aborted_request,
            aborted_at=150.0,
        )
        exercise_record(
            self.controller._tombstones,
            id(tombstone),
            "tombstone_snapshot",
            lambda: self.controller.inspect_lifecycle_tombstone(tombstone),
        )
        tombstone_record = self.controller._idempotency[
            aborted_request.idempotency_key
        ]
        self.controller._idempotency[aborted_request.idempotency_key] = (
            TruthyImpostor(tombstone_record)
        )
        try:
            with self.assertRaises(ContractViolation):
                self.controller.lookup_idempotent_request(
                    aborted_request.idempotency_key
                )
        finally:
            self.controller._idempotency[aborted_request.idempotency_key] = (
                tombstone_record
            )
        with self.assertRaisesRegex(
            ContractViolation,
            "idempotency_key_reclaimed",
        ):
            self.controller.lookup_idempotent_request(
                aborted_request.idempotency_key
            )

    def test_prepared_abort_rolls_back_write_then_baseexception_and_retries(self):
        class WriteThenRaiseDict(dict):
            def __init__(self, values):
                super().__init__(values)
                self.armed = True
                self.last_value = None

            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                self.last_value = value
                if self.armed:
                    self.armed = False
                    raise KeyboardInterrupt("write_then_raise")

        admission = self.fixture.admitted(
            session="prepared-abort-rollback",
            message="prepared-abort-rollback",
        )
        request = self.issue(
            admission,
            idempotency_key=digest("prepared-abort-rollback"),
        )
        self.controller._idempotency = WriteThenRaiseDict(
            self.controller._idempotency
        )

        with self.assertRaisesRegex(KeyboardInterrupt, "write_then_raise"):
            self.durable_abort(request, aborted_at=110.0)

        self.assertIs(self.controller.inspect_request(request).request, request)
        self.assertIs(
            self.controller.lookup_idempotent_request(request.idempotency_key),
            request,
        )
        self.assertEqual(self.controller.ledger_size, 1)
        self.assertEqual(len(self.controller._routes), 1)
        self.assertEqual(len(self.controller._route_by_ticket), 1)
        self.assertEqual(len(self.controller._tombstones), 0)
        orphan_record = self.controller._idempotency.last_value
        assert orphan_record is not None
        orphan_tombstone = orphan_record.tombstone
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_not_canonical",
        ):
            orphan_tombstone.trace_metadata()
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_not_canonical",
        ):
            repr(orphan_tombstone)

        tombstone = self.durable_abort(request, aborted_at=111.0)
        self.assertIs(
            self.controller.inspect_lifecycle_tombstone(tombstone),
            tombstone,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_not_canonical",
        ):
            orphan_tombstone.trace_metadata()
        self.assertEqual(self.controller.ledger_size, 0)

    def test_recover_interrupted_execution_is_code_mapped_and_never_retries(self):
        read_admission = self.fixture.admitted(
            session="recovery-read",
            message="recovery-read",
        )
        read_request = self.issue(
            read_admission,
            idempotency_key=digest("recovery-read"),
        )
        read_claim = self.controller.claim_execution(
            read_request,
            current_binding=read_request.binding,
            now=120.0,
        )
        assert read_claim.lease is not None
        with self.assertRaisesRegex(
            ContractViolation,
            "recovered_before_execution_start",
        ):
            self.controller.recover_interrupted_execution(
                read_claim.lease,
                recovered_at=119.0,
            )
        self.assertIs(
            self.controller.inspect_request(read_request).state,
            OwnerActionLedgerState.IN_PROGRESS,
        )
        read_receipt = self.controller.recover_interrupted_execution(
            read_claim.lease,
            recovered_at=130.0,
        )
        self.assertIs(read_receipt.status, ActionReceiptStatus.FAILED)
        self.assertIs(read_receipt.effect_state, ActionEffectState.NO_SIDE_EFFECT)
        self.assertEqual(read_receipt.attempt_count, 1)
        terminal = self.controller.claim_execution(
            read_request,
            current_binding=read_request.binding,
            now=131.0,
        )
        self.assertIs(terminal.status, ActionClaimStatus.TERMINAL)
        self.assertIs(terminal.receipt, read_receipt)
        with self.assertRaisesRegex(ContractViolation, "execution_lease_consumed"):
            self.controller.recover_interrupted_execution(
                read_claim.lease,
                recovered_at=132.0,
            )

        mutation_admission = self.fixture.admitted(
            session="recovery-mutation",
            message="recovery-mutation",
            content=second_turn_message("recovery-mutation"),
        )
        current_turn_memory = dataclasses.replace(
            self.second_turn_attestation,
            confirmation_policy=ActionConfirmationPolicy.CURRENT_EXPLICIT_REQUEST,
        )
        mutation_request = self.issue(
            mutation_admission,
            adapter=current_turn_memory,
            idempotency_key=digest("recovery-mutation"),
        )
        mutation_claim = self.controller.claim_execution(
            mutation_request,
            current_binding=mutation_request.binding,
            now=120.0,
        )
        assert mutation_claim.lease is not None

        with self.assertRaisesRegex(ContractViolation, "execution_lease_not_canonical"):
            self.controller.recover_interrupted_execution(
                copy.copy(mutation_claim.lease),
                recovered_at=130.0,
            )

        def recover_once():
            try:
                return self.controller.recover_interrupted_execution(
                    mutation_claim.lease,
                    recovered_at=130.0,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: recover_once(), range(8)))
        receipts = tuple(value for value in results if type(value) is ActionReceipt)
        failures = tuple(value for value in results if type(value) is str)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(failures, ("execution_lease_consumed",) * 7)
        mutation_receipt = receipts[0]
        self.assertIs(mutation_receipt.status, ActionReceiptStatus.EFFECT_UNKNOWN)
        self.assertIs(mutation_receipt.effect_state, ActionEffectState.UNKNOWN)
        self.assertEqual(mutation_receipt.attempt_count, 1)
        terminal = self.controller.claim_execution(
            mutation_request,
            current_binding=mutation_request.binding,
            now=131.0,
        )
        self.assertIs(terminal.status, ActionClaimStatus.TERMINAL)
        self.assertIs(terminal.receipt, mutation_receipt)

    def test_interrupted_recovery_rolls_back_write_then_baseexception_and_retries(self):
        class WriteThenRaiseDict(dict):
            def __init__(self, values):
                super().__init__(values)
                self.armed = True

            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                if self.armed:
                    self.armed = False
                    raise KeyboardInterrupt("write_then_raise")

        admission = self.fixture.admitted(
            session="recovery-rollback",
            message="recovery-rollback",
        )
        request = self.issue(
            admission,
            idempotency_key=digest("recovery-rollback"),
        )
        claim = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        self.controller._receipts = WriteThenRaiseDict(self.controller._receipts)

        with self.assertRaisesRegex(KeyboardInterrupt, "write_then_raise"):
            self.controller.recover_interrupted_execution(
                claim.lease,
                recovered_at=130.0,
            )

        inspection = self.controller.inspect_request(request)
        self.assertIs(inspection.state, OwnerActionLedgerState.IN_PROGRESS)
        record = self.controller._ledger[id(request)]
        self.assertFalse(record.lease_consumed)
        self.assertIsNone(record.latest_receipt)
        self.assertEqual(len(self.controller._receipts), 0)

        receipt = self.controller.recover_interrupted_execution(
            claim.lease,
            recovered_at=131.0,
        )
        self.assertIs(receipt.status, ActionReceiptStatus.FAILED)
        self.assertIs(receipt.effect_state, ActionEffectState.NO_SIDE_EFFECT)
        terminal = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=132.0,
        )
        self.assertIs(terminal.status, ActionClaimStatus.TERMINAL)
        self.assertIs(terminal.receipt, receipt)

    def test_interrupted_mutation_recovery_rolls_back_continuation_and_retries(self):
        class WriteThenRaiseDict(dict):
            def __init__(self, values):
                super().__init__(values)
                self.armed = True

            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                if self.armed:
                    self.armed = False
                    raise KeyboardInterrupt("write_then_raise")

        origin = self.fixture.admitted(
            session="recovery-mutation-rollback",
            message="recovery-mutation-rollback-origin",
            content=second_turn_message("recovery-mutation-rollback"),
        )
        request = self.issue(
            origin,
            adapter=self.second_turn_attestation,
            idempotency_key=digest("recovery-mutation-rollback"),
            issued_at=100.0,
            deadline=220.0,
        )
        pending = self.controller.start_confirmation(request, now=110.0, ttl=80.0)
        confirmation_receipt = self.controller.canonical_receipt_for(request)
        assert confirmation_receipt is not None
        confirmation = self.fixture.admitted(
            session="recovery-mutation-rollback",
            message="recovery-mutation-rollback-confirm",
            content="确认保存",
        )
        route = self.resolution_route(
            pending,
            confirmation,
            now=130.0,
            expected=PendingResolutionStatus.CONFIRM,
        )
        confirmed = self.resolve(route, now=130.0)
        lineage = self.controller.continuation_lineage_for(route)
        claim = self.controller.claim_execution(
            request,
            current_binding=confirmed.confirmation_binding,
            pending_confirmation=confirmed,
            now=140.0,
        )
        assert claim.lease is not None
        self.controller._receipts = WriteThenRaiseDict(self.controller._receipts)

        with self.assertRaisesRegex(KeyboardInterrupt, "write_then_raise"):
            self.controller.recover_interrupted_execution(
                claim.lease,
                recovered_at=150.0,
            )

        inspection = self.controller.inspect_request(request)
        self.assertIs(inspection.state, OwnerActionLedgerState.IN_PROGRESS)
        record = self.controller._ledger[id(request)]
        self.assertFalse(record.lease_consumed)
        self.assertIs(record.latest_receipt, confirmation_receipt)
        self.assertIs(
            self.controller.canonical_receipt_for(request),
            confirmation_receipt,
        )
        self.assertEqual(len(self.controller._receipts), 1)
        self.assertIsNone(
            self.controller.inspect_continuation_lineage(lineage).receipt
        )

        receipt = self.controller.recover_interrupted_execution(
            claim.lease,
            recovered_at=151.0,
        )
        self.assertIs(receipt.status, ActionReceiptStatus.EFFECT_UNKNOWN)
        self.assertIs(receipt.effect_state, ActionEffectState.UNKNOWN)
        self.assertIs(
            self.controller.inspect_continuation_lineage(lineage).receipt,
            receipt,
        )
        terminal = self.controller.claim_execution(
            request,
            current_binding=confirmed.confirmation_binding,
            pending_confirmation=confirmed,
            now=152.0,
        )
        self.assertIs(terminal.status, ActionClaimStatus.TERMINAL)
        self.assertIs(terminal.receipt, receipt)

    def test_terminal_release_requires_reclaimed_outcome_and_compacts_full_lineage(self):
        from astrbot_plugin_shio.core.action_outcome import ActionOutcomeAuthority

        admission = self.fixture.admitted(
            session="terminal-release",
            message="terminal-release",
        )
        ticket = self.fixture.ticket_for(admission)
        decision = self.route_decision(admission)
        proposal = decision.proposal
        assert proposal is not None
        plan = self.planned_action(admission)
        route = self.controller.seal_action_route(ticket, plan, decision)
        draft = self.adapter_draft(admission, proposal)
        draft_ref = weakref.ref(draft)
        request = self.controller.issue_request(
            ticket,
            route,
            adapter_draft=draft,
            issued_at=100.0,
            deadline=200.0,
            idempotency_key=digest("terminal-release"),
        )
        claim = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        receipt = self.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.FAILED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            result_digest=digest("terminal-release-result"),
            completed_at=130.0,
            reason_codes=("bounded_failure",),
        )
        receipt_ref = weakref.ref(receipt)
        authority = ActionOutcomeAuthority.issue_for_runtime(
            self.controller,
            self.plan_authority,
        )
        outcome = authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        with self.assertRaises(TypeError):
            self.controller.release_terminal_lineage(
                copy.copy(receipt),
                outcome=outcome,
                outcome_authority=authority,
                reclaimed_at=140.0,
            )
        with self.assertRaises(TypeError):
            self.controller.release_terminal_lineage(
                receipt,
                outcome=outcome,
                outcome_authority=authority,
                reclaimed_at=140.0,
            )
        self.assertIs(self.controller.inspect_receipt(receipt), receipt)
        self.assertIs(
            authority.inspect_outcome(outcome, current_planned_action=plan),
            outcome,
        )

        authority.abandon_before_composer(
            outcome,
            current_planned_action=plan,
        )
        durable = DurableFinalizeHarness(self.controller, authority)
        tickets = durable.abandoned_tickets(
            source=receipt,
            outcome=outcome,
            current_planned_action=plan,
            now=150.0,
        )

        def release_once():
            try:
                return self.controller.release_terminal_lineage(
                    receipt,
                    outcome=outcome,
                    outcome_authority=authority,
                    ticket=tickets.controller,
                    durable_authority=durable.authority,
                    reclaimed_at=140.0,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(lambda _: release_once(), range(8)))
        tombstones = tuple(
            value
            for value in results
            if type(value) is controller_module.OwnerActionLifecycleTombstone
        )
        failures = tuple(value for value in results if type(value) is str)
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(
            failures,
            ("durable_finalize_ticket_consumed",) * 7,
        )
        tombstone = tombstones[0]
        self.assertIs(tombstone.status, ActionReceiptStatus.FAILED)
        self.assertIs(tombstone.effect_state, ActionEffectState.NO_SIDE_EFFECT)
        self.assertTrue(tombstone.attempted)
        self.assertTrue(tombstone.retry_blocked)
        self.assertIs(
            self.controller.inspect_lifecycle_tombstone(tombstone),
            tombstone,
        )
        self.assertEqual(self.controller.ledger_size, 0)
        self.assertEqual(len(self.controller._routes), 0)
        self.assertEqual(len(self.controller._route_by_ticket), 0)
        self.assertEqual(len(self.controller._leases), 0)
        self.assertEqual(len(self.controller._receipts), 0)
        with self.assertRaisesRegex(ContractViolation, "request_not_canonical"):
            self.controller.inspect_request(request)
        with self.assertRaisesRegex(ContractViolation, "receipt_not_canonical"):
            self.controller.inspect_receipt(receipt)
        with self.assertRaisesRegex(
            ContractViolation,
            "idempotency_key_reclaimed",
        ):
            self.controller.lookup_idempotent_request(request.idempotency_key)

        rendered = repr(tombstone) + json.dumps(tombstone.trace_metadata())
        for forbidden in (
            request.action_id,
            request.request_digest,
            request.parameter_digest,
            request.idempotency_key,
            "C:\\trusted\\terminal-release.txt",
        ):
            self.assertNotIn(forbidden, rendered)

        durable.finalize_outcome(
            outcome=outcome,
            current_planned_action=plan,
            ticket=tickets.outcome,
        )
        durable.close()
        self.assertIsNone(
            self.controller.lookup_idempotent_request(request.idempotency_key)
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "lifecycle_tombstone_not_canonical",
        ):
            self.controller.inspect_lifecycle_tombstone(tombstone)

        del request, draft, route, claim, receipt, outcome, tombstone, tombstones, results
        gc.collect()
        self.assertIsNone(draft_ref())
        self.assertIsNone(receipt_ref())

    def test_terminal_release_rolls_back_delete_then_baseexception_and_retries(self):
        from astrbot_plugin_shio.core.action_outcome import ActionOutcomeAuthority

        class DeleteThenRaiseDict(dict):
            def __init__(self, values):
                super().__init__(values)
                self.armed = True

            def __delitem__(self, key):
                super().__delitem__(key)
                if self.armed:
                    self.armed = False
                    raise KeyboardInterrupt("delete_then_raise")

        admission = self.fixture.admitted(
            session="terminal-release-rollback",
            message="terminal-release-rollback",
        )
        request = self.issue(
            admission,
            idempotency_key=digest("terminal-release-rollback"),
        )
        claim = self.controller.claim_execution(
            request,
            current_binding=request.binding,
            now=120.0,
        )
        assert claim.lease is not None
        receipt = self.complete_fixture(
            request,
            claim.lease,
            status=ActionReceiptStatus.FAILED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            result_digest=digest("terminal-release-rollback-result"),
            completed_at=130.0,
            reason_codes=("bounded_failure",),
        )
        plan = self.controller.inspect_request_lineage(request).planned_action
        authority = ActionOutcomeAuthority.issue_for_runtime(
            self.controller,
            self.plan_authority,
        )
        outcome = authority.issue_from_receipt(
            current_planned_action=plan,
            origin_request=request,
            receipt=receipt,
        )
        authority.abandon_before_composer(
            outcome,
            current_planned_action=plan,
        )
        durable = DurableFinalizeHarness(self.controller, authority)
        tickets = durable.abandoned_tickets(
            source=receipt,
            outcome=outcome,
            current_planned_action=plan,
            now=150.0,
        )
        self.controller._receipts = DeleteThenRaiseDict(
            self.controller._receipts
        )

        with self.assertRaisesRegex(KeyboardInterrupt, "delete_then_raise"):
            self.controller.release_terminal_lineage(
                receipt,
                outcome=outcome,
                outcome_authority=authority,
                ticket=tickets.controller,
                durable_authority=durable.authority,
                reclaimed_at=140.0,
            )

        self.assertIs(self.controller.inspect_request(request).request, request)
        self.assertIs(self.controller.inspect_receipt(receipt), receipt)
        self.assertIs(
            self.controller.lookup_idempotent_request(request.idempotency_key),
            request,
        )
        self.assertEqual(self.controller.ledger_size, 1)
        self.assertEqual(len(self.controller._routes), 1)
        self.assertEqual(len(self.controller._route_by_ticket), 1)
        self.assertEqual(len(self.controller._leases), 1)
        self.assertEqual(len(self.controller._receipts), 1)
        self.assertEqual(len(self.controller._tombstones), 0)

        tombstone = self.controller.release_terminal_lineage(
            receipt,
            outcome=outcome,
            outcome_authority=authority,
            ticket=tickets.controller,
            durable_authority=durable.authority,
            reclaimed_at=141.0,
        )
        self.assertIs(
            self.controller.inspect_lifecycle_tombstone(tombstone),
            tombstone,
        )
        durable.finalize_outcome(
            outcome=outcome,
            current_planned_action=plan,
            ticket=tickets.outcome,
        )
        durable.close()
        self.assertEqual(self.controller.ledger_size, 0)


if __name__ == "__main__":
    unittest.main()
