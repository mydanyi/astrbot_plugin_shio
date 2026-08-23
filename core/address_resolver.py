from __future__ import annotations

import hashlib
import re
import threading
import weakref
from dataclasses import dataclass, field
from typing import Iterable

from .accepted_turn_authority import AcceptedTurnContext
from .contracts import (
    AddressDecision,
    AddressEvidence,
    AddressKind,
    ContractViolation,
    DecisionBinding,
    IngressDecision,
    IngressDisposition,
    SenderKind,
)
from .contracts._validation import require_text
from .conversation_event import ConversationEvent, PluginSource
from .group_scene import GroupSceneSnapshot
from .identity import build_sender_key
from .name_wake_filter import IngressWakeCandidate


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_OPEN_GROUP_RE = re.compile(
    r"(?:^|[，,。；;！？!?\s])(?:大家(?!庭)|各位|你们|群友们|有没有人|有人知道|谁知道|谁能)"
)
_QUESTION_OR_REQUEST_RE = re.compile(
    r"[？?]|(?:你|您).{0,10}(?:觉得|看看|知道|记得|能|可以|会不会)|"
    r"(?:怎么|如何|为啥|为什么|是不是|有没有|能不能|可不可以|帮我|"
    r"告诉我|回答|解释|说说|看看|过来|听我说)"
)
_DIRECT_AFTER_ALIAS_RE = re.compile(
    r"^[\s，,、：:；;！？!?。.…~～]*(?:你|您|帮我|请|能不能|能否|可以|"
    r"可不可以|看看|告诉我|回答|解释|说说|过来|听我说)"
)
_META_PREFIX_RE = re.compile(
    r"(?:我(?:很|也|一直)?(?:觉得|认为|喜欢|讨厌)|大家(?:刚刚)?(?:讨论|提到)|"
    r"刚刚.*?(?:提到|说起)|关于|讨论|提到|听说|扮演|像)\s*$"
)
_META_SUFFIX_RE = re.compile(
    r"^(?:这个)?(?:称呼|名字|角色|设定|原作|剧情|立绘|配音|发型|壁纸|"
    r"作品|游戏|机器人)|^(?:的|这个)(?:称呼|名字|角色|设定|原作|剧情|"
    r"立绘|配音|发型|壁纸)|^(?:今天|刚才)?(?:看起来|显得)"
)
_EMOTIONAL_PREFIX_RE = re.compile(
    r"^(?:喂|哎|欸|诶|哼|笨蛋|小笨蛋|傻瓜|坏蛋|臭家伙|讨厌鬼|"
    r"可恶(?:的)?|讨厌(?:的)?|亲爱(?:的)?|可爱(?:的)?)$"
)
_TITLE_BEFORE_RE = re.compile(
    r"(?:买了|玩了|看了|通关了|下载了|推荐).{0,8}$"
)
_TITLE_AFTER_RE = re.compile(
    r"^.{0,5}(?:游戏|作品|动画|视觉小说|原作)"
)
_VOCATIVE_PUNCTUATION = "，,、：:；;！？!?。.…~～"
_TRIM_PUNCTUATION = _VOCATIVE_PUNCTUATION + " \t\r\n"
_TAIL_PARTICLES = "啊呀呢嘛吗吧哦哟啦呐呗诶唉欸"
_ADDRESS_AUTHORITY_SEAL = object()
_DEFAULT_MAX_ADDRESS_DECISIONS = 256


def _mask(pattern: re.Pattern[str], value: str) -> str:
    return pattern.sub(lambda match: " " * len(match.group(0)), value)


def _visible_text(value: str) -> str:
    masked = _mask(_FENCED_CODE_RE, value)
    masked = _mask(_INLINE_CODE_RE, masked)
    return _mask(_URL_RE, masked)


def _content_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    if alias.isascii() and any(character.isalnum() for character in alias):
        return re.compile(
            rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
    return re.compile(escaped, re.IGNORECASE)


def _inside_title(value: str, start: int, end: int) -> bool:
    pairs = (("《", "》"), ("〈", "〉"), ("「", "」"), ("『", "』"))
    for opener, closer in pairs:
        open_index = value.rfind(opener, 0, start + 1)
        close_before = value.rfind(closer, 0, start + 1)
        close_after = value.find(closer, end)
        if open_index > close_before and close_after >= end:
            return True
    before = value[max(0, start - 14) : start]
    after = value[end : end + 12]
    return bool(_TITLE_BEFORE_RE.search(before) or _TITLE_AFTER_RE.search(after))


def _aliases(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        values = (str(values),)
    try:
        normalized = tuple(
            dict.fromkeys(
                require_text(value, "address_alias")
                for value in values
                if str(value or "").strip()
            )
        )
    except TypeError as exc:
        raise ContractViolation("address_aliases_invalid") from exc
    return tuple(sorted(normalized, key=lambda value: (-len(value), value.casefold())))


def _alias_matches(
    visible_text: str,
    aliases: tuple[str, ...],
) -> tuple[tuple[str, int, int], ...]:
    matches: list[tuple[str, int, int]] = []
    occupied: set[tuple[int, int]] = set()
    for alias in aliases:
        for match in _alias_pattern(alias).finditer(visible_text):
            span = match.span()
            if span in occupied or _inside_title(visible_text, *span):
                continue
            occupied.add(span)
            matches.append((alias, span[0], span[1]))
    return tuple(sorted(matches, key=lambda value: (value[1], value[2])))


def _is_meta_reference(value: str, start: int, end: int) -> bool:
    before = value[max(0, start - 24) : start].strip(_TRIM_PUNCTUATION)
    after = value[end : end + 24].strip()
    return bool(_META_PREFIX_RE.search(before) or _META_SUFFIX_RE.search(after))


def _is_direct_vocative(
    value: str,
    *,
    alias: str,
    start: int,
    end: int,
    candidate: IngressWakeCandidate | None,
) -> bool:
    before = value[:start]
    after = value[end:]
    before_core = before.strip(_TRIM_PUNCTUATION)
    after_core = after.strip(_TRIM_PUNCTUATION + _TAIL_PARTICLES)
    after_left = after.lstrip()
    punctuation_after = bool(
        after_left and after_left[0] in _VOCATIVE_PUNCTUATION
    )
    meta_reference = _is_meta_reference(value, start, end)

    if value.strip().casefold() == alias.casefold():
        return True
    if _EMOTIONAL_PREFIX_RE.fullmatch(before_core):
        if punctuation_after or _DIRECT_AFTER_ALIAS_RE.search(after) or not after_core:
            return True
    if _DIRECT_AFTER_ALIAS_RE.search(after):
        return True
    if not before_core:
        if punctuation_after:
            return True
        if meta_reference:
            return False
        return True
    if not after_core and _QUESTION_OR_REQUEST_RE.search(before):
        return True
    adjacent_punctuation = (
        punctuation_after
        or bool(before.rstrip() and before.rstrip()[-1] in _VOCATIVE_PUNCTUATION)
    )
    if adjacent_punctuation and _QUESTION_OR_REQUEST_RE.search(value):
        return True
    if (
        candidate is not None
        and candidate.should_observe
        and candidate.natural_direct
        and candidate.alias.casefold() == alias.casefold()
        and not meta_reference
    ):
        return True
    return False


@dataclass(frozen=True, slots=True)
class StructuredMentionEvidence:
    source_message_id: str = field(repr=False)
    target_sender_id: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_message_id",
            require_text(self.source_message_id, "mention_source_message_id"),
        )
        object.__setattr__(
            self,
            "target_sender_id",
            require_text(self.target_sender_id, "mention_target_sender_id"),
        )

    def trace_metadata(self) -> dict[str, bool]:
        return {"structured_mention_present": True}


@dataclass(frozen=True, slots=True)
class StructuredReplyEvidence:
    source_message_id: str = field(repr=False)
    referenced_message_id: str = field(repr=False)
    referenced_sender_id: str = field(repr=False)
    quoted_content: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "source_message_id",
            "referenced_message_id",
            "referenced_sender_id",
        ):
            object.__setattr__(
                self,
                field_name,
                require_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.quoted_content, str):
            raise ContractViolation("quoted_content_invalid")

    def trace_metadata(self) -> dict[str, bool]:
        return {
            "structured_reply_present": True,
            "structured_reply_has_quoted_content": bool(self.quoted_content),
        }


def _binding_snapshot(binding: DecisionBinding) -> tuple[object, ...]:
    if type(binding) is not DecisionBinding:
        raise ContractViolation("resolved_address_binding_invalid")
    try:
        values = (
            binding.scope_key,
            binding.session_id,
            binding.current_message_id,
            binding.current_sender_key,
            binding.current_content_digest,
            binding.conversation_revision,
            binding.generation_epoch,
            binding.trace_id,
        )
    except AttributeError as exc:
        raise ContractViolation("resolved_address_binding_invalid") from exc
    if (
        any(type(value) is not str for value in (*values[:5], values[7]))
        or type(values[5]) is not int
        or type(values[6]) is not int
    ):
        raise ContractViolation("resolved_address_binding_invalid")
    return values


def _address_snapshot(decision: AddressDecision) -> tuple[object, ...]:
    if type(decision) is not AddressDecision:
        raise ContractViolation("resolved_address_required")
    try:
        binding = decision.binding
        evidence = decision.evidence
        reason_codes = decision.reason_codes
        values = (
            decision.kind,
            evidence,
            decision.confidence,
            decision.is_meta_discussion,
            decision.referenced_message_id,
            decision.referenced_sender_key,
            reason_codes,
        )
    except AttributeError as exc:
        raise ContractViolation("resolved_address_corrupt") from exc
    if (
        type(values[0]) is not AddressKind
        or type(evidence) is not tuple
        or any(type(value) is not AddressEvidence for value in evidence)
        or type(values[2]) is not float
        or type(values[3]) is not bool
        or type(values[4]) is not str
        or type(values[5]) is not str
        or type(reason_codes) is not tuple
        or any(type(value) is not str for value in reason_codes)
    ):
        raise ContractViolation("resolved_address_corrupt")
    return (binding, *_binding_snapshot(binding), *values)


def _validate_accepted_context(
    context: AcceptedTurnContext,
    event: ConversationEvent,
) -> None:
    if type(context) is not AcceptedTurnContext:
        raise ContractViolation("resolved_address_context_required")
    try:
        context_event = context.conversation_event
        binding = context.binding
        envelope = context.envelope
        principal = context.principal
        sender_kind = context.sender_kind
        plugin_source = context.plugin_source
        content_digest = context.content_digest
    except AttributeError as exc:
        raise ContractViolation("resolved_address_context_corrupt") from exc
    if (
        type(event) is not ConversationEvent
        or context_event is not event
        or event.binding is not binding
        or event.envelope is not envelope
        or event.principal is not principal
        or event.sender_kind is not sender_kind
        or event.plugin_source is not plugin_source
        or event.content_digest != content_digest
        or sender_kind is not SenderKind.HUMAN
        or plugin_source is not PluginSource.NONE
    ):
        raise ContractViolation("resolved_address_context_mismatch")
    _binding_snapshot(binding)


@dataclass(frozen=True, slots=True)
class _ResolvedAddressRecord:
    context: AcceptedTurnContext
    binding: DecisionBinding
    snapshot: tuple[object, ...]


@dataclass(slots=True)
class _AddressAuthorityState:
    max_decisions: int
    records: weakref.WeakKeyDictionary[AddressDecision, _ResolvedAddressRecord]


def _build_address_authority_vault():
    lock = threading.RLock()
    authorities: weakref.WeakKeyDictionary[
        AddressResolutionAuthority,
        _AddressAuthorityState,
    ] = weakref.WeakKeyDictionary()

    def state_for(authority: AddressResolutionAuthority) -> _AddressAuthorityState:
        if type(authority) is not AddressResolutionAuthority:
            raise ContractViolation("address_resolution_authority_required")
        state = authorities.get(authority)
        if (
            state is None
            or getattr(authority, "_seal", None) is not _ADDRESS_AUTHORITY_SEAL
            or type(state.max_decisions) is not int
        ):
            raise ContractViolation("address_resolution_authority_not_canonical")
        return state

    def register(authority: AddressResolutionAuthority, max_decisions: int) -> None:
        with lock:
            if authority in authorities:
                raise ContractViolation("address_resolution_authority_replayed")
            authorities[authority] = _AddressAuthorityState(
                max_decisions=max_decisions,
                records=weakref.WeakKeyDictionary(),
            )

    def publish(
        authority: AddressResolutionAuthority,
        context: AcceptedTurnContext,
        event: ConversationEvent,
        decision: AddressDecision,
    ) -> AddressDecision:
        _validate_accepted_context(context, event)
        snapshot = _address_snapshot(decision)
        if decision.binding is not context.binding:
            raise ContractViolation("resolved_address_binding_mismatch")
        with lock:
            state = state_for(authority)
            if len(state.records) >= state.max_decisions:
                raise ContractViolation("resolved_address_ledger_full")
            state.records[decision] = _ResolvedAddressRecord(
                context=context,
                binding=context.binding,
                snapshot=snapshot,
            )
            return decision

    def inspect(
        authority: AddressResolutionAuthority,
        decision: AddressDecision,
        *,
        context: AcceptedTurnContext,
        binding: DecisionBinding,
    ) -> AddressDecision:
        if type(decision) is not AddressDecision:
            raise ContractViolation("resolved_address_required")
        with lock:
            state = state_for(authority)
            record = state.records.get(decision)
            if record is None:
                raise ContractViolation("resolved_address_not_canonical")
            try:
                current_snapshot = _address_snapshot(decision)
            except ContractViolation as exc:
                raise ContractViolation("resolved_address_corrupt") from exc
            if (
                type(context) is not AcceptedTurnContext
                or context is not record.context
                or type(binding) is not DecisionBinding
                or binding is not record.binding
                or decision.binding is not record.binding
                or current_snapshot != record.snapshot
            ):
                raise ContractViolation("resolved_address_corrupt")
            return decision

    def metrics(authority: AddressResolutionAuthority) -> dict[str, int | bool]:
        with lock:
            state = state_for(authority)
            count = len(state.records)
            return {
                "schema_version": 1,
                "resolved_address_count": count,
                "resolved_address_bounded": count <= state.max_decisions,
            }

    return register, publish, inspect, metrics


(
    _register_address_authority,
    _publish_resolved_address,
    _inspect_resolved_address,
    _address_authority_metrics,
) = _build_address_authority_vault()


class AddressResolutionAuthority:
    """Runtime-local issuer for exact resolver-produced address decisions."""

    __slots__ = ("_seal", "__weakref__")

    def __init__(self, *, max_decisions: int = _DEFAULT_MAX_ADDRESS_DECISIONS) -> None:
        if type(max_decisions) is not int or max_decisions < 1:
            raise ContractViolation("resolved_address_ledger_limit_invalid")
        self._seal = _ADDRESS_AUTHORITY_SEAL
        _register_address_authority(self, max_decisions)

    def resolve_private(
        self,
        context: AcceptedTurnContext,
        event: ConversationEvent,
        ingress: IngressDecision,
    ) -> AddressDecision:
        decision = resolve_private_address(event, ingress)
        return _publish_resolved_address(self, context, event, decision)

    def resolve_group(
        self,
        context: AcceptedTurnContext,
        event: ConversationEvent,
        *,
        ingress: IngressDecision,
        message_text: str,
        aliases: Iterable[str],
        structured_mentions: Iterable[StructuredMentionEvidence] = (),
        structured_reply: StructuredReplyEvidence | None = None,
        name_wake_candidate: IngressWakeCandidate | None = None,
        group_scene: GroupSceneSnapshot | None = None,
    ) -> AddressDecision:
        decision = resolve_address(
            event,
            ingress=ingress,
            message_text=message_text,
            aliases=aliases,
            structured_mentions=structured_mentions,
            structured_reply=structured_reply,
            name_wake_candidate=name_wake_candidate,
            group_scene=group_scene,
        )
        return _publish_resolved_address(self, context, event, decision)

    def inspect(
        self,
        decision: AddressDecision,
        *,
        context: AcceptedTurnContext,
        binding: DecisionBinding,
    ) -> AddressDecision:
        return _inspect_resolved_address(
            self,
            decision,
            context=context,
            binding=binding,
        )

    def trace_metadata(self) -> dict[str, int | bool]:
        return _address_authority_metrics(self)


def _decision(
    event: ConversationEvent,
    *,
    kind: AddressKind,
    evidence: tuple[AddressEvidence, ...],
    confidence: float,
    is_meta_discussion: bool = False,
    referenced_message_id: str = "",
    referenced_sender_id: str = "",
    reason_code: str,
) -> AddressDecision:
    return AddressDecision(
        binding=event.binding,
        kind=kind,
        evidence=evidence,
        confidence=confidence,
        is_meta_discussion=is_meta_discussion,
        referenced_message_id=referenced_message_id,
        referenced_sender_key=(
            build_sender_key(event.envelope.scope_key, referenced_sender_id)
            if referenced_sender_id
            else ""
        ),
        reason_codes=(reason_code,),
    )


def resolve_private_address(
    event: ConversationEvent,
    ingress: IngressDecision,
) -> AddressDecision:
    """Bind an accepted human private turn directly to the current bot.

    A private channel is structural address evidence.  No visible content,
    display name, self-claim, history, or Persona input is accepted here.
    """

    if not isinstance(event, ConversationEvent):
        raise ContractViolation("address_conversation_event_required")
    if not isinstance(ingress, IngressDecision) or ingress.binding != event.binding:
        raise ContractViolation("address_binding_mismatch")
    if event.envelope.chat_type != "private":
        raise ContractViolation("address_private_event_required")
    if (
        ingress.disposition is not IngressDisposition.ACCEPT_HUMAN
        or not ingress.allows_state_mutation
        or ingress.sender_kind is not SenderKind.HUMAN
        or event.sender_kind is not SenderKind.HUMAN
        or event.plugin_source is not PluginSource.NONE
    ):
        raise ContractViolation("address_ingress_not_accepted")
    return _decision(
        event,
        kind=AddressKind.DIRECT_SELF,
        evidence=(AddressEvidence.PRIVATE_CHANNEL,),
        confidence=1.0,
        reason_code="private_channel_direct",
    )


def resolve_address(
    event: ConversationEvent,
    *,
    ingress: IngressDecision,
    message_text: str,
    aliases: Iterable[str],
    structured_mentions: Iterable[StructuredMentionEvidence] = (),
    structured_reply: StructuredReplyEvidence | None = None,
    name_wake_candidate: IngressWakeCandidate | None = None,
    group_scene: GroupSceneSnapshot | None = None,
) -> AddressDecision:
    """Resolve who the accepted current human message addresses, not whether to reply."""

    if not isinstance(event, ConversationEvent):
        raise ContractViolation("address_conversation_event_required")
    if not isinstance(ingress, IngressDecision) or ingress.binding != event.binding:
        raise ContractViolation("address_binding_mismatch")
    if (
        ingress.disposition is not IngressDisposition.ACCEPT_HUMAN
        or not ingress.allows_state_mutation
        or ingress.sender_kind is not SenderKind.HUMAN
        or event.sender_kind is not SenderKind.HUMAN
        or event.plugin_source is not PluginSource.NONE
    ):
        raise ContractViolation("address_ingress_not_accepted")
    if not isinstance(message_text, str) or _content_digest(message_text) != event.content_digest:
        raise ContractViolation("address_content_binding_mismatch")
    if event.envelope.chat_type != "group":
        raise ContractViolation("address_group_event_required")
    if name_wake_candidate is not None and not isinstance(
        name_wake_candidate,
        IngressWakeCandidate,
    ):
        raise ContractViolation("address_name_wake_candidate_invalid")
    if group_scene is not None:
        if not isinstance(group_scene, GroupSceneSnapshot) or (
            group_scene.scope_key != event.envelope.scope_key
            or group_scene.conversation_revision
            != event.binding.conversation_revision
            or group_scene.participant(event.envelope.sender_key) is None
        ):
            raise ContractViolation("address_scene_binding_mismatch")

    if isinstance(structured_mentions, (str, bytes)):
        raise ContractViolation("structured_mentions_invalid")
    try:
        mentions = tuple(structured_mentions)
    except TypeError as exc:
        raise ContractViolation("structured_mentions_invalid") from exc
    if any(not isinstance(value, StructuredMentionEvidence) for value in mentions):
        raise ContractViolation("structured_mention_invalid")
    if any(
        value.source_message_id != event.envelope.message_id
        for value in mentions
    ):
        raise ContractViolation("structured_mention_binding_mismatch")

    if structured_reply is not None:
        if not isinstance(structured_reply, StructuredReplyEvidence):
            raise ContractViolation("structured_reply_invalid")
        if (
            structured_reply.source_message_id != event.envelope.message_id
            or structured_reply.referenced_message_id
            != event.envelope.reply_to_message_id
            or structured_reply.referenced_sender_id
            != event.envelope.reply_to_sender_id
        ):
            raise ContractViolation("structured_reply_binding_mismatch")
        reply_message_id = structured_reply.referenced_message_id
        reply_sender_id = structured_reply.referenced_sender_id
    else:
        reply_message_id = event.envelope.reply_to_message_id
        reply_sender_id = event.envelope.reply_to_sender_id

    self_id = event.envelope.bot_id
    if name_wake_candidate is not None and name_wake_candidate.was_native_wake:
        return _decision(
            event,
            kind=AddressKind.DIRECT_SELF,
            evidence=(AddressEvidence.NATIVE_WAKE,),
            confidence=1.0,
            reason_code="astrbot_native_wake",
        )
    if any(value.target_sender_id == self_id for value in mentions):
        return _decision(
            event,
            kind=AddressKind.DIRECT_SELF,
            evidence=(AddressEvidence.STRUCTURED_MENTION,),
            confidence=1.0,
            reason_code="structured_self_mention",
        )
    if reply_message_id and reply_sender_id == self_id:
        return _decision(
            event,
            kind=AddressKind.DIRECT_SELF,
            evidence=(AddressEvidence.REPLY_TO_SELF,),
            confidence=0.99,
            referenced_message_id=reply_message_id,
            referenced_sender_id=reply_sender_id,
            reason_code="reply_to_self",
        )

    visible = _visible_text(message_text)
    normalized_aliases = _aliases(aliases)
    matches = _alias_matches(visible, normalized_aliases)
    for alias, start, end in matches:
        if _is_direct_vocative(
            visible,
            alias=alias,
            start=start,
            end=end,
            candidate=name_wake_candidate,
        ):
            return _decision(
                event,
                kind=AddressKind.DIRECT_SELF,
                evidence=(AddressEvidence.VOCATIVE_ALIAS,),
                confidence=(
                    0.97
                    if name_wake_candidate is not None
                    and name_wake_candidate.natural_direct
                    else 0.94
                ),
                reason_code="vocative_alias",
            )

    other_mention = next(
        (
            value
            for value in mentions
            if value.target_sender_id != self_id
        ),
        None,
    )
    if other_mention is not None:
        return _decision(
            event,
            kind=AddressKind.OTHER_PERSON,
            evidence=(AddressEvidence.OTHER_PERSON_TARGET,),
            confidence=0.98,
            reason_code="structured_other_target",
        )
    if reply_message_id and reply_sender_id and reply_sender_id != self_id:
        return _decision(
            event,
            kind=AddressKind.OTHER_PERSON,
            evidence=(AddressEvidence.OTHER_PERSON_TARGET,),
            confidence=0.97,
            referenced_message_id=reply_message_id,
            referenced_sender_id=reply_sender_id,
            reason_code="reply_to_other",
        )

    if matches:
        return _decision(
            event,
            kind=AddressKind.ABOUT_SELF,
            evidence=(AddressEvidence.THIRD_PERSON_REFERENCE,),
            confidence=0.84,
            is_meta_discussion=True,
            reason_code="self_reference_discussion",
        )
    if _OPEN_GROUP_RE.search(visible) or (
        _QUESTION_OR_REQUEST_RE.search(visible)
        and not mentions
        and not reply_message_id
    ):
        return _decision(
            event,
            kind=AddressKind.OPEN_GROUP,
            evidence=(AddressEvidence.OPEN_GROUP_MARKER,),
            confidence=0.72,
            reason_code="open_group_utterance",
        )
    return _decision(
        event,
        kind=AddressKind.UNCERTAIN,
        evidence=(AddressEvidence.NONE,),
        confidence=0.35,
        reason_code="insufficient_address_evidence",
    )


__all__ = [
    "AddressResolutionAuthority",
    "StructuredMentionEvidence",
    "StructuredReplyEvidence",
    "resolve_address",
    "resolve_private_address",
]
