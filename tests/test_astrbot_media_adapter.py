from __future__ import annotations

import hashlib
import json
import types
import unittest

from astrbot_plugin_shio.core.astrbot_media_adapter import (
    MEDIA_ONLY_CURRENT_MESSAGE,
    adapt_astrbot_media,
    media_only_current_message,
)
from astrbot_plugin_shio.core.contracts import (
    DecisionBinding,
    MediaAvailability,
    MediaKind,
    MediaOrigin,
)


SCOPE = "platform:test|bot:shio|group:group-a"
CURRENT_SENDER = f"{SCOPE}|user:peer-a"
QUOTED_SENDER = f"{SCOPE}|user:peer-b"


def binding() -> DecisionBinding:
    return DecisionBinding(
        scope_key=SCOPE,
        session_id="group-a",
        current_message_id="message-current",
        current_sender_key=CURRENT_SENDER,
        current_content_digest="a" * 64,
        conversation_revision=7,
        generation_epoch=3,
        trace_id="b" * 32,
    )


class Plain:
    type = "Plain"

    def __init__(self, text: str) -> None:
        self.text = text


class Image:
    type = "Image"


class Record:
    type = "Record"


class Reply:
    type = "Reply"

    def __init__(
        self,
        reply_id: str,
        sender_id: str,
        chain: list[object] | None = None,
    ) -> None:
        self.id = reply_id
        self.sender_id = sender_id
        self.chain = list(chain or [])


class TextPart:
    def __init__(self, text: str) -> None:
        self.text = text


def request(
    *,
    images: tuple[str, ...] = (),
    audio: tuple[str, ...] = (),
    parts: tuple[object, ...] = (),
):
    return types.SimpleNamespace(
        image_urls=list(images),
        audio_urls=list(audio),
        extra_user_content_parts=list(parts),
    )


class AstrBotMediaAdapterTests(unittest.TestCase):
    def test_media_only_current_message_requires_native_media_evidence(self):
        self.assertEqual(media_only_current_message([Image()]), MEDIA_ONLY_CURRENT_MESSAGE)
        self.assertEqual(media_only_current_message([Record()]), MEDIA_ONLY_CURRENT_MESSAGE)
        self.assertEqual(
            media_only_current_message([Reply("quoted", "peer-b", [Image()])]),
            MEDIA_ONLY_CURRENT_MESSAGE,
        )
        self.assertEqual(media_only_current_message([]), "")
        self.assertEqual(media_only_current_message([Plain("not media")]), "")
        self.assertEqual(media_only_current_message([Reply("quoted", "peer-b")]), "")

    def test_direct_text_image_and_image_only_bind_to_current_turn(self):
        for chain in ([Plain("看看这个"), Image()], [Image()]):
            with self.subTest(image_only=len(chain) == 1):
                adaptation = adapt_astrbot_media(
                    binding=binding(),
                    provider_request=request(images=("C:/transport/direct.png",)),
                    message_chain=chain,
                )

                self.assertEqual(len(adaptation.context.items), 1)
                item = adaptation.context.items[0]
                self.assertEqual(item.kind, MediaKind.IMAGE)
                self.assertEqual(item.origin, MediaOrigin.DIRECT)
                self.assertEqual(item.provider_index, 0)
                self.assertEqual(item.source_message_id, "message-current")
                self.assertEqual(item.source_sender_key, CURRENT_SENDER)
                self.assertEqual(item.availability, MediaAvailability.RAW_MEDIA)
                self.assertEqual(
                    adaptation.transport.image_urls,
                    ("C:/transport/direct.png",),
                )

    def test_embedded_quoted_media_keeps_quoted_sender_and_message(self):
        adaptation = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(images=("/transport/quoted.png",)),
            message_chain=[Reply("message-quoted", "peer-b", [Image()])],
        )

        item = adaptation.context.items[0]
        self.assertEqual(item.origin, MediaOrigin.QUOTED)
        self.assertEqual(item.source_message_id, "message-quoted")
        self.assertEqual(item.source_sender_key, QUOTED_SENDER)
        self.assertEqual(item.reference_message_id, "message-quoted")
        self.assertEqual(item.reference_sender_key, QUOTED_SENDER)

    def test_reply_id_only_provider_image_is_bound_as_quoted_fallback(self):
        adaptation = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(
                images=("https://transport.invalid/fallback.png",),
                parts=(
                    TextPart(
                        "[Image Attachment in quoted message: path "
                        "https://transport.invalid/fallback.png]"
                    ),
                ),
            ),
            message_chain=[Reply("message-fallback", "peer-b")],
        )

        item = adaptation.context.items[0]
        self.assertEqual(item.origin, MediaOrigin.QUOTED_FALLBACK)
        self.assertEqual(item.source_message_id, "message-fallback")
        self.assertEqual(item.source_sender_key, QUOTED_SENDER)
        self.assertEqual(item.availability, MediaAvailability.RAW_MEDIA)

    def test_multiple_media_preserve_provider_array_order_and_unique_indexes(self):
        adaptation = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(
                images=("direct-1", "direct-2", "quoted-1"),
                audio=("direct-audio",),
            ),
            message_chain=[
                Image(),
                Record(),
                Image(),
                Reply("message-quoted", "peer-b", [Image()]),
            ],
        )

        self.assertEqual(
            [item.provider_index for item in adaptation.context.items],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [item.kind for item in adaptation.context.items],
            [MediaKind.IMAGE, MediaKind.IMAGE, MediaKind.IMAGE, MediaKind.AUDIO],
        )
        self.assertEqual(
            [item.origin for item in adaptation.context.items],
            [
                MediaOrigin.DIRECT,
                MediaOrigin.DIRECT,
                MediaOrigin.QUOTED,
                MediaOrigin.DIRECT,
            ],
        )
        self.assertEqual(
            adaptation.transport.image_urls,
            ("direct-1", "direct-2", "quoted-1"),
        )
        self.assertEqual(adaptation.transport.audio_urls, ("direct-audio",))

    def test_native_caption_is_digest_only_in_typed_context(self):
        caption = "画面里是一只拿着红伞的小猫。"
        adaptation = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(
                parts=(TextPart(f"<image_caption>{caption}</image_caption>"),),
            ),
            message_chain=[Image()],
        )

        item = adaptation.context.items[0]
        self.assertEqual(item.availability, MediaAvailability.NATIVE_CAPTION)
        self.assertEqual(
            item.native_caption_digest,
            hashlib.sha256(caption.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn(caption, repr(adaptation.context))
        self.assertIn(caption, adaptation.transport.safe_prompt_evidence)

    def test_unavailable_native_media_is_explicit_and_never_invented(self):
        adaptation = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(
                parts=(TextPart("[Image Captioning Failed]"),),
            ),
            message_chain=[Image()],
        )

        item = adaptation.context.items[0]
        self.assertEqual(item.availability, MediaAvailability.UNAVAILABLE)
        self.assertIn("native_media_unavailable", adaptation.context.degradation_reasons)
        self.assertIn("media-unavailable", adaptation.transport.safe_prompt_evidence)

    def test_caption_containing_a_raw_locator_fails_closed_before_prompt_evidence(self):
        unsafe_caption = "缓存图片位于 /private/cache/secret.png"
        adaptation = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(
                parts=(
                    TextPart(f"<image_caption>{unsafe_caption}</image_caption>"),
                ),
            ),
            message_chain=[Image()],
        )

        self.assertEqual(
            adaptation.context.items[0].availability,
            MediaAvailability.UNAVAILABLE,
        )
        self.assertNotIn(unsafe_caption, adaptation.transport.safe_prompt_evidence)
        self.assertNotIn("/private/cache/secret.png", repr(adaptation))

    def test_repair_reuses_same_item_ids_and_exact_transport_arrays(self):
        original = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(
                images=("first-image",),
                audio=("first-audio",),
            ),
            message_chain=[Image(), Record()],
        )
        repair = original.for_repair()
        repair_request = request(images=("wrong",), audio=("wrong",))
        repair.restore_request_media(repair_request)

        self.assertIs(repair.context, original.context)
        self.assertEqual(
            [item.item_id for item in repair.context.items],
            [item.item_id for item in original.context.items],
        )
        self.assertEqual(repair_request.image_urls, ["first-image"])
        self.assertEqual(repair_request.audio_urls, ["first-audio"])

        moved_transport = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(
                images=("resolved-to-a-new-path",),
                audio=("resolved-to-a-new-audio-path",),
            ),
            message_chain=[Image(), Record()],
        )
        self.assertEqual(
            [item.item_id for item in moved_transport.context.items],
            [item.item_id for item in original.context.items],
        )

    def test_raw_locators_exist_only_in_transport_not_typed_trace_or_safe_prompt(self):
        raw_image = "https://private.invalid/a.png?token=secret"
        raw_audio = "C:/private/voice.amr"
        raw_base64 = "base64://QUJDREVGRw=="
        adaptation = adapt_astrbot_media(
            binding=binding(),
            provider_request=request(
                images=(raw_image, raw_base64),
                audio=(raw_audio,),
                parts=(
                    TextPart(f"[Image Attachment: path {raw_image}]"),
                    TextPart(f"[Image Attachment: path {raw_base64}]"),
                    TextPart(f"[Audio Attachment: path {raw_audio}]"),
                ),
            ),
            message_chain=[Image(), Image(), Record()],
        )

        typed = json.dumps(
            {
                "context": adaptation.context.trace_metadata(),
                "adapter": adaptation.trace_metadata(),
                "prompt": adaptation.transport.safe_prompt_evidence,
            },
            ensure_ascii=False,
        ) + repr(adaptation) + repr(adaptation.transport) + repr(adaptation.context)
        for locator in (raw_image, raw_audio, raw_base64, "token=secret"):
            with self.subTest(locator=locator):
                self.assertNotIn(locator, typed)

        self.assertEqual(adaptation.transport.image_urls, (raw_image, raw_base64))
        self.assertEqual(adaptation.transport.audio_urls, (raw_audio,))


if __name__ == "__main__":
    unittest.main()
