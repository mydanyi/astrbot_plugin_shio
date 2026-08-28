from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TURN_ENVELOPE_EXTRA = "_shio_turn_envelope"
PRINCIPAL_CONTEXT_EXTRA = "_shio_principal_context"


def _identifier(value: Any) -> str:
    if value in (None, "", 0, "0"):
        return ""
    return str(value).strip()


def _safe_call(event: Any, method_name: str) -> Any:
    method = getattr(event, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


def _first_identifier(*values: Any) -> str:
    for value in values:
        normalized = _identifier(value)
        if normalized:
            return normalized
    return ""


def build_scope_key(
    *,
    platform_id: str,
    bot_id: str,
    chat_type: str,
    group_id: str = "",
    session_id: str = "",
) -> str:
    """Build one collision-resistant-enough human-readable conversation scope.

    Missing structural identifiers fail closed to an empty key. Callers must
    not replace them with display names or a shared ``unknown`` bucket.
    """

    platform = _identifier(platform_id)
    bot = _identifier(bot_id)
    normalized_chat_type = str(chat_type or "").strip().lower()
    if normalized_chat_type == "group":
        conversation_id = _identifier(group_id)
    elif normalized_chat_type == "private":
        conversation_id = _identifier(session_id)
    else:
        return ""
    if not all((platform, bot, conversation_id)):
        return ""
    return (
        f"platform:{platform}|bot:{bot}|"
        f"{normalized_chat_type}:{conversation_id}"
    )


def build_sender_key(scope_key: str, sender_id: str) -> str:
    scope = str(scope_key or "").strip()
    sender = _identifier(sender_id)
    if not scope or not sender:
        return ""
    return f"{scope}|user:{sender}"


def build_account_key(platform_id: str, sender_id: str) -> str:
    platform = _identifier(platform_id)
    sender = _identifier(sender_id)
    return f"platform:{platform}|account:{sender}" if platform and sender else ""


def _message_type(event: Any, message_obj: Any) -> str:
    raw = _safe_call(event, "get_message_type")
    if raw is None and message_obj is not None:
        raw = getattr(message_obj, "type", None)
    value = getattr(raw, "value", raw)
    return str(value or "").strip().lower()


def _message_chain(event: Any, message_obj: Any) -> list[Any]:
    raw = _safe_call(event, "get_messages")
    if raw is None and message_obj is not None:
        raw = getattr(message_obj, "message", None)
    try:
        return list(raw or [])
    except (TypeError, ValueError):
        return []


def _reply_component(event: Any, message_obj: Any) -> Any | None:
    for component in _message_chain(event, message_obj):
        component_type = getattr(component, "type", None)
        component_type = getattr(component_type, "value", component_type)
        if (
            component.__class__.__name__.lower() == "reply"
            or str(component_type or "").strip().lower() == "reply"
        ):
            if _identifier(getattr(component, "id", "")):
                return component
    return None


@dataclass(frozen=True, slots=True)
class TurnEnvelope:
    """Trusted structural identity for one AstrBot event.

    Display names and message text are intentionally absent: neither is an
    identity source. Empty identifiers remain empty and are explained through
    ``degradation_reasons`` instead of being guessed from adjacent messages.
    """

    session_id: str
    message_id: str
    scope_key: str
    sender_key: str
    sender_id: str
    platform_id: str
    bot_id: str
    chat_type: str
    group_id: str
    reply_to_message_id: str
    reply_to_sender_id: str
    timestamp: float
    timestamp_source: str
    source_kind: str
    degradation_reasons: tuple[str, ...]
    account_key: str = ""

    @property
    def is_degraded(self) -> bool:
        return bool(self.degradation_reasons)

    @property
    def identity_status(self) -> str:
        if self.sender_key:
            return "complete" if not self.is_degraded else "partial"
        return "unavailable"

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        """Return content-free metadata suitable for pipeline diagnostics."""

        return {
            "source_kind": self.source_kind,
            "identity_status": self.identity_status,
            "has_session_id": bool(self.session_id),
            "has_message_id": bool(self.message_id),
            "has_sender_key": bool(self.sender_key),
            "has_reply_to_message_id": bool(self.reply_to_message_id),
            "has_timestamp": self.timestamp > 0,
            "timestamp_source": self.timestamp_source,
            "degradation_count": len(self.degradation_reasons),
        }


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    """Verified relationship for the current structural sender identity."""

    sender_key: str
    sender_id: str
    is_owner: bool
    relationship_role: str
    verification_source: str
    account_key: str = ""


def resolve_principal(
    *,
    sender_id: str,
    sender_key: str,
    account_key: str = "",
    chat_type: str,
    owner_ids: Any,
    verification_source: str,
    identity_verified: bool,
) -> PrincipalContext:
    """Resolve owner/peer status without accepting names or message claims."""

    normalized_sender_id = _identifier(sender_id)
    if isinstance(owner_ids, str):
        normalized_owner_ids = {
            item.strip()
            for item in owner_ids.replace(";", ",").split(",")
            if item.strip()
        }
    else:
        try:
            normalized_owner_ids = {
                normalized
                for item in (owner_ids or [])
                if (normalized := _identifier(item))
            }
        except TypeError:
            normalized_owner_ids = set()

    base_source = str(verification_source or "unverified").strip() or "unverified"
    if not identity_verified or not normalized_sender_id:
        return PrincipalContext(
            sender_key=str(sender_key or ""),
            sender_id=normalized_sender_id,
            is_owner=False,
            relationship_role="unverified",
            verification_source=f"{base_source}:identity_unverified",
            account_key=str(account_key or ""),
        )

    is_owner = normalized_sender_id in normalized_owner_ids
    if is_owner:
        relationship_role = "owner"
        verdict = "configured_owner_id"
    elif str(chat_type or "").strip().lower() == "group":
        relationship_role = "group_peer"
        verdict = "owner_allowlist_miss"
    else:
        relationship_role = "private_peer"
        verdict = "owner_allowlist_miss"
    return PrincipalContext(
        sender_key=str(sender_key or ""),
        sender_id=normalized_sender_id,
        is_owner=is_owner,
        relationship_role=relationship_role,
        verification_source=f"{base_source}:{verdict}",
        account_key=str(account_key or ""),
    )


def build_turn_envelope(event: Any, *, source_kind: str = "inbound") -> TurnEnvelope:
    """Extract only structured, adapter-provided identity from an event."""

    message_obj = getattr(event, "message_obj", None)
    sender_obj = getattr(message_obj, "sender", None) if message_obj is not None else None

    session_id = _first_identifier(
        _safe_call(event, "get_session_id"),
        getattr(event, "session_id", None),
        getattr(message_obj, "session_id", None),
    )
    message_id = _first_identifier(
        getattr(message_obj, "message_id", None),
        getattr(event, "message_id", None),
    )
    sender_id = _first_identifier(
        _safe_call(event, "get_sender_id"),
        getattr(sender_obj, "user_id", None),
    )
    platform_id = _first_identifier(_safe_call(event, "get_platform_id"))
    bot_id = _first_identifier(
        _safe_call(event, "get_self_id"),
        getattr(message_obj, "self_id", None),
    )
    group_id = _first_identifier(
        _safe_call(event, "get_group_id"),
        getattr(message_obj, "group_id", None),
    )

    message_type = _message_type(event, message_obj)
    if group_id or "group" in message_type:
        chat_type = "group"
    elif "friend" in message_type or "private" in message_type:
        chat_type = "private"
    else:
        chat_type = "unknown"

    reply = _reply_component(event, message_obj)
    reply_to_message_id = _identifier(getattr(reply, "id", "")) if reply else ""
    reply_to_sender_id = (
        _identifier(getattr(reply, "sender_id", "")) if reply else ""
    )

    timestamp = 0.0
    timestamp_source = "missing"
    for candidate, candidate_source in (
        (getattr(message_obj, "timestamp", None), "message"),
        (getattr(event, "created_at", None), "event_created"),
        (getattr(event, "timestamp", None), "event"),
    ):
        try:
            parsed = float(candidate or 0)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            timestamp = parsed
            timestamp_source = candidate_source
            break

    scope_key = build_scope_key(
        platform_id=platform_id,
        bot_id=bot_id,
        chat_type=chat_type,
        group_id=group_id,
        session_id=session_id,
    )
    sender_key = build_sender_key(scope_key, sender_id)
    account_key = build_account_key(platform_id, sender_id)

    degradation_reasons: list[str] = []
    for value, reason in (
        (session_id, "missing_session_id"),
        (message_id, "missing_message_id"),
        (sender_id, "missing_sender_id"),
        (platform_id, "missing_platform_id"),
        (bot_id, "missing_bot_id"),
    ):
        if not value:
            degradation_reasons.append(reason)
    if chat_type == "unknown":
        degradation_reasons.append("unknown_chat_type")
    if timestamp <= 0:
        degradation_reasons.append("missing_timestamp")
    if not sender_key:
        degradation_reasons.append("sender_key_unavailable")

    return TurnEnvelope(
        session_id=session_id,
        message_id=message_id,
        scope_key=scope_key,
        sender_key=sender_key,
        sender_id=sender_id,
        platform_id=platform_id,
        bot_id=bot_id,
        chat_type=chat_type,
        group_id=group_id,
        reply_to_message_id=reply_to_message_id,
        reply_to_sender_id=reply_to_sender_id,
        timestamp=timestamp,
        timestamp_source=timestamp_source,
        source_kind=str(source_kind or "inbound").strip() or "inbound",
        degradation_reasons=tuple(degradation_reasons),
        account_key=account_key,
    )


def ensure_turn_envelope(event: Any, *, source_kind: str = "inbound") -> TurnEnvelope:
    """Build once per event and keep the typed envelope in event-local state."""

    get_extra = getattr(event, "get_extra", None)
    if callable(get_extra):
        existing = get_extra(TURN_ENVELOPE_EXTRA, None)
        if isinstance(existing, TurnEnvelope):
            return existing
    envelope = build_turn_envelope(event, source_kind=source_kind)
    set_extra = getattr(event, "set_extra", None)
    if callable(set_extra):
        set_extra(TURN_ENVELOPE_EXTRA, envelope)
    return envelope


def build_principal_context(event: Any, owner_ids: Any) -> PrincipalContext:
    envelope = ensure_turn_envelope(event)
    return resolve_principal(
        sender_id=envelope.sender_id,
        sender_key=envelope.sender_key,
        account_key=envelope.account_key,
        chat_type=envelope.chat_type,
        owner_ids=owner_ids,
        verification_source="astrbot_event_sender_id",
        identity_verified=bool(envelope.sender_id),
    )


def ensure_principal_context(event: Any, owner_ids: Any) -> PrincipalContext:
    """Resolve once per event so all hooks use the same owner decision."""

    get_extra = getattr(event, "get_extra", None)
    if callable(get_extra):
        existing = get_extra(PRINCIPAL_CONTEXT_EXTRA, None)
        if isinstance(existing, PrincipalContext):
            return existing
    principal = build_principal_context(event, owner_ids)
    set_extra = getattr(event, "set_extra", None)
    if callable(set_extra):
        set_extra(PRINCIPAL_CONTEXT_EXTRA, principal)
    return principal
