from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .context_assembler import ReplyTarget
from .conversation_ledger import ledger_content_digest
from .identity import PrincipalContext


class AffectTrigger(str, Enum):
    NEUTRAL = "neutral"
    PRAISE = "praise"
    PLAYFUL_PROVOCATION = "playful_provocation"
    BEING_SEEN_THROUGH = "being_seen_through"
    CORRECTION_OR_MISTAKE = "correction_or_mistake"
    CONCERN_FOR_AGENT = "concern_for_agent"
    USER_NEEDS_CARE = "user_needs_care"
    APOLOGY = "apology"
    GRATITUDE = "gratitude"
    DISAGREEMENT = "disagreement"
    INFORMATION_REQUEST = "information_request"
    ACTION_REQUEST = "action_request"


class RelationshipDistance(str, Enum):
    PRIMARY_BOND = "primary_bond"
    PEER = "peer"
    UNVERIFIED = "unverified"


class SurfaceEmotion(str, Enum):
    CALM = "calm"
    PLEASED = "pleased"
    EMBARRASSED = "embarrassed"
    STARTLED = "startled"
    CONCERNED = "concerned"
    REMORSEFUL = "remorseful"
    WARM = "warm"
    DEFENSIVE = "defensive"
    CURIOUS = "curious"
    SERIOUS = "serious"


class HiddenConcern(str, Enum):
    NONE = "none"
    MAINTAIN_COMPETENCE = "maintain_competence"
    PROTECT_RELATIONSHIP = "protect_relationship"
    RECIPIENT_WELLBEING = "recipient_wellbeing"
    CORRECT_MISUNDERSTANDING = "correct_misunderstanding"
    RESTORE_TRUST = "restore_trust"
    RESPECT_BOUNDARY = "respect_boundary"


class BehavioralTendency(str, Enum):
    ANSWER_CURRENT_REQUEST = "answer_current_request"
    ACKNOWLEDGE = "acknowledge"
    SHOW_REACTION = "show_reaction"
    SOFTEN = "soften"
    CLARIFY = "clarify"
    REPAIR = "repair"
    REASSURE = "reassure"
    PLAYFUL_DEFLECT = "playful_deflect"
    OFFER_HELP = "offer_help"
    MAINTAIN_BOUNDARY = "maintain_boundary"
    REQUEST_REPLAN = "request_replan"


class TopicReturn(str, Enum):
    ANSWER_TARGET = "answer_target"
    ACKNOWLEDGE_THEN_ANSWER = "acknowledge_then_answer"
    REACT_THEN_ANSWER = "react_then_answer"
    CARE_THEN_FOLLOW_UP = "care_then_follow_up"
    REPAIR_THEN_CONTINUE = "repair_then_continue"
    REPLAN_CURRENT_TARGET = "replan_current_target"


@dataclass(frozen=True, slots=True)
class AffectAppraisal:
    trigger: AffectTrigger
    focus_sender_key: str
    relationship_distance: RelationshipDistance
    surface_emotion: SurfaceEmotion
    secondary_emotion: SurfaceEmotion | None
    hidden_concern: HiddenConcern
    behavioral_tendency: tuple[BehavioralTendency, ...]
    topic_return: TopicReturn
    target_message_id: str
    target_content_digest: str
    confidence: float
    degradation_reasons: tuple[str, ...]

    @property
    def is_actionable(self) -> bool:
        return not self.degradation_reasons and self.confidence >= 0.5


PRAISE_RE = re.compile(
    r"(?:真|很|太|好)?(?:可爱|厉害|聪明|优秀|靠谱|棒|帅|漂亮)|"
    r"(?:做得|干得)(?:真|很)?好|(?:great|amazing|cute|smart)\b",
    re.IGNORECASE,
)
SEEN_THROUGH_RE = re.compile(
    r"(?:明明|其实).{0,12}(?:在意|喜欢|开心|担心)|"
    r"(?:被我|让我)(?:说中|看穿)|(?:嘴硬|口是心非)",
    re.IGNORECASE,
)
PLAYFUL_PROVOCATION_RE = re.compile(
    r"(?:逗你|捉弄|欺负一下|急了|害羞了|脸红了|不敢吧|就这|"
    r"笨蛋|小笨蛋|傻瓜|呆子)|"
    r"(?:tease|blush|got you)\b",
    re.IGNORECASE,
)
CORRECTION_RE = re.compile(
    r"(?:你|刚才).{0,8}(?:说错|弄错|认错|答错|搞反|搞混)|"
    r"不是这个人|前后(?:不搭|矛盾)|答非所问|wrong person",
    re.IGNORECASE,
)
CONCERN_FOR_AGENT_RE = re.compile(
    r"(?:你|机器人).{0,8}(?:累不累|还好吗|没事吧|要不要休息|辛苦了)|"
    r"(?:担心|心疼)你",
    re.IGNORECASE,
)
USER_NEEDS_CARE_RE = re.compile(
    r"(?:我|本人).{0,10}(?:难受|伤心|害怕|焦虑|累死|撑不住|睡不着|生病)|"
    r"(?:安慰|陪陪)我",
    re.IGNORECASE,
)
APOLOGY_RE = re.compile(
    r"(?:对不起|抱歉|不好意思|是我不对|\bsorry\b)",
    re.IGNORECASE,
)
GRATITUDE_RE = re.compile(r"(?:谢谢|多谢|辛苦你了|thank(?:s| you))", re.IGNORECASE)
DISAGREEMENT_RE = re.compile(
    r"(?:我不同意|不对吧|不是这样的|你说得不对|胡说|瞎说)",
    re.IGNORECASE,
)
ACTION_REQUEST_RE = re.compile(
    r"(?:帮我|替我|给我|请你|能不能|可以帮忙|麻烦你)(?:做|写|查|找|看|分析|处理|运行|打开)?",
    re.IGNORECASE,
)
INFORMATION_REQUEST_RE = re.compile(
    r"[？?]|(?:什么|怎么|如何|为什么|为啥|哪里|多少|谁|是不是|有没有|解释|介绍|讲讲|查一下|搜一下)",
    re.IGNORECASE,
)


def trusted_relationship_distance(
    principal: PrincipalContext | None,
    conversation_mode: str,
) -> RelationshipDistance:
    """Map code-resolved principal state to expression distance, failing closed."""

    mode = str(conversation_mode or "direct_reply").strip().lower()
    if mode not in {"direct_reply", "group_join"}:
        return RelationshipDistance.UNVERIFIED
    if principal is None or not principal.sender_id or not principal.sender_key:
        return RelationshipDistance.UNVERIFIED
    if mode == "group_join" and (
        principal is not None
        and principal.sender_id
        and principal.sender_key
        and principal.relationship_role in {"owner", "group_peer"}
        and principal.verification_source
        in {
            "astrbot_event_sender_id:configured_owner_id",
            "astrbot_event_sender_id:owner_allowlist_miss",
        }
    ):
        # Opportunistic group participation is peer-facing even when the
        # current speaker is the owner; it must not unlock owner-only intimacy.
        return RelationshipDistance.PEER
    if (
        principal.is_owner
        and principal.relationship_role == "owner"
        and principal.verification_source
        == "astrbot_event_sender_id:configured_owner_id"
    ):
        return RelationshipDistance.PRIMARY_BOND
    if (
        not principal.is_owner
        and principal.relationship_role in {"group_peer", "private_peer"}
        and principal.verification_source
        == "astrbot_event_sender_id:owner_allowlist_miss"
    ):
        return RelationshipDistance.PEER
    return RelationshipDistance.UNVERIFIED


def _classify_trigger(message: str, mode: str) -> AffectTrigger:
    checks = (
        (CORRECTION_RE, AffectTrigger.CORRECTION_OR_MISTAKE),
        (USER_NEEDS_CARE_RE, AffectTrigger.USER_NEEDS_CARE),
        (CONCERN_FOR_AGENT_RE, AffectTrigger.CONCERN_FOR_AGENT),
        (APOLOGY_RE, AffectTrigger.APOLOGY),
        (GRATITUDE_RE, AffectTrigger.GRATITUDE),
        (SEEN_THROUGH_RE, AffectTrigger.BEING_SEEN_THROUGH),
        (PLAYFUL_PROVOCATION_RE, AffectTrigger.PLAYFUL_PROVOCATION),
        (PRAISE_RE, AffectTrigger.PRAISE),
        (DISAGREEMENT_RE, AffectTrigger.DISAGREEMENT),
        (ACTION_REQUEST_RE, AffectTrigger.ACTION_REQUEST),
        (INFORMATION_REQUEST_RE, AffectTrigger.INFORMATION_REQUEST),
    )
    for pattern, trigger in checks:
        if pattern.search(message):
            return trigger
    return AffectTrigger.NEUTRAL


def _trajectory(
    trigger: AffectTrigger,
) -> tuple[
    SurfaceEmotion,
    SurfaceEmotion | None,
    HiddenConcern,
    tuple[BehavioralTendency, ...],
    TopicReturn,
]:
    mapping = {
        AffectTrigger.PRAISE: (
            SurfaceEmotion.PLEASED,
            SurfaceEmotion.EMBARRASSED,
            HiddenConcern.PROTECT_RELATIONSHIP,
            (
                BehavioralTendency.SHOW_REACTION,
                BehavioralTendency.ACKNOWLEDGE,
                BehavioralTendency.SOFTEN,
            ),
            TopicReturn.REACT_THEN_ANSWER,
        ),
        AffectTrigger.PLAYFUL_PROVOCATION: (
            SurfaceEmotion.STARTLED,
            SurfaceEmotion.EMBARRASSED,
            HiddenConcern.RESPECT_BOUNDARY,
            (
                BehavioralTendency.SHOW_REACTION,
                BehavioralTendency.PLAYFUL_DEFLECT,
                BehavioralTendency.MAINTAIN_BOUNDARY,
            ),
            TopicReturn.REACT_THEN_ANSWER,
        ),
        AffectTrigger.BEING_SEEN_THROUGH: (
            SurfaceEmotion.EMBARRASSED,
            SurfaceEmotion.PLEASED,
            HiddenConcern.PROTECT_RELATIONSHIP,
            (
                BehavioralTendency.SHOW_REACTION,
                BehavioralTendency.PLAYFUL_DEFLECT,
                BehavioralTendency.SOFTEN,
            ),
            TopicReturn.REACT_THEN_ANSWER,
        ),
        AffectTrigger.CORRECTION_OR_MISTAKE: (
            SurfaceEmotion.REMORSEFUL,
            SurfaceEmotion.DEFENSIVE,
            HiddenConcern.RESTORE_TRUST,
            (
                BehavioralTendency.ACKNOWLEDGE,
                BehavioralTendency.REPAIR,
                BehavioralTendency.CLARIFY,
            ),
            TopicReturn.REPAIR_THEN_CONTINUE,
        ),
        AffectTrigger.CONCERN_FOR_AGENT: (
            SurfaceEmotion.WARM,
            SurfaceEmotion.EMBARRASSED,
            HiddenConcern.PROTECT_RELATIONSHIP,
            (BehavioralTendency.ACKNOWLEDGE, BehavioralTendency.REASSURE),
            TopicReturn.ACKNOWLEDGE_THEN_ANSWER,
        ),
        AffectTrigger.USER_NEEDS_CARE: (
            SurfaceEmotion.CONCERNED,
            SurfaceEmotion.WARM,
            HiddenConcern.RECIPIENT_WELLBEING,
            (
                BehavioralTendency.SHOW_REACTION,
                BehavioralTendency.REASSURE,
                BehavioralTendency.OFFER_HELP,
            ),
            TopicReturn.CARE_THEN_FOLLOW_UP,
        ),
        AffectTrigger.APOLOGY: (
            SurfaceEmotion.WARM,
            None,
            HiddenConcern.PROTECT_RELATIONSHIP,
            (BehavioralTendency.ACKNOWLEDGE, BehavioralTendency.SOFTEN),
            TopicReturn.ACKNOWLEDGE_THEN_ANSWER,
        ),
        AffectTrigger.GRATITUDE: (
            SurfaceEmotion.PLEASED,
            SurfaceEmotion.WARM,
            HiddenConcern.PROTECT_RELATIONSHIP,
            (BehavioralTendency.ACKNOWLEDGE, BehavioralTendency.SOFTEN),
            TopicReturn.ACKNOWLEDGE_THEN_ANSWER,
        ),
        AffectTrigger.DISAGREEMENT: (
            SurfaceEmotion.SERIOUS,
            None,
            HiddenConcern.CORRECT_MISUNDERSTANDING,
            (BehavioralTendency.CLARIFY, BehavioralTendency.SOFTEN),
            TopicReturn.ANSWER_TARGET,
        ),
        AffectTrigger.ACTION_REQUEST: (
            SurfaceEmotion.SERIOUS,
            None,
            HiddenConcern.MAINTAIN_COMPETENCE,
            (BehavioralTendency.ANSWER_CURRENT_REQUEST,),
            TopicReturn.ANSWER_TARGET,
        ),
        AffectTrigger.INFORMATION_REQUEST: (
            SurfaceEmotion.CURIOUS,
            None,
            HiddenConcern.MAINTAIN_COMPETENCE,
            (BehavioralTendency.ANSWER_CURRENT_REQUEST,),
            TopicReturn.ANSWER_TARGET,
        ),
        AffectTrigger.NEUTRAL: (
            SurfaceEmotion.CALM,
            None,
            HiddenConcern.NONE,
            (BehavioralTendency.ANSWER_CURRENT_REQUEST,),
            TopicReturn.ANSWER_TARGET,
        ),
    }
    return mapping[trigger]


def appraise_affect(
    *,
    principal: PrincipalContext | None,
    reply_target: ReplyTarget | None,
    current_message: str,
    conversation_mode: str = "direct_reply",
) -> AffectAppraisal:
    mode = str(conversation_mode or "direct_reply").strip().lower()
    message = str(current_message or "").strip()
    degradation_reasons: list[str] = []
    target_message_id = reply_target.message_id if reply_target is not None else ""
    target_digest = reply_target.content_digest if reply_target is not None else ""
    focus_sender_key = ""

    if mode in {"direct_reply", "group_join"}:
        if reply_target is None:
            degradation_reasons.append("missing_reply_target")
        else:
            degradation_reasons.extend(reply_target.degradation_reasons)
            if ledger_content_digest(message) != reply_target.content_digest:
                degradation_reasons.append("target_content_mismatch")
            if principal is None or not principal.sender_key:
                degradation_reasons.append("principal_unavailable")
            elif principal.sender_key != reply_target.sender_key:
                degradation_reasons.append("principal_target_mismatch")
            else:
                focus_sender_key = principal.sender_key
    else:
        degradation_reasons.append("unsupported_conversation_mode")

    trigger = _classify_trigger(message, mode)
    surface, secondary, hidden, behavior, topic_return = _trajectory(trigger)
    if degradation_reasons:
        behavior = (BehavioralTendency.REQUEST_REPLAN,)
        topic_return = TopicReturn.REPLAN_CURRENT_TARGET

    confidence = 0.25 if degradation_reasons else (0.75 if trigger != AffectTrigger.NEUTRAL else 0.6)
    return AffectAppraisal(
        trigger=trigger,
        focus_sender_key=focus_sender_key,
        relationship_distance=trusted_relationship_distance(principal, mode),
        surface_emotion=surface,
        secondary_emotion=secondary,
        hidden_concern=hidden,
        behavioral_tendency=behavior,
        topic_return=topic_return,
        target_message_id=target_message_id,
        target_content_digest=target_digest,
        confidence=confidence,
        degradation_reasons=tuple(dict.fromkeys(degradation_reasons)),
    )
