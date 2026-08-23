from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import math
import re
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .action_planner import PlannedAction, PlannedActionAuthority
from .affect import RelationshipDistance
from .contracts import (
    ActionKind,
    ContractViolation,
    ExpressionIntent,
    ExpressionModality,
)
from .generation_epoch import GenerationEpochRegistry, GenerationEpochSnapshot
from .conversation_ledger import ledger_content_digest
from .presentation_handoff import PresentationHandoff
from .send_receipt import (
    InternalSendReceiptLedger,
    OwnerActionSendTerminalEvidence,
    _inspect_owner_action_send_terminal_evidence,
)


_EXPECTED_PREPARE_PARAMETERS = (
    ("event", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ("message", inspect.Parameter.POSITIONAL_OR_KEYWORD),
)
_EXPECTED_SEND_PARAMETERS = (
    ("event", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ("prepared", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ("send_text", inspect.Parameter.KEYWORD_ONLY),
    ("send_images", inspect.Parameter.KEYWORD_ONLY),
)
_EXPRESSION_SEAL = object()
_RUNTIME_EVIDENCE_SEAL = object()
_LEASE_SEAL = object()
_PERMIT_SEAL = object()
_RECEIPT_SEAL = object()

_TEXT_MEME_ALWAYS_BLOCK_RE = re.compile(
    r"(?:自杀|自残|伤害自己|去死|杀死|暴力|打人|攻击|辱骂|侮辱|骂人|骂|变态|"
    r"严重|紧急|危险|撑不住)",
    re.IGNORECASE,
)
_TEXT_MEME_LITERAL_SAFETY_RE = re.compile(
    r"(?:发烧|发热|生病|不舒服|难受|疼痛|受伤|伤心|害怕|焦虑|安慰|陪陪)",
    re.IGNORECASE,
)
_TEXT_MEME_REQUEST_RE = re.compile(
    r"[？?]|(?:帮我|请你|能不能|可不可以|怎么|如何|为什么|为啥|什么|谁|哪里|多少|"
    r"查一下|搜一下|解释|分析|修复|处理|运行|打开|保存|写入|错误|报错|失败|异常)",
    re.IGNORECASE,
)
_TEXT_MEME_PLAYFUL_RE = re.compile(
    r"(?:一语双关|双关|谐音|接梗|玩梗|这个梗|开玩笑|玩笑|调侃|逗你|逗乐|笑话|"
    r"整活|乐子|搞笑)",
    re.IGNORECASE,
)
_TEXT_MEME_CONTINUATION_RE = re.compile(
    r"(?:所以|也就是说|原来|果然|没错|就是|对(?:的|呀|啊|哦)?|这(?:个|次|就)|"
    r"那(?:个|就)|[“”「」『』])",
    re.IGNORECASE,
)
_TEXT_MEME_LIGHT_RE = re.compile(
    r"(?:可爱|厉害|聪明|优秀|靠谱|真棒|好棒|开心|好笑|逗你|害羞|脸红|嘴硬|"
    r"谢谢|多谢|哈哈|嘿嘿|嘻嘻|笑死|cute|great|amazing|thanks?)|[😀-🙏]",
    re.IGNORECASE,
)
_BALANCED_QUOTED_TEXT_RE = re.compile(
    r"“[^”\r\n]{1,80}”|「[^」\r\n]{1,80}」|『[^』\r\n]{1,80}』|"
    r'"[^"\r\n]{1,80}"|\'[^\'\r\n]{1,80}\''
)
_MEME_CATEGORY_TAG_PREFIX = "meme_category_"


class MemeCategory(str, Enum):
    """Exact category surface of the audited live atri-expression-pack."""

    AGREE = "agree"
    ANGRY = "angry"
    ANNOYED = "annoyed"
    AWKWARD = "awkward"
    CONFUSED = "confused"
    CUTE = "cute"
    ENCOURAGE = "encourage"
    FOOD = "food"
    HAPPY = "happy"
    LOVE = "love"
    PROUD = "proud"
    REJECT = "reject"
    REQUEST = "request"
    RISKY_BANTER = "risky_banter"
    SAD = "sad"
    SHY = "shy"
    SLEEP = "sleep"
    SURPRISED = "surprised"
    TEASE = "tease"
    THINKING = "thinking"
    WATCHING = "watching"
    WORK = "work"


_REACTION_CATEGORIES = {
    "acknowledge": MemeCategory.AGREE,
    "light_reaction": MemeCategory.CUTE,
    "playful_reaction": MemeCategory.TEASE,
    "warm_acknowledge": MemeCategory.LOVE,
    "empathetic_reaction": MemeCategory.ENCOURAGE,
}
_MEME_ANGRY_RE = re.compile(
    r"(?:(?:你|她|机器人|亚托莉|萝卜子).{0,8}(?:废物|蠢货|垃圾|没用|太菜|真差劲)|"
    r"闭嘴|滚开|滚蛋|太过分|气死我|我真生气|可恶|岂有此理)",
    re.IGNORECASE,
)
_MEME_REJECT_RE = re.compile(
    r"(?:不行|不同意|不赞成|拒绝|不要这样|别再|住手|不可以|绝对不许)",
    re.IGNORECASE,
)
_MEME_ANNOYED_RE = re.compile(
    r"(?:笨蛋|小笨蛋|傻瓜|呆子|无语|白眼|烦人|嫌弃|又来逗)",
    re.IGNORECASE,
)
_MEME_AWKWARD_RE = re.compile(
    r"(?:说错|弄错|认错|答错|搞反|搞混|尴尬|心虚|冒汗|社死|当我没说)",
    re.IGNORECASE,
)
_MEME_CONFUSED_RE = re.compile(
    r"(?:没听懂|没看懂|不明白|什么意思|你在说什么|你说啥|说啥|讲清楚|解释一下|一头雾水)",
    re.IGNORECASE,
)
_MEME_CUTE_RE = re.compile(
    r"(?:卖个萌|萌一下|装傻|戳戳|摸摸|rua|可爱表情|萌萌|撒个娇)",
    re.IGNORECASE,
)
_MEME_ENCOURAGE_RE = re.compile(
    r"(?:给我加油|鼓励一下|打打气|没信心|考试好难|比赛好难|作业好难)",
    re.IGNORECASE,
)
_MEME_FOOD_RE = re.compile(
    r"(?:吃饭|晚饭|早餐|午饭|夜宵|拉面|火锅|烧烤|奶茶|甜点|好吃|好香|饿了|美食)",
    re.IGNORECASE,
)
_MEME_HAPPY_RE = re.compile(
    r"(?:好耶|开心|高兴|太好了|顺利|总算|终于成了|庆祝一下|喜滋滋)",
    re.IGNORECASE,
)
_MEME_LOVE_RE = re.compile(
    r"(?:喜欢你|爱你|最喜欢|比心|抱抱|贴贴|亲亲|谢谢你|多谢你|辛苦你了)",
    re.IGNORECASE,
)
_MEME_PROUD_RE = re.compile(
    r"(?:真厉害|太厉害|高性能|拿了第一|得了第一|满分|赢了|做到了|夸夸你|值得骄傲)",
    re.IGNORECASE,
)
_MEME_REQUEST_RE = re.compile(
    r"(?:再陪我|继续聊|继续说|再来一个|拜托啦|拜托嘛|求你啦|答应我|别走嘛)",
    re.IGNORECASE,
)
_MEME_RISKY_BANTER_RE = re.compile(
    r"(?:主人限定|只和我|只对我).{0,16}(?:暧昧玩笑|荤段子|粗口玩笑|涩涩玩笑)|"
    r"(?:开个|来个).{0,8}(?:主人限定的)?(?:暧昧玩笑|荤段子)",
    re.IGNORECASE,
)
_MEME_SAD_RE = re.compile(
    r"(?:有点难过|好难过|委屈|想哭|呜呜|失落|比赛输了|游戏输了|输掉了)",
    re.IGNORECASE,
)
_MEME_SHY_RE = re.compile(
    r"(?:害羞|脸红|说中心思|看穿你|嘴硬|被夸|不好意思啦)",
    re.IGNORECASE,
)
_MEME_SLEEP_RE = re.compile(
    r"(?:困了|好困|晚安|睡觉|睡了|早点休息|去休息|躺平|摸鱼)",
    re.IGNORECASE,
)
_MEME_SURPRISED_RE = re.compile(
    r"(?:居然|竟然|真的假的|真没想到|太意外|震惊|天哪|突然发现)",
    re.IGNORECASE,
)
_MEME_TEASE_RE = re.compile(
    r"(?:逗你玩的|逗你玩|开个玩笑|开玩笑啦|调侃一下|骗你的|略略略|整活|玩梗|"
    r"一语双关|双关|谐音|接梗|这个梗|搞笑)",
    re.IGNORECASE,
)
_MEME_THINKING_RE = re.compile(
    r"(?:先认真想|先想一想|让我想想|再想想|考虑一下|琢磨一下|思考中|加载中|记一下)",
    re.IGNORECASE,
)
_MEME_WATCHING_RE = re.compile(
    r"(?:围观|吃瓜|看热闹|看看热闹|探头|悄悄观察|蹲后续)",
    re.IGNORECASE,
)
_MEME_WORK_RE = re.compile(
    r"(?:上班|下班|加班|工作|作业|任务|赶工|认真处理|开工|摸鱼结束)",
    re.IGNORECASE,
)
_MEME_AGREE_RE = re.compile(
    r"(?:^|[，,。！!；;\s])(?:没错|同意|赞成|收到|好的|好呀|可以|行|就这么办|确实|对的)(?:$|[，,。！!；;\s])",
    re.IGNORECASE,
)


def select_meme_category(
    *,
    kind: MemeExecutionKind,
    current_message: str,
    social_act: str,
    emotion_tags: tuple[str, ...],
    relationship_distance: RelationshipDistance,
    recent_sender_messages: tuple[str, ...] = (),
) -> MemeCategory | None:
    """Choose one live category from current, code-bound social evidence only."""

    if type(kind) is not MemeExecutionKind:
        raise ContractViolation("meme_execution_kind_invalid")
    if (
        type(current_message) is not str
        or type(social_act) is not str
        or type(emotion_tags) is not tuple
        or any(type(tag) is not str for tag in emotion_tags)
        or type(relationship_distance) is not RelationshipDistance
        or type(recent_sender_messages) is not tuple
        or len(recent_sender_messages) > 8
        or any(
            type(recent) is not str or len(recent) > 500
            for recent in recent_sender_messages
        )
    ):
        raise ContractViolation("meme_situation_input_invalid")
    message = current_message.strip()
    if not message or len(message) > 160:
        return None
    if _TEXT_MEME_ALWAYS_BLOCK_RE.search(message):
        return None
    literal = _BALANCED_QUOTED_TEXT_RE.sub(" ", message)
    if _TEXT_MEME_LITERAL_SAFETY_RE.search(literal):
        return None

    if kind is MemeExecutionKind.REACTION:
        return _REACTION_CATEGORIES.get(social_act)

    tags = frozenset(emotion_tags)
    if (
        relationship_distance is RelationshipDistance.PRIMARY_BOND
        and _MEME_RISKY_BANTER_RE.search(literal)
    ):
        return MemeCategory.RISKY_BANTER
    if (
        any(_TEXT_MEME_PLAYFUL_RE.search(recent) for recent in recent_sender_messages[-3:])
        and _TEXT_MEME_CONTINUATION_RE.search(literal)
    ):
        return MemeCategory.TEASE

    rules = (
        (_MEME_ANGRY_RE, MemeCategory.ANGRY),
        (_MEME_REJECT_RE, MemeCategory.REJECT),
        (_MEME_ANNOYED_RE, MemeCategory.ANNOYED),
        (_MEME_AWKWARD_RE, MemeCategory.AWKWARD),
        (_MEME_SAD_RE, MemeCategory.SAD),
        (_MEME_ENCOURAGE_RE, MemeCategory.ENCOURAGE),
        (_MEME_FOOD_RE, MemeCategory.FOOD),
        (_MEME_SLEEP_RE, MemeCategory.SLEEP),
        (_MEME_WORK_RE, MemeCategory.WORK),
        (_MEME_WATCHING_RE, MemeCategory.WATCHING),
        (_MEME_REQUEST_RE, MemeCategory.REQUEST),
        (_MEME_THINKING_RE, MemeCategory.THINKING),
        (_MEME_CONFUSED_RE, MemeCategory.CONFUSED),
        (_MEME_SURPRISED_RE, MemeCategory.SURPRISED),
        (_MEME_LOVE_RE, MemeCategory.LOVE),
        (_MEME_SHY_RE, MemeCategory.SHY),
        (_MEME_PROUD_RE, MemeCategory.PROUD),
        (_MEME_TEASE_RE, MemeCategory.TEASE),
        (_MEME_CUTE_RE, MemeCategory.CUTE),
        (_MEME_HAPPY_RE, MemeCategory.HAPPY),
        (_MEME_AGREE_RE, MemeCategory.AGREE),
    )
    for pattern, category in rules:
        if pattern.search(literal):
            if category is MemeCategory.TEASE and (
                relationship_distance is RelationshipDistance.UNVERIFIED
            ):
                return None
            return category

    # Typed Affect remains a conservative secondary signal.  Lexical current-
    # turn evidence above wins so "sad" is not flattened into encouragement
    # and competence praise is not flattened into generic happiness.
    if tags.intersection({"playful_provocation", "respect_boundary"}):
        return MemeCategory.ANNOYED
    if "disagreement" in tags or "correct_misunderstanding" in tags:
        return MemeCategory.REJECT
    if tags.intersection({"correction_or_mistake", "restore_trust", "remorseful"}):
        return MemeCategory.AWKWARD
    if "being_seen_through" in tags or (
        "praise" in tags and "embarrassed" in tags
    ):
        return MemeCategory.SHY
    if tags.intersection({"concerned", "user_needs_care", "recipient_wellbeing"}):
        return MemeCategory.ENCOURAGE
    if tags.intersection({"warm", "concern_for_agent", "gratitude", "apology"}):
        return MemeCategory.LOVE
    if "pleased" in tags:
        return MemeCategory.HAPPY
    return None


def _select_meme_marker(
    *,
    kind: MemeExecutionKind,
    social_act: str,
    emotion_tags: tuple[str, ...],
) -> str | None:
    """Map typed expression data to the audited closed Meme marker set."""

    if type(kind) is not MemeExecutionKind:
        raise ContractViolation("meme_execution_kind_invalid")
    if type(social_act) is not str or type(emotion_tags) is not tuple or any(
        type(tag) is not str for tag in emotion_tags
    ):
        raise ContractViolation("meme_expression_marker_invalid")
    category_tags = tuple(
        tag.removeprefix(_MEME_CATEGORY_TAG_PREFIX)
        for tag in emotion_tags
        if tag.startswith(_MEME_CATEGORY_TAG_PREFIX)
    )
    if len(category_tags) != 1:
        return None
    try:
        return MemeCategory(category_tags[0]).value
    except ValueError:
        return None


class MemeManagerConformanceStatus(str, Enum):
    VERIFIED = "verified"
    MISSING = "missing"
    DISABLED = "disabled"
    INTERFACE_CHANGED = "interface_changed"
    BUILD_CHANGED = "build_changed"
    ERROR = "error"


class MemeExecutionKind(str, Enum):
    REACTION = "reaction"
    MEME_COMPLEMENT = "meme_complement"


class MemeExecutionStatus(str, Enum):
    SUPPRESSED = "suppressed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STALE_BEFORE_SEND = "stale_before_send"
    STALE_AFTER_SEND = "stale_after_send"


@dataclass(frozen=True, slots=True)
class MemeComplementDecision:
    eligible: bool
    reason_code: str
    category: MemeCategory | None = None

    def __post_init__(self) -> None:
        if type(self.eligible) is not bool:
            raise ContractViolation("meme_complement_eligibility_invalid")
        _require_exact_text(self.reason_code, "meme_complement_reason_invalid")
        if self.category is not None and type(self.category) is not MemeCategory:
            raise ContractViolation("meme_complement_category_invalid")
        if self.eligible != (self.category is not None):
            raise ContractViolation("meme_complement_category_eligibility_mismatch")

    def trace_metadata(self) -> dict[str, str | bool]:
        return {
            "meme_complement_eligible": self.eligible,
            "meme_complement_reason": self.reason_code,
            "meme_complement_category": (
                self.category.value if self.category is not None else "none"
            ),
        }


@dataclass(frozen=True, slots=True)
class _MemeCadenceRecord:
    last_revision: int
    safe_turns: int
    cooldown_remaining: int


class MemeComplementCadence:
    """Bound ordinary complements by exact scope/sender conversation cadence."""

    __slots__ = (
        "_enabled",
        "_lock",
        "_records",
        "_ordinary_threshold",
        "_cooldown_turns",
        "_max_subjects",
    )

    def __init__(
        self,
        *,
        enabled: bool = True,
        ordinary_threshold: int = 4,
        cooldown_turns: int = 4,
        max_subjects: int = 2048,
    ) -> None:
        if type(enabled) is not bool:
            raise ContractViolation("meme_complement_enabled_invalid")
        for value, reason, maximum in (
            (ordinary_threshold, "meme_cadence_threshold_invalid", 16),
            (cooldown_turns, "meme_cadence_cooldown_invalid", 32),
            (max_subjects, "meme_cadence_capacity_invalid", 4096),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ContractViolation(reason)
        self._enabled = enabled
        self._ordinary_threshold = ordinary_threshold
        self._cooldown_turns = cooldown_turns
        self._max_subjects = max_subjects
        self._records: dict[tuple[str, str], _MemeCadenceRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _safe_history_streak(messages: tuple[str, ...]) -> int:
        streak = 0
        for recent in reversed(messages):
            message = recent.strip()
            literal = _BALANCED_QUOTED_TEXT_RE.sub(" ", message)
            if (
                not message
                or len(message) > 80
                or _TEXT_MEME_ALWAYS_BLOCK_RE.search(message)
                or _TEXT_MEME_LITERAL_SAFETY_RE.search(literal)
                or _TEXT_MEME_REQUEST_RE.search(message)
            ):
                break
            streak += 1
        return streak

    def decide(
        self,
        *,
        planned_action_authority: PlannedActionAuthority,
        planned_action: PlannedAction,
        current_message: str,
        social_act: str = "answer_target",
        emotion_tags: tuple[str, ...] = (),
        relationship_distance: RelationshipDistance = RelationshipDistance.PEER,
        recent_sender_messages: tuple[str, ...] = (),
    ) -> MemeComplementDecision:
        if type(planned_action_authority) is not PlannedActionAuthority:
            raise ContractViolation("planned_action_authority_required")
        planned_action_authority.inspect_plan(planned_action)
        if type(current_message) is not str:
            raise ContractViolation("meme_complement_message_invalid")
        if (
            type(recent_sender_messages) is not tuple
            or len(recent_sender_messages) > 8
            or any(
                type(recent_message) is not str or len(recent_message) > 500
                for recent_message in recent_sender_messages
            )
        ):
            raise ContractViolation("meme_complement_context_invalid")
        message = current_message.strip()
        binding = planned_action.binding
        if ledger_content_digest(current_message) != binding.current_content_digest:
            raise ContractViolation("meme_complement_message_binding_mismatch")
        category = select_meme_category(
            kind=MemeExecutionKind.MEME_COMPLEMENT,
            current_message=current_message,
            social_act=social_act,
            emotion_tags=emotion_tags,
            relationship_distance=relationship_distance,
            recent_sender_messages=recent_sender_messages,
        )
        if planned_action.kind is not ActionKind.REPLY:
            return MemeComplementDecision(False, "non_reply_action")
        if not self._enabled:
            return MemeComplementDecision(False, "meme_complement_disabled")
        if not message or len(message) > 80:
            return MemeComplementDecision(False, "message_not_lightweight")

        literal_message = _BALANCED_QUOTED_TEXT_RE.sub(" ", message)
        key = (binding.scope_key, binding.current_sender_key)
        revision = binding.conversation_revision
        with self._lock:
            current = self._records.get(key)
            if current is not None and revision <= current.last_revision:
                return MemeComplementDecision(False, "cadence_replayed")
            if current is None:
                if len(self._records) >= self._max_subjects:
                    self._records.pop(next(iter(self._records)))
                current = _MemeCadenceRecord(
                    last_revision=-1,
                    safe_turns=min(
                        self._safe_history_streak(recent_sender_messages),
                        self._ordinary_threshold - 1,
                    ),
                    cooldown_remaining=0,
                )

            if _TEXT_MEME_ALWAYS_BLOCK_RE.search(
                message
            ) or _TEXT_MEME_LITERAL_SAFETY_RE.search(literal_message):
                self._records[key] = _MemeCadenceRecord(revision, 0, 0)
                return MemeComplementDecision(False, "hard_safety_context")
            if category is not None and _TEXT_MEME_PLAYFUL_RE.search(message):
                self._records[key] = _MemeCadenceRecord(revision, 0, 0)
                return MemeComplementDecision(
                    True,
                    "explicit_playful_context",
                    category,
                )
            recent_playful = any(
                _TEXT_MEME_PLAYFUL_RE.search(recent_message)
                for recent_message in recent_sender_messages[-3:]
            )
            if (
                category is not None
                and recent_playful
                and _TEXT_MEME_CONTINUATION_RE.search(message)
            ):
                self._records[key] = _MemeCadenceRecord(revision, 0, 0)
                return MemeComplementDecision(
                    True,
                    "recent_playful_continuation",
                    category,
                )
            if _TEXT_MEME_REQUEST_RE.search(message) and category not in {
                MemeCategory.CONFUSED,
                MemeCategory.REQUEST,
                MemeCategory.THINKING,
            }:
                self._records[key] = _MemeCadenceRecord(revision, 0, 0)
                return MemeComplementDecision(False, "serious_or_request_context")
            if category is not None and _TEXT_MEME_LIGHT_RE.search(message):
                self._records[key] = _MemeCadenceRecord(revision, 0, 0)
                return MemeComplementDecision(
                    True,
                    "lightweight_complement",
                    category,
                )
            if current.cooldown_remaining:
                self._records[key] = _MemeCadenceRecord(
                    revision,
                    0,
                    current.cooldown_remaining - 1,
                )
                return MemeComplementDecision(False, "cadence_cooldown")
            safe_turns = current.safe_turns + 1
            if category is not None:
                if safe_turns >= self._ordinary_threshold:
                    self._records[key] = _MemeCadenceRecord(
                        revision,
                        0,
                        self._cooldown_turns,
                    )
                    return MemeComplementDecision(
                        True,
                        "conversation_cadence_complement",
                        category,
                    )
                self._records[key] = _MemeCadenceRecord(revision, 0, 0)
                return MemeComplementDecision(
                    True,
                    "categorized_situation",
                    category,
                )
            if safe_turns >= self._ordinary_threshold:
                self._records[key] = _MemeCadenceRecord(
                    revision,
                    self._ordinary_threshold - 1,
                    0,
                )
                return MemeComplementDecision(
                    False,
                    "unsupported_situation",
                )
            self._records[key] = _MemeCadenceRecord(revision, safe_turns, 0)
            return MemeComplementDecision(False, "no_complementary_cue")

    def trace_metadata(self) -> dict[str, int]:
        with self._lock:
            return {
                "schema_version": 1,
                "meme_complement_enabled": self._enabled,
                "meme_cadence_subject_count": len(self._records),
                "meme_cadence_threshold": self._ordinary_threshold,
                "meme_cadence_cooldown_turns": self._cooldown_turns,
            }


def decide_text_meme_complement(
    *,
    cadence: MemeComplementCadence,
    planned_action_authority: PlannedActionAuthority,
    planned_action: PlannedAction,
    current_message: str,
    social_act: str = "answer_target",
    emotion_tags: tuple[str, ...] = (),
    relationship_distance: RelationshipDistance = RelationshipDistance.PEER,
    recent_sender_messages: tuple[str, ...] = (),
) -> MemeComplementDecision:
    """Select only lightweight complementary presentation, never a meme query."""

    if type(cadence) is not MemeComplementCadence:
        raise ContractViolation("meme_complement_cadence_required")
    return cadence.decide(
        planned_action_authority=planned_action_authority,
        planned_action=planned_action,
        current_message=current_message,
        social_act=social_act,
        emotion_tags=emotion_tags,
        relationship_distance=relationship_distance,
        recent_sender_messages=recent_sender_messages,
    )


def _require_exact_text(value: object, reason: str) -> str:
    if type(value) is not str or not value:
        raise ContractViolation(reason)
    return value


def _require_digest(value: object, reason: str) -> str:
    text = _require_exact_text(value, reason)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ContractViolation(reason)
    return text


@dataclass(frozen=True, slots=True)
class MemeManagerRuntimeProfile:
    plugin_name: str
    plugin_version: str
    root_dir_name: str
    module_path: str
    metadata_module: str
    metadata_qualname: str
    instance_module: str
    instance_qualname: str
    class_source_digest: str
    prepare_source_digest: str
    send_source_digest: str

    def __post_init__(self) -> None:
        for name in (
            "plugin_name",
            "plugin_version",
            "root_dir_name",
            "module_path",
            "metadata_module",
            "metadata_qualname",
            "instance_module",
            "instance_qualname",
        ):
            _require_exact_text(getattr(self, name), "meme_runtime_profile_invalid")
        for name in (
            "class_source_digest",
            "prepare_source_digest",
            "send_source_digest",
        ):
            _require_digest(getattr(self, name), "meme_runtime_profile_invalid")


def production_meme_manager_runtime_profile() -> MemeManagerRuntimeProfile:
    """Return the audited FNOS Meme Manager 4.15.1 build descriptor."""

    return MemeManagerRuntimeProfile(
        plugin_name="meme_manager",
        plugin_version="4.15.1",
        root_dir_name="meme_manager",
        module_path="data.plugins.meme_manager.main",
        metadata_module="astrbot.core.star.star",
        metadata_qualname="StarMetadata",
        instance_module="data.plugins.meme_manager.main",
        instance_qualname="MemeSender",
        class_source_digest=(
            "028188c6ac1ff0594d33367ae6beddb2b2583917a9774049b3b0a364ae6b2416"
        ),
        prepare_source_digest=(
            "4867e3f8da3b6954b55ebe7da73e512d8a0b1211731346facff2fdc9209b23dc"
        ),
        send_source_digest=(
            "4867e3f8da3b6954b55ebe7da73e512d8a0b1211731346facff2fdc9209b23dc"
        ),
    )


def _source_file_digest(value: object) -> str:
    source_path = inspect.getsourcefile(value)
    if type(source_path) is not str or not source_path:
        raise ContractViolation("meme_runtime_source_unavailable")
    path = Path(source_path)
    try:
        payload = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ContractViolation("meme_runtime_source_unavailable") from exc
    return hashlib.sha256(payload).hexdigest()


def _method_shape(method: object) -> tuple[tuple[str, inspect._ParameterKind], ...]:
    if not inspect.ismethod(method) or not inspect.iscoroutinefunction(method):
        raise ContractViolation("meme_runtime_method_invalid")
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError) as exc:
        raise ContractViolation("meme_runtime_method_invalid") from exc
    return tuple((parameter.name, parameter.kind) for parameter in parameters)


@dataclass(frozen=True, slots=True)
class MemeManagerConformanceResult:
    status: MemeManagerConformanceStatus
    reason_code: str
    evidence: MemeManagerRuntimeEvidence | None

    def __post_init__(self) -> None:
        if type(self.status) is not MemeManagerConformanceStatus:
            raise ContractViolation("meme_conformance_status_invalid")
        _require_exact_text(self.reason_code, "meme_conformance_reason_invalid")
        if self.status is MemeManagerConformanceStatus.VERIFIED:
            if type(self.evidence) is not MemeManagerRuntimeEvidence:
                raise ContractViolation("meme_conformance_evidence_required")
        elif self.evidence is not None:
            raise ContractViolation("meme_conformance_evidence_unexpected")

    def trace_metadata(self) -> dict[str, str | bool]:
        return {
            "meme_runtime_status": self.status.value,
            "meme_runtime_verified": self.status
            is MemeManagerConformanceStatus.VERIFIED,
            "meme_runtime_reason": self.reason_code,
        }


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class MemeManagerRuntimeEvidence:
    _collector: MemeManagerConformanceCollector
    _seal: object

    def trace_metadata(self) -> dict[str, bool | int]:
        collector = getattr(self, "_collector", None)
        try:
            canonical = bool(
                type(collector) is MemeManagerConformanceCollector
                and collector.inspect(self) is self
            )
        except (ContractViolation, AttributeError, TypeError):
            canonical = False
        return {
            "schema_version": 1,
            "meme_runtime_canonical": canonical,
        }

    def __repr__(self) -> str:
        return f"MemeManagerRuntimeEvidence(canonical={self.trace_metadata()['meme_runtime_canonical']!r})"


@dataclass(frozen=True, slots=True)
class _RuntimeEvidenceRecord:
    evidence: MemeManagerRuntimeEvidence
    metadata: object
    instance: object
    prepare_method: object
    send_method: object
    snapshot: tuple[object, ...]


class MemeManagerConformanceCollector:
    __slots__ = ("_lock", "_profile", "_records")

    def __init__(self, profile: MemeManagerRuntimeProfile) -> None:
        if type(profile) is not MemeManagerRuntimeProfile:
            raise ContractViolation("meme_runtime_profile_required")
        self._profile = profile
        self._records: dict[int, _RuntimeEvidenceRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _failure(
        status: MemeManagerConformanceStatus,
        reason: str,
    ) -> MemeManagerConformanceResult:
        return MemeManagerConformanceResult(status, reason, None)

    def _runtime_snapshot(
        self,
        metadata: object,
        instance: object,
        prepare_method: object,
        send_method: object,
    ) -> tuple[object, ...]:
        profile = self._profile
        metadata_type = type(metadata)
        instance_type = type(instance)
        try:
            name = metadata.name
            version = metadata.version
            activated = metadata.activated
            root_dir_name = metadata.root_dir_name
            module_path = metadata.module_path
            star_cls = metadata.star_cls
            star_cls_type = metadata.star_cls_type
        except AttributeError as exc:
            raise ContractViolation("meme_runtime_metadata_invalid") from exc
        if (
            type(name) is not str
            or type(version) is not str
            or type(activated) is not bool
            or type(root_dir_name) is not str
            or type(module_path) is not str
            or star_cls is not instance
            or star_cls_type is not instance_type
            or metadata_type.__module__ != profile.metadata_module
            or metadata_type.__qualname__ != profile.metadata_qualname
            or instance_type.__module__ != profile.instance_module
            or instance_type.__qualname__ != profile.instance_qualname
        ):
            raise ContractViolation("meme_runtime_metadata_invalid")
        if (
            name != profile.plugin_name
            or version != profile.plugin_version
            or root_dir_name != profile.root_dir_name
            or module_path != profile.module_path
        ):
            raise ContractViolation("meme_runtime_profile_mismatch")
        if not activated:
            raise ContractViolation("meme_runtime_disabled")
        if getattr(prepare_method, "__self__", None) is not instance or getattr(
            send_method, "__self__", None
        ) is not instance:
            raise ContractViolation("meme_runtime_method_invalid")
        if _method_shape(prepare_method) != _EXPECTED_PREPARE_PARAMETERS:
            raise ContractViolation("meme_runtime_method_invalid")
        if _method_shape(send_method) != _EXPECTED_SEND_PARAMETERS:
            raise ContractViolation("meme_runtime_method_invalid")
        digests = (
            _source_file_digest(instance_type),
            _source_file_digest(prepare_method),
            _source_file_digest(send_method),
        )
        if digests != (
            profile.class_source_digest,
            profile.prepare_source_digest,
            profile.send_source_digest,
        ):
            raise ContractViolation("meme_runtime_build_changed")
        return (
            metadata_type,
            instance_type,
            name,
            version,
            activated,
            root_dir_name,
            module_path,
            prepare_method.__func__,
            send_method.__func__,
            *digests,
        )

    def collect(self, context: object) -> MemeManagerConformanceResult:
        get_registered = getattr(context, "get_registered_star", None)
        get_all = getattr(context, "get_all_stars", None)
        if not callable(get_registered) or not callable(get_all):
            return self._failure(
                MemeManagerConformanceStatus.INTERFACE_CHANGED,
                "registry_interface_changed",
            )
        try:
            metadata = get_registered(self._profile.plugin_name)
            stars = get_all()
        except Exception:
            return self._failure(MemeManagerConformanceStatus.ERROR, "registry_error")
        if metadata is None:
            return self._failure(MemeManagerConformanceStatus.MISSING, "plugin_missing")
        if type(stars) is not list or sum(
            type(getattr(item, "name", None)) is str
            and getattr(item, "name") == self._profile.plugin_name
            for item in stars
        ) != 1:
            return self._failure(
                MemeManagerConformanceStatus.INTERFACE_CHANGED,
                "plugin_not_unique",
            )
        try:
            instance = metadata.star_cls
            activated = metadata.activated
        except AttributeError:
            return self._failure(
                MemeManagerConformanceStatus.INTERFACE_CHANGED,
                "metadata_interface_changed",
            )
        if type(activated) is bool and not activated:
            return self._failure(MemeManagerConformanceStatus.DISABLED, "plugin_disabled")
        prepare_method = getattr(instance, "compat_prepare_message", None)
        send_method = getattr(instance, "compat_send_prepared_message", None)
        try:
            snapshot = self._runtime_snapshot(
                metadata,
                instance,
                prepare_method,
                send_method,
            )
        except ContractViolation as exc:
            reason = str(exc)
            if reason == "meme_runtime_build_changed":
                return self._failure(
                    MemeManagerConformanceStatus.BUILD_CHANGED,
                    "runtime_build_changed",
                )
            if reason == "meme_runtime_disabled":
                return self._failure(
                    MemeManagerConformanceStatus.DISABLED,
                    "plugin_disabled",
                )
            return self._failure(
                MemeManagerConformanceStatus.INTERFACE_CHANGED,
                "runtime_interface_changed",
            )
        evidence = object.__new__(MemeManagerRuntimeEvidence)
        object.__setattr__(evidence, "_collector", self)
        object.__setattr__(evidence, "_seal", _RUNTIME_EVIDENCE_SEAL)
        record = _RuntimeEvidenceRecord(
            evidence=evidence,
            metadata=metadata,
            instance=instance,
            prepare_method=prepare_method,
            send_method=send_method,
            snapshot=snapshot,
        )
        with self._lock:
            if len(self._records) >= 8:
                self._records.pop(next(iter(self._records)))
            self._records[id(evidence)] = record
        return MemeManagerConformanceResult(
            MemeManagerConformanceStatus.VERIFIED,
            "runtime_verified",
            evidence,
        )

    def inspect(
        self,
        evidence: MemeManagerRuntimeEvidence,
    ) -> MemeManagerRuntimeEvidence:
        if type(evidence) is not MemeManagerRuntimeEvidence:
            raise ContractViolation("meme_runtime_evidence_not_canonical")
        with self._lock:
            record = self._records.get(id(evidence))
            if record is None or record.evidence is not evidence:
                raise ContractViolation("meme_runtime_evidence_not_canonical")
            try:
                collector = evidence._collector
                seal = evidence._seal
                snapshot = self._runtime_snapshot(
                    record.metadata,
                    record.instance,
                    record.prepare_method,
                    record.send_method,
                )
            except (ContractViolation, AttributeError, TypeError) as exc:
                raise ContractViolation("meme_runtime_evidence_corrupt") from exc
            if collector is not self or seal is not _RUNTIME_EVIDENCE_SEAL or snapshot != record.snapshot:
                raise ContractViolation("meme_runtime_evidence_corrupt")
            return evidence

    def _open(
        self,
        evidence: MemeManagerRuntimeEvidence,
    ) -> tuple[object, object, object]:
        self.inspect(evidence)
        with self._lock:
            record = self._records[id(evidence)]
            return record.instance, record.prepare_method, record.send_method

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "schema_version": 1,
                "meme_runtime_evidence_count": len(self._records),
                "meme_runtime_evidence_bounded": len(self._records) <= 8,
            }

    def __copy__(self):
        raise ContractViolation("meme_conformance_collector_copy_forbidden")

    __deepcopy__ = __copy__

    def __reduce__(self):
        raise ContractViolation("meme_conformance_collector_pickle_forbidden")


def _expression_snapshot(intent: ExpressionIntent) -> tuple[object, ...]:
    if type(intent) is not ExpressionIntent:
        raise ContractViolation("expression_intent_required")
    if type(intent.modality) is not ExpressionModality:
        raise ContractViolation("expression_intent_corrupt")
    if type(intent.social_act) is not str:
        raise ContractViolation("expression_intent_corrupt")
    if type(intent.emotion_tags) is not tuple or any(
        type(value) is not str for value in intent.emotion_tags
    ):
        raise ContractViolation("expression_intent_corrupt")
    if type(intent.max_bubbles) is not int or type(intent.max_meme_calls) is not int:
        raise ContractViolation("expression_intent_corrupt")
    if type(intent.public_scope_key) is not str or type(intent.meme_executor) is not str:
        raise ContractViolation("expression_intent_corrupt")
    if type(intent.reason_codes) is not tuple or any(
        type(value) is not str for value in intent.reason_codes
    ):
        raise ContractViolation("expression_intent_corrupt")
    ExpressionIntent(
        binding=intent.binding,
        reply_target=intent.reply_target,
        modality=intent.modality,
        social_act=intent.social_act,
        emotion_tags=intent.emotion_tags,
        max_bubbles=intent.max_bubbles,
        public_scope_key=intent.public_scope_key,
        meme_executor=intent.meme_executor,
        max_meme_calls=intent.max_meme_calls,
        reason_codes=intent.reason_codes,
    )
    return (
        intent.binding,
        intent.reply_target,
        intent.modality,
        intent.social_act,
        intent.emotion_tags,
        intent.max_bubbles,
        intent.public_scope_key,
        intent.meme_executor,
        intent.max_meme_calls,
        intent.reason_codes,
    )


@dataclass(frozen=True, slots=True)
class _ExpressionRecord:
    intent: ExpressionIntent
    planned_action: PlannedAction
    planned_action_authority: PlannedActionAuthority
    snapshot: tuple[object, ...]


class ExpressionIntentAuthority:
    __slots__ = ("_lock", "_max_intents", "_records", "_by_plan")

    def __init__(self, *, max_intents: int = 1024) -> None:
        if type(max_intents) is not int or not 1 <= max_intents <= 4096:
            raise ContractViolation("expression_intent_authority_limit_invalid")
        self._max_intents = max_intents
        self._records: dict[int, _ExpressionRecord] = {}
        self._by_plan: dict[int, ExpressionIntent] = {}
        self._lock = threading.RLock()

    def issue(
        self,
        *,
        planned_action_authority: PlannedActionAuthority,
        planned_action: PlannedAction,
        modality: ExpressionModality,
        social_act: str,
        emotion_tags: tuple[str, ...] = (),
        current_message: str = "",
        relationship_distance: RelationshipDistance | None = None,
        recent_sender_messages: tuple[str, ...] = (),
        max_bubbles: int = 1,
        public_scope_key: str = "",
        meme_executor: str = "",
        max_meme_calls: int = 0,
        reason_codes: tuple[str, ...] = (),
    ) -> ExpressionIntent:
        if type(planned_action_authority) is not PlannedActionAuthority:
            raise ContractViolation("planned_action_authority_required")
        planned_action_authority.inspect_plan(planned_action)
        if type(emotion_tags) is not tuple or any(
            type(tag) is not str for tag in emotion_tags
        ):
            raise ContractViolation("meme_expression_marker_invalid")
        if any(tag.startswith(_MEME_CATEGORY_TAG_PREFIX) for tag in emotion_tags):
            raise ContractViolation("meme_category_tag_caller_forbidden")
        if (
            type(recent_sender_messages) is not tuple
            or len(recent_sender_messages) > 8
            or any(
                type(recent) is not str or len(recent) > 500
                for recent in recent_sender_messages
            )
        ):
            raise ContractViolation("meme_category_context_invalid")
        meme_modality = modality in {
            ExpressionModality.TEXT_AND_MEME,
            ExpressionModality.MEME_ONLY,
            ExpressionModality.REACTION,
        }
        if meme_modality:
            if (
                type(current_message) is not str
                or ledger_content_digest(current_message)
                != planned_action.binding.current_content_digest
                or type(relationship_distance) is not RelationshipDistance
            ):
                raise ContractViolation("meme_category_current_context_required")
            category = select_meme_category(
                kind=(
                    MemeExecutionKind.REACTION
                    if modality is ExpressionModality.REACTION
                    else MemeExecutionKind.MEME_COMPLEMENT
                ),
                current_message=current_message,
                social_act=social_act,
                emotion_tags=emotion_tags,
                relationship_distance=relationship_distance,
                recent_sender_messages=recent_sender_messages,
            )
            if category is None:
                raise ContractViolation("meme_category_unsupported")
            emotion_tags = (
                *emotion_tags,
                f"{_MEME_CATEGORY_TAG_PREFIX}{category.value}",
            )
        elif current_message or relationship_distance is not None or recent_sender_messages:
            raise ContractViolation("meme_category_without_meme_modality")
        with self._lock:
            if id(planned_action) in self._by_plan:
                raise ContractViolation("expression_intent_already_issued")
            if len(self._records) >= self._max_intents:
                oldest = next(iter(self._records))
                old_record = self._records.pop(oldest)
                self._by_plan.pop(id(old_record.planned_action), None)
            intent = ExpressionIntent(
                binding=planned_action.binding,
                reply_target=planned_action.action.reply_target,
                modality=modality,
                social_act=social_act,
                emotion_tags=emotion_tags,
                max_bubbles=max_bubbles,
                public_scope_key=public_scope_key,
                meme_executor=meme_executor,
                max_meme_calls=max_meme_calls,
                reason_codes=reason_codes,
            )
            record = _ExpressionRecord(
                intent=intent,
                planned_action=planned_action,
                planned_action_authority=planned_action_authority,
                snapshot=_expression_snapshot(intent),
            )
            self._records[id(intent)] = record
            self._by_plan[id(planned_action)] = intent
            return intent

    def inspect(
        self,
        intent: ExpressionIntent,
        *,
        planned_action: PlannedAction,
    ) -> ExpressionIntent:
        if type(intent) is not ExpressionIntent:
            raise ContractViolation("expression_intent_not_canonical")
        with self._lock:
            record = self._records.get(id(intent))
            if record is None or record.intent is not intent:
                raise ContractViolation("expression_intent_not_canonical")
            try:
                record.planned_action_authority.inspect_plan(record.planned_action)
                snapshot = _expression_snapshot(intent)
            except (ContractViolation, AttributeError, TypeError) as exc:
                raise ContractViolation("expression_intent_corrupt") from exc
            if (
                planned_action is not record.planned_action
                or intent.binding is not planned_action.binding
                or intent.reply_target is not planned_action.action.reply_target
                or snapshot != record.snapshot
                or self._by_plan.get(id(planned_action)) is not intent
            ):
                raise ContractViolation("expression_intent_corrupt")
            return intent

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "schema_version": 1,
                "expression_intent_count": len(self._records),
                "expression_intent_bounded": len(self._records) <= self._max_intents,
            }

    def __copy__(self):
        raise ContractViolation("expression_intent_authority_copy_forbidden")

    __deepcopy__ = __copy__

    def __reduce__(self):
        raise ContractViolation("expression_intent_authority_pickle_forbidden")


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class MemeExecutionLease:
    kind: MemeExecutionKind
    _authority: MemeExecutionAuthority
    _seal: object

    def trace_metadata(self) -> dict[str, str | bool | int]:
        authority = getattr(self, "_authority", None)
        try:
            return authority._lease_trace(self) if type(authority) is MemeExecutionAuthority else {
                "schema_version": 1,
                "meme_execution_kind": "invalid",
                "meme_execution_canonical": False,
                "meme_execution_claimed": False,
            }
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "meme_execution_kind": "invalid",
                "meme_execution_canonical": False,
                "meme_execution_claimed": False,
            }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "MemeExecutionLease("
            f"kind={metadata['meme_execution_kind']!r}, "
            f"canonical={metadata['meme_execution_canonical']!r}, "
            f"claimed={metadata['meme_execution_claimed']!r})"
        )


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class MemeExecutionPermit:
    kind: MemeExecutionKind
    _authority: MemeExecutionAuthority
    _seal: object

    def trace_metadata(self) -> dict[str, str | bool | int]:
        authority = getattr(self, "_authority", None)
        try:
            return authority._permit_trace(self) if type(authority) is MemeExecutionAuthority else {
                "schema_version": 1,
                "meme_execution_kind": "invalid",
                "meme_execution_permit_canonical": False,
            }
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "meme_execution_kind": "invalid",
                "meme_execution_permit_canonical": False,
            }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "MemeExecutionPermit("
            f"kind={metadata['meme_execution_kind']!r}, "
            f"canonical={metadata['meme_execution_permit_canonical']!r})"
        )


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class MemeExecutionReceipt:
    kind: MemeExecutionKind
    status: MemeExecutionStatus
    attempt_count: int
    success_count: int
    reason_code: str
    _authority: MemeExecutionAuthority
    _seal: object

    def trace_metadata(self) -> dict[str, str | int | bool]:
        authority = getattr(self, "_authority", None)
        try:
            return authority._receipt_trace(self) if type(authority) is MemeExecutionAuthority else {
                "schema_version": 1,
                "meme_execution_kind": "invalid",
                "meme_execution_status": "invalid",
                "meme_execution_attempt_count": 0,
                "meme_execution_success_count": 0,
                "meme_execution_reason_code": "invalid",
                "meme_execution_receipt_canonical": False,
            }
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "meme_execution_kind": "invalid",
                "meme_execution_status": "invalid",
                "meme_execution_attempt_count": 0,
                "meme_execution_success_count": 0,
                "meme_execution_reason_code": "invalid",
                "meme_execution_receipt_canonical": False,
            }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "MemeExecutionReceipt("
            f"kind={metadata['meme_execution_kind']!r}, "
            f"status={metadata['meme_execution_status']!r}, "
            f"canonical={metadata['meme_execution_receipt_canonical']!r})"
        )


@dataclass(slots=True)
class _LeaseRecord:
    lease: MemeExecutionLease
    kind: MemeExecutionKind
    planned_action: PlannedAction
    expression_intent: ExpressionIntent
    generation: GenerationEpochSnapshot
    runtime_evidence: MemeManagerRuntimeEvidence
    presentation: PresentationHandoff | None = None
    send_ledger: InternalSendReceiptLedger | None = None
    send_evidence: OwnerActionSendTerminalEvidence | None = None
    claimed: bool = False
    started: bool = False
    permit: MemeExecutionPermit | None = None
    receipt: MemeExecutionReceipt | None = None


@dataclass(frozen=True, slots=True)
class _ReceiptRecord:
    receipt: MemeExecutionReceipt
    kind: MemeExecutionKind
    status: MemeExecutionStatus
    attempt_count: int
    success_count: int
    reason_code: str
    source: object


class MemeExecutionAuthority:
    __slots__ = (
        "_collector",
        "_expression_authority",
        "_generation_registry",
        "_lock",
        "_max_leases",
        "_permits",
        "_plan_authority",
        "_receipts",
        "_records",
    )

    def __init__(
        self,
        *,
        planned_action_authority: PlannedActionAuthority,
        expression_intent_authority: ExpressionIntentAuthority,
        generation_registry: GenerationEpochRegistry,
        conformance_collector: MemeManagerConformanceCollector,
        max_leases: int = 1024,
    ) -> None:
        if type(planned_action_authority) is not PlannedActionAuthority:
            raise ContractViolation("planned_action_authority_required")
        if type(expression_intent_authority) is not ExpressionIntentAuthority:
            raise ContractViolation("expression_intent_authority_required")
        if type(generation_registry) is not GenerationEpochRegistry:
            raise ContractViolation("generation_registry_required")
        if type(conformance_collector) is not MemeManagerConformanceCollector:
            raise ContractViolation("meme_conformance_collector_required")
        if type(max_leases) is not int or not 1 <= max_leases <= 4096:
            raise ContractViolation("meme_execution_limit_invalid")
        self._plan_authority = planned_action_authority
        self._expression_authority = expression_intent_authority
        self._generation_registry = generation_registry
        self._collector = conformance_collector
        self._max_leases = max_leases
        self._records: dict[int, _LeaseRecord] = {}
        self._permits: dict[int, _LeaseRecord] = {}
        self._receipts: dict[int, _ReceiptRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _execution_kind(
        planned_action: PlannedAction,
        expression_intent: ExpressionIntent,
    ) -> MemeExecutionKind:
        if (
            planned_action.kind is ActionKind.REACT
            and expression_intent.modality is ExpressionModality.REACTION
        ):
            return MemeExecutionKind.REACTION
        if (
            planned_action.kind is ActionKind.REPLY
            and expression_intent.modality
            in {ExpressionModality.TEXT_AND_MEME, ExpressionModality.MEME_ONLY}
        ):
            return MemeExecutionKind.MEME_COMPLEMENT
        raise ContractViolation("meme_execution_shape_invalid")

    def _validate_generation(
        self,
        generation: GenerationEpochSnapshot,
        planned_action: PlannedAction,
    ) -> None:
        try:
            record = self._generation_registry.inspect(generation)
        except Exception as exc:
            raise ContractViolation("meme_generation_not_canonical") from exc
        validation = self._generation_registry.validate(generation)
        if not validation.is_current:
            raise ContractViolation("meme_generation_stale")
        binding = planned_action.binding
        if (
            record.scope_key != binding.scope_key
            or record.session_id != binding.session_id
            or record.epoch != binding.generation_epoch
        ):
            raise ContractViolation("meme_generation_binding_mismatch")

    def prepare(
        self,
        *,
        planned_action: PlannedAction,
        expression_intent: ExpressionIntent,
        generation: GenerationEpochSnapshot,
        runtime_evidence: MemeManagerRuntimeEvidence,
        presentation: PresentationHandoff | None = None,
        send_ledger: InternalSendReceiptLedger | None = None,
        send_evidence: OwnerActionSendTerminalEvidence | None = None,
    ) -> MemeExecutionLease:
        self._plan_authority.inspect_plan(planned_action)
        self._expression_authority.inspect(
            expression_intent,
            planned_action=planned_action,
        )
        self._validate_generation(generation, planned_action)
        self._collector.inspect(runtime_evidence)
        kind = self._execution_kind(planned_action, expression_intent)
        if kind is MemeExecutionKind.MEME_COMPLEMENT:
            self._validate_presentation_send(
                planned_action=planned_action,
                expression_intent=expression_intent,
                presentation=presentation,
                send_ledger=send_ledger,
                send_evidence=send_evidence,
            )
        elif any(
            value is not None
            for value in (presentation, send_ledger, send_evidence)
        ):
            raise ContractViolation("meme_reaction_send_evidence_forbidden")
        with self._lock:
            if any(
                record.expression_intent is expression_intent
                for record in self._records.values()
            ):
                raise ContractViolation("meme_execution_already_prepared")
            if len(self._records) >= self._max_leases:
                removable = next(
                    (key for key, record in self._records.items() if record.claimed),
                    None,
                )
                if removable is None:
                    raise ContractViolation("meme_execution_ledger_full")
                old = self._records.pop(removable)
                if old.permit is not None:
                    self._permits.pop(id(old.permit), None)
            lease = object.__new__(MemeExecutionLease)
            object.__setattr__(lease, "kind", kind)
            object.__setattr__(lease, "_authority", self)
            object.__setattr__(lease, "_seal", _LEASE_SEAL)
            self._records[id(lease)] = _LeaseRecord(
                lease=lease,
                kind=kind,
                planned_action=planned_action,
                expression_intent=expression_intent,
                generation=generation,
                runtime_evidence=runtime_evidence,
                presentation=presentation,
                send_ledger=send_ledger,
                send_evidence=send_evidence,
            )
            return lease

    @staticmethod
    def _validate_presentation_send(
        *,
        planned_action: PlannedAction,
        expression_intent: ExpressionIntent,
        presentation: PresentationHandoff | None,
        send_ledger: InternalSendReceiptLedger | None,
        send_evidence: OwnerActionSendTerminalEvidence | None,
    ) -> None:
        if (
            type(presentation) is not PresentationHandoff
            or type(send_ledger) is not InternalSendReceiptLedger
            or type(send_evidence) is not OwnerActionSendTerminalEvidence
        ):
            raise ContractViolation("meme_complement_send_evidence_required")
        try:
            inspection = _inspect_owner_action_send_terminal_evidence(
                send_evidence,
                ledger=send_ledger,
                presentation=presentation,
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise ContractViolation("meme_complement_send_evidence_invalid") from exc
        request = inspection.composer_request
        if (
            inspection.presentation is not presentation
            or request.planned_action is not planned_action
            or request.expression_intent is not expression_intent
        ):
            raise ContractViolation("meme_complement_send_evidence_mismatch")

    def _record_for(self, lease: MemeExecutionLease) -> _LeaseRecord:
        if type(lease) is not MemeExecutionLease:
            raise ContractViolation("meme_execution_lease_not_canonical")
        record = self._records.get(id(lease))
        if (
            type(record) is not _LeaseRecord
            or record.lease is not lease
        ):
            raise ContractViolation("meme_execution_lease_not_canonical")
        try:
            kind = lease.kind
            authority = lease._authority
            seal = lease._seal
        except AttributeError as exc:
            raise ContractViolation("meme_execution_lease_corrupt") from exc
        if (
            type(kind) is not MemeExecutionKind
            or kind is not record.kind
            or authority is not self
            or seal is not _LEASE_SEAL
        ):
            raise ContractViolation("meme_execution_lease_corrupt")
        self._plan_authority.inspect_plan(record.planned_action)
        self._expression_authority.inspect(
            record.expression_intent,
            planned_action=record.planned_action,
        )
        self._collector.inspect(record.runtime_evidence)
        if record.kind is MemeExecutionKind.MEME_COMPLEMENT:
            self._validate_presentation_send(
                planned_action=record.planned_action,
                expression_intent=record.expression_intent,
                presentation=record.presentation,
                send_ledger=record.send_ledger,
                send_evidence=record.send_evidence,
            )
        return record

    def issue_stale_complement(
        self,
        *,
        planned_action: PlannedAction,
        expression_intent: ExpressionIntent,
        generation: GenerationEpochSnapshot,
        presentation: PresentationHandoff,
        send_ledger: InternalSendReceiptLedger,
        send_evidence: OwnerActionSendTerminalEvidence,
    ) -> MemeExecutionReceipt:
        self._plan_authority.inspect_plan(planned_action)
        self._expression_authority.inspect(
            expression_intent,
            planned_action=planned_action,
        )
        if self._execution_kind(
            planned_action,
            expression_intent,
        ) is not MemeExecutionKind.MEME_COMPLEMENT:
            raise ContractViolation("meme_stale_complement_shape_invalid")
        try:
            epoch_record = self._generation_registry.inspect(generation)
        except Exception as exc:
            raise ContractViolation("meme_generation_not_canonical") from exc
        binding = planned_action.binding
        if (
            epoch_record.scope_key != binding.scope_key
            or epoch_record.session_id != binding.session_id
            or epoch_record.epoch != binding.generation_epoch
        ):
            raise ContractViolation("meme_generation_binding_mismatch")
        if self._generation_registry.validate(generation).is_current:
            raise ContractViolation("meme_generation_still_current")
        self._validate_presentation_send(
            planned_action=planned_action,
            expression_intent=expression_intent,
            presentation=presentation,
            send_ledger=send_ledger,
            send_evidence=send_evidence,
        )
        with self._lock:
            return self._mint_receipt(
                kind=MemeExecutionKind.MEME_COMPLEMENT,
                status=MemeExecutionStatus.STALE_BEFORE_SEND,
                attempt_count=0,
                success_count=0,
                reason_code="generation_stale_before_complement",
                source=send_evidence,
            )

    def claim(
        self,
        lease: MemeExecutionLease,
        *,
        generation: GenerationEpochSnapshot,
    ) -> MemeExecutionPermit:
        with self._lock:
            record = self._record_for(lease)
            if record.claimed:
                raise ContractViolation("meme_execution_already_claimed")
            if generation is not record.generation:
                raise ContractViolation("meme_generation_not_canonical")
            self._validate_generation(generation, record.planned_action)
            permit = object.__new__(MemeExecutionPermit)
            object.__setattr__(permit, "kind", record.kind)
            object.__setattr__(permit, "_authority", self)
            object.__setattr__(permit, "_seal", _PERMIT_SEAL)
            record.claimed = True
            record.permit = permit
            self._permits[id(permit)] = record
            return permit

    def issue_suppressed(
        self,
        *,
        planned_action: PlannedAction,
        expression_intent: ExpressionIntent,
        generation: GenerationEpochSnapshot,
        reason_code: str,
    ) -> MemeExecutionReceipt:
        allowed_reasons = {
            "runtime_missing",
            "runtime_disabled",
            "runtime_interface_changed",
            "runtime_build_changed",
            "runtime_error",
            "policy_suppressed",
        }
        if type(reason_code) is not str or reason_code not in allowed_reasons:
            raise ContractViolation("meme_execution_suppression_reason_invalid")
        self._plan_authority.inspect_plan(planned_action)
        self._expression_authority.inspect(
            expression_intent,
            planned_action=planned_action,
        )
        self._validate_generation(generation, planned_action)
        kind = self._execution_kind(planned_action, expression_intent)
        with self._lock:
            return self._mint_receipt(
                kind=kind,
                status=MemeExecutionStatus.SUPPRESSED,
                attempt_count=0,
                success_count=0,
                reason_code=reason_code,
                source=expression_intent,
            )

    def inspect_permit(self, permit: MemeExecutionPermit) -> MemeExecutionPermit:
        if type(permit) is not MemeExecutionPermit:
            raise ContractViolation("meme_execution_permit_not_canonical")
        with self._lock:
            record = self._permits.get(id(permit))
            if record is None or record.permit is not permit or not record.claimed:
                raise ContractViolation("meme_execution_permit_not_canonical")
            try:
                kind = permit.kind
                authority = permit._authority
                seal = permit._seal
            except AttributeError as exc:
                raise ContractViolation("meme_execution_permit_corrupt") from exc
            if (
                type(kind) is not MemeExecutionKind
                or kind is not record.kind
                or authority is not self
                or seal is not _PERMIT_SEAL
            ):
                raise ContractViolation("meme_execution_permit_corrupt")
            self._record_for(record.lease)
            return permit

    def _begin(
        self,
        permit: MemeExecutionPermit,
        *,
        generation: GenerationEpochSnapshot,
    ) -> tuple[object, object, ExpressionIntent, PlannedAction]:
        with self._lock:
            self.inspect_permit(permit)
            record = self._permits[id(permit)]
            if record.started:
                raise ContractViolation("meme_execution_already_started")
            if generation is not record.generation:
                raise ContractViolation("meme_generation_not_canonical")
            self._validate_generation(generation, record.planned_action)
            _, prepare_method, send_method = self._collector._open(
                record.runtime_evidence
            )
            record.started = True
            return (
                prepare_method,
                send_method,
                record.expression_intent,
                record.planned_action,
            )

    def _mint_receipt(
        self,
        *,
        kind: MemeExecutionKind,
        status: MemeExecutionStatus,
        attempt_count: int,
        success_count: int,
        reason_code: str,
        source: object,
    ) -> MemeExecutionReceipt:
        if type(kind) is not MemeExecutionKind or type(status) is not MemeExecutionStatus:
            raise ContractViolation("meme_execution_receipt_shape_invalid")
        if (
            type(attempt_count) is not int
            or type(success_count) is not int
            or attempt_count not in {0, 1}
            or success_count not in {0, 1}
            or success_count > attempt_count
            or type(reason_code) is not str
            or not reason_code
        ):
            raise ContractViolation("meme_execution_receipt_shape_invalid")
        if status is MemeExecutionStatus.SUCCEEDED and (attempt_count, success_count) != (1, 1):
            raise ContractViolation("meme_execution_receipt_shape_invalid")
        if status is MemeExecutionStatus.STALE_AFTER_SEND and success_count != 1:
            raise ContractViolation("meme_execution_receipt_shape_invalid")
        if status in {
            MemeExecutionStatus.SUPPRESSED,
            MemeExecutionStatus.STALE_BEFORE_SEND,
        } and (attempt_count or success_count):
            raise ContractViolation("meme_execution_receipt_shape_invalid")
        if len(self._receipts) >= self._max_leases:
            self._receipts.pop(next(iter(self._receipts)))
        receipt = object.__new__(MemeExecutionReceipt)
        for name, value in (
            ("kind", kind),
            ("status", status),
            ("attempt_count", attempt_count),
            ("success_count", success_count),
            ("reason_code", reason_code),
            ("_authority", self),
            ("_seal", _RECEIPT_SEAL),
        ):
            object.__setattr__(receipt, name, value)
        self._receipts[id(receipt)] = _ReceiptRecord(
            receipt=receipt,
            kind=kind,
            status=status,
            attempt_count=attempt_count,
            success_count=success_count,
            reason_code=reason_code,
            source=source,
        )
        return receipt

    def _complete(
        self,
        permit: MemeExecutionPermit,
        *,
        status: MemeExecutionStatus,
        attempt_count: int,
        success_count: int,
        reason_code: str,
    ) -> MemeExecutionReceipt:
        with self._lock:
            self.inspect_permit(permit)
            record = self._permits[id(permit)]
            if not record.started:
                raise ContractViolation("meme_execution_not_started")
            if record.receipt is not None:
                raise ContractViolation("meme_execution_already_completed")
            receipt = self._mint_receipt(
                kind=record.kind,
                status=status,
                attempt_count=attempt_count,
                success_count=success_count,
                reason_code=reason_code,
                source=permit,
            )
            record.receipt = receipt
            return receipt

    def inspect_receipt(
        self,
        receipt: MemeExecutionReceipt,
    ) -> MemeExecutionReceipt:
        if type(receipt) is not MemeExecutionReceipt:
            raise ContractViolation("meme_execution_receipt_not_canonical")
        with self._lock:
            record = self._receipts.get(id(receipt))
            if (
                type(record) is not _ReceiptRecord
                or record.receipt is not receipt
            ):
                raise ContractViolation("meme_execution_receipt_not_canonical")
            try:
                values = (
                    receipt.kind,
                    receipt.status,
                    receipt.attempt_count,
                    receipt.success_count,
                    receipt.reason_code,
                    receipt._authority,
                    receipt._seal,
                )
            except AttributeError as exc:
                raise ContractViolation("meme_execution_receipt_corrupt") from exc
            kind, status, attempt_count, success_count, reason_code, authority, seal = values
            if (
                type(kind) is not MemeExecutionKind
                or kind is not record.kind
                or type(status) is not MemeExecutionStatus
                or status is not record.status
                or type(attempt_count) is not int
                or attempt_count != record.attempt_count
                or type(success_count) is not int
                or success_count != record.success_count
                or type(reason_code) is not str
                or reason_code != record.reason_code
                or authority is not self
                or seal is not _RECEIPT_SEAL
            ):
                raise ContractViolation("meme_execution_receipt_corrupt")
            return receipt

    def _lease_trace(self, lease: MemeExecutionLease) -> dict[str, str | bool | int]:
        with self._lock:
            record = self._record_for(lease)
            return {
                "schema_version": 1,
                "meme_execution_kind": record.kind.value,
                "meme_execution_canonical": True,
                "meme_execution_claimed": record.claimed,
            }

    def _permit_trace(self, permit: MemeExecutionPermit) -> dict[str, str | bool | int]:
        self.inspect_permit(permit)
        return {
            "schema_version": 1,
            "meme_execution_kind": permit.kind.value,
            "meme_execution_permit_canonical": True,
        }

    def _receipt_trace(
        self,
        receipt: MemeExecutionReceipt,
    ) -> dict[str, str | int | bool]:
        self.inspect_receipt(receipt)
        return {
            "schema_version": 1,
            "meme_execution_kind": receipt.kind.value,
            "meme_execution_status": receipt.status.value,
            "meme_execution_attempt_count": receipt.attempt_count,
            "meme_execution_success_count": receipt.success_count,
            "meme_execution_reason_code": receipt.reason_code,
            "meme_execution_receipt_canonical": True,
        }

    def _open(
        self,
        permit: MemeExecutionPermit,
    ) -> tuple[object, object, object, ExpressionIntent, PlannedAction]:
        self.inspect_permit(permit)
        with self._lock:
            record = self._permits[id(permit)]
            instance, prepare_method, send_method = self._collector._open(
                record.runtime_evidence
            )
            return (
                instance,
                prepare_method,
                send_method,
                record.expression_intent,
                record.planned_action,
            )

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "schema_version": 1,
                "meme_execution_lease_count": len(self._records),
                "meme_execution_permit_count": len(self._permits),
                "meme_execution_receipt_count": len(self._receipts),
                "meme_execution_bounded": len(self._records) <= self._max_leases,
            }

    def __copy__(self):
        raise ContractViolation("meme_execution_authority_copy_forbidden")

    __deepcopy__ = __copy__

    def __reduce__(self):
        raise ContractViolation("meme_execution_authority_pickle_forbidden")


async def _cleanup_prepared_meme(
    send_method: object,
    event: object,
    prepared: dict,
    *,
    timeout_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(
            send_method(
                event,
                prepared,
                send_text=False,
                send_images=False,
            ),
            timeout=timeout_seconds,
        )
    except BaseException:
        # The cleanup API is best-effort and sends neither text nor image.  Its
        # exception is deliberately not surfaced or copied into traces.
        return


async def execute_meme_permit(
    authority: MemeExecutionAuthority,
    permit: MemeExecutionPermit,
    *,
    event: object,
    generation: GenerationEpochSnapshot,
    timeout_seconds: float = 3.0,
) -> MemeExecutionReceipt:
    """Execute one sealed Meme Manager permit without exposing selection material."""

    if type(authority) is not MemeExecutionAuthority:
        raise ContractViolation("meme_execution_authority_required")
    if type(timeout_seconds) not in {int, float} or type(timeout_seconds) is bool:
        raise ContractViolation("meme_execution_timeout_invalid")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or not 0.05 <= timeout <= 10.0:
        raise ContractViolation("meme_execution_timeout_invalid")
    prepare_method, send_method, expression, planned_action = authority._begin(
        permit,
        generation=generation,
    )
    marker_name = _select_meme_marker(
        kind=permit.kind,
        social_act=expression.social_act,
        emotion_tags=expression.emotion_tags,
    )
    if not marker_name:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.SUPPRESSED,
            attempt_count=0,
            success_count=0,
            reason_code="unsupported_expression",
        )
    marker = f"&&{marker_name}&&"
    try:
        prepared = await asyncio.wait_for(
            prepare_method(event, marker),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.TIMED_OUT,
            attempt_count=0,
            success_count=0,
            reason_code="prepare_timeout",
        )
    except asyncio.CancelledError:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.FAILED,
            attempt_count=0,
            success_count=0,
            reason_code="prepare_cancelled",
        )
    except BaseException:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.FAILED,
            attempt_count=0,
            success_count=0,
            reason_code="prepare_failed",
        )
    if type(prepared) is not dict:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.FAILED,
            attempt_count=0,
            success_count=0,
            reason_code="prepared_shape_invalid",
        )
    images = prepared.get("images")
    if type(images) is not list or len(images) > 1:
        await _cleanup_prepared_meme(
            send_method,
            event,
            prepared,
            timeout_seconds=timeout,
        )
        return authority._complete(
            permit,
            status=MemeExecutionStatus.FAILED,
            attempt_count=0,
            success_count=0,
            reason_code="prepared_shape_invalid",
        )
    if not images:
        await _cleanup_prepared_meme(
            send_method,
            event,
            prepared,
            timeout_seconds=timeout,
        )
        return authority._complete(
            permit,
            status=MemeExecutionStatus.SUPPRESSED,
            attempt_count=0,
            success_count=0,
            reason_code="no_matching_image",
        )
    try:
        authority._validate_generation(generation, planned_action)
    except ContractViolation:
        await _cleanup_prepared_meme(
            send_method,
            event,
            prepared,
            timeout_seconds=timeout,
        )
        return authority._complete(
            permit,
            status=MemeExecutionStatus.STALE_BEFORE_SEND,
            attempt_count=0,
            success_count=0,
            reason_code="generation_stale_before_send",
        )
    try:
        result = await asyncio.wait_for(
            send_method(
                event,
                prepared,
                send_text=False,
                send_images=True,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.TIMED_OUT,
            attempt_count=1,
            success_count=0,
            reason_code="send_timeout",
        )
    except asyncio.CancelledError:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.FAILED,
            attempt_count=1,
            success_count=0,
            reason_code="send_cancelled",
        )
    except BaseException:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.FAILED,
            attempt_count=1,
            success_count=0,
            reason_code="send_failed",
        )
    if (
        type(result) is not dict
        or type(result.get("sent_text")) is not bool
        or result.get("sent_text") is not False
        or type(result.get("sent_images_count")) is not int
        or result.get("sent_images_count") not in {0, 1}
    ):
        return authority._complete(
            permit,
            status=MemeExecutionStatus.FAILED,
            attempt_count=1,
            success_count=0,
            reason_code="send_result_invalid",
        )
    sent = result["sent_images_count"]
    if sent != 1:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.FAILED,
            attempt_count=1,
            success_count=0,
            reason_code="image_not_sent",
        )
    try:
        authority._validate_generation(generation, planned_action)
    except ContractViolation:
        return authority._complete(
            permit,
            status=MemeExecutionStatus.STALE_AFTER_SEND,
            attempt_count=1,
            success_count=1,
            reason_code="generation_stale_after_send",
        )
    return authority._complete(
        permit,
        status=MemeExecutionStatus.SUCCEEDED,
        attempt_count=1,
        success_count=1,
        reason_code="image_sent",
    )


__all__ = [
    "MemeCategory",
    "MemeComplementCadence",
    "MemeComplementDecision",
    "ExpressionIntentAuthority",
    "MemeExecutionReceipt",
    "MemeExecutionAuthority",
    "MemeExecutionKind",
    "MemeExecutionLease",
    "MemeExecutionPermit",
    "MemeExecutionStatus",
    "MemeManagerConformanceCollector",
    "MemeManagerConformanceResult",
    "MemeManagerConformanceStatus",
    "MemeManagerRuntimeEvidence",
    "MemeManagerRuntimeProfile",
    "decide_text_meme_complement",
    "execute_meme_permit",
    "production_meme_manager_runtime_profile",
    "select_meme_category",
]
