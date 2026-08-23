from __future__ import annotations

import json
import unittest

from astrbot_plugin_shio.core.product_trace import (
    ProductOutcome,
    ProductStage,
    ProductTrace,
    ProductTracePayload,
    ProductTraceStatus,
)


class ProductTraceTests(unittest.TestCase):
    def make_trace(self) -> ProductTrace:
        return ProductTrace(
            trace_id="0123456789abcdef0123456789abcdef",
            conversation_revision=4,
            generation_epoch=7,
        )

    def test_sent_path_is_closed_ordered_and_content_free(self):
        trace = self.make_trace()
        trace.append(
            ProductStage.INGRESS,
            elapsed_ms=0.1,
            payload=ProductTracePayload(
                status=ProductTraceStatus.ACCEPTED,
                reason_code="ingress.human",
            ),
        )
        trace.append(
            ProductStage.ACTION,
            elapsed_ms=1.0,
            payload=ProductTracePayload(
                status=ProductTraceStatus.SELECTED,
                reason_code="action.reply",
                target_bound=True,
            ),
        )
        trace.append(
            ProductStage.PRESENTATION_INTENT,
            elapsed_ms=2.0,
            payload=ProductTracePayload(
                status=ProductTraceStatus.READY,
                reason_code="presentation.text",
                item_count=1,
            ),
        )
        trace.append(
            ProductStage.PRESENTATION_RECEIPT,
            elapsed_ms=3.0,
            payload=ProductTracePayload(
                status=ProductTraceStatus.SUCCEEDED,
                reason_code="receipt.text",
                success_count=1,
            ),
        )
        trace.append(
            ProductStage.SEND,
            elapsed_ms=4.0,
            payload=ProductTracePayload(
                status=ProductTraceStatus.SUCCEEDED,
                reason_code="send.receipted",
                success_count=1,
            ),
        )
        trace.terminate(ProductOutcome.SENT, elapsed_ms=4.1, reason_code="terminal.sent")

        snapshot = trace.snapshot()
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual([event["sequence"] for event in snapshot["events"]], list(range(6)))
        self.assertEqual(snapshot["events"][-1]["outcome"], "sent")
        rendered = json.dumps(snapshot, ensure_ascii=False)
        for secret in ("sender", "message", "scope", "https://", "base64", "query"):
            self.assertNotIn(secret, rendered.lower())

    def test_drop_no_action_react_block_cancel_and_failure_are_distinct(self):
        for outcome, terminal_stage in (
            (ProductOutcome.DROPPED, ProductStage.INGRESS),
            (ProductOutcome.NO_ACTION, ProductStage.ACTION),
            (ProductOutcome.REACTED, ProductStage.PRESENTATION_RECEIPT),
            (ProductOutcome.BLOCKED, ProductStage.VALIDATION),
            (ProductOutcome.CANCELLED, ProductStage.ACTION),
            (ProductOutcome.FAILED, ProductStage.EVIDENCE),
        ):
            with self.subTest(outcome=outcome):
                trace = self.make_trace()
                trace.append(
                    terminal_stage,
                    elapsed_ms=1.0,
                    payload=ProductTracePayload(
                        status=ProductTraceStatus.FAILED,
                        reason_code=f"path.{outcome.value}",
                    ),
                )
                trace.terminate(outcome, elapsed_ms=2.0, reason_code=f"terminal.{outcome.value}")
                self.assertEqual(trace.snapshot()["events"][-1]["outcome"], outcome.value)

    def test_duplicate_out_of_order_double_terminal_and_incomplete_send_fail_closed(self):
        trace = self.make_trace()
        trace.append(ProductStage.INGRESS, elapsed_ms=0.1, payload=ProductTracePayload())
        with self.assertRaises(ValueError):
            trace.append(ProductStage.INGRESS, elapsed_ms=0.2, payload=ProductTracePayload())
        trace.append(ProductStage.ACTION, elapsed_ms=0.3, payload=ProductTracePayload())
        with self.assertRaises(ValueError):
            trace.append(ProductStage.MEMORY, elapsed_ms=0.4, payload=ProductTracePayload())
        with self.assertRaises(ValueError):
            trace.terminate(ProductOutcome.SENT, elapsed_ms=0.5, reason_code="terminal.sent")

        trace.terminate(ProductOutcome.NO_ACTION, elapsed_ms=0.6, reason_code="terminal.no_action")
        with self.assertRaises(ValueError):
            trace.terminate(ProductOutcome.NO_ACTION, elapsed_ms=0.7, reason_code="terminal.no_action")

    def test_payload_rejects_free_text_locators_ids_and_invalid_counts(self):
        for unsafe in (
            "https://example.invalid/item",
            "raw chat sentence",
            "data:image/png;base64,aaaa",
            "C:\\private\\image.png",
            "sender_id",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                ProductTracePayload(reason_code=unsafe)
        with self.assertRaises(ValueError):
            ProductTracePayload(item_count=-1)
        with self.assertRaises(TypeError):
            ProductTracePayload(url="https://example.invalid")

    def test_trace_binding_is_immutable_and_stale_metadata_cannot_be_mixed(self):
        trace = self.make_trace()
        with self.assertRaises(AttributeError):
            trace.conversation_revision = 5
        with self.assertRaises(ValueError):
            ProductTrace(
                trace_id="not-a-trace",
                conversation_revision=1,
                generation_epoch=1,
            )
        with self.assertRaises(ValueError):
            ProductTrace(
                trace_id="f" * 32,
                conversation_revision=-1,
                generation_epoch=1,
            )


if __name__ == "__main__":
    unittest.main()
