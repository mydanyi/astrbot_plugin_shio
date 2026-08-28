from __future__ import annotations

import hashlib
import re
import threading
import weakref
from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple

from .answer_obligation import inspect_answer_obligation
from .answer_obligation import AttributionRisk
from .contracts import SemanticAtomKind
from .context_assembler import AssembledContext
from .conversation_ledger import (
    LedgerRole,
    _matches_identity_literal,
    acknowledges_unavailable_group_context,
    has_verified_public_group_context,
    is_explicit_group_context_request,
)
from .current_question_anchor import (
    AnchorCoverage,
    CurrentQuestionAnchor,
    QuestionRelation,
    extract_question_relations,
)
from .dialogue_quality import find_dialogue_repetition
from .persona import CatchphraseRule, LanguagePreferences, PersonaPackage
from .reply_composer import (
    ReplyComposerRequest,
    ReplyComposerResult,
)
from .response_guard import (
    contains_internal_reasoning,
    contains_nonowner_identity_confusion,
    contains_nonowner_intimacy,
    contains_tool_protocol,
    contains_unsupported_market_claim,
    contains_unsupported_personal_experience,
)
from .semantic_guard import (
    SemanticGuardContract,
    SemanticGuardIssue,
    SemanticGuardPhase,
    SemanticGuardReport,
    SemanticGuardSeverity,
    inspect_semantic_guard_contract,
    validate_semantic_media_guard,
)


HIDDEN_REASONING_CHANNEL_RE = re.compile(
    r"<\s*(?:[|｜]\s*channel\s*[|｜]?|channel\s*[|｜])\s*>\s*"
    r"(?:analysis|thought|commentary)",
    re.IGNORECASE,
)


class OutputIssueSeverity(str, Enum):
    REPAIRABLE = "repairable"
    BLOCKING = "blocking"


class OutputDisposition(str, Enum):
    PASS = "pass"
    REPAIR_ONCE = "repair_once"
    BLOCK = "block"


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class OutputValidationContext:
    composer_request: ReplyComposerRequest = field(repr=False)
    current_message: str = field(repr=False)
    expected_target_message_id: str = field(repr=False)
    expected_target_sender_key: str = field(repr=False)
    is_owner: bool
    semantic_contract: SemanticGuardContract = field(repr=False)
    current_question_anchor: CurrentQuestionAnchor = field(repr=False)
    grounding_facts: tuple[str, ...] = ()
    forbidden_fact_fragments: tuple[str, ...] = ()
    context_reference_fragments: tuple[str, ...] = ()
    raw_identity_ids: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "OutputValidationContext("
            f"is_owner={self.is_owner}, "
            f"grounding_fact_count={len(self.grounding_facts)}, "
            f"forbidden_fact_count={len(self.forbidden_fact_fragments)}, "
            f"context_reference_count={len(self.context_reference_fragments)}, "
            "typed_semantic_contract=True, exact_composer_request=True)"
        )


def _validation_context_snapshot(
    context: OutputValidationContext,
) -> tuple[object, ...]:
    if type(context) is not OutputValidationContext:
        raise ValueError("output_validation_context_invalid")
    return (
        context.current_message,
        context.expected_target_message_id,
        context.expected_target_sender_key,
        context.is_owner,
        context.grounding_facts,
        context.forbidden_fact_fragments,
        context.context_reference_fragments,
        context.raw_identity_ids,
    )


class _ValidationContextRecord(NamedTuple):
    context_ref: weakref.ReferenceType
    request_ref: weakref.ReferenceType
    contract_ref: weakref.ReferenceType
    snapshot: tuple[object, ...]


def _derived_validation_fragments(
    composer_request: ReplyComposerRequest,
    semantic_contract: SemanticGuardContract,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Derive all text evidence from the exact canonical request graph."""

    typed_grounding = tuple(
        fact.claim
        for fact in semantic_contract.content_intent.grounding_facts
        if type(fact.claim) is str and fact.claim.strip()
    )
    assembled = composer_request.assembled_context
    if assembled is None:
        screened_facts: tuple[str, ...] = ()
        forbidden_facts: tuple[str, ...] = ()
        replyer_fragments: tuple[str, ...] = ()
    else:
        if type(assembled) is not AssembledContext:
            raise ValueError("output_validation_assembled_context_invalid")
        selection = assembled.fact_selection
        screened_facts = tuple(
            fact.content.strip()
            for fact in (
                *selection.must_include_candidates,
                *selection.public_background,
            )
            if type(fact.content) is str and fact.content.strip()
        )
        forbidden_facts = tuple(
            fact.content.strip()
            for fact in selection.other_subject_facts
            if type(fact.content) is str and fact.content.strip()
        )
        replyer_fragments = tuple(
            record.content.strip()
            for record in (
                *assembled.replyer_thread,
                *assembled.public_background,
            )
            if type(record.content) is str
            and record.content.strip()
            and record.content.strip() != semantic_contract.current_message
        )
    grounding = tuple(dict.fromkeys((*typed_grounding, *screened_facts)))
    forbidden = tuple(dict.fromkeys(forbidden_facts))
    context_references = tuple(
        dict.fromkeys((*replyer_fragments, *screened_facts))
    )
    return grounding, forbidden, context_references


def _structured_raw_identity_ids(request: ReplyComposerRequest) -> tuple[str, ...]:
    """Return only source-record IDs; never reconstruct identity from keys."""
    assembled = request.assembled_context
    if type(assembled) is not AssembledContext:
        return ()
    records = (
        *assembled.planner_records,
        *((assembled.current_record,) if assembled.current_record is not None else ()),
    )
    return tuple(dict.fromkeys(
        value
        for record in records
        for value in (
            str(getattr(record, "sender_id", "") or "").strip(),
            str(getattr(record, "referenced_sender_id", "") or "").strip(),
            *(str(item or "").strip() for item in getattr(record, "mention_sender_ids", ())),
            str(getattr(record, "bot_id", "") or "").strip(),
        )
        if value
    ))


def _explicit_identity_id_query(text: str) -> bool:
    normalized = str(text or "").casefold()
    return ("账号" in normalized or "帐户" in normalized or "账户" in normalized or "id" in normalized) and (
        "问" in normalized or "是" in normalized or "多少" in normalized or "告诉" in normalized or "什么" in normalized
    )


def _build_validation_context_vault():
    lock = threading.RLock()
    records: tuple[_ValidationContextRecord, ...] = ()
    capacity = 2048

    def prune_locked() -> None:
        nonlocal records
        if type(records) is not tuple:
            raise ValueError("output_validation_context_vault_corrupt")
        live: list[_ValidationContextRecord] = []
        for record in records:
            if type(record) is not _ValidationContextRecord:
                raise ValueError("output_validation_context_vault_corrupt")
            # The request is the identity/tombstone key.  Keep the record
            # after context GC while that exact request lives so no plugin can
            # mint a second context with altered fragment sets.
            if record.request_ref() is None:
                continue
            live.append(record)
        records = tuple(live)

    def mint(
        *,
        composer_request: ReplyComposerRequest,
        current_message: str,
        expected_target_message_id: str,
        expected_target_sender_key: str,
        is_owner: bool,
        semantic_contract: SemanticGuardContract,
        current_question_anchor: CurrentQuestionAnchor,
    ) -> OutputValidationContext:
        from .reply_composer import _inspect_canonical_reply_composer_request

        if type(composer_request) is not ReplyComposerRequest:
            raise TypeError("exact composer request is required")
        _inspect_canonical_reply_composer_request(composer_request)
        inspect_semantic_guard_contract(
            semantic_contract,
            composer_request=composer_request,
        )
        if type(current_question_anchor) is not CurrentQuestionAnchor:
            raise TypeError("typed current question anchor is required")
        policy = composer_request.capability_policy
        if (
            composer_request.current_message != str(current_message or "")
            or composer_request.target_message_id != expected_target_message_id
            or composer_request.target_sender_key != expected_target_sender_key
            or composer_request.current_question_anchor is not current_question_anchor
            or semantic_contract.current_question_anchor is not current_question_anchor
            or policy is None
            or type(is_owner) is not bool
            or policy.is_owner is not is_owner
        ):
            raise ValueError("output_validation_context_source_mismatch")
        (
            grounding_facts,
            forbidden_fact_fragments,
            context_reference_fragments,
        ) = _derived_validation_fragments(
            composer_request,
            semantic_contract,
        )
        raw_identity_ids = _structured_raw_identity_ids(composer_request)
        with lock:
            nonlocal records
            prune_locked()
            if any(
                record.request_ref() is composer_request for record in records
            ):
                raise ValueError("output_validation_context_already_bound")
            if len(records) >= capacity:
                raise ValueError("output_validation_context_vault_full")
            context = object.__new__(OutputValidationContext)
            exact_values: dict[str, object] = {
                "composer_request": composer_request,
                "semantic_contract": semantic_contract,
                "current_question_anchor": current_question_anchor,
                "current_message": current_message,
                "expected_target_message_id": expected_target_message_id,
                "expected_target_sender_key": expected_target_sender_key,
                "is_owner": is_owner,
                "grounding_facts": grounding_facts,
                "forbidden_fact_fragments": forbidden_fact_fragments,
                "context_reference_fragments": context_reference_fragments,
                "raw_identity_ids": raw_identity_ids,
            }
            for name, value in exact_values.items():
                object.__setattr__(context, name, value)
            records = (
                *records,
                _ValidationContextRecord(
                    weakref.ref(context),
                    weakref.ref(composer_request),
                    weakref.ref(semantic_contract),
                    _validation_context_snapshot(context),
                ),
            )
            return context

    def inspect(
        context: OutputValidationContext,
        *,
        composer_request: ReplyComposerRequest,
        semantic_contract: SemanticGuardContract,
    ) -> OutputValidationContext:
        if type(context) is not OutputValidationContext:
            raise ValueError("output_validation_context_not_canonical")
        with lock:
            prune_locked()
            matches = tuple(
                record for record in records if record.context_ref() is context
            )
            if len(matches) != 1:
                raise ValueError("output_validation_context_not_canonical")
            record = matches[0]
        if (
            record.request_ref() is not composer_request
            or record.contract_ref() is not semantic_contract
            or context.composer_request is not composer_request
            or context.semantic_contract is not semantic_contract
            or _validation_context_snapshot(context) != record.snapshot
        ):
            raise ValueError("output_validation_context_corrupt")
        inspect_semantic_guard_contract(
            semantic_contract,
            composer_request=composer_request,
        )
        return context

    return mint, inspect


(
    _mint_output_validation_context,
    _inspect_output_validation_context,
) = _build_validation_context_vault()
del _build_validation_context_vault


def build_output_validation_context(
    *,
    composer_request: ReplyComposerRequest,
    current_message: str,
    expected_target_message_id: str,
    expected_target_sender_key: str,
    is_owner: bool,
    semantic_contract: SemanticGuardContract,
    current_question_anchor: CurrentQuestionAnchor,
) -> OutputValidationContext:
    """Mint the sole validator context for an exact Composer request."""

    return _mint_output_validation_context(
        composer_request=composer_request,
        current_message=current_message,
        expected_target_message_id=expected_target_message_id,
        expected_target_sender_key=expected_target_sender_key,
        is_owner=is_owner,
        semantic_contract=semantic_contract,
        current_question_anchor=current_question_anchor,
    )


@dataclass(frozen=True, slots=True)
class OutputValidationIssue:
    code: str
    severity: OutputIssueSeverity
    detail: str


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False, weakref_slot=True)
class OutputValidationTicket:
    phase: SemanticGuardPhase
    passed: bool

    def __repr__(self) -> str:
        return (
            "OutputValidationTicket("
            f"phase={self.phase.value!r}, passed={self.passed!r})"
        )


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class OutputValidationReport:
    issues: tuple[OutputValidationIssue, ...]
    anchor_coverage: AnchorCoverage | None = None
    semantic_guard_report: SemanticGuardReport | None = None
    validation_ticket: OutputValidationTicket | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    validated_request: ReplyComposerRequest | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    validated_result: ReplyComposerResult | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    validation_context: OutputValidationContext | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def is_valid(self) -> bool:
        return not self.issues

    @property
    def issue_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    @property
    def disposition(self) -> OutputDisposition:
        if not self.issues:
            return OutputDisposition.PASS
        if any(issue.severity == OutputIssueSeverity.BLOCKING for issue in self.issues):
            return OutputDisposition.BLOCK
        return OutputDisposition.REPAIR_ONCE

    def trace_metadata(self) -> dict[str, str | int | bool]:
        metadata: dict[str, str | int | bool] = {
            "validation_issue_count": len(self.issues),
            "validation_primary_issue_code": (
                self.issue_codes[0] if self.issue_codes else "none"
            ),
            "validation_disposition_code": self.disposition.value,
            "anchor_coverage_available": self.anchor_coverage is not None,
            "anchor_context_drift_detected": (
                "current_question_context_drift" in self.issue_codes
            ),
            "grounding_evidence_drift_detected": (
                "grounding_evidence_drift" in self.issue_codes
            ),
            "relation_context_drift_detected": (
                "current_question_relation_drift" in self.issue_codes
            ),
        }
        if self.anchor_coverage is not None:
            metadata.update(self.anchor_coverage.trace_metadata())
        if self.semantic_guard_report is not None:
            metadata.update(self.semantic_guard_report.trace_metadata())
        return metadata


class _ValidationRecord(NamedTuple):
    ticket_ref: weakref.ReferenceType
    report_ref: weakref.ReferenceType
    request_ref: weakref.ReferenceType
    result_ref: weakref.ReferenceType
    context_ref: weakref.ReferenceType
    repair_request_ref: weakref.ReferenceType | None
    phase: SemanticGuardPhase
    raw_digest: str
    visible_digest: str
    report_snapshot: tuple[object, ...]
    passed: bool
    result_snapshot: tuple[object, ...]


def _result_snapshot(result: ReplyComposerResult) -> tuple[object, ...]:
    if type(result) is not ReplyComposerResult:
        raise ValueError("output_validation_result_invalid")
    return (
        result.target_message_id,
        result.raw_response_digest,
        result.visible_text,
        result.bubbles,
        result.reply_shape,
        result.rewrites_performed,
        repr(result.typed_attribution),
    )


def _requires_typed_attribution(request: ReplyComposerRequest) -> bool:
    """Use canonical sender ownership only; never infer an actor from prose."""

    return request.attribution_risk is not AttributionRisk.NONE


def _validate_typed_attribution(
    *, request: ReplyComposerRequest, result: ReplyComposerResult
) -> tuple[str, ...]:
    """Validate R11 provider segments against this request's canonical history."""

    if not _requires_typed_attribution(request):
        return ()
    payload = result.typed_attribution
    if type(payload) is not dict:
        return ("typed_attribution_missing",)
    segments = payload.get("segments")
    if type(segments) is not list or not segments:
        return ("typed_attribution_segments_missing",)
    evidence = {
        f"history-{index}": message
        for index, message in enumerate(request.model_messages, start=1)
        if message.source_message_id and message.platform_id and message.sender_id
    }
    current = getattr(request, "current_model_message", None)
    if (
        current is not None
        and current.source_message_id
        and current.platform_id
        and current.sender_id
    ):
        evidence["current"] = current
    protected_pronouns = {"我", "你", "他", "她", "它", "TA", "ta"}
    protected_literals: set[str] = set(protected_pronouns)
    for record in evidence.values():
        display = getattr(getattr(record, "identity_metadata", None), "display", None)
        for value in (
            getattr(display, "value", ""),
            getattr(record, "platform_id", ""),
            getattr(record, "sender_id", ""),
            getattr(record, "account_key", ""),
            getattr(record, "sender_key", ""),
        ):
            if type(value) is str and value:
                protected_literals.add(value)
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for segment in segments:
        if type(segment) is not dict:
            return ("typed_attribution_segment_invalid",)
        actor_platform_id = segment.get("actor_platform_id")
        actor_sender_id = segment.get("actor_sender_id")
        predicate = segment.get("predicate")
        polarity = segment.get("polarity")
        scope = segment.get("scope")
        evidence_ids = segment.get("evidence_message_ids")
        if (
            not all(type(value) is str and value.strip() for value in (
                actor_platform_id, actor_sender_id, predicate, polarity, scope
            ))
            or polarity not in {"affirmed", "denied"}
            or scope not in {"history", "current"}
            or type(evidence_ids) is not list
            or not evidence_ids
            or any(type(value) is not str or not value for value in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)
        ):
            return ("typed_attribution_segment_invalid",)
        if (
            any(token in predicate for token in protected_pronouns)
            or _matches_identity_literal(predicate, protected_literals)
        ):
            return ("typed_attribution_predicate_actor_slot",)
        key = (actor_platform_id, actor_sender_id, predicate, tuple(evidence_ids))
        if key in seen:
            return ("typed_attribution_segment_duplicate",)
        seen.add(key)
        records = tuple(evidence.get(message_id) for message_id in evidence_ids)
        if any(record is None for record in records):
            return ("typed_attribution_evidence_out_of_scope",)
        if any(
            record.platform_id != actor_platform_id
            or record.sender_id != actor_sender_id
            for record in records
        ):
            return ("typed_attribution_actor_mismatch",)
        if scope == "current" and any(message_id != "current" for message_id in evidence_ids):
            return ("typed_attribution_evidence_out_of_scope",)
        if scope == "history" and any(message_id == "current" for message_id in evidence_ids):
            return ("typed_attribution_evidence_out_of_scope",)
    return ()


def _validation_report_snapshot(
    report: OutputValidationReport,
) -> tuple[object, ...]:
    if type(report) is not OutputValidationReport:
        raise ValueError("output_validation_report_invalid")
    issue_snapshot: list[tuple[object, ...]] = []
    for issue in report.issues:
        if (
            type(issue) is not OutputValidationIssue
            or type(issue.code) is not str
            or type(issue.severity) is not OutputIssueSeverity
            or type(issue.detail) is not str
        ):
            raise ValueError("output_validation_report_issue_invalid")
        issue_snapshot.append((issue.code, issue.severity, issue.detail))
    anchor = report.anchor_coverage
    if anchor is None:
        anchor_snapshot: tuple[object, ...] | None = None
    else:
        if type(anchor) is not AnchorCoverage:
            raise ValueError("output_validation_anchor_coverage_invalid")
        anchor_snapshot = (
            anchor.matched_atom_count,
            anchor.total_anchor_atom_count,
            anchor.matched_kinds,
            anchor.current_topic_supported,
        )
    semantic = report.semantic_guard_report
    if semantic is None:
        semantic_snapshot: tuple[object, ...] | None = None
    else:
        if type(semantic) is not SemanticGuardReport:
            raise ValueError("output_validation_semantic_report_invalid")
        semantic_issues: list[tuple[object, ...]] = []
        for issue in semantic.issues:
            if (
                type(issue) is not SemanticGuardIssue
                or type(issue.code) is not str
                or type(issue.severity) is not SemanticGuardSeverity
                or type(issue.detail) is not str
            ):
                raise ValueError("output_validation_semantic_issue_invalid")
            semantic_issues.append((issue.code, issue.severity, issue.detail))
        semantic_snapshot = (
            semantic.phase,
            semantic.visible_digest,
            tuple(semantic_issues),
        )
    return (tuple(issue_snapshot), anchor_snapshot, semantic_snapshot)


def _build_validation_vault():
    lock = threading.RLock()
    records: tuple[_ValidationRecord, ...] = ()
    capacity = 2048

    def prune_locked() -> None:
        nonlocal records
        if type(records) is not tuple:
            raise ValueError("output_validation_vault_corrupt")
        live: list[_ValidationRecord] = []
        for record in records:
            if type(record) is not _ValidationRecord:
                raise ValueError("output_validation_vault_corrupt")
            # The request is the INITIAL/REPAIR phase tombstone key.  Reports
            # and results are normally released as an event advances, but a
            # second validation in the same phase must remain impossible while
            # that exact request lives.
            if record.request_ref() is None:
                continue
            live.append(record)
        records = tuple(live)

    def validate_and_issue(
        *,
        request: ReplyComposerRequest,
        result: ReplyComposerResult,
        context: OutputValidationContext,
        raw_output: str,
        phase: SemanticGuardPhase,
        repair_request: object | None,
    ) -> OutputValidationReport:
        from .reply_composer import _inspect_canonical_reply_composer_request

        _inspect_canonical_reply_composer_request(request)
        inspect_semantic_guard_contract(
            context.semantic_contract,
            composer_request=request,
        )
        if context.composer_request is not request:
            raise ValueError("output_validation_context_request_mismatch")
        _inspect_output_validation_context(
            context,
            composer_request=request,
            semantic_contract=context.semantic_contract,
        )
        with lock:
            nonlocal records
            prune_locked()
            if not isinstance(phase, SemanticGuardPhase):
                raise TypeError("output_validation_phase_invalid")
            same_request = tuple(
                record for record in records if record.request_ref() is request
            )
            if any(record.phase is phase for record in same_request):
                raise ValueError("output_validation_phase_already_issued")
            if phase is SemanticGuardPhase.INITIAL and same_request:
                raise ValueError("output_validation_initial_out_of_order")
            if phase is SemanticGuardPhase.REPAIR:
                initial = tuple(
                    record
                    for record in same_request
                    if record.phase is SemanticGuardPhase.INITIAL
                )
                if len(initial) != 1 or initial[0].passed:
                    raise ValueError("output_validation_repair_without_rejection")
            repair_permit_valid = semantic_phase_is_repair = (
                phase is SemanticGuardPhase.REPAIR
            )
            if semantic_phase_is_repair:
                try:
                    from .repair_controller import inspect_and_consume_repair_result

                    inspect_and_consume_repair_result(
                        repair_request,
                        composer_request=request,
                        semantic_contract=context.semantic_contract,
                    )
                except Exception:
                    repair_permit_valid = False
            # The closure owns report construction.  No caller, including
            # another function in this module, can submit a forged issue list.
            report = _compute_reply_composer_output_validation(
                request=request,
                result=result,
                raw_output=raw_output,
                context=context,
                semantic_phase=phase,
                repair_request=repair_request,
                repair_permit_valid=repair_permit_valid,
            )
            if len(records) >= capacity:
                raise ValueError("output_validation_vault_full")
            ticket = object.__new__(OutputValidationTicket)
            object.__setattr__(ticket, "phase", phase)
            object.__setattr__(ticket, "passed", report.is_valid)
            object.__setattr__(report, "validation_ticket", ticket)
            object.__setattr__(report, "validated_request", request)
            object.__setattr__(report, "validated_result", result)
            object.__setattr__(report, "validation_context", context)
            repair_ref = weakref.ref(repair_request) if repair_request is not None else None
            records = (
                *records,
                _ValidationRecord(
                    weakref.ref(ticket),
                    weakref.ref(report),
                    weakref.ref(request),
                    weakref.ref(result),
                    weakref.ref(context),
                    repair_ref,
                    phase,
                    hashlib.sha256(
                        str(raw_output or "").encode("utf-8", errors="replace")
                    ).hexdigest(),
                    hashlib.sha256(
                        str(result.visible_text or "").encode(
                            "utf-8", errors="replace"
                        )
                    ).hexdigest(),
                    _validation_report_snapshot(report),
                    report.is_valid,
                    _result_snapshot(result),
                ),
            )
            return report

    def inspect(
        report: OutputValidationReport,
        *,
        composer_request: ReplyComposerRequest,
        require_passed: bool | None,
        phase: SemanticGuardPhase | None,
        expected_result: ReplyComposerResult | None,
        visible_text: str | None,
    ) -> _ValidationRecord:
        if type(report) is not OutputValidationReport:
            raise ValueError("output_validation_report_invalid")
        ticket = report.validation_ticket
        if type(ticket) is not OutputValidationTicket:
            raise ValueError("output_validation_ticket_missing")
        with lock:
            prune_locked()
            matches = tuple(
                record
                for record in records
                if record.report_ref() is report
                and record.ticket_ref() is ticket
                and record.request_ref() is composer_request
            )
            if len(matches) != 1:
                raise ValueError("output_validation_report_not_canonical")
            record = matches[0]
        request = record.request_ref()
        result = record.result_ref()
        context = record.context_ref()
        if (
            request is not composer_request
            or type(result) is not ReplyComposerResult
            or type(context) is not OutputValidationContext
            or context.composer_request is not request
            or ticket.phase is not record.phase
            or ticket.passed is not record.passed
            or report.is_valid is not record.passed
            or _validation_report_snapshot(report) != record.report_snapshot
            or report.validated_request is not request
            or report.validated_result is not result
            or report.validation_context is not context
            or _result_snapshot(result) != record.result_snapshot
        ):
            raise ValueError("output_validation_report_corrupt")
        if expected_result is not None and record.result_ref() is not expected_result:
            raise ValueError("output_validation_result_mismatch")
        if visible_text is not None and hashlib.sha256(
            str(visible_text or "").encode("utf-8", errors="replace")
        ).hexdigest() != record.visible_digest:
            raise ValueError("output_validation_visible_text_mismatch")
        from .reply_composer import _inspect_canonical_reply_composer_request

        _inspect_canonical_reply_composer_request(request)
        inspect_semantic_guard_contract(
            context.semantic_contract,
            composer_request=request,
        )
        if require_passed is not None and record.passed is not require_passed:
            raise ValueError("output_validation_pass_state_mismatch")
        if phase is not None and record.phase is not phase:
            raise ValueError("output_validation_phase_mismatch")
        return record

    def metrics() -> tuple[int, int, bool]:
        with lock:
            prune_locked()
            return len(records), capacity, len(records) <= capacity

    return validate_and_issue, inspect, metrics


(
    _validate_and_issue_output,
    _inspect_output_validation_ticket,
    _output_validation_vault_metrics,
) = _build_validation_vault()
del _build_validation_vault


def inspect_output_validation_report(
    report: OutputValidationReport,
    *,
    composer_request: ReplyComposerRequest,
    require_passed: bool | None = None,
    phase: SemanticGuardPhase | None = None,
    result: ReplyComposerResult | None = None,
    visible_text: str | None = None,
) -> OutputValidationTicket:
    record = _inspect_output_validation_ticket(
        report,
        composer_request=composer_request,
        require_passed=require_passed,
        phase=phase,
        expected_result=result,
        visible_text=visible_text,
    )
    ticket = record.ticket_ref()
    if type(ticket) is not OutputValidationTicket:
        raise ValueError("output_validation_ticket_missing")
    return ticket


def _issue(
    issues: list[OutputValidationIssue],
    code: str,
    detail: str,
    *,
    severity: OutputIssueSeverity = OutputIssueSeverity.REPAIRABLE,
) -> None:
    issues.append(OutputValidationIssue(code, severity, detail))


_HIGH_CONFIDENCE_ANCHOR_KINDS = {
    SemanticAtomKind.TOPIC,
    SemanticAtomKind.ENTITY,
    SemanticAtomKind.MEDIA_REFERENCE,
}


def _normalize_fragment(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


_GROUNDING_TEXT_RE = re.compile(r"[a-z0-9\u3400-\u9fff]+", re.IGNORECASE)


def _normalize_grounding_text(value: str) -> str:
    """Keep only comparable CJK/alphanumeric text for a conservative check."""

    return "".join(_GROUNDING_TEXT_RE.findall(str(value or "").casefold()))


def _character_ngrams(value: str, *, size: int = 2) -> frozenset[str]:
    normalized = _normalize_grounding_text(value)
    if len(normalized) < size:
        return frozenset()
    return frozenset(
        normalized[index : index + size]
        for index in range(len(normalized) - size + 1)
    )


def _has_obvious_grounding_evidence_drift(
    visible_text: str,
    facts: tuple[str, ...],
    *,
    answer_language: str,
) -> bool:
    """Detect only large lexical departures from accepted Chinese evidence.

    This is intentionally not an entailment oracle.  It catches the production
    failure mode where a renderer mentions the queried entity but invents an
    unrelated explanation.  Short evidence and cross-language answers remain
    fail-open because lexical coverage would be unreliable there.
    """

    if str(answer_language or "").strip().casefold() not in {
        "zh",
        "zh-cn",
        "zh-hans",
    }:
        return False
    evidence = _normalize_grounding_text("\n".join(facts))
    visible = _normalize_grounding_text(visible_text)
    if (
        len(evidence) < 24
        or len(visible) < 10
        or not re.search(r"[\u3400-\u9fff]", evidence)
        or not re.search(r"[\u3400-\u9fff]", visible)
    ):
        return False
    visible_grams = _character_ngrams(visible)
    evidence_grams = _character_ngrams(evidence)
    if len(visible_grams) < 8 or not evidence_grams:
        return False
    overlap = len(visible_grams.intersection(evidence_grams))
    coverage = overlap / len(visible_grams)
    return overlap < 3 or coverage < 0.14


def _copies_noncurrent_context(
    visible_text: str,
    fragments: tuple[str, ...],
) -> bool:
    visible = _normalize_fragment(visible_text)
    if len(visible) < 8:
        return False
    for value in fragments:
        fragment = _normalize_fragment(value)
        if len(fragment) < 8:
            continue
        if fragment in visible or visible in fragment:
            return True
    return False


_EXPLICIT_OLD_CURRENT_CONTRAST_RE = re.compile(
    r"(?:之前|刚才|上一(?:条|次)).{0,28}(?:现在|这次|而你(?:现在)?问)",
    re.IGNORECASE,
)


def _relation_targets(
    relations: tuple[QuestionRelation, ...],
) -> frozenset[tuple[str, str]]:
    return frozenset(
        (relation.operator_code, relation.right_operand)
        for relation in relations
        if relation.right_operand
    )


def _has_current_question_relation_drift(
    anchor: CurrentQuestionAnchor,
    visible_text: str,
    context_fragments: tuple[str, ...],
) -> bool:
    """Detect a recent relation target copied into a changed current question."""

    current_targets = _relation_targets(anchor.relation_assertions)
    if not current_targets or _EXPLICIT_OLD_CURRENT_CONTRAST_RE.search(visible_text):
        return False
    candidate_targets = _relation_targets(extract_question_relations(visible_text))
    if not candidate_targets:
        return False
    context_targets = frozenset(
        target
        for fragment in context_fragments
        for target in _relation_targets(extract_question_relations(fragment))
    )
    stale_targets = candidate_targets.intersection(context_targets).difference(
        current_targets
    )
    if not stale_targets:
        return False
    return any(
        stale_operator == current_operator
        and stale_operand != current_operand
        for stale_operator, stale_operand in stale_targets
        for current_operator, current_operand in current_targets
    )


def _request_repetition_inputs(
    request: ReplyComposerRequest,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]:
    assembled = request.assembled_context
    records = (
        assembled.replyer_thread[-12:]
        if type(assembled) is AssembledContext
        else ()
    )
    recent_replies = tuple(
        record.content
        for record in records
        if record.role is LedgerRole.ASSISTANT
        and type(record.content) is str
        and record.content.strip()
    )
    package = request.persona_package
    language = package.language if type(package) is PersonaPackage else None
    role_phrases = tuple(
        rule.text
        for rule in (
            language.catchphrases
            if type(language) is LanguagePreferences
            and type(language.catchphrases) is tuple
            else ()
        )
        if type(rule) is CatchphraseRule and type(rule.text) is str
    )
    recent_exchanges: list[tuple[str, str]] = []
    latest_user_message = ""
    for record in records:
        if (
            record.role is LedgerRole.USER
            and record.message_id != request.target_message_id
            and type(record.content) is str
            and record.content.strip()
        ):
            latest_user_message = record.content
        elif (
            record.role is LedgerRole.ASSISTANT
            and latest_user_message
            and type(record.content) is str
            and record.content.strip()
        ):
            recent_exchanges.append((latest_user_message, record.content))
            latest_user_message = ""
    return recent_replies[-6:], role_phrases, tuple(recent_exchanges[-6:])


def _compute_reply_composer_output_validation(
    *,
    request: ReplyComposerRequest,
    result: ReplyComposerResult,
    raw_output: str,
    context: OutputValidationContext,
    semantic_phase: SemanticGuardPhase = SemanticGuardPhase.INITIAL,
    repair_request: object | None = None,
    repair_permit_valid: bool = False,
) -> OutputValidationReport:
    """Pure issue computation; it cannot mint presentation authority."""

    issues: list[OutputValidationIssue] = []
    anchor_coverage: AnchorCoverage | None = None
    semantic_guard_report: SemanticGuardReport | None = None
    raw = str(raw_output or "")
    visible = str(result.visible_text or "")
    try:
        from .reply_composer import (
            _inspect_canonical_reply_composer_request,
            parse_reply_composer_output,
        )

        _inspect_canonical_reply_composer_request(request)
        inspect_semantic_guard_contract(
            context.semantic_contract,
            composer_request=request,
        )
        _inspect_output_validation_context(
            context,
            composer_request=request,
            semantic_contract=context.semantic_contract,
        )
    except Exception:
        _issue(
            issues,
            "validation_exact_request_mismatch",
            "输出校验没有复用当前 canonical Composer 请求与语义合同",
            severity=OutputIssueSeverity.BLOCKING,
        )
    try:
        reparsed = parse_reply_composer_output(request, raw)
    except Exception:
        reparsed = None
    if reparsed is None or _result_snapshot(reparsed) != _result_snapshot(result):
        _issue(
            issues,
            "validation_result_not_deterministic_parse",
            "ReplyComposerResult 不是当前 raw_output 的确定性解析结果",
            severity=OutputIssueSeverity.BLOCKING,
        )
    if context.composer_request is not request:
        _issue(
            issues,
            "validation_context_request_mismatch",
            "OutputValidationContext 与当前 Composer 请求不是同一对象",
            severity=OutputIssueSeverity.BLOCKING,
        )
    if hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest() != (
        result.raw_response_digest
    ):
        _issue(
            issues,
            "validation_raw_result_mismatch",
            "原始生成正文与解析结果摘要不一致",
            severity=OutputIssueSeverity.BLOCKING,
        )
    for typed_issue in _validate_typed_attribution(
        request=request,
        result=result,
    ):
        _issue(
            issues,
            typed_issue,
            "人物归因缺少当前 canonical 历史可验证的 typed evidence",
            # The existing single repair is allowed to replace a malformed
            # provider envelope with an honest uncertainty; final-send still
            # receives no pass ticket unless the repaired claim is verified.
            severity=OutputIssueSeverity.REPAIRABLE,
        )
    if semantic_phase is SemanticGuardPhase.REPAIR:
        if repair_permit_valid is not True:
            _issue(
                issues,
                "repair_permit_missing_or_replayed",
                "REPAIR 校验缺少同一初稿签发的一次性 repair permit",
                severity=OutputIssueSeverity.BLOCKING,
            )
    elif repair_request is not None:
        _issue(
            issues,
            "repair_permit_wrong_phase",
            "INITIAL 校验不得携带 repair permit",
            severity=OutputIssueSeverity.BLOCKING,
        )
    if not visible.strip():
        _issue(
            issues,
            "empty_visible_reply",
            "确定性解析后没有可见回复",
        )
    disclosed_raw_ids = tuple(
        raw_id
        for raw_id in context.raw_identity_ids
        if _matches_identity_literal(visible, (raw_id,))
    )
    if disclosed_raw_ids and not _explicit_identity_id_query(context.current_message):
        _issue(
            issues,
            "raw_identity_id_visible_without_explicit_query",
            "当前请求未明确询问账号或机器人 ID，默认可见回复不得展示结构化原始 ID",
            severity=OutputIssueSeverity.BLOCKING,
        )
    if (
        request.target_message_id != context.expected_target_message_id
        or result.target_message_id != context.expected_target_message_id
        or request.target_sender_key != context.expected_target_sender_key
    ):
        _issue(
            issues,
            "target_binding_mismatch",
            "回复结果与当前可信目标不一致",
            severity=OutputIssueSeverity.BLOCKING,
        )
    request_anchor = request.current_question_anchor
    context_anchor = context.current_question_anchor
    if request_anchor is not None or context_anchor is not None:
        if (
            not isinstance(request_anchor, CurrentQuestionAnchor)
            or not isinstance(context_anchor, CurrentQuestionAnchor)
            or request_anchor != context_anchor
            or context_anchor.binding.current_message_id
            != context.expected_target_message_id
            or context_anchor.binding.current_sender_key
            != context.expected_target_sender_key
        ):
            _issue(
                issues,
                "current_anchor_binding_mismatch",
                "回复校验未复用当前轮的同一语义锚点",
                severity=OutputIssueSeverity.BLOCKING,
            )
            _issue(
                issues,
                "target_binding_mismatch",
                "当前语义锚点与回复目标绑定不一致",
                severity=OutputIssueSeverity.BLOCKING,
            )
        else:
            anchor_coverage = context_anchor.coverage(visible)
            has_high_confidence_anchor = any(
                atom.kind in _HIGH_CONFIDENCE_ANCHOR_KINDS
                for atom in context_anchor.semantic_atoms
            )
            if (
                visible
                and has_high_confidence_anchor
                and not anchor_coverage.current_topic_supported
                and _copies_noncurrent_context(
                    visible,
                    context.context_reference_fragments,
                )
            ):
                _issue(
                    issues,
                    "current_question_context_drift",
                    "回复完全落在旧上下文且未覆盖当前高置信语义锚点",
                )
            if visible and _has_current_question_relation_drift(
                context_anchor,
                visible,
                context.context_reference_fragments,
            ):
                _issue(
                    issues,
                    "current_question_relation_drift",
                    "回复复用了旧问题的比较对象，没有围绕本轮已变化的关系目标作答",
                )
    if visible.strip():
        obligation = inspect_answer_obligation(
            current_message=context.current_message,
            visible_text=visible,
            context_messages=tuple(
                message.content for message in request.model_messages
            ),
            capability_snapshot=request.capability_snapshot,
            action_role_assertions=(
                context_anchor.action_role_assertions
                if isinstance(context_anchor, CurrentQuestionAnchor)
                else ()
            ),
            model_messages=request.model_messages,
            current_sender_key=request.target_sender_key,
            typed_attribution=result.typed_attribution,
        )
        obligation_details = {
            "answer_obligation_question_echo": "回复只换一种说法复述了当前问题，没有给出答案",
            "answer_obligation_clarification_drift": "回复用新实体改写了可直接回答的短追问",
            "answer_obligation_capability_evasion": "回复没有按代码拥有的真实能力状态回答能力问题",
            "answer_obligation_physical_boundary": "回复让人格幻想覆盖了必须先说明的现实或物理边界",
            "answer_obligation_multi_action_drift": "回复没有回应当前日常话语中的多个核心动作",
            "answer_obligation_action_role_drift": "回复交换了当前互动中的施事者与受事者",
            "answer_obligation_action_completion_invented": "回复把当前尚未发生的互动编成了已经完成",
            "answer_obligation_history_speaker_attribution": "回复把其他已验证群友的历史行为错误归给当前发言者",
        }
        for issue_code in obligation.issue_codes:
            _issue(
                issues,
                issue_code,
                obligation_details.get(issue_code, "回复没有履行当前问题的直接回答义务"),
            )
    if contains_tool_protocol(raw):
        _issue(issues, "tool_protocol_leak", "原始生成包含内部工具或通道协议")
    if contains_internal_reasoning(raw) or HIDDEN_REASONING_CHANNEL_RE.search(raw):
        _issue(issues, "internal_reasoning_leak", "原始生成包含内部规划或推理")

    assembled_context = request.assembled_context
    public_records = (
        assembled_context.public_background
        if type(assembled_context) is AssembledContext
        else ()
    )
    group_join_records = (
        tuple(
            record
            for record in (
                *assembled_context.replyer_thread,
                *assembled_context.public_background,
            )
            if not (
                record.role is LedgerRole.USER
                and record.message_id
                and record.message_id == request.target_message_id
            )
        )
        if type(assembled_context) is AssembledContext
        else ()
    )
    if (
        request.capability_policy.conversation_mode == "group_join"
        and not has_verified_public_group_context(group_join_records)
    ):
        _issue(
            issues,
            "group_join_context_unavailable",
            "自然参与群聊必须绑定至少一条当前消息之前的可信公开上下文",
            severity=OutputIssueSeverity.BLOCKING,
        )
    if (
        is_explicit_group_context_request(context.current_message)
        and not has_verified_public_group_context(public_records)
        and not acknowledges_unavailable_group_context(visible)
    ):
        _issue(
            issues,
            "group_context_unavailable_unacknowledged",
            "明确请求群聊回顾但没有可验证的前文，回复必须说明无法可靠概括",
        )

    recent_replies, role_phrases, recent_exchanges = _request_repetition_inputs(request)
    if find_dialogue_repetition(
        visible,
        recent_replies,
        current_message=context.current_message,
        role_phrases=role_phrases,
        recent_exchanges=recent_exchanges,
    ):
        _issue(
            issues,
            "dialogue_repetition",
            "回复复用了近期可见回复的完整内容、固定开头或 Persona 情境短语",
        )

    if not context.is_owner:
        if contains_nonowner_identity_confusion(visible):
            _issue(issues, "nonowner_identity_confusion", "把当前普通对象称作主人")
        if contains_nonowner_intimacy(visible):
            _issue(issues, "nonowner_relationship_escalation", "向普通对象输出主关系专属亲密")

    semantic_guard_report = validate_semantic_media_guard(
        contract=context.semantic_contract,
        visible_text=visible,
        visible_segments=result.bubbles,
        phase=semantic_phase,
    )
    semantic_binding = context.semantic_contract.content_intent.binding
    if (
        context.semantic_contract.current_question_anchor
        is not context.current_question_anchor
        or context.semantic_contract.composer_request is not request
        or context.composer_request is not request
        or request.current_question_anchor is not context.current_question_anchor
        or request.planned_action is not context.semantic_contract.planned_action
        or request.content_seed is None
        or request.content_seed.intent is not context.semantic_contract.content_intent
        or request.evidence_outcome is not context.semantic_contract.evidence_outcome
        or request.action_outcome is not context.semantic_contract.action_outcome
        or request.action_outcome_authority
        is not context.semantic_contract.action_outcome_authority
        or context.current_message != context.semantic_contract.current_message
        or context.expected_target_message_id != semantic_binding.current_message_id
        or context.expected_target_sender_key != semantic_binding.current_sender_key
    ):
        _issue(
            issues,
            "semantic_contract_reference_mismatch",
            "OutputValidationContext 与 ReplyComposerRequest 未复用同一当前轮语义合同",
            severity=OutputIssueSeverity.BLOCKING,
        )
    for semantic_issue in semantic_guard_report.issues:
        _issue(
            issues,
            semantic_issue.code,
            semantic_issue.detail,
            severity=(
                OutputIssueSeverity.BLOCKING
                if semantic_issue.severity is SemanticGuardSeverity.BLOCKING
                else OutputIssueSeverity.REPAIRABLE
            ),
        )
    if contains_unsupported_personal_experience(visible, context.grounding_facts):
        _issue(issues, "unsupported_personal_experience", "声称了无可信事实支持的自身经历")
    if contains_unsupported_market_claim(visible, context.grounding_facts):
        _issue(issues, "unsupported_current_fact", "输出了无可信事实支持的当前行情断言")
    typed_grounding_facts = tuple(
        fact.claim
        for fact in context.semantic_contract.content_intent.grounding_facts
        if type(fact.claim) is str and fact.claim.strip()
    )
    if _has_obvious_grounding_evidence_drift(
        visible,
        typed_grounding_facts,
        answer_language=context.semantic_contract.content_intent.answer_language,
    ):
        _issue(
            issues,
            "grounding_evidence_drift",
            "回复的事实解释明显偏离本轮已验证的检索证据",
        )

    normalized = visible.casefold()
    for fragment in context.forbidden_fact_fragments:
        text = str(fragment or "").strip()
        if text and text.casefold() in normalized:
            _issue(
                issues,
                "forbidden_fact_fragment",
                "回复包含当前目标不允许使用的事实片段",
            )

    deduped: list[OutputValidationIssue] = []
    seen: set[str] = set()
    for issue in issues:
        if issue.code in seen:
            continue
        seen.add(issue.code)
        deduped.append(issue)
    report = OutputValidationReport(
        tuple(deduped),
        anchor_coverage=anchor_coverage,
        semantic_guard_report=semantic_guard_report,
    )
    return report


def validate_reply_composer_output(
    *,
    request: ReplyComposerRequest,
    result: ReplyComposerResult,
    raw_output: str,
    context: OutputValidationContext,
    semantic_phase: SemanticGuardPhase = SemanticGuardPhase.INITIAL,
    repair_request: object | None = None,
) -> OutputValidationReport:
    """Validate severe correctness/boundary failures and mint one exact ticket."""

    return _validate_and_issue_output(
        request=request,
        result=result,
        context=context,
        raw_output=raw_output,
        phase=semantic_phase,
        repair_request=repair_request,
    )


def _preflight_reply_composer_repair_candidate(
    *,
    request: ReplyComposerRequest,
    result: ReplyComposerResult,
    raw_output: str,
    context: OutputValidationContext,
) -> bool:
    """Evaluate a repair candidate without minting delivery authority.

    The result is advisory only.  The selected candidate must still go through
    ``validate_reply_composer_output`` with the exact one-shot repair permit.
    This lets the orchestrator replace a rejected model candidate with a
    narrow code-owned fallback while ensuring only one REPAIR report becomes
    canonical.
    """

    report = _compute_reply_composer_output_validation(
        request=request,
        result=result,
        raw_output=raw_output,
        context=context,
        semantic_phase=SemanticGuardPhase.REPAIR,
        repair_request=None,
        repair_permit_valid=True,
    )
    return report.is_valid
