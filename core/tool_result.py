from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .capability_policy import (
    CapabilityClass,
    SideEffectClass,
    ToolClassification,
    ToolDescriptor,
    classify_descriptor,
    classify_tool,
)
from .contracts import ContractViolation, DecisionBinding
from .tool_broker import AcquisitionRequest


class ToolResultVisibility(str, Enum):
    REFERENCE_ONLY = "reference_only"
    LOCAL_PRESENTATION = "local_presentation"
    INTERNAL_ONLY = "internal_only"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True, slots=True, repr=False)
class TypedToolResult:
    """One result envelope, optionally bound to one brokered acquisition.

    Payload and identifiers deliberately have no dataclass repr. A result can
    remain useful to compatibility diagnostics while only a fully broker-bound
    envelope is eligible for P3-05 grounding.
    """

    binding: DecisionBinding | None
    action_id: str
    request_shape_digest: str
    acquisition_deadline: float
    observed_at: float
    call_arguments_digest: str
    arguments_attested: bool
    call_id: str
    result_id: str
    tool_name: str
    tool_source: str
    source_attested: bool
    capability: CapabilityClass
    side_effect: SideEffectClass
    scope_key: str
    target_sender_key: str
    source_kind: str
    visibility: ToolResultVisibility
    content: str
    content_digest: str
    success: bool
    provenance_status: str

    def __post_init__(self) -> None:
        if self.binding is not None and not isinstance(self.binding, DecisionBinding):
            raise ContractViolation("tool_result_binding_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.content_digest or "")):
            raise ContractViolation("tool_result_content_digest_invalid")
        if self.call_arguments_digest and not re.fullmatch(
            r"[0-9a-f]{64}", str(self.call_arguments_digest)
        ):
            raise ContractViolation("tool_result_arguments_digest_invalid")
        if self.binding is None:
            if self.action_id or self.request_shape_digest or self.acquisition_deadline:
                raise ContractViolation("unbound_tool_result_authority_forbidden")
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", str(self.action_id or "")):
                raise ContractViolation("tool_result_action_id_invalid")
            if not re.fullmatch(
                r"[0-9a-f]{64}", str(self.request_shape_digest or "")
            ):
                raise ContractViolation("tool_result_request_shape_digest_invalid")
            if (
                not math.isfinite(float(self.acquisition_deadline))
                or self.acquisition_deadline <= 0
            ):
                raise ContractViolation("tool_result_deadline_invalid")
        if not math.isfinite(float(self.observed_at)) or self.observed_at < 0:
            raise ContractViolation("tool_result_observed_at_invalid")
        if (
            type(self.arguments_attested) is not bool
            or type(self.source_attested) is not bool
            or type(self.success) is not bool
        ):
            raise ContractViolation("tool_result_flag_invalid")

    def as_reference(self) -> str:
        """Compatibility API: raw result envelopes are never renderer input."""

        return ""

    def __repr__(self) -> str:
        return (
            "TypedToolResult("
            f"bound={self.binding is not None}, "
            f"capability={self.capability.value!r}, "
            f"visibility={self.visibility.value!r}, "
            f"success={self.success!r}, "
            f"provenance_status={self.provenance_status!r}, "
            f"content_chars={len(self.content)})"
        )


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _identifier(value: Any) -> str:
    return str(value or "").strip()[:240]


def _content_text(value: Any, *, limit: int) -> str:
    """Normalize result content only; tool arguments are never passed here."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, (int, float, bool)):
        return str(value)[:limit]
    if isinstance(value, (list, tuple)):
        parts = [
            text
            for item in value
            if (text := _content_text(item, limit=limit))
        ]
        return "\n".join(parts)[:limit]
    if isinstance(value, dict):
        # Preserve result shape for the P3-05 allowlisted extractor. It is
        # repr-hidden and never rendered directly.
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )[:limit]
        except (TypeError, ValueError):
            return ""
    text = getattr(value, "text", None)
    if text is not None:
        return _content_text(text, limit=limit)
    content = getattr(value, "content", None)
    if content is not None and content is not value:
        return _content_text(content, limit=limit)
    return ""


def _digest(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _arguments_digest(value: Any) -> str:
    """Hash a canonical argument object without retaining its payload."""

    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
    if not isinstance(parsed, Mapping):
        return ""
    try:
        encoded = json.dumps(
            dict(parsed),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def _visibility(classification: ToolClassification) -> ToolResultVisibility:
    if classification.capability in {
        CapabilityClass.PUBLIC_WEB_READ,
        CapabilityClass.CHAT_RETRIEVAL,
    }:
        return ToolResultVisibility.REFERENCE_ONLY
    if classification.capability == CapabilityClass.LOCAL_PRESENTATION:
        return ToolResultVisibility.LOCAL_PRESENTATION
    if classification.capability == CapabilityClass.UNKNOWN:
        return ToolResultVisibility.UNTRUSTED
    return ToolResultVisibility.INTERNAL_ONLY


def _unknown_classification(name: str) -> ToolClassification:
    return classify_descriptor(
        ToolDescriptor(
            name=name,
            description="",
            origin="tool_result",
            module_path="",
            parameter_names=(),
            active=True,
            declared_capability="",
            declared_side_effect="",
        )
    )


def _tool_classifications(tools: Iterable[Any]) -> tuple[ToolClassification, ...]:
    return tuple(classify_tool(tool) for tool in tools)


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


def _selection_is_attested(
    request: AcquisitionRequest,
    classifications: tuple[ToolClassification, ...],
) -> tuple[ToolClassification | None, str]:
    selection = request.selection
    matches = tuple(
        value
        for value in classifications
        if value.descriptor.name == selection.tool_name
    )
    if len(matches) != 1:
        return None, "runtime_tool_missing_or_ambiguous"
    classification = matches[0]
    if not classification.descriptor.active:
        return classification, "runtime_tool_inactive"
    if classification.capability is not selection.capability:
        return classification, "capability_mismatch"
    descriptor = classification.descriptor
    expected_argument_names = set(request.materialize_arguments())
    if not expected_argument_names.issubset(set(descriptor.parameter_names)):
        return classification, "runtime_tool_interface_changed"
    source = selection.source.strip().lower()
    if source == "astrbot_plugin_anysearch":
        tokens = _source_tokens(f"{descriptor.origin}.{descriptor.module_path}")
        if source not in tokens:
            return classification, "source_mismatch"
    else:
        runtime_sources = {
            descriptor.origin.strip().lower(),
            descriptor.module_path.strip().lower(),
        }
        if source not in runtime_sources:
            return classification, "source_mismatch"
    return classification, "source_attested"


def _authority_fields(
    request: AcquisitionRequest | None,
    *,
    observed_at: float,
) -> dict[str, Any]:
    if request is None:
        return {
            "binding": None,
            "action_id": "",
            "request_shape_digest": "",
            "acquisition_deadline": 0.0,
            "observed_at": observed_at,
            "tool_source": "",
            "source_attested": False,
        }
    return {
        "binding": request.binding,
        "action_id": request.action_id,
        "request_shape_digest": request.request_shape_digest,
        "acquisition_deadline": request.selection.deadline,
        "observed_at": observed_at,
        "tool_source": request.selection.source,
        "source_attested": True,
    }


def adapt_tool_call_results(
    raw_results: Any,
    *,
    tools: Iterable[Any] = (),
    scope_key: str,
    target_sender_key: str,
    acquisition_request: AcquisitionRequest | None = None,
    observed_at: float | None = None,
    max_content_chars: int = 12000,
    max_results: int = 16,
) -> tuple[TypedToolResult, ...]:
    """Adapt AstrBot ToolCallsResult without retaining call arguments.

    With an acquisition request, the result binds to the broker's action,
    binding, deadline, and attested source. The legacy unbound form remains
    readable until P3-06 replaces the hot-path call site, but can never produce
    a GroundingFact.
    """

    if acquisition_request is not None and not isinstance(
        acquisition_request, AcquisitionRequest
    ):
        raise TypeError("acquisition_request_invalid")
    if acquisition_request is not None and observed_at is None:
        raise ContractViolation("tool_result_clock_required")
    try:
        observed = float(0.0 if observed_at is None else observed_at)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("tool_result_clock_invalid") from exc
    if not math.isfinite(observed) or observed < 0:
        raise ContractViolation("tool_result_clock_invalid")
    if not raw_results:
        return ()
    batches = raw_results if isinstance(raw_results, (list, tuple)) else [raw_results]
    classifications = _tool_classifications(tools)
    by_name: dict[str, ToolClassification] = {}
    for classification in classifications:
        name = classification.descriptor.name
        if name and name not in by_name:
            by_name[name] = classification
    selection_status = "unbound_request"
    if acquisition_request is not None:
        _, selection_status = _selection_is_attested(
            acquisition_request,
            classifications,
        )
    limit = max(200, min(50000, int(max_content_chars)))
    remaining = max(1, min(64, int(max_results)))
    authoritative_scope = (
        acquisition_request.binding.scope_key
        if acquisition_request is not None
        else str(scope_key or "").strip()
    )
    authoritative_sender = (
        acquisition_request.binding.current_sender_key
        if acquisition_request is not None
        else str(target_sender_key or "").strip()
    )
    authority = _authority_fields(acquisition_request, observed_at=observed)
    expected_arguments_digest = (
        _arguments_digest(acquisition_request.materialize_arguments())
        if acquisition_request is not None
        else ""
    )
    typed: list[TypedToolResult] = []

    batch_values: list[tuple[list[Any], list[Any]]] = []
    all_calls: list[Any] = []
    for batch in batches:
        info = _field(batch, "tool_calls_info", None)
        calls = list(_field(info, "tool_calls", []) or [])
        result_segments = list(_field(batch, "tool_calls_result", []) or [])
        batch_values.append((calls, result_segments))
        all_calls.extend(calls)
    unique_call_ok = acquisition_request is None or len(all_calls) == 1

    for calls, result_segments in batch_values:
        if remaining <= 0:
            break
        results_by_call: dict[str, list[Any]] = {}
        for segment in result_segments:
            results_by_call.setdefault(
                _identifier(_field(segment, "tool_call_id", "")), []
            ).append(segment)
        seen_result_ids: set[int] = set()

        for call in calls:
            if remaining <= 0:
                break
            function = _field(call, "function", None)
            name = _identifier(_field(function, "name", ""))
            call_id = _identifier(_field(call, "id", ""))
            actual_arguments_digest = (
                _arguments_digest(_field(function, "arguments", None))
                if acquisition_request is not None
                else ""
            )
            arguments_attested = bool(
                acquisition_request is not None
                and actual_arguments_digest
                and actual_arguments_digest == expected_arguments_digest
            )
            matched = results_by_call.get(call_id, []) if call_id else []
            segment = matched[0] if len(matched) == 1 else None
            if segment is not None:
                seen_result_ids.add(id(segment))
            result_id = _identifier(
                _field(segment, "id", "")
                or _field(segment, "result_id", "")
                or call_id
            )
            content = _content_text(_field(segment, "content", None), limit=limit)
            classification = by_name.get(name) or _unknown_classification(name)
            status = "matched_call_and_result"
            if not unique_call_ok:
                status = "ambiguous_call_count"
            elif not call_id:
                status = "missing_call_id"
            elif segment is None:
                status = "missing_result" if not matched else "ambiguous_result"
            elif acquisition_request is not None:
                if name != acquisition_request.selection.tool_name:
                    status = "tool_mismatch"
                elif selection_status != "source_attested":
                    status = selection_status
                elif (
                    classification.capability
                    is not acquisition_request.selection.capability
                ):
                    status = "capability_mismatch"
                elif not arguments_attested:
                    status = "argument_mismatch"
            is_error = bool(
                _field(segment, "is_error", False)
                or _field(segment, "isError", False)
            )
            if status == "matched_call_and_result" and is_error:
                status = "tool_error"
            elif status == "matched_call_and_result" and not content:
                status = "empty_result"
            elif (
                status == "matched_call_and_result"
                and acquisition_request is not None
                and observed > acquisition_request.selection.deadline
            ):
                status = "timeout"
            typed.append(
                TypedToolResult(
                    **authority,
                    call_arguments_digest=actual_arguments_digest,
                    arguments_attested=arguments_attested,
                    call_id=call_id,
                    result_id=result_id or _digest(content),
                    tool_name=name,
                    capability=classification.capability,
                    side_effect=classification.side_effect,
                    scope_key=authoritative_scope,
                    target_sender_key=authoritative_sender,
                    source_kind="astrbot_tool_calls_result",
                    visibility=_visibility(classification),
                    content=content,
                    content_digest=_digest(content),
                    success=status == "matched_call_and_result",
                    provenance_status=status,
                )
            )
            remaining -= 1

        for segment in result_segments:
            if remaining <= 0:
                break
            if id(segment) in seen_result_ids:
                continue
            call_id = _identifier(_field(segment, "tool_call_id", ""))
            content = _content_text(_field(segment, "content", None), limit=limit)
            classification = _unknown_classification("")
            typed.append(
                TypedToolResult(
                    **{**authority, "source_attested": False},
                    call_arguments_digest="",
                    arguments_attested=False,
                    call_id=call_id,
                    result_id=(
                        _identifier(_field(segment, "id", ""))
                        or call_id
                        or _digest(content)
                    ),
                    tool_name="",
                    capability=classification.capability,
                    side_effect=classification.side_effect,
                    scope_key=authoritative_scope,
                    target_sender_key=authoritative_sender,
                    source_kind="astrbot_tool_calls_result",
                    visibility=ToolResultVisibility.UNTRUSTED,
                    content=content,
                    content_digest=_digest(content),
                    success=False,
                    provenance_status="orphan_result",
                )
            )
            remaining -= 1

    return tuple(typed)


def render_tool_result_references(results: Iterable[TypedToolResult]) -> str:
    """Raw tool-result XML was a protocol leak; renderer consumes facts only."""

    tuple(results)
    return ""


def tool_result_trace_metadata(
    results: Iterable[TypedToolResult],
) -> dict[str, str | int | bool]:
    values = list(results)
    capability_counts: dict[str, int] = {}
    for value in values:
        capability_counts[value.capability.value] = (
            capability_counts.get(value.capability.value, 0) + 1
        )
    return {
        "typed_tool_result_count": len(values),
        "typed_tool_result_success_count": sum(1 for value in values if value.success),
        "typed_tool_result_reference_count": sum(
            1
            for value in values
            if value.visibility == ToolResultVisibility.REFERENCE_ONLY
        ),
        "typed_tool_result_orphan_count": sum(
            1 for value in values if value.provenance_status == "orphan_result"
        ),
        "typed_tool_result_bound_count": sum(
            1 for value in values if value.binding is not None
        ),
        "typed_tool_result_capabilities": ",".join(
            f"{key}:{capability_counts[key]}" for key in sorted(capability_counts)
        ),
        "typed_tool_result_has_content": any(bool(value.content) for value in values),
    }


__all__ = [
    "ToolResultVisibility",
    "TypedToolResult",
    "adapt_tool_call_results",
    "render_tool_result_references",
    "tool_result_trace_metadata",
]
