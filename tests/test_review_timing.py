"""Review deadline and diagnostic regressions, without logging message bodies."""
import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_name_semantic_provider_routing as fixtures

MAIN = fixtures.MAIN


class ReviewTimingTests(unittest.IsolatedAsyncioTestCase):
    async def run_review(self, outputs, **settings):
        provider = fixtures.FakeProvider(*outputs)
        provider.meta = lambda: SimpleNamespace(id='review-test')
        plugin = fixtures.NameSemanticProviderRoutingTests().plugin(fixtures.FakeContext(current=provider))
        plugin.config['sys001']['final_review'] = {
            'mode': 'core', 'repair_enabled': True, 'use_repaired_text': True,
            'max_repair_attempts': 1, **settings,
        }
        event, _ = fixtures.FinalAgentObservationTests.event_with_live_turn()
        event.unified_msg_origin = event.get_extra(MAIN.SYS001_TURN_EXTRA).scope
        sink = logging.getLogger('shio-review-timing-test')
        with patch.object(MAIN, 'logger', sink), self.assertLogs(sink, level='INFO') as logs:
            result = await plugin._review_final_text(event, event.get_extra(MAIN.SYS001_TURN_EXTRA), 'PRIVATE_REPLY_SENTINEL')
        records = [json.loads(record.getMessage().split('Shio review attempt=', 1)[1])
                   for record in logs.records if 'Shio review attempt=' in record.getMessage()]
        return result, records, '\n'.join(logs.output)

    async def test_default_budget_accepts_review_finishing_after_old_eight_seconds(self):
        result, _, _ = await self.run_review([('sleep', 8.2, '{"action":"keep"}')])
        self.assertFalse(result.exhausted)
        self.assertEqual('keep', result.reason)

    async def test_repair_and_recheck_report_separate_calls_with_shared_remaining_budget(self):
        result, records, text = await self.run_review([
            ('sleep', 0.02, '{"action":"replace","text":"PRIVATE_REPAIR_SENTINEL"}'),
            ('sleep', 0.02, '{"action":"keep"}'),
        ], timeout_seconds=1)
        self.assertEqual('repaired', result.reason)
        self.assertEqual([1, 2], [r['round'] for r in records])
        self.assertEqual(['replace', 'keep'], [r['outcome'] for r in records])
        self.assertEqual(['review-test', 'review-test'], [r['provider_id'] for r in records])
        self.assertTrue(all(r['elapsed_ms'] >= 15 for r in records))
        self.assertLess(records[1]['remaining_ms'], records[0]['remaining_ms'])
        self.assertNotIn('PRIVATE_', text)

    async def test_failed_attempt_records_reason_and_does_not_log_provider_details(self):
        cases = [
            (('sleep', 0.1, '{"action":"keep"}'), 'timeout'),
            ('PRIVATE_INVALID_BODY', 'invalid'),
            (RuntimeError('PRIVATE_ERROR_DETAIL'), 'unavailable'),
        ]
        for output, outcome in cases:
            with self.subTest(outcome=outcome):
                result, records, text = await self.run_review([output], timeout_seconds=0.04)
                self.assertEqual(outcome, result.reason)
                self.assertEqual([outcome], [r['outcome'] for r in records])
                self.assertNotIn('PRIVATE_', text)
                if outcome == 'timeout':
                    self.assertEqual(0, records[0]['remaining_ms'])
