"""D-075 cadence contract independent of AstrBot transport delivery."""

from __future__ import annotations

import math
import json
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.sys001 import (
    NaturalCadence,
    NaturalCadenceRecord,
    decode_natural_cadence,
    decode_natural_cadence_record,
    encode_natural_cadence,
    encode_natural_cadence_record,
    natural_cadence_allows,
    natural_no_action,
    natural_reply_completed,
    prune_natural_cadence,
)


class NaturalCadenceD075Tests(unittest.TestCase):
    def test_schema_uses_only_the_approved_new_cadence_keys_and_defaults(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        group = json.loads(schema_path.read_text(encoding="utf-8"))["sys001"]["items"]["group"]["items"]
        expected = {
            "natural_reply_cooldown_seconds": 45,
            "natural_frequency_window_minutes": 5,
            "natural_max_replies_per_window": 2,
            "natural_no_action_backoff_base_seconds": 2,
            "natural_no_action_backoff_max_seconds": 30,
        }
        self.assertEqual(expected, {key: group[key]["default"] for key in expected})
        self.assertFalse(any(key.startswith("natural_group_participation_") for key in group))

    def test_decode_encode_accepts_only_strict_record(self) -> None:
        state = NaturalCadence(1, 90.0, (20.0, 90.0), 2, 104.0)
        encoded = encode_natural_cadence(state)
        self.assertEqual(decode_natural_cadence(encoded, now=100.0), state)

        invalid_records = (
            {},
            {**encoded, "version": 2},
            {key: value for key, value in encoded.items() if key != "completed_at"},
            {**encoded, "last_completed_at": True},
            {**encoded, "no_action_streak": True},
            {**encoded, "completed_at": [20.0, True]},
            {**encoded, "last_completed_at": math.nan},
            {**encoded, "backoff_until": math.inf},
            {**encoded, "completed_at": [-1.0]},
            {**encoded, "completed_at": [90.0, 20.0]},
            {**encoded, "last_completed_at": 101.0},
            {**encoded, "completed_at": [20.0, 101.0]},
        )
        for record in invalid_records:
            with self.subTest(record=record):
                self.assertIsNone(decode_natural_cadence(record, now=100.0))

    def test_scope_record_binds_only_committed_transaction_not_event_lifecycle(self) -> None:
        record = NaturalCadenceRecord(
            "qq:group:202", NaturalCadence(1, 90.0, (90.0,), 0, 0.0),
            "0123456789abcdef0123456789abcdef", "committed",
        )
        encoded = encode_natural_cadence_record(record)
        self.assertEqual(
            decode_natural_cadence_record(
                encoded, expected_scope="qq:group:202", now=100.0
            ),
            record,
        )
        self.assertIsNone(
            decode_natural_cadence_record(
                encoded, expected_scope="qq:group:other", now=100.0
            )
        )
        self.assertIsNone(
            decode_natural_cadence_record(
                {**encoded, "transaction_id": "not-a-transaction"},
                expected_scope="qq:group:202",
                now=100.0,
            )
        )

    def test_wait_does_not_change_empty_state(self) -> None:
        state = NaturalCadence.empty()
        # WAIT has no transition: it must neither start cooldown nor consume quota.
        self.assertEqual(state, NaturalCadence.empty())
        self.assertTrue(natural_cadence_allows(state, now=100, cooldown=45, window_seconds=300, maximum=2))

    def test_no_action_uses_bounded_backoff_without_reply_count(self) -> None:
        initial = NaturalCadence(1, 50.0, (20.0, 50.0), 0, 0.0)
        first = natural_no_action(initial, now=100.0, base=2, maximum=30)
        second = natural_no_action(first, now=101.0, base=2, maximum=30)
        capped = initial
        for index in range(8):
            capped = natural_no_action(capped, now=200.0 + index, base=2, maximum=30)

        self.assertEqual((first.no_action_streak, first.backoff_until), (1, 102.0))
        self.assertEqual((second.no_action_streak, second.backoff_until), (2, 105.0))
        self.assertEqual((capped.no_action_streak, capped.backoff_until), (8, 237.0))
        self.assertEqual(first.last_completed_at, initial.last_completed_at)
        self.assertEqual(first.completed_at, initial.completed_at)

    def test_reply_completion_blocks_cooldown_and_window(self) -> None:
        # Creating a reply pending is deliberately not a cadence transition;
        # only the later RespondStage-completed transition can create this state.
        state = natural_reply_completed(NaturalCadence.empty(), now=100, window_seconds=300)
        self.assertEqual(state.completed_at, (100,))
        self.assertFalse(natural_cadence_allows(state, now=120, cooldown=45, window_seconds=300, maximum=2))
        self.assertTrue(natural_cadence_allows(state, now=146, cooldown=45, window_seconds=300, maximum=2))

    def test_reply_completion_cleans_expired_window_before_appending(self) -> None:
        state = NaturalCadence(1, 10.0, (10.0, 50.0), 3, 99.0)
        completed = natural_reply_completed(state, now=400.0, window_seconds=300)
        self.assertEqual(completed.completed_at, (400.0,))
        self.assertEqual(completed.last_completed_at, 400.0)
        self.assertEqual(completed.no_action_streak, 0)
        self.assertEqual(completed.backoff_until, 0.0)

    def test_prune_clears_elapsed_backoff_and_expired_window(self) -> None:
        state = NaturalCadence(1, 10.0, (10.0, 50.0), 4, 99.0)
        self.assertEqual(
            prune_natural_cadence(state, now=400.0, window_seconds=300),
            NaturalCadence(1, 10.0, (), 0, 0.0),
        )

    def test_future_or_backoff_state_fails_closed(self) -> None:
        self.assertFalse(natural_cadence_allows(NaturalCadence(1, 200, (), 0, 0), now=100, cooldown=45, window_seconds=300, maximum=2))
        self.assertFalse(natural_cadence_allows(NaturalCadence(1, 0, (), 1, 200), now=100, cooldown=45, window_seconds=300, maximum=2))


if __name__ == "__main__":
    unittest.main()
