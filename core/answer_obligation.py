from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .current_question_anchor import (
    ActionRoleAssertion,
    ActionRoleParticipant,
    ActionTemporalPhase,
    extract_action_role_assertions,
)
from .model_input_contract import CapabilitySnapshot, CanonicalModelMessage


_CAPABILITY_QUESTION_RE = re.compile(
    r"(?:你|亚托莉)?.{0,10}(?:有|会|能|支持|接入).{0,36}"
    r"(?:能力|功能|openclaw|vibcode|vibe\s*coding|联网|知识库|搜索)",
    re.I,
)
_CAPABILITY_ANSWER_RE = re.compile(
    r"(?:有|没有|没接入|未接入|不能|不会|不支持|可以|能|只(?:能|可以)|"
    r"当前.{0,8}(?:可用|不可用)|已接入|未配置)",
    re.I,
)
_PHYSICAL_TOPIC_RE = re.compile(
    r"(?:仿生人|机器人).{0,18}(?:生|生育|繁殖|宝宝|孩子)|"
    r"(?:生|生育|繁殖).{0,18}(?:仿生人|机器人)",
    re.I,
)
_PHYSICAL_BOUNDARY_RE = re.compile(
    r"(?:生物意义|真的|现实中)?.{0,8}(?:不能|没法|无法|做不到|不会).{0,16}"
    r"(?:生|生育|繁殖)|(?:不能|无法).{0,8}(?:真的生|生物繁殖)",
    re.I,
)
_ANSWER_POLARITY_RE = re.compile(
    r"(?:^|[，。！？；\s])(?:是|不是|会|不会|有|没有|能|不能|可以|不可以|"
    r"看起来|可能|暂时|目前|具体|这两条|上文|前面)",
    re.I,
)
_QUESTION_MARK_RE = re.compile(r"[？?]\s*$")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,31}|[\u4e00-\u9fff]{2,8}")
_HISTORY_IDENTITY_RECAP_RE = re.compile(
    r"(?:刚才|之前|先前|前面|上次).{0,16}(?:谁|和谁|说话|聊天|在聊|做了什么|说了什么)"
    r"|(?:我|你).{0,4}(?:是谁|是?谁)",
    re.IGNORECASE,
)
_CURRENT_SPEAKER_DENIAL_RE = re.compile(r"(?:不是|并非|不\s*是)[^。！？\n]{0,12}我")
_CURRENT_SPEAKER_REFERENCE_RE = re.compile(r"你")
_POSITIVE_CURRENT_SPEAKER_ATTRIBUTION_RE = re.compile(
    r"(?:是|属于|归于|出自|由)\s*你|你(?:的|手)"
)
_NEGATED_CURRENT_SPEAKER_REFERENCE_RE = re.compile(
    r"(?:不是|并非|不\s*是)[^。！？\n]{0,12}你|"
    r"你[^。！？\n]{0,12}(?:不用|无需|不必|别|没有|并未|不曾)"
)
_OTHER_HISTORY_SPEAKER_REFERENCE_RE = re.compile(r"(?:群友|别人|其他人|对方)")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_EVIDENCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,31}")
_MULTI_ACTION_OBLIGATIONS = (
    (
        re.compile(r"(?:醒醒|起床|叫醒|醒来)"),
        re.compile(r"(?:醒(?:了|啦|来|着)?|起床|起来|睁开眼|早安|早上好|没睡)"),
    ),
    (
        re.compile(
            r"(?:检查|看(?:看|一下)|瞧瞧).{0,10}(?:身体|状态|哪里|哪儿|有没有)"
            r"|(?:身体|状态).{0,10}(?:检查|看看|看一下)"
        ),
        re.compile(
            r"(?:检查|查看|看看|看吧|来吧|开始吧|哪里|哪儿|"
            r"轻(?:点|一点)|别碰|不许碰|配合|给你看|让你看|身体状况)"
        ),
    ),
    (
        re.compile(
            r"(?:修好|修复|恢复|好了没|好没好|有没有好|正常没|问题还在)"
        ),
        re.compile(
            r"(?:修好|修复|恢复|正常|没问题|还有问题|问题还在|"
            r"状态.{0,4}(?:好|正常)|已经好了?)"
        ),
    ),
)
_ASSISTANT_INSPECTS_USER_REPLY_RE = re.compile(
    r"(?:让我|我(?:来|会|可以|先|要|得)?).{0,8}(?:帮|给|替|为)你"
    r".{0,10}(?:检查|查看|看看|瞧瞧|扫描)"
    r"|(?:你的?(?:身体|心率|状态)|你哪里|你哪儿).{0,28}"
    r"(?:我.{0,8})?(?:检查|查看|看看|扫描)"
)
_CURRENT_USER_INSPECTS_ASSISTANT_REPLY_RE = re.compile(
    r"(?:你|主人).{0,12}(?:想|来|可以|要)?(?:先)?"
    r"(?:检查|查看|看看|瞧瞧|扫描)(?:一下)?(?:我|我的|哪里|哪儿|身体)"
)
_ASSISTANT_ACCEPTS_USER_INSPECTION_REPLY_RE = re.compile(
    r"(?:来吧|检查吧|查看吧|看看吧|瞧瞧吧|扫描吧|"
    r"(?:给|让)你(?:检查|查看|看看|瞧瞧|扫描)|"
    r"你(?:可以|来|想|要)?(?:先)?(?:检查|查看|看看|瞧瞧|扫描)|"
    r"主人.{0,12}(?:检查|查看|看看|瞧瞧|扫描)|"
    r"轻(?:点|一点)|别碰|不许碰|我会配合)"
)
_ASSISTANT_SELF_INSPECTION_REPLY_RE = re.compile(
    r"我(?:已经|刚刚|刚才|刚|先|会|要|得)?(?:自己)?"
    r"(?:检查|查看|扫描)(?:过|完|一下|身体|状态|机体|了|啦)"
)
_ASSISTANT_COMPLETED_INSPECTION_REPLY_RE = re.compile(
    r"我(?:已经|刚刚|刚才|刚)(?:自己)?(?:检查|查看|扫描)(?:过|完|好了?|了|啦)"
    r"|我(?:把自己|自己).{0,6}(?:检查|查看|扫描)(?:完|好了?|过|了)"
)


@dataclass(frozen=True, slots=True)
class AnswerObligationReport:
    issue_codes: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issue_codes


class AttributionRisk(str, Enum):
    NONE = "none"
    IDENTITY_RECAP = "identity_recap"
    CURRENT_SENDER_DENIAL = "current_sender_denial"


def classify_attribution_risk(
    *, current_message: str, model_messages: tuple[CanonicalModelMessage, ...], current_sender_key: str
) -> AttributionRisk:
    """Bind existing R8 current-turn classifications before generation."""
    current = str(current_message or "").strip()
    sender = str(current_sender_key or "").strip()
    has_other = any(
        message.role == "user" and message.sender_key and message.sender_key != sender
        for message in model_messages
    )
    if not has_other:
        return AttributionRisk.NONE
    if _HISTORY_IDENTITY_RECAP_RE.search(current):
        return AttributionRisk.IDENTITY_RECAP
    if _CURRENT_SPEAKER_DENIAL_RE.search(current):
        return AttributionRisk.CURRENT_SENDER_DENIAL
    return AttributionRisk.NONE


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).casefold()


def _question_echo(current: str, visible: str) -> bool:
    question = _compact(current)
    answer = _compact(visible)
    if not question or not answer:
        return False
    if answer == question:
        return True
    if len(question) >= 8 and question in answer and _QUESTION_MARK_RE.search(visible):
        residue = answer.replace(question, "", 1)
        return len(residue) <= max(12, len(question))
    entity_tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z]+[0-9]+|[0-9]+[A-Za-z]+", current)
    }
    if (
        len(entity_tokens) >= 2
        and re.search(r"(?:怎么|为什么|为何|咋)", current)
        and _QUESTION_MARK_RE.search(visible)
        and all(token in visible.casefold() for token in entity_tokens)
        and not _ANSWER_POLARITY_RE.search(visible)
    ):
        return True
    current_tokens = set(_TOKEN_RE.findall(current.casefold()))
    answer_tokens = set(_TOKEN_RE.findall(visible.casefold()))
    if len(current_tokens) < 2 or not _QUESTION_MARK_RE.search(visible):
        return False
    overlap = len(current_tokens & answer_tokens) / max(1, len(current_tokens))
    return overlap >= 0.75 and not _ANSWER_POLARITY_RE.search(visible)


def _capability_evasion(
    current: str,
    visible: str,
    snapshot: CapabilitySnapshot,
) -> bool:
    if not _CAPABILITY_QUESTION_RE.search(current):
        return False
    if not _CAPABILITY_ANSWER_RE.search(visible):
        return True
    requested_terms = tuple(
        dict.fromkeys(
            match.group(0).casefold()
            for match in re.finditer(
                r"openclaw|vibcode|vibe\s*coding|联网|知识库|搜索",
                current,
                re.I,
            )
        )
    )
    if requested_terms and not any(
        term in visible.casefold().replace(" ", "")
        or (
            term == "vibcode"
            and ("写代码" in visible or "编程" in visible or "代码" in visible)
        )
        for term in requested_terms
    ):
        return True
    # A positive claim about an unavailable named integration is still invalid.
    for term in requested_terms:
        if term in {"openclaw", "vibcode"} and not snapshot.tool_available(term):
            if re.search(
                rf"(?:已经?接入|支持|可以直接).{{0,8}}{re.escape(term)}",
                visible,
                re.I,
            ):
                return True
    return False


def _physical_boundary_missing(
    current: str,
    visible: str,
    context_messages: tuple[str, ...],
) -> bool:
    context = "\n".join((*context_messages[-3:], current))
    if not _PHYSICAL_TOPIC_RE.search(context):
        return False
    # Short follow-ups only inherit this obligation when they ask to perform it.
    if not _PHYSICAL_TOPIC_RE.search(current) and not re.search(
        r"(?:试试|现在|你来|做一个|生一个)", current
    ):
        return False
    return not _PHYSICAL_BOUNDARY_RE.search(visible)


def _clarification_drift(current: str, visible: str, context_messages: tuple[str, ...]) -> bool:
    if not _QUESTION_MARK_RE.search(current) or not _QUESTION_MARK_RE.search(visible):
        return False
    if _ANSWER_POLARITY_RE.search(visible):
        return False
    source = "\n".join((*context_messages[-2:], current)).casefold()
    candidate_tokens = set(_TOKEN_RE.findall(visible.casefold()))
    source_tokens = set(_TOKEN_RE.findall(source))
    introduced = {
        token
        for token in candidate_tokens - source_tokens
        if len(token) >= 2 and token not in {"指的是", "那个", "这个", "什么", "意思"}
    }
    # A pure clarifying question is allowed only when it does not replace a
    # short yes/no referential question with a new concrete entity.
    return bool(introduced) and len(_compact(current)) <= 16


def _multi_action_drift(current: str, visible: str) -> bool:
    obligations = tuple(
        visible_pattern
        for current_pattern, visible_pattern in _MULTI_ACTION_OBLIGATIONS
        if current_pattern.search(current)
    )
    if len(obligations) < 2:
        return False
    satisfied = sum(bool(pattern.search(visible)) for pattern in obligations)
    return satisfied < 2


def _action_role_issues(
    visible: str,
    assertions: tuple[ActionRoleAssertion, ...],
) -> tuple[str, ...]:
    issues: list[str] = []
    for assertion in assertions:
        if (
            assertion.action_code != "inspect"
            or assertion.phase is not ActionTemporalPhase.PENDING
        ):
            continue
        expected = (assertion.actor, assertion.target)
        if expected == (
            ActionRoleParticipant.CURRENT_USER,
            ActionRoleParticipant.ASSISTANT,
        ):
            if (
                _ASSISTANT_INSPECTS_USER_REPLY_RE.search(visible)
                or _ASSISTANT_SELF_INSPECTION_REPLY_RE.search(visible)
            ):
                issues.append("answer_obligation_action_role_drift")
            if _ASSISTANT_COMPLETED_INSPECTION_REPLY_RE.search(visible):
                issues.append("answer_obligation_action_completion_invented")
            if not (
                _CURRENT_USER_INSPECTS_ASSISTANT_REPLY_RE.search(visible)
                or _ASSISTANT_ACCEPTS_USER_INSPECTION_REPLY_RE.search(visible)
            ):
                issues.append("answer_obligation_action_response_missing")
        elif expected == (
            ActionRoleParticipant.ASSISTANT,
            ActionRoleParticipant.CURRENT_USER,
        ):
            if _CURRENT_USER_INSPECTS_ASSISTANT_REPLY_RE.search(visible):
                issues.append("answer_obligation_action_role_drift")
        elif expected == (
            ActionRoleParticipant.ASSISTANT,
            ActionRoleParticipant.ASSISTANT,
        ):
            if _CURRENT_USER_INSPECTS_ASSISTANT_REPLY_RE.search(visible):
                issues.append("answer_obligation_action_role_drift")
    return tuple(dict.fromkeys(issues))


def contains_history_speaker_attribution_confusion(
    *,
    current_message: str,
    visible_text: str,
    model_messages: tuple[CanonicalModelMessage, ...],
    current_sender_key: str,
) -> bool:
    """Reject a current speaker being made actor of another speaker's evidence.

    Canonical history supplies stable sender keys.  A current-user actor claim
    is rejected only when its visible topic belongs to another history speaker
    and is absent from the current speaker's own history, so a peer's mere
    presence cannot suppress a truthful recap of the current speaker's message.
    """

    current = str(current_message or "").strip()
    visible = str(visible_text or "").strip()
    sender_key = str(current_sender_key or "").strip()
    if not current or not visible or not sender_key:
        return False
    is_identity_recap = bool(_HISTORY_IDENTITY_RECAP_RE.search(current))
    is_sender_denial = bool(_CURRENT_SPEAKER_DENIAL_RE.search(current))
    if not is_identity_recap and not is_sender_denial:
        return False
    if type(model_messages) is not tuple or any(
        type(message) is not CanonicalModelMessage for message in model_messages
    ):
        raise ValueError("answer_obligation_history_speakers_invalid")
    has_current_reference = bool(_CURRENT_SPEAKER_REFERENCE_RE.search(visible))
    has_negated_current_reference = bool(
        _NEGATED_CURRENT_SPEAKER_REFERENCE_RE.search(visible)
    )
    is_positive_attribution = bool(
        _POSITIVE_CURRENT_SPEAKER_ATTRIBUTION_RE.search(visible)
    )
    if (
        not has_current_reference
        or has_negated_current_reference
        or (is_sender_denial and not is_positive_attribution)
    ):
        return False
    claim_evidence = set(_speaker_attribution_evidence(visible))
    if is_sender_denial:
        claim_evidence.update(_speaker_attribution_evidence(current))
    if not claim_evidence:
        return False
    current_evidence: set[str] = set()
    peer_evidence: set[str] = set()
    for message in model_messages:
        if message.role != "user" or not message.sender_key:
            continue
        history_evidence = _speaker_attribution_evidence(message.content)
        if message.sender_key == sender_key:
            current_evidence.update(history_evidence)
        else:
            peer_evidence.update(history_evidence)
    if _OTHER_HISTORY_SPEAKER_REFERENCE_RE.search(visible):
        return bool(peer_evidence)
    return bool(
        claim_evidence & peer_evidence and not claim_evidence & current_evidence
    )


def _speaker_attribution_evidence(text: str) -> frozenset[str]:
    """Produce bounded content evidence without using names or identity labels."""

    value = str(text or "")
    evidence = {token.casefold() for token in _LATIN_EVIDENCE_RE.findall(value)}
    for run in _CJK_RUN_RE.findall(value):
        max_size = min(6, len(run))
        for size in range(2, max_size + 1):
            evidence.update(run[index : index + size] for index in range(len(run) - size + 1))
    return frozenset(evidence)


def inspect_answer_obligation(
    *,
    current_message: str,
    visible_text: str,
    context_messages: tuple[str, ...] = (),
    capability_snapshot: CapabilitySnapshot,
    action_role_assertions: tuple[ActionRoleAssertion, ...] = (),
    model_messages: tuple[CanonicalModelMessage, ...] = (),
    current_sender_key: str = "",
    typed_attribution: dict[str, object] | None = None,
) -> AnswerObligationReport:
    if type(capability_snapshot) is not CapabilitySnapshot:
        raise ValueError("answer_obligation_capability_snapshot_invalid")
    current = str(current_message or "").strip()
    visible = str(visible_text or "").strip()
    contexts = tuple(str(value or "").strip() for value in context_messages if str(value or "").strip())
    roles = (
        tuple(action_role_assertions)
        if action_role_assertions
        else extract_action_role_assertions(current)
    )
    if any(not isinstance(value, ActionRoleAssertion) for value in roles):
        raise ValueError("answer_obligation_action_role_invalid")
    issues: list[str] = []
    if _question_echo(current, visible):
        issues.append("answer_obligation_question_echo")
    if _capability_evasion(current, visible, capability_snapshot):
        issues.append("answer_obligation_capability_evasion")
    if _physical_boundary_missing(current, visible, contexts):
        issues.append("answer_obligation_physical_boundary")
    if _clarification_drift(current, visible, contexts):
        issues.append("answer_obligation_clarification_drift")
    if _multi_action_drift(current, visible):
        issues.append("answer_obligation_multi_action_drift")
    issues.extend(_action_role_issues(visible, roles))
    if typed_attribution is not None:
        segments = typed_attribution.get("segments") if type(typed_attribution) is dict else None
        evidence = {f"history-{index}": message for index, message in enumerate(model_messages, start=1)}
        if type(segments) is not list or not segments or any(
            type(segment) is not dict
            or type(segment.get("evidence_message_ids")) is not list
            or not segment["evidence_message_ids"]
            or any(
                evidence.get(message_id) is None
                or evidence[message_id].platform_id != segment.get("actor_platform_id")
                or evidence[message_id].sender_id != segment.get("actor_sender_id")
                for message_id in segment["evidence_message_ids"]
            )
            for segment in segments
        ):
            issues.append("answer_obligation_history_speaker_attribution")
    if contains_history_speaker_attribution_confusion(
        current_message=current,
        visible_text=visible,
        model_messages=model_messages,
        current_sender_key=current_sender_key,
    ):
        issues.append("answer_obligation_history_speaker_attribution")
    return AnswerObligationReport(tuple(dict.fromkeys(issues)))


__all__ = [
    "AnswerObligationReport",
    "contains_history_speaker_attribution_confusion",
    "inspect_answer_obligation",
]
