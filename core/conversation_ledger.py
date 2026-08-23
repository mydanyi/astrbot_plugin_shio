from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .identity import TurnEnvelope, build_sender_key


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
    bot_id: str = ""
    group_id: str = ""
    unified_msg_origin: str = ""


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


class ConversationLedger:
    """Bounded typed ledger with an optional restart-safe public inbound tail.

    Only source-verified human inbound records are persisted.  Assistant
    output, references, tool results and third-party legacy history remain
    process-local because they require their original receipt/authority graph.
    """

    _PERSISTENCE_SCHEMA_VERSION = 2
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
            "timestamp": record.timestamp,
            "content": record.content,
            "content_digest": record.content_digest,
            "source_id": record.source_id,
            "attribution_status": record.attribution_status,
            "platform_id": record.platform_id,
            "bot_id": record.bot_id,
            "group_id": record.group_id,
            "unified_msg_origin": record.unified_msg_origin,
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
        expected_keys = base_keys if schema_version == 1 else base_keys | target_keys
        if type(value) is not dict or set(value) != expected_keys:
            raise ValueError("state_invalid")
        normalized = dict(value)
        if schema_version == 1:
            normalized.update({key: "" for key in target_keys})
        value = normalized
        expected_keys = base_keys | target_keys
        text_keys = expected_keys - {"timestamp"}
        if any(type(value[key]) is not str for key in text_keys):
            raise ValueError("state_invalid")
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
                or schema_version not in {1, self._PERSISTENCE_SCHEMA_VERSION}
                or type(payload["records"]) is not list
                or len(payload["records"])
                > self.max_persisted_scopes * self.max_persisted_records_per_scope
            ):
                raise ValueError("state_invalid")
            if schema_version == self._PERSISTENCE_SCHEMA_VERSION:
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
                            b"public-group-ledger-v2\x00" + rows_encoded,
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
                        timestamp=float(value["timestamp"]),
                        content=str(value["content"]),
                        source_id=str(value["source_id"]),
                        attribution_status="verified",
                        platform_id=str(value["platform_id"]),
                        bot_id=str(value["bot_id"]),
                        group_id=str(value["group_id"]),
                        unified_msg_origin=str(value["unified_msg_origin"]),
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
                        b"public-group-ledger-v2\x00" + rows_encoded,
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
        bot_id: str = "",
        group_id: str = "",
        unified_msg_origin: str = "",
    ) -> LedgerRecord:
        with self._lock:
            normalized_scope = str(scope_key or "").strip()
            if not normalized_scope:
                raise ValueError("ledger records require a trusted non-empty scope_key")
            normalized_content = str(content or "")
            record = LedgerRecord(
                sequence=self._next_sequence,
                source_kind=source_kind,
                role=role,
                scope_key=normalized_scope,
                session_id=str(session_id or "").strip(),
                message_id=str(message_id or "").strip(),
                sender_key=str(sender_key or "").strip(),
                reply_to_message_id=str(reply_to_message_id or "").strip(),
                referenced_sender_key=str(referenced_sender_key or "").strip(),
                target_sender_key=str(target_sender_key or "").strip(),
                timestamp=max(0.0, float(timestamp or 0)),
                content=normalized_content,
                content_digest=ledger_content_digest(normalized_content),
                source_id=str(source_id or "").strip(),
                attribution_status=str(attribution_status or "unknown").strip()
                or "unknown",
                platform_id=str(platform_id or "").strip(),
                bot_id=str(bot_id or "").strip(),
                group_id=str(group_id or "").strip(),
                unified_msg_origin=str(unified_msg_origin or "").strip(),
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
                    return existing
        return self._append(
            source_kind=LedgerSourceKind.INBOUND,
            role=LedgerRole.USER,
            scope_key=envelope.scope_key,
            session_id=envelope.session_id,
            message_id=envelope.message_id,
            sender_key=envelope.sender_key,
            reply_to_message_id=envelope.reply_to_message_id,
            timestamp=envelope.timestamp,
            content=content,
            source_id=envelope.message_id,
            platform_id=envelope.platform_id,
            bot_id=envelope.bot_id,
            group_id=envelope.group_id,
            unified_msg_origin=unified_msg_origin,
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
                ):
                    raise ValueError("restored_record_corrupt")
            return restored

    def record_reference(
        self,
        envelope: TurnEnvelope,
        *,
        referenced_content: str = "",
        referenced_sender_key: str = "",
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


def _platform_history_text(content: object) -> str:
    """Render AstrBot's public path-free message-chain representation."""

    if type(content) is not dict or set(content) != {"type", "message"}:
        return ""
    if content.get("type") != "user" or type(content.get("message")) is not list:
        return ""
    rendered: list[str] = []
    for part in content["message"]:
        if type(part) is not dict or type(part.get("type")) is not str:
            return ""
        kind = part["type"]
        if kind == "plain":
            if type(part.get("text")) is not str:
                return ""
            text = part["text"]
        elif kind == "at":
            name = part.get("name", "")
            if type(name) is not str:
                return ""
            text = f"@{name.strip() or '群成员'}"
        elif kind == "reply":
            sender_name = part.get("sender_name", "")
            reply_text = part.get("text", "")
            if type(sender_name) is not str or type(reply_text) is not str:
                return ""
            sender_label = sender_name.strip()[:40]
            bounded_reply = reply_text.strip()[:500]
            text = "[回复"
            if sender_label:
                text += f" {sender_label}"
            if bounded_reply:
                text += f"：{bounded_reply}"
            text += "]"
        else:
            text = "[消息]"
        if text:
            rendered.append(text)
    return "".join(rendered).strip()[:2000]


def _safe_platform_sender_name(value: object) -> str:
    if type(value) is not str:
        return "群成员"
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    return normalized[:60] or "群成员"


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

    accepted: dict[tuple[str, str], list[LedgerRecord]] = {}
    for record in tuple(accepted_records or ()):
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
    records: list[LedgerRecord] = []
    for row in rows:
        row_id = getattr(row, "id", None)
        row_platform = getattr(row, "platform_id", None)
        row_user = getattr(row, "user_id", None)
        sender_id = getattr(row, "sender_id", None)
        created_at = _platform_history_timestamp(getattr(row, "created_at", None))
        raw_content = _platform_history_text(getattr(row, "content", None))
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
            or created_at <= 0
            or not raw_content
        ):
            continue
        sender_key = build_sender_key(normalized_scope, sender_id.strip())
        candidates = accepted.get(
            (sender_key, ledger_content_digest(raw_content)),
            (),
        )
        match = min(
            (
                candidate
                for candidate in candidates
                if candidate.message_id not in matched_ids
                and abs(candidate.timestamp - created_at) <= 180.0
            ),
            key=lambda candidate: abs(candidate.timestamp - created_at),
            default=None,
        )
        if match is None:
            continue
        matched_ids.add(match.message_id)
        sender_name = _safe_platform_sender_name(
            getattr(row, "sender_name", None)
        )
        visible_content = f"{sender_name}：{raw_content}"
        records.append(
            LedgerRecord(
                sequence=row_id,
                source_kind=LedgerSourceKind.PLATFORM_GROUP_HISTORY,
                role=LedgerRole.USER,
                scope_key=normalized_scope,
                session_id=normalized_session,
                message_id=f"platform-history:{row_id}",
                sender_key=sender_key,
                reply_to_message_id="",
                referenced_sender_key="",
                target_sender_key="",
                timestamp=created_at,
                content=visible_content,
                content_digest=ledger_content_digest(visible_content),
                source_id=match.message_id,
                attribution_status="verified_platform_sender_and_admission",
            )
        )
    selected = tuple(records[-bounded_max:])
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
