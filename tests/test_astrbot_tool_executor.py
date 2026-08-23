from __future__ import annotations

import asyncio
import dataclasses
import unittest
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_shio.core.action_planner import PlannedAction, StructuralOutcome
from astrbot_plugin_shio.core.astrbot_tool_executor import (
    SealedExecutionStatus,
    execute_sealed_acquisition,
)
from astrbot_plugin_shio.core.capability_policy import (
    CapabilityClass,
    build_guest_capability_policy,
    classify_tool,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    DecisionBinding,
    KnowledgeGapDecision,
    KnowledgeNeed,
)
from astrbot_plugin_shio.core.grounding_adapter import (
    EvidenceOutcomeKind,
    adapt_grounding_evidence,
)
from astrbot_plugin_shio.core.identity import PrincipalContext
from astrbot_plugin_shio.core.tool_broker import (
    AcquisitionKind,
    AcquisitionRequest,
    broker_tool_request,
    build_batch_request_shape,
    build_extract_request_shape,
    build_search_request_shape,
)
from astrbot_plugin_shio.core.tool_result import adapt_tool_call_results


ANYSEARCH_SOURCE = "data.plugins.astrbot_plugin_anysearch.main"


def _binding(*, epoch: int = 7) -> DecisionBinding:
    scope = "platform:test|bot:shio|group:sealed-tool"
    return DecisionBinding(
        scope_key=scope,
        session_id="session-private",
        current_message_id="message-private",
        current_sender_key=f"{scope}|user:peer-a",
        current_content_digest="a" * 64,
        conversation_revision=3,
        generation_epoch=epoch,
        trace_id="b" * 32,
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


def _request(
    tool: Any,
    *,
    now: float = 100.0,
    kind: AcquisitionKind = AcquisitionKind.SEARCH,
) -> AcquisitionRequest:
    binding = _binding()
    principal = PrincipalContext(
        sender_key=binding.current_sender_key,
        sender_id="peer-a",
        is_owner=False,
        relationship_role="group_peer",
        verification_source="astrbot_event_sender_id:owner_allowlist_miss",
    )
    policy = build_guest_capability_policy(
        principal,
        configured_tool_names=(tool.name,),
    )
    action = PlannedAction(
        action=ActionDecision(
            binding=binding,
            kind=ActionKind.USE_TOOL,
            reply_target=_target(binding),
            capability_intent=CapabilityClass.PUBLIC_WEB_READ.value,
            reason_codes=("fixture_explicit_evidence",),
        ),
        structural_outcome=StructuralOutcome.CONTINUE,
        planner_reason_codes=("fixture_explicit_evidence",),
    )
    gap = KnowledgeGapDecision(
        binding=binding,
        need=KnowledgeNeed.UNKNOWN_TERM,
        requires_evidence=True,
        requested_capability=CapabilityClass.PUBLIC_WEB_READ,
        max_tool_calls=1,
    )
    if kind is AcquisitionKind.SEARCH:
        request_shape = build_search_request_shape(
            binding,
            query="这个合成术语是什么意思",
        )
    elif kind is AcquisitionKind.EXTRACT:
        request_shape = build_extract_request_shape(
            binding,
            url="https://example.invalid/public-page",
        )
    else:
        request_shape = build_batch_request_shape(
            binding,
            queries=("公开术语甲", "公开术语乙"),
        )
    return broker_tool_request(
        planned_action=action,
        knowledge_gap=gap,
        capability_policy=policy,
        runtime_tools=(classify_tool(tool),),
        configured_tool_names=(tool.name,),
        request_shape=request_shape,
        now=now,
    )


def _schema(*, query_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": query_schema or {"type": "string"},
        },
        "required": ["query"],
    }


class RuntimeTool:
    def __init__(
        self,
        *,
        name: str = "anysearch_search",
        parameters: dict[str, Any] | None = None,
        module: str = ANYSEARCH_SOURCE,
        active: bool = True,
        background: bool = False,
    ) -> None:
        self.name = name
        self.description = "Public web search"
        self.parameters = parameters or _schema()
        self.metadata: dict[str, Any] = {}
        self.handler_module_path = module
        self.origin = module
        self.active = active
        self.is_background_task = background


class PermissionGuardedTool(RuntimeTool):
    def __init__(self) -> None:
        super().__init__()
        self._wrapped = object()


class HandoffTool(RuntimeTool):
    pass


class MCPTool(RuntimeTool):
    pass


class TextContent:
    def __init__(self, text: str) -> None:
        self.text = text


class CallToolResult:
    def __init__(
        self,
        text: str = "公开来源给出的可验证解释",
        *,
        is_error: bool = False,
        content: list[Any] | None = None,
    ) -> None:
        self.content = [TextContent(text)] if content is None else content
        self.isError = is_error


class LiveEvent:
    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.result: Any = "live-result-must-not-change"
        self.extras: dict[str, Any] = {}

    async def send(self, value: Any) -> None:
        self.sent.append(value)

    def get_result(self) -> Any:
        return self.result

    def set_result(self, value: Any) -> None:
        self.result = value

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value

    def is_stopped(self) -> bool:
        return False


def _strict_schema_validator(schema: dict[str, Any], instance: Any) -> None:
    def visit(node: dict[str, Any], value: Any) -> None:
        expected_type = node.get("type")
        if expected_type == "object":
            if not isinstance(value, dict):
                raise ValueError("object_required")
            properties = node.get("properties", {})
            for required in node.get("required", []):
                if required not in value:
                    raise ValueError("required_missing")
            if node.get("additionalProperties") is False:
                if set(value).difference(properties):
                    raise ValueError("additional_property")
            for key, child in value.items():
                if key in properties:
                    visit(properties[key], child)
        elif expected_type == "array":
            if not isinstance(value, list):
                raise ValueError("array_required")
            item_schema = node.get("items", {})
            for item in value:
                visit(item_schema, item)
        elif expected_type == "string" and not isinstance(value, str):
            raise ValueError("string_required")
        elif expected_type == "integer" and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise ValueError("integer_required")
        elif expected_type == "number" and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise ValueError("number_required")
        elif expected_type == "boolean" and not isinstance(value, bool):
            raise ValueError("boolean_required")
        if "enum" in node and value not in node["enum"]:
            raise ValueError("enum_mismatch")

    visit(schema, instance)


def _executor_for(values: list[Any], capture: dict[str, Any]):
    async def executor(*, tool: Any, run_context: Any, **kwargs: Any):
        capture["tool"] = tool
        capture["run_context"] = run_context
        capture["arguments"] = kwargs
        capture["calls"] = capture.get("calls", 0) + 1
        for value in values:
            yield value

    return executor


async def _noop_hook(run_context: Any, tool: Any, arguments: dict[str, Any]) -> None:
    del run_context, tool, arguments


class SealedAstrBotToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def _execute(
        self,
        request: AcquisitionRequest,
        runtime_tools: Any,
        *,
        executor: Any,
        hook: Any = _noop_hook,
        clock: Any = lambda: 101.0,
        event_stopped: Any = lambda: False,
        epoch_current: Any = lambda _binding: True,
        schema_validator: Any = _strict_schema_validator,
    ):
        return await execute_sealed_acquisition(
            request=request,
            runtime_tools=runtime_tools,
            run_context=SimpleNamespace(context="test-context"),
            executor=executor,
            hook=hook,
            clock=clock,
            event_stopped=event_stopped,
            epoch_current=epoch_current,
            schema_validator=schema_validator,
        )

    async def test_success_keeps_exact_permission_wrapper_and_feeds_grounding(self):
        tool = PermissionGuardedTool()
        request = _request(tool)
        capture: dict[str, Any] = {}

        outcome = await self._execute(
            request,
            SimpleNamespace(tools=[tool]),
            executor=_executor_for([CallToolResult()], capture),
        )

        self.assertIs(outcome.status, SealedExecutionStatus.SUCCESS)
        self.assertTrue(outcome.executed)
        self.assertIsNotNone(outcome.batch)
        self.assertIs(capture["tool"], tool)
        self.assertEqual(capture["arguments"], request.materialize_arguments())

        typed = adapt_tool_call_results(
            outcome.batch,
            tools=(tool,),
            scope_key=request.binding.scope_key,
            target_sender_key=request.binding.current_sender_key,
            acquisition_request=request,
            observed_at=101.0,
        )
        grounding = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=101.0,
        )
        self.assertIs(grounding.kind, EvidenceOutcomeKind.ACCEPTED)
        self.assertEqual(len(grounding.facts), 1)

    async def test_extract_and_batch_paths_use_only_their_brokered_shapes(self):
        cases = (
            (
                AcquisitionKind.EXTRACT,
                RuntimeTool(
                    name="anysearch_extract",
                    parameters={
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                ),
                {"url": "https://example.invalid/public-page"},
            ),
            (
                AcquisitionKind.BATCH_SEARCH,
                RuntimeTool(
                    name="anysearch_batch_search",
                    parameters={
                        "type": "object",
                        "properties": {
                            "queries": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                    "required": ["query"],
                                },
                            }
                        },
                        "required": ["queries"],
                    },
                ),
                {
                    "queries": [
                        {"query": "公开术语甲"},
                        {"query": "公开术语乙"},
                    ]
                },
            ),
        )
        for kind, tool, expected in cases:
            request = _request(tool, kind=kind)
            capture: dict[str, Any] = {}
            with self.subTest(kind=kind.value):
                outcome = await self._execute(
                    request,
                    (tool,),
                    executor=_executor_for([CallToolResult()], capture),
                )
                self.assertIs(outcome.status, SealedExecutionStatus.SUCCESS)
                self.assertEqual(capture["arguments"], expected)

    async def test_hook_only_receives_copy_and_arguments_are_rematerialized(self):
        tool = PermissionGuardedTool()
        request = _request(tool)
        capture: dict[str, Any] = {}
        hook_seen: dict[str, Any] = {}

        async def mutating_hook(_context, hook_tool, arguments):
            hook_seen["tool"] = hook_tool
            hook_seen["arguments"] = arguments
            arguments["query"] = "hook-tampered-secret"
            arguments["extra"] = "must-not-reach-executor"

        outcome = await self._execute(
            request,
            (tool,),
            executor=_executor_for([CallToolResult()], capture),
            hook=mutating_hook,
        )

        self.assertIs(outcome.status, SealedExecutionStatus.SUCCESS)
        self.assertIs(hook_seen["tool"], tool)
        self.assertIsNot(hook_seen["arguments"], capture["arguments"])
        self.assertEqual(capture["arguments"], request.materialize_arguments())
        self.assertNotIn("hook-tampered-secret", repr(outcome))

    async def test_tool_cannot_send_or_mutate_live_event_before_result_check(self):
        tool = PermissionGuardedTool()
        request = _request(tool)
        live_event = LiveEvent()
        run_context = SimpleNamespace(
            context=SimpleNamespace(context=object(), event=live_event, extra={}),
            messages=[],
            tool_call_timeout=20,
        )

        async def direct_sender(*, tool, run_context, **kwargs):
            del tool, kwargs
            isolated_event = run_context.context.event
            self.assertIsNone(run_context.context.context)
            self.assertIsNot(isolated_event, live_event)
            with self.assertRaises(AttributeError):
                getattr(isolated_event, "_event")
            isolated_event.set_result("tool-owned-result")
            isolated_event.set_extra("tool-owned-extra", "must-not-survive")
            await isolated_event.send("must-not-send")
            yield CallToolResult()  # pragma: no cover

        outcome = await execute_sealed_acquisition(
            request=request,
            runtime_tools=(tool,),
            run_context=run_context,
            executor=direct_sender,
            hook=None,
            clock=lambda: 101.0,
            event_stopped=lambda: False,
            epoch_current=lambda _binding: True,
            schema_validator=_strict_schema_validator,
        )

        self.assertIs(outcome.status, SealedExecutionStatus.DIRECT_SEND_FORBIDDEN)
        self.assertEqual(live_event.sent, [])
        self.assertEqual(live_event.result, "live-result-must-not-change")
        self.assertNotIn("tool-owned-extra", live_event.extras)
        self.assertIsNone(outcome.batch)

    async def test_hook_cannot_send_through_live_event(self):
        tool = PermissionGuardedTool()
        request = _request(tool)
        live_event = LiveEvent()
        run_context = SimpleNamespace(
            context=SimpleNamespace(context=object(), event=live_event, extra={}),
            messages=[],
            tool_call_timeout=20,
        )
        capture: dict[str, Any] = {}

        async def sending_hook(context, _tool, _arguments):
            await context.context.event.send("hook-must-not-send")

        outcome = await execute_sealed_acquisition(
            request=request,
            runtime_tools=(tool,),
            run_context=run_context,
            executor=_executor_for([CallToolResult()], capture),
            hook=sending_hook,
            clock=lambda: 101.0,
            event_stopped=lambda: False,
            epoch_current=lambda _binding: True,
            schema_validator=_strict_schema_validator,
        )

        self.assertIs(outcome.status, SealedExecutionStatus.DIRECT_SEND_FORBIDDEN)
        self.assertEqual(live_event.sent, [])
        self.assertNotIn("calls", capture)
        self.assertIsNone(outcome.batch)

    async def test_caught_send_exception_still_invalidates_hook_and_tool(self):
        tool = PermissionGuardedTool()
        request = _request(tool)
        live_event = LiveEvent()

        def run_context():
            return SimpleNamespace(
                context=SimpleNamespace(context=object(), event=live_event, extra={}),
                messages=[],
                tool_call_timeout=20,
            )

        async def catching_hook(context, _tool, _arguments):
            try:
                await context.context.event.send("caught-hook-send")
            except Exception:
                pass

        hook_capture: dict[str, Any] = {}
        hook_outcome = await execute_sealed_acquisition(
            request=request,
            runtime_tools=(tool,),
            run_context=run_context(),
            executor=_executor_for([CallToolResult()], hook_capture),
            hook=catching_hook,
            clock=lambda: 101.0,
            event_stopped=lambda: False,
            epoch_current=lambda _binding: True,
            schema_validator=_strict_schema_validator,
        )
        self.assertIs(
            hook_outcome.status,
            SealedExecutionStatus.DIRECT_SEND_FORBIDDEN,
        )
        self.assertNotIn("calls", hook_capture)

        async def catching_tool(*, tool, run_context, **kwargs):
            del tool, kwargs
            try:
                await run_context.context.event.send("caught-tool-send")
            except Exception:
                pass
            yield CallToolResult()

        tool_outcome = await execute_sealed_acquisition(
            request=request,
            runtime_tools=(tool,),
            run_context=run_context(),
            executor=catching_tool,
            hook=None,
            clock=lambda: 101.0,
            event_stopped=lambda: False,
            epoch_current=lambda _binding: True,
            schema_validator=_strict_schema_validator,
        )
        self.assertIs(
            tool_outcome.status,
            SealedExecutionStatus.DIRECT_SEND_FORBIDDEN,
        )
        self.assertEqual(live_event.sent, [])
        self.assertIsNone(tool_outcome.batch)

    async def test_duplicate_exact_runtime_name_executes_nothing(self):
        tool = RuntimeTool()
        request = _request(tool)
        capture: dict[str, Any] = {}

        outcome = await self._execute(
            request,
            (tool, RuntimeTool()),
            executor=_executor_for([CallToolResult()], capture),
        )

        self.assertIs(outcome.status, SealedExecutionStatus.TOOL_AMBIGUOUS)
        self.assertFalse(outcome.executed)
        self.assertNotIn("calls", capture)
        self.assertIsNone(outcome.batch)

    async def test_non_allowlisted_name_and_wrong_source_are_rejected(self):
        tool = RuntimeTool()
        request = _request(tool)
        capture: dict[str, Any] = {}
        mutations = (
            (
                dataclasses.replace(
                    request,
                    selection=dataclasses.replace(
                        request.selection,
                        tool_name="web_search",
                    ),
                ),
                (tool,),
                SealedExecutionStatus.REQUEST_NOT_ALLOWED,
            ),
            (
                dataclasses.replace(
                    request,
                    selection=dataclasses.replace(
                        request.selection,
                        source="unattested.clone",
                    ),
                ),
                (tool,),
                SealedExecutionStatus.SOURCE_UNATTESTED,
            ),
            (
                request,
                (RuntimeTool(module="data.plugins.untrusted.main"),),
                SealedExecutionStatus.SOURCE_UNATTESTED,
            ),
        )
        for mutated, tools, expected in mutations:
            with self.subTest(expected=expected.value):
                outcome = await self._execute(
                    mutated,
                    tools,
                    executor=_executor_for([CallToolResult()], capture),
                )
                self.assertIs(outcome.status, expected)
                self.assertFalse(outcome.executed)
                self.assertIsNone(outcome.batch)
        self.assertNotIn("calls", capture)

    async def test_handoff_background_and_mcp_are_rejected_before_execution(self):
        ordinary = RuntimeTool()
        request = _request(ordinary)
        capture: dict[str, Any] = {}
        cases = (
            (HandoffTool(), SealedExecutionStatus.HANDOFF_FORBIDDEN),
            (
                RuntimeTool(background=True),
                SealedExecutionStatus.BACKGROUND_FORBIDDEN,
            ),
            (MCPTool(), SealedExecutionStatus.MCP_FORBIDDEN),
        )
        for tool, expected in cases:
            with self.subTest(expected=expected.value):
                outcome = await self._execute(
                    request,
                    (tool,),
                    executor=_executor_for([CallToolResult()], capture),
                )
                self.assertIs(outcome.status, expected)
                self.assertFalse(outcome.executed)
                self.assertIsNone(outcome.batch)
        self.assertNotIn("calls", capture)

    async def test_schema_missing_required_wrong_type_enum_and_shape_fail_closed(self):
        valid_tool = RuntimeTool()
        request = _request(valid_tool)
        capture: dict[str, Any] = {}
        cases = (
            (
                RuntimeTool(parameters={"type": "object", "properties": {}}),
                SealedExecutionStatus.SCHEMA_INVALID,
            ),
            (
                RuntimeTool(
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "extra": {"type": "string"},
                        },
                        "required": ["query", "extra"],
                    }
                ),
                SealedExecutionStatus.ARGUMENTS_INVALID,
            ),
            (
                RuntimeTool(parameters=_schema(query_schema={"type": "integer"})),
                SealedExecutionStatus.ARGUMENTS_INVALID,
            ),
            (
                RuntimeTool(
                    parameters=_schema(
                        query_schema={"type": "string", "enum": ["别的查询"]}
                    )
                ),
                SealedExecutionStatus.ARGUMENTS_INVALID,
            ),
        )
        for tool, expected in cases:
            with self.subTest(expected=expected.value):
                outcome = await self._execute(
                    request,
                    (tool,),
                    executor=_executor_for([CallToolResult()], capture),
                )
                self.assertIs(outcome.status, expected)
                self.assertFalse(outcome.executed)
                self.assertIsNone(outcome.batch)
        self.assertNotIn("calls", capture)

    async def test_schema_is_hardened_to_reject_additional_properties(self):
        tool = RuntimeTool()
        request = _request(tool)
        capture: dict[str, Any] = {}
        schemas: list[dict[str, Any]] = []

        def observing_validator(schema, instance):
            schemas.append(schema)
            self.assertIs(schema["additionalProperties"], False)
            _strict_schema_validator(schema, instance)

        outcome = await self._execute(
            request,
            (tool,),
            executor=_executor_for([CallToolResult()], capture),
            schema_validator=observing_validator,
        )

        self.assertIs(outcome.status, SealedExecutionStatus.SUCCESS)
        self.assertGreaterEqual(len(schemas), 2)

    async def test_tampered_request_shape_never_reaches_schema_or_executor(self):
        tool = RuntimeTool()
        request = _request(tool)
        tampered = dataclasses.replace(
            request,
            argument_items=(
                ("query", "这个合成术语是什么意思"),
                ("extra", "authority-smuggling"),
            ),
        )
        capture: dict[str, Any] = {}
        validator_calls = 0

        def validator(schema, instance):
            nonlocal validator_calls
            validator_calls += 1
            _strict_schema_validator(schema, instance)

        outcome = await self._execute(
            tampered,
            (tool,),
            executor=_executor_for([CallToolResult()], capture),
            schema_validator=validator,
        )

        self.assertIs(outcome.status, SealedExecutionStatus.REQUEST_TAMPERED)
        self.assertEqual(validator_calls, 0)
        self.assertNotIn("calls", capture)

    async def test_initial_event_stop_or_stale_epoch_is_zero_hook_zero_execution(self):
        tool = RuntimeTool()
        request = _request(tool)
        for stopped, current, expected in (
            (True, True, SealedExecutionStatus.EVENT_STOPPED),
            (False, False, SealedExecutionStatus.STALE_EPOCH),
        ):
            capture: dict[str, Any] = {}
            hook_calls = 0

            async def hook(*_args):
                nonlocal hook_calls
                hook_calls += 1

            with self.subTest(expected=expected.value):
                outcome = await self._execute(
                    request,
                    (tool,),
                    executor=_executor_for([CallToolResult()], capture),
                    hook=hook,
                    event_stopped=lambda: stopped,
                    epoch_current=lambda _binding: current,
                )
                self.assertIs(outcome.status, expected)
                self.assertEqual(hook_calls, 0)
                self.assertNotIn("calls", capture)

    async def test_hook_stop_and_hook_epoch_change_are_zero_execution(self):
        tool = RuntimeTool()
        request = _request(tool)
        cases = ("stop", "stale")
        for case in cases:
            state = {"stopped": False, "current": True}
            capture: dict[str, Any] = {}

            async def hook(*_args):
                if case == "stop":
                    state["stopped"] = True
                else:
                    state["current"] = False

            with self.subTest(case=case):
                outcome = await self._execute(
                    request,
                    (tool,),
                    executor=_executor_for([CallToolResult()], capture),
                    hook=hook,
                    event_stopped=lambda: state["stopped"],
                    epoch_current=lambda _binding: state["current"],
                )
                self.assertIs(
                    outcome.status,
                    SealedExecutionStatus.EVENT_STOPPED
                    if case == "stop"
                    else SealedExecutionStatus.STALE_EPOCH,
                )
                self.assertFalse(outcome.executed)
                self.assertNotIn("calls", capture)

    async def test_deadline_expired_and_outer_timeout_produce_no_batch(self):
        tool = RuntimeTool()
        request = _request(tool, now=0.0)

        expired_capture: dict[str, Any] = {}
        expired = await self._execute(
            request,
            (tool,),
            executor=_executor_for([CallToolResult()], expired_capture),
            clock=lambda: 21.0,
        )
        self.assertIs(expired.status, SealedExecutionStatus.DEADLINE_EXPIRED)
        self.assertNotIn("calls", expired_capture)
        self.assertIsNone(expired.batch)

        timeout_capture: dict[str, Any] = {}

        async def slow_executor(*, tool, run_context, **kwargs):
            del tool, run_context, kwargs
            timeout_capture["calls"] = timeout_capture.get("calls", 0) + 1
            await asyncio.sleep(0.05)
            yield CallToolResult()

        timed_out = await self._execute(
            request,
            (tool,),
            executor=slow_executor,
            clock=lambda: 19.995,
        )
        self.assertIs(timed_out.status, SealedExecutionStatus.EXECUTION_TIMEOUT)
        self.assertEqual(timeout_capture["calls"], 1)
        self.assertIsNone(timed_out.batch)

    async def test_none_multiple_error_empty_and_unsupported_results_never_make_batch(self):
        tool = RuntimeTool()
        request = _request(tool)
        cases = (
            ([None], SealedExecutionStatus.DIRECT_SEND_FORBIDDEN),
            (
                [CallToolResult("one"), CallToolResult("two")],
                SealedExecutionStatus.RESULT_MULTIPLE,
            ),
            ([CallToolResult(is_error=True)], SealedExecutionStatus.RESULT_ERROR),
            (
                [CallToolResult(content=[])],
                SealedExecutionStatus.RESULT_EMPTY,
            ),
            (["raw-result"], SealedExecutionStatus.RESULT_INVALID),
        )
        for values, expected in cases:
            capture: dict[str, Any] = {}
            with self.subTest(expected=expected.value):
                outcome = await self._execute(
                    request,
                    (tool,),
                    executor=_executor_for(values, capture),
                )
                self.assertIs(outcome.status, expected)
                self.assertTrue(outcome.executed)
                self.assertIsNone(outcome.batch)

    async def test_executor_exception_produces_no_batch(self):
        tool = RuntimeTool()
        request = _request(tool)

        async def failing_executor(*, tool, run_context, **kwargs):
            del tool, run_context, kwargs
            raise RuntimeError("secret-provider-failure")
            yield  # pragma: no cover

        outcome = await self._execute(
            request,
            (tool,),
            executor=failing_executor,
        )

        self.assertIs(outcome.status, SealedExecutionStatus.EXECUTION_FAILED)
        self.assertTrue(outcome.executed)
        self.assertIsNone(outcome.batch)
        self.assertNotIn("secret-provider-failure", repr(outcome))

    async def test_repr_trace_and_batch_repr_hide_arguments_content_and_ids(self):
        tool = RuntimeTool()
        request = _request(tool)
        secret_content = "private-result-body"
        capture: dict[str, Any] = {}

        outcome = await self._execute(
            request,
            (tool,),
            executor=_executor_for([CallToolResult(secret_content)], capture),
        )
        rendered = (
            repr(outcome)
            + repr(outcome.trace_metadata())
            + repr(outcome.batch)
            + repr(outcome.batch.tool_calls_info)
            + repr(outcome.batch.tool_calls_info.tool_calls[0])
            + repr(outcome.batch.tool_calls_result[0])
        )

        for forbidden in (
            "这个合成术语是什么意思",
            secret_content,
            request.binding.scope_key,
            request.binding.current_sender_key,
            request.binding.current_message_id,
            request.action_id,
            outcome.batch.tool_calls_info.tool_calls[0].id,
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn("arguments", rendered.lower())


if __name__ == "__main__":
    unittest.main()
