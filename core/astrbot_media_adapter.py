from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import (
    DecisionBinding,
    MediaAvailability,
    MediaContext,
    MediaItem,
    MediaKind,
    MediaOrigin,
)
from .identity import build_sender_key


_DIRECT_CAPTION = re.compile(
    r"<image_caption>\s*(.*?)\s*</image_caption>",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED_CAPTION = re.compile(
    r"\[Image Caption in quoted message\]\s*:\s*(.*?)(?=\n|$)",
    re.IGNORECASE,
)
_UNSAFE_LOCATOR = re.compile(
    r"(?:https?://|file://|base64://|data:[^\s;,]+;base64,|"
    r"[a-zA-Z]:[\\/]|\\\\[^\\\s]+[\\/]|(?:^|\s)/(?:[^/\s]+/)+[^\s<>]+)",
    re.IGNORECASE,
)


# AstrBot may expose an image/audio-only turn with no textual outline.  The
# typed pipeline still needs a non-empty, deterministic current-message value
# before the later native-media adapter can bind its opaque transport.  This
# code-owned descriptor contains no caller locator or caption and is issued
# only when the native message chain itself proves media is present.
MEDIA_ONLY_CURRENT_MESSAGE = "[仅媒体消息]"


@dataclass(frozen=True, slots=True)
class _SourceSlot:
    kind: MediaKind
    origin: MediaOrigin
    source_message_id: str
    source_sender_key: str
    reference_message_id: str = ""
    reference_sender_key: str = ""


@dataclass(frozen=True, slots=True, repr=False)
class NativeMediaTransport:
    """Opaque native payload retained only for Provider transport and repair.

    Raw locators deliberately live outside :class:`MediaContext`.  The custom
    repr and trace metadata expose counts only so routine diagnostics cannot
    accidentally print URLs, paths, base64 payloads, or native captions.
    """

    image_urls: tuple[str, ...]
    audio_urls: tuple[str, ...]
    safe_prompt_evidence: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "NativeMediaTransport("
            f"image_count={len(self.image_urls)}, "
            f"audio_count={len(self.audio_urls)}, "
            f"prompt_evidence_count={len(self.safe_prompt_evidence)})"
        )

    def trace_metadata(self) -> dict[str, int]:
        return {
            "media_transport_image_count": len(self.image_urls),
            "media_transport_audio_count": len(self.audio_urls),
            "media_prompt_evidence_count": len(self.safe_prompt_evidence),
        }

    def restore_request_media(self, provider_request: Any) -> Any:
        """Restore exactly the arrays observed on the original native request."""

        if provider_request is None:
            raise ValueError("provider_request_required")
        provider_request.image_urls = list(self.image_urls)
        provider_request.audio_urls = list(self.audio_urls)
        return provider_request


@dataclass(frozen=True, slots=True, repr=False)
class AstrBotMediaAdaptation:
    context: MediaContext
    transport: NativeMediaTransport

    def __repr__(self) -> str:
        return (
            "AstrBotMediaAdaptation("
            f"media_count={len(self.context.items)}, "
            f"degraded={bool(self.context.degradation_reasons)}, "
            f"image_count={len(self.transport.image_urls)}, "
            f"audio_count={len(self.transport.audio_urls)})"
        )

    def for_repair(self) -> "AstrBotMediaAdaptation":
        """Return the immutable evidence and native arrays for one repair call."""

        return self

    def restore_request_media(self, provider_request: Any) -> Any:
        return self.transport.restore_request_media(provider_request)

    def trace_metadata(self) -> dict[str, int | bool]:
        return {
            **self.context.trace_metadata(),
            **self.transport.trace_metadata(),
        }


def _component_token(component: Any) -> str:
    raw = getattr(component, "type", "")
    value = getattr(raw, "value", raw)
    name = getattr(raw, "name", "")
    candidates = (
        str(value or "").strip().lower(),
        str(name or "").strip().lower(),
        type(component).__name__.strip().lower(),
    )
    return "|".join(candidate for candidate in candidates if candidate)


def _is_image(component: Any) -> bool:
    token = _component_token(component)
    return any(part in {"image", "componenttype.image"} for part in token.split("|"))


def _is_audio(component: Any) -> bool:
    token = _component_token(component)
    return any(
        part in {"audio", "record", "componenttype.audio", "componenttype.record"}
        for part in token.split("|")
    )


def _is_reply(component: Any) -> bool:
    token = _component_token(component)
    return any(part in {"reply", "componenttype.reply"} for part in token.split("|"))


def _message_components(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    message_obj = getattr(value, "message_obj", None)
    if message_obj is not None:
        message = getattr(message_obj, "message", None)
        if isinstance(message, Sequence):
            return tuple(message)
    message = getattr(value, "message", None)
    if isinstance(message, Sequence) and not isinstance(message, (str, bytes, bytearray)):
        return tuple(message)
    chain = getattr(value, "chain", None)
    if isinstance(chain, Sequence) and not isinstance(chain, (str, bytes, bytearray)):
        return tuple(chain)
    return ()


def media_only_current_message(message_chain: Any) -> str:
    """Return the safe canonical descriptor for a text-empty native media turn.

    A direct image/audio component is sufficient.  A quoted component is
    sufficient only when its embedded native chain contains image/audio.  The
    helper deliberately ignores provider URL arrays because those are not
    available at ingress and are not source authority.
    """

    components = _message_components(message_chain)
    for component in components:
        if _is_image(component) or _is_audio(component):
            return MEDIA_ONLY_CURRENT_MESSAGE
        if _is_reply(component) and any(
            _is_image(item) or _is_audio(item)
            for item in _message_components(component)
        ):
            return MEDIA_ONLY_CURRENT_MESSAGE
    return ""


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        text = part.get("text")
        if text is None and isinstance(part.get("data"), dict):
            text = part["data"].get("text")
        return str(text or "")
    return str(getattr(part, "text", "") or "")


def _request_values(provider_request: Any, field: str) -> tuple[str, ...]:
    values = getattr(provider_request, field, ()) if provider_request is not None else ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    return tuple(str(value) for value in values)


def _reply_source(
    binding: DecisionBinding,
    reply: Any,
    sender_key_resolver: Callable[[str], str] | None,
) -> tuple[str, str]:
    message_id = str(getattr(reply, "id", "") or "").strip()
    sender_id = str(getattr(reply, "sender_id", "") or "").strip()
    if sender_id == "0":
        sender_id = ""
    if not message_id or not sender_id:
        return "", ""
    sender_key = (
        str(sender_key_resolver(sender_id) or "").strip()
        if sender_key_resolver is not None
        else build_sender_key(binding.scope_key, sender_id)
    )
    return message_id, sender_key


def _slot_for_direct(binding: DecisionBinding, kind: MediaKind) -> _SourceSlot:
    return _SourceSlot(
        kind=kind,
        origin=MediaOrigin.DIRECT,
        source_message_id=binding.current_message_id,
        source_sender_key=binding.current_sender_key,
    )


def _slot_for_quote(
    kind: MediaKind,
    origin: MediaOrigin,
    message_id: str,
    sender_key: str,
) -> _SourceSlot:
    return _SourceSlot(
        kind=kind,
        origin=origin,
        source_message_id=message_id,
        source_sender_key=sender_key,
        reference_message_id=message_id,
        reference_sender_key=sender_key,
    )


def _safe_caption(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or _UNSAFE_LOCATOR.search(normalized):
        return ""
    return normalized


def _captions(provider_request: Any) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    direct: list[str] = []
    quoted: list[str] = []
    failed = False
    parts = getattr(provider_request, "extra_user_content_parts", ()) or ()
    for part in parts:
        text = _part_text(part)
        if not text:
            continue
        failed = failed or "[Image Captioning Failed]" in text
        direct.extend(
            caption
            for match in _DIRECT_CAPTION.finditer(text)
            if (caption := _safe_caption(match.group(1)))
        )
        quoted.extend(
            caption
            for match in _QUOTED_CAPTION.finditer(text)
            if (caption := _safe_caption(match.group(1)))
        )
    return tuple(direct), tuple(quoted), failed


def _quoted_image_attachment_count(provider_request: Any) -> int:
    parts = getattr(provider_request, "extra_user_content_parts", ()) or ()
    return sum(
        _part_text(part).count("[Image Attachment in quoted message:")
        for part in parts
    )


def _item_id(
    binding: DecisionBinding,
    slot: _SourceSlot,
    provider_index: int,
) -> str:
    material = "\x1f".join(
        (
            binding.current_message_id,
            str(binding.conversation_revision),
            slot.kind.value,
            slot.origin.value,
            str(provider_index),
            slot.source_message_id,
            slot.source_sender_key,
        )
    )
    return f"media-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def adapt_astrbot_media(
    *,
    binding: DecisionBinding,
    provider_request: Any,
    message_chain: Any,
    sender_key_resolver: Callable[[str], str] | None = None,
) -> AstrBotMediaAdaptation:
    """Bind AstrBot-native media to typed, locator-free evidence.

    AstrBot remains responsible for download, compression, quoted-message
    lookup, fallback extraction, captioning, and Provider conversion.  This
    adapter only aligns that native result with the immutable current turn and
    preserves its arrays in an opaque transport for the original generation or
    one repair attempt.
    """

    if not isinstance(binding, DecisionBinding):
        raise TypeError("decision_binding_required")
    if provider_request is None:
        raise ValueError("provider_request_required")

    image_urls = _request_values(provider_request, "image_urls")
    audio_urls = _request_values(provider_request, "audio_urls")
    components = _message_components(message_chain)

    direct_images: list[_SourceSlot] = []
    direct_audio: list[_SourceSlot] = []
    replies: list[tuple[str, str, tuple[Any, ...]]] = []
    degradation: list[str] = []

    for component in components:
        if _is_image(component):
            direct_images.append(_slot_for_direct(binding, MediaKind.IMAGE))
        elif _is_audio(component):
            direct_audio.append(_slot_for_direct(binding, MediaKind.AUDIO))
        elif _is_reply(component):
            message_id, sender_key = _reply_source(
                binding,
                component,
                sender_key_resolver,
            )
            if not message_id or not sender_key:
                degradation.append("quoted_media_source_unresolved")
                continue
            replies.append((message_id, sender_key, _message_components(component)))

    quoted_images: list[_SourceSlot] = []
    quoted_audio: list[_SourceSlot] = []
    fallback_candidates: list[tuple[str, str]] = []
    for message_id, sender_key, reply_chain in replies:
        embedded_images = [item for item in reply_chain if _is_image(item)]
        embedded_audio = [item for item in reply_chain if _is_audio(item)]
        quoted_images.extend(
            _slot_for_quote(
                MediaKind.IMAGE,
                MediaOrigin.QUOTED,
                message_id,
                sender_key,
            )
            for _ in embedded_images
        )
        quoted_audio.extend(
            _slot_for_quote(
                MediaKind.AUDIO,
                MediaOrigin.QUOTED,
                message_id,
                sender_key,
            )
            for _ in embedded_audio
        )
        if not embedded_images:
            fallback_candidates.append((message_id, sender_key))

    image_slots = [*direct_images, *quoted_images]
    extra_image_count = max(0, len(image_urls) - len(image_slots))
    if extra_image_count:
        quoted_attachment_count = _quoted_image_attachment_count(provider_request)
        if (
            len(fallback_candidates) == 1
            and quoted_attachment_count >= extra_image_count
        ):
            message_id, sender_key = fallback_candidates[0]
            image_slots.extend(
                _slot_for_quote(
                    MediaKind.IMAGE,
                    MediaOrigin.QUOTED_FALLBACK,
                    message_id,
                    sender_key,
                )
                for _ in range(extra_image_count)
            )
        else:
            degradation.append("provider_image_source_unresolved")

    audio_slots = [*direct_audio, *quoted_audio]
    if len(audio_urls) > len(audio_slots):
        degradation.append("provider_audio_source_unresolved")

    direct_captions, quoted_captions, caption_failed = _captions(provider_request)
    all_slots = [*image_slots, *audio_slots]
    missing_image_indexes = [
        index
        for index, slot in enumerate(image_slots)
        if index >= len(image_urls) and slot.kind is MediaKind.IMAGE
    ]
    caption_by_index: dict[int, str] = {}

    quoted_missing = [
        index
        for index in missing_image_indexes
        if image_slots[index].origin is not MediaOrigin.DIRECT
    ]
    if quoted_captions and len(quoted_captions) == len(quoted_missing):
        caption_by_index.update(zip(quoted_missing, quoted_captions, strict=True))

    remaining_missing = [index for index in missing_image_indexes if index not in caption_by_index]
    if direct_captions and len(direct_captions) == len(remaining_missing):
        caption_by_index.update(zip(remaining_missing, direct_captions, strict=True))
    elif direct_captions and len(remaining_missing) == 1:
        caption_by_index[remaining_missing[0]] = direct_captions[0]
    elif (direct_captions or quoted_captions) and remaining_missing:
        degradation.append("native_caption_ambiguous")

    items: list[MediaItem] = []
    safe_prompt_evidence: list[str] = []
    for provider_index, slot in enumerate(all_slots):
        if slot.kind is MediaKind.IMAGE:
            kind_index = provider_index
            has_raw_transport = kind_index < len(image_urls)
        else:
            kind_index = provider_index - len(image_slots)
            has_raw_transport = kind_index < len(audio_urls)

        caption = caption_by_index.get(provider_index, "")
        if has_raw_transport:
            availability = MediaAvailability.RAW_MEDIA
            native_caption_digest = ""
            safe_prompt_evidence.append(
                f"{slot.origin.value}-{slot.kind.value}-attached"
            )
        elif caption:
            availability = MediaAvailability.NATIVE_CAPTION
            native_caption_digest = hashlib.sha256(caption.encode("utf-8")).hexdigest()
            safe_prompt_evidence.append(caption)
        else:
            availability = MediaAvailability.UNAVAILABLE
            native_caption_digest = ""
            safe_prompt_evidence.append("media-unavailable")

        items.append(
            MediaItem(
                item_id=_item_id(binding, slot, provider_index),
                kind=slot.kind,
                origin=slot.origin,
                provider_index=provider_index,
                source_message_id=slot.source_message_id,
                source_sender_key=slot.source_sender_key,
                availability=availability,
                reference_message_id=slot.reference_message_id,
                reference_sender_key=slot.reference_sender_key,
                native_caption_digest=native_caption_digest,
            )
        )

    if any(item.availability is MediaAvailability.UNAVAILABLE for item in items):
        degradation.append("native_media_unavailable")
    if caption_failed and not items:
        degradation.append("native_media_unavailable")

    context = MediaContext(
        binding=binding,
        items=tuple(items),
        degradation_reasons=tuple(dict.fromkeys(degradation)),
    )
    transport = NativeMediaTransport(
        image_urls=image_urls,
        audio_urls=audio_urls,
        safe_prompt_evidence=tuple(dict.fromkeys(safe_prompt_evidence)),
    )
    return AstrBotMediaAdaptation(context=context, transport=transport)


__all__ = [
    "AstrBotMediaAdaptation",
    "MEDIA_ONLY_CURRENT_MESSAGE",
    "NativeMediaTransport",
    "adapt_astrbot_media",
    "media_only_current_message",
]
