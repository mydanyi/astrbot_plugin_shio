from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any

from .capability_policy import CapabilityClass, SideEffectClass, classify_tool
from .contracts import DecisionBinding
from .tool_broker import AcquisitionKind, AcquisitionRequest


_ANYSEARCH_NAMES = {
    AcquisitionKind.SEARCH: "anysearch_search",
    AcquisitionKind.EXTRACT: "anysearch_extract",
    AcquisitionKind.BATCH_SEARCH: "anysearch_batch_search",
}
SUPPORTED_SEALED_ACQUISITION_TOOL_NAMES = frozenset(_ANYSEARCH_NAMES.values())
_ANYSEARCH_SOURCE = "astrbot_plugin_anysearch"
_MAX_SCHEMA_CHARS = 100_000


class _DirectSendForbiddenError(RuntimeError):
    pass


class _SealedEventProxy:
    """Read-compatible event view that cannot send or mutate the live result."""

    __slots__ = (
        "_admin",
        "_initially_stopped",
        "_local_extras",
        "_local_result",
        "_locally_stopped",
        "_sender_id",
        "unified_msg_origin",
        "send_attempted",
    )

    def __init__(self, event: Any) -> None:
        admin_reader = getattr(event, "is_admin", None)
        sender_reader = getattr(event, "get_sender_id", None)
        stopped_reader = getattr(event, "is_stopped", None)
        try:
            is_admin = bool(admin_reader()) if callable(admin_reader) else False
        except Exception:
            is_admin = False
        try:
            sender_id = str(sender_reader() or "") if callable(sender_reader) else ""
        except Exception:
            sender_id = ""
        try:
            initially_stopped = (
                bool(stopped_reader()) if callable(stopped_reader) else False
            )
        except Exception:
            initially_stopped = True
        object.__setattr__(self, "_admin", is_admin)
        object.__setattr__(self, "_sender_id", sender_id)
        object.__setattr__(self, "_initially_stopped", initially_stopped)
        object.__setattr__(
            self,
            "unified_msg_origin",
            str(getattr(event, "unified_msg_origin", "") or ""),
        )
        object.__setattr__(self, "_local_extras", {})
        object.__setattr__(self, "_local_result", None)
        object.__setattr__(self, "_locally_stopped", False)
        object.__setattr__(self, "send_attempted", False)

    async def _blocked_send(self, *_args: Any, **_kwargs: Any) -> None:
        object.__setattr__(self, "send_attempted", True)
        raise _DirectSendForbiddenError("sealed_acquisition_direct_send_forbidden")

    async def send(self, *_args: Any, **_kwargs: Any) -> None:
        await self._blocked_send()

    def get_result(self) -> Any:
        return self._local_result

    def set_result(self, result: Any) -> None:
        object.__setattr__(self, "_local_result", result)

    def clear_result(self) -> None:
        object.__setattr__(self, "_local_result", None)

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._local_extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._local_extras[str(key)] = value

    def stop_event(self) -> None:
        object.__setattr__(self, "_locally_stopped", True)

    def is_stopped(self) -> bool:
        return self._locally_stopped or self._initially_stopped

    def is_admin(self) -> bool:
        return self._admin

    def get_sender_id(self) -> str:
        return self._sender_id

    def __getattr__(self, name: str) -> Any:
        if name == "send" or name.startswith("send_"):
            return self._blocked_send
        raise AttributeError(name)

    def __repr__(self) -> str:
        return "SealedEventProxy(send_disabled=True)"


def _sealed_run_context(run_context: Any, event: Any = None) -> Any:
    """Clone the small AstrBot run-context surface with an isolated event."""

    source_agent_context = getattr(run_context, "context", None)
    source_event = (
        event if event is not None else getattr(source_agent_context, "event", None)
    )
    if source_event is None:
        return run_context
    proxy = _SealedEventProxy(source_event)
    sealed_agent_context = SimpleNamespace(
        # Local AnySearch execution and AstrBot's permission wrapper only need
        # the isolated event.  Do not expose the live plugin Context, which can
        # reach platform send APIs through a side channel.
        context=None,
        event=proxy,
        extra=copy.deepcopy(getattr(source_agent_context, "extra", {}) or {}),
    )
    return SimpleNamespace(
        context=sealed_agent_context,
        messages=list(getattr(run_context, "messages", ()) or ()),
        tool_call_timeout=getattr(run_context, "tool_call_timeout", 120),
        _shio_event_proxy=proxy,
    )


class SealedExecutionStatus(str, Enum):
    SUCCESS = "success"
    REQUEST_NOT_ALLOWED = "request_not_allowed"
    REQUEST_TAMPERED = "request_tampered"
    SOURCE_UNATTESTED = "source_unattested"
    TOOL_MISSING = "tool_missing"
    TOOL_AMBIGUOUS = "tool_ambiguous"
    TOOL_INACTIVE = "tool_inactive"
    HANDOFF_FORBIDDEN = "handoff_forbidden"
    BACKGROUND_FORBIDDEN = "background_forbidden"
    MCP_FORBIDDEN = "mcp_forbidden"
    SCHEMA_INVALID = "schema_invalid"
    ARGUMENTS_INVALID = "arguments_invalid"
    EVENT_STOPPED = "event_stopped"
    STALE_EPOCH = "stale_epoch"
    DEADLINE_EXPIRED = "deadline_expired"
    HOOK_FAILED = "hook_failed"
    EXECUTION_TIMEOUT = "execution_timeout"
    EXECUTION_FAILED = "execution_failed"
    DIRECT_SEND_FORBIDDEN = "direct_send_forbidden"
    RESULT_MISSING = "result_missing"
    RESULT_MULTIPLE = "result_multiple"
    RESULT_INVALID = "result_invalid"
    RESULT_ERROR = "result_error"
    RESULT_EMPTY = "result_empty"
    PRODUCTION_ADAPTER_UNAVAILABLE = "production_adapter_unavailable"
    GUARD_FAILED = "guard_failed"


@dataclass(frozen=True, slots=True, repr=False)
class _SealedFunction:
    name: str
    _payload_json: str = field(repr=False)

    @property
    def arguments(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    def __repr__(self) -> str:
        return f"SealedFunction(name={self.name!r}, payload_sealed=True)"


@dataclass(frozen=True, slots=True, repr=False)
class _SealedToolCall:
    _call_id: str = field(repr=False)
    function: _SealedFunction

    @property
    def id(self) -> str:
        return self._call_id

    def __repr__(self) -> str:
        return f"SealedToolCall(name={self.function.name!r}, id_sealed=True)"


@dataclass(frozen=True, slots=True, repr=False)
class _SealedToolCallsInfo:
    tool_calls: tuple[_SealedToolCall, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"SealedToolCallsInfo(count={len(self.tool_calls)})"


@dataclass(frozen=True, slots=True, repr=False)
class _SealedToolResultSegment:
    _call_id: str = field(repr=False)
    _content: tuple[Any, ...] = field(repr=False)
    _is_error: bool = field(default=False, repr=False)

    @property
    def tool_call_id(self) -> str:
        return self._call_id

    @property
    def id(self) -> str:
        return self._call_id

    @property
    def content(self) -> list[Any]:
        return copy.deepcopy(list(self._content))

    @property
    def is_error(self) -> bool:
        return self._is_error

    @property
    def isError(self) -> bool:  # AstrBot/MCP compatibility spelling.
        return self._is_error

    def __repr__(self) -> str:
        return (
            "SealedToolResultSegment("
            f"content_items={len(self._content)}, error={self._is_error!r}, "
            "id_sealed=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SealedToolCallsBatch:
    """Minimal AstrBot-compatible batch with payload-safe diagnostics."""

    tool_calls_info: _SealedToolCallsInfo
    tool_calls_result: tuple[_SealedToolResultSegment, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "SealedToolCallsBatch("
            f"call_count={len(self.tool_calls_info.tool_calls)}, "
            f"result_count={len(self.tool_calls_result)}, payload_sealed=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SealedExecutionOutcome:
    """Fail-closed result of one broker-bound acquisition execution."""

    status: SealedExecutionStatus
    executed: bool
    result_count: int
    batch: SealedToolCallsBatch | None = field(default=None, repr=False)

    @property
    def succeeded(self) -> bool:
        return self.status is SealedExecutionStatus.SUCCESS and self.batch is not None

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "sealed_execution_status": self.status.value,
            "sealed_execution_executed": self.executed,
            "sealed_execution_result_count": self.result_count,
            "sealed_execution_has_batch": self.batch is not None,
        }

    def __repr__(self) -> str:
        return (
            "SealedExecutionOutcome("
            f"status={self.status.value!r}, executed={self.executed!r}, "
            f"result_count={self.result_count}, has_batch={self.batch is not None})"
        )


def _outcome(
    status: SealedExecutionStatus,
    *,
    executed: bool = False,
    result_count: int = 0,
    batch: SealedToolCallsBatch | None = None,
) -> SealedExecutionOutcome:
    return SealedExecutionOutcome(
        status=status,
        executed=executed,
        result_count=max(0, int(result_count)),
        batch=batch if status is SealedExecutionStatus.SUCCESS else None,
    )


def _binding_payload(binding: DecisionBinding) -> dict[str, str | int]:
    return {
        "scope_key": binding.scope_key,
        "session_id": binding.session_id,
        "message_id": binding.current_message_id,
        "sender_key": binding.current_sender_key,
        "content_digest": binding.current_content_digest,
        "conversation_revision": binding.conversation_revision,
        "generation_epoch": binding.generation_epoch,
        "trace_id": binding.trace_id,
    }


def _recompute_shape_digest(request: AcquisitionRequest) -> str:
    encoded = json.dumps(
        {
            "binding": _binding_payload(request.binding),
            "kind": request.kind.value,
            "capability": request.selection.capability.value,
            "arguments": request.argument_items,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_preflight(
    request: AcquisitionRequest,
) -> tuple[SealedExecutionStatus | None, dict[str, Any] | None]:
    expected_name = _ANYSEARCH_NAMES.get(request.kind)
    if (
        expected_name is None
        or request.selection.tool_name != expected_name
        or request.selection.capability is not CapabilityClass.PUBLIC_WEB_READ
        or request.selection.call_budget != 1
    ):
        return SealedExecutionStatus.REQUEST_NOT_ALLOWED, None
    if request.selection.source != _ANYSEARCH_SOURCE:
        return SealedExecutionStatus.SOURCE_UNATTESTED, None
    try:
        if _recompute_shape_digest(request) != request.request_shape_digest:
            return SealedExecutionStatus.REQUEST_TAMPERED, None
        values = request.materialize_arguments()
    except Exception:
        return SealedExecutionStatus.REQUEST_TAMPERED, None
    if not isinstance(values, dict):
        return SealedExecutionStatus.REQUEST_TAMPERED, None
    if request.kind is AcquisitionKind.SEARCH:
        valid = (
            set(values) == {"query"}
            and isinstance(values.get("query"), str)
            and bool(values["query"].strip())
        )
    elif request.kind is AcquisitionKind.EXTRACT:
        valid = (
            set(values) == {"url"}
            and isinstance(values.get("url"), str)
            and bool(values["url"].strip())
        )
    else:
        queries = values.get("queries")
        valid = bool(
            set(values) == {"queries"}
            and isinstance(queries, list)
            and 2 <= len(queries) <= 5
            and all(
                isinstance(item, dict)
                and set(item) == {"query"}
                and isinstance(item.get("query"), str)
                and bool(item["query"].strip())
                for item in queries
            )
        )
    if not valid:
        return SealedExecutionStatus.REQUEST_TAMPERED, None
    return None, values


def _runtime_values(runtime_tools: Any) -> tuple[Any, ...] | None:
    if isinstance(runtime_tools, (str, bytes, bytearray, Mapping)):
        return None
    values = getattr(runtime_tools, "tools", runtime_tools)
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        return None
    try:
        return tuple(values)
    except TypeError:
        return None


def _mro_names(tool: Any) -> frozenset[str]:
    try:
        return frozenset(value.__name__.replace("_", "").lower() for value in type(tool).mro())
    except Exception:
        return frozenset()


def _source_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in str(value or "")
        .lower()
        .replace("\\", "/")
        .replace("/", ".")
        .split(".")
        if token
    )


def _select_runtime_tool(
    request: AcquisitionRequest,
    runtime_tools: Any,
) -> tuple[SealedExecutionStatus | None, Any | None]:
    values = _runtime_values(runtime_tools)
    if values is None:
        return SealedExecutionStatus.TOOL_MISSING, None
    matches = tuple(
        value
        for value in values
        if str(getattr(value, "name", "") or "") == request.selection.tool_name
    )
    if not matches:
        return SealedExecutionStatus.TOOL_MISSING, None
    if len(matches) != 1:
        return SealedExecutionStatus.TOOL_AMBIGUOUS, None
    tool = matches[0]
    names = _mro_names(tool)
    if "handofftool" in names:
        return SealedExecutionStatus.HANDOFF_FORBIDDEN, None
    if "mcptool" in names:
        return SealedExecutionStatus.MCP_FORBIDDEN, None
    if bool(getattr(tool, "is_background_task", False)):
        return SealedExecutionStatus.BACKGROUND_FORBIDDEN, None
    if not bool(getattr(tool, "active", True)):
        return SealedExecutionStatus.TOOL_INACTIVE, None
    try:
        classification = classify_tool(tool)
    except Exception:
        return SealedExecutionStatus.SOURCE_UNATTESTED, None
    descriptor = classification.descriptor
    if (
        descriptor.name != request.selection.tool_name
        or classification.capability is not CapabilityClass.PUBLIC_WEB_READ
        or classification.side_effect is not SideEffectClass.PUBLIC_READ
    ):
        return SealedExecutionStatus.SOURCE_UNATTESTED, None
    source_tokens = _source_tokens(f"{descriptor.origin}.{descriptor.module_path}")
    if _ANYSEARCH_SOURCE not in source_tokens or not descriptor.module_path:
        return SealedExecutionStatus.SOURCE_UNATTESTED, None
    return None, tool


def _harden_schema_node(value: Any) -> Any:
    if isinstance(value, list):
        return [_harden_schema_node(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _harden_schema_node(item) for key, item in value.items()}
    node_type = result.get("type")
    if node_type == "object" or "properties" in result:
        result["additionalProperties"] = False
    return result


def _prepare_schema(
    tool: Any,
    arguments: Mapping[str, Any],
) -> tuple[SealedExecutionStatus | None, dict[str, Any] | None]:
    raw = getattr(tool, "parameters", None)
    if not isinstance(raw, Mapping):
        return SealedExecutionStatus.SCHEMA_INVALID, None
    try:
        schema = _harden_schema_node(copy.deepcopy(dict(raw)))
        serialized = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except Exception:
        return SealedExecutionStatus.SCHEMA_INVALID, None
    if len(serialized) > _MAX_SCHEMA_CHARS:
        return SealedExecutionStatus.SCHEMA_INVALID, None
    if schema.get("type") != "object":
        return SealedExecutionStatus.SCHEMA_INVALID, None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return SealedExecutionStatus.SCHEMA_INVALID, None
    if not set(arguments).issubset(properties):
        return SealedExecutionStatus.SCHEMA_INVALID, None
    if any(not isinstance(properties[key], dict) for key in arguments):
        return SealedExecutionStatus.SCHEMA_INVALID, None
    required = schema.get("required", [])
    if (
        not isinstance(required, list)
        or any(not isinstance(item, str) for item in required)
        or len(set(required)) != len(required)
        or not set(required).issubset(properties)
    ):
        return SealedExecutionStatus.SCHEMA_INVALID, None
    return None, schema


def _default_draft202012_validate(
    schema: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> SealedExecutionStatus | None:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except Exception:
        return SealedExecutionStatus.SCHEMA_INVALID
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return SealedExecutionStatus.SCHEMA_INVALID
    try:
        validator = Draft202012Validator(schema)
        if next(validator.iter_errors(arguments), None) is not None:
            return SealedExecutionStatus.ARGUMENTS_INVALID
    except Exception:
        return SealedExecutionStatus.ARGUMENTS_INVALID
    return None


def _validate_arguments(
    schema: Mapping[str, Any],
    arguments: Mapping[str, Any],
    schema_validator: Callable[[dict[str, Any], dict[str, Any]], Any] | None,
) -> SealedExecutionStatus | None:
    if schema_validator is None:
        return _default_draft202012_validate(schema, arguments)
    try:
        result = schema_validator(copy.deepcopy(dict(schema)), copy.deepcopy(dict(arguments)))
        if result is False:
            return SealedExecutionStatus.ARGUMENTS_INVALID
    except Exception:
        return SealedExecutionStatus.ARGUMENTS_INVALID
    return None


def _now(clock: Callable[[], float]) -> float | None:
    try:
        value = float(clock())
    except Exception:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _guard_status(
    *,
    request: AcquisitionRequest,
    event_stopped: Callable[[], bool],
    epoch_current: Callable[[DecisionBinding], bool],
) -> SealedExecutionStatus | None:
    try:
        if bool(event_stopped()):
            return SealedExecutionStatus.EVENT_STOPPED
        if not bool(epoch_current(request.binding)):
            return SealedExecutionStatus.STALE_EPOCH
    except Exception:
        return SealedExecutionStatus.GUARD_FAILED
    return None


def _direct_send_attempted(run_context: Any) -> bool:
    proxy = getattr(run_context, "_shio_event_proxy", None)
    return bool(
        isinstance(proxy, _SealedEventProxy)
        and getattr(proxy, "send_attempted", False)
    )


async def _await_hook(
    hook: Callable[[Any, Any, dict[str, Any]], Any],
    *,
    run_context: Any,
    tool: Any,
    arguments: dict[str, Any],
    timeout: float,
) -> None:
    result = hook(run_context, tool, copy.deepcopy(arguments))
    if inspect.isawaitable(result):
        await asyncio.wait_for(result, timeout=timeout)


async def _collect_executor_results(
    executor: Callable[..., Any],
    *,
    tool: Any,
    run_context: Any,
    arguments: dict[str, Any],
) -> list[Any]:
    produced = executor(tool=tool, run_context=run_context, **arguments)
    if inspect.isawaitable(produced):
        produced = await produced
    if hasattr(produced, "__aiter__"):
        values: list[Any] = []
        async for value in produced:
            values.append(value)
            if len(values) > 1:
                break
        return values
    return [] if produced is None else [produced]


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str):
            return text.strip()
        resource = value.get("resource")
        if resource is not None:
            return _text_value(resource)
        return ""
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text.strip()
    resource = getattr(value, "resource", None)
    if resource is not None:
        return _text_value(resource)
    return ""


def _result_status(value: Any) -> tuple[SealedExecutionStatus | None, tuple[Any, ...]]:
    if isinstance(value, (str, bytes, bytearray)):
        return SealedExecutionStatus.RESULT_INVALID, ()
    sentinel = object()
    content = _field(value, "content", sentinel)
    if content is sentinel or not isinstance(content, (list, tuple)):
        return SealedExecutionStatus.RESULT_INVALID, ()
    if bool(_field(value, "is_error", False) or _field(value, "isError", False)):
        return SealedExecutionStatus.RESULT_ERROR, ()
    texts = tuple(_text_value(item) for item in content)
    if not any(texts):
        return SealedExecutionStatus.RESULT_EMPTY, ()
    if any(text.lower().startswith("error:") for text in texts if text):
        return SealedExecutionStatus.RESULT_ERROR, ()
    try:
        copied = tuple(copy.deepcopy(list(content)))
    except Exception:
        return SealedExecutionStatus.RESULT_INVALID, ()
    return None, copied


def _build_batch(
    request: AcquisitionRequest,
    arguments: dict[str, Any],
    content: tuple[Any, ...],
) -> SealedToolCallsBatch:
    call_id = secrets.token_hex(32)
    payload_json = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    call = _SealedToolCall(
        _call_id=call_id,
        function=_SealedFunction(
            name=request.selection.tool_name,
            _payload_json=payload_json,
        ),
    )
    segment = _SealedToolResultSegment(
        _call_id=call_id,
        _content=content,
        _is_error=False,
    )
    return SealedToolCallsBatch(
        tool_calls_info=_SealedToolCallsInfo(tool_calls=(call,)),
        tool_calls_result=(segment,),
    )


def _production_adapters(
    *,
    run_context: Any,
    plugin_context: Any,
    event: Any,
    timeout: float,
) -> tuple[Any, Any, Any]:
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

    if run_context is None:
        if plugin_context is None or event is None:
            raise RuntimeError("astrbot_run_context_inputs_required")
        run_context = ContextWrapper(
            context=AstrAgentContext(context=plugin_context, event=event),
            tool_call_timeout=max(1, int(math.ceil(timeout))),
        )
    # AstrBot's generic on_tool_start hook receives the executable tool and run
    # context and is not an authorization boundary.  The sealed reference-only
    # path deliberately omits it: a hook is able to send or mutate state before
    # an after-the-fact result check could run.
    return FunctionToolExecutor.execute, None, _sealed_run_context(run_context, event)


async def execute_sealed_acquisition(
    *,
    request: AcquisitionRequest,
    runtime_tools: Any,
    run_context: Any = None,
    plugin_context: Any = None,
    event: Any = None,
    executor: Callable[..., Any] | None = None,
    hook: Callable[[Any, Any, dict[str, Any]], Any] | None = None,
    clock: Callable[[], float] = time.time,
    event_stopped: Callable[[], bool] | None = None,
    epoch_current: Callable[[DecisionBinding], bool] | None = None,
    schema_validator: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None,
) -> SealedExecutionOutcome:
    """Execute one attested AnySearch request without exposing a tool loop.

    The brokered request is the only authority for the tool, arguments, source,
    binding, epoch, call budget, and deadline. The hook receives a disposable
    deep copy. The executor receives freshly materialized and revalidated
    arguments, and only one non-empty CallToolResult-like value can become an
    AstrBot-compatible batch for the P3-05 adapters.
    """

    if not isinstance(request, AcquisitionRequest):
        raise TypeError("acquisition_request_required")
    preflight, arguments = _request_preflight(request)
    if preflight is not None or arguments is None:
        return _outcome(preflight or SealedExecutionStatus.REQUEST_TAMPERED)

    now = _now(clock)
    if now is None:
        return _outcome(SealedExecutionStatus.GUARD_FAILED)
    remaining = request.selection.deadline - now
    if remaining <= 0:
        return _outcome(SealedExecutionStatus.DEADLINE_EXPIRED)

    runtime_snapshot = _runtime_values(runtime_tools)
    if runtime_snapshot is None:
        return _outcome(SealedExecutionStatus.TOOL_MISSING)
    selection_status, tool = _select_runtime_tool(request, runtime_snapshot)
    if selection_status is not None or tool is None:
        return _outcome(selection_status or SealedExecutionStatus.TOOL_MISSING)

    schema_status, schema = _prepare_schema(tool, arguments)
    if schema_status is not None or schema is None:
        return _outcome(schema_status or SealedExecutionStatus.SCHEMA_INVALID)
    validation = _validate_arguments(schema, arguments, schema_validator)
    if validation is not None:
        return _outcome(validation)

    if event_stopped is None:
        candidate_event = event
        if candidate_event is None:
            candidate_event = getattr(getattr(run_context, "context", None), "event", None)
        stopped_method = getattr(candidate_event, "is_stopped", None)
        event_stopped = stopped_method if callable(stopped_method) else (lambda: False)
    if epoch_current is None:
        return _outcome(SealedExecutionStatus.GUARD_FAILED)
    guard = _guard_status(
        request=request,
        event_stopped=event_stopped,
        epoch_current=epoch_current,
    )
    if guard is not None:
        return _outcome(guard)

    production_defaults = executor is None
    if production_defaults:
        try:
            executor, production_hook, run_context = _production_adapters(
                run_context=run_context,
                plugin_context=plugin_context,
                event=event,
                timeout=remaining,
            )
            if hook is None:
                hook = production_hook
        except Exception:
            return _outcome(SealedExecutionStatus.PRODUCTION_ADAPTER_UNAVAILABLE)
    elif executor is not None and not callable(executor):
        executor = getattr(executor, "execute", None)
    if not callable(executor):
        return _outcome(SealedExecutionStatus.PRODUCTION_ADAPTER_UNAVAILABLE)
    if not production_defaults:
        run_context = _sealed_run_context(run_context, event)
    if hook is not None and not callable(hook):
        hook = getattr(hook, "on_tool_start", None)
        if not callable(hook):
            return _outcome(SealedExecutionStatus.HOOK_FAILED)

    if hook is not None:
        now = _now(clock)
        if now is None:
            return _outcome(SealedExecutionStatus.GUARD_FAILED)
        remaining = request.selection.deadline - now
        if remaining <= 0:
            return _outcome(SealedExecutionStatus.DEADLINE_EXPIRED)
        try:
            await _await_hook(
                hook,
                run_context=run_context,
                tool=tool,
                arguments=arguments,
                timeout=remaining,
            )
        except _DirectSendForbiddenError:
            return _outcome(SealedExecutionStatus.DIRECT_SEND_FORBIDDEN)
        except asyncio.TimeoutError:
            return _outcome(SealedExecutionStatus.EXECUTION_TIMEOUT)
        except Exception:
            return _outcome(SealedExecutionStatus.HOOK_FAILED)
        if _direct_send_attempted(run_context):
            return _outcome(SealedExecutionStatus.DIRECT_SEND_FORBIDDEN)

    guard = _guard_status(
        request=request,
        event_stopped=event_stopped,
        epoch_current=epoch_current,
    )
    if guard is not None:
        return _outcome(guard)

    # Hooks have no authority over the actual call. Re-select the exact same
    # object, regenerate arguments from the sealed request, and validate again.
    post_selection_status, post_tool = _select_runtime_tool(request, runtime_snapshot)
    if post_selection_status is not None or post_tool is not tool:
        return _outcome(post_selection_status or SealedExecutionStatus.REQUEST_TAMPERED)
    try:
        arguments = request.materialize_arguments()
    except Exception:
        return _outcome(SealedExecutionStatus.REQUEST_TAMPERED)
    schema_status, schema = _prepare_schema(tool, arguments)
    if schema_status is not None or schema is None:
        return _outcome(schema_status or SealedExecutionStatus.SCHEMA_INVALID)
    validation = _validate_arguments(schema, arguments, schema_validator)
    if validation is not None:
        return _outcome(validation)

    now = _now(clock)
    if now is None:
        return _outcome(SealedExecutionStatus.GUARD_FAILED)
    remaining = request.selection.deadline - now
    if remaining <= 0:
        return _outcome(SealedExecutionStatus.DEADLINE_EXPIRED)
    try:
        values = await asyncio.wait_for(
            _collect_executor_results(
                executor,
                tool=tool,
                run_context=run_context,
                arguments=copy.deepcopy(arguments),
            ),
            timeout=remaining,
        )
    except _DirectSendForbiddenError:
        return _outcome(SealedExecutionStatus.DIRECT_SEND_FORBIDDEN, executed=True)
    except asyncio.TimeoutError:
        return _outcome(SealedExecutionStatus.EXECUTION_TIMEOUT, executed=True)
    except Exception:
        return _outcome(SealedExecutionStatus.EXECUTION_FAILED, executed=True)

    if _direct_send_attempted(run_context):
        return _outcome(SealedExecutionStatus.DIRECT_SEND_FORBIDDEN, executed=True)

    count = len(values)
    if count == 0:
        return _outcome(
            SealedExecutionStatus.RESULT_MISSING,
            executed=True,
            result_count=0,
        )
    if count != 1:
        return _outcome(
            SealedExecutionStatus.RESULT_MULTIPLE,
            executed=True,
            result_count=count,
        )
    if values[0] is None:
        return _outcome(
            SealedExecutionStatus.DIRECT_SEND_FORBIDDEN,
            executed=True,
            result_count=1,
        )

    guard = _guard_status(
        request=request,
        event_stopped=event_stopped,
        epoch_current=epoch_current,
    )
    if guard is not None:
        return _outcome(guard, executed=True, result_count=1)
    now = _now(clock)
    if now is None:
        return _outcome(
            SealedExecutionStatus.GUARD_FAILED,
            executed=True,
            result_count=1,
        )
    if now > request.selection.deadline:
        return _outcome(
            SealedExecutionStatus.EXECUTION_TIMEOUT,
            executed=True,
            result_count=1,
        )

    result_status, content = _result_status(values[0])
    if result_status is not None:
        return _outcome(result_status, executed=True, result_count=1)
    try:
        batch = _build_batch(request, arguments, content)
    except Exception:
        return _outcome(
            SealedExecutionStatus.RESULT_INVALID,
            executed=True,
            result_count=1,
        )
    return _outcome(
        SealedExecutionStatus.SUCCESS,
        executed=True,
        result_count=1,
        batch=batch,
    )


__all__ = [
    "SUPPORTED_SEALED_ACQUISITION_TOOL_NAMES",
    "SealedExecutionOutcome",
    "SealedExecutionStatus",
    "SealedToolCallsBatch",
    "execute_sealed_acquisition",
]
