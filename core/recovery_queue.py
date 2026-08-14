from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class PendingReply:
    id: str
    dedupe_key: str
    unified_msg_origin: str
    platform_id: str
    bot_id: str
    chat_type: str
    group_id: str
    sender_id: str
    sender_name: str
    message_id: str
    current_message: str
    failed_draft: str = ""
    contexts: list[dict[str, str]] = field(default_factory=list)
    reply_shape: str = "chat_bubbles"
    created_at: float = 0.0
    next_retry_at: float = 0.0
    expires_at: float = 0.0
    attempts: int = 0
    failure_reason: str = ""


class PendingReplyStore:
    """持久化待补答队列；只保存必要文本，不保存工具、密钥或附件。"""

    def __init__(
        self,
        data_dir: Path,
        logger: Any,
        *,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.logger = logger
        self.now_fn = now_fn or time.time
        self.path = Path(data_dir) / "pending_replies.json"
        self.items: dict[str, PendingReply] = {}
        self._load()

    @staticmethod
    def make_dedupe_key(
        *,
        platform_id: str,
        bot_id: str,
        chat_type: str,
        group_id: str,
        sender_id: str,
        message_id: str,
        current_message: str,
    ) -> str:
        raw = "\x1f".join(
            (
                platform_id,
                bot_id,
                chat_type,
                group_id,
                sender_id,
                message_id or current_message.strip(),
            )
        )
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    def enqueue(
        self,
        *,
        unified_msg_origin: str,
        platform_id: str,
        bot_id: str,
        chat_type: str,
        group_id: str,
        sender_id: str,
        sender_name: str,
        message_id: str,
        current_message: str,
        contexts: list[dict[str, str]],
        reply_shape: str,
        initial_delay_seconds: float,
        ttl_seconds: float,
        failure_reason: str,
        failed_draft: str = "",
        max_items: int = 100,
    ) -> tuple[PendingReply, bool]:
        now = float(self.now_fn())
        self.prune(now=now, flush=False)
        dedupe_key = self.make_dedupe_key(
            platform_id=platform_id,
            bot_id=bot_id,
            chat_type=chat_type,
            group_id=group_id,
            sender_id=sender_id,
            message_id=message_id,
            current_message=current_message,
        )
        for item in self.items.values():
            if item.dedupe_key == dedupe_key:
                item.failure_reason = str(failure_reason or item.failure_reason)[:500]
                item.expires_at = max(item.expires_at, now + max(60.0, ttl_seconds))
                self.flush()
                return item, False

        if len(self.items) >= max(1, int(max_items)):
            oldest = min(self.items.values(), key=lambda entry: entry.created_at)
            self.items.pop(oldest.id, None)
        item = PendingReply(
            id=uuid.uuid4().hex,
            dedupe_key=dedupe_key,
            unified_msg_origin=str(unified_msg_origin or ""),
            platform_id=str(platform_id or ""),
            bot_id=str(bot_id or ""),
            chat_type=str(chat_type or "private"),
            group_id=str(group_id or ""),
            sender_id=str(sender_id or ""),
            sender_name=str(sender_name or sender_id or "当前说话者"),
            message_id=str(message_id or ""),
            current_message=str(current_message or "").strip()[:4000],
            failed_draft=str(failed_draft or "").strip()[:4000],
            contexts=list(contexts or []),
            reply_shape=("long_form" if reply_shape == "long_form" else "chat_bubbles"),
            created_at=now,
            next_retry_at=now + max(1.0, float(initial_delay_seconds)),
            expires_at=now + max(60.0, float(ttl_seconds)),
            attempts=0,
            failure_reason=str(failure_reason or "")[:500],
        )
        self.items[item.id] = item
        self.flush()
        return item, True

    def due(self, *, limit: int = 1, now: float | None = None) -> list[PendingReply]:
        current = float(self.now_fn() if now is None else now)
        self.prune(now=current)
        result = [
            item
            for item in self.items.values()
            if item.next_retry_at <= current < item.expires_at
        ]
        result.sort(key=lambda item: (item.next_retry_at, item.created_at))
        return result[: max(1, int(limit))]

    def mark_failed(
        self,
        item_id: str,
        *,
        reason: str,
        delays_seconds: list[float],
        max_attempts: int,
    ) -> None:
        item = self.items.get(item_id)
        if item is None:
            return
        now = float(self.now_fn())
        item.attempts += 1
        item.failure_reason = str(reason or "")[:500]
        if item.attempts >= max(1, int(max_attempts)) or now >= item.expires_at:
            self.items.pop(item_id, None)
        else:
            delays = [max(1.0, float(value)) for value in delays_seconds] or [60.0]
            delay = delays[min(item.attempts - 1, len(delays) - 1)]
            item.next_retry_at = now + delay
        self.flush()

    def complete(self, item_id: str) -> None:
        if self.items.pop(item_id, None) is not None:
            self.flush()

    def expedite(self, *, now: float | None = None) -> None:
        current = float(self.now_fn() if now is None else now)
        changed = False
        for item in self.items.values():
            if item.next_retry_at > current:
                item.next_retry_at = current
                changed = True
        if changed:
            self.flush()

    def prune(self, *, now: float | None = None, flush: bool = True) -> None:
        current = float(self.now_fn() if now is None else now)
        expired = [item_id for item_id, item in self.items.items() if item.expires_at <= current]
        for item_id in expired:
            self.items.pop(item_id, None)
        if expired and flush:
            self.flush()

    def flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "pending": [asdict(item) for item in self.items.values()],
            }
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        except Exception as exc:
            self.logger.warning("[星汐/补答] 持久化待补答队列失败：%s", exc)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            for raw in list(payload.get("pending", []) or []):
                if not isinstance(raw, dict):
                    continue
                item = PendingReply(**raw)
                if item.id and item.current_message and item.expires_at > self.now_fn():
                    self.items[item.id] = item
        except Exception as exc:
            self.logger.warning("[星汐/补答] 读取待补答队列失败，已从空队列继续：%s", exc)
