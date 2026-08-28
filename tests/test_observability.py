import json
import unittest

from astrbot_plugin_shio.core.observability import (
    diagnostic_digest,
    safe_exception_kind,
    sanitize_trace_metadata,
    structured_log,
)


class CapturingLogger:
    def __init__(self):
        self.calls = []

    def info(self, message, *args):
        self.calls.append(("info", message % args))

    def warning(self, message, *args):
        self.calls.append(("warning", message % args))


class ObservabilityTests(unittest.TestCase):
    def test_fail_closed_metadata_drops_content_credentials_and_raw_ids(self):
        secret = "Bearer top-secret-token-value"
        raw = sanitize_trace_metadata(
            {
                "sender_id": "real-user-42",
                "message_id": 123456,
                "prompt": "private chat text",
                "authorization": secret,
                "api_key": "sk-private",
                "request_body": {"content": "private"},
                "source_kind": "direct_event",
                "subject_digest": diagnostic_digest("real-user-42"),
                "call_count": 2,
                "guard_hit": True,
                "unknown_string": "should be dropped",
                "repair_output_token_count": 64,
                "access_token": "still-secret",
            }
        )
        self.assertEqual(
            raw,
            {
                "source_kind": "direct_event",
                "subject_digest": diagnostic_digest("real-user-42"),
                "call_count": 2,
                "guard_hit": True,
                "repair_output_token_count": 64,
            },
        )
        self.assertNotIn(secret, repr(raw))

    def test_structured_log_contains_only_valid_trace_and_sanitized_metadata(self):
        logger = CapturingLogger()
        payload = structured_log(
            logger,
            "warning",
            "reply.guard",
            trace_id="0123456789abcdef0123456789abcdef",
            source_kind="direct_event",
            target_digest=diagnostic_digest("platform-message-99"),
            sender_id="real-user-42",
            content="private reply",
            guard_hit_count=3,
            latency_ms=12.5,
        )
        self.assertEqual(payload["component"], "reply.guard")
        self.assertEqual(payload["guard_hit_count"], 3)
        self.assertNotIn("sender_id", payload)
        self.assertNotIn("content", payload)
        rendered = logger.calls[0][1]
        self.assertNotIn("real-user-42", rendered)
        self.assertNotIn("private reply", rendered)
        json.loads(rendered.split(" ", 1)[1])

    def test_exception_message_is_never_returned(self):
        exc = RuntimeError("Authorization: Bearer secret-body")
        self.assertEqual(safe_exception_kind(exc), "RuntimeError")
        self.assertNotIn("secret-body", safe_exception_kind(exc))

    def test_taxonomy_suffixes_do_not_allow_urls_or_freeform_chat(self):
        raw = sanitize_trace_metadata(
            {
                "media_source": "https://example.invalid/private-image",
                "plugin_status": "raw chat sentence from a user",
                "safe_status": "ready",
                "safe_source": "astrbot.native_caption",
            }
        )
        self.assertEqual(
            raw,
            {
                "safe_status": "ready",
                "safe_source": "astrbot.native_caption",
            },
        )

    def test_provider_and_meme_ownership_are_visible_without_content(self):
        provider_digest = diagnostic_digest("configured-provider")
        raw = sanitize_trace_metadata(
            {
                "primary_provider_owner": "astrbot_default_request",
                "proactive_provider_owner": "shio_proactive_config",
                "proactive_provider_digest": provider_digest,
                "provider_context_count": 4,
                "meme_selection_owner": "meme_manager_semantic",
                "meme_complement_reason": "manager_semantic_handoff",
                "meme_complement_category": "food",
                "meme_execution_category": "food",
                "proactive_meme_category": "food",
                "prompt": "private group history",
            }
        )

        self.assertEqual(
            raw,
            {
                "primary_provider_owner": "astrbot_default_request",
                "proactive_provider_owner": "shio_proactive_config",
                "proactive_provider_digest": provider_digest,
                "provider_context_count": 4,
                "meme_selection_owner": "meme_manager_semantic",
                "meme_complement_reason": "manager_semantic_handoff",
                "meme_complement_category": "food",
                "meme_execution_category": "food",
                "proactive_meme_category": "food",
            },
        )


if __name__ == "__main__":
    unittest.main()
