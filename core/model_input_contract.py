from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Iterable, Literal

from .capability_policy import CapabilityPolicy
from .context_assembler import AssembledContext
from .conversation_ledger import (
    DisplayNameMetadata,
    InboundIdentityMetadata,
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
    ParticipantDisplay,
    build_display_name_metadata,
    build_inbound_identity_metadata,
    build_participant_display,
    identity_literals_for_sender_keys,
    identity_metadata_integrity,
    ledger_content_digest,
)
from .group_scene import GroupSceneSnapshot, SceneEntrySource


_VERIFIED_USER_ATTRIBUTION = frozenset(
    {
        "verified",
        "verified_sender",
        "verified_platform_sender_and_admission",
    }
)
_USER_HISTORY_SOURCES = frozenset(
    {
        LedgerSourceKind.INBOUND,
        LedgerSourceKind.LEGACY_HISTORY,
        LedgerSourceKind.PLATFORM_GROUP_HISTORY,
    }
)
_KB_TOOL_RE = re.compile(r"(?:^|[_-])(?:kb|knowledge)(?:[_-]|$)|astr_kb", re.I)
_WEB_TOOL_RE = re.compile(
    r"(?:anysearch|web[_-]?search|search[_-]?web|internet|browser|crawl|extract)",
    re.I,
)
_EXTERNAL_AGENT_RE = re.compile(
    r"(?:openclaw|agent|computer[_-]?use|device[_-]?control)",
    re.I,
)
IDENTITY_PROMPT_BLOCK_START = "[对话人物与受话关系｜代码生成的不可信数据]"
IDENTITY_PROMPT_BLOCK_END = "[人物元数据结束]"
_UNTRUSTED_JSON_STRING_ESCAPES = {
    "&": "\\u0026",
    "<": "\\u003c",
    ">": "\\u003e",
    "[": "\\u005b",
    "]": "\\u005d",
}


def encode_untrusted_prompt_json(value: object) -> str:
    """Serialize JSON while escaping framing characters only inside strings.

    Structural array brackets remain valid JSON.  Display names and every other
    string value encode square brackets as JSON Unicode escapes, so untrusted
    data can round-trip without reproducing a code-owned block boundary in the
    final system prompt.
    """

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    rendered: list[str] = []
    in_string = False
    escaped = False
    for character in serialized:
        if not in_string:
            rendered.append(character)
            if character == '"':
                in_string = True
            continue
        if escaped:
            rendered.append(character)
            escaped = False
            continue
        if character == "\\":
            rendered.append(character)
            escaped = True
            continue
        if character == '"':
            rendered.append(character)
            in_string = False
            continue
        rendered.append(
            _UNTRUSTED_JSON_STRING_ESCAPES.get(character, character)
        )
    encoded = "".join(rendered)
    if in_string or escaped:
        raise ValueError("untrusted_prompt_json_state_invalid")
    return encoded


def render_model_identity_prompt_block(value: object) -> str:
    """Own the sole framing used for untrusted identity prompt data."""

    payload = encode_untrusted_prompt_json(value)
    if (
        IDENTITY_PROMPT_BLOCK_START in payload
        or IDENTITY_PROMPT_BLOCK_END in payload
    ):
        raise ValueError("identity_prompt_block_boundary_collision")
    return (
        IDENTITY_PROMPT_BLOCK_START
        + "\n"
        + payload
        + "\n"
        + IDENTITY_PROMPT_BLOCK_END
    )


@dataclass(frozen=True, slots=True)
class CanonicalModelMessage:
    role: str
    content: str
    source_kind: str
    source_message_id: str = field(default="", repr=False)
    sender_key: str = field(default="", repr=False)
    platform_id: str = field(default="", repr=False)
    sender_id: str = field(default="", repr=False)
    account_key: str = field(default="", repr=False)
    conversation_scope: str = field(default="", repr=False)
    referenced_sender_id: str = field(default="", repr=False)
    mention_sender_ids: tuple[str, ...] = field(default=(), repr=False)
    timestamp: float = field(default=0.0, repr=False)
    speaker_kind: str = field(default="unknown", repr=False)
    reply_to_message_id: str = field(default="", repr=False)
    referenced_sender_key: str = field(default="", repr=False)
    identity_metadata: InboundIdentityMetadata = field(
        default_factory=InboundIdentityMetadata,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("canonical_model_role_invalid")
        if type(self.content) is not str or not self.content.strip():
            raise ValueError("canonical_model_content_invalid")
        if type(self.source_kind) is not str or not self.source_kind.strip():
            raise ValueError("canonical_model_source_invalid")
        if type(self.identity_metadata) is not InboundIdentityMetadata:
            raise ValueError("canonical_model_identity_invalid")
        if type(self.referenced_sender_id) is not str or any(
            type(value) is not str or not value
            for value in tuple(self.mention_sender_ids)
        ):
            raise ValueError("canonical_model_raw_address_identity_invalid")
        object.__setattr__(self, "mention_sender_ids", tuple(self.mention_sender_ids))
        referenced = self.identity_metadata.referenced_sender
        if referenced is not None and self.referenced_sender_key not in {
            "",
            referenced.sender_key,
        }:
            raise ValueError("canonical_model_reference_invalid")
        if referenced is not None and not self.referenced_sender_key:
            object.__setattr__(
                self,
                "referenced_sender_key",
                referenced.sender_key,
            )

    def provider_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


def canonical_model_messages_digest(
    messages: Iterable[CanonicalModelMessage],
) -> str:
    values = tuple(messages or ())
    if any(type(message) is not CanonicalModelMessage for message in values):
        raise ValueError("canonical_model_messages_invalid")
    encoded = json.dumps(
        [
            {
                "role": message.role,
                "content": message.content,
                "source_kind": message.source_kind,
                "source_message_id": message.source_message_id,
                "sender_key": message.sender_key,
                "platform_id": message.platform_id,
                "sender_id": message.sender_id,
                "account_key": message.account_key,
                "conversation_scope": message.conversation_scope,
                "referenced_sender_id": message.referenced_sender_id,
                "mention_sender_ids": list(message.mention_sender_ids),
                "timestamp": message.timestamp,
                "speaker_kind": message.speaker_kind,
                "reply_to_message_id": message.reply_to_message_id,
                "referenced_sender_key": message.referenced_sender_key,
                "identity": identity_metadata_integrity(
                    message.identity_metadata
                ),
            }
            for message in values
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def project_group_scene_model_messages(
    scene: GroupSceneSnapshot,
    *,
    assistant_display_name: str,
    source_kind: str,
    max_messages: int = 8,
    max_chars: int = 4000,
    max_message_chars: int = 600,
    exclude_message_id: str = "",
    selection_order: Literal["latest", "chronological"] = "latest",
) -> tuple[CanonicalModelMessage, ...]:
    """Project one bounded public scene through the canonical message owner.

    Speaker and addressing data remain structured metadata; provider ``content``
    is always the exact public message body.  ``exclude_message_id`` is used by
    admission-time semantic participation so the current message is carried by
    the provider prompt exactly once rather than duplicated in history.  The
    default keeps the latest bounded messages.  ``chronological`` first fixes
    the source window to the last ``max_messages`` scene entries, then applies
    the same projection and character budget in source order.
    """

    if type(scene) is not GroupSceneSnapshot:
        raise ValueError("canonical_group_scene_invalid")
    assistant_name = str(assistant_display_name or "").strip()
    normalized_source = str(source_kind or "").strip()
    excluded = str(exclude_message_id or "").strip()
    if not assistant_name:
        raise ValueError("canonical_assistant_display_invalid")
    if not normalized_source:
        raise ValueError("canonical_group_scene_source_invalid")
    if type(max_messages) is not int or not 1 <= max_messages <= 32:
        raise ValueError("canonical_group_scene_message_limit_invalid")
    if type(max_chars) is not int or not 64 <= max_chars <= 20000:
        raise ValueError("canonical_group_scene_char_limit_invalid")
    if type(max_message_chars) is not int or not 16 <= max_message_chars <= 4000:
        raise ValueError("canonical_group_scene_item_limit_invalid")
    if selection_order not in {"latest", "chronological"}:
        raise ValueError("canonical_group_scene_selection_order_invalid")

    admitted: list[CanonicalModelMessage] = []
    source_topics = (
        scene.public_topics[-max_messages:]
        if selection_order == "chronological"
        else scene.public_topics
    )
    for topic in source_topics:
        if excluded and topic.message_id == excluded:
            continue
        if topic.source is SceneEntrySource.HUMAN_INBOUND:
            role = "user"
            sender_key = topic.author_sender_key
            identity_metadata = topic.identity_metadata
        elif topic.source is SceneEntrySource.SHIO_OUTBOUND:
            role = "assistant"
            sender_key = ""
            referenced_sender = (
                build_participant_display(topic.target_sender_key)
                if topic.reply_to_message_id and topic.target_sender_key
                else None
            )
            identity_metadata = build_inbound_identity_metadata(
                display_name=assistant_name,
                display_name_source="persona_config",
                referenced_sender=referenced_sender,
                has_reply_edge=bool(topic.reply_to_message_id),
            )
        else:  # pragma: no cover - GroupSceneSnapshot rejects other values.
            continue
        content = topic.content[:max_message_chars]
        if not content.strip():
            continue
        admitted.append(
            CanonicalModelMessage(
                role=role,
                content=content,
                source_kind=normalized_source,
                source_message_id=topic.message_id,
                sender_key=sender_key,
                reply_to_message_id=topic.reply_to_message_id,
                referenced_sender_key=topic.referenced_sender_key,
                identity_metadata=identity_metadata,
            )
        )

    selected: list[CanonicalModelMessage] = []
    used_chars = 0
    ordered_messages = (
        reversed(admitted)
        if selection_order == "latest"
        else iter(admitted)
    )
    for message in ordered_messages:
        if len(selected) >= max_messages:
            break
        if used_chars + len(message.content) > max_chars:
            continue
        selected.append(message)
        used_chars += len(message.content)
    if selection_order == "latest":
        selected.reverse()
    return tuple(selected)


def _display_prompt_data(
    display: DisplayNameMetadata,
    sender_key: str = "",
    protected_identity_literals: Iterable[object] = (),
) -> dict[str, object]:
    guarded = build_display_name_metadata(
        display.value,
        source=display.source,
        identity_literals=identity_literals_for_sender_keys(
            (sender_key,),
            extra_literals=protected_identity_literals,
        ),
    )
    if display.available and guarded.available:
        guarded = display
    data: dict[str, object] = {
        "display_name_available": guarded.available,
        "display_name_status": guarded.status,
        "display_name_source": guarded.source,
    }
    if guarded.available:
        data["display_name"] = guarded.value
    return data


def project_model_identity_prompt_data(
    messages: Iterable[CanonicalModelMessage],
    *,
    current_sender_key: str = "",
    current_display: DisplayNameMetadata | None = None,
    current_message: CanonicalModelMessage | None = None,
    server_now: float | None = None,
) -> dict[str, object]:
    """Project real names and structural edges without exposing identity keys."""

    values = tuple(messages or ())
    if any(type(message) is not CanonicalModelMessage for message in values):
        raise ValueError("canonical_model_messages_invalid")
    if current_message is not None and (
        type(current_message) is not CanonicalModelMessage
        or current_message.role != "user"
    ):
        raise ValueError("canonical_current_message_invalid")
    current = (
        current_display
        if current_display is not None
        else DisplayNameMetadata()
    )
    if type(current) is not DisplayNameMetadata:
        raise ValueError("canonical_current_display_invalid")
    normalized_current_key = str(current_sender_key or "").strip()
    if current_message is not None:
        if (
            normalized_current_key
            and current_message.sender_key != normalized_current_key
        ):
            raise ValueError("canonical_current_message_sender_mismatch")
        if not normalized_current_key:
            normalized_current_key = current_message.sender_key
        current = current_message.identity_metadata.display

    protected_sender_keys: list[str] = []

    def collect_message_identity_keys(message: CanonicalModelMessage) -> None:
        protected_sender_keys.extend(
            value
            for value in (
                message.sender_key,
                message.referenced_sender_key,
            )
            if value
        )
        referenced = message.identity_metadata.referenced_sender
        if referenced is not None:
            protected_sender_keys.append(referenced.sender_key)
        protected_sender_keys.extend(
            target.sender_key
            for target in message.identity_metadata.mention_targets
        )

    if normalized_current_key:
        protected_sender_keys.append(normalized_current_key)
    for message in values:
        collect_message_identity_keys(message)
    if current_message is not None:
        collect_message_identity_keys(current_message)
    protected_identity_literals = identity_literals_for_sender_keys(
        protected_sender_keys
    )
    source_indexes: dict[str, int] = {}
    source_messages: dict[str, CanonicalModelMessage] = {}
    latest_speaker_index: dict[str, int] = {}
    latest_display: dict[str, DisplayNameMetadata] = {}
    for index, message in enumerate(values, start=1):
        if message.source_message_id:
            source_indexes[message.source_message_id] = index
            source_messages[message.source_message_id] = message
        if message.sender_key:
            latest_speaker_index[message.sender_key] = index
            if message.identity_metadata.display.available:
                latest_display[message.sender_key] = (
                    message.identity_metadata.display
                )
    if normalized_current_key and current.available:
        latest_display[normalized_current_key] = current

    def participant_data(target: ParticipantDisplay) -> dict[str, object]:
        display = target.display
        if not display.available:
            display = latest_display.get(target.sender_key, display)
        return {
            **_display_prompt_data(
                display,
                target.sender_key,
                protected_identity_literals,
            ),
            "same_as_current_sender": bool(
                normalized_current_key
                and target.sender_key == normalized_current_key
            ),
            "same_as_history_speaker_index": latest_speaker_index.get(
                target.sender_key
            ),
        }

    def addressing_data(message: CanonicalModelMessage) -> dict[str, object]:
        metadata = message.identity_metadata
        referenced = metadata.referenced_sender
        if referenced is None and message.referenced_sender_key:
            referenced = ParticipantDisplay(
                sender_key=message.referenced_sender_key
            )
        referenced_message = source_messages.get(message.reply_to_message_id)
        return {
            "kind": metadata.addressing_status,
            "reply_message_index": source_indexes.get(
                message.reply_to_message_id
            ),
            "reply_message_status": (
                "in_context"
                if message.reply_to_message_id in source_indexes
                else (
                    "outside_context"
                    if message.reply_to_message_id
                    else "not_applicable"
                )
            ),
            "referenced_speaker_kind": (
                referenced_message.speaker_kind
                if referenced_message is not None
                else "unknown"
            ),
            "reply_target_sender_id": message.referenced_sender_id,
            "referenced_sender": (
                participant_data(referenced)
                if referenced is not None
                else None
            ),
            "mention_target_sender_ids": list(message.mention_sender_ids),
            "mention_targets": [
                participant_data(target)
                for target in metadata.mention_targets
            ],
        }

    bound_server_now = (
        float(server_now)
        if type(server_now) in {int, float} and float(server_now) > 0
        else datetime.now(UTC).timestamp()
    )

    def record_data(message: CanonicalModelMessage) -> dict[str, object]:
        timestamp = max(0.0, float(message.timestamp or 0.0))
        hkt = timezone(timedelta(hours=8))
        return {
            "message_id": message.source_message_id,
            "platform_id": message.platform_id,
            "sender_id": message.sender_id,
            "account_key": message.account_key,
            "conversation_scope": message.conversation_scope,
            "speaker_kind": message.speaker_kind,
            "unix_timestamp": timestamp,
            "hkt_iso_timestamp": datetime.fromtimestamp(timestamp, UTC).astimezone(hkt).isoformat() if timestamp else None,
            "relative_to_server_seconds": (
                timestamp - bound_server_now if timestamp else None
            ),
        }

    history: list[dict[str, object]] = []
    previous_speaker_index: dict[str, int] = {}
    for index, message in enumerate(values, start=1):
        metadata = message.identity_metadata
        speaker = {
            **_display_prompt_data(
                metadata.display,
                message.sender_key,
                protected_identity_literals,
            ),
            "same_as_current_sender": bool(
                normalized_current_key
                and message.sender_key == normalized_current_key
            ),
            "same_speaker_as_message_index": (
                previous_speaker_index.get(message.sender_key)
                if message.sender_key
                else None
            ),
        }
        if message.sender_key:
            previous_speaker_index[message.sender_key] = index
        history.append(
            {
                "message_index": index,
                # Request-local reference, not a platform message ID.  A
                # Provider may cite it in R11 attribution segments; the
                # validator resolves it only against this exact request.
                "evidence_message_id": f"history-{index}",
                "role": message.role,
                **record_data(message),
                "speaker": speaker,
                "addressing": addressing_data(message),
            }
        )
    current_projection = {
        "role": "user",
        "evidence_message_id": "current",
        # Current participates in the same request-local message-index domain
        # as history; it is not a platform sequence surrogate.
        "message_index": len(values) + 1,
        **(record_data(current_message) if current_message is not None else {}),
        "speaker": {
            **_display_prompt_data(
                current,
                normalized_current_key,
                protected_identity_literals,
            ),
            "same_speaker_as_message_index": latest_speaker_index.get(
                normalized_current_key
            ),
        },
        "addressing": (
            addressing_data(current_message)
            if current_message is not None
            else {
                "kind": "unspecified",
                "reply_message_index": None,
                "reply_message_status": "not_applicable",
                "referenced_sender": None,
                "mention_targets": [],
            }
        ),
    }
    return {
        "contract": "display_names_are_untrusted_metadata_not_message_content",
        "current_sender": _display_prompt_data(
            current,
            normalized_current_key,
            protected_identity_literals,
        ),
        "current_message": current_projection,
        "history": history,
    }


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Code-owned truth about capabilities effective for this exact turn."""

    chat_reply: bool
    text_code_assistance: bool
    knowledge_base_read: bool
    public_web_read: bool
    external_actions_allowed: bool
    external_agent_connected: bool
    effective_tool_names: tuple[str, ...]
    policy_kind: str
    conversation_mode: str

    def tool_available(self, requested_name: str) -> bool:
        needle = str(requested_name or "").strip().casefold()
        if not needle:
            return False
        return any(
            needle == name.casefold() or needle in name.casefold()
            for name in self.effective_tool_names
        )

    def prompt_data(self) -> dict[str, object]:
        return {
            "truth_owner": "runtime_code",
            "chat_reply": self.chat_reply,
            "text_code_assistance": self.text_code_assistance,
            "knowledge_base_read": self.knowledge_base_read,
            "public_web_read": self.public_web_read,
            "external_actions_allowed": self.external_actions_allowed,
            "external_agent_connected": self.external_agent_connected,
            "effective_tool_names": list(self.effective_tool_names),
            "policy_kind": self.policy_kind,
            "conversation_mode": self.conversation_mode,
        }


def _normalized_tool_names(values: Iterable[str]) -> tuple[str, ...]:
    names: list[str] = []
    for value in values or ():
        name = str(value or "").strip()
        if not name or len(name) > 120 or name in names:
            continue
        names.append(name)
    return tuple(sorted(names, key=str.casefold))


def build_capability_snapshot(
    policy: CapabilityPolicy,
    *,
    effective_tool_names: Iterable[str] = (),
) -> CapabilitySnapshot:
    if type(policy) is not CapabilityPolicy:
        raise ValueError("capability_snapshot_policy_invalid")
    names = _normalized_tool_names(effective_tool_names)
    knowledge_base_read = policy.chat_retrieval and any(
        _KB_TOOL_RE.search(name) for name in names
    )
    public_web_read = policy.public_web_read and any(
        _WEB_TOOL_RE.search(name) for name in names
    )
    external_agent_connected = policy.agent_full and any(
        _EXTERNAL_AGENT_RE.search(name) for name in names
    )
    return CapabilitySnapshot(
        chat_reply=bool(policy.chat_read),
        text_code_assistance=bool(policy.chat_read),
        knowledge_base_read=bool(knowledge_base_read),
        public_web_read=bool(public_web_read),
        external_actions_allowed=bool(
            policy.artifact_write
            or policy.shell_exec
            or policy.device_control
            or policy.agent_full
        ),
        external_agent_connected=bool(external_agent_connected),
        effective_tool_names=names,
        policy_kind=policy.policy_kind,
        conversation_mode=policy.conversation_mode,
    )


def _admitted_model_message(
    assembled: AssembledContext,
    record,
    *,
    assistant_display_name: str = "",
) -> CanonicalModelMessage | None:
    if record.scope_key != assembled.reply_target.scope_key:
        return None
    content = str(record.content or "").strip()
    if not content:
        return None
    if (
        record.role is LedgerRole.USER
        and record.source_kind in _USER_HISTORY_SOURCES
        and record.attribution_status in _VERIFIED_USER_ATTRIBUTION
        and record.message_id != assembled.reply_target.message_id
    ):
        return CanonicalModelMessage(
            role="user",
            content=str(record.content or ""),
            source_kind=record.source_kind.value,
            source_message_id=(record.source_id or record.message_id),
            sender_key=record.sender_key,
            platform_id=record.platform_id,
            sender_id=record.sender_id,
            account_key=record.account_key,
            conversation_scope=record.scope_key,
            referenced_sender_id=record.referenced_sender_id,
            mention_sender_ids=record.mention_sender_ids,
            timestamp=record.timestamp,
            speaker_kind="human_peer",
            reply_to_message_id=record.reply_to_message_id,
            referenced_sender_key=record.referenced_sender_key,
            identity_metadata=record.identity_metadata,
        )
    if (
        record.role is LedgerRole.ASSISTANT
        and record.source_kind is LedgerSourceKind.OUTBOUND
        and record.attribution_status == "verified"
        and record.target_sender_key == assembled.reply_target.sender_key
    ):
        referenced_sender = (
            build_participant_display(record.target_sender_key)
            if record.reply_to_message_id and record.target_sender_key
            else None
        )
        return CanonicalModelMessage(
            role="assistant",
            content=content,
            source_kind=record.source_kind.value,
            source_message_id=(record.source_id or record.message_id),
            sender_key=record.sender_key,
            platform_id=record.platform_id,
            sender_id=record.sender_id,
            account_key=record.account_key,
            conversation_scope=record.scope_key,
            referenced_sender_id=record.referenced_sender_id,
            mention_sender_ids=record.mention_sender_ids,
            timestamp=record.timestamp,
            speaker_kind="assistant_self" if record.bot_id and record.sender_id == record.bot_id else "external_bot",
            reply_to_message_id=record.reply_to_message_id,
            referenced_sender_key=(
                record.target_sender_key if referenced_sender is not None else ""
            ),
            identity_metadata=build_inbound_identity_metadata(
                display_name=assistant_display_name,
                display_name_source="persona_config",
                referenced_sender=referenced_sender,
                has_reply_edge=bool(record.reply_to_message_id),
            ),
        )
    return None


def project_current_model_message(
    assembled_context: AssembledContext | None,
    *,
    content: str,
    sender_key: str,
    display: DisplayNameMetadata,
) -> CanonicalModelMessage:
    """Bind current identity/addressing to the same canonical message type.

    The returned message is projected only into system-owned identity metadata;
    callers keep it separate from historical Provider contexts.
    """

    message_content = str(content or "")
    normalized_sender_key = str(sender_key or "").strip()
    if not message_content.strip() or not normalized_sender_key:
        raise ValueError("canonical_current_message_binding_invalid")
    if type(display) is not DisplayNameMetadata:
        raise ValueError("canonical_current_display_invalid")

    target = None
    current_record = None
    reference = None
    if assembled_context is not None:
        if type(assembled_context) is not AssembledContext:
            raise ValueError("model_context_assembled_invalid")
        target = assembled_context.reply_target
        if (
            target.sender_key != normalized_sender_key
            or target.content_digest != ledger_content_digest(message_content)
        ):
            raise ValueError("canonical_current_message_target_mismatch")
        current_record = assembled_context.current_record
        reference = assembled_context.reference

    if current_record is not None:
        if (
            type(current_record) is not LedgerRecord
            or current_record.role is not LedgerRole.USER
            or current_record.source_kind is not LedgerSourceKind.INBOUND
            or current_record.attribution_status != "verified"
            or target is None
            or current_record.scope_key != target.scope_key
            or current_record.message_id != target.message_id
            or current_record.sender_key != normalized_sender_key
            or current_record.content_digest != target.content_digest
            or current_record.content != message_content
        ):
            raise ValueError("canonical_current_record_invalid")
        return CanonicalModelMessage(
            role="user",
            content=message_content,
            source_kind="current_inbound",
            source_message_id=current_record.source_id or current_record.message_id,
            sender_key=current_record.sender_key,
            platform_id=current_record.platform_id,
            sender_id=current_record.sender_id,
            account_key=current_record.account_key,
            conversation_scope=current_record.scope_key,
            referenced_sender_id=current_record.referenced_sender_id,
            mention_sender_ids=current_record.mention_sender_ids,
            timestamp=current_record.timestamp,
            speaker_kind="current_user",
            reply_to_message_id=current_record.reply_to_message_id,
            referenced_sender_key=current_record.referenced_sender_key,
            identity_metadata=current_record.identity_metadata,
        )

    reply_to_message_id = (
        target.referenced_message_id if target is not None else ""
    )
    referenced_sender = None
    if (
        reply_to_message_id
        and reference is not None
        and reference.message_id == reply_to_message_id
        and target is not None
        and reference.scope_key == target.scope_key
        and reference.sender_key
    ):
        referenced_sender = build_participant_display(reference.sender_key)
    identity_metadata = build_inbound_identity_metadata(
        display_name=display.value if display.available else "",
        display_name_source=display.source,
        display_name_identity_literals=(normalized_sender_key,),
        referenced_sender=referenced_sender,
        has_reply_edge=bool(reply_to_message_id),
    )
    return CanonicalModelMessage(
        role="user",
        content=message_content,
        source_kind="current_inbound",
        source_message_id=(target.message_id if target is not None else ""),
        sender_key=normalized_sender_key,
        reply_to_message_id=reply_to_message_id,
        referenced_sender_key=(
            referenced_sender.sender_key if referenced_sender is not None else ""
        ),
        identity_metadata=identity_metadata,
    )


def project_model_messages(
    assembled_context: AssembledContext | None,
    *,
    max_messages: int = 16,
    max_chars: int = 9000,
    max_message_chars: int = 1600,
    assistant_display_name: str = "",
) -> tuple[CanonicalModelMessage, ...]:
    if assembled_context is None:
        return ()
    if type(assembled_context) is not AssembledContext:
        raise ValueError("model_context_assembled_invalid")
    message_limit = max(0, min(32, int(max_messages)))
    char_limit = max(0, min(20000, int(max_chars)))
    per_message_limit = max(64, min(4000, int(max_message_chars)))
    if not message_limit or not char_limit:
        return ()

    admitted: list[CanonicalModelMessage] = []
    # The canonical owner, not ledger arrival order, owns chronological order.
    # A history item without a source timestamp is not safe evidence for
    # before/after claims and is deliberately omitted.
    ordered_records = sorted(
        (
            record
            for record in assembled_context.planner_records
            if type(record) is LedgerRecord and float(record.timestamp or 0) > 0
        ),
        key=lambda record: (
            float(record.timestamp),
            int(record.sequence),
            str(record.source_id or record.message_id),
        ),
    )
    for record in ordered_records:
        message = _admitted_model_message(
            assembled_context,
            record,
            assistant_display_name=assistant_display_name,
        )
        if message is None:
            continue
        admitted.append(
            CanonicalModelMessage(
                role=message.role,
                content=message.content[:per_message_limit],
                source_kind=message.source_kind,
                source_message_id=message.source_message_id,
                sender_key=message.sender_key,
                platform_id=message.platform_id,
                sender_id=message.sender_id,
                account_key=message.account_key,
                conversation_scope=message.conversation_scope,
                referenced_sender_id=message.referenced_sender_id,
                mention_sender_ids=message.mention_sender_ids,
                timestamp=message.timestamp,
                speaker_kind=message.speaker_kind,
                reply_to_message_id=message.reply_to_message_id,
                referenced_sender_key=message.referenced_sender_key,
                identity_metadata=message.identity_metadata,
            )
        )

    selected_reversed: list[CanonicalModelMessage] = []
    used_chars = 0
    for message in reversed(admitted):
        if len(selected_reversed) >= message_limit:
            break
        if used_chars + len(message.content) > char_limit:
            continue
        selected_reversed.append(message)
        used_chars += len(message.content)
    return tuple(reversed(selected_reversed))


def project_provider_contexts(
    assembled_context: AssembledContext | None,
    *,
    max_messages: int = 16,
    max_chars: int = 9000,
) -> list[dict[str, str]]:
    return [
        message.provider_dict()
        for message in project_model_messages(
            assembled_context,
            max_messages=max_messages,
            max_chars=max_chars,
        )
    ]


__all__ = [
    "CanonicalModelMessage",
    "CapabilitySnapshot",
    "IDENTITY_PROMPT_BLOCK_END",
    "IDENTITY_PROMPT_BLOCK_START",
    "build_capability_snapshot",
    "canonical_model_messages_digest",
    "encode_untrusted_prompt_json",
    "project_current_model_message",
    "project_group_scene_model_messages",
    "project_model_identity_prompt_data",
    "project_model_messages",
    "project_provider_contexts",
    "render_model_identity_prompt_block",
]
