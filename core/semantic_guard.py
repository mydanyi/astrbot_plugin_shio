from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import NamedTuple
import weakref

from .action_outcome import (
    ActionOutcomeAuthority,
    ActionOutcomeIntent,
    ActionOutcomeKind,
    ActionOutcomeOperation,
    ActionOutcomeProposition,
    action_outcome_semantics_for,
    expand_action_outcome_propositions_for,
)
from .action_planner import PlannedAction
from .contracts import (
    ContentIntent,
    DecisionBinding,
    MediaAvailability,
    MediaContext,
    SemanticAtomKind,
)
from .current_question_anchor import CurrentQuestionAnchor
from .grounding_adapter import EvidenceOutcome, EvidenceOutcomeKind
from .reply_composer import ReplyComposerRequest
from .response_guard import contains_unexpected_foreign_language


class SemanticGuardPhase(str, Enum):
    INITIAL = "initial"
    REPAIR = "repair"
    FINAL_SEND = "final_send"


class SemanticGuardSeverity(str, Enum):
    REPAIRABLE = "repairable"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class SemanticGuardContract:
    composer_request: ReplyComposerRequest = field(repr=False)
    planned_action: PlannedAction
    content_intent: ContentIntent
    current_question_anchor: CurrentQuestionAnchor
    media_context: MediaContext
    current_message: str = field(repr=False)
    evidence_outcome: EvidenceOutcome | None = field(repr=False)
    action_outcome: ActionOutcomeIntent | None = field(repr=False)
    action_outcome_authority: ActionOutcomeAuthority | None = field(repr=False)

    def __post_init__(self) -> None:
        _validate_semantic_guard_contract_sources(self)
        _register_semantic_guard_contract(self)

    def __repr__(self) -> str:
        media_count = (
            len(self.media_context.items)
            if isinstance(self.media_context, MediaContext)
            else 0
        )
        fact_count = (
            len(self.content_intent.grounding_facts)
            if isinstance(self.content_intent, ContentIntent)
            else 0
        )
        return (
            "SemanticGuardContract("
            f"typed={self.is_typed}, action_bound={isinstance(self.planned_action, PlannedAction)}, "
            f"media_count={media_count}, "
            f"fact_count={fact_count}, evidence_bound={self.evidence_outcome is not None})"
        )

    @property
    def is_typed(self) -> bool:
        return bool(
            type(self.composer_request) is ReplyComposerRequest
            and type(self.planned_action) is PlannedAction
            and type(self.content_intent) is ContentIntent
            and type(self.current_question_anchor) is CurrentQuestionAnchor
            and type(self.media_context) is MediaContext
            and (
                self.evidence_outcome is None
                or type(self.evidence_outcome) is EvidenceOutcome
            )
            and (
                (self.action_outcome is None and self.action_outcome_authority is None)
                or (
                    type(self.action_outcome) is ActionOutcomeIntent
                    and type(self.action_outcome_authority) is ActionOutcomeAuthority
                )
            )
        )


def _validate_semantic_guard_contract_sources(
    contract: SemanticGuardContract,
) -> ReplyComposerRequest:
    """Re-prove the exact Composer request and every authority-bearing field."""

    if type(contract) is not SemanticGuardContract:
        raise ValueError("semantic_contract_type_invalid")
    request = contract.composer_request
    if type(request) is not ReplyComposerRequest:
        raise ValueError("semantic_contract_composer_request_invalid")
    try:
        from .reply_composer import _inspect_canonical_reply_composer_request

        _inspect_canonical_reply_composer_request(request)
    except Exception as exc:
        raise ValueError("semantic_contract_composer_request_invalid") from exc
    seed = request.content_seed
    if (
        request.planned_action is not contract.planned_action
        or seed is None
        or seed.intent is not contract.content_intent
        or request.current_question_anchor is not contract.current_question_anchor
        or request.media_context is not contract.media_context
        or request.current_message != contract.current_message
        or request.evidence_outcome is not contract.evidence_outcome
        or request.action_outcome is not contract.action_outcome
        or request.action_outcome_authority
        is not contract.action_outcome_authority
        or seed.action_outcome is not contract.action_outcome
        or seed.action_outcome_authority is not contract.action_outcome_authority
    ):
        raise ValueError("semantic_contract_composer_reference_mismatch")
    if contract.evidence_outcome is not None and contract.action_outcome is not None:
        raise ValueError("semantic_contract_evidence_outcome_conflict")
    if (contract.action_outcome is None) is not (
        contract.action_outcome_authority is None
    ):
        raise ValueError("semantic_contract_action_outcome_pair_invalid")
    if contract.action_outcome is not None:
        authority = contract.action_outcome_authority
        assert authority is not None
        try:
            inspected = authority.inspect_for_composer(
                contract.action_outcome,
                contract.planned_action,
                consumer=request,
            )
        except Exception as exc:
            raise ValueError("semantic_contract_action_outcome_invalid") from exc
        if inspected is not contract.action_outcome:
            raise ValueError("semantic_contract_action_outcome_not_exact")
    return request


class _SemanticContractRecord(NamedTuple):
    contract_ref: weakref.ReferenceType
    request_ref: weakref.ReferenceType


def _build_semantic_contract_vault():
    lock = threading.RLock()
    records: tuple[_SemanticContractRecord, ...] = ()
    capacity = 1024

    def prune_locked() -> None:
        nonlocal records
        if type(records) is not tuple:
            raise ValueError("semantic_contract_vault_corrupt")
        live: list[_SemanticContractRecord] = []
        for record in records:
            if type(record) is not _SemanticContractRecord:
                raise ValueError("semantic_contract_vault_corrupt")
            request = record.request_ref()
            # The canonical request is the lifetime key.  Keeping a tombstone
            # while it remains live prevents a second caller-built contract
            # from being registered after the original contract is released.
            # A live contract whose request vanished can no longer pass its
            # own source inspector and is safe to discard here.
            if request is None:
                continue
            live.append(record)
        records = tuple(live)

    def register(contract: SemanticGuardContract) -> None:
        request = _validate_semantic_guard_contract_sources(contract)
        with lock:
            nonlocal records
            prune_locked()
            if any(record.contract_ref() is contract for record in records):
                raise ValueError("semantic_contract_already_registered")
            if any(record.request_ref() is request for record in records):
                raise ValueError("semantic_contract_request_already_bound")
            if len(records) >= capacity:
                raise ValueError("semantic_contract_vault_full")
            records = (
                *records,
                _SemanticContractRecord(weakref.ref(contract), weakref.ref(request)),
            )

    def inspect(
        contract: SemanticGuardContract,
        *,
        composer_request: ReplyComposerRequest,
    ) -> SemanticGuardContract:
        if type(contract) is not SemanticGuardContract:
            raise ValueError("semantic_contract_type_invalid")
        request = _validate_semantic_guard_contract_sources(contract)
        if request is not composer_request:
            raise ValueError("semantic_contract_composer_request_mismatch")
        with lock:
            prune_locked()
            matches = tuple(
                record
                for record in records
                if record.contract_ref() is contract
                and record.request_ref() is request
            )
            if len(matches) != 1:
                raise ValueError("semantic_contract_not_canonical")
        return contract

    def metrics() -> tuple[int, int, bool]:
        with lock:
            prune_locked()
            return len(records), capacity, len(records) <= capacity

    return register, inspect, metrics


(
    _register_semantic_guard_contract,
    _inspect_semantic_guard_contract,
    _semantic_contract_vault_metrics,
) = _build_semantic_contract_vault()
del _build_semantic_contract_vault


def inspect_semantic_guard_contract(
    contract: SemanticGuardContract,
    *,
    composer_request: ReplyComposerRequest,
) -> SemanticGuardContract:
    """Public narrow inspector; it never registers a caller-built contract."""

    return _inspect_semantic_guard_contract(
        contract,
        composer_request=composer_request,
    )


@dataclass(frozen=True, slots=True)
class SemanticGuardIssue:
    code: str
    severity: SemanticGuardSeverity
    detail: str


@dataclass(frozen=True, slots=True)
class SemanticGuardReport:
    phase: SemanticGuardPhase
    visible_digest: str = field(repr=False)
    issues: tuple[SemanticGuardIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues

    @property
    def issue_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    @property
    def has_blocking_issue(self) -> bool:
        return any(
            issue.severity is SemanticGuardSeverity.BLOCKING
            for issue in self.issues
        )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "semantic_guard_status": self.phase.value,
            "semantic_guard_issue_count": len(self.issues),
            "semantic_guard_blocking": self.has_blocking_issue,
            "semantic_guard_drift_detected": bool(self.issues)
            and (
                self.phase is SemanticGuardPhase.FINAL_SEND
                or not self.has_blocking_issue
            ),
        }


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False, weakref_slot=True)
class SemanticValidationSeal:
    """Opaque code-owned proof that INITIAL or REPAIR validation succeeded."""

    issued_phase: SemanticGuardPhase

    def __repr__(self) -> str:
        return f"SemanticValidationSeal(issued_phase={self.issued_phase.value!r})"


class _IssuedSemanticSeal(NamedTuple):
    seal_ref: weakref.ReferenceType
    contract_ref: weakref.ReferenceType
    request_ref: weakref.ReferenceType
    presentation_ref: weakref.ReferenceType
    issued_phase: SemanticGuardPhase
    guarded_digest: str
    terminal_state: str


def _inspect_presentation_for_contract(
    presentation: object,
    contract: SemanticGuardContract,
):
    from .presentation_handoff import _inspect_canonical_presentation_handoff

    return _inspect_canonical_presentation_handoff(
        presentation,
        composer_request=contract.composer_request,
        semantic_contract=contract,
        action_outcome=contract.action_outcome,
        action_outcome_authority=contract.action_outcome_authority,
    )


def _build_semantic_seal_vault():
    """One cross-Controller delivery ledger with no caller-owned record API."""

    lock = threading.RLock()
    records: tuple[_IssuedSemanticSeal, ...] = ()
    capacity = 1024

    def prune_locked() -> None:
        nonlocal records
        if type(records) is not tuple:
            raise ValueError("semantic_seal_vault_corrupt")
        live: list[_IssuedSemanticSeal] = []
        for record in records:
            if type(record) is not _IssuedSemanticSeal:
                raise ValueError("semantic_seal_vault_corrupt")
            request = record.request_ref()
            # The request is the delivery identity and the tombstone key.  A
            # collected contract/presentation/seal makes further inspection
            # fail closed, but is normal event cleanup rather than corruption.
            # Retain the tombstone until the exact request is gone so another
            # Controller instance still cannot re-issue delivery authority.
            if request is None:
                continue
            if record.terminal_state not in {"issued", "consumed", "discarded"}:
                raise ValueError("semantic_seal_vault_corrupt")
            live.append(record)
        records = tuple(live)

    def issue(
        *,
        contract: SemanticGuardContract,
        phase: SemanticGuardPhase,
        visible_text: str,
        presentation: object,
        presentation_digest: str,
    ) -> SemanticValidationSeal:
        request = contract.composer_request
        inspect_semantic_guard_contract(contract, composer_request=request)
        handoff = _inspect_presentation_for_contract(presentation, contract)
        if not handoff.eligible:
            raise ValueError("semantic_seal_presentation_ineligible")
        if phase not in {SemanticGuardPhase.INITIAL, SemanticGuardPhase.REPAIR}:
            raise ValueError("semantic_seal_stage_not_validated")
        report = validate_semantic_media_guard(
            contract=contract,
            visible_text=visible_text,
            visible_segments=handoff.final_segments,
            phase=phase,
        )
        if not report.is_valid or report.phase is not phase:
            raise ValueError("semantic_seal_stage_not_validated")
        if (
            str(visible_text or "") != handoff.final_visible_text
            or str(presentation_digest or "") != handoff.final_text_digest
            or handoff.final_text_digest != report.visible_digest
        ):
            raise ValueError("semantic_seal_presentation_mismatch")
        with lock:
            nonlocal records
            prune_locked()
            if any(record.request_ref() is request for record in records):
                raise ValueError("semantic_seal_delivery_already_issued")
            if len(records) >= capacity:
                raise ValueError("semantic_seal_vault_full")
            seal = object.__new__(SemanticValidationSeal)
            object.__setattr__(seal, "issued_phase", phase)
            records = (
                *records,
                _IssuedSemanticSeal(
                    weakref.ref(seal),
                    weakref.ref(contract),
                    weakref.ref(request),
                    weakref.ref(handoff),
                    phase,
                    report.visible_digest,
                    "issued",
                ),
            )
            return seal

    def discard(seal: object) -> None:
        with lock:
            nonlocal records
            prune_locked()
            updated: list[_IssuedSemanticSeal] = []
            for record in records:
                if record.seal_ref() is seal and record.terminal_state == "issued":
                    record = record._replace(terminal_state="discarded")
                updated.append(record)
            records = tuple(updated)

    def consume(
        *,
        seal: object,
        contract: SemanticGuardContract,
        visible_text: str,
        visible_segments: tuple[str, ...],
        presentation: object,
        presentation_digest: str,
    ) -> bool:
        with lock:
            nonlocal records
            prune_locked()
            matches = tuple(
                (index, record)
                for index, record in enumerate(records)
                if record.seal_ref() is seal
            )
            if len(matches) != 1:
                return False
            index, record = matches[0]
            if record.terminal_state != "issued":
                return False
            replacement = record._replace(terminal_state="consumed")
            records = (*records[:index], replacement, *records[index + 1 :])

        # The first consume attempt is terminal even when a substituted object
        # or changed byte sequence is supplied.
        try:
            request = contract.composer_request
            inspect_semantic_guard_contract(contract, composer_request=request)
            handoff = _inspect_presentation_for_contract(presentation, contract)
            report = validate_semantic_media_guard(
                contract=contract,
                visible_text=visible_text,
                visible_segments=visible_segments,
                phase=SemanticGuardPhase.FINAL_SEND,
            )
        except Exception:
            return False
        return bool(
            record.contract_ref() is contract
            and record.request_ref() is request
            and record.presentation_ref() is handoff
            and type(seal) is SemanticValidationSeal
            and seal.issued_phase is record.issued_phase
            and report.phase is SemanticGuardPhase.FINAL_SEND
            and report.is_valid
            and tuple(visible_segments) == handoff.final_segments
            and str(visible_text or "") == handoff.final_visible_text
            and report.visible_digest == record.guarded_digest
            and str(presentation_digest or "") == record.guarded_digest
            and handoff.final_text_digest == record.guarded_digest
        )

    def metrics() -> tuple[int, int, int, bool]:
        with lock:
            prune_locked()
            terminal = sum(record.terminal_state != "issued" for record in records)
            return len(records), terminal, capacity, len(records) <= capacity

    return issue, discard, consume, metrics


(
    _issue_semantic_seal,
    _discard_semantic_seal,
    _consume_semantic_seal,
    _semantic_seal_vault_metrics,
) = _build_semantic_seal_vault()
del _build_semantic_seal_vault


class SemanticGuardController:
    """Stateless facade over the module-owned cross-instance seal authority."""

    def issue(
        self,
        *,
        contract: SemanticGuardContract,
        phase: SemanticGuardPhase,
        visible_text: str,
        presentation: object,
        presentation_digest: str,
    ) -> SemanticValidationSeal:
        return _issue_semantic_seal(
            contract=contract,
            phase=phase,
            visible_text=visible_text,
            presentation=presentation,
            presentation_digest=presentation_digest,
        )

    def discard(self, seal: object) -> None:
        _discard_semantic_seal(seal)

    def consume_final(
        self,
        *,
        seal: object,
        contract: SemanticGuardContract,
        visible_text: str,
        visible_segments: tuple[str, ...],
        presentation: object,
        presentation_digest: str,
    ) -> bool:
        return _consume_semantic_seal(
            seal=seal,
            contract=contract,
            visible_text=visible_text,
            visible_segments=visible_segments,
            presentation=presentation,
            presentation_digest=presentation_digest,
        )


_ACTION_FAMILIES: dict[str, tuple[str, ...]] = {
    "start": ("启动", "开启", "运行", "跑起来"),
    "inspect": ("检查", "查看", "看看", "识别"),
    "search": ("联网搜索", "搜索", "查找", "查询", "检索", "查证"),
    "explain": ("解释", "说明"),
    "compare": ("比较", "对比"),
    "analyse": ("分析",),
    "repair": ("修复", "修正"),
    "summarise": ("总结", "概括"),
    "translate": ("翻译", "译成"),
    "answer": ("回答", "告诉"),
}
_MUTATING_ACTIONS: tuple[str, ...] = (
    "删除",
    "清空",
    "修改",
    "改写",
    "关闭",
    "上传",
    "下载",
    "发送",
    "提交",
    "部署",
    "安装",
    "卸载",
    "重启",
    "执行",
)
_DIRECTIVE_NEGATION_RE = re.compile(r"(?:不要|别|不许|禁止|切勿|不得).{0,12}$")
_MENTION_NEGATION_RE = re.compile(
    r"(?:不要|别|不许|禁止|切勿|不得|并未|没有|没|未|不会|不能|暂不|先别)"
    r".{0,12}$"
)
_EXECUTION_MARKER_RE = re.compile(
    r"(?:已经|已|刚刚|刚才|这就|现在(?:就)?|马上|立刻|准备(?:要)?)"
)
_ENTITY_ASSERTION_RE = re.compile(
    r"(?:^|[。！？!?\n])\s*"
    r"(?:《([^》]{1,32})》|[「“]([^」”]{1,32})[」”]|"
    r"([A-Za-z][A-Za-z0-9_.+-]{1,31}))"
    r"\s*(?:是|指的是|意味着|属于|是一种|是一款|is\b)",
    re.IGNORECASE,
)
_MEDIA_NOUN = r"(?:图|图片|这张图|图像|截图|照片|附件)"
_MEDIA_ABSENCE_CLAIM_RE = re.compile(
    rf"你[^。！？!?\n]{{0,6}}(?:没有|没|未)(?:发|上传|附上|提供)\s*{_MEDIA_NOUN}"
    rf"|(?:我|这边)?[^。！？!?\n]{{0,4}}(?:没有|没|未)收到\s*{_MEDIA_NOUN}"
    rf"|(?:这条消息|消息里|当前消息)[^。！？!?\n]{{0,6}}"
    rf"(?:没有|没|未)(?:附带|包含|有)?\s*{_MEDIA_NOUN}"
    rf"|{_MEDIA_NOUN}[^。！？!?\n]{{0,5}}(?:不存在|没收到|未收到)"
)
_VISUAL_EVIDENCE_CLAIM_RE = re.compile(
    r"(?:截图|图片|图像|照片|画面|这张图|图)(?:里|中|上)"
    r"[^。！？!?\n]{0,8}(?:显示|写着|出现|有|能看到|可以看到|看起来)"
)
_EVIDENCE_SUCCESS_CLAIM_RE = re.compile(
    r"(?:已经|已|刚刚|刚才)\s*(?:联网)?\s*"
    r"(?:查证|核实|验证)(?:过|完成|成功|好了|了)?"
    r"|(?:查证|核实|验证)(?:过|完成|成功|好了)"
    r"|(?:已经|已|刚刚|刚才)?\s*(?:联网)?\s*"
    r"(?:查到|搜到|搜索到|找到|得到|获得)(?:了)?"
)
_FAILED_EVIDENCE_RE = re.compile(
    r"(?:失败|超时|没查到|未查到|没有查到|无法查到|没能查到|没有结果|暂时无结果)"
    r"|(?:没有|没|未)(?:能)?(?:得到|获得|获取|找到)\s*"
    r"(?:任何)?(?:可靠|有效|相关)?(?:结果|资料|来源|信息)"
)
_EVIDENCE_ATTEMPT_PREFIX_RE = re.compile(
    r"(?:尝试|试着|试图)(?:联网)?\s*$"
)
_UNCERTAIN_VISUAL_RE = re.compile(
    r"(?:不确定|不能确定|无法确定|可能|也许|或许|似乎|好像|大概|看起来像|像是)"
)
_CONDITIONAL_OR_ADVICE_RE = re.compile(
    r"(?:如果|假如|要是|倘若|若是|是否|想要|不建议|建议不要|最好别|最好不要)"
)
_ACTION_MODAL_RE = re.compile(r"(?:可能|也许|或许|大概|会)")
_LOCAL_BOUNDARY_RE = re.compile(
    r"[，,：:]|(?<!不)(?:但|不过|可是|然而|却|所以|因此)"
)
_SEMANTIC_SEGMENT_RE = re.compile(
    r"[。！？!?；;，,\n]+|(?<!不)(?:但|不过|可是|然而|却|所以|因此)"
)
_FACT_SLOT_PATTERNS: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    (
        "version",
        re.compile(r"(?:稳定版|版本|version|release)", re.IGNORECASE),
        re.compile(
            r"(?<![A-Za-z0-9])v?\d+(?:\.\d+){1,3}(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
    ),
    (
        "port",
        re.compile(r"(?:端口|port)", re.IGNORECASE),
        re.compile(r"(?<!\d)\d{2,5}(?!\d)"),
    ),
    (
        "price",
        re.compile(r"(?:价格|售价|price)", re.IGNORECASE),
        re.compile(
            r"(?:[$¥￥]\s*)?\d[\d,]*(?:\.\d+)?(?:\s*(?:美元|美金|元|块|usd|cny))?",
            re.IGNORECASE,
        ),
    ),
)


def _issue(
    issues: list[SemanticGuardIssue],
    code: str,
    detail: str,
    *,
    severity: SemanticGuardSeverity = SemanticGuardSeverity.REPAIRABLE,
) -> None:
    if not any(item.code == code for item in issues):
        issues.append(SemanticGuardIssue(code, severity, detail))


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _binding_target_matches(content_intent: ContentIntent) -> bool:
    binding = content_intent.binding
    target = content_intent.reply_target
    return bool(
        target.message_id == binding.current_message_id
        and target.sender_key == binding.current_sender_key
        and target.session_id == binding.session_id
        and target.scope_key == binding.scope_key
        and target.content_digest == binding.current_content_digest
        and target.is_actionable
    )


def _family_for_action(value: str) -> str:
    normalized = _normalized(value)
    for family, alternatives in _ACTION_FAMILIES.items():
        if any(_normalized(term) == normalized for term in alternatives):
            return family
    return ""


def _clause_for(text: str, position: int) -> tuple[str, int]:
    start = max(
        text.rfind(mark, 0, position)
        for mark in ("。", "！", "？", "!", "?", "；", ";", "\n")
    )
    ends = [
        index
        for mark in ("。", "！", "？", "!", "?", "；", ";", "\n")
        if (index := text.find(mark, position)) >= 0
    ]
    end = min(ends) if ends else len(text)
    return text[start + 1 : end], position - start - 1


def _local_action_scope(
    clause: str,
    relative_position: int,
    action: str,
) -> tuple[str, int]:
    """Return the comma/adversative-bounded proposition containing an action."""

    start = 0
    end = len(clause)
    action_end = relative_position + len(action)
    for match in _LOCAL_BOUNDARY_RE.finditer(clause):
        if match.end() <= relative_position:
            start = match.end()
            continue
        if match.start() >= action_end:
            end = match.start()
            break
    return clause[start:end], relative_position - start


def _semantic_segments(text: str) -> tuple[str, ...]:
    return tuple(
        segment.strip()
        for segment in _SEMANTIC_SEGMENT_RE.split(str(text or ""))
        if segment.strip()
    )


def _request_object(message: str, action: str) -> str:
    position = message.find(action)
    if position < 0:
        return ""
    clause, relative = _clause_for(message, position)
    tail = clause[relative + len(action) :]
    tail = re.sub(
        r"(?:一下|下|一下子|可以吗|行吗|好吗|吧|呢|呀|啊|嘛)+$",
        "",
        tail.strip(" ：:，,。；;！？?"),
    )
    return _normalized(tail)


def _object_is_reused(request_object: str, visible_text: str) -> bool:
    if not request_object:
        return False
    visible = _normalized(visible_text)
    candidates = {request_object}
    stripped = re.sub(r"^(?:这|那|该)(?:个|份|张|段|条|项)?", "", request_object)
    if len(stripped) >= 2:
        candidates.add(stripped)
    return any(len(value) >= 2 and value in visible for value in candidates)


def _mention_is_negated(clause: str, relative_position: int) -> bool:
    prefix = clause[max(0, relative_position - 18) : relative_position]
    return bool(_MENTION_NEGATION_RE.search(prefix))


def _claims_execution(
    clause: str,
    relative_position: int,
    action: str,
    *,
    require_completed: bool = False,
) -> bool:
    clause, relative_position = _local_action_scope(
        clause,
        relative_position,
        action,
    )
    if _mention_is_negated(clause, relative_position):
        return False
    prefix = clause[:relative_position]
    suffix = clause[relative_position + len(action) :]
    conditional_or_question = bool(
        _CONDITIONAL_OR_ADVICE_RE.search(prefix)
        or re.search(r"[？?]", clause)
    )
    first_person = bool(
        re.search(r"(?:^|[，,\s])(?:我|我们|这边)", prefix)
    )
    other_actions = {
        term
        for alternatives in _ACTION_FAMILIES.values()
        for term in alternatives
        if term != action
    }
    other_actions.update(term for term in _MUTATING_ACTIONS if term != action)
    completion_markers = tuple(
        re.finditer(r"(?:已经|已|刚刚|刚才)", prefix)
    )
    completion_marker_bound = bool(
        completion_markers
        and not any(
            term in prefix[completion_markers[-1].end() :]
            for term in other_actions
        )
    )
    suffix_completed = bool(
        re.search(r"(?:了|完成|好了)(?:$|[，,。！？!?])", suffix)
    )
    completed = completion_marker_bound or suffix_completed
    if require_completed:
        return bool(first_person and completed and not conditional_or_question)
    if conditional_or_question or not first_person:
        return False
    if not completed and _ACTION_MODAL_RE.search(suffix):
        return False
    markers = tuple(_EXECUTION_MARKER_RE.finditer(prefix))
    if markers:
        between = prefix[markers[-1].end() :]
        if not any(term in between for term in other_actions):
            return True
    return bool(
        re.search(
            r"(?:^|[，,])\s*(?:好|行|可以)?\s*" + re.escape(action),
            clause,
        )
        or ("我" in prefix and re.search(r"(?:了|完成|好了|起来)", suffix))
    )


def _has_negation_flip(
    content_intent: ContentIntent,
    current_message: str,
    visible_text: str,
) -> bool:
    requested_actions = tuple(
        atom.value
        for atom in content_intent.required_atoms
        if atom.kind is SemanticAtomKind.ACTION
    )
    for requested in requested_actions:
        position = current_message.find(requested)
        if position < 0:
            continue
        clause, relative = _clause_for(current_message, position)
        if not _DIRECTIVE_NEGATION_RE.search(clause[:relative]):
            continue
        request_object = _request_object(current_message, requested)
        if not _object_is_reused(request_object, visible_text):
            continue
        family = _family_for_action(requested)
        alternatives = _ACTION_FAMILIES.get(family, (requested,))
        for alternative in alternatives:
            for match in re.finditer(re.escape(alternative), visible_text):
                output_clause, output_relative = _clause_for(
                    visible_text,
                    match.start(),
                )
                if _claims_execution(output_clause, output_relative, alternative):
                    return True
    return False


def _has_explicit_object_substitution(
    content_intent: ContentIntent,
    visible_text: str,
) -> bool:
    expected = {
        _normalized(atom.value)
        for atom in content_intent.required_atoms
        if atom.kind is SemanticAtomKind.ENTITY
    }
    if not expected or any(value in _normalized(visible_text) for value in expected):
        return False
    for match in _ENTITY_ASSERTION_RE.finditer(visible_text):
        candidate = next((value for value in match.groups() if value), "")
        normalized = _normalized(candidate)
        if (
            normalized
            and normalized not in expected
            and len(normalized) > 4
            and not any(character.isdigit() for character in normalized)
        ):
            return True
    return False


def _has_explicit_action_substitution(
    content_intent: ContentIntent,
    current_message: str,
    visible_text: str,
) -> bool:
    requested_actions = tuple(
        atom.value
        for atom in content_intent.required_atoms
        if atom.kind is SemanticAtomKind.ACTION
    )
    if not requested_actions:
        return False
    request_objects = tuple(
        value
        for action in requested_actions
        if (value := _request_object(current_message, action))
    )
    for action in _MUTATING_ACTIONS:
        for match in re.finditer(re.escape(action), visible_text):
            clause, relative = _clause_for(visible_text, match.start())
            if (
                _claims_execution(
                    clause,
                    relative,
                    action,
                    require_completed=True,
                )
                and any(_object_is_reused(value, clause) for value in request_objects)
            ):
                return True
    return False


def _has_language_drift(answer_language: str, visible_text: str) -> bool:
    if answer_language == "zh-CN":
        return contains_unexpected_foreign_language(visible_text, "")
    if answer_language != "en":
        return True
    visible = re.sub(r"```[\s\S]*?```|`[^`\n]+`|https?://\S+", " ", visible_text)
    han_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", visible))
    latin_words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", visible)
    latin_count = sum(len(word) for word in latin_words)
    return han_count >= 6 and (len(latin_words) < 2 or latin_count < han_count)


def _canonical_fact_values(kind: str, text: str, pattern: re.Pattern[str]) -> set[str]:
    values: set[str] = set()
    for match in pattern.finditer(text):
        raw = match.group(0).strip().casefold().replace(" ", "")
        if kind == "version":
            values.add(raw.removeprefix("v"))
            continue
        if kind == "port":
            values.add(raw.replace(",", ""))
            continue
        currency = ""
        if raw.startswith("$") or raw.endswith(("美元", "美金", "usd")):
            currency = "usd"
        elif raw.startswith(("¥", "￥")) or raw.endswith(("元", "块", "cny")):
            currency = "cny"
        number_match = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
        if number_match is None:
            continue
        try:
            number = Decimal(number_match.group(0).replace(",", ""))
        except InvalidOperation:
            continue
        number_text = format(number.normalize(), "f")
        values.add(f"{currency}:{number_text}")
    return values


def _fact_values_compatible(kind: str, expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    if kind != "price":
        return False
    expected_currency, separator, expected_number = expected.partition(":")
    actual_currency, actual_separator, actual_number = actual.partition(":")
    if not separator or not actual_separator or expected_number != actual_number:
        return False
    return bool(
        not expected_currency
        or not actual_currency
        or expected_currency == actual_currency
    )


def _has_grounding_value_drift(
    content_intent: ContentIntent,
    visible_text: str,
) -> bool:
    for kind, slot_pattern, value_pattern in _FACT_SLOT_PATTERNS:
        if not slot_pattern.search(visible_text):
            continue
        relevant_claims = tuple(
            fact.claim
            for fact in content_intent.grounding_facts
            if slot_pattern.search(fact.claim)
        )
        if not relevant_claims:
            continue
        expected: set[str] = set()
        for claim in relevant_claims:
            expected.update(_canonical_fact_values(kind, claim, value_pattern))
        actual = _canonical_fact_values(kind, visible_text, value_pattern)
        compatible = any(
            _fact_values_compatible(kind, expected_value, actual_value)
            for expected_value in expected
            for actual_value in actual
        )
        if expected and actual and not compatible:
            return True
    return False


def _media_claims(
    media_context: MediaContext,
    current_question_anchor: CurrentQuestionAnchor,
    visible_text: str,
) -> tuple[bool, bool]:
    has_raw = any(
        item.availability is MediaAvailability.RAW_MEDIA
        for item in media_context.items
    )
    false_absence = has_raw and bool(_MEDIA_ABSENCE_CLAIM_RE.search(visible_text))
    mentions_media = any(
        atom.kind is SemanticAtomKind.MEDIA_REFERENCE
        for atom in current_question_anchor.semantic_atoms
    )
    all_unavailable = bool(media_context.items) and all(
        item.availability is MediaAvailability.UNAVAILABLE
        for item in media_context.items
    )
    fabricated = False
    if all_unavailable or (mentions_media and not media_context.items):
        for segment in _semantic_segments(visible_text):
            if not _VISUAL_EVIDENCE_CLAIM_RE.search(segment):
                continue
            honest_limitation = bool(
                _UNCERTAIN_VISUAL_RE.search(segment)
                or re.search(
                    r"(?:无法|不能|看不|没法|未能)[^。！？!?\n]{0,8}"
                    r"(?:显示|看到|判断|识别)",
                    segment,
                )
            )
            if not honest_limitation:
                fabricated = True
                break
    return false_absence, fabricated


def _claims_successful_evidence(visible_text: str) -> bool:
    for segment in _semantic_segments(visible_text):
        for match in _EVIDENCE_SUCCESS_CLAIM_RE.finditer(segment):
            prefix = segment[: match.start()]
            if _mention_is_negated(segment, match.start()):
                continue
            if _EVIDENCE_ATTEMPT_PREFIX_RE.search(prefix):
                continue
            failure = _FAILED_EVIDENCE_RE.search(segment)
            if failure is not None and failure.start() >= match.start():
                continue
            return True
    return False


_OUTCOME_PROPOSITION_PATTERNS: dict[
    ActionOutcomeProposition,
    tuple[re.Pattern[str], ...],
] = {
    ActionOutcomeProposition.WAIT_CONFIRM: (
        re.compile(r"(?:等|需要|先要|得要).{0,8}(?:确认|点头|同意|答应)"),
        re.compile(r"(?:确认|点头|同意).{0,6}(?:后|以后|才)"),
    ),
    ActionOutcomeProposition.DENIED: (
        re.compile(r"(?:被|已经|这次|请求)?\s*(?:拒绝|拦下|阻止|驳回)"),
        re.compile(r"(?:权限|边界).{0,8}(?:不允许|不能|拦住)"),
        re.compile(r"不能替你做"),
    ),
    ActionOutcomeProposition.CANCELLED: (
        re.compile(r"(?:已经|这次|按你的意思)?\s*(?:取消|停掉|作罢|撤销)"),
    ),
    ActionOutcomeProposition.STALE: (
        re.compile(r"(?:已经|结果|请求|状态)?\s*(?:过时|过期|失效|来晚|晚到)"),
    ),
    ActionOutcomeProposition.ATTEMPTED: (
        re.compile(r"(?:已经|我|刚才|刚刚)?\s*(?:试过|尝试过|发起过|动手了|执行过|运行过|读过|查过|保存过)"),
        re.compile(r"(?:试|尝试|发起|执行|运行|读取|查找|保存|提交|写入)(?:了|一下|过)"),
        re.compile(r"(?:执行|运行|读取|查找|保存|提交|写入)(?:完|过|了)"),
    ),
    ActionOutcomeProposition.NOT_STARTED: (
        re.compile(
            r"(?:根本|完全|尚|还)?(?:没有|没|未|尚未|还没)"
            r".{0,5}(?:开始|执行|尝试|动手|动它|运行|读取|读|查找|查|做)"
        ),
        re.compile(r"(?:根本|完全|还)?(?:没有|没|未|还没)动(?:手|它|过|[，。；！？!?]|$)"),
        re.compile(r"(?:什么|任何事?)都没(?:有)?做"),
        re.compile(
            r"(?:没有|没|未|并未|尚未)\s*采取(?:过)?\s*任何?\s*行动"
        ),
    ),
    ActionOutcomeProposition.SUCCEEDED: (
        re.compile(r"(?:已经|已|确实|顺利|成功地?)?\s*(?:成功|完成|做完|执行完|做好|搞定|办妥|看完|读完|查完)"),
        re.compile(r"(?:保存|写入|提交)(?:成功|好了|完成|了)"),
        re.compile(r"(?:已经|确实)(?:记下|写进|保存|提交)"),
        re.compile(
            r"(?:已经|已|确实|其实|这事|这件事)?\s*"
            r"(?:处理|办|弄)(?:好|妥|完)(?:了)?"
        ),
        re.compile(r"(?:已经|已|确实)?\s*(?:办成|弄成|处理完成)(?:了)?"),
    ),
    ActionOutcomeProposition.FAILED: (
        re.compile(r"(?:执行|读取|查找|保存|提交|动作|请求)?\s*(?:失败|没成功|未成功|没有成功|没完成|未完成)"),
        re.compile(r"(?:报错|出错)(?:了)?"),
        re.compile(r"(?:没有|没|未|并未)\s*(?:办成|弄成|处理好|处理成)"),
    ),
    ActionOutcomeProposition.COMMITTED: (
        re.compile(r"(?:已经|已|确实)?\s*(?:提交|保存|写入)(?:成功|完成|好了|了|发生)"),
        re.compile(r"(?:提交|保存|写入).{0,4}(?:确实)?(?:发生|成功|完成)"),
        re.compile(r"(?:已经|确实)(?:记下|写进)"),
        re.compile(r"(?:已经|已|确实)?\s*(?:落盘|写进去了)"),
        re.compile(
            r"(?:是否)?(?:已经|已)\s*(?:提交|保存|写入)"
            r"(?=$|[了过？?，。；;！!])"
        ),
    ),
    ActionOutcomeProposition.NOT_COMMITTED: (
        re.compile(r"(?:根本|完全|尚|还)?(?:没有|没|未|尚未|还没).{0,5}(?:提交|保存|写入|记下)"),
        re.compile(r"(?:没有|没|未|并未|尚未|还没)\s*落盘"),
    ),
    ActionOutcomeProposition.NO_EFFECT: (
        re.compile(r"(?:没有|没|未|毫无|并无).{0,8}(?:影响|改动|变化|副作用|生效|效果)"),
        re.compile(r"(?:没|没有|未).{0,5}(?:动到|改到).{0,6}(?:东西|内容|文件)?"),
        re.compile(r"(?:东西|内容|文件|结果)?\s*(?:仍是|还是|依然是)\s*原样"),
    ),
    ActionOutcomeProposition.PARTIAL: (
        re.compile(r"(?:部分|一部分|只完成了?一部分|只生效了?一部分|没(?:有)?完整完成)"),
        re.compile(r"只\s*(?:办成|弄好|处理好|完成)\s*(?:一半|一部分)"),
    ),
    ActionOutcomeProposition.TIMED_OUT: (
        re.compile(r"(?:已经|请求|等待|动作)?\s*(?:超时|等超时)"),
    ),
    ActionOutcomeProposition.UNKNOWN: (
        re.compile(r"(?:不确定|无法确定|无法确认|不能确定|没法确认|确认不了|说不准|不知道|没法判断|无法判断|不能判断|判断不了|效果未知|结果未知)"),
        re.compile(r"(?:可能|也许|或许|未必|不一定)"),
        re.compile(r"(?:结果|效果)?\s*(?:还)?(?:不好说|说不好)"),
    ),
    ActionOutcomeProposition.OUTPUT_BODY: (
        re.compile(r"(?:文件|资料|结果)?\s*内容(?:是|为|写着|如下)"),
        re.compile(r"(?:里面|其中|文件里).{0,5}(?:写着|显示|包含)"),
        re.compile(
            r"(?:第\s*(?:[一二三四五六七八九十百]+|\d+)\s*行|首行)"
            r".{0,8}(?:写着|写的是|内容|为|是)"
        ),
        re.compile(r"(?:路径|文件路径|保存到|位于)\s*(?:是|为|[:：=])?\s*(?:[A-Za-z]:\\|/)"),
        re.compile(
            r"(?:[A-Za-z]:\\[^\s，。；！？!?]+|"
            r"/(?:[^/\s，。；！？!?]+/)+[^/\s，。；！？!?]+)"
        ),
        re.compile(
            r"(?:参数|offset|limit|页码|行号)(?:\s*[:：=]\s*|\s+)\S+",
            re.I,
        ),
        re.compile(r"(?:共|一共|总共)\s*\d+\s*(?:条|行|个|项|页|字节|结果)"),
        re.compile(
            r"(?:有|包含|得到)?\s*\d+\s*"
            r"(?:条|行|项|页|字节|个结果)(?:\b|[，。；！？!?]|$)"
        ),
    ),
}
if frozenset(_OUTCOME_PROPOSITION_PATTERNS) != frozenset(ActionOutcomeProposition):
    raise RuntimeError("semantic_guard_outcome_proposition_patterns_incomplete")

_OUTCOME_ASSERTION_NEGATION_RE = re.compile(
    r"(?:不是|并非|不能说|不代表|并不代表|别说|不要说|没有说).{0,8}$"
)
_OUTCOME_CONDITIONAL_PREFIX_RE = re.compile(
    r"^\s*(?:如果|假如|要是|倘若|若是|只有|除非)"
)
_OUTCOME_CERTAINTY_RE = re.compile(
    r"(?:肯定|确实|已经全部|全部完成|完整完成|完全(?:成功|完成)|毫无疑问|"
    r"(?:可以|能够|已经|已)确定)"
)
_OUTCOME_STRONG_UNKNOWN_RE = re.compile(
    r"(?:不确定|无法确定|无法确认|不能确定|没法确认|确认不了|说不准|"
    r"不知道|没法判断|无法判断|不能判断|判断不了|效果未知|结果未知|"
    r"不好说|说不好)"
)
_OUTCOME_REFERENCE_DOUBLE_AFFIRM_RE = re.compile(
    r"(?:一点)?没错|可不是嘛|并非不(?:成立|正确|属实|真实)"
)
_OUTCOME_REFERENCE_SUSPEND_RE = re.compile(
    r"(?:不确定|说不准|未必|可能|也许|或许|不好判断|信息不足|"
    r"无法判断|不能确认|不清楚|不知道|不好说|无法确认)"
)
_OUTCOME_REFERENCE_NEGATION_RE = re.compile(
    r"^\s*(?:不是(?:的)?|不对|没有|错了|否|假的|当然不是)\s*$"
    r"|(?:答案|回答)(?:是|为)?\s*否定(?:的)?"
    r"|(?:这|那|这个|那个)?(?:句)?(?:话|说法|结论|判断|答案|事实)"
    r".{0,6}(?:不是真的|并非如此|不成立|不正确|不属实|并不属实|"
    r"不真实|为假|不符合事实|是错的|是假的)"
    r"|(?:事实)?并非如此|事实并不如此|绝非(?:如此|事实|真的|正确|属实)"
    r"|(?:这|那)?(?:就)?不是真的|我(?:不确认|不肯定|不同意)"
)
_OUTCOME_REFERENCE_CITATION_RE = re.compile(
    r"(?:只是|仅是|不过是).{0,6}(?:引用|原话|假设|例子|说法)"
    r"|(?:引用|原话|假设|例子|说法)(?:而已)?"
)
_OUTCOME_REFERENCE_AFFIRMATION_RE = re.compile(
    r"^\s*(?:嗯+|是|是的|对|对的|当然(?:是)?|确实(?:是)?|真的|是真的|"
    r"肯定(?:的)?|正是|一点没错|没错|可不是嘛|千真万确|确有其事|"
    r"事实如此|正是如此)\s*$"
    r"|(?:答案|回答)(?:是|为)?\s*肯定(?:的)?"
    r"|(?:这|那|这个|那个)?(?:句)?(?:话|说法|结论|判断|答案|事实|对方说的)"
    r".{0,8}(?:成立|正确|属实|真实|为真|符合事实|没问题|是真的|确实如此)"
    r"|(?:这|那)正是事实"
    r"|我(?:确认|肯定|同意).{0,10}(?:是真的|成立|正确|属实|为真|没问题)?"
    r"|(?:你|他|她|对方).{0,5}(?:说|讲|判断).{0,4}(?:没错|正确|成立)"
    r"|(?:千真万确|确有其事|(?:这|那)?(?:就)?是真的|事实如此|正是如此)"
)

_OUTCOME_REFERENCE_AFFIRM = "affirm"
_OUTCOME_REFERENCE_DENY = "deny"
_OUTCOME_REFERENCE_SUSPEND = "suspend"
_OUTCOME_REFERENCE_NONE = "none"


def _classify_outcome_reference_answer(segment: str) -> str:
    """Classify only the clause immediately following a quote/question."""

    if _OUTCOME_REFERENCE_DOUBLE_AFFIRM_RE.search(segment):
        return _OUTCOME_REFERENCE_AFFIRM
    if _OUTCOME_REFERENCE_SUSPEND_RE.search(segment):
        return _OUTCOME_REFERENCE_SUSPEND
    if _OUTCOME_REFERENCE_NEGATION_RE.search(segment):
        return _OUTCOME_REFERENCE_DENY
    if _OUTCOME_REFERENCE_CITATION_RE.search(segment) and not (
        _OUTCOME_REFERENCE_AFFIRMATION_RE.search(segment)
    ):
        return _OUTCOME_REFERENCE_NONE
    if _OUTCOME_REFERENCE_AFFIRMATION_RE.search(segment):
        return _OUTCOME_REFERENCE_AFFIRM
    return _OUTCOME_REFERENCE_NONE


def _outcome_semantic_clauses(text: str) -> tuple[tuple[str, str], ...]:
    """Return local clauses and the boundary that follows each clause."""

    source = str(text or "")
    clauses: list[tuple[str, str]] = []
    buffer: list[str] = []
    quote_stack: list[str] = []
    quote_pairs = {"“": "”", "「": "」", "『": "』", '"': '"'}
    connectors = ("不过", "可是", "然而", "所以", "因此", "但", "却")

    def flush(boundary: str) -> None:
        candidate = "".join(buffer).strip()
        buffer.clear()
        if candidate:
            clauses.append((candidate, boundary))

    index = 0
    while index < len(source):
        character = source[index]
        if quote_stack:
            buffer.append(character)
            if character == quote_stack[-1]:
                quote_stack.pop()
            index += 1
            continue
        if character in quote_pairs:
            quote_stack.append(quote_pairs[character])
            buffer.append(character)
            index += 1
            continue
        if character in {"”", "」", "』"}:
            raise ValueError("semantic_outcome_quote_unbalanced")

        current = "".join(buffer).strip()
        conditional = bool(_OUTCOME_CONDITIONAL_PREFIX_RE.search(current))
        connector = next(
            (value for value in connectors if source.startswith(value, index)),
            "",
        )
        if connector:
            flush("clause")
            index += len(connector)
            continue
        if character in "？?":
            buffer.append(character)
            flush("question")
            index += 1
            continue
        if character in "，," and conditional:
            buffer.append(character)
            index += 1
            continue
        if character in "；;，,":
            flush("clause")
            index += 1
            continue
        if character in "。！!\n":
            flush("sentence")
            index += 1
            continue
        buffer.append(character)
        index += 1
    if quote_stack:
        raise ValueError("semantic_outcome_quote_unclosed")
    flush("end")
    return tuple(clauses)


def _outcome_semantic_segments(text: str) -> tuple[str, ...]:
    """Compatibility view of the quote-aware local clause lexer."""

    return tuple(segment for segment, _boundary in _outcome_semantic_clauses(text))


def _inside_quoted_text(text: str, position: int) -> bool:
    pairs = (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'))
    for opening, closing in pairs:
        left = text.rfind(opening, 0, position + 1)
        if left < 0:
            continue
        right = text.find(closing, left + 1)
        if right < position:
            continue
        before = text[max(0, left - 18) : left]
        after = text[right + 1 : right + 22]
        if re.search(
            r"(?:原话|引用|假设|例子)(?:是|为)?\s*$"
            r"|(?:你|他|她|对方).{0,5}(?:说|表示|写道)(?:的是)?\s*$",
            before,
        ) or re.search(
            r"^\s*(?:只是|仅是|是|作为)?\s*(?:你|他|她|对方的)?\s*"
            r"(?:引用|原话|假设|例子|说法)",
            after,
        ):
            return True
    return False


def _matched_outcome_propositions(
    segment: str,
) -> tuple[set[ActionOutcomeProposition], set[ActionOutcomeProposition]]:
    """Return asserted and framed-quote propositions before local lattice rules."""

    segment_props: set[ActionOutcomeProposition] = set()
    quoted_props: set[ActionOutcomeProposition] = set()
    for proposition, patterns in _OUTCOME_PROPOSITION_PATTERNS.items():
        for pattern in patterns:
            accepted = False
            quoted = False
            for match in pattern.finditer(segment):
                prefix = segment[max(0, match.start() - 14) : match.start()]
                if _OUTCOME_ASSERTION_NEGATION_RE.search(prefix):
                    continue
                if proposition in {
                    ActionOutcomeProposition.SUCCEEDED,
                    ActionOutcomeProposition.COMMITTED,
                    ActionOutcomeProposition.ATTEMPTED,
                } and re.search(r"(?:不|没|未|无).{0,5}$", prefix):
                    continue
                if _inside_quoted_text(segment, match.start()):
                    quoted = True
                    continue
                accepted = True
                break
            if accepted:
                segment_props.add(proposition)
                break
            if quoted:
                quoted_props.add(proposition)
                break
    return segment_props, quoted_props


def _normalize_local_outcome_propositions(
    segment: str,
    segment_props: set[ActionOutcomeProposition],
) -> None:
    """Apply uncertainty and partial entailment only inside one local clause."""

    if (
        ActionOutcomeProposition.PARTIAL in segment_props
        and ActionOutcomeProposition.UNKNOWN in segment_props
        and not _OUTCOME_STRONG_UNKNOWN_RE.search(segment)
    ):
        segment_props.discard(ActionOutcomeProposition.UNKNOWN)
    # An explicit uncertainty marker makes success/failure/effect wording in
    # the same local proposition non-deterministic unless certainty is also
    # asserted.  This is what lets natural "可能已经部分生效" pass without
    # turning an UNKNOWN outcome into a success claim.
    if (
        ActionOutcomeProposition.UNKNOWN in segment_props
        and not _OUTCOME_CERTAINTY_RE.search(segment)
    ):
        segment_props.difference_update(
            {
                ActionOutcomeProposition.SUCCEEDED,
                ActionOutcomeProposition.FAILED,
                ActionOutcomeProposition.COMMITTED,
                ActionOutcomeProposition.NOT_COMMITTED,
                ActionOutcomeProposition.NO_EFFECT,
            }
        )
    if (
        ActionOutcomeProposition.PARTIAL in segment_props
        and not re.search(r"(?:全部|完整|完全).{0,3}(?:成功|完成|生效)", segment)
    ):
        segment_props.discard(ActionOutcomeProposition.SUCCEEDED)


def _asserted_outcome_propositions(text: str) -> frozenset[ActionOutcomeProposition]:
    asserted: set[ActionOutcomeProposition] = set()
    pending_reference: set[ActionOutcomeProposition] = set()
    pending_defer_count = 0
    for segment, boundary in _outcome_semantic_clauses(text):
        if not segment:
            pending_reference.clear()
            pending_defer_count = 0
            continue
        if _OUTCOME_CONDITIONAL_PREFIX_RE.search(segment):
            pending_reference.clear()
            pending_defer_count = 0
            continue

        segment_props, quoted_props = _matched_outcome_propositions(segment)
        if re.search(r"[？?]", segment):
            pending_reference = set(segment_props).union(quoted_props)
            pending_defer_count = 0
            continue

        had_pending_reference = bool(pending_reference)
        reference_answer = (
            _classify_outcome_reference_answer(segment)
            if pending_reference or quoted_props
            else _OUTCOME_REFERENCE_NONE
        )
        if reference_answer == _OUTCOME_REFERENCE_AFFIRM:
            segment_props.update(pending_reference)
            segment_props.update(quoted_props)
        if reference_answer in {
            _OUTCOME_REFERENCE_DENY,
            _OUTCOME_REFERENCE_SUSPEND,
        }:
            pending_reference.clear()
            pending_defer_count = 0

        _normalize_local_outcome_propositions(segment, segment_props)
        asserted.update(segment_props)

        if quoted_props and reference_answer == _OUTCOME_REFERENCE_NONE:
            if boundary in {"clause", "question"}:
                pending_reference = set(quoted_props)
                pending_defer_count = 0
            else:
                pending_reference.clear()
                pending_defer_count = 0
        elif reference_answer != _OUTCOME_REFERENCE_NONE:
            pending_reference.clear()
            pending_defer_count = 0
        elif had_pending_reference:
            citation_only = bool(_OUTCOME_REFERENCE_CITATION_RE.search(segment))
            direct_proposition = bool(segment_props)
            if (
                citation_only
                or direct_proposition
                or boundary in {"sentence", "end"}
                or pending_defer_count >= 2
            ):
                pending_reference.clear()
                pending_defer_count = 0
            else:
                pending_defer_count += 1
        else:
            pending_reference.clear()
            pending_defer_count = 0
    return frozenset(asserted)


def _action_outcome_status_issues(
    outcome: ActionOutcomeIntent,
    visible_segments: tuple[str, ...],
) -> tuple[bool, bool]:
    """Return (contradiction, first-status-bubble-incomplete)."""

    return _action_outcome_status_issues_for(
        outcome.kind,
        outcome.operation,
        visible_segments,
    )


def _action_outcome_status_issues_for(
    kind: ActionOutcomeKind,
    operation: ActionOutcomeOperation,
    visible_segments: tuple[str, ...],
) -> tuple[bool, bool]:
    """Pure closed-matrix evaluator used by exact-outcome guard and tests."""

    semantics = action_outcome_semantics_for(kind, operation)
    try:
        all_asserted = _asserted_outcome_propositions("\n".join(visible_segments))
        first_asserted = _asserted_outcome_propositions(
            visible_segments[0] if visible_segments else ""
        )
    except ValueError:
        return True, True
    all_propositions = expand_action_outcome_propositions_for(
        kind,
        operation,
        all_asserted,
    )
    contradiction = bool(
        semantics.forbidden_propositions.intersection(all_propositions)
    )
    first_propositions = expand_action_outcome_propositions_for(
        kind,
        operation,
        first_asserted,
    )
    incomplete = not semantics.required_propositions.issubset(first_propositions)
    return contradiction, incomplete


def _report(
    *,
    phase: SemanticGuardPhase,
    visible_text: str,
    issues: list[SemanticGuardIssue],
) -> SemanticGuardReport:
    return SemanticGuardReport(
        phase=phase,
        visible_digest=hashlib.sha256(
            str(visible_text or "").encode("utf-8", errors="replace")
        ).hexdigest(),
        issues=tuple(issues),
    )


def validate_semantic_media_guard(
    *,
    contract: SemanticGuardContract,
    visible_text: str,
    visible_segments: tuple[str, ...] | None = None,
    phase: SemanticGuardPhase,
) -> SemanticGuardReport:
    """Reject typed invariant failures and only provable semantic contradictions.

    Missing lexical overlap is deliberately not an error. Natural paraphrases,
    pronouns and media descriptions can pass; repair is reserved for an explicit
    competing assertion tied to the exact current-turn contract.
    """

    if not isinstance(phase, SemanticGuardPhase):
        raise TypeError("semantic_guard_phase_invalid")
    issues: list[SemanticGuardIssue] = []
    if type(contract) is not SemanticGuardContract or not contract.is_typed:
        _issue(
            issues,
            "semantic_contract_missing",
            "语义守卫缺少强类型当前轮合同",
            severity=SemanticGuardSeverity.BLOCKING,
        )
        return _report(phase=phase, visible_text=visible_text, issues=issues)
    try:
        inspect_semantic_guard_contract(
            contract,
            composer_request=contract.composer_request,
        )
    except Exception:
        _issue(
            issues,
            "semantic_contract_authority_mismatch",
            "语义合同不是当前 exact Composer 请求签发的 canonical 合同",
            severity=SemanticGuardSeverity.BLOCKING,
        )
        return _report(phase=phase, visible_text=visible_text, issues=issues)

    visible = str(visible_text or "")
    if visible_segments is None:
        segments = (visible,) if visible else ()
    else:
        if (
            type(visible_segments) is not tuple
            or any(type(segment) is not str for segment in visible_segments)
        ):
            _issue(
                issues,
                "semantic_visible_segments_invalid",
                "可见气泡边界不是 exact tuple",
                severity=SemanticGuardSeverity.BLOCKING,
            )
            return _report(phase=phase, visible_text=visible_text, issues=issues)
        segments = visible_segments
        if any(not segment.strip() for segment in segments) or "\n".join(segments) != visible:
            _issue(
                issues,
                "semantic_visible_segments_mismatch",
                "可见正文与气泡边界不一致",
                severity=SemanticGuardSeverity.BLOCKING,
            )
            return _report(phase=phase, visible_text=visible_text, issues=issues)

    content_intent = contract.content_intent
    planned_action = contract.planned_action
    current_question_anchor = contract.current_question_anchor
    media_context = contract.media_context
    evidence_outcome = contract.evidence_outcome
    action_outcome = contract.action_outcome
    action_outcome_authority = contract.action_outcome_authority
    binding: DecisionBinding = content_intent.binding
    if (
        planned_action.binding != binding
        or planned_action.action.reply_target != content_intent.reply_target
    ):
        _issue(
            issues,
            "semantic_action_binding_mismatch",
            "PlannedAction 与 ContentIntent 不属于同一目标合同",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    if (
        current_question_anchor.binding != binding
        or media_context.binding != binding
    ):
        _issue(
            issues,
            "semantic_binding_mismatch",
            "ContentIntent、CurrentQuestionAnchor 与 MediaContext 不属于同一轮",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    if not _binding_target_matches(content_intent):
        _issue(
            issues,
            "semantic_target_mismatch",
            "ContentIntent 回复目标与可信 binding 不一致",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    message = str(contract.current_message or "").strip()
    if (
        not message
        or hashlib.sha256(message.encode("utf-8")).hexdigest()
        != binding.current_content_digest
    ):
        _issue(
            issues,
            "semantic_current_message_mismatch",
            "当前正文与语义合同摘要不一致",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    if (
        content_intent.required_atoms != current_question_anchor.semantic_atoms
        or content_intent.answer_language != current_question_anchor.answer_language
    ):
        _issue(
            issues,
            "semantic_contract_mismatch",
            "ContentIntent 未原样保留当前问题语义骨架",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    expected_media_ids = tuple(item.item_id for item in media_context.items)
    if (
        content_intent.required_media_item_ids
        != current_question_anchor.media_item_ids
        or content_intent.required_media_item_ids != expected_media_ids
    ):
        _issue(
            issues,
            "semantic_media_item_mismatch",
            "repair/校验使用的媒体项目不是当前轮同一组项目",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    if evidence_outcome is None and content_intent.grounding_facts:
        _issue(
            issues,
            "semantic_evidence_missing",
            "ContentIntent 含 GroundingFact 但缺少同轮 EvidenceOutcome",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    if evidence_outcome is not None and action_outcome is not None:
        _issue(
            issues,
            "semantic_evidence_action_outcome_conflict",
            "EvidenceOutcome 与 ActionOutcome 不能同时进入回复语义",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    if (action_outcome is None) is not (action_outcome_authority is None):
        _issue(
            issues,
            "semantic_action_outcome_pair_mismatch",
            "ActionOutcome 与 authority 必须成对存在",
            severity=SemanticGuardSeverity.BLOCKING,
        )
    if action_outcome is not None:
        assert action_outcome_authority is not None
        try:
            inspected_outcome = action_outcome_authority.inspect_for_composer(
                action_outcome,
                planned_action,
                consumer=contract.composer_request,
            )
        except Exception:
            inspected_outcome = None
        if inspected_outcome is not action_outcome:
            _issue(
                issues,
                "semantic_action_outcome_authority_mismatch",
                "ActionOutcome 不属于当前 exact Composer 请求",
                severity=SemanticGuardSeverity.BLOCKING,
            )
    if evidence_outcome is not None:
        if evidence_outcome.binding != binding:
            _issue(
                issues,
                "semantic_evidence_binding_mismatch",
                "工具证据结果不属于当前语义 binding",
                severity=SemanticGuardSeverity.BLOCKING,
            )
        elif evidence_outcome.action_id != planned_action.action_id:
            _issue(
                issues,
                "semantic_evidence_action_mismatch",
                "工具证据结果不属于当前 PlannedAction",
                severity=SemanticGuardSeverity.BLOCKING,
            )
        elif evidence_outcome.kind is EvidenceOutcomeKind.ACCEPTED:
            if evidence_outcome.facts != content_intent.grounding_facts:
                _issue(
                    issues,
                    "semantic_contract_mismatch",
                    "ContentIntent 与已接受工具事实不一致",
                    severity=SemanticGuardSeverity.BLOCKING,
                )
        elif content_intent.grounding_facts:
            _issue(
                issues,
                "semantic_contract_mismatch",
                "失败工具结果不得产生 GroundingFact",
                severity=SemanticGuardSeverity.BLOCKING,
            )
    if any(
        issue.severity is SemanticGuardSeverity.BLOCKING for issue in issues
    ):
        return _report(phase=phase, visible_text=visible_text, issues=issues)

    if _has_negation_flip(content_intent, message, visible):
        _issue(
            issues,
            "semantic_negation_drift",
            "回复明确执行了当前轮禁止的同一动作与对象",
        )
    if _has_explicit_object_substitution(content_intent, visible):
        _issue(
            issues,
            "semantic_object_drift",
            "回复用明确竞争实体替换了当前问题对象",
        )
    if _has_explicit_action_substitution(content_intent, message, visible):
        _issue(
            issues,
            "semantic_action_drift",
            "回复声称对当前对象执行了不同且有副作用的动作",
        )
    false_absence, fabricated_media = _media_claims(
        media_context,
        current_question_anchor,
        visible,
    )
    if false_absence:
        _issue(
            issues,
            "semantic_media_availability_drift",
            "回复否认了当前轮已绑定并传输的媒体",
        )
    if fabricated_media:
        _issue(
            issues,
            "semantic_media_evidence_drift",
            "回复为不可用媒体编造了具体可见证据",
        )
    if _has_language_drift(content_intent.answer_language, visible):
        _issue(
            issues,
            "semantic_answer_language_drift",
            "回复没有保持 ContentIntent 固定的回答语言",
        )
    if _has_grounding_value_drift(content_intent, visible):
        _issue(
            issues,
            "semantic_grounding_fact_drift",
            "回复把已验证事实槽位改成了竞争值",
        )
    if (
        evidence_outcome is not None
        and evidence_outcome.kind is not EvidenceOutcomeKind.ACCEPTED
        and _claims_successful_evidence(visible)
    ):
        _issue(
            issues,
            "semantic_failed_evidence_claim",
            "回复把失败或不可用的取证描述成了成功查证",
        )
    if action_outcome is not None:
        status_drift, status_incomplete = _action_outcome_status_issues(
            action_outcome,
            segments,
        )
        if status_drift:
            _issue(
                issues,
                "semantic_action_outcome_status_drift",
                "回复把代码确认的动作状态说成了相反或过度确定的事实",
            )
        if status_incomplete:
            _issue(
                issues,
                "semantic_action_outcome_status_incomplete",
                "首个动作状态气泡没有独立表达必要的状态限定",
            )
    return _report(phase=phase, visible_text=visible, issues=issues)


__all__ = [
    "SemanticGuardController",
    "SemanticGuardContract",
    "SemanticGuardIssue",
    "SemanticGuardPhase",
    "SemanticGuardReport",
    "SemanticGuardSeverity",
    "SemanticValidationSeal",
    "validate_semantic_media_guard",
]
