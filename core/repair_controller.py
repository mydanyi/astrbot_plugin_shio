from __future__ import annotations

import hashlib
import json
import re
import threading
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

from .action_planner import PlannedAction
from .action_outcome import ActionOutcomeAuthority, ActionOutcomeIntent
from .contracts import ActionKind, ExpressionModality
from .conversation_ledger import (
    has_verified_public_group_context,
    is_explicit_group_context_request,
)
from .current_question_anchor import CurrentTurnKind
from .output_validator_v2 import (
    OutputIssueSeverity,
    OutputValidationReport,
    inspect_output_validation_report,
)
from .reply_composer import ReplyComposerRequest
from .semantic_guard import (
    SemanticGuardContract,
    SemanticGuardPhase,
    inspect_semantic_guard_contract,
)


class RepairAction(str, Enum):
    SEND = "send"
    GENERATE_ONCE = "generate_once"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class RepairDecision:
    action: RepairAction
    reason_codes: tuple[str, ...]
    repair_generation_budget: int
    safe_visible_text: str


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False, weakref_slot=True)
class RepairGenerationRequest:
    composer_request: ReplyComposerRequest
    target_message_id: str
    issue_codes: tuple[str, ...]
    system_prompt: str
    user_prompt: str
    trusted_presentation_prompt: str
    semantic_contract: SemanticGuardContract
    action_outcome: ActionOutcomeIntent | None
    action_outcome_authority: ActionOutcomeAuthority | None
    media_item_ids: tuple[str, ...]
    rejected_visible_digest: str
    generation_call_budget: int
    routine_style_rewrite_budget: int

    def __repr__(self) -> str:
        return (
            "RepairGenerationRequest("
            f"issue_count={len(self.issue_codes)}, "
            f"media_count={len(self.media_item_ids)}, "
            f"generation_call_budget={self.generation_call_budget}, "
            f"routine_style_rewrite_budget={self.routine_style_rewrite_budget}, "
            f"has_action_outcome={self.action_outcome is not None}, "
            "typed_semantic_contract=True, exact_composer_request=True)"
        )


class _RepairPermitRecord(NamedTuple):
    repair_ref: weakref.ReferenceType
    report_ref: weakref.ReferenceType
    request_ref: weakref.ReferenceType
    contract_ref: weakref.ReferenceType
    outcome_ref: weakref.ReferenceType | None
    authority_ref: weakref.ReferenceType | None
    snapshot: tuple[object, ...]
    consumed: bool


def _repair_snapshot(request: RepairGenerationRequest) -> tuple[object, ...]:
    if type(request) is not RepairGenerationRequest:
        raise ValueError("repair_generation_request_invalid")
    return (
        request.target_message_id,
        request.issue_codes,
        request.system_prompt,
        request.user_prompt,
        request.trusted_presentation_prompt,
        request.media_item_ids,
        request.rejected_visible_digest,
        request.generation_call_budget,
        request.routine_style_rewrite_budget,
    )


def _build_repair_permit_vault():
    lock = threading.RLock()
    records: tuple[_RepairPermitRecord, ...] = ()
    capacity = 1024

    def prune_locked() -> None:
        nonlocal records
        if type(records) is not tuple:
            raise ValueError("repair_permit_vault_corrupt")
        live: list[_RepairPermitRecord] = []
        for record in records:
            if type(record) is not _RepairPermitRecord:
                raise ValueError("repair_permit_vault_corrupt")
            # The Composer request is the once-per-turn tombstone key.  Permit
            # and report GC must never reopen allocation for that request.
            if record.request_ref() is None:
                continue
            live.append(record)
        records = tuple(live)

    def mint(
        *,
        original_request: ReplyComposerRequest,
        rejected_visible_text: str,
        report: OutputValidationReport,
        repair_attempts_used: int,
        semantic_contract: SemanticGuardContract,
        trusted_presentation_prompt: str,
    ) -> RepairGenerationRequest:
        decision = decide_output_repair(
            report,
            repair_attempts_used=repair_attempts_used,
        )
        if decision.action != RepairAction.GENERATE_ONCE:
            raise ValueError("当前状态不允许修复生成")
        visible = str(rejected_visible_text or "")
        inspect_semantic_guard_contract(
            semantic_contract,
            composer_request=original_request,
        )
        inspect_output_validation_report(
            report,
            composer_request=original_request,
            require_passed=False,
            phase=SemanticGuardPhase.INITIAL,
            visible_text=visible,
        )
        if report.semantic_guard_report is None:
            raise ValueError("typed semantic validation report is required")
        if report.semantic_guard_report.phase is not SemanticGuardPhase.INITIAL:
            raise ValueError("only initial semantic validation may allocate repair")
        if report.semantic_guard_report.visible_digest != hashlib.sha256(
            visible.encode("utf-8", errors="replace")
        ).hexdigest():
            raise ValueError("semantic repair source digest mismatch")
        if (
            original_request.current_question_anchor
            is not semantic_contract.current_question_anchor
            or original_request.planned_action is not semantic_contract.planned_action
        ):
            raise ValueError("semantic repair anchor/action identity mismatch")
        media_item_ids = tuple(
            item.item_id for item in semantic_contract.media_context.items
        )
        if (
            semantic_contract.content_intent.required_media_item_ids
            != media_item_ids
            or semantic_contract.current_question_anchor.media_item_ids
            != media_item_ids
        ):
            raise ValueError("semantic repair media item mismatch")
        outcome = semantic_contract.action_outcome
        authority = semantic_contract.action_outcome_authority
        if outcome is not None:
            assert authority is not None
            inspected = authority.inspect_for_composer(
                outcome,
                semantic_contract.planned_action,
                consumer=original_request,
            )
            if inspected is not outcome:
                raise ValueError("repair_action_outcome_not_exact")
        payload: dict[str, object] = {
            "original_generation_data": original_request.user_prompt,
            "repair_constraints": {
                "issue_codes": list(report.issue_codes),
                "must_change_visible_form": (
                    "dialogue_repetition" in report.issue_codes
                ),
                "do_not_copy_rejected_reply": True,
            },
        }
        if outcome is not None:
            payload["action_outcome"] = {
                "operation": outcome.operation.value,
                "kind": outcome.kind.value,
                "attempted": outcome.attempted,
                "has_output": outcome.has_output,
            }
        presentation_prompt = str(trusted_presentation_prompt or "").strip()
        if len(presentation_prompt) > 65536:
            raise ValueError("trusted_presentation_prompt_too_large")
        system_prompt = original_request.system_prompt + """

[受限修复模式]
你只进行一次受限重新生成。严格复用 original_generation_data 中当前发言者、当前问题、对象、动作、否定、媒体、事实和回答语言，并继续遵守上方系统消息中的人格、关系与真实能力边界；不得重新认人、重新规划、调用工具或声称获得新结果。grounding_facts 非空时，所有客观事实都必须直接来自这些证据；不得凭常识、印象或联想补写动机、价格、人物行为、因果或结局。可以自然转述和保留角色语气，但必须保留证据中的关键原因与结果。action_outcome 若存在，只能按 operation、kind、attempted、has_output 这四个代码字段自然表达，不能补充回执、正文、路径、参数、计数或原因。

original_generation_data.current_anchor.action_role_assertions 非空时必须逐项保持 actor、target 与 phase：current_user 是当前发言者，assistant 是角色；不得交换施事者与受事者，也不得把 phase=pending 改写成已经执行或已经完成。actor=current_user、target=assistant、phase=pending 时，必须先明确允许、邀请或继续当前发言者要做的动作；只汇报自己的状态不算回应了该动作。

首个气泡必须独立完整表达必要动作状态；其余气泡不得矛盾。repair_constraints.must_change_visible_form 为 true 时，保留语义但更换开头、句式和 Persona 情境短语，且不得复用近期历史或被拒绝回答。直接输出最终可见回复，不得输出分析、JSON、标签、协议、工具调用、修复说明或被拒绝的旧回答。"""
        if presentation_prompt:
            system_prompt += """

[受信展示后处理协议]
下面的带边界提示来自已在正常请求钩子中生效的展示插件。它只允许在可见正文之后附加该插件规定的机器标记；机器标记不属于可见正文，也不放进正文气泡。除此以外仍禁止输出任何标签、协议或工具调用。
""" + presentation_prompt
        user_prompt = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with lock:
            nonlocal records
            prune_locked()
            if any(
                record.request_ref() is original_request
                or record.contract_ref() is semantic_contract
                or record.report_ref() is report
                for record in records
            ):
                raise ValueError("repair_permit_already_issued")
            if len(records) >= capacity:
                raise ValueError("repair_permit_vault_full")
            request = object.__new__(RepairGenerationRequest)
            values = {
                "composer_request": original_request,
                "target_message_id": original_request.target_message_id,
                "issue_codes": report.issue_codes,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "trusted_presentation_prompt": presentation_prompt,
                "semantic_contract": semantic_contract,
                "action_outcome": outcome,
                "action_outcome_authority": authority,
                "media_item_ids": media_item_ids,
                "rejected_visible_digest": hashlib.sha256(
                    str(rejected_visible_text or "").encode(
                        "utf-8", errors="replace"
                    )
                ).hexdigest(),
                "generation_call_budget": 1,
                "routine_style_rewrite_budget": 0,
            }
            for name, value in values.items():
                object.__setattr__(request, name, value)
            records = (
                *records,
                _RepairPermitRecord(
                    weakref.ref(request),
                    weakref.ref(report),
                    weakref.ref(original_request),
                    weakref.ref(semantic_contract),
                    weakref.ref(outcome) if outcome is not None else None,
                    weakref.ref(authority) if authority is not None else None,
                    _repair_snapshot(request),
                    False,
                ),
            )
            return request

    def inspect_consume(
        value: object,
        *,
        composer_request: ReplyComposerRequest,
        semantic_contract: SemanticGuardContract,
    ) -> RepairGenerationRequest:
        if type(value) is not RepairGenerationRequest:
            raise ValueError("repair_permit_required")
        inspect_semantic_guard_contract(
            semantic_contract,
            composer_request=composer_request,
        )
        with lock:
            nonlocal records
            prune_locked()
            matches = tuple(
                (index, record)
                for index, record in enumerate(records)
                if record.repair_ref() is value
            )
            if len(matches) != 1:
                raise ValueError("repair_permit_not_canonical")
            index, record = matches[0]
            if record.consumed:
                raise ValueError("repair_permit_replayed")
            outcome = record.outcome_ref() if record.outcome_ref is not None else None
            authority = (
                record.authority_ref() if record.authority_ref is not None else None
            )
            if (
                record.request_ref() is not composer_request
                or record.contract_ref() is not semantic_contract
                or value.composer_request is not composer_request
                or value.semantic_contract is not semantic_contract
                or value.action_outcome is not outcome
                or value.action_outcome_authority is not authority
                or _repair_snapshot(value) != record.snapshot
            ):
                raise ValueError("repair_permit_corrupt")
            if outcome is not None:
                assert authority is not None
                inspected = authority.inspect_for_composer(
                    outcome,
                    semantic_contract.planned_action,
                    consumer=composer_request,
                )
                if inspected is not outcome:
                    raise ValueError("repair_action_outcome_not_exact")
            records = (
                *records[:index],
                record._replace(consumed=True),
                *records[index + 1 :],
            )
            return value

    def metrics() -> tuple[int, int, int, bool]:
        with lock:
            prune_locked()
            consumed = sum(record.consumed for record in records)
            return len(records), consumed, capacity, len(records) <= capacity

    return mint, inspect_consume, metrics


(
    _mint_repair_generation_request,
    _inspect_consume_repair_generation_request,
    _repair_permit_vault_metrics,
) = _build_repair_permit_vault()
del _build_repair_permit_vault


def inspect_and_consume_repair_result(
    repair_request: object,
    *,
    composer_request: ReplyComposerRequest,
    semantic_contract: SemanticGuardContract,
) -> RepairGenerationRequest:
    return _inspect_consume_repair_generation_request(
        repair_request,
        composer_request=composer_request,
        semantic_contract=semantic_contract,
    )


def decide_output_repair(
    report: OutputValidationReport,
    *,
    repair_attempts_used: int,
) -> RepairDecision:
    """Permit one severe-error repair and then fail closed."""

    if report.is_valid:
        return RepairDecision(RepairAction.SEND, (), 0, "")
    issue_codes = report.issue_codes
    immediate_blocking = tuple(
        issue.code
        for issue in report.issues
        if issue.severity is OutputIssueSeverity.BLOCKING
    )
    if immediate_blocking:
        return RepairDecision(
            RepairAction.BLOCK,
            immediate_blocking,
            0,
            "",
        )
    used = max(0, int(repair_attempts_used))
    if used == 0:
        return RepairDecision(
            RepairAction.GENERATE_ONCE,
            issue_codes,
            1,
            "",
        )

    return RepairDecision(
        RepairAction.BLOCK,
        ("repair_failed_no_validated_output",),
        0,
        "",
    )


_SAFE_DIRECT_REPLY_FALLBACKS = (
    "我在。刚才这句话没接稳，你再说一次，我会认真回应。",
    "我还在。刚刚没能稳妥回应，你再说一遍，我会好好接住。",
    "我在。刚才没接稳你这句话，你再说一次，我会认真回应。",
    "我没有走开。刚才这句没回应好，你愿意再说一次吗？",
)

_SAFE_DIRECT_QUESTION_FALLBACKS = (
    "这次回答卡在半路了。我不想拿旧答案敷衍你，麻烦把刚才的问题再发一次。",
    "我刚刚没把这次回答完整接回来，也不该拿上一条顶上。你再问我一次吧。",
    "这次我没能生成完整回答。我宁可老实告诉你，也不拿前一个答案糊弄过去。",
    "刚才这次回答没有完整回来。我不想装作答过了，麻烦你再问我一次。",
)


_GROUNDING_FALLBACK_LABEL_RE = re.compile(
    r"^\s*(?:摘要|简介|释义|定义|答案|说明|正文|内容)\s*[:：]\s*"
)
_GROUNDING_FALLBACK_MARKUP_RE = re.compile(
    r"^\s*(?:#{1,6}|[-*+>]|\d+[.)、])\s*"
)


def build_grounded_evidence_fallback(
    original_request: ReplyComposerRequest,
) -> str | None:
    """Return an evidence-bearing fallback when the one-shot rewrite drifts.

    The text is selected from the exact sealed facts already attached to the
    canonical request.  It is not a second model call and is still validated
    in the REPAIR phase before presentation authority can be issued.
    """

    from .reply_composer import _inspect_canonical_reply_composer_request

    _inspect_canonical_reply_composer_request(original_request)
    planned_action = original_request.planned_action
    seed = original_request.content_seed
    evidence = original_request.evidence_outcome
    if (
        type(planned_action) is not PlannedAction
        or planned_action.kind is not ActionKind.USE_TOOL
        or seed is None
        or evidence is None
        or not seed.intent.grounding_facts
        or tuple(evidence.facts) != tuple(seed.intent.grounding_facts)
        or original_request.action_outcome is not None
        or original_request.action_outcome_authority is not None
    ):
        return None
    candidates: list[tuple[bool, str]] = []
    for fact in seed.intent.grounding_facts:
        raw = str(fact.claim or "").strip()
        if not raw:
            continue
        preferred = bool(_GROUNDING_FALLBACK_LABEL_RE.match(raw))
        cleaned = _GROUNDING_FALLBACK_MARKUP_RE.sub("", raw, count=1)
        cleaned = _GROUNDING_FALLBACK_LABEL_RE.sub("", cleaned, count=1).strip()
        if cleaned:
            candidates.append((preferred, cleaned))
    if not candidates:
        return None
    preferred_candidates = tuple(text for preferred, text in candidates if preferred)
    pool = preferred_candidates or tuple(text for _, text in candidates)
    claim = max(pool, key=lambda text: (len(text), text))
    return f"我查到的资料里是这样说的：{claim}"


def build_safe_direct_reply_fallback(
    original_request: ReplyComposerRequest,
) -> str | None:
    """Return a content-free retry acknowledgement for a narrow direct turn.

    This is not a second repair generator and grants no presentation authority.
    The caller must still consume the one canonical REPAIR permit and pass the
    selected text through the normal parser, validator, semantic guard and
    final-send seal.  Tools, media, evidence and action outcomes deliberately
    remain fail-closed.  A direct question/request may receive a transparent
    retry acknowledgement so repair failure cannot silently consume the turn
    or disguise a copied older answer as a successful response.
    """

    from .reply_composer import _inspect_canonical_reply_composer_request

    _inspect_canonical_reply_composer_request(original_request)
    planned_action = original_request.planned_action
    anchor = original_request.current_question_anchor
    expression = original_request.expression_intent
    if (
        type(planned_action) is not PlannedAction
        or planned_action.kind is not ActionKind.REPLY
        or original_request.capability_policy.conversation_mode != "direct_reply"
        or original_request.evidence_outcome is not None
        or original_request.action_outcome is not None
        or original_request.action_outcome_authority is not None
        or original_request.grounding_fact_count != 0
        or original_request.media_item_count != 0
        or bool(anchor.media_item_ids)
        or expression is None
        or expression.modality is not ExpressionModality.TEXT
    ):
        return None
    assembled = original_request.assembled_context
    if is_explicit_group_context_request(original_request.current_message):
        public_records = (
            assembled.public_background
            if assembled is not None
            else ()
        )
        if not has_verified_public_group_context(public_records):
            return "我这边没有拿到前面的群聊记录，不能可靠概括。"
    digest = original_request.target_content_digest
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        return None
    if anchor.turn_kind is CurrentTurnKind.STATEMENT:
        if anchor.semantic_atoms:
            return None
        choices = _SAFE_DIRECT_REPLY_FALLBACKS
    elif anchor.turn_kind in {CurrentTurnKind.QUESTION, CurrentTurnKind.REQUEST}:
        choices = _SAFE_DIRECT_QUESTION_FALLBACKS
    else:
        return None
    return choices[int(digest[:8], 16) % len(choices)]


def build_single_repair_request(
    *,
    original_request: ReplyComposerRequest,
    rejected_visible_text: str,
    report: OutputValidationReport,
    repair_attempts_used: int,
    semantic_contract: SemanticGuardContract,
    trusted_presentation_prompt: str = "",
) -> RepairGenerationRequest:
    """Build the only allowed repair request without copying raw hidden output."""
    return _mint_repair_generation_request(
        original_request=original_request,
        rejected_visible_text=rejected_visible_text,
        report=report,
        repair_attempts_used=repair_attempts_used,
        semantic_contract=semantic_contract,
        trusted_presentation_prompt=trusted_presentation_prompt,
    )
