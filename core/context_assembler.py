from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from .conversation_ledger import (
    LedgerRecord,
    LedgerRole,
    ledger_content_digest,
    records_for_sender_thread,
)
from .identity import TurnEnvelope, build_sender_key


REPLY_TARGET_EXTRA = "_shio_reply_target_v2"
REFERENCE_CONTEXT_EXTRA = "_shio_reference_context_v2"


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """The exact current message and principal a generated reply must answer."""

    message_id: str
    sender_key: str
    session_id: str
    scope_key: str
    content_digest: str
    source_kind: str
    referenced_message_id: str
    degradation_reasons: tuple[str, ...]

    @property
    def is_actionable(self) -> bool:
        return not self.degradation_reasons


@dataclass(frozen=True, slots=True)
class ReferenceContext:
    """A quoted message edge that never replaces the current principal."""

    message_id: str
    sender_key: str
    scope_key: str
    source_kind: str
    degradation_reasons: tuple[str, ...]

    @property
    def attribution_status(self) -> str:
        return "verified_sender" if self.sender_key else "unknown_sender"


@dataclass(frozen=True, slots=True)
class ProvenancedFact:
    subject_key: str
    scope: str
    content: str
    source_kind: str
    source_id: str
    observed_at: float
    confidence: float

    @property
    def has_verified_subject(self) -> bool:
        return bool(self.subject_key)


@dataclass(frozen=True, slots=True)
class FactSelection:
    must_include_candidates: tuple[ProvenancedFact, ...]
    uncertain_target_facts: tuple[ProvenancedFact, ...]
    public_background: tuple[ProvenancedFact, ...]
    other_subject_facts: tuple[ProvenancedFact, ...]


@dataclass(frozen=True, slots=True)
class AssembledContext:
    reply_target: ReplyTarget
    reference: ReferenceContext | None
    planner_records: tuple[LedgerRecord, ...]
    replyer_thread: tuple[LedgerRecord, ...]
    public_background: tuple[LedgerRecord, ...]
    fact_selection: FactSelection


def build_direct_reply_target(envelope: TurnEnvelope, content: str) -> ReplyTarget:
    """Bind a direct reply to the current inbound turn, never its quote."""

    degradation_reasons: list[str] = []
    for value, reason in (
        (envelope.message_id, "missing_message_id"),
        (envelope.sender_key, "missing_sender_key"),
        (envelope.session_id, "missing_session_id"),
        (envelope.scope_key, "missing_scope_key"),
    ):
        if not value:
            degradation_reasons.append(reason)
    return ReplyTarget(
        message_id=envelope.message_id,
        sender_key=envelope.sender_key,
        session_id=envelope.session_id,
        scope_key=envelope.scope_key,
        content_digest=ledger_content_digest(content),
        source_kind="current_inbound",
        referenced_message_id=envelope.reply_to_message_id,
        degradation_reasons=tuple(degradation_reasons),
    )


def ensure_direct_reply_target(
    event: Any,
    envelope: TurnEnvelope,
    content: str,
) -> ReplyTarget:
    get_extra = getattr(event, "get_extra", None)
    if callable(get_extra):
        existing = get_extra(REPLY_TARGET_EXTRA, None)
        if isinstance(existing, ReplyTarget):
            return existing
    target = build_direct_reply_target(envelope, content)
    set_extra = getattr(event, "set_extra", None)
    if callable(set_extra):
        set_extra(REPLY_TARGET_EXTRA, target)
    return target


def build_reference_context(envelope: TurnEnvelope) -> ReferenceContext | None:
    if not envelope.reply_to_message_id:
        return None
    sender_key = build_sender_key(
        envelope.scope_key,
        envelope.reply_to_sender_id,
    )
    degradation_reasons: list[str] = []
    if not envelope.scope_key:
        degradation_reasons.append("missing_scope_key")
    if not envelope.reply_to_sender_id:
        degradation_reasons.append("missing_referenced_sender_id")
    if not sender_key:
        degradation_reasons.append("referenced_sender_key_unavailable")
    return ReferenceContext(
        message_id=envelope.reply_to_message_id,
        sender_key=sender_key,
        scope_key=envelope.scope_key,
        source_kind="quoted_message",
        degradation_reasons=tuple(degradation_reasons),
    )


def ensure_reference_context(
    event: Any,
    envelope: TurnEnvelope,
) -> ReferenceContext | None:
    get_extra = getattr(event, "get_extra", None)
    if callable(get_extra):
        existing = get_extra(REFERENCE_CONTEXT_EXTRA, None)
        if isinstance(existing, ReferenceContext):
            return existing
    reference = build_reference_context(envelope)
    set_extra = getattr(event, "set_extra", None)
    if reference is not None and callable(set_extra):
        set_extra(REFERENCE_CONTEXT_EXTRA, reference)
    return reference


def _fact_source_id(content: str, explicit_id: object = "") -> str:
    normalized = str(explicit_id or "").strip()
    return normalized or ledger_content_digest(content)[:20]


def _bounded_confidence(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(1.0, max(0.0, parsed))


def _structured_fact(
    item: dict[str, Any],
    *,
    scope_key: str,
    source_kind: str,
    default_observed_at: float,
) -> ProvenancedFact | None:
    content = str(
        item.get("content", "")
        or item.get("text", "")
        or item.get("memory", "")
        or ""
    ).strip()
    if not content:
        return None

    explicit_subject = str(item.get("subject_key", "") or "").strip()
    if explicit_subject.startswith(f"{scope_key}|user:"):
        subject_key = explicit_subject
    else:
        sender_id = str(
            item.get("sender_id", "") or item.get("user_id", "") or ""
        ).strip()
        subject_key = build_sender_key(scope_key, sender_id)

    raw_scope = str(item.get("scope", "") or "").strip().lower()
    if subject_key:
        fact_scope = "personal"
        confidence = _bounded_confidence(
            item.get("confidence", item.get("score")),
            0.7,
        )
    else:
        fact_scope = raw_scope if raw_scope in {"group", "global", "public"} else "group_background"
        confidence = min(
            0.35,
            _bounded_confidence(
                item.get("confidence", item.get("score")),
                0.25,
            ),
        )
    try:
        observed_at = float(
            item.get("observed_at", item.get("timestamp", default_observed_at)) or 0
        )
    except (TypeError, ValueError):
        observed_at = float(default_observed_at or 0)
    return ProvenancedFact(
        subject_key=subject_key,
        scope=fact_scope,
        content=content[:4000],
        source_kind=source_kind,
        source_id=_fact_source_id(
            content,
            item.get("source_id", item.get("memory_id", item.get("id", ""))),
        ),
        observed_at=max(0.0, observed_at),
        confidence=confidence,
    )


def _facts_from_payload(
    payload_text: str,
    *,
    scope_key: str,
    source_kind: str,
    observed_at: float,
) -> list[ProvenancedFact]:
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    candidates: list[object]
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        candidates = list(payload["results"])
    elif isinstance(payload, list):
        candidates = list(payload)
    elif isinstance(payload, dict):
        candidates = [payload]
    else:
        candidates = []
    facts = [
        fact
        for item in candidates
        if isinstance(item, dict)
        if (
            fact := _structured_fact(
                item,
                scope_key=scope_key,
                source_kind=source_kind,
                default_observed_at=observed_at,
            )
        )
        is not None
    ]
    if facts or not str(payload_text or "").strip():
        return facts
    content = str(payload_text).strip()[:4000]
    return [
        ProvenancedFact(
            subject_key="",
            scope="group_background",
            content=content,
            source_kind=source_kind,
            source_id=_fact_source_id(content),
            observed_at=max(0.0, float(observed_at or 0)),
            confidence=0.2,
        )
    ]


def adapt_livingmemory_facts(
    request: Any,
    *,
    scope_key: str,
    observed_at: float = 0.0,
) -> tuple[ProvenancedFact, ...]:
    """Adapt recalls without guessing a person from prose or adjacency."""

    normalized_scope = str(scope_key or "").strip()
    if not normalized_scope:
        return ()
    facts: list[ProvenancedFact] = []
    for item in list(getattr(request, "contexts", None) or []):
        if not isinstance(item, dict) or str(item.get("role", "")) != "tool":
            continue
        tool_call_id = str(item.get("tool_call_id", "") or "")
        tool_name = str(item.get("name", "") or "")
        if not (
            tool_call_id.startswith("fake_recall_")
            or tool_name == "recall_long_term_memory"
        ):
            continue
        facts.extend(
            _facts_from_payload(
                str(item.get("content", "") or ""),
                scope_key=normalized_scope,
                source_kind="livingmemory_tool_recall",
                observed_at=observed_at,
            )
        )

    memory_blocks: list[str] = []
    for part in list(getattr(request, "extra_user_content_parts", None) or []):
        text = getattr(part, "text", None)
        if text is None and isinstance(part, dict):
            text = part.get("text", "")
        normalized = str(text or "").strip()
        lowered = normalized.lower()
        if normalized and (
            "rag-faiss-memory" in lowered
            or "livingmemory" in lowered
            or "<memory" in lowered
            or "[memory" in lowered
        ):
            memory_blocks.append(normalized)
    prompt = str(getattr(request, "prompt", "") or "")
    memory_blocks.extend(
        block.strip()
        for block in re.findall(
            r"<RAG-Faiss-Memory>.*?</RAG-Faiss-Memory>",
            prompt,
            flags=re.DOTALL | re.IGNORECASE,
        )
    )
    for block in memory_blocks:
        facts.extend(
            _facts_from_payload(
                block,
                scope_key=normalized_scope,
                source_kind="livingmemory_unstructured_recall",
                observed_at=observed_at,
            )
        )

    deduped: list[ProvenancedFact] = []
    seen: set[tuple[str, str, str]] = set()
    for fact in facts:
        key = (fact.source_kind, fact.source_id, fact.content)
        if key not in seen:
            seen.add(key)
            deduped.append(fact)
    return tuple(deduped)


def select_facts_for_target(
    facts: list[ProvenancedFact] | tuple[ProvenancedFact, ...],
    *,
    target_sender_key: str,
    minimum_personal_confidence: float = 0.5,
) -> FactSelection:
    """Partition facts so only verified current-subject facts can be required."""

    target_key = str(target_sender_key or "").strip()
    threshold = min(1.0, max(0.0, float(minimum_personal_confidence)))
    must_include: list[ProvenancedFact] = []
    uncertain_target: list[ProvenancedFact] = []
    public_background: list[ProvenancedFact] = []
    other_subjects: list[ProvenancedFact] = []
    for fact in facts:
        if fact.subject_key:
            if target_key and fact.subject_key == target_key:
                if fact.scope == "personal" and fact.confidence >= threshold:
                    must_include.append(fact)
                else:
                    uncertain_target.append(fact)
            else:
                other_subjects.append(fact)
            continue
        public_background.append(
            replace(
                fact,
                scope=(
                    fact.scope
                    if fact.scope in {"group", "global", "public", "group_background"}
                    else "group_background"
                ),
                confidence=min(0.35, fact.confidence),
            )
        )
    return FactSelection(
        must_include_candidates=tuple(must_include),
        uncertain_target_facts=tuple(uncertain_target),
        public_background=tuple(public_background),
        other_subject_facts=tuple(other_subjects),
    )


def assemble_context_views(
    records: list[LedgerRecord] | tuple[LedgerRecord, ...],
    *,
    reply_target: ReplyTarget,
    reference: ReferenceContext | None,
    fact_selection: FactSelection,
    planner_record_limit: int = 32,
    replyer_record_limit: int = 16,
    background_record_limit: int = 12,
) -> AssembledContext:
    """Build separate typed views without adjacency-based attribution."""

    scoped: list[LedgerRecord] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for record in records:
        if record.scope_key != reply_target.scope_key:
            continue
        dedupe_key = (
            record.source_kind.value,
            record.message_id,
            record.sender_key,
            record.target_sender_key,
            record.content_digest,
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        scoped.append(record)

    planner_records = tuple(scoped[-max(1, int(planner_record_limit)) :])
    thread_candidates = records_for_sender_thread(
        planner_records,
        reply_target.sender_key,
    )
    replyer_thread = tuple(
        record
        for record in thread_candidates
        if not (
            record.role == LedgerRole.USER
            and record.message_id
            and record.message_id == reply_target.message_id
        )
    )[-max(1, int(replyer_record_limit)) :]
    thread_records = set(replyer_thread)
    public_background = tuple(
        record
        for record in planner_records
        if record not in thread_records
        and not (
            record.role == LedgerRole.USER
            and record.message_id
            and record.message_id == reply_target.message_id
        )
        and record.role in {LedgerRole.USER, LedgerRole.ASSISTANT}
    )[-max(0, int(background_record_limit)) :]
    return AssembledContext(
        reply_target=reply_target,
        reference=reference,
        planner_records=planner_records,
        replyer_thread=replyer_thread,
        public_background=public_background,
        fact_selection=fact_selection,
    )
