from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import inspect
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from astrbot_plugin_shio.core import owner_action_adapters as adapters
from astrbot_plugin_shio.core import owner_action_executor as executor_mod
from astrbot_plugin_shio.core import owner_action_runtime_collector as collector_mod
from astrbot_plugin_shio.core.contracts import (
    ActionEffectState,
    ActionOutput,
    ActionReceiptStatus,
    ActionSideEffect,
    ContractViolation,
    DecisionBinding,
    OwnerActionOperation,
)
from astrbot_plugin_shio.core.owner_action_controller import (
    ActionClaimStatus,
    OwnerActionController,
    OwnerActionLedgerState,
)
from astrbot_plugin_shio.tests.test_owner_action_runtime_collector import (
    FILE_READ_SCHEMA,
    MEMORY_SCHEMA,
)
from astrbot_plugin_shio.tests.test_owner_action_output_guard import _guard


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


async def _fake_file_call(self, context, **arguments):
    self.calls += 1
    self.contexts.append(context)
    self.arguments.append(arguments)
    if self.mode == "timeout":
        await asyncio.sleep(0.2)
    if self.mode == "send":
        await context.send("must-not-send")
    if self.mode == "error":
        raise RuntimeError(self.secret)
    self.completed += 1
    return self.result


async def _fake_memory_call(self, context, **arguments):
    self.calls += 1
    self.contexts.append(context)
    self.arguments.append(arguments)
    self.completed += 1
    return self.result


FileReadTool = type(
    "FileReadTool",
    (),
    {
        "__module__": "astrbot.core.tools.computer_tools.fs",
        "call": _fake_file_call,
    },
)
MemoryMemorizeTool = type(
    "MemoryMemorizeTool",
    (),
    {
        "__module__": (
            "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool"
        ),
        "call": _fake_memory_call,
    },
)
PermissionGuardedTool = type(
    "_PermissionGuardedTool",
    (),
    {
        "__module__": "astrbot.core.provider.func_tool_manager",
        "call": _fake_memory_call,
    },
)


def _tool(tool_type, name: str, schema: dict, *, result=b"safe bytes"):
    tool = tool_type()
    tool.name = name
    tool.parameters = copy.deepcopy(schema)
    tool.active = True
    tool.handler = None
    tool.handler_module_path = type(tool).__module__
    tool.is_background_task = False
    tool.calls = 0
    tool.completed = 0
    tool.contexts = []
    tool.arguments = []
    tool.mode = "return"
    tool.result = result
    tool.secret = "raw-secret-exception"
    return tool


class _BuiltinManager:
    def __init__(self, tool) -> None:
        self.tool = tool
        self.func_list = [tool]

    def get_builtin_tool(self, name: str):
        if name != self.tool.name:
            raise KeyError(name)
        return self.tool


class _ToolSet:
    def __init__(self, tool) -> None:
        self.tool = tool

    def get_tool(self, name: str):
        return self.tool if self.tool.name == name else None


class _MemoryManager:
    def __init__(self, raw, wrapper) -> None:
        self.raw = raw
        self.wrapper = wrapper
        self.func_list = [raw]

    def get_full_tool_set(self):
        return _ToolSet(self.wrapper)

    def _check_tool_permission(self, name: str, context):
        if name != self.raw.name:
            return "error"
        return None if context.context.event.is_admin() else "error: denied"


@dataclasses.dataclass
class _Attempt:
    support: object
    manager: object
    tool: object
    collector: collector_mod.RuntimeConformanceCollector
    draft: adapters.AdapterDraft
    request: object
    lease: object
    clock: _Clock
    executor: executor_mod.OwnerActionExecutor


def _read_attempt(
    *,
    result=b"complete raw bytes",
    mode: str = "return",
    deadline: float = 200.0,
    now: float = 110.0,
) -> _Attempt:
    from astrbot_plugin_shio.tests import (
        test_owner_action_controller as controller_fixture,
    )

    support = controller_fixture.OwnerActionControllerTests(methodName="runTest")
    support.setUp()
    message = '请读取文件 path="C:\\trusted\\executor.txt" offset=0 limit=20'
    admission = support.fixture.admitted(
        message="executor-read",
        content=message,
    )
    conversation = admission.conversation_event
    assert conversation is not None
    binding = conversation.binding
    tool = _tool(
        FileReadTool,
        "astrbot_file_read_tool",
        FILE_READ_SCHEMA,
        result=result,
    )
    tool.mode = mode
    manager = _BuiltinManager(tool)
    descriptor = adapters.OWNER_ACTION_ADAPTER_REGISTRY[
        OwnerActionOperation.ARTIFACT_READ_EXACT
    ]
    collector = collector_mod._build_test_runtime_collector(
        manager,
        operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
        implementation_version=descriptor.implementation_version,
    )
    candidate = collector.collect(binding, OwnerActionOperation.ARTIFACT_READ_EXACT)
    path = "C:\\trusted\\executor.txt"
    proof = collector_mod._issue_test_operation_proof(
        artifact_path=adapters.ArtifactPathConformance(
            root_digest=_digest("C:\\trusted"),
            path_digest=_digest(path),
            preflight_token_digest=_digest("executor-preflight"),
            binding_revision=binding.conversation_revision,
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
        )
    )
    evidence = collector.issue_evidence(candidate, proof)
    proposal = support.proposal(admission)
    draft = adapters.compile_owner_action_draft(
        proposal,
        message,
        adapters.AdapterConfig(
            artifact_read_exact_enabled=True,
            artifact_root="C:\\trusted",
            path_flavor=adapters.ArtifactPathFlavor.WINDOWS,
        ),
        evidence,
    )
    decision = support.route_decision(admission)
    route = support.controller.seal_action_route(
        support.fixture.ticket_for(admission),
        support.planned_action(admission),
        decision,
    )
    request = support.controller.issue_request(
        support.fixture.ticket_for(admission),
        route,
        adapter_draft=draft,
        issued_at=100.0,
        deadline=deadline,
        idempotency_key=_digest(f"executor-{id(tool)}"),
        reason_codes=("owner_current_explicit_request",),
    )
    claim = support.controller.claim_execution(
        request,
        current_binding=request.binding,
        now=now,
    )
    assert claim.status is ActionClaimStatus.CLAIMED and claim.lease is not None
    clock = _Clock(now + 1.0)
    executor = executor_mod._build_test_owner_action_executor(
        support.controller,
        clock=clock,
    )
    return _Attempt(
        support=support,
        manager=manager,
        tool=tool,
        collector=collector,
        draft=draft,
        request=request,
        lease=claim.lease,
        clock=clock,
        executor=executor,
    )


def _memory_attempt() -> _Attempt:
    from astrbot_plugin_shio.tests import (
        test_owner_action_controller as controller_fixture,
    )

    support = controller_fixture.OwnerActionControllerTests(methodName="runTest")
    support.setUp()
    message = "请记住：主人只允许保存这一条私聊事实"
    admission = support.fixture.admitted(
        message="executor-memory",
        content=message,
    )
    conversation = admission.conversation_event
    assert conversation is not None
    binding = conversation.binding
    raw = _tool(
        MemoryMemorizeTool,
        "memorize_long_term_memory",
        MEMORY_SCHEMA,
    )
    wrapper = _tool(
        PermissionGuardedTool,
        "memorize_long_term_memory",
        MEMORY_SCHEMA,
    )
    wrapper._wrapped = raw
    wrapper.handler_module_path = type(raw).__module__
    manager = _MemoryManager(raw, wrapper)
    descriptor = adapters.OWNER_ACTION_ADAPTER_REGISTRY[
        OwnerActionOperation.MEMORY_WRITE_LITERAL
    ]
    collector = collector_mod._build_test_runtime_collector(
        manager,
        operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
        implementation_version=descriptor.implementation_version,
        memory_scope_probe=lambda: "private_user",
    )
    candidate = collector.collect(binding, OwnerActionOperation.MEMORY_WRITE_LITERAL)
    token = "owner-private-executor"
    proof = collector_mod._issue_test_operation_proof(
        memory_scope=adapters.MemoryScopeConformance(
            mode=adapters.MemoryScopeMode.PRIVATE_USER,
            expected_scope_token=token,
            resolved_scope_token=token,
            private_chat=True,
            current_owner_subject=True,
            identity_degraded=False,
            global_or_shared_alias_disabled=True,
            dashboard_permission_admin=True,
            owner_is_astrbot_admin=True,
            permission_wrapper_preserved=True,
        )
    )
    evidence = collector.issue_evidence(candidate, proof)
    proposal = support.proposal(
        admission,
        operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
    )
    draft = adapters.compile_owner_action_draft(
        proposal,
        message,
        adapters.AdapterConfig(memory_write_literal_enabled=True),
        evidence,
    )
    decision = support.route_decision(admission)
    route = support.controller.seal_action_route(
        support.fixture.ticket_for(admission),
        support.planned_action(
            admission,
            capability=proposal.capability,
            operation=proposal.operation,
        ),
        decision,
    )
    request = support.controller.issue_request(
        support.fixture.ticket_for(admission),
        route,
        adapter_draft=draft,
        issued_at=100.0,
        deadline=200.0,
        idempotency_key=_digest(f"memory-{id(raw)}"),
    )
    claim = support.controller.claim_execution(
        request,
        current_binding=request.binding,
        now=110.0,
    )
    assert claim.lease is not None
    clock = _Clock(111.0)
    executor = executor_mod._build_test_owner_action_executor(
        support.controller,
        clock=clock,
    )
    return _Attempt(
        support=support,
        manager=manager,
        tool=wrapper,
        collector=collector,
        draft=draft,
        request=request,
        lease=claim.lease,
        clock=clock,
        executor=executor,
    )


def _completion(attempt: _Attempt, blueprint):
    return executor_mod._open_execution_blueprint(
        blueprint,
        controller=attempt.support.controller,
        request=attempt.request,
        lease=attempt.lease,
    )


class OwnerActionExecutorContractTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_pre_call_terminal(
        self,
        attempt: _Attempt,
        *,
        expected_reason: str,
    ):
        blueprint = await attempt.executor.execute(
            attempt.request,
            attempt.lease,
            attempt.draft,
            attempt.collector,
        )
        receipt = attempt.support.controller.complete_execution(
            attempt.lease,
            blueprint,
        )
        self.assertIs(receipt.status, ActionReceiptStatus.DENIED)
        self.assertIs(receipt.effect_state, ActionEffectState.NOT_STARTED)
        self.assertEqual(receipt.attempt_count, 0)
        self.assertEqual(receipt.reason_codes, (expected_reason,))
        self.assertIs(
            attempt.support.controller.inspect_request(attempt.request).state,
            OwnerActionLedgerState.TERMINAL,
        )
        return receipt

    async def test_retained_exact_tool_and_typed_schema_arguments_are_used_once(self):
        attempt = _read_attempt(result=b"raw artifact payload")
        replacement = _tool(
            FileReadTool,
            "astrbot_file_read_tool",
            FILE_READ_SCHEMA,
            result=b"replacement",
        )
        attempt.manager.tool = replacement
        blueprint = await attempt.executor.execute(
            attempt.request,
            attempt.lease,
            attempt.draft,
            attempt.collector,
        )
        self.assertEqual(attempt.tool.calls, 1)
        self.assertEqual(replacement.calls, 0)
        self.assertEqual(
            attempt.tool.arguments,
            [{"path": "C:\\trusted\\executor.txt", "offset": 0, "limit": 20}],
        )
        with self.assertRaisesRegex(RuntimeError, "tool_authority_forbidden"):
            getattr(attempt.tool.contexts[0], "event")
        completion = _completion(attempt, blueprint)
        self.assertIs(completion.status, ActionReceiptStatus.FAILED)
        self.assertIs(completion.effect_state, ActionEffectState.NO_SIDE_EFFECT)
        self.assertEqual(
            completion.reason_codes,
            ("artifact_live_identity_proof_unavailable",),
        )
        self.assertIsNone(completion.output)
        self.assertTrue(completion.receipt_eligible)

    async def test_production_executor_is_hard_blocked_before_tool_invocation(self):
        attempt = _read_attempt()
        production = executor_mod.OwnerActionExecutor(attempt.support.controller)
        blueprint = await production.execute(
            attempt.request,
            attempt.lease,
            attempt.draft,
            attempt.collector,
        )
        self.assertEqual(attempt.tool.calls, 0)
        self.assertTrue(blueprint.attempt_started)
        self.assertFalse(blueprint.tool_invoked)
        receipt = attempt.support.controller.complete_execution(
            attempt.lease,
            blueprint,
        )
        self.assertIs(receipt.status, ActionReceiptStatus.DENIED)
        self.assertIs(receipt.effect_state, ActionEffectState.NOT_STARTED)
        self.assertEqual(receipt.attempt_count, 0)
        self.assertEqual(
            receipt.reason_codes,
            ("production_execution_hard_blocked",),
        )
        self.assertIs(
            attempt.support.controller.inspect_request(attempt.request).state,
            OwnerActionLedgerState.TERMINAL,
        )

    async def test_memory_permission_wrapper_is_preserved_but_never_executed(self):
        attempt = _memory_attempt()
        self.assertEqual(attempt.manager.wrapper.calls, 0)
        self.assertEqual(attempt.manager.raw.calls, 0)
        await self._assert_pre_call_terminal(
            attempt,
            expected_reason="memory_execution_not_audited",
        )

    async def test_parameter_schema_runtime_and_tool_pre_call_rejections_terminalize(self):
        parameter_attempt = _read_attempt()
        parameter_material = adapters._open_adapter_draft(parameter_attempt.draft)
        object.__setattr__(parameter_material.parameter_record, "limit", 0)
        await self._assert_pre_call_terminal(
            parameter_attempt,
            expected_reason="parameter_record_drift",
        )

        runtime_attempt = _read_attempt()
        runtime_attempt.tool.parameters["additionalProperties"] = True
        await self._assert_pre_call_terminal(
            runtime_attempt,
            expected_reason="runtime_exact_object_drift",
        )

        schema_attempt = _read_attempt()
        with mock.patch.object(
            executor_mod,
            "_validate_exact_schema",
            side_effect=executor_mod.OwnerActionExecutionRejected(
                "tool_schema_mismatch"
            ),
        ):
            await self._assert_pre_call_terminal(
                schema_attempt,
                expected_reason="tool_schema_mismatch",
            )

        tool_attempt = _read_attempt()
        with mock.patch.object(
            executor_mod,
            "_assert_local_tool",
            side_effect=executor_mod.OwnerActionExecutionRejected(
                "runtime_call_missing"
            ),
        ):
            await self._assert_pre_call_terminal(
                tool_attempt,
                expected_reason="runtime_call_missing",
            )

    async def test_direct_send_attempt_is_blocked_without_live_event(self):
        attempt = _read_attempt(mode="send")
        blueprint = await attempt.executor.execute(
            attempt.request,
            attempt.lease,
            attempt.draft,
            attempt.collector,
        )
        completion = _completion(attempt, blueprint)
        self.assertEqual(completion.reason_codes, ("direct_send_forbidden",))
        self.assertIsNone(completion.output)

    async def test_total_deadline_cancels_async_call_and_never_retries(self):
        attempt = _read_attempt(mode="timeout", deadline=111.02, now=110.0)
        attempt.clock.value = 111.0
        blueprint = await attempt.executor.execute(
            attempt.request,
            attempt.lease,
            attempt.draft,
            attempt.collector,
        )
        completion = _completion(attempt, blueprint)
        self.assertEqual(attempt.tool.calls, 1)
        self.assertIs(completion.status, ActionReceiptStatus.TIMED_OUT)
        self.assertIs(completion.effect_state, ActionEffectState.NO_SIDE_EFFECT)
        self.assertEqual(completion.reason_codes, ("execution_deadline_exceeded",))

    async def test_external_cancellation_still_returns_terminalizable_blueprint(self):
        attempt = _read_attempt(mode="timeout")
        task = asyncio.create_task(
            attempt.executor.execute(
                attempt.request,
                attempt.lease,
                attempt.draft,
                attempt.collector,
            )
        )
        while attempt.tool.calls == 0:
            await asyncio.sleep(0)
        task.cancel()

        blueprint = await task
        receipt = attempt.support.controller.complete_execution(
            attempt.lease,
            blueprint,
        )
        self.assertEqual(attempt.tool.calls, 1)
        self.assertEqual(attempt.tool.completed, 0)
        self.assertIs(receipt.status, ActionReceiptStatus.FAILED)
        self.assertIs(receipt.effect_state, ActionEffectState.NO_SIDE_EFFECT)
        self.assertEqual(receipt.attempt_count, 1)
        self.assertEqual(receipt.reason_codes, ("execution_cancelled",))
        self.assertIs(
            attempt.support.controller.inspect_request(attempt.request).state,
            OwnerActionLedgerState.TERMINAL,
        )

    async def test_external_cancellation_preserves_mutation_unknown_and_terminalizes(self):
        attempt = _memory_attempt()
        completion = executor_mod._cancelled_completion(
            attempt.request,
            clock=attempt.clock,
            lower_bound=attempt.lease.issued_at,
        )
        self.assertIs(completion.status, ActionReceiptStatus.EFFECT_UNKNOWN)
        self.assertIs(completion.effect_state, ActionEffectState.UNKNOWN)
        self.assertEqual(completion.attempt_count, 1)
        self.assertEqual(completion.reason_codes, ("execution_cancelled",))

        blueprint = executor_mod._issue_blueprint(
            executor=attempt.executor,
            request=attempt.request,
            lease=attempt.lease,
            draft=attempt.draft,
            collector=attempt.collector,
            completion=completion,
            tool_invoked=True,
        )
        receipt = attempt.support.controller.complete_execution(
            attempt.lease,
            blueprint,
        )
        self.assertIs(receipt.status, ActionReceiptStatus.EFFECT_UNKNOWN)
        self.assertIs(receipt.effect_state, ActionEffectState.UNKNOWN)
        self.assertEqual(receipt.attempt_count, 1)
        self.assertIs(
            attempt.support.controller.inspect_request(attempt.request).state,
            OwnerActionLedgerState.TERMINAL,
        )

    async def test_zero_and_multiple_results_fail_closed(self):
        for result in ([], [b"one", b"two"]):
            with self.subTest(result_count=len(result)):
                attempt = _read_attempt(result=result)
                blueprint = await attempt.executor.execute(
                    attempt.request,
                    attempt.lease,
                    attempt.draft,
                    attempt.collector,
                )
                completion = _completion(attempt, blueprint)
                self.assertEqual(
                    completion.reason_codes,
                    ("result_cardinality_invalid",),
                )
                self.assertIsNone(completion.output)

    async def test_call_exception_does_not_leak_raw_error_and_is_not_retried(self):
        attempt = _read_attempt(mode="error")
        blueprint = await attempt.executor.execute(
            attempt.request,
            attempt.lease,
            attempt.draft,
            attempt.collector,
        )
        completion = _completion(attempt, blueprint)
        self.assertEqual(attempt.tool.calls, 1)
        self.assertEqual(completion.reason_codes, ("runtime_call_failed",))
        diagnostic = repr(blueprint) + repr(completion) + str(blueprint.trace_metadata())
        self.assertNotIn(attempt.tool.secret, diagnostic)

    async def test_request_lease_draft_collector_copy_cross_and_replay_fail_closed(self):
        first = _read_attempt()
        second = _read_attempt(result=b"second")
        cases = (
            (copy.copy(first.request), first.lease, first.draft, first.collector),
            (first.request, copy.copy(first.lease), first.draft, first.collector),
            (first.request, first.lease, copy.copy(first.draft), first.collector),
            (first.request, first.lease, second.draft, first.collector),
            (first.request, first.lease, first.draft, second.collector),
        )
        for values in cases:
            with self.subTest(case=tuple(type(value).__name__ for value in values)):
                with self.assertRaises(executor_mod.OwnerActionExecutionRejected):
                    await first.executor.execute(*values)

        blueprint = await first.executor.execute(
            first.request,
            first.lease,
            first.draft,
            first.collector,
        )
        with self.assertRaises(executor_mod.OwnerActionExecutionRejected):
            await first.executor.execute(
                first.request,
                first.lease,
                first.draft,
                first.collector,
            )
        _completion(first, blueprint)
        with self.assertRaisesRegex(
            executor_mod.OwnerActionExecutionRejected,
            "blueprint_replayed",
        ):
            _completion(first, blueprint)

    async def test_stale_binding_and_epoch_cannot_cross_attempt_lineage(self):
        first = _read_attempt()
        second = _read_attempt()
        with self.assertRaisesRegex(
            executor_mod.OwnerActionExecutionRejected,
            "execution_lineage_mismatch|controller_attempt_not_canonical",
        ):
            await first.executor.execute(
                first.request,
                second.lease,
                first.draft,
                first.collector,
            )

    async def test_blueprint_constructor_copy_tamper_and_cross_open_fail_closed(self):
        with self.assertRaisesRegex(
            executor_mod.OwnerActionExecutionRejected,
            "blueprint_issuer_seal_invalid",
        ):
            executor_mod.OwnerExecutionBlueprint(
                operation=OwnerActionOperation.ARTIFACT_READ_EXACT,
                attempt_started=True,
                tool_invoked=False,
                has_safe_output=False,
            )
        attempt = _read_attempt(result=b"raw-secret-should-never-appear")
        blueprint = await attempt.executor.execute(
            attempt.request,
            attempt.lease,
            attempt.draft,
            attempt.collector,
        )
        with self.assertRaisesRegex(
            executor_mod.OwnerActionExecutionRejected,
            "blueprint_copy_forbidden",
        ):
            copy.copy(blueprint)
        other = _read_attempt()
        with self.assertRaisesRegex(
            executor_mod.OwnerActionExecutionRejected,
            "blueprint_lineage_mismatch",
        ):
            executor_mod._open_execution_blueprint(
                blueprint,
                controller=attempt.support.controller,
                request=other.request,
                lease=other.lease,
            )
        _completion(attempt, blueprint)
        diagnostic = repr(blueprint) + str(blueprint.trace_metadata())
        self.assertNotIn("raw-secret-should-never-appear", diagnostic)
        object.__setattr__(blueprint, "operation", "raw-secret-tamper")
        self.assertEqual(
            repr(blueprint),
            "OwnerExecutionBlueprint(canonical=False, authority_hidden=True, output_hidden=True)",
        )
        with self.assertRaisesRegex(
            executor_mod.OwnerActionExecutionRejected,
            "blueprint_not_canonical",
        ):
            blueprint.trace_metadata()

    async def test_cross_controller_rejection_does_not_consume_blueprint(self):
        first = _read_attempt()
        second = _read_attempt(result=b"second")
        blueprint = await first.executor.execute(
            first.request,
            first.lease,
            first.draft,
            first.collector,
        )
        with self.assertRaisesRegex(ContractViolation, "blueprint_lineage_mismatch"):
            second.support.controller.complete_execution(second.lease, blueprint)
        receipt = first.support.controller.complete_execution(first.lease, blueprint)
        self.assertIs(receipt.status, ActionReceiptStatus.FAILED)
        self.assertEqual(receipt.attempt_count, 1)

    async def test_concurrent_blueprint_completion_has_one_terminal_winner(self):
        attempt = _read_attempt()
        blueprint = await attempt.executor.execute(
            attempt.request,
            attempt.lease,
            attempt.draft,
            attempt.collector,
        )

        def complete_once():
            try:
                return attempt.support.controller.complete_execution(
                    attempt.lease,
                    blueprint,
                )
            except ContractViolation as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = tuple(pool.map(lambda _: complete_once(), range(8)))
        receipts = tuple(value for value in outcomes if not isinstance(value, str))
        failures = tuple(value for value in outcomes if isinstance(value, str))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(failures), 7)
        self.assertTrue(
            all(value == "execution_lease_consumed" for value in failures)
        )

    def test_exact_schema_validator_checks_type_minimum_enum_and_pattern(self):
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "mode": {"type": "string", "enum": ["safe"]},
                "name": {"type": "string", "pattern": "^[a-z]+$"},
            },
            "required": ["count", "mode", "name"],
            "additionalProperties": False,
        }
        self.assertTrue(
            executor_mod._validate_schema_node(
                schema,
                {"count": 2, "mode": "safe", "name": "alpha"},
            )
        )
        for invalid in (
            {"count": True, "mode": "safe", "name": "alpha"},
            {"count": 0, "mode": "safe", "name": "alpha"},
            {"count": 2, "mode": "unsafe", "name": "alpha"},
            {"count": 2, "mode": "safe", "name": "A1"},
            {"count": 2, "mode": "safe", "name": "alpha", "extra": 1},
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(executor_mod._validate_schema_node(schema, invalid))

    def test_mutation_timeout_and_exception_are_unknown_and_non_retryable(self):
        request = types.SimpleNamespace(
            request_digest="a" * 64,
            operation=OwnerActionOperation.MEMORY_WRITE_LITERAL,
            side_effect=ActionSideEffect.STATE_WRITE,
        )
        for reason, timed_out in (("mutation_timeout", True), ("mutation_error", False)):
            with self.subTest(reason=reason):
                completion = executor_mod._failure_completion(
                    request,
                    reason_code=reason,
                    completed_at=12.0,
                    timed_out=timed_out,
                    uncertain_mutation=True,
                )
                self.assertIs(completion.status, ActionReceiptStatus.EFFECT_UNKNOWN)
                self.assertIs(completion.effect_state, ActionEffectState.UNKNOWN)
                self.assertEqual(completion.attempt_count, 1)

    def test_handoff_mcp_background_and_plugin_hook_are_closed(self):
        async def call(_self, _context, **_arguments):
            return b"unused"

        cases = (
            (
                type("HandoffTool", (), {"call": call})(),
                "handoff_forbidden",
            ),
            (
                type("MCPTool", (), {"call": call})(),
                "mcp_forbidden",
            ),
            (
                types.SimpleNamespace(
                    call=call,
                    is_background_task=True,
                    handler=None,
                ),
                "background_forbidden",
            ),
            (
                types.SimpleNamespace(
                    call=call,
                    is_background_task=False,
                    handler=object(),
                ),
                "plugin_hook_forbidden",
            ),
        )
        for tool, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                executor_mod.OwnerActionExecutionRejected,
                reason,
            ):
                executor_mod._assert_local_tool(tool)

    def test_public_action_output_is_not_safe_output_authority(self):
        attempt = _read_attempt()
        public_output = ActionOutput.from_safe_text(
            binding=attempt.request.binding,
            request_digest=attempt.request.request_digest,
            text="caller supplied text",
        )
        completion = executor_mod._ExecutionCompletion(
            kind=executor_mod._CompletionKind.EXECUTED,
            status=ActionReceiptStatus.SUCCEEDED,
            effect_state=ActionEffectState.NO_SIDE_EFFECT,
            attempt_count=1,
            result_digest="e" * 64,
            completed_at=12.0,
            output=public_output,
        )
        with self.assertRaisesRegex(
            executor_mod.OwnerActionExecutionRejected,
            "artifact_safe_output_authority_unavailable",
        ):
            executor_mod._issue_blueprint(
                executor=attempt.executor,
                request=attempt.request,
                lease=attempt.lease,
                draft=attempt.draft,
                collector=attempt.collector,
                completion=completion,
                tool_invoked=True,
            )

    def test_safe_artifact_output_cannot_impersonate_action_output_authority(self):
        artifact_output = _guard("guarded artifact text")
        self.assertTrue(artifact_output.is_canonical)
        with self.assertRaisesRegex(
            executor_mod.OwnerActionExecutionRejected,
            "completion_output_invalid",
        ):
            executor_mod._ExecutionCompletion(
                kind=executor_mod._CompletionKind.EXECUTED,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                attempt_count=1,
                result_digest="e" * 64,
                completed_at=12.0,
                output=artifact_output,  # type: ignore[arg-type]
            )

    def test_public_surface_has_no_hook_event_direct_send_or_raw_completion_fields(self):
        parameters = tuple(inspect.signature(executor_mod.OwnerActionExecutor.execute).parameters)
        self.assertEqual(
            parameters,
            ("self", "request", "lease", "draft", "collector"),
        )
        forbidden = {
            "hook",
            "event",
            "plugin_context",
            "run_context",
            "status",
            "effect_state",
            "result_digest",
            "output",
        }
        self.assertTrue(forbidden.isdisjoint(parameters))
        self.assertEqual(
            set(executor_mod.__all__),
            {
                "OwnerActionExecutionRejected",
                "OwnerActionExecutor",
                "OwnerExecutionBlueprint",
            },
        )

    def test_controller_completion_has_no_raw_authority_and_executor_has_no_ledger_access(self):
        parameters = tuple(
            inspect.signature(OwnerActionController.complete_execution).parameters
        )
        self.assertEqual(parameters, ("self", "lease", "blueprint"))
        source = inspect.getsource(executor_mod._open_exact_controller_attempt)
        for forbidden in ("controller._lock", "controller._ledger", "controller._leases"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
