from __future__ import annotations

import json
import math
import os
import random
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


_QUESTION_RE = re.compile(
    r"[？?]|(?:怎么|如何|为啥|为什么|是不是|有没有|能不能|该不该|什么|谁|哪里|哪种|多少)"
)
_OPINION_RE = re.compile(r"(?:你觉得|怎么看|你们觉得|有谁知道|求推荐|帮忙看看|懂不懂|对不对)")
_BOT_RELEVANT_RE = re.compile(r"(?:机器人|bot|ai|人工智能|亚托莉|atri|星汐)", re.I)
_LIGHT_REACTION_RE = re.compile(
    r"^(?:哈+|呵+|草+|笑死|确实|好耶|好家伙|牛+|6+|哦+|嗯+|啊+|[\W_]+)$",
    re.I,
)
_NEGATIVE_RE = re.compile(
    r"(?:别插嘴|不要插嘴|闭嘴|没问你|不是问你|答非所问|认错人|又把.+当成|别说了|吵死了)"
)
_POSITIVE_RE = re.compile(
    r"(?:哈哈|笑死|好可爱|可爱|不错|说得对|对对对|有道理|可以的|好耶|太懂了|真棒)"
)


@dataclass(slots=True)
class AmbientMessage:
    scope_key: str
    sequence: int
    platform_id: str
    bot_id: str
    group_id: str
    unified_msg_origin: str
    sender_id: str
    sender_name: str
    text: str
    created_at: float
    is_owner: bool = False
    is_direct_wake: bool = False

    def target_payload(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "sequence": self.sequence,
            "platform_id": self.platform_id,
            "bot_id": self.bot_id,
            "group_id": self.group_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "text": self.text,
            "unified_msg_origin": self.unified_msg_origin,
            "is_owner": self.is_owner,
        }


@dataclass(slots=True)
class ParticipationDecision:
    action: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    target: AmbientMessage | None = None


@dataclass(slots=True)
class InteractionProfile:
    interaction_count: int = 0
    positive_feedback: int = 0
    negative_feedback: int = 0
    last_interaction_at: float = 0.0

    @property
    def affinity(self) -> float:
        evidence = self.positive_feedback + self.negative_feedback
        if evidence <= 0:
            return 0.0
        return max(
            -1.0,
            min(1.0, (self.positive_feedback - self.negative_feedback) / evidence),
        )


@dataclass(slots=True)
class ReplyObservation:
    scope_key: str
    target_identity_key: str
    reply_text: str
    expression_ids: list[str]
    created_at: float
    expires_at: float
    feedback_seen_from: set[str] = field(default_factory=set)


@dataclass
class GroupState:
    scope_key: str
    platform_id: str
    bot_id: str
    group_id: str
    unified_msg_origin: str
    messages: deque[AmbientMessage]
    sequence: int = 0
    last_decided_sequence: int = 0
    last_replied_sequence: int = 0
    first_activity_at: float = 0.0
    last_activity_at: float = 0.0
    last_bot_reply_at: float = 0.0
    last_quiet_topic_at: float = 0.0
    last_quiet_topic_attempt_at: float = 0.0
    reply_timestamps: deque[float] = field(default_factory=deque)
    quiet_topic_dates: list[str] = field(default_factory=list)


class ConversationRuntime:
    """低成本的群聊参与门控、身份画像与回复效果观察。

    原始群聊文本仅保存在内存的有限队列中；磁盘只保存聚合计数与表达反馈。
    """

    def __init__(
        self,
        data_dir: Path,
        logger: Any,
        *,
        max_messages_per_group: int = 80,
        random_fn: Callable[[], float] | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.logger = logger
        self.max_messages_per_group = max(20, int(max_messages_per_group))
        self.random_fn = random_fn or random.random
        self.now_fn = now_fn or time.time
        self.groups: dict[str, GroupState] = {}
        self.profiles: dict[str, InteractionProfile] = {}
        self.expression_feedback: dict[str, float] = {}
        self.quiet_topic_history: dict[str, list[str]] = {}
        self.observations: dict[str, ReplyObservation] = {}
        self.state_path = data_dir / "social_state.json"
        self._dirty = False
        self._load_state()

    @staticmethod
    def group_scope(
        platform_id: str,
        bot_id: str,
        group_id: str,
    ) -> str:
        return (
            f"platform:{platform_id or 'unknown'}|bot:{bot_id or 'unknown'}|"
            f"group:{group_id or 'unknown'}"
        )

    @staticmethod
    def identity_key(scope_key: str, sender_id: str) -> str:
        return f"{scope_key}|user:{sender_id or 'unknown'}"

    def _group_state(
        self,
        scope_key: str,
        platform_id: str,
        bot_id: str,
        group_id: str,
        unified_msg_origin: str,
    ) -> GroupState:
        state = self.groups.get(scope_key)
        if state is None:
            state = GroupState(
                scope_key=scope_key,
                platform_id=platform_id,
                bot_id=bot_id,
                group_id=group_id,
                unified_msg_origin=unified_msg_origin,
                messages=deque(maxlen=self.max_messages_per_group),
                quiet_topic_dates=list(self.quiet_topic_history.get(scope_key, [])),
            )
            self.groups[scope_key] = state
        else:
            state.unified_msg_origin = unified_msg_origin or state.unified_msg_origin
        return state

    def ingest(
        self,
        *,
        platform_id: str,
        bot_id: str,
        group_id: str,
        unified_msg_origin: str,
        sender_id: str,
        sender_name: str,
        text: str,
        is_owner: bool,
        is_direct_wake: bool,
        observe_feedback: bool = True,
        created_at: float | None = None,
    ) -> AmbientMessage | None:
        clean_text = str(text or "").strip()
        if not group_id or not sender_id or not clean_text or sender_id == bot_id:
            return None
        now = float(created_at if created_at is not None else self.now_fn())
        scope_key = self.group_scope(platform_id, bot_id, group_id)
        state = self._group_state(
            scope_key,
            platform_id,
            bot_id,
            group_id,
            unified_msg_origin,
        )
        state.sequence += 1
        if not state.first_activity_at:
            state.first_activity_at = now
        state.last_activity_at = now
        message = AmbientMessage(
            scope_key=scope_key,
            sequence=state.sequence,
            platform_id=platform_id,
            bot_id=bot_id,
            group_id=group_id,
            unified_msg_origin=unified_msg_origin,
            sender_id=sender_id,
            sender_name=sender_name or sender_id,
            text=clean_text[:2000],
            created_at=now,
            is_owner=is_owner,
            is_direct_wake=is_direct_wake,
        )
        state.messages.append(message)
        if observe_feedback:
            self._observe_feedback(message)
        return message

    @staticmethod
    def _prune_times(values: deque[float], minimum: float) -> None:
        while values and values[0] < minimum:
            values.popleft()

    def _message_score(
        self,
        message: AmbientMessage,
        recent: list[AmbientMessage],
        persona_names: list[str],
    ) -> tuple[float, list[str]]:
        text = message.text.strip()
        score = 0.0
        reasons: list[str] = []
        if _QUESTION_RE.search(text):
            score += 2.8
            reasons.append("疑问或求助")
        if _OPINION_RE.search(text):
            score += 1.7
            reasons.append("邀请观点")
        lowered = text.lower()
        names = [name.strip().lower() for name in persona_names if name.strip()]
        if _BOT_RELEVANT_RE.search(text) or any(name in lowered for name in names):
            score += 2.4
            reasons.append("与角色相关")
        recent_users = {item.sender_id for item in recent[-8:]}
        if len(recent) >= 4 and len(recent_users) >= 2:
            score += 1.0
            reasons.append("多人话题活跃")
        if 8 <= len(text) <= 160:
            score += 0.5
        if len(text) <= 3 or _LIGHT_REACTION_RE.fullmatch(text):
            score -= 2.6
            reasons.append("只有简短反应")
        if text.startswith(("/", "!", "！", ".", "。")):
            score -= 5.0
            reasons.append("疑似指令")
        if re.fullmatch(r"https?://\S+", text, re.I):
            score -= 4.0
            reasons.append("仅链接")
        if _NEGATIVE_RE.search(text):
            score -= 6.0
            reasons.append("明确拒绝参与")
        return score, reasons

    def decide_participation(
        self,
        scope_key: str,
        expected_sequence: int,
        *,
        threshold: float,
        cooldown_seconds: float,
        max_replies_per_hour: int,
        recent_window_seconds: float,
        recent_window_messages: int,
        persona_names: list[str],
        base_reply_probability: float = 0.65,
        max_reply_probability: float = 0.95,
        always_reply_score: float = 6.2,
        now: float | None = None,
    ) -> ParticipationDecision:
        current = float(now if now is not None else self.now_fn())
        state = self.groups.get(scope_key)
        if state is None or expected_sequence != state.sequence:
            return ParticipationDecision("wait", reasons=["已有更新消息，等待最新一轮判定"])
        if state.last_decided_sequence >= expected_sequence:
            return ParticipationDecision("listen", reasons=["本轮已经判定"])

        state.last_decided_sequence = expected_sequence
        if state.last_bot_reply_at and current - state.last_bot_reply_at < cooldown_seconds:
            return ParticipationDecision("wait", reasons=["机器人刚刚说过话"])
        self._prune_times(state.reply_timestamps, current - 3600)
        if len(state.reply_timestamps) >= max(1, int(max_replies_per_hour)):
            return ParticipationDecision("wait", reasons=["已达到每小时存在感上限"])

        recent = [
            item
            for item in state.messages
            if item.sequence > state.last_replied_sequence
            and current - item.created_at <= recent_window_seconds
            and not item.is_direct_wake
        ][-max(1, int(recent_window_messages)) :]
        if not recent:
            return ParticipationDecision("listen", reasons=["没有适合主动接话的新消息"])

        # 处理器最终借用的是 expected_sequence 对应的 AstrBot 事件，身份锚点
        # 必须与这个事件一致。不能从更早消息里另挑一个高分用户，否则计划
        # 会按甲的身份生成、消息却挂在乙的事件下发送。
        target = recent[-1]
        score, reasons = self._message_score(target, recent, persona_names)
        if score < threshold:
            return ParticipationDecision("listen", score, reasons, target)

        # 高相关消息稳定参与；边缘话题保留一点“有时听着、有时接话”的人类感。
        probability = min(
            max(0.05, float(max_reply_probability)),
            max(0.05, float(base_reply_probability))
            + max(0.0, score - threshold) * 0.18,
        )
        if score < max(threshold, float(always_reply_score)) and self.random_fn() > probability:
            return ParticipationDecision(
                "listen",
                score,
                [*reasons, f"存在感抽样未命中({probability:.2f})"],
                target,
            )
        return ParticipationDecision("reply", score, reasons, target)

    def is_current_target(self, scope_key: str, target_sequence: int) -> bool:
        """确认主动回复生成期间没有出现更新消息。"""
        state = self.groups.get(str(scope_key or ""))
        return bool(
            state is not None
            and int(target_sequence or 0) > 0
            and state.sequence == int(target_sequence)
        )

    @staticmethod
    def _topic_terms(text: str) -> set[str]:
        clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "").lower())
        terms = set(re.findall(r"[a-z0-9]{2,}", clean))
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]", clean))
        terms.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
        return {term for term in terms if term}

    def is_target_relevant(
        self,
        scope_key: str,
        target_sequence: int,
        *,
        max_new_messages: int = 4,
        max_age_seconds: float = 45.0,
        now: float | None = None,
    ) -> bool:
        """允许生成期间出现少量同话题消息，而不是把任何新消息都视为过期。"""

        state = self.groups.get(str(scope_key or ""))
        if state is None or int(target_sequence or 0) <= 0:
            return False
        target = next(
            (item for item in state.messages if item.sequence == int(target_sequence)),
            None,
        )
        if target is None:
            return False
        current = float(self.now_fn() if now is None else now)
        if current - target.created_at > max(5.0, float(max_age_seconds)):
            return False
        newer = [item for item in state.messages if item.sequence > target.sequence]
        if len(newer) > max(0, int(max_new_messages)):
            return False
        if not newer:
            return True
        if any(item.is_direct_wake or _NEGATIVE_RE.search(item.text) for item in newer):
            return False
        # 一两句补充通常仍处于同一轮口语对话；消息更多时再要求有话题词重叠。
        if len(newer) <= 2:
            return True
        target_terms = self._topic_terms(target.text)
        newer_terms = set().union(*(self._topic_terms(item.text) for item in newer))
        if target_terms and target_terms.intersection(newer_terms):
            return True
        return all(len(item.text.strip()) <= 8 for item in newer)

    def record_bot_reply(
        self,
        *,
        scope_key: str,
        target_sender_id: str,
        reply_text: str,
        expression_ids: list[str] | None = None,
        target_sequence: int = 0,
        feedback_window_seconds: float = 600,
        quiet_topic: bool = False,
        ambient_participation: bool = False,
        now: float | None = None,
    ) -> None:
        current = float(now if now is not None else self.now_fn())
        state = self.groups.get(scope_key)
        if state is None:
            return
        state.last_bot_reply_at = current
        state.last_replied_sequence = max(
            state.last_replied_sequence,
            int(target_sequence or state.sequence),
        )
        if ambient_participation or quiet_topic:
            state.reply_timestamps.append(current)
        if quiet_topic:
            state.last_quiet_topic_at = current
            state.last_quiet_topic_attempt_at = current
            state.quiet_topic_dates.append(datetime.fromtimestamp(current).date().isoformat())
            self.quiet_topic_history[scope_key] = list(state.quiet_topic_dates)
        target_key = self.identity_key(scope_key, target_sender_id)
        profile = self.profiles.setdefault(target_key, InteractionProfile())
        profile.interaction_count += 1
        profile.last_interaction_at = current
        self.observations[scope_key] = ReplyObservation(
            scope_key=scope_key,
            target_identity_key=target_key,
            reply_text=str(reply_text or "")[:500],
            expression_ids=list(dict.fromkeys(expression_ids or []))[:3],
            created_at=current,
            expires_at=current + max(60, feedback_window_seconds),
        )
        self._dirty = True

    def _observe_feedback(self, message: AmbientMessage) -> None:
        observation = self.observations.get(message.scope_key)
        if observation is None or message.created_at > observation.expires_at:
            self.observations.pop(message.scope_key, None)
            return
        reviewer_key = self.identity_key(message.scope_key, message.sender_id)
        if reviewer_key in observation.feedback_seen_from:
            return
        positive = bool(_POSITIVE_RE.search(message.text))
        negative = bool(_NEGATIVE_RE.search(message.text))
        if positive == negative:
            return
        observation.feedback_seen_from.add(reviewer_key)
        target_profile = self.profiles.setdefault(
            observation.target_identity_key,
            InteractionProfile(),
        )
        delta = 1.0 if positive else -1.0
        if positive:
            target_profile.positive_feedback += 1
        else:
            target_profile.negative_feedback += 1
        for expression_id in observation.expression_ids:
            previous = float(self.expression_feedback.get(expression_id, 0.0))
            # 有界指数移动，单次群友反应不能永久支配表达排序。
            self.expression_feedback[expression_id] = max(
                -2.0,
                min(2.0, previous * 0.85 + delta * 0.35),
            )
        self._dirty = True

    def profile_summary(self, scope_key: str, sender_id: str) -> str:
        profile = self.profiles.get(self.identity_key(scope_key, sender_id))
        if profile is None or profile.interaction_count <= 0:
            return ""
        if profile.affinity >= 0.35:
            attitude = "过往互动反馈偏积极，可稍微熟络一点，但不要擅自编造关系"
        elif profile.affinity <= -0.35:
            attitude = "过往互动反馈偏消极，应降低打扰感并保持克制"
        else:
            attitude = "过往互动反馈中性，保持自然礼貌即可"
        return f"已记录互动 {profile.interaction_count} 次；{attitude}。"

    def quiet_topic_candidates(
        self,
        *,
        group_whitelist: set[str],
        idle_seconds: float,
        cooldown_seconds: float,
        bot_reply_guard_seconds: float | None = None,
        failure_backoff_seconds: float = 600.0,
        max_per_day: int,
        active_start: str,
        active_end: str,
        now: float | None = None,
    ) -> list[GroupState]:
        current = float(now if now is not None else self.now_fn())
        local = datetime.fromtimestamp(current)
        if not self._time_in_range(local.strftime("%H:%M"), active_start, active_end):
            return []
        today = local.date().isoformat()
        reply_guard = (
            float(cooldown_seconds)
            if bot_reply_guard_seconds is None
            else max(0.0, float(bot_reply_guard_seconds))
        )
        result: list[GroupState] = []
        for state in self.groups.values():
            if not group_whitelist or state.group_id not in group_whitelist:
                continue
            if not state.unified_msg_origin or not state.messages:
                continue
            if current - state.last_activity_at < idle_seconds:
                continue
            if (
                state.last_bot_reply_at
                and current - state.last_bot_reply_at < reply_guard
            ):
                continue
            if (
                state.last_quiet_topic_at
                and current - state.last_quiet_topic_at < cooldown_seconds
            ):
                continue
            if (
                state.last_quiet_topic_attempt_at
                and current - state.last_quiet_topic_attempt_at
                < max(60.0, float(failure_backoff_seconds))
            ):
                continue
            previous_dates = list(state.quiet_topic_dates)
            state.quiet_topic_dates = [item for item in state.quiet_topic_dates if item == today]
            self.quiet_topic_history[state.scope_key] = list(state.quiet_topic_dates)
            if state.quiet_topic_dates != previous_dates:
                self._dirty = True
            if len(state.quiet_topic_dates) >= max(1, int(max_per_day)):
                continue
            result.append(state)
        return result

    def active_topic_candidates(
        self,
        *,
        group_whitelist: set[str],
        minimum_lull_seconds: float,
        quiet_idle_seconds: float,
        minimum_observation_seconds: float,
        cooldown_seconds: float,
        bot_reply_guard_seconds: float,
        failure_backoff_seconds: float = 600.0,
        max_per_day: int,
        active_start: str,
        active_end: str,
        now: float | None = None,
    ) -> list[GroupState]:
        """在活跃群的自然间隙提供确定性的主动开话题机会。

        这条路径不依赖自然接话的随机概率。它要求已经观察群聊一段时间、
        当前出现短暂间隙，并继续遵守成功冷却、失败退避和每日上限。
        """

        current = float(now if now is not None else self.now_fn())
        local = datetime.fromtimestamp(current)
        if not self._time_in_range(local.strftime("%H:%M"), active_start, active_end):
            return []
        today = local.date().isoformat()
        minimum_lull = max(30.0, float(minimum_lull_seconds))
        quiet_boundary = max(minimum_lull + 1.0, float(quiet_idle_seconds))
        result: list[GroupState] = []
        for state in self.groups.values():
            if not group_whitelist or state.group_id not in group_whitelist:
                continue
            if not state.unified_msg_origin or not state.messages:
                continue
            observed_since = state.first_activity_at or state.messages[0].created_at
            if current - observed_since < max(60.0, float(minimum_observation_seconds)):
                continue
            idle_for = current - state.last_activity_at
            if idle_for < minimum_lull or idle_for >= quiet_boundary:
                continue
            if (
                state.last_bot_reply_at
                and current - state.last_bot_reply_at
                < max(0.0, float(bot_reply_guard_seconds))
            ):
                continue
            if (
                state.last_quiet_topic_at
                and current - state.last_quiet_topic_at < cooldown_seconds
            ):
                continue
            if (
                state.last_quiet_topic_attempt_at
                and current - state.last_quiet_topic_attempt_at
                < max(60.0, float(failure_backoff_seconds))
            ):
                continue
            previous_dates = list(state.quiet_topic_dates)
            state.quiet_topic_dates = [
                item for item in state.quiet_topic_dates if item == today
            ]
            self.quiet_topic_history[state.scope_key] = list(state.quiet_topic_dates)
            if state.quiet_topic_dates != previous_dates:
                self._dirty = True
            if len(state.quiet_topic_dates) >= max(1, int(max_per_day)):
                continue
            result.append(state)
        return result

    def mark_quiet_topic_attempt(
        self,
        scope_key: str,
        now: float | None = None,
    ) -> None:
        state = self.groups.get(scope_key)
        if state is not None:
            state.last_quiet_topic_attempt_at = float(
                now if now is not None else self.now_fn()
            )

    @staticmethod
    def _time_in_range(current: str, start: str, end: str) -> bool:
        try:
            current_minutes = int(current[:2]) * 60 + int(current[3:5])
            start_minutes = int(start[:2]) * 60 + int(start[3:5])
            end_minutes = int(end[:2]) * 60 + int(end[3:5])
        except (TypeError, ValueError):
            return False
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes <= end_minutes
        return current_minutes >= start_minutes or current_minutes <= end_minutes

    def recent_contexts(self, scope_key: str, limit: int = 12) -> list[dict[str, str]]:
        state = self.groups.get(scope_key)
        if state is None:
            return []
        contexts: list[dict[str, str]] = []
        for message in list(state.messages)[-max(1, int(limit)) :]:
            content = (
                f"[群ID:{message.group_id}][昵称:{message.sender_name}]"
                f"[发送者ID:{message.sender_id}] {message.text}"
            )
            contexts.append({"role": "user", "content": content})
        return contexts

    def quiet_topic_seed(self, scope_key: str) -> str:
        state = self.groups.get(scope_key)
        if state is None or not state.messages:
            return ""
        useful = [
            item.text
            for item in list(state.messages)[-12:]
            if len(item.text.strip()) >= 6 and not item.text.startswith(("/", "!"))
        ]
        return useful[-1][:240] if useful else ""

    def flush(self) -> None:
        if not self._dirty:
            return
        payload = {
            "version": 1,
            "profiles": {
                key: asdict(value)
                for key, value in self.profiles.items()
                if value.interaction_count > 0
                or value.positive_feedback > 0
                or value.negative_feedback > 0
            },
            "expression_feedback": {
                key: round(float(value), 4)
                for key, value in self.expression_feedback.items()
                if math.isfinite(float(value)) and abs(float(value)) >= 0.01
            },
            "quiet_topic_history": {
                key: list(dict.fromkeys(value))[-3:]
                for key, value in self.quiet_topic_history.items()
                if value
            },
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
            self._dirty = False
        except Exception as exc:
            self.logger.warning("[星汐/交互学习] 保存聚合状态失败：%s", exc)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            profiles = payload.get("profiles", {}) if isinstance(payload, dict) else {}
            if isinstance(profiles, dict):
                for key, value in profiles.items():
                    if not isinstance(value, dict):
                        continue
                    self.profiles[str(key)] = InteractionProfile(
                        interaction_count=max(0, int(value.get("interaction_count", 0))),
                        positive_feedback=max(0, int(value.get("positive_feedback", 0))),
                        negative_feedback=max(0, int(value.get("negative_feedback", 0))),
                        last_interaction_at=float(value.get("last_interaction_at", 0.0)),
                    )
            feedback = payload.get("expression_feedback", {}) if isinstance(payload, dict) else {}
            if isinstance(feedback, dict):
                for key, value in feedback.items():
                    score = float(value)
                    if math.isfinite(score):
                        self.expression_feedback[str(key)] = max(-2.0, min(2.0, score))
            quiet_history = (
                payload.get("quiet_topic_history", {})
                if isinstance(payload, dict)
                else {}
            )
            if isinstance(quiet_history, dict):
                for key, value in quiet_history.items():
                    if not isinstance(value, list):
                        continue
                    dates = [str(item)[:10] for item in value if str(item).strip()]
                    self.quiet_topic_history[str(key)] = list(dict.fromkeys(dates))[-3:]
        except Exception as exc:
            self.logger.warning("[星汐/交互学习] 读取聚合状态失败，已从空状态继续：%s", exc)
