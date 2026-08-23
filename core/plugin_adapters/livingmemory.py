from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..context_assembler import ProvenancedFact
from ..contracts import DecisionBinding, PluginEvidenceStatus
from ..identity import build_sender_key


EXPECTED_PLUGIN_NAME = "astrbot_plugin_livingmemory"
DEFAULT_TIMEOUT_SECONDS = 1.5
MAX_RECENT_LIMIT = 20


class LivingMemoryAdapterReason(str, Enum):
    PLUGIN_MISSING = "plugin_missing"
    PLUGIN_DISABLED = "plugin_disabled"
    READER_INTERFACE_CHANGED = "reader_interface_changed"
    HOOK_ORDER_INVALID = "hook_order_invalid"
    READ_TIMEOUT = "read_timeout"
    READ_ERROR = "read_error"
    READ_INTERFACE_CHANGED = "read_interface_changed"


class LivingMemoryRecordOrigin(str, Enum):
    RECENT = "recent"
    PROVIDED_RECALL = "provided_recall"


@dataclass(frozen=True, slots=True, repr=False)
class LivingMemoryReadRequest:
    """Transport-only request. Its repr never exposes structural identifiers."""

    binding: DecisionBinding = field(repr=False)
    recent_limit: int

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DecisionBinding):
            raise TypeError("memory_binding_required")
        if (
            isinstance(self.recent_limit, bool)
            or not isinstance(self.recent_limit, int)
            or not 1 <= self.recent_limit <= MAX_RECENT_LIMIT
        ):
            raise ValueError("memory_recent_limit_invalid")

    @property
    def session_id(self) -> str:
        return self.binding.session_id

    @property
    def scope_key(self) -> str:
        return self.binding.scope_key

    @property
    def current_sender_key(self) -> str:
        return self.binding.current_sender_key

    def __repr__(self) -> str:
        return (
            "LivingMemoryReadRequest("
            f"recent_limit={self.recent_limit}, bound=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LivingMemoryCandidate:
    """Typed candidate retaining content only for policy selection, never repr."""

    fact: ProvenancedFact = field(repr=False)
    origin: LivingMemoryRecordOrigin
    relevance: float

    def __post_init__(self) -> None:
        if not isinstance(self.fact, ProvenancedFact):
            raise TypeError("memory_fact_required")
        if not isinstance(self.origin, LivingMemoryRecordOrigin):
            raise TypeError("memory_origin_invalid")
        if isinstance(self.relevance, bool) or not isinstance(
            self.relevance,
            (int, float),
        ):
            raise TypeError("memory_relevance_invalid")
        bounded = float(self.relevance)
        if not 0.0 <= bounded <= 1.0:
            raise ValueError("memory_relevance_invalid")
        object.__setattr__(self, "relevance", bounded)

    def __repr__(self) -> str:
        return (
            "LivingMemoryCandidate("
            f"origin={self.origin.value!r}, relevance={self.relevance:.3f}, "
            f"has_subject={bool(self.fact.subject_key)})"
        )


def _normalize_counts(
    counts: Mapping[str, int] | Sequence[tuple[str, int]] | None,
) -> tuple[tuple[str, int], ...]:
    normalized: Counter[str] = Counter()
    items = counts.items() if isinstance(counts, Mapping) else tuple(counts or ())
    for raw_reason, raw_count in items:
        reason = str(raw_reason or "").strip().lower()
        if not reason or not re.fullmatch(r"[a-z0-9_]+", reason):
            raise ValueError("memory_exclusion_reason_invalid")
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 1
        ):
            raise ValueError("memory_exclusion_count_invalid")
        normalized[reason] += raw_count
    return tuple(sorted(normalized.items()))


@dataclass(frozen=True, slots=True, repr=False)
class LivingMemoryAdapterResult:
    status: PluginEvidenceStatus
    candidates: tuple[LivingMemoryCandidate, ...] = field(
        default=(),
        repr=False,
    )
    read_count: int = 0
    recent_record_count: int = 0
    provided_record_count: int = 0
    exclusion_counts: tuple[tuple[str, int], ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, PluginEvidenceStatus):
            raise TypeError("memory_adapter_status_invalid")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if any(
            not isinstance(candidate, LivingMemoryCandidate)
            for candidate in self.candidates
        ):
            raise TypeError("memory_candidate_invalid")
        for name, upper in (
            ("read_count", 1),
            ("recent_record_count", 10_000),
            ("provided_record_count", 10_000),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= upper
            ):
                raise ValueError(f"memory_{name}_invalid")
        object.__setattr__(
            self,
            "exclusion_counts",
            _normalize_counts(self.exclusion_counts),
        )
        reasons = tuple(
            dict.fromkeys(
                str(reason or "").strip().lower()
                for reason in self.reason_codes
                if str(reason or "").strip()
            )
        )
        if any(not re.fullmatch(r"[a-z0-9_]+", reason) for reason in reasons):
            raise ValueError("memory_adapter_reason_invalid")
        object.__setattr__(self, "reason_codes", reasons)
        if self.status is PluginEvidenceStatus.VERIFIED:
            if reasons:
                raise ValueError("verified_memory_adapter_reason_forbidden")
        else:
            if self.candidates:
                raise ValueError("degraded_memory_candidates_forbidden")
            if not reasons:
                raise ValueError("degraded_memory_reason_required")

    @classmethod
    def degraded(
        cls,
        status: PluginEvidenceStatus,
        *,
        reason_code: str,
        read_count: int = 0,
        exclusion_counts: Mapping[str, int] | None = None,
    ) -> "LivingMemoryAdapterResult":
        if status is PluginEvidenceStatus.VERIFIED:
            raise ValueError("degraded_memory_status_required")
        return cls(
            status=status,
            read_count=read_count,
            exclusion_counts=tuple((exclusion_counts or {}).items()),
            reason_codes=(reason_code,),
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "memory_plugin_status": self.status.value,
            "memory_read_count": self.read_count,
            "memory_recent_record_count": self.recent_record_count,
            "memory_provided_record_count": self.provided_record_count,
            "memory_candidate_count": len(self.candidates),
            "memory_exclusion_count": sum(
                count for _, count in self.exclusion_counts
            ),
            "memory_adapter_degraded": (
                self.status is not PluginEvidenceStatus.VERIFIED
            ),
            "memory_adapter_reason_count": len(self.reason_codes),
        }

    def __repr__(self) -> str:
        return (
            "LivingMemoryAdapterResult("
            f"status={self.status.value!r}, candidates={len(self.candidates)}, "
            f"read_count={self.read_count}, "
            f"excluded={sum(count for _, count in self.exclusion_counts)})"
        )


MemoryReader = Callable[[LivingMemoryReadRequest], Awaitable[Any] | Any]


def _score(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(1.0, max(0.0, parsed))


def _mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        mapped = to_dict()
    except Exception:
        return None
    return mapped if isinstance(mapped, Mapping) else None


def _metadata(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("metadata", {})
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def _first(row: Mapping[str, Any], metadata: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
        value = metadata.get(name)
        if value not in (None, ""):
            return value
    return None


_BOT_KINDS = {"self", "known_bot", "bot", "automation", "unknown_automation"}
_PLUGIN_KINDS = {
    "plugin_echo",
    "external_plugin_output",
    "parser",
    "keywords",
    "progress",
    "management",
    "meme_manager",
    "meme_query",
    "tool",
    "tool_result",
    "presentation_effect",
}


def _unsafe_reason(
    row: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> str:
    if _first(row, metadata, "banned", "is_banned") is True:
        return "banned_source"
    sender_kind = str(
        _first(row, metadata, "sender_kind", "author_kind") or ""
    ).strip().lower()
    role = str(row.get("role", "") or "").strip().lower()
    if (
        sender_kind in _BOT_KINDS
        or _first(row, metadata, "is_bot", "is_bot_message") is True
        or role == "assistant"
    ):
        return "bot_source"
    plugin_source = str(
        _first(row, metadata, "plugin_source", "external_source") or ""
    ).strip().lower()
    source_kind = str(
        _first(row, metadata, "source_kind", "source_type") or ""
    ).strip().lower()
    if role in {"tool", "system"}:
        return "plugin_source"
    if plugin_source and plugin_source not in {"none", "human", "inbound"}:
        return "plugin_source"
    if source_kind in _PLUGIN_KINDS or source_kind.startswith("plugin_"):
        return "plugin_source"
    return ""


def _content(row: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    raw = _first(
        row,
        metadata,
        "content",
        "text",
        "memory",
        "memory_content",
    )
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:4000]


def _adapt_candidate(
    raw: object,
    *,
    binding: DecisionBinding,
    origin: LivingMemoryRecordOrigin,
) -> tuple[LivingMemoryCandidate | None, str]:
    row = _mapping(raw)
    if row is None:
        return None, "record_interface_changed"
    metadata = _metadata(row)
    unsafe = _unsafe_reason(row, metadata)
    if unsafe:
        return None, unsafe
    if origin is LivingMemoryRecordOrigin.RECENT:
        role = str(row.get("role", "user") or "").strip().lower()
        if role != "user":
            return None, "nonhuman_recent_role"
    record_session = str(
        _first(row, metadata, "session_id") or ""
    ).strip()
    if record_session and record_session != binding.session_id:
        return None, "foreign_session"

    content = _content(row, metadata)
    if not content:
        return None, "empty_content"
    explicit_subject = str(
        _first(row, metadata, "subject_key") or ""
    ).strip()
    if explicit_subject:
        if not explicit_subject.startswith(f"{binding.scope_key}|user:"):
            return None, "foreign_scope"
        subject_key = explicit_subject
    else:
        sender_id = str(
            _first(row, metadata, "sender_id", "user_id") or ""
        ).strip()
        subject_key = build_sender_key(binding.scope_key, sender_id)

    raw_scope = str(_first(row, metadata, "scope") or "").strip().lower()
    if subject_key:
        if raw_scope in {"", "personal"}:
            fact_scope = "personal"
        elif raw_scope == "owner_private":
            fact_scope = "owner_private"
        else:
            return None, "subject_scope_invalid"
    else:
        if origin is LivingMemoryRecordOrigin.RECENT:
            return None, "missing_subject"
        if raw_scope == "personal":
            return None, "missing_subject"
        if raw_scope not in {"group", "group_background", "global", "public"}:
            return None, "missing_subject"
        fact_scope = raw_scope

    default_confidence = (
        0.75 if origin is LivingMemoryRecordOrigin.RECENT else 0.5
    )
    confidence = _score(
        _first(row, metadata, "confidence", "importance"),
        default_confidence,
    )
    relevance = _score(
        _first(row, metadata, "relevance", "score", "final_score"),
        0.0,
    )
    try:
        observed_at = float(
            _first(row, metadata, "observed_at", "timestamp", "create_time")
            or 0.0
        )
    except (TypeError, ValueError):
        observed_at = 0.0
    raw_id = str(
        _first(row, metadata, "source_id", "memory_id", "doc_id", "id")
        or ""
    ).strip()
    source_id = raw_id or hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]
    fact = ProvenancedFact(
        subject_key=subject_key,
        scope=fact_scope,
        content=content,
        source_kind=(
            "livingmemory_recent"
            if origin is LivingMemoryRecordOrigin.RECENT
            else "livingmemory_semantic"
        ),
        source_id=source_id,
        observed_at=max(0.0, observed_at),
        confidence=confidence,
    )
    return LivingMemoryCandidate(fact=fact, origin=origin, relevance=relevance), ""


def _provided_rows(value: object) -> tuple[list[object], Counter[str]]:
    rows: list[object] = []
    excluded: Counter[str] = Counter()
    if value is None:
        return rows, excluded
    if isinstance(value, Mapping):
        results = value.get("results")
        if isinstance(results, list):
            rows.extend(results)
        else:
            excluded["provided_interface_changed"] += 1
        return rows, excluded
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rows.extend(value)
        return rows, excluded

    contexts = getattr(value, "contexts", None)
    if contexts is not None:
        try:
            context_items = list(contexts or [])
        except (TypeError, ValueError):
            context_items = []
            excluded["provided_interface_changed"] += 1
        for item in context_items:
            mapped = _mapping(item)
            if mapped is None:
                continue
            if str(mapped.get("role", "") or "").strip().lower() != "tool":
                continue
            if str(mapped.get("name", "") or "").strip() != "recall_long_term_memory":
                continue
            call_id = str(mapped.get("tool_call_id", "") or "").strip()
            if not call_id.startswith("fake_recall_"):
                excluded["historical_recall_not_current"] += 1
                continue
            try:
                payload = json.loads(str(mapped.get("content", "") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                excluded["provided_interface_changed"] += 1
                continue
            results = payload.get("results") if isinstance(payload, Mapping) else None
            if not isinstance(results, list):
                excluded["provided_interface_changed"] += 1
                continue
            rows.extend(results)

    saw_unstructured = False
    try:
        parts = list(getattr(value, "extra_user_content_parts", None) or [])
    except (TypeError, ValueError):
        parts = []
        excluded["provided_interface_changed"] += 1
    for part in parts:
        text = part.get("text", "") if isinstance(part, Mapping) else getattr(part, "text", "")
        lowered = str(text or "").lower()
        if "<rag-faiss-memory>" in lowered or "livingmemory" in lowered:
            saw_unstructured = True
    prompt = str(getattr(value, "prompt", "") or "")
    if "<rag-faiss-memory>" in prompt.lower():
        saw_unstructured = True
    if saw_unstructured:
        excluded["unstructured_recall"] += 1
    return rows, excluded


def has_provided_recall(value: object) -> bool:
    """Detect only current LivingMemory transport markers, never prose meaning."""

    if value is None:
        return False
    if isinstance(value, Mapping):
        return isinstance(value.get("results"), list)
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return bool(value)
    try:
        contexts = list(getattr(value, "contexts", None) or [])
    except (TypeError, ValueError):
        contexts = []
    for item in contexts:
        mapped = _mapping(item)
        if mapped is None:
            continue
        if str(mapped.get("role", "") or "").strip().lower() != "tool":
            continue
        if str(mapped.get("name", "") or "").strip() != "recall_long_term_memory":
            continue
        if str(mapped.get("tool_call_id", "") or "").startswith("fake_recall_"):
            return True
    try:
        parts = list(getattr(value, "extra_user_content_parts", None) or [])
    except (TypeError, ValueError):
        parts = []
    for part in parts:
        text = (
            part.get("text", "")
            if isinstance(part, Mapping)
            else getattr(part, "text", "")
        )
        if "<rag-faiss-memory>" in str(text or "").lower():
            return True
    return "<rag-faiss-memory>" in str(
        getattr(value, "prompt", "") or ""
    ).lower()


class LivingMemoryAdapter:
    """Single-read compatibility boundary for LivingMemory 2.x shapes."""

    __slots__ = ("_status", "_reader", "_reason_codes", "_timeout_seconds")

    def __init__(
        self,
        *,
        status: PluginEvidenceStatus,
        reader: MemoryReader | None = None,
        reason_codes: Sequence[str] = (),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(status, PluginEvidenceStatus):
            raise TypeError("memory_adapter_status_invalid")
        if reader is not None and not callable(reader):
            raise TypeError("memory_reader_invalid")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("memory_timeout_invalid") from exc
        if not 0.0 < timeout <= 30.0:
            raise ValueError("memory_timeout_invalid")
        normalized_reasons = tuple(
            dict.fromkeys(str(reason or "").strip().lower() for reason in reason_codes)
        )
        if status is PluginEvidenceStatus.VERIFIED and normalized_reasons:
            raise ValueError("verified_memory_adapter_reason_forbidden")
        if status is not PluginEvidenceStatus.VERIFIED and not normalized_reasons:
            raise ValueError("degraded_memory_reason_required")
        self._status = status
        self._reader = reader
        self._reason_codes = normalized_reasons
        self._timeout_seconds = timeout

    @property
    def status(self) -> PluginEvidenceStatus:
        return self._status

    @classmethod
    def verified(
        cls,
        *,
        reader: MemoryReader | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> "LivingMemoryAdapter":
        return cls(
            status=PluginEvidenceStatus.VERIFIED,
            reader=reader,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def degraded(
        cls,
        status: PluginEvidenceStatus,
        *,
        reason_code: str | None = None,
    ) -> "LivingMemoryAdapter":
        if status is PluginEvidenceStatus.VERIFIED:
            raise ValueError("degraded_memory_status_required")
        return cls(
            status=status,
            reason_codes=(reason_code or f"plugin_{status.value}",),
        )

    async def collect(
        self,
        *,
        binding: DecisionBinding,
        provided_recall: object = None,
        include_recent: bool,
        include_semantic: bool,
        recent_limit: int,
    ) -> LivingMemoryAdapterResult:
        if not isinstance(binding, DecisionBinding):
            raise TypeError("memory_binding_required")
        if type(include_recent) is not bool or type(include_semantic) is not bool:
            raise TypeError("memory_collection_flag_invalid")
        request = LivingMemoryReadRequest(
            binding=binding,
            recent_limit=recent_limit,
        )
        if self._status is not PluginEvidenceStatus.VERIFIED:
            return LivingMemoryAdapterResult.degraded(
                self._status,
                reason_code=self._reason_codes[0],
            )

        candidates: list[LivingMemoryCandidate] = []
        exclusions: Counter[str] = Counter()
        provided_count = 0
        recent_count = 0
        if include_semantic:
            provided_rows, provided_exclusions = _provided_rows(provided_recall)
            exclusions.update(provided_exclusions)
            provided_count = len(provided_rows)
            for row in provided_rows:
                candidate, reason = _adapt_candidate(
                    row,
                    binding=binding,
                    origin=LivingMemoryRecordOrigin.PROVIDED_RECALL,
                )
                if candidate is None:
                    exclusions[reason] += 1
                else:
                    candidates.append(candidate)

        read_count = 0
        if include_recent:
            if self._reader is None:
                return LivingMemoryAdapterResult.degraded(
                    PluginEvidenceStatus.INTERFACE_CHANGED,
                    reason_code=LivingMemoryAdapterReason.READER_INTERFACE_CHANGED.value,
                    exclusion_counts=exclusions,
                )
            read_count = 1
            try:
                raw_result = self._reader(request)
                if inspect.isawaitable(raw_result):
                    raw_result = await asyncio.wait_for(
                        raw_result,
                        timeout=self._timeout_seconds,
                    )
            except (TimeoutError, asyncio.TimeoutError):
                return LivingMemoryAdapterResult.degraded(
                    PluginEvidenceStatus.TIMEOUT,
                    reason_code=LivingMemoryAdapterReason.READ_TIMEOUT.value,
                    read_count=1,
                    exclusion_counts=exclusions,
                )
            except (TypeError, AttributeError):
                return LivingMemoryAdapterResult.degraded(
                    PluginEvidenceStatus.INTERFACE_CHANGED,
                    reason_code=LivingMemoryAdapterReason.READ_INTERFACE_CHANGED.value,
                    read_count=1,
                    exclusion_counts=exclusions,
                )
            except Exception:
                return LivingMemoryAdapterResult.degraded(
                    PluginEvidenceStatus.ERROR,
                    reason_code=LivingMemoryAdapterReason.READ_ERROR.value,
                    read_count=1,
                    exclusion_counts=exclusions,
                )
            if not isinstance(raw_result, (list, tuple)):
                return LivingMemoryAdapterResult.degraded(
                    PluginEvidenceStatus.INTERFACE_CHANGED,
                    reason_code=LivingMemoryAdapterReason.READ_INTERFACE_CHANGED.value,
                    read_count=1,
                    exclusion_counts=exclusions,
                )
            recent_count = len(raw_result)
            for row in raw_result:
                candidate, reason = _adapt_candidate(
                    row,
                    binding=binding,
                    origin=LivingMemoryRecordOrigin.RECENT,
                )
                if candidate is None:
                    exclusions[reason] += 1
                else:
                    candidates.append(candidate)

        return LivingMemoryAdapterResult(
            status=PluginEvidenceStatus.VERIFIED,
            candidates=tuple(candidates),
            read_count=read_count,
            recent_record_count=recent_count,
            provided_record_count=provided_count,
            exclusion_counts=tuple(exclusions.items()),
        )

    def __repr__(self) -> str:
        return (
            "LivingMemoryAdapter("
            f"status={self._status.value!r}, "
            f"reader_available={self._reader is not None})"
        )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EXPECTED_PLUGIN_NAME",
    "MAX_RECENT_LIMIT",
    "LivingMemoryAdapter",
    "LivingMemoryAdapterReason",
    "LivingMemoryAdapterResult",
    "LivingMemoryCandidate",
    "LivingMemoryReadRequest",
    "LivingMemoryRecordOrigin",
    "has_provided_recall",
]
