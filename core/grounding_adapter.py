from __future__ import annotations

import hashlib
import html
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .capability_policy import CapabilityClass, SideEffectClass
from .contracts import ContractViolation, DecisionBinding, GroundingFact
from .tool_broker import (
    AcquisitionKind,
    AcquisitionRequest,
    DEFAULT_ACQUISITION_TIMEOUT_S,
)
from .tool_result import TypedToolResult, ToolResultVisibility


MAX_GROUNDING_FACTS = 8
MAX_CLAIM_CHARS = 600
_ALLOWED_CLAIM_FIELDS = frozenset(
    {
        "title",
        "snippet",
        "text",
        "content",
        "summary",
        "description",
        "answer",
    }
)

_PROTOCOL_BLOCK_RE = re.compile(
    r"<(?P<tag>channel|tool_call|function_call|search_memes|"
    r"anysearch_(?:search|extract|batch_search)|shio_[a-z0-9_.:-]+)\b[^>]*>"
    r".*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_PROTOCOL_CALL_RE = re.compile(
    r"\b(?:search_memes|anysearch_(?:search|extract|batch_search)|"
    r"tool_call|function_call)\s*(?:\(|\{).*",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"</?[a-z][^>]{0,240}>", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?://|data:)[^\s<>'\"，。；;]+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![\w])(?:[a-z]:[\\/])[^\s，。；;]+", re.IGNORECASE
)
_UNIX_PATH_RE = re.compile(
    r"(?<![\w:])/(?:[a-z0-9_.-]+/)+[a-z0-9_.-]+", re.IGNORECASE
)
_TOKEN_RE = re.compile(
    r"(?i)\b(?:"
    r"bearer\s+[a-z0-9._~+/-]{6,}|"
    r"sk-[a-z0-9_-]{6,}|"
    r"gh[oprsu]_[a-z0-9_]{12,}|"
    r"ey[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
    r"\s*[:=]\s*[^\s，。；;]+"
    r")"
)
_JSON_KEY_RE = re.compile(r'"(?:arguments|tool_calls?|function|api_key|token)"\s*:?', re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_RELEVANCE_ASCII_RE = re.compile(r"[a-z][a-z0-9_.+-]{1,31}|\d{4,}", re.I)
_RELEVANCE_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_RELEVANCE_STOP_RE = re.compile(
    r"(?:请(?:你)?|帮我|麻烦(?:你)?|劳驾(?:你)?|让我|联网|上网|"
    r"(?:你|妳|您)?(?:知道|了解|听说过)|"
    r"查证|核实|验证|搜索一下|搜一下|查一下|查查|查资料|"
    r"检查一下|排查一下|自查一下|看看|有没有|是否|是不是|"
    r"网络黑话|网络用语|黑话|挺有意思|有意思|"
    r"是什么意思|是什么|为什么|怎么|如何|多少|这个|那个|这条|"
    r"一下|的|吗|嘛|么)"
)
_RELEVANCE_EQUIVALENTS = (
    (re.compile(r"(?:明日)"), "明天"),
    (re.compile(r"(?:今日)"), "今天"),
    (re.compile(r"(?:降雨|有雨|下雨|雨天)"), "天气"),
    (re.compile(r"(?:价钱|售价|多少钱)"), "价格"),
    (re.compile(r"(?:发布日期|发布日|发售时间|上市时间)"), "发布时间"),
)


class EvidenceOutcomeKind(str, Enum):
    ACCEPTED = "accepted"
    MISSING_RESULT = "missing_result"
    ORPHAN_RESULT = "orphan_result"
    TOOL_ERROR = "tool_error"
    TIMEOUT = "timeout"
    STALE_BINDING = "stale_binding"
    OTHER_SENDER = "other_sender"
    BINDING_MISMATCH = "binding_mismatch"
    ACTION_MISMATCH = "action_mismatch"
    ARGUMENT_MISMATCH = "argument_mismatch"
    DEADLINE_MISMATCH = "deadline_mismatch"
    TOOL_MISMATCH = "tool_mismatch"
    CAPABILITY_MISMATCH = "capability_mismatch"
    SOURCE_MISMATCH = "source_mismatch"
    VISIBILITY_DENIED = "visibility_denied"
    UNSAFE_CONTENT = "unsafe_content"
    INVALID_RESULT = "invalid_result"
    MULTIPLE_RESULTS = "multiple_results"
    IRRELEVANT_RESULT = "irrelevant_result"


@dataclass(frozen=True, slots=True, repr=False)
class EvidenceOutcome:
    """Code-owned result of validating evidence for the current action."""

    binding: DecisionBinding = field(repr=False)
    action_id: str = field(repr=False)
    kind: EvidenceOutcomeKind
    facts: tuple[GroundingFact, ...] = field(default=(), repr=False)
    result_count: int = 0
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise ContractViolation("evidence_binding_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.action_id or "")):
            raise ContractViolation("evidence_action_id_invalid")
        if not isinstance(self.kind, EvidenceOutcomeKind):
            raise ContractViolation("evidence_outcome_kind_invalid")
        if not isinstance(self.result_count, int) or self.result_count < 0:
            raise ContractViolation("evidence_result_count_invalid")
        if any(fact.binding != self.binding for fact in self.facts):
            raise ContractViolation("evidence_fact_binding_mismatch")
        if self.kind is EvidenceOutcomeKind.ACCEPTED:
            if not self.facts:
                raise ContractViolation("accepted_evidence_requires_facts")
        elif self.facts:
            raise ContractViolation("failed_evidence_must_be_empty")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "evidence_outcome": self.kind.value,
            "evidence_result_count": self.result_count,
            "evidence_fact_count": len(self.facts),
            "evidence_accepted": self.kind is EvidenceOutcomeKind.ACCEPTED,
            "evidence_reason_count": len(self.reason_codes),
        }

    def __repr__(self) -> str:
        return (
            "EvidenceOutcome("
            f"kind={self.kind.value!r}, "
            f"result_count={self.result_count}, "
            f"fact_count={len(self.facts)}, "
            f"reason_count={len(self.reason_codes)})"
        )


def _outcome(
    request: AcquisitionRequest,
    kind: EvidenceOutcomeKind,
    *,
    result_count: int,
    facts: tuple[GroundingFact, ...] = (),
) -> EvidenceOutcome:
    return EvidenceOutcome(
        binding=request.binding,
        action_id=request.action_id,
        kind=kind,
        facts=facts,
        result_count=result_count,
        reason_codes=(kind.value,),
    )


def _stale_binding(expected: DecisionBinding, actual: DecisionBinding) -> bool:
    return bool(
        expected.scope_key == actual.scope_key
        and expected.session_id == actual.session_id
        and expected.current_sender_key == actual.current_sender_key
        and (
            expected.conversation_revision != actual.conversation_revision
            or expected.generation_epoch != actual.generation_epoch
            or expected.current_message_id != actual.current_message_id
            or expected.current_content_digest != actual.current_content_digest
        )
    )


def _clean_protocol_wrappers(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"```(?:json|xml|text)?", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "")
    previous = None
    while previous != text:
        previous = text
        text = _PROTOCOL_BLOCK_RE.sub(" ", text)
    return text.strip()


def _structured_candidates(value: Any) -> list[str]:
    candidates: list[str] = []

    def visit(current: Any, *, allowed_scalar: bool = False) -> None:
        if len(candidates) >= MAX_GROUNDING_FACTS * 3:
            return
        if isinstance(current, dict):
            for raw_key, child in current.items():
                key = str(raw_key or "").strip().lower()
                visit(child, allowed_scalar=key in _ALLOWED_CLAIM_FIELDS)
            return
        if isinstance(current, (list, tuple)):
            for child in current:
                visit(child, allowed_scalar=allowed_scalar)
            return
        if allowed_scalar and isinstance(current, (str, int, float, bool)):
            candidates.append(str(current))

    if isinstance(value, str):
        candidates.append(value)
    else:
        visit(value)
    return candidates


def _claim_candidates(content: str) -> list[str]:
    cleaned = _clean_protocol_wrappers(content)
    if not cleaned:
        return []
    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError, json.JSONDecodeError):
        # JSON-looking or protocol-call residue is not downgraded to plain text.
        stripped = cleaned.lstrip()
        if stripped.startswith(("{", "[")) or _PROTOCOL_CALL_RE.search(cleaned):
            return []
        return [line for line in cleaned.splitlines() if line.strip()]
    return _structured_candidates(parsed)


def _sanitize_claim(value: str) -> str:
    text = _clean_protocol_wrappers(value)
    if not text or _PROTOCOL_CALL_RE.search(text):
        return ""
    if text.lstrip().startswith(("{", "[")):
        return ""
    text = _TOKEN_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _WINDOWS_PATH_RE.sub(" ", text)
    text = _UNIX_PATH_RE.sub(" ", text)
    text = _JSON_KEY_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = text.translate(str.maketrans({"{": " ", "}": " ", "[": " ", "]": " "}))
    text = _WHITESPACE_RE.sub(" ", text).strip(" \t\r\n,，;；:：|-")
    if len(text) < 2:
        return ""
    return text[:MAX_CLAIM_CHARS].rstrip()


def _safe_claims(content: str) -> tuple[str, ...]:
    claims: list[str] = []
    for candidate in _claim_candidates(content):
        claim = _sanitize_claim(candidate)
        if claim and claim not in claims:
            claims.append(claim)
        if len(claims) >= MAX_GROUNDING_FACTS:
            break
    return tuple(claims)


def _normalize_relevance_text(value: str) -> str:
    text = html.unescape(str(value or "")).casefold()
    for pattern, replacement in _RELEVANCE_EQUIVALENTS:
        text = pattern.sub(replacement, text)
    return text


def _query_relevance_terms(value: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return bounded lexical anchors without logging or persisting the query."""

    text = _normalize_relevance_text(value)
    ascii_terms = frozenset(_RELEVANCE_ASCII_RE.findall(text))
    stripped = _RELEVANCE_STOP_RE.sub(" ", text)
    bigrams: set[str] = set()
    strong: set[str] = set()
    for chunk in _RELEVANCE_CJK_RE.findall(stripped):
        if len(chunk) <= 12 and len(chunk) >= 3:
            strong.add(chunk)
        if len(chunk) == 2:
            bigrams.add(chunk)
            continue
        for index in range(len(chunk) - 1):
            bigrams.add(chunk[index : index + 2])
        for index in range(len(chunk) - 2):
            strong.add(chunk[index : index + 3])
    return frozenset((*ascii_terms, *strong)), frozenset(bigrams)


def _claims_relevant_to_query(query: str, claims: tuple[str, ...]) -> bool:
    candidate = _WHITESPACE_RE.sub(
        "",
        _normalize_relevance_text("\n".join(claims)),
    )
    strong, bigrams = _query_relevance_terms(query)
    if not strong and not bigrams:
        return False
    if any(term in candidate for term in strong):
        return True
    matched_bigrams = sum(term in candidate for term in bigrams)
    minimum = 1 if len(bigrams) <= 2 else 2
    return matched_bigrams >= minimum


def _claims_relevant_to_request(
    request: AcquisitionRequest,
    claims: tuple[str, ...],
) -> bool:
    # Exact URL extraction is already bound to the requested URL and cannot be
    # judged by lexical overlap between that URL and its page content.
    if request.kind is AcquisitionKind.EXTRACT:
        return True
    arguments = request.materialize_arguments()
    queries: tuple[str, ...]
    if request.kind is AcquisitionKind.BATCH_SEARCH:
        queries = tuple(
            str(item.get("query", "") or "")
            for item in arguments.get("queries", ())
            if isinstance(item, dict)
        )
    elif request.kind in {AcquisitionKind.SEARCH, AcquisitionKind.KNOWLEDGE_BASE}:
        queries = (str(arguments.get("query", "") or ""),)
    else:
        return True
    return any(_claims_relevant_to_query(query, claims) for query in queries if query)


def _source_kind(result: TypedToolResult) -> str:
    if result.tool_name.startswith("anysearch_"):
        return "anysearch"
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", result.tool_name.lower()).strip("-.")
    return normalized[:96] or "typed_tool"


def _source_digest(request: AcquisitionRequest, result: TypedToolResult) -> str:
    payload = "\x1f".join(
        (
            request.action_id,
            request.request_shape_digest,
            request.selection.tool_name,
            request.selection.source,
            request.selection.capability.value,
            result.call_id,
            result.result_id,
            result.content_digest,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _facts(
    request: AcquisitionRequest,
    result: TypedToolResult,
    claims: tuple[str, ...],
) -> tuple[GroundingFact, ...]:
    source_digest = _source_digest(request, result)
    confidence = (
        0.9
        if result.capability is CapabilityClass.PUBLIC_WEB_READ
        else 0.85
    )
    values: list[GroundingFact] = []
    for index, claim in enumerate(claims):
        identity = hashlib.sha256(
            f"{source_digest}\x1f{index}\x1f{claim}".encode("utf-8")
        ).hexdigest()[:32]
        values.append(
            GroundingFact(
                binding=request.binding,
                fact_id=f"fact-{identity}",
                claim=claim,
                source_kind=_source_kind(result),
                source_digest=source_digest,
                confidence=confidence,
                observed_at=result.observed_at,
            )
        )
    return tuple(values)


def _provenance_failure(value: str) -> EvidenceOutcomeKind | None:
    return {
        "missing_result": EvidenceOutcomeKind.MISSING_RESULT,
        "missing_call_id": EvidenceOutcomeKind.MISSING_RESULT,
        "orphan_result": EvidenceOutcomeKind.ORPHAN_RESULT,
        "tool_error": EvidenceOutcomeKind.TOOL_ERROR,
        "timeout": EvidenceOutcomeKind.TIMEOUT,
        "tool_mismatch": EvidenceOutcomeKind.TOOL_MISMATCH,
        "argument_mismatch": EvidenceOutcomeKind.ARGUMENT_MISMATCH,
        "capability_mismatch": EvidenceOutcomeKind.CAPABILITY_MISMATCH,
        "source_mismatch": EvidenceOutcomeKind.SOURCE_MISMATCH,
        "runtime_tool_missing_or_ambiguous": EvidenceOutcomeKind.SOURCE_MISMATCH,
        "runtime_tool_inactive": EvidenceOutcomeKind.SOURCE_MISMATCH,
        "runtime_tool_interface_changed": EvidenceOutcomeKind.SOURCE_MISMATCH,
        "ambiguous_call_count": EvidenceOutcomeKind.MULTIPLE_RESULTS,
        "ambiguous_result": EvidenceOutcomeKind.MULTIPLE_RESULTS,
        "empty_result": EvidenceOutcomeKind.UNSAFE_CONTENT,
        "unbound_request": EvidenceOutcomeKind.ACTION_MISMATCH,
    }.get(str(value or "").strip().lower())


def adapt_grounding_evidence(
    *,
    request: AcquisitionRequest,
    results: Iterable[TypedToolResult],
    current_binding: DecisionBinding,
    now: float,
) -> EvidenceOutcome:
    """Validate one brokered result and emit only safe same-turn facts."""

    if not isinstance(request, AcquisitionRequest):
        raise TypeError("acquisition_request_required")
    if not isinstance(current_binding, DecisionBinding):
        raise TypeError("current_binding_required")
    try:
        current_time = float(now)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("evidence_clock_invalid") from exc
    if not math.isfinite(current_time) or current_time < 0:
        raise ContractViolation("evidence_clock_invalid")
    try:
        values = tuple(results)
    except TypeError as exc:
        raise TypeError("typed_tool_results_required") from exc
    if any(not isinstance(value, TypedToolResult) for value in values):
        raise TypeError("typed_tool_result_invalid")
    count = len(values)

    if current_binding != request.binding:
        if current_binding.current_sender_key != request.binding.current_sender_key:
            kind = EvidenceOutcomeKind.OTHER_SENDER
        elif _stale_binding(request.binding, current_binding):
            kind = EvidenceOutcomeKind.STALE_BINDING
        else:
            kind = EvidenceOutcomeKind.BINDING_MISMATCH
        return _outcome(request, kind, result_count=count)
    if current_time > request.selection.deadline:
        return _outcome(request, EvidenceOutcomeKind.TIMEOUT, result_count=count)
    if not values:
        return _outcome(request, EvidenceOutcomeKind.MISSING_RESULT, result_count=0)
    if count != 1:
        kind = (
            EvidenceOutcomeKind.ORPHAN_RESULT
            if any(value.provenance_status == "orphan_result" for value in values)
            else EvidenceOutcomeKind.MULTIPLE_RESULTS
        )
        return _outcome(request, kind, result_count=count)

    result = values[0]
    provenance_failure = _provenance_failure(result.provenance_status)
    if provenance_failure is not None:
        return _outcome(request, provenance_failure, result_count=count)
    if result.binding != request.binding:
        if result.binding is not None and _stale_binding(request.binding, result.binding):
            kind = EvidenceOutcomeKind.STALE_BINDING
        elif (
            result.binding is not None
            and result.binding.current_sender_key != request.binding.current_sender_key
        ):
            kind = EvidenceOutcomeKind.OTHER_SENDER
        else:
            kind = EvidenceOutcomeKind.BINDING_MISMATCH
        return _outcome(request, kind, result_count=count)
    if (
        result.action_id != request.action_id
        or result.request_shape_digest != request.request_shape_digest
    ):
        return _outcome(request, EvidenceOutcomeKind.ACTION_MISMATCH, result_count=count)
    expected_arguments = request.materialize_arguments()
    expected_arguments_digest = hashlib.sha256(
        json.dumps(
            expected_arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        not result.arguments_attested
        or result.call_arguments_digest != expected_arguments_digest
    ):
        return _outcome(
            request,
            EvidenceOutcomeKind.ARGUMENT_MISMATCH,
            result_count=count,
        )
    if not math.isclose(
        result.acquisition_deadline,
        request.selection.deadline,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        return _outcome(request, EvidenceOutcomeKind.DEADLINE_MISMATCH, result_count=count)
    if result.observed_at > request.selection.deadline:
        return _outcome(request, EvidenceOutcomeKind.TIMEOUT, result_count=count)
    issued_at = max(0.0, request.selection.deadline - DEFAULT_ACQUISITION_TIMEOUT_S)
    if result.observed_at < issued_at or result.observed_at > current_time:
        return _outcome(request, EvidenceOutcomeKind.INVALID_RESULT, result_count=count)
    if result.target_sender_key != request.binding.current_sender_key:
        return _outcome(request, EvidenceOutcomeKind.OTHER_SENDER, result_count=count)
    if result.scope_key != request.binding.scope_key:
        return _outcome(request, EvidenceOutcomeKind.BINDING_MISMATCH, result_count=count)
    if result.tool_name != request.selection.tool_name:
        return _outcome(request, EvidenceOutcomeKind.TOOL_MISMATCH, result_count=count)
    if result.capability is not request.selection.capability:
        return _outcome(
            request,
            EvidenceOutcomeKind.CAPABILITY_MISMATCH,
            result_count=count,
        )
    expected_side_effect = {
        CapabilityClass.PUBLIC_WEB_READ: SideEffectClass.PUBLIC_READ,
        CapabilityClass.CHAT_RETRIEVAL: SideEffectClass.SCOPED_READ,
    }.get(request.selection.capability)
    if expected_side_effect is None or result.side_effect is not expected_side_effect:
        return _outcome(
            request,
            EvidenceOutcomeKind.CAPABILITY_MISMATCH,
            result_count=count,
        )
    if not result.source_attested or result.tool_source != request.selection.source:
        return _outcome(request, EvidenceOutcomeKind.SOURCE_MISMATCH, result_count=count)
    if result.source_kind != "astrbot_tool_calls_result":
        return _outcome(request, EvidenceOutcomeKind.SOURCE_MISMATCH, result_count=count)
    if result.visibility is not ToolResultVisibility.REFERENCE_ONLY:
        return _outcome(
            request,
            EvidenceOutcomeKind.VISIBILITY_DENIED,
            result_count=count,
        )
    if not result.call_id or not result.result_id:
        return _outcome(request, EvidenceOutcomeKind.MISSING_RESULT, result_count=count)
    if not result.success:
        return _outcome(request, EvidenceOutcomeKind.INVALID_RESULT, result_count=count)
    if hashlib.sha256(result.content.encode("utf-8")).hexdigest() != result.content_digest:
        return _outcome(request, EvidenceOutcomeKind.INVALID_RESULT, result_count=count)

    claims = _safe_claims(result.content)
    if not claims:
        return _outcome(request, EvidenceOutcomeKind.UNSAFE_CONTENT, result_count=count)
    if not _claims_relevant_to_request(request, claims):
        return _outcome(
            request,
            EvidenceOutcomeKind.IRRELEVANT_RESULT,
            result_count=count,
        )
    facts = _facts(request, result, claims)
    return _outcome(
        request,
        EvidenceOutcomeKind.ACCEPTED,
        result_count=count,
        facts=facts,
    )


__all__ = [
    "MAX_CLAIM_CHARS",
    "MAX_GROUNDING_FACTS",
    "EvidenceOutcome",
    "EvidenceOutcomeKind",
    "adapt_grounding_evidence",
]
