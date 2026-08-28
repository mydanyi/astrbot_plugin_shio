from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .identity import TurnEnvelope, build_account_key, build_sender_key


class LedgerSourceKind(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    REFERENCE = "reference"
    TOOL_RESULT = "tool_result"
    LEGACY_HISTORY = "legacy_history"
    PLATFORM_GROUP_HISTORY = "platform_group_history"


class LedgerRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    CONTEXT = "context"
    TOOL = "tool"


def ledger_content_digest(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


_DISPLAY_NAME_MAX_CHARS = 80
_DISPLAY_NAME_SOURCES = frozenset(
    {
        "event_sender",
        "platform_history",
        "reply_component",
        "mention_component",
        "persona_config",
    }
)
_ADDRESSING_STATUSES = frozenset(
    {
        "open_group",
        "reply",
        "mentions",
        "reply_and_mentions",
        "reply_target_unavailable",
        "reply_target_unavailable_and_mentions",
        "unspecified",
    }
)


def _bounded_display_name(value: object) -> tuple[str, str]:
    if type(value) is not str:
        return "", "unavailable"
    raw = value.strip()
    if not raw:
        return "", "unavailable"
    sanitized = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in raw
    )
    sanitized = " ".join(sanitized.split())
    if not sanitized:
        return "", "unavailable"
    changed = sanitized != raw
    truncated = len(sanitized) > _DISPLAY_NAME_MAX_CHARS
    bounded = sanitized[:_DISPLAY_NAME_MAX_CHARS]
    if changed and truncated:
        status = "verified_sanitized_truncated"
    elif changed:
        status = "verified_sanitized"
    elif truncated:
        status = "verified_truncated"
    else:
        status = "verified"
    return bounded, status


def _matches_identity_literal(
    value: object,
    identity_literals: Iterable[object],
) -> bool:
    bounded, _status = _bounded_display_name(value)
    if not bounded:
        return False
    normalized = bounded.casefold()
    for candidate in tuple(identity_literals or ()):
        if type(candidate) is not str or not candidate.strip():
            continue
        literal = candidate.strip().casefold()
        if re.search(rf"(?<!\w){re.escape(literal)}(?!\w)", normalized):
            return True
    return False


def identity_literals_for_sender_keys(
    sender_keys: Iterable[object] = (),
    *,
    extra_literals: Iterable[object] = (),
) -> tuple[str, ...]:
    """Collect one request-wide set of raw identity literals.

    The canonical sender/scope keys are the only registry.  This helper merely
    projects their bot, conversation and user components for display-name
    privacy checks; it does not create another identity mapping.
    """

    values: list[str] = []
    seen: set[str] = set()

    def append(value: object) -> None:
        if type(value) is not str:
            return
        normalized = value.strip()
        folded = normalized.casefold()
        if not normalized or folded in seen:
            return
        seen.add(folded)
        values.append(normalized)

    for candidate in tuple(sender_keys or ()):
        append(candidate)
        if type(candidate) is not str:
            continue
        for segment in candidate.strip().split("|"):
            kind, separator, raw_value = segment.partition(":")
            if (
                separator
                and kind.strip().casefold() in {"bot", "group", "private", "user"}
            ):
                append(raw_value)
    for candidate in tuple(extra_literals or ()):
        append(candidate)
    return tuple(values)


def identity_literals_for_sender_key(sender_key: str) -> tuple[str, ...]:
    return identity_literals_for_sender_keys((sender_key,))


@dataclass(frozen=True, slots=True)
class DisplayNameMetadata:
    """Bounded untrusted presentation data, never a stable identity source."""

    value: str = ""
    source: str = "unavailable"
    status: str = field(init=False)

    def __post_init__(self) -> None:
        source = str(self.source or "").strip()
        if source not in _DISPLAY_NAME_SOURCES:
            object.__setattr__(self, "value", "")
            object.__setattr__(self, "source", "unavailable")
            object.__setattr__(self, "status", "unavailable")
            return
        value, status = _bounded_display_name(self.value)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "source", source if value else "unavailable")
        object.__setattr__(self, "status", status if value else "unavailable")

    @property
    def available(self) -> bool:
        return self.status.startswith("verified") and bool(self.value)


@dataclass(frozen=True, slots=True)
class ParticipantDisplay:
    """One structurally identified participant plus optional real display data."""

    sender_key: str = field(repr=False)
    display: DisplayNameMetadata = field(default_factory=DisplayNameMetadata)

    def __post_init__(self) -> None:
        sender_key = str(self.sender_key or "").strip()
        if not sender_key:
            raise ValueError("participant_display_sender_key_required")
        if type(self.display) is not DisplayNameMetadata:
            raise ValueError("participant_display_metadata_invalid")
        object.__setattr__(self, "sender_key", sender_key)


@dataclass(frozen=True, slots=True)
class InboundIdentityMetadata:
    """One inbound speaker and its source-verified addressing relations."""

    display: DisplayNameMetadata = field(default_factory=DisplayNameMetadata)
    referenced_sender: ParticipantDisplay | None = None
    mention_targets: tuple[ParticipantDisplay, ...] = ()
    addressing_status: str = "unspecified"

    def __post_init__(self) -> None:
        if type(self.display) is not DisplayNameMetadata:
            raise ValueError("inbound_display_metadata_invalid")
        if self.referenced_sender is not None and type(
            self.referenced_sender
        ) is not ParticipantDisplay:
            raise ValueError("inbound_referenced_sender_invalid")
        mentions = tuple(self.mention_targets)
        if (
            len(mentions) > 16
            or any(type(target) is not ParticipantDisplay for target in mentions)
            or len({target.sender_key for target in mentions}) != len(mentions)
        ):
            raise ValueError("inbound_mention_targets_invalid")
        status = str(self.addressing_status or "").strip()
        if status not in _ADDRESSING_STATUSES:
            raise ValueError("inbound_addressing_status_invalid")
        has_reference = self.referenced_sender is not None
        has_mentions = bool(mentions)
        if status in {"reply", "reply_and_mentions"} and not has_reference:
            raise ValueError("inbound_reply_target_required")
        if status in {
            "reply_target_unavailable",
            "reply_target_unavailable_and_mentions",
        } and has_reference:
            raise ValueError("inbound_reply_target_must_be_unavailable")
        if status in {"mentions", "reply_and_mentions", "reply_target_unavailable_and_mentions"} and not has_mentions:
            raise ValueError("inbound_mentions_required")
        if status in {"open_group", "unspecified", "reply", "reply_target_unavailable"} and has_mentions:
            raise ValueError("inbound_mentions_status_mismatch")
        if status in {"open_group", "unspecified", "mentions"} and has_reference:
            raise ValueError("inbound_reference_status_mismatch")
        object.__setattr__(self, "mention_targets", mentions)
        object.__setattr__(self, "addressing_status", status)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "display_name_status": self.display.status,
            "addressing_status": self.addressing_status,
            "has_referenced_sender": self.referenced_sender is not None,
            "mention_target_count": len(self.mention_targets),
        }


def build_display_name_metadata(
    value: object,
    *,
    source: str,
    identity_literals: Iterable[object] = (),
) -> DisplayNameMetadata:
    return DisplayNameMetadata(
        value=(
            value
            if type(value) is str
            and not _matches_identity_literal(value, identity_literals)
            else ""
        ),
        source=source,
    )


def select_display_name_metadata(
    candidates: Iterable[object],
    *,
    source: str,
    identity_literals: Iterable[object] = (),
) -> DisplayNameMetadata:
    """Select the first bounded non-blank platform display candidate.

    An unsafe non-blank primary value deliberately stops fallback.  This keeps
    a raw account literal from being bypassed merely because a later legacy
    field happens to contain a more attractive name.
    """

    for candidate in tuple(candidates or ()):
        bounded, _status = _bounded_display_name(candidate)
        if not bounded:
            continue
        return build_display_name_metadata(
            candidate,
            source=source,
            identity_literals=identity_literals,
        )
    return DisplayNameMetadata()


def build_participant_display(
    sender_key: str,
    *,
    display_name: object = "",
    display_name_source: str = "unavailable",
    identity_literals: Iterable[object] = (),
) -> ParticipantDisplay:
    return ParticipantDisplay(
        sender_key=str(sender_key or "").strip(),
        display=build_display_name_metadata(
            display_name,
            source=display_name_source,
            identity_literals=identity_literals,
        ),
    )


def build_inbound_identity_metadata(
    *,
    display_name: object = "",
    display_name_source: str = "unavailable",
    display_name_identity_literals: Iterable[object] = (),
    referenced_sender: ParticipantDisplay | None = None,
    mention_targets: Iterable[ParticipantDisplay] = (),
    has_reply_edge: bool = False,
    open_group: bool = False,
) -> InboundIdentityMetadata:
    mentions: list[ParticipantDisplay] = []
    seen: set[str] = set()
    for target in tuple(mention_targets or ()):
        if type(target) is not ParticipantDisplay:
            raise ValueError("inbound_mention_targets_invalid")
        if target.sender_key in seen:
            continue
        seen.add(target.sender_key)
        mentions.append(target)
    if has_reply_edge and referenced_sender is not None:
        status = "reply_and_mentions" if mentions else "reply"
    elif has_reply_edge:
        status = (
            "reply_target_unavailable_and_mentions"
            if mentions
            else "reply_target_unavailable"
        )
    elif mentions:
        status = "mentions"
    elif open_group:
        status = "open_group"
    else:
        status = "unspecified"
    return InboundIdentityMetadata(
        display=build_display_name_metadata(
            display_name,
            source=display_name_source,
            identity_literals=display_name_identity_literals,
        ),
        referenced_sender=referenced_sender,
        mention_targets=tuple(mentions),
        addressing_status=status,
    )


def identity_metadata_integrity(
    metadata: InboundIdentityMetadata,
) -> tuple[object, ...]:
    if type(metadata) is not InboundIdentityMetadata:
        raise ValueError("inbound_identity_metadata_invalid")

    def display_snapshot(value: DisplayNameMetadata) -> tuple[str, str, str]:
        return (value.value, value.source, value.status)

    referenced = (
        (
            metadata.referenced_sender.sender_key,
            display_snapshot(metadata.referenced_sender.display),
        )
        if metadata.referenced_sender is not None
        else None
    )
    mentions = tuple(
        (target.sender_key, display_snapshot(target.display))
        for target in metadata.mention_targets
    )
    return (
        display_snapshot(metadata.display),
        referenced,
        mentions,
        metadata.addressing_status,
    )


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    sequence: int
    source_kind: LedgerSourceKind
    role: LedgerRole
    scope_key: str
    session_id: str
    message_id: str
    sender_key: str
    reply_to_message_id: str
    referenced_sender_key: str
    target_sender_key: str
    timestamp: float
    content: str
    content_digest: str
    source_id: str
    attribution_status: str
    platform_id: str = ""
    sender_id: str = ""
    account_key: str = ""
    referenced_sender_id: str = ""
    mention_sender_ids: tuple[str, ...] = ()
    bot_id: str = ""
    group_id: str = ""
    unified_msg_origin: str = ""
    identity_metadata: InboundIdentityMetadata = field(
        default_factory=InboundIdentityMetadata,
        repr=False,
    )

    def __post_init__(self) -> None:
        if type(self.identity_metadata) is not InboundIdentityMetadata:
            raise ValueError("ledger_identity_metadata_invalid")
        referenced = self.identity_metadata.referenced_sender
        if referenced is not None:
            if self.referenced_sender_key and (
                self.referenced_sender_key != referenced.sender_key
            ):
                raise ValueError("ledger_referenced_sender_mismatch")
            if not self.referenced_sender_key:
                object.__setattr__(
                    self,
                    "referenced_sender_key",
                    referenced.sender_key,
                )
        if type(self.referenced_sender_id) is not str or any(
            type(value) is not str or not value
            for value in tuple(self.mention_sender_ids)
        ):
            raise ValueError("ledger_raw_address_identity_invalid")
        object.__setattr__(self, "mention_sender_ids", tuple(self.mention_sender_ids))

    @property
    def display_name(self) -> str:
        return self.identity_metadata.display.value

    @property
    def display_name_status(self) -> str:
        return self.identity_metadata.display.status

    @property
    def referenced_display_name(self) -> str:
        referenced = self.identity_metadata.referenced_sender
        return referenced.display.value if referenced is not None else ""

    @property
    def mention_targets(self) -> tuple[ParticipantDisplay, ...]:
        return self.identity_metadata.mention_targets

    @property
    def addressing_status(self) -> str:
        return self.identity_metadata.addressing_status


class PlatformGroupHistoryStatus(str, Enum):
    VERIFIED = "verified"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    BOUNDARY_UNAVAILABLE = "boundary_unavailable"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PlatformGroupHistoryRead:
    """Content-free result metadata plus admission-reconciled history rows."""

    status: PlatformGroupHistoryStatus
    records: tuple[LedgerRecord, ...]
    matched_ledger_message_ids: tuple[str, ...]
    reason_code: str

    def trace_metadata(self) -> dict[str, object]:
        return {
            "group_history_status": self.status.value,
            "group_history_record_count": len(self.records),
            "group_history_reason_code": self.reason_code,
        }


GROUP_CONTEXT_REQUEST_RE = re.compile(
    r"(?:他们|大家|群里|上面|前面|刚才|之前).{0,12}"
    r"(?:聊|说|谈|讨论|争论|吵|讲).{0,12}"
    r"(?:什么|啥|内容|事情|话题|哪件事)|"
    r"(?:在聊啥|在聊什么|说了啥|说了什么)",
    re.IGNORECASE,
)

GROUP_CONTEXT_UNAVAILABLE_ACK_RE = re.compile(
    r"(?:没(?:有|能)?(?:拿到|看到|读到)|看不到|读不到|无法(?:看到|读取|确认|概括)|缺少)"
    r".{0,18}(?:前面|上面|刚才|之前|群聊|聊天|消息|上下文|记录)|"
    r"(?:前面|上面|刚才|之前|群聊).{0,18}"
    r"(?:没(?:有|能)?(?:拿到|看到|读到)|看不到|读不到|无法(?:确认|概括)|"
    r"没(?:有)?接全|没跟上|断了|缺了)",
    re.IGNORECASE,
)


def is_explicit_group_context_request(message: str) -> bool:
    return bool(GROUP_CONTEXT_REQUEST_RE.search(str(message or "")))


def has_verified_public_group_context(records: Iterable[LedgerRecord]) -> bool:
    return any(
        type(record) is LedgerRecord
        and record.role is LedgerRole.USER
        and record.source_kind
        in {
            LedgerSourceKind.INBOUND,
            LedgerSourceKind.LEGACY_HISTORY,
            LedgerSourceKind.PLATFORM_GROUP_HISTORY,
        }
        and record.attribution_status
        in {
            "verified",
            "verified_sender",
            "verified_platform_sender_and_admission",
        }
        and type(record.content) is str
        and bool(record.content.strip())
        for record in tuple(records or ())
    )


def acknowledges_unavailable_group_context(visible_text: str) -> bool:
    return bool(GROUP_CONTEXT_UNAVAILABLE_ACK_RE.search(str(visible_text or "")))


def _display_name_payload(value: DisplayNameMetadata) -> dict[str, str]:
    if type(value) is not DisplayNameMetadata:
        raise ValueError("display_name_metadata_invalid")
    return {
        "value": value.value,
        "source": value.source,
        "status": value.status,
    }


def _participant_display_payload(value: ParticipantDisplay) -> dict[str, object]:
    if type(value) is not ParticipantDisplay:
        raise ValueError("participant_display_invalid")
    return {
        "sender_key": value.sender_key,
        "display": _display_name_payload(value.display),
    }


def _identity_metadata_payload(
    value: InboundIdentityMetadata,
) -> dict[str, object]:
    if type(value) is not InboundIdentityMetadata:
        raise ValueError("inbound_identity_metadata_invalid")
    return {
        "display": _display_name_payload(value.display),
        "referenced_sender": (
            _participant_display_payload(value.referenced_sender)
            if value.referenced_sender is not None
            else None
        ),
        "mention_targets": [
            _participant_display_payload(target)
            for target in value.mention_targets
        ],
        "addressing_status": value.addressing_status,
    }


def _validated_display_name_payload(value: object) -> DisplayNameMetadata:
    if type(value) is not dict or set(value) != {"value", "source", "status"}:
        raise ValueError("state_invalid")
    if any(type(value[key]) is not str for key in value):
        raise ValueError("state_invalid")
    metadata = DisplayNameMetadata(
        value=value["value"],
        source=value["source"],
    )
    if metadata.status != value["status"]:
        raise ValueError("state_invalid")
    return metadata


def _validated_participant_display_payload(value: object) -> ParticipantDisplay:
    if type(value) is not dict or set(value) != {"sender_key", "display"}:
        raise ValueError("state_invalid")
    if type(value["sender_key"]) is not str:
        raise ValueError("state_invalid")
    return ParticipantDisplay(
        sender_key=value["sender_key"],
        display=_validated_display_name_payload(value["display"]),
    )


def _validated_identity_metadata_payload(value: object) -> InboundIdentityMetadata:
    expected = {
        "display",
        "referenced_sender",
        "mention_targets",
        "addressing_status",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("state_invalid")
    if (
        type(value["addressing_status"]) is not str
        or type(value["mention_targets"]) is not list
        or len(value["mention_targets"]) > 16
        or value["referenced_sender"] is not None
        and type(value["referenced_sender"]) is not dict
    ):
        raise ValueError("state_invalid")
    return InboundIdentityMetadata(
        display=_validated_display_name_payload(value["display"]),
        referenced_sender=(
            _validated_participant_display_payload(value["referenced_sender"])
            if value["referenced_sender"] is not None
            else None
        ),
        mention_targets=tuple(
            _validated_participant_display_payload(target)
            for target in value["mention_targets"]
        ),
        addressing_status=value["addressing_status"],
    )


class ConversationLedger:
    """Bounded typed ledger with an optional restart-safe public inbound tail.

    Only source-verified human inbound records are persisted.  Assistant
    output, references, tool results and third-party legacy history remain
    process-local because they require their original receipt/authority graph.
    """

    _PERSISTENCE_SCHEMA_VERSION = 4
    _MAX_STATE_BYTES = 16 * 1024 * 1024
    _SECRET_FILENAME = ".public_group_ledger_secret"

    def __init__(
        self,
        *,
        max_records_per_scope: int = 256,
        state_path: Path | str | None = None,
        max_persisted_records_per_scope: int = 32,
        max_persisted_scopes: int = 128,
    ) -> None:
        self.max_records_per_scope = max(8, int(max_records_per_scope))
        self.max_persisted_records_per_scope = max(
            8,
            min(
                self.max_records_per_scope,
                int(max_persisted_records_per_scope),
            ),
        )
        self.max_persisted_scopes = max(
            1,
            min(1024, int(max_persisted_scopes)),
        )
        self.state_path = Path(state_path) if state_path is not None else None
        self._lock = threading.RLock()
        self._records: dict[str, deque[LedgerRecord]] = {}
        self._message_index: dict[tuple[str, str], list[LedgerRecord]] = {}
        self._reply_index: dict[tuple[str, str], list[LedgerRecord]] = {}
        self._sender_index: dict[tuple[str, str], list[LedgerRecord]] = {}
        self._next_sequence = 1
        self._loading_state = False
        self._persistence_loaded = False
        self._persistence_failure_code = ""
        self._state_secret = b""
        self._restored_records: tuple[LedgerRecord, ...] = ()
        self._initialize_state_secret()
        self._load_persisted_state()

    def _initialize_state_secret(self) -> None:
        if self.state_path is None:
            return
        secret_path = self.state_path.parent / self._SECRET_FILENAME
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                secret = secret_path.read_bytes()
            except FileNotFoundError:
                secret = secrets.token_bytes(32)
                descriptor = os.open(
                    secret_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(secret)
                    stream.flush()
                    os.fsync(stream.fileno())
            if type(secret) is not bytes or len(secret) != 32:
                raise ValueError("state_secret_invalid")
            self._state_secret = secret
        except BaseException:
            self._state_secret = b""
            self._persistence_failure_code = "state_secret_unavailable"

    @staticmethod
    def _persisted_record_payload(record: LedgerRecord) -> dict[str, object]:
        return {
            "scope_key": record.scope_key,
            "session_id": record.session_id,
            "message_id": record.message_id,
            "sender_key": record.sender_key,
            "reply_to_message_id": record.reply_to_message_id,
            "referenced_sender_key": record.referenced_sender_key,
            "target_sender_key": record.target_sender_key,
            "timestamp": record.timestamp,
            "content": record.content,
            "content_digest": record.content_digest,
            "source_id": record.source_id,
            "attribution_status": record.attribution_status,
            "platform_id": record.platform_id,
            "sender_id": record.sender_id,
            "referenced_sender_id": record.referenced_sender_id,
            "mention_sender_ids": list(record.mention_sender_ids),
            "bot_id": record.bot_id,
            "group_id": record.group_id,
            "unified_msg_origin": record.unified_msg_origin,
            "identity_metadata": _identity_metadata_payload(
                record.identity_metadata
            ),
        }

    @classmethod
    def _validated_persisted_record(
        cls,
        value: object,
        *,
        schema_version: int,
    ) -> dict[str, object]:
        base_keys = {
            "scope_key",
            "session_id",
            "message_id",
            "sender_key",
            "reply_to_message_id",
            "timestamp",
            "content",
            "content_digest",
            "source_id",
            "attribution_status",
        }
        target_keys = {
            "platform_id",
            "bot_id",
            "group_id",
            "unified_msg_origin",
        }
        relation_keys = {
            "referenced_sender_key",
            "target_sender_key",
            "identity_metadata",
        }
        if schema_version == 1:
            expected_keys = base_keys
        elif schema_version == 2:
            expected_keys = base_keys | target_keys
        elif schema_version == 3:
            expected_keys = base_keys | target_keys | relation_keys
        else:
            expected_keys = base_keys | target_keys | relation_keys | {
                "sender_id",
                "referenced_sender_id",
                "mention_sender_ids",
            }
        if type(value) is not dict or set(value) != expected_keys:
            raise ValueError("state_invalid")
        normalized = dict(value)
        if schema_version == 1:
            normalized.update({key: "" for key in target_keys})
        if schema_version in {1, 2}:
            normalized.update(
                {
                    "sender_id": "",
                    "referenced_sender_id": "",
                    "mention_sender_ids": [],
                    "referenced_sender_key": "",
                    "target_sender_key": "",
                    "identity_metadata": build_inbound_identity_metadata(
                        has_reply_edge=(
                            type(normalized["reply_to_message_id"]) is str
                            and bool(normalized["reply_to_message_id"].strip())
                        ),
                    ),
                }
            )
        elif schema_version == 3:
            normalized["identity_metadata"] = (
                _validated_identity_metadata_payload(
                    normalized["identity_metadata"]
                )
            )
            normalized["sender_id"] = ""
            normalized["referenced_sender_id"] = ""
            normalized["mention_sender_ids"] = []
        else:
            normalized["identity_metadata"] = _validated_identity_metadata_payload(normalized["identity_metadata"])
        value = normalized
        expected_keys = base_keys | target_keys | relation_keys | {
            "sender_id", "referenced_sender_id", "mention_sender_ids"
        }
        text_keys = expected_keys - {"timestamp", "identity_metadata", "mention_sender_ids"}
        if any(type(value[key]) is not str for key in text_keys):
            raise ValueError("state_invalid")
        if (
            type(value["mention_sender_ids"]) is not list
            or any(type(item) is not str or not item for item in value["mention_sender_ids"])
            or len(value["mention_sender_ids"]) > 16
        ):
            raise ValueError("state_invalid")
        value["mention_sender_ids"] = tuple(value["mention_sender_ids"])
        timestamp = value["timestamp"]
        if type(timestamp) not in {int, float} or isinstance(timestamp, bool):
            raise ValueError("state_invalid")
        if (
            not value["scope_key"]
            or not value["message_id"]
            or not value["sender_key"]
            or value["attribution_status"] != "verified"
            or len(value["content"]) > 4000
            or ledger_content_digest(value["content"]) != value["content_digest"]
            or type(value["identity_metadata"]) is not InboundIdentityMetadata
        ):
            raise ValueError("state_invalid")
        referenced = value["identity_metadata"].referenced_sender
        if referenced is not None and (
            referenced.sender_key != value["referenced_sender_key"]
        ):
            raise ValueError("state_invalid")
        return value

    def _load_persisted_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            if not self._state_secret:
                raise ValueError("state_secret_unavailable")
            if not self.state_path.is_file():
                raise ValueError("state_invalid")
            size = self.state_path.stat().st_size
            if size < 2 or size > self._MAX_STATE_BYTES:
                raise ValueError("state_invalid")
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if type(payload) is not dict or type(payload.get("schema_version")) is not int:
                raise ValueError("state_invalid")
            schema_version = payload["schema_version"]
            expected_payload_keys = (
                {"schema_version", "records"}
                if schema_version == 1
                else {"schema_version", "records", "state_mac"}
            )
            if (
                set(payload) != expected_payload_keys
                or schema_version not in {
                    1,
                    2,
                    3, self._PERSISTENCE_SCHEMA_VERSION,
                }
                or type(payload["records"]) is not list
                or len(payload["records"])
                > self.max_persisted_scopes * self.max_persisted_records_per_scope
            ):
                raise ValueError("state_invalid")
            if schema_version in {2, self._PERSISTENCE_SCHEMA_VERSION}:
                rows_encoded = json.dumps(
                    payload["records"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                state_mac = payload["state_mac"]
                if (
                    type(state_mac) is not str
                    or len(state_mac) != 64
                    or not hmac.compare_digest(
                        state_mac,
                        hmac.new(
                            self._state_secret,
                            (
                                b"public-group-ledger-v2\x00"
                                if schema_version == 2
                                else (b"public-group-ledger-v3\x00" if schema_version == 3 else b"public-group-ledger-v4\x00")
                            )
                            + rows_encoded,
                            hashlib.sha256,
                        ).hexdigest(),
                    )
                ):
                    raise ValueError("state_invalid")
            validated = [
                self._validated_persisted_record(
                    value,
                    schema_version=schema_version,
                )
                for value in payload["records"]
            ]
            scopes: list[str] = []
            for value in validated:
                scope_key = str(value["scope_key"])
                if scope_key not in scopes:
                    scopes.append(scope_key)
            if len(scopes) > self.max_persisted_scopes:
                raise ValueError("state_invalid")
            self._loading_state = True
            try:
                restored: list[LedgerRecord] = []
                for value in validated:
                    restored.append(self._append(
                        source_kind=LedgerSourceKind.INBOUND,
                        role=LedgerRole.USER,
                        scope_key=str(value["scope_key"]),
                        session_id=str(value["session_id"]),
                        message_id=str(value["message_id"]),
                        sender_key=str(value["sender_key"]),
                        reply_to_message_id=str(value["reply_to_message_id"]),
                        referenced_sender_key=str(
                            value["referenced_sender_key"]
                        ),
                        target_sender_key=str(value["target_sender_key"]),
                        timestamp=float(value["timestamp"]),
                        content=str(value["content"]),
                        source_id=str(value["source_id"]),
                        attribution_status="verified",
                        platform_id=str(value["platform_id"]),
                        sender_id=str(value["sender_id"]),
                        bot_id=str(value["bot_id"]),
                        group_id=str(value["group_id"]),
                        unified_msg_origin=str(value["unified_msg_origin"]),
                        identity_metadata=value["identity_metadata"],
                    ))
                self._restored_records = tuple(restored)
            finally:
                self._loading_state = False
            self._persistence_loaded = True
            self._persistence_failure_code = ""
        except Exception:
            self._records.clear()
            self._message_index.clear()
            self._reply_index.clear()
            self._sender_index.clear()
            self._next_sequence = 1
            self._loading_state = False
            self._persistence_loaded = False
            self._persistence_failure_code = "state_invalid"
            self._restored_records = ()

    def _persist_locked(self) -> None:
        if self.state_path is None or self._loading_state:
            return
        try:
            if not self._state_secret:
                raise ValueError("state_secret_unavailable")
            scope_keys = list(self._records)[-self.max_persisted_scopes :]
            records: list[dict[str, object]] = []
            for scope_key in scope_keys:
                inbound = [
                    record
                    for record in self._records[scope_key]
                    if record.source_kind is LedgerSourceKind.INBOUND
                    and record.role is LedgerRole.USER
                    and record.attribution_status == "verified"
                ][-self.max_persisted_records_per_scope :]
                records.extend(self._persisted_record_payload(record) for record in inbound)
            rows_encoded = json.dumps(
                records,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            encoded = json.dumps(
                {
                    "schema_version": self._PERSISTENCE_SCHEMA_VERSION,
                    "records": records,
                    "state_mac": hmac.new(
                        self._state_secret,
                        b"public-group-ledger-v4\x00" + rows_encoded,
                        hashlib.sha256,
                    ).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > self._MAX_STATE_BYTES:
                raise ValueError("state_too_large")
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            with temporary.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            self._persistence_failure_code = ""
        except Exception:
            self._persistence_failure_code = "write_failed"

    def flush(self) -> None:
        with self._lock:
            self._persist_locked()

    def persistence_metadata(self) -> dict[str, object]:
        with self._lock:
            persisted_scope_keys = list(self._records)[
                -self.max_persisted_scopes :
            ]
            persisted_records = sum(
                min(
                    self.max_persisted_records_per_scope,
                    sum(
                        record.source_kind is LedgerSourceKind.INBOUND
                        and record.role is LedgerRole.USER
                        for record in self._records[scope_key]
                    ),
                )
                for scope_key in persisted_scope_keys
            )
            return {
                "persistence_enabled": self.state_path is not None,
                "persistence_loaded": self._persistence_loaded,
                "persistence_failure_code": self._persistence_failure_code,
                "persisted_scope_count": len(persisted_scope_keys),
                "persisted_record_count": persisted_records,
                "persistence_bounded": (
                    len(persisted_scope_keys) <= self.max_persisted_scopes
                    and persisted_records
                    <= self.max_persisted_scopes
                    * self.max_persisted_records_per_scope
                ),
            }

    @staticmethod
    def _add_index(
        index: dict[tuple[str, str], list[LedgerRecord]],
        scope_key: str,
        value: str,
        record: LedgerRecord,
    ) -> None:
        normalized_value = str(value or "").strip()
        if normalized_value:
            index.setdefault((scope_key, normalized_value), []).append(record)

    @staticmethod
    def _remove_index(
        index: dict[tuple[str, str], list[LedgerRecord]],
        scope_key: str,
        value: str,
        record: LedgerRecord,
    ) -> None:
        normalized_value = str(value or "").strip()
        key = (scope_key, normalized_value)
        if not normalized_value or key not in index:
            return
        index[key] = [candidate for candidate in index[key] if candidate is not record]
        if not index[key]:
            index.pop(key, None)

    def _index_record(self, record: LedgerRecord) -> None:
        self._add_index(
            self._message_index,
            record.scope_key,
            record.message_id,
            record,
        )
        self._add_index(
            self._reply_index,
            record.scope_key,
            record.reply_to_message_id,
            record,
        )
        self._add_index(
            self._sender_index,
            record.scope_key,
            record.sender_key,
            record,
        )

    def _unindex_record(self, record: LedgerRecord) -> None:
        self._remove_index(
            self._message_index,
            record.scope_key,
            record.message_id,
            record,
        )
        self._remove_index(
            self._reply_index,
            record.scope_key,
            record.reply_to_message_id,
            record,
        )
        self._remove_index(
            self._sender_index,
            record.scope_key,
            record.sender_key,
            record,
        )

    def _append(
        self,
        *,
        source_kind: LedgerSourceKind,
        role: LedgerRole,
        scope_key: str,
        session_id: str,
        message_id: str,
        sender_key: str,
        reply_to_message_id: str = "",
        referenced_sender_key: str = "",
        target_sender_key: str = "",
        timestamp: float = 0.0,
        content: str = "",
        source_id: str = "",
        attribution_status: str = "verified",
        platform_id: str = "",
        sender_id: str = "",
        referenced_sender_id: str = "",
        mention_sender_ids: Iterable[str] = (),
        bot_id: str = "",
        group_id: str = "",
        unified_msg_origin: str = "",
        identity_metadata: InboundIdentityMetadata | None = None,
    ) -> LedgerRecord:
        with self._lock:
            normalized_scope = str(scope_key or "").strip()
            if not normalized_scope:
                raise ValueError("ledger records require a trusted non-empty scope_key")
            normalized_content = str(content or "")
            normalized_identity = (
                identity_metadata
                if identity_metadata is not None
                else InboundIdentityMetadata()
            )
            if type(normalized_identity) is not InboundIdentityMetadata:
                raise ValueError("ledger_identity_metadata_invalid")
            normalized_referenced_sender_key = str(
                referenced_sender_key or ""
            ).strip()
            metadata_referenced = normalized_identity.referenced_sender
            if metadata_referenced is not None:
                if normalized_referenced_sender_key and (
                    normalized_referenced_sender_key
                    != metadata_referenced.sender_key
                ):
                    raise ValueError("ledger_referenced_sender_mismatch")
                normalized_referenced_sender_key = (
                    metadata_referenced.sender_key
                )
            record = LedgerRecord(
                sequence=self._next_sequence,
                source_kind=source_kind,
                role=role,
                scope_key=normalized_scope,
                session_id=str(session_id or "").strip(),
                message_id=str(message_id or "").strip(),
                sender_key=str(sender_key or "").strip(),
                reply_to_message_id=str(reply_to_message_id or "").strip(),
                referenced_sender_key=normalized_referenced_sender_key,
                target_sender_key=str(target_sender_key or "").strip(),
                timestamp=max(0.0, float(timestamp or 0)),
                content=normalized_content,
                content_digest=ledger_content_digest(normalized_content),
                source_id=str(source_id or "").strip(),
                attribution_status=str(attribution_status or "unknown").strip()
                or "unknown",
                platform_id=str(platform_id or "").strip(),
                sender_id=str(sender_id or "").strip(),
                account_key=build_account_key(platform_id, sender_id),
                referenced_sender_id=str(referenced_sender_id or "").strip(),
                mention_sender_ids=tuple(
                    str(value or "").strip()
                    for value in tuple(mention_sender_ids or ())
                    if str(value or "").strip()
                ),
                bot_id=str(bot_id or "").strip(),
                group_id=str(group_id or "").strip(),
                unified_msg_origin=str(unified_msg_origin or "").strip(),
                identity_metadata=normalized_identity,
            )
            self._next_sequence += 1
            if normalized_scope not in self._records and (
                self.state_path is not None
                and len(self._records) >= self.max_persisted_scopes
            ):
                oldest_scope = next(iter(self._records))
                for stale in self._records.pop(oldest_scope):
                    self._unindex_record(stale)
            bucket = self._records.setdefault(
                normalized_scope,
                deque(),
            )
            if len(bucket) >= self.max_records_per_scope:
                self._unindex_record(bucket.popleft())
            bucket.append(record)
            self._index_record(record)
            if source_kind is LedgerSourceKind.INBOUND:
                self._persist_locked()
            return record

    def record_inbound(
        self,
        envelope: TurnEnvelope,
        content: str,
        *,
        unified_msg_origin: str = "",
        identity_metadata: InboundIdentityMetadata | None = None,
        mention_sender_ids: Iterable[str] = (),
    ) -> LedgerRecord:
        with self._lock:
            expected_digest = ledger_content_digest(content)
            for existing in self._message_index.get(
                (envelope.scope_key, envelope.message_id),
                (),
            ):
                if (
                    existing.source_kind is LedgerSourceKind.INBOUND
                    and existing.role is LedgerRole.USER
                    and existing.sender_key == envelope.sender_key
                    and existing.content_digest == expected_digest
                ):
                    if existing.reply_to_message_id != envelope.reply_to_message_id:
                        raise ValueError("ledger_inbound_reply_mismatch")
                    if identity_metadata is not None:
                        if type(identity_metadata) is not InboundIdentityMetadata:
                            raise ValueError("ledger_identity_metadata_invalid")
                        if identity_metadata_integrity(
                            existing.identity_metadata
                        ) != identity_metadata_integrity(identity_metadata):
                            raise ValueError("ledger_inbound_identity_mismatch")
                    return existing
        normalized_identity = identity_metadata
        if normalized_identity is None:
            referenced_sender = None
            if envelope.reply_to_sender_id:
                referenced_sender_key = build_sender_key(
                    envelope.scope_key,
                    envelope.reply_to_sender_id,
                )
                if referenced_sender_key:
                    referenced_sender = build_participant_display(
                        referenced_sender_key
                    )
            normalized_identity = build_inbound_identity_metadata(
                referenced_sender=referenced_sender,
                has_reply_edge=bool(envelope.reply_to_message_id),
                open_group=envelope.chat_type == "group",
            )
        if type(normalized_identity) is not InboundIdentityMetadata:
            raise ValueError("ledger_identity_metadata_invalid")
        referenced_sender_key = (
            normalized_identity.referenced_sender.sender_key
            if normalized_identity.referenced_sender is not None
            else ""
        )
        return self._append(
            source_kind=LedgerSourceKind.INBOUND,
            role=LedgerRole.USER,
            scope_key=envelope.scope_key,
            session_id=envelope.session_id,
            message_id=envelope.message_id,
            sender_key=envelope.sender_key,
            reply_to_message_id=envelope.reply_to_message_id,
            referenced_sender_key=referenced_sender_key,
            timestamp=envelope.timestamp,
            content=content,
            source_id=envelope.message_id,
            platform_id=envelope.platform_id,
            sender_id=envelope.sender_id,
            referenced_sender_id=envelope.reply_to_sender_id,
            mention_sender_ids=mention_sender_ids,
            bot_id=envelope.bot_id,
            group_id=envelope.group_id,
            unified_msg_origin=unified_msg_origin,
            identity_metadata=normalized_identity,
        )

    def restored_inbound_records(self) -> tuple[LedgerRecord, ...]:
        """Return exact records loaded from the bounded restart-safe state."""

        with self._lock:
            restored = self._restored_records
            for record in restored:
                if (
                    type(record) is not LedgerRecord
                    or record.source_kind is not LedgerSourceKind.INBOUND
                    or record.role is not LedgerRole.USER
                    or record.attribution_status != "verified"
                    or not any(record is current for current in self._records.get(record.scope_key, ()))
                    or ledger_content_digest(record.content) != record.content_digest
                    or any(
                        type(value) is not str
                        for value in (
                            record.scope_key,
                            record.session_id,
                            record.message_id,
                            record.sender_key,
                            record.platform_id,
                            record.bot_id,
                            record.group_id,
                            record.unified_msg_origin,
                        )
                    )
                    or type(record.identity_metadata)
                    is not InboundIdentityMetadata
                ):
                    raise ValueError("restored_record_corrupt")
            return restored

    def record_reference(
        self,
        envelope: TurnEnvelope,
        *,
        referenced_content: str = "",
        referenced_sender_key: str = "",
        identity_metadata: InboundIdentityMetadata | None = None,
    ) -> LedgerRecord:
        if not envelope.reply_to_message_id:
            raise ValueError("reference records require reply_to_message_id")
        return self._append(
            source_kind=LedgerSourceKind.REFERENCE,
            role=LedgerRole.CONTEXT,
            scope_key=envelope.scope_key,
            session_id=envelope.session_id,
            message_id=envelope.message_id,
            sender_key=envelope.sender_key,
            reply_to_message_id=envelope.reply_to_message_id,
            referenced_sender_key=referenced_sender_key,
            timestamp=envelope.timestamp,
            content=referenced_content,
            source_id=envelope.reply_to_message_id,
            identity_metadata=identity_metadata,
        )

    def record_outbound(
        self,
        *,
        scope_key: str,
        session_id: str,
        message_id: str,
        bot_sender_key: str,
        target_sender_key: str,
        reply_to_message_id: str,
        timestamp: float,
        content: str,
        platform_id: str = "",
        bot_id: str = "",
    ) -> LedgerRecord:
        return self._append(
            source_kind=LedgerSourceKind.OUTBOUND,
            role=LedgerRole.ASSISTANT,
            scope_key=scope_key,
            session_id=session_id,
            message_id=message_id,
            sender_key=bot_sender_key,
            reply_to_message_id=reply_to_message_id,
            target_sender_key=target_sender_key,
            timestamp=timestamp,
            content=content,
            source_id=message_id,
            platform_id=platform_id,
            sender_id=bot_id,
            bot_id=bot_id,
        )

    def record_tool_result(
        self,
        *,
        scope_key: str,
        session_id: str,
        source_id: str,
        tool_name: str,
        timestamp: float,
        content: str,
    ) -> LedgerRecord:
        return self._append(
            source_kind=LedgerSourceKind.TOOL_RESULT,
            role=LedgerRole.TOOL,
            scope_key=scope_key,
            session_id=session_id,
            message_id="",
            sender_key="",
            timestamp=timestamp,
            content=content,
            source_id=f"{str(tool_name or '').strip()}:{str(source_id or '').strip()}",
        )

    def records(
        self,
        scope_key: str,
        *,
        source_kinds: Iterable[LedgerSourceKind] | None = None,
    ) -> tuple[LedgerRecord, ...]:
        with self._lock:
            records = tuple(self._records.get(str(scope_key or "").strip(), ()))
            if source_kinds is None:
                return records
            allowed = set(source_kinds)
            return tuple(record for record in records if record.source_kind in allowed)

    def records_by_message_id(
        self,
        scope_key: str,
        message_id: str,
    ) -> tuple[LedgerRecord, ...]:
        with self._lock:
            return tuple(
                self._message_index.get(
                    (str(scope_key or "").strip(), str(message_id or "").strip()),
                    (),
                )
            )

    def records_replying_to(
        self,
        scope_key: str,
        message_id: str,
    ) -> tuple[LedgerRecord, ...]:
        with self._lock:
            return tuple(
                self._reply_index.get(
                    (str(scope_key or "").strip(), str(message_id or "").strip()),
                    (),
                )
            )

    def records_by_sender(
        self,
        scope_key: str,
        sender_key: str,
    ) -> tuple[LedgerRecord, ...]:
        with self._lock:
            return tuple(
                self._sender_index.get(
                    (str(scope_key or "").strip(), str(sender_key or "").strip()),
                    (),
                )
            )


def _platform_history_timestamp(value: object) -> float:
    if type(value) is datetime:
        parsed = value
    elif type(value) is str:
        normalized = value.strip()
        if not normalized or len(normalized) > 64:
            return 0.0
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    else:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return max(0.0, float(parsed.timestamp()))
    except (OverflowError, OSError, ValueError):
        return 0.0


def _platform_history_semantics(
    content: object,
) -> tuple[
    str,
    tuple[str, str] | None,
    tuple[tuple[str, str], ...],
]:
    """Project one structured row into its body and addressing evidence."""

    if type(content) is not dict or set(content) != {"type", "message"}:
        return "", None, ()
    if content.get("type") != "user" or type(content.get("message")) is not list:
        return "", None, ()
    body: list[str] = []
    reply_evidence: tuple[str, str] | None = None
    mention_evidence: list[tuple[str, str]] = []
    for part in content["message"]:
        if type(part) is not dict or type(part.get("type")) is not str:
            return "", None, ()
        kind = part["type"]
        if kind == "plain":
            if type(part.get("text")) is not str:
                return "", None, ()
            body.append(part["text"])
        elif kind == "at":
            name = part.get("name", "")
            if type(name) is not str:
                return "", None, ()
            raw_target_id = part.get("qq", "")
            mention_evidence.append(
                (
                    str(raw_target_id).strip()
                    if type(raw_target_id) in {str, int}
                    else "",
                    name,
                )
            )
        elif kind == "reply":
            sender_name = part.get("sender_name", "")
            raw_sender_id = part.get("sender_id", "")
            reply_text = part.get("text", "")
            if (
                reply_evidence is not None
                or type(sender_name) is not str
                or type(reply_text) is not str
            ):
                return "", None, ()
            reply_evidence = (
                str(raw_sender_id).strip()
                if type(raw_sender_id) in {str, int}
                else "",
                sender_name,
            )
        else:
            body.append("[消息]")
    return (
        "".join(body).strip()[:2000],
        reply_evidence,
        tuple(mention_evidence),
    )


def _platform_identity_metadata(
    match: LedgerRecord,
    *,
    scope_key: str,
    sender_display_name: str,
    protected_identity_literals: tuple[str, ...],
    reply_evidence: tuple[str, str] | None,
    mention_evidence: tuple[tuple[str, str], ...],
) -> InboundIdentityMetadata:
    current = match.identity_metadata
    current_speaker_display = build_display_name_metadata(
        current.display.value,
        source=current.display.source,
        identity_literals=protected_identity_literals,
    )
    row_speaker_display = build_display_name_metadata(
        sender_display_name,
        source="platform_history",
        identity_literals=protected_identity_literals,
    )
    if current.display.available:
        # A previously accepted non-blank value that is unsafe against the
        # request-wide identity set remains unavailable; a later row name must
        # not bypass that fail-closed result.
        speaker_display = current_speaker_display
    elif row_speaker_display.available:
        speaker_display = row_speaker_display
    else:
        speaker_display = current_speaker_display

    def guarded_participant(target: ParticipantDisplay) -> ParticipantDisplay:
        return ParticipantDisplay(
            sender_key=target.sender_key,
            display=build_display_name_metadata(
                target.display.value,
                source=target.display.source,
                identity_literals=protected_identity_literals,
            ),
        )

    referenced_sender = current.referenced_sender
    referenced_name_was_available = bool(
        referenced_sender is not None and referenced_sender.display.available
    )
    if referenced_sender is not None:
        referenced_sender = guarded_participant(referenced_sender)
    if referenced_sender is None and match.referenced_sender_key:
        referenced_sender = build_participant_display(
            match.referenced_sender_key,
            identity_literals=protected_identity_literals,
        )
    if (
        referenced_sender is not None
        and not referenced_name_was_available
        and not referenced_sender.display.available
        and reply_evidence is not None
    ):
        reply_target_id, reply_display_name = reply_evidence
        if (
            build_sender_key(scope_key, reply_target_id)
            == referenced_sender.sender_key
        ):
            referenced_sender = build_participant_display(
                referenced_sender.sender_key,
                display_name=reply_display_name,
                display_name_source="reply_component",
                identity_literals=protected_identity_literals,
            )
    blocked_mention_keys: set[str] = set()
    guarded_mentions: list[ParticipantDisplay] = []
    for target in current.mention_targets:
        guarded = guarded_participant(target)
        if target.display.available and not guarded.display.available:
            blocked_mention_keys.add(target.sender_key)
        guarded_mentions.append(guarded)
    mention_targets = tuple(guarded_mentions)
    if mention_targets and mention_evidence:
        row_names_by_key: dict[str, str] = {}
        ambiguous_row_keys: set[str] = set()
        for raw_target_id, display_name in mention_evidence:
            target_key = build_sender_key(scope_key, raw_target_id)
            if not target_key:
                continue
            if target_key in row_names_by_key:
                ambiguous_row_keys.add(target_key)
                continue
            row_names_by_key[target_key] = display_name
        resolved_mentions: list[ParticipantDisplay] = []
        for target in mention_targets:
            if (
                target.display.available
                or target.sender_key in blocked_mention_keys
                or target.sender_key not in row_names_by_key
                or target.sender_key in ambiguous_row_keys
            ):
                resolved_mentions.append(target)
                continue
            resolved_mentions.append(
                build_participant_display(
                    target.sender_key,
                    display_name=row_names_by_key[target.sender_key],
                    display_name_source="mention_component",
                    identity_literals=protected_identity_literals,
                )
            )
        mention_targets = tuple(resolved_mentions)
    return build_inbound_identity_metadata(
        display_name=speaker_display.value,
        display_name_source=speaker_display.source,
        display_name_identity_literals=protected_identity_literals,
        referenced_sender=referenced_sender,
        mention_targets=mention_targets,
        has_reply_edge=bool(match.reply_to_message_id),
        open_group=(
            not match.reply_to_message_id
            and not mention_targets
            and current.addressing_status == "open_group"
        ),
    )


async def read_astrbot_group_history(
    *,
    context: Any,
    event: Any,
    accepted_records: Iterable[LedgerRecord],
    scope_key: str,
    session_id: str,
    max_records: int = 16,
) -> PlatformGroupHistoryRead:
    """Read the real AstrBot group-history manager and reconcile admission.

    AstrBot persists platform history before downstream plugin gates.  Shio
    therefore treats those rows only as sender/name/order evidence and admits a
    row into the prompt after it matches an exact source-verified human inbound
    record from Shio's accepted-turn ledger.
    """

    normalized_scope = str(scope_key or "").strip()
    normalized_session = str(session_id or "").strip()
    manager = getattr(context, "message_history_manager", None)
    reader = getattr(manager, "get", None)
    if not callable(reader):
        return PlatformGroupHistoryRead(
            PlatformGroupHistoryStatus.UNAVAILABLE,
            (),
            (),
            "manager_unavailable",
        )
    get_extra = getattr(event, "get_extra", None)
    current_id = (
        get_extra("_current_platform_message_history_id", None)
        if callable(get_extra)
        else None
    )
    if type(current_id) is not int or current_id <= 0:
        return PlatformGroupHistoryRead(
            PlatformGroupHistoryStatus.BOUNDARY_UNAVAILABLE,
            (),
            (),
            "current_history_boundary_unavailable",
        )
    get_platform_id = getattr(event, "get_platform_id", None)
    platform_id = get_platform_id() if callable(get_platform_id) else ""
    user_id = getattr(event, "unified_msg_origin", "")
    if (
        type(platform_id) is not str
        or not platform_id.strip()
        or type(user_id) is not str
        or not user_id.strip()
        or not normalized_scope
        or not normalized_session
    ):
        return PlatformGroupHistoryRead(
            PlatformGroupHistoryStatus.UNAVAILABLE,
            (),
            (),
            "history_scope_unavailable",
        )

    accepted_values = tuple(accepted_records or ())
    protected_sender_keys: list[str] = [normalized_scope]
    for record in accepted_values:
        if type(record) is not LedgerRecord or record.scope_key != normalized_scope:
            continue
        protected_sender_keys.extend(
            value
            for value in (
                record.sender_key,
                record.referenced_sender_key,
                record.target_sender_key,
            )
            if value
        )
        if record.identity_metadata.referenced_sender is not None:
            protected_sender_keys.append(
                record.identity_metadata.referenced_sender.sender_key
            )
        protected_sender_keys.extend(
            target.sender_key
            for target in record.identity_metadata.mention_targets
        )
    accepted: dict[tuple[str, str], list[LedgerRecord]] = {}
    for record in accepted_values:
        if (
            type(record) is LedgerRecord
            and record.source_kind is LedgerSourceKind.INBOUND
            and record.role is LedgerRole.USER
            and record.scope_key == normalized_scope
            and record.attribution_status == "verified"
            and record.message_id
            and record.sender_key
            and record.timestamp > 0
        ):
            accepted.setdefault(
                (record.sender_key, record.content_digest),
                [],
            ).append(record)
    bounded_max = max(1, min(32, int(max_records)))
    try:
        rows = await reader(
            platform_id=platform_id,
            user_id=user_id,
            page=1,
            page_size=max(32, min(200, bounded_max * 4)),
        )
    except Exception:
        return PlatformGroupHistoryRead(
            PlatformGroupHistoryStatus.ERROR,
            (),
            (),
            "manager_read_failed",
        )
    if type(rows) not in {list, tuple}:
        return PlatformGroupHistoryRead(
            PlatformGroupHistoryStatus.ERROR,
            (),
            (),
            "manager_result_invalid",
        )
    if not accepted:
        return PlatformGroupHistoryRead(
            PlatformGroupHistoryStatus.EMPTY,
            (),
            (),
            "accepted_history_empty",
        )

    matched_ids: set[str] = set()
    admitted_rows: list[
        tuple[
            int,
            str,
            str,
            str,
            float,
            str,
            tuple[str, str] | None,
            tuple[tuple[str, str], ...],
            LedgerRecord,
        ]
    ] = []
    for row in rows:
        row_id = getattr(row, "id", None)
        row_platform = getattr(row, "platform_id", None)
        row_user = getattr(row, "user_id", None)
        sender_id = getattr(row, "sender_id", None)
        sender_name = getattr(row, "sender_name", None)
        if sender_name is None:
            sender_name = ""
        created_at = _platform_history_timestamp(getattr(row, "created_at", None))
        row_content = getattr(row, "content", None)
        (
            semantic_content,
            reply_evidence,
            mention_evidence,
        ) = _platform_history_semantics(
            row_content,
        )
        if (
            type(row_id) is not int
            or row_id <= 0
            or row_id >= current_id
            or type(row_platform) is not str
            or row_platform != platform_id
            or type(row_user) is not str
            or row_user != user_id
            or type(sender_id) is not str
            or not sender_id.strip()
            or type(sender_name) is not str
            or created_at <= 0
            or not semantic_content
        ):
            continue
        sender_key = build_sender_key(normalized_scope, sender_id.strip())
        candidates = accepted.get(
            (sender_key, ledger_content_digest(semantic_content)),
            (),
        )
        # Relation components cannot identify an accepted message.  First bind
        # the row to exactly one source-verified message using code-owned
        # sender/body/time evidence; only then may relation IDs enrich names.
        eligible = tuple(
            candidate
            for candidate in candidates
            if candidate.message_id not in matched_ids
            and abs(candidate.timestamp - created_at) <= 180.0
        )
        if len(eligible) != 1:
            continue
        match = eligible[0]
        matched_ids.add(match.message_id)
        admitted_rows.append(
            (
                row_id,
                sender_key,
                sender_id.strip(),
                sender_name,
                created_at,
                semantic_content,
                reply_evidence,
                mention_evidence,
                match,
            )
        )

    selected_rows = tuple(admitted_rows[-bounded_max:])
    # Raw relation IDs are deny-only evidence.  Source admission above remains
    # independent of relation shape/identity; only after the final rows are
    # selected do all of their raw Reply/At IDs join the one request-wide
    # display-name privacy set, before any platform display is projected.
    row_relation_identity_literals: list[str] = []
    for (
        _row_id,
        _sender_key,
        _sender_id,
        _sender_name,
        _created_at,
        _semantic_content,
        reply_evidence,
        mention_evidence,
        _match,
    ) in selected_rows:
        if reply_evidence is not None:
            row_relation_identity_literals.append(reply_evidence[0])
        row_relation_identity_literals.extend(
            raw_target_id for raw_target_id, _display_name in mention_evidence
        )
    protected_identity_literals = identity_literals_for_sender_keys(
        protected_sender_keys,
        extra_literals=row_relation_identity_literals,
    )

    records: list[LedgerRecord] = []
    for (
        row_id,
        sender_key,
        sender_id,
        sender_name,
        created_at,
        semantic_content,
        reply_evidence,
        mention_evidence,
        match,
    ) in selected_rows:
        identity_metadata = _platform_identity_metadata(
            match,
            scope_key=normalized_scope,
            sender_display_name=sender_name,
            protected_identity_literals=protected_identity_literals,
            reply_evidence=reply_evidence,
            mention_evidence=mention_evidence,
        )
        records.append(
            LedgerRecord(
                sequence=row_id,
                source_kind=LedgerSourceKind.PLATFORM_GROUP_HISTORY,
                role=LedgerRole.USER,
                scope_key=normalized_scope,
                session_id=normalized_session,
                message_id=f"platform-history:{row_id}",
                sender_key=sender_key,
                reply_to_message_id=match.reply_to_message_id,
                referenced_sender_key=match.referenced_sender_key,
                target_sender_key=match.target_sender_key,
                timestamp=created_at,
                content=semantic_content,
                content_digest=ledger_content_digest(semantic_content),
                source_id=match.message_id,
                attribution_status="verified_platform_sender_and_admission",
                platform_id=match.platform_id,
                sender_id=sender_id,
                referenced_sender_id=(
                    reply_evidence[0] if reply_evidence is not None else ""
                ),
                mention_sender_ids=tuple(
                    raw_target_id
                    for raw_target_id, _display_name in mention_evidence
                    if raw_target_id
                ),
                bot_id=match.bot_id,
                group_id=match.group_id,
                unified_msg_origin=match.unified_msg_origin,
                identity_metadata=identity_metadata,
            )
        )
    selected = tuple(records)
    return PlatformGroupHistoryRead(
        PlatformGroupHistoryStatus.VERIFIED
        if selected
        else PlatformGroupHistoryStatus.EMPTY,
        selected,
        tuple(record.source_id for record in selected),
        "verified" if selected else "no_admission_reconciled_rows",
    )


def _history_field(item: object, key: str, default: object = "") -> object:
    if isinstance(item, dict):
        value = item.get(key, default)
        metadata = item.get("metadata", {})
    else:
        value = getattr(item, key, default)
        metadata = getattr(item, "metadata", {})
    if value not in (None, ""):
        return value
    if isinstance(metadata, dict):
        return metadata.get(key, default)
    return default


def _history_content(item: object) -> str:
    value = _history_field(item, "content", "")
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value or "")


def _validated_history_sender_key(
    *,
    explicit_key: str,
    sender_id: str,
    scope_key: str,
    scope_matches: bool,
) -> str:
    if not scope_matches:
        return ""
    normalized_explicit = str(explicit_key or "").strip()
    if normalized_explicit.startswith(f"{scope_key}|user:"):
        return normalized_explicit
    return build_sender_key(scope_key, sender_id)


def adapt_astrbot_history(
    contexts: Iterable[object],
    *,
    scope_key: str,
    session_id: str,
    group_id: str = "",
    bot_sender_key: str = "",
) -> tuple[LedgerRecord, ...]:
    """Read legacy history without inferring assistant targets by adjacency."""

    normalized_scope = str(scope_key or "").strip()
    if not normalized_scope:
        return ()
    records: list[LedgerRecord] = []
    for item in contexts or ():
        raw_role = str(_history_field(item, "role", "") or "").strip().lower()
        if raw_role in {"user", "human"}:
            role = LedgerRole.USER
        elif raw_role in {"assistant", "ai"}:
            role = LedgerRole.ASSISTANT
        elif raw_role in {"tool", "function"}:
            role = LedgerRole.TOOL
        else:
            continue

        item_group_id = str(_history_field(item, "group_id", "") or "").strip()
        expected_group_id = str(group_id or "").strip()
        scope_matches = not (
            expected_group_id
            and item_group_id
            and item_group_id != expected_group_id
            and item_group_id.rsplit(":", 1)[-1] != expected_group_id
        )
        sender_id = str(_history_field(item, "sender_id", "") or "").strip()
        sender_key = ""
        target_sender_key = ""
        if role == LedgerRole.USER:
            sender_key = _validated_history_sender_key(
                explicit_key=str(_history_field(item, "sender_key", "") or ""),
                sender_id=sender_id,
                scope_key=normalized_scope,
                scope_matches=scope_matches,
            )
            if not scope_matches:
                attribution_status = "scope_mismatch"
            elif sender_key:
                attribution_status = "verified_sender"
            else:
                attribution_status = "unknown_sender"
        elif role == LedgerRole.ASSISTANT:
            sender_key = str(bot_sender_key or "").strip() if scope_matches else ""
            target_sender_key = _validated_history_sender_key(
                explicit_key=str(
                    _history_field(item, "target_sender_key", "") or ""
                ),
                sender_id=str(
                    _history_field(item, "target_sender_id", "") or ""
                ),
                scope_key=normalized_scope,
                scope_matches=scope_matches,
            )
            if not scope_matches:
                attribution_status = "scope_mismatch"
            elif target_sender_key:
                attribution_status = "verified_target"
            else:
                attribution_status = "unknown_target"
        else:
            attribution_status = "legacy_tool_result"

        content = _history_content(item)
        message_id = str(_history_field(item, "message_id", "") or "").strip()
        try:
            timestamp = max(
                0.0,
                float(_history_field(item, "timestamp", 0) or 0),
            )
        except (TypeError, ValueError):
            timestamp = 0.0
        records.append(
            LedgerRecord(
                sequence=len(records) + 1,
                source_kind=LedgerSourceKind.LEGACY_HISTORY,
                role=role,
                scope_key=normalized_scope,
                session_id=str(session_id or "").strip(),
                message_id=message_id,
                sender_key=sender_key,
                reply_to_message_id=str(
                    _history_field(item, "reply_to_message_id", "") or ""
                ).strip(),
                referenced_sender_key="",
                target_sender_key=target_sender_key,
                timestamp=timestamp,
                content=content,
                content_digest=ledger_content_digest(content),
                source_id=message_id,
                attribution_status=attribution_status,
            )
        )
    return tuple(records)


def records_for_sender_thread(
    records: Iterable[LedgerRecord],
    sender_key: str,
) -> tuple[LedgerRecord, ...]:
    """Select only turns explicitly authored by or targeted to one sender."""

    normalized_sender = str(sender_key or "").strip()
    if not normalized_sender:
        return ()
    selected: list[LedgerRecord] = []
    for record in records:
        if record.role == LedgerRole.USER and record.sender_key == normalized_sender:
            selected.append(record)
        elif (
            record.role == LedgerRole.ASSISTANT
            and record.target_sender_key == normalized_sender
        ):
            selected.append(record)
    return tuple(selected)
