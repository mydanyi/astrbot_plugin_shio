from __future__ import annotations

import hashlib
import ipaddress
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from .action_planner import PlannedAction
from .capability_policy import (
    AUDITED_SOURCE_HINTS,
    CapabilityClass,
    CapabilityPolicy,
    SideEffectClass,
    ToolClassification,
    classify_descriptor,
    decide_tool,
)
from .contracts import (
    ActionKind,
    ContractViolation,
    DecisionBinding,
    KnowledgeGapDecision,
    KnowledgeNeed,
)
from .contracts._validation import require_hex, require_safe_name, require_text


DEFAULT_ACQUISITION_TIMEOUT_S = 20.0
_REQUEST_SHAPE_SEAL = object()
_ANYSEARCH_PLUGIN_ID = "astrbot_plugin_anysearch"
_ASTRBOT_KB_TOOL_NAME = "astr_kb_search"
_ASTRBOT_KB_MODULE = "astrbot.core.tools.knowledge_base_tools"
_ANYSEARCH_BY_KIND: dict["AcquisitionKind", tuple[str, str]] = {}
_FORBIDDEN_ARGUMENT_KEYS = frozenset(
    {
        "owner",
        "is_owner",
        "binding",
        "scope_key",
        "session_id",
        "message_id",
        "sender_id",
        "sender_key",
        "tool",
        "tool_name",
        "exact_tool",
        "source",
        "deadline",
        "budget",
        "call_budget",
        "max_tool_calls",
    }
)


class AcquisitionKind(str, Enum):
    SEARCH = "search"
    EXTRACT = "extract"
    BATCH_SEARCH = "batch_search"
    KNOWLEDGE_BASE = "knowledge_base"
    CAPABILITY_ACTION = "capability_action"


_ANYSEARCH_BY_KIND.update(
    {
        AcquisitionKind.SEARCH: ("anysearch_search", "query"),
        AcquisitionKind.EXTRACT: ("anysearch_extract", "url"),
        AcquisitionKind.BATCH_SEARCH: ("anysearch_batch_search", "queries"),
    }
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


def _shape_digest(
    binding: DecisionBinding,
    kind: AcquisitionKind,
    capability: CapabilityClass,
    argument_items: tuple[tuple[str, Any], ...],
) -> str:
    payload = {
        "binding": _binding_payload(binding),
        "kind": kind.value,
        "capability": capability.value,
        "arguments": argument_items,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_text(value: Any, field_name: str, *, max_length: int = 2048) -> str:
    normalized = require_text(value, field_name)
    if len(normalized) > max_length:
        raise ContractViolation(f"{field_name}_too_long")
    return normalized


def _validated_url(value: Any) -> str:
    normalized = _clean_text(value, "acquisition_url", max_length=4096)
    if any(character.isspace() for character in normalized):
        raise ContractViolation("acquisition_url_invalid")
    parsed = urlsplit(normalized)
    try:
        hostname = (
            str(parsed.hostname or "")
            .encode("idna")
            .decode("ascii")
            .rstrip(".")
        )
        parsed.port
    except (UnicodeError, ValueError):
        raise ContractViolation("acquisition_url_invalid") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
    ):
        raise ContractViolation("acquisition_url_invalid")
    lowered_host = hostname.lower()
    forbidden_suffixes = (
        ".localhost",
        ".localdomain",
        ".local",
        ".internal",
        ".lan",
        ".home",
        ".home.arpa",
    )
    if lowered_host in {"localhost", "localdomain"} or lowered_host.endswith(
        forbidden_suffixes
    ):
        raise ContractViolation("acquisition_url_nonpublic")
    address_text = lowered_host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        address = None
    if address is not None and (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ContractViolation("acquisition_url_nonpublic")
    # URL libraries may accept legacy decimal/octal/hex IPv4 forms that
    # ipaddress intentionally rejects.  Do not let ambiguous numeric hosts
    # reach a public-read extractor.
    compact = lowered_host.replace(".", "")
    if compact.isdigit() or lowered_host.startswith(("0x", "0X")):
        raise ContractViolation("acquisition_url_nonpublic")
    return normalized


def _is_single_url(value: str) -> bool:
    try:
        return _validated_url(value) == value.strip()
    except ContractViolation:
        return False


def _normalize_scalar(value: Any, field_name: str) -> str | int | float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractViolation(f"{field_name}_invalid")
        return value
    if isinstance(value, str):
        return _clean_text(value, field_name, max_length=8192)
    raise ContractViolation(f"{field_name}_invalid")


@dataclass(frozen=True, slots=True, init=False)
class AcquisitionRequestShape:
    binding: DecisionBinding = field(repr=False)
    kind: AcquisitionKind
    capability: CapabilityClass
    argument_items: tuple[tuple[str, Any], ...] = field(repr=False)
    shape_digest: str = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "request_shape_kind": self.kind.value,
            "request_shape_capability": self.capability.value,
            "request_shape_argument_count": len(self.argument_items),
            "request_shape_bound": True,
        }


def _issue_request_shape(
    *,
    binding: DecisionBinding,
    kind: AcquisitionKind,
    capability: CapabilityClass,
    argument_items: tuple[tuple[str, Any], ...],
) -> AcquisitionRequestShape:
    if not isinstance(binding, DecisionBinding):
        raise ContractViolation("broker_binding_required")
    if not isinstance(kind, AcquisitionKind):
        raise ContractViolation("broker_request_kind_invalid")
    if not isinstance(capability, CapabilityClass):
        raise ContractViolation("broker_request_capability_invalid")
    shape = object.__new__(AcquisitionRequestShape)
    object.__setattr__(shape, "binding", binding)
    object.__setattr__(shape, "kind", kind)
    object.__setattr__(shape, "capability", capability)
    object.__setattr__(shape, "argument_items", argument_items)
    object.__setattr__(
        shape,
        "shape_digest",
        _shape_digest(binding, kind, capability, argument_items),
    )
    object.__setattr__(shape, "_seal", _REQUEST_SHAPE_SEAL)
    return shape


def build_search_request_shape(
    binding: DecisionBinding,
    *,
    query: str,
) -> AcquisitionRequestShape:
    normalized = _clean_text(query, "acquisition_query")
    if _is_single_url(normalized):
        raise ContractViolation("explicit_url_requires_extract_shape")
    return _issue_request_shape(
        binding=binding,
        kind=AcquisitionKind.SEARCH,
        capability=CapabilityClass.PUBLIC_WEB_READ,
        argument_items=(("query", normalized),),
    )


def build_extract_request_shape(
    binding: DecisionBinding,
    *,
    url: str,
) -> AcquisitionRequestShape:
    return _issue_request_shape(
        binding=binding,
        kind=AcquisitionKind.EXTRACT,
        capability=CapabilityClass.PUBLIC_WEB_READ,
        argument_items=(("url", _validated_url(url)),),
    )


def build_knowledge_base_request_shape(
    binding: DecisionBinding,
    *,
    query: str,
) -> AcquisitionRequestShape:
    return _issue_request_shape(
        binding=binding,
        kind=AcquisitionKind.KNOWLEDGE_BASE,
        capability=CapabilityClass.CHAT_RETRIEVAL,
        argument_items=(("query", _clean_text(query, "knowledge_base_query")),),
    )


def build_batch_request_shape(
    binding: DecisionBinding,
    *,
    queries: Iterable[str],
) -> AcquisitionRequestShape:
    if isinstance(queries, (str, bytes)):
        raise ContractViolation("acquisition_batch_queries_invalid")
    try:
        normalized = tuple(
            dict.fromkeys(
                _clean_text(value, "acquisition_batch_query")
                for value in queries
            )
        )
    except TypeError as exc:
        raise ContractViolation("acquisition_batch_queries_invalid") from exc
    if not 2 <= len(normalized) <= 5:
        raise ContractViolation("acquisition_batch_query_count_invalid")
    return _issue_request_shape(
        binding=binding,
        kind=AcquisitionKind.BATCH_SEARCH,
        capability=CapabilityClass.PUBLIC_WEB_READ,
        argument_items=(("queries", normalized),),
    )


def build_capability_request_shape(
    binding: DecisionBinding,
    *,
    capability: CapabilityClass,
    arguments: Mapping[str, Any],
) -> AcquisitionRequestShape:
    if not isinstance(capability, CapabilityClass) or capability in {
        CapabilityClass.PUBLIC_WEB_READ,
        CapabilityClass.CHAT_RETRIEVAL,
        CapabilityClass.LOCAL_PRESENTATION,
        CapabilityClass.UNKNOWN,
    }:
        raise ContractViolation("broker_owner_capability_shape_invalid")
    if not isinstance(arguments, Mapping) or not arguments or len(arguments) > 16:
        raise ContractViolation("broker_capability_arguments_invalid")
    items: list[tuple[str, str | int | float | bool]] = []
    for raw_key, raw_value in arguments.items():
        key = require_safe_name(raw_key, "broker_argument_name")
        if key in _FORBIDDEN_ARGUMENT_KEYS:
            raise ContractViolation("broker_argument_authority_field_forbidden")
        items.append((key, _normalize_scalar(raw_value, f"broker_argument_{key}")))
    argument_items = tuple(sorted(items))
    return _issue_request_shape(
        binding=binding,
        kind=AcquisitionKind.CAPABILITY_ACTION,
        capability=capability,
        argument_items=argument_items,
    )


@dataclass(frozen=True, slots=True)
class ToolSelection:
    binding: DecisionBinding = field(repr=False)
    action_id: str = field(repr=False)
    tool_name: str
    source: str
    capability: CapabilityClass
    deadline: float
    call_budget: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise ContractViolation("broker_binding_required")
        object.__setattr__(
            self,
            "action_id",
            require_hex(self.action_id, "broker_action_id", lengths=(64,)),
        )
        object.__setattr__(
            self,
            "tool_name",
            require_safe_name(self.tool_name, "broker_tool_name"),
        )
        object.__setattr__(self, "source", require_text(self.source, "broker_tool_source"))
        if not isinstance(self.capability, CapabilityClass):
            raise ContractViolation("broker_capability_invalid")
        try:
            deadline = float(self.deadline)
        except (TypeError, ValueError) as exc:
            raise ContractViolation("broker_deadline_invalid") from exc
        if not math.isfinite(deadline) or deadline <= 0:
            raise ContractViolation("broker_deadline_invalid")
        object.__setattr__(self, "deadline", deadline)
        if self.call_budget != 1:
            raise ContractViolation("broker_call_budget_must_be_one")

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        return {
            "tool_selected": True,
            "tool_name": self.tool_name,
            "tool_source": self.source,
            "tool_capability": self.capability.value,
            "tool_call_budget": self.call_budget,
            "tool_deadline": self.deadline,
            "tool_action_bound": True,
        }


@dataclass(frozen=True, slots=True)
class AcquisitionRequest:
    selection: ToolSelection
    kind: AcquisitionKind
    request_shape_digest: str = field(repr=False)
    argument_items: tuple[tuple[str, Any], ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.selection, ToolSelection):
            raise ContractViolation("broker_selection_required")
        if not isinstance(self.kind, AcquisitionKind):
            raise ContractViolation("broker_request_kind_invalid")
        object.__setattr__(
            self,
            "request_shape_digest",
            require_hex(
                self.request_shape_digest,
                "broker_request_shape_digest",
                lengths=(64,),
            ),
        )
        if not isinstance(self.argument_items, tuple) or not self.argument_items:
            raise ContractViolation("broker_request_arguments_invalid")

    @property
    def binding(self) -> DecisionBinding:
        return self.selection.binding

    @property
    def action_id(self) -> str:
        return self.selection.action_id

    def materialize_arguments(self) -> dict[str, Any]:
        if self.kind is AcquisitionKind.BATCH_SEARCH:
            values = dict(self.argument_items)["queries"]
            return {"queries": [{"query": value} for value in values]}
        return dict(self.argument_items)

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        return {
            **self.selection.trace_metadata(),
            "acquisition_kind": self.kind.value,
            "acquisition_argument_count": len(self.argument_items),
            "acquisition_shape_bound": True,
        }


def _configured_names(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ContractViolation("broker_configured_tools_invalid")
    try:
        return frozenset(
            require_safe_name(value, "broker_configured_tool") for value in values
        )
    except TypeError as exc:
        raise ContractViolation("broker_configured_tools_invalid") from exc


def _runtime_classifications(
    values: Iterable[ToolClassification],
    configured_names: frozenset[str],
) -> tuple[ToolClassification, ...]:
    if isinstance(values, (str, bytes)):
        raise ContractViolation("broker_runtime_tools_invalid")
    try:
        supplied = tuple(values)
    except TypeError as exc:
        raise ContractViolation("broker_runtime_tools_invalid") from exc
    canonical: list[ToolClassification] = []
    for classification in supplied:
        if not isinstance(classification, ToolClassification):
            raise ContractViolation("broker_runtime_classification_invalid")
        recalculated = classify_descriptor(classification.descriptor)
        if recalculated != classification:
            if classification.descriptor.name.strip().lower() in configured_names:
                raise ContractViolation("broker_runtime_classification_forged")
            continue
        canonical.append(classification)
    return tuple(canonical)


def _verified_owner_policy(policy: CapabilityPolicy) -> bool:
    return bool(
        policy.policy_kind == "owner"
        and policy.is_owner
        and policy.relationship_role == "owner"
        and policy.verification_source.startswith("astrbot_event_sender_id:")
        and policy.verification_source.endswith(":configured_owner_id")
        and not policy.is_degraded
    )


def _verified_guest_policy(policy: CapabilityPolicy) -> bool:
    return bool(
        policy.policy_kind == "guest"
        and not policy.is_owner
        and policy.relationship_role in {"group_peer", "private_peer"}
        and policy.verification_source.startswith("astrbot_event_sender_id:")
        and "identity_unverified" not in policy.verification_source
        and not policy.is_degraded
    )


def _attested_source(classification: ToolClassification) -> str:
    descriptor = classification.descriptor
    name = descriptor.name.strip().lower()
    hints = AUDITED_SOURCE_HINTS.get(name, ())
    source_text = f"{descriptor.origin} {descriptor.module_path}".strip().lower()

    def source_has_hint(hint: str) -> bool:
        normalized = str(hint or "").strip().lower()
        if normalized.startswith("astrbot_plugin_"):
            tokens = {
                token
                for value in (descriptor.origin, descriptor.module_path)
                for token in value.lower().replace("\\", "/").replace("/", ".").split(".")
                if token
            }
            return normalized in tokens
        if normalized == "astrbot":
            return any(
                value.lower() == "astrbot" or value.lower().startswith("astrbot.")
                for value in (descriptor.origin, descriptor.module_path)
            )
        return normalized in {
            token
            for value in (descriptor.origin, descriptor.module_path)
            for token in value.lower().replace("\\", "/").replace("/", ".").split(".")
            if token
        }

    if not hints or not any(source_has_hint(hint) for hint in hints):
        raise ContractViolation("broker_tool_source_unattested")
    if name in {value[0] for value in _ANYSEARCH_BY_KIND.values()}:
        if _ANYSEARCH_PLUGIN_ID not in source_text:
            raise ContractViolation("broker_anysearch_source_mismatch")
        return _ANYSEARCH_PLUGIN_ID
    if name == _ASTRBOT_KB_TOOL_NAME:
        if descriptor.module_path.strip().lower() != _ASTRBOT_KB_MODULE:
            raise ContractViolation("broker_knowledge_base_source_mismatch")
        return _ASTRBOT_KB_MODULE
    source = descriptor.module_path.strip() or descriptor.origin.strip()
    return require_text(source, "broker_tool_source")


def _select_candidate(
    *,
    request_shape: AcquisitionRequestShape,
    capability: CapabilityClass,
    classifications: tuple[ToolClassification, ...],
    configured_names: frozenset[str],
) -> ToolClassification:
    if request_shape.kind in _ANYSEARCH_BY_KIND:
        expected_name, required_parameter = _ANYSEARCH_BY_KIND[request_shape.kind]
        if expected_name not in configured_names:
            raise ContractViolation("broker_required_tool_not_configured")
        matches = tuple(
            item
            for item in classifications
            if item.descriptor.name.strip().lower() == expected_name
        )
        if len(matches) != 1:
            raise ContractViolation("broker_required_tool_missing_or_ambiguous")
        candidate = matches[0]
        if (
            candidate.capability is not CapabilityClass.PUBLIC_WEB_READ
            or candidate.side_effect is not SideEffectClass.PUBLIC_READ
        ):
            raise ContractViolation("broker_anysearch_capability_mismatch")
        if required_parameter not in candidate.descriptor.parameter_names:
            raise ContractViolation("broker_anysearch_interface_changed")
        return candidate

    if request_shape.kind is AcquisitionKind.KNOWLEDGE_BASE:
        if _ASTRBOT_KB_TOOL_NAME not in configured_names:
            raise ContractViolation("broker_required_tool_not_configured")
        matches = tuple(
            item
            for item in classifications
            if item.descriptor.name.strip().lower() == _ASTRBOT_KB_TOOL_NAME
        )
        if len(matches) != 1:
            raise ContractViolation("broker_required_tool_missing_or_ambiguous")
        candidate = matches[0]
        if (
            candidate.capability is not CapabilityClass.CHAT_RETRIEVAL
            or candidate.side_effect is not SideEffectClass.SCOPED_READ
        ):
            raise ContractViolation("broker_knowledge_base_capability_mismatch")
        if "query" not in candidate.descriptor.parameter_names:
            raise ContractViolation("broker_knowledge_base_interface_changed")
        return candidate

    matches = tuple(
        item
        for item in classifications
        if item.descriptor.name.strip().lower() in configured_names
        and item.capability is capability
    )
    if len(matches) != 1:
        raise ContractViolation("broker_owner_tool_missing_or_ambiguous")
    candidate = matches[0]
    argument_names = {name for name, _ in request_shape.argument_items}
    if not argument_names.issubset(set(candidate.descriptor.parameter_names)):
        raise ContractViolation("broker_owner_tool_interface_changed")
    return candidate


def broker_tool_request(
    *,
    planned_action: PlannedAction,
    knowledge_gap: KnowledgeGapDecision,
    capability_policy: CapabilityPolicy,
    runtime_tools: Iterable[ToolClassification],
    configured_tool_names: Iterable[str],
    request_shape: AcquisitionRequestShape,
    now: float,
) -> AcquisitionRequest:
    """Select exactly one attested tool and issue one digest-bound request."""

    if not isinstance(planned_action, PlannedAction):
        raise ContractViolation("broker_planned_action_required")
    if planned_action.action.kind is not ActionKind.USE_TOOL:
        raise ContractViolation("broker_use_tool_action_required")
    binding = planned_action.binding
    if not isinstance(knowledge_gap, KnowledgeGapDecision):
        raise ContractViolation("broker_knowledge_gap_required")
    if knowledge_gap.binding != binding:
        raise ContractViolation("broker_knowledge_binding_mismatch")
    if not isinstance(capability_policy, CapabilityPolicy):
        raise ContractViolation("broker_capability_policy_required")
    if capability_policy.principal_key != binding.current_sender_key:
        raise ContractViolation("broker_capability_principal_mismatch")
    if capability_policy.conversation_mode not in {"direct_reply", "group_join"}:
        raise ContractViolation("broker_capability_mode_mismatch")
    if (
        not isinstance(request_shape, AcquisitionRequestShape)
        or getattr(request_shape, "_seal", None) is not _REQUEST_SHAPE_SEAL
    ):
        raise ContractViolation("broker_request_shape_untrusted")
    if request_shape.binding != binding:
        raise ContractViolation("broker_request_shape_binding_mismatch")

    try:
        capability = CapabilityClass(planned_action.action.capability_intent)
    except ValueError as exc:
        raise ContractViolation("broker_action_capability_invalid") from exc
    if request_shape.capability is not capability:
        raise ContractViolation("broker_request_capability_mismatch")

    owner_verified = _verified_owner_policy(capability_policy)
    guest_verified = _verified_guest_policy(capability_policy)
    if not owner_verified and not guest_verified:
        raise ContractViolation("broker_principal_policy_unverified")
    if guest_verified:
        if capability not in {
            CapabilityClass.PUBLIC_WEB_READ,
            CapabilityClass.CHAT_RETRIEVAL,
        }:
            raise ContractViolation("broker_guest_capability_forbidden")
        if (
            request_shape.kind not in _ANYSEARCH_BY_KIND
            and request_shape.kind is not AcquisitionKind.KNOWLEDGE_BASE
        ):
            raise ContractViolation("broker_guest_request_shape_forbidden")
        if (
            knowledge_gap.need is KnowledgeNeed.NONE
            or not knowledge_gap.requires_evidence
            or knowledge_gap.requested_capability is not capability
            or knowledge_gap.max_tool_calls < 1
        ):
            raise ContractViolation("broker_guest_knowledge_gap_required")
    elif capability in {
        CapabilityClass.PUBLIC_WEB_READ,
        CapabilityClass.CHAT_RETRIEVAL,
    }:
        if (
            knowledge_gap.need is KnowledgeNeed.NONE
            or not knowledge_gap.requires_evidence
            or knowledge_gap.requested_capability is not capability
            or knowledge_gap.max_tool_calls < 1
        ):
            raise ContractViolation("broker_owner_knowledge_gap_required")
    elif knowledge_gap.need is not KnowledgeNeed.NONE:
        raise ContractViolation("broker_owner_nonknowledge_gap_mismatch")

    if not capability_policy.allows_capability(capability):
        raise ContractViolation("broker_capability_denied")
    policy_budget = capability_policy.max_external_tool_calls
    if policy_budget != -1 and policy_budget < 1:
        raise ContractViolation("broker_policy_budget_denied")

    try:
        current_time = float(now)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("broker_clock_invalid") from exc
    if not math.isfinite(current_time) or current_time < 0:
        raise ContractViolation("broker_clock_invalid")
    deadline = current_time + DEFAULT_ACQUISITION_TIMEOUT_S

    configured_names = _configured_names(configured_tool_names)
    classifications = _runtime_classifications(runtime_tools, configured_names)
    candidate = _select_candidate(
        request_shape=request_shape,
        capability=capability,
        classifications=classifications,
        configured_names=configured_names,
    )
    if not candidate.descriptor.active:
        raise ContractViolation("broker_tool_disabled")
    policy_decision = decide_tool(capability_policy, candidate)
    if not policy_decision.allowed:
        raise ContractViolation("broker_tool_policy_denied")
    source = _attested_source(candidate)

    selection = ToolSelection(
        binding=binding,
        action_id=planned_action.action_id,
        tool_name=candidate.descriptor.name,
        source=source,
        capability=capability,
        deadline=deadline,
        call_budget=1,
    )
    return AcquisitionRequest(
        selection=selection,
        kind=request_shape.kind,
        request_shape_digest=request_shape.shape_digest,
        argument_items=request_shape.argument_items,
    )


def broker_proactive_read_request(
    *,
    proactive_request: object,
    capability: CapabilityClass,
    runtime_tools: Iterable[ToolClassification],
    configured_tool_names: Iterable[str],
    now: float,
) -> AcquisitionRequest:
    """Issue one public-read request for a canonical group-initiation topic."""

    from .proactive_runtime import ProactiveComposerRequest

    if type(proactive_request) is not ProactiveComposerRequest:
        raise ContractViolation("broker_proactive_request_required")
    authority_ref = getattr(proactive_request, "_authority_ref", None)
    authority = authority_ref() if callable(authority_ref) else None
    try:
        authority.inspect_request(proactive_request)
    except (AttributeError, ContractViolation, TypeError) as exc:
        raise ContractViolation("broker_proactive_request_untrusted") from exc
    if capability not in {
        CapabilityClass.CHAT_RETRIEVAL,
        CapabilityClass.PUBLIC_WEB_READ,
    }:
        raise ContractViolation("broker_proactive_capability_forbidden")
    plan = proactive_request.plan
    trace_id = hashlib.sha256(
        f"proactive-read|{plan.target.scope_key}|{plan.topic_digest}".encode("utf-8")
    ).hexdigest()[:32]
    binding = DecisionBinding(
        scope_key=plan.target.scope_key,
        session_id=plan.target.unified_msg_origin,
        current_message_id=f"proactive:{plan.topic_digest[:32]}",
        current_sender_key=f"{plan.target.scope_key}|actor:proactive_group",
        current_content_digest=plan.topic_digest,
        conversation_revision=plan.scene_revision,
        generation_epoch=plan.scene_revision,
        trace_id=trace_id,
    )
    request_shape = (
        build_knowledge_base_request_shape(binding, query=plan.topic_text)
        if capability is CapabilityClass.CHAT_RETRIEVAL
        else build_search_request_shape(binding, query=plan.topic_text)
    )
    configured_names = _configured_names(configured_tool_names)
    classifications = _runtime_classifications(runtime_tools, configured_names)
    candidate = _select_candidate(
        request_shape=request_shape,
        capability=capability,
        classifications=classifications,
        configured_names=configured_names,
    )
    if not candidate.descriptor.active:
        raise ContractViolation("broker_tool_disabled")
    if candidate.side_effect not in {
        SideEffectClass.PUBLIC_READ,
        SideEffectClass.SCOPED_READ,
    }:
        raise ContractViolation("broker_proactive_side_effect_forbidden")
    try:
        current_time = float(now)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("broker_clock_invalid") from exc
    if not math.isfinite(current_time) or current_time < 0:
        raise ContractViolation("broker_clock_invalid")
    action_id = hashlib.sha256(
        (
            "proactive-read-action|"
            + plan.topic_digest
            + "|"
            + request_shape.shape_digest
        ).encode("utf-8")
    ).hexdigest()
    selection = ToolSelection(
        binding=binding,
        action_id=action_id,
        tool_name=candidate.descriptor.name,
        source=_attested_source(candidate),
        capability=capability,
        deadline=current_time + DEFAULT_ACQUISITION_TIMEOUT_S,
        call_budget=1,
    )
    return AcquisitionRequest(
        selection=selection,
        kind=request_shape.kind,
        request_shape_digest=request_shape.shape_digest,
        argument_items=request_shape.argument_items,
    )


__all__ = [
    "DEFAULT_ACQUISITION_TIMEOUT_S",
    "AcquisitionKind",
    "AcquisitionRequest",
    "AcquisitionRequestShape",
    "ToolSelection",
    "broker_proactive_read_request",
    "broker_tool_request",
    "build_batch_request_shape",
    "build_capability_request_shape",
    "build_extract_request_shape",
    "build_knowledge_base_request_shape",
    "build_search_request_shape",
]
