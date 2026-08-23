import unittest

from astrbot_plugin_shio.core.feedback import (
    FeedbackConfidence,
    FeedbackEvidenceSource,
    FeedbackSignal,
    classify_feedback_evidence,
)


class FeedbackEvidenceTests(unittest.TestCase):
    def test_target_followup_is_high_confidence_and_affects_target_profile(self):
        evidence = classify_feedback_evidence(
            reviewer_key="scope|user:a",
            target_identity_key="scope|user:a",
            reply_id="shio-reply-a",
            signal=FeedbackSignal.POSITIVE,
        )

        self.assertEqual(evidence.source, FeedbackEvidenceSource.TARGET_FOLLOWUP)
        self.assertEqual(evidence.confidence, FeedbackConfidence.HIGH)
        self.assertTrue(evidence.affects_target_profile)

    def test_adjacent_other_user_message_is_low_confidence(self):
        evidence = classify_feedback_evidence(
            reviewer_key="scope|user:b",
            target_identity_key="scope|user:a",
            reply_id="shio-reply-a",
            signal=FeedbackSignal.NEGATIVE,
        )

        self.assertEqual(
            evidence.source,
            FeedbackEvidenceSource.ADJACENT_GROUP_MESSAGE,
        )
        self.assertEqual(evidence.confidence, FeedbackConfidence.LOW)
        self.assertFalse(evidence.affects_target_profile)

    def test_exact_platform_reply_reference_is_high_confidence_without_profile_pollution(self):
        evidence = classify_feedback_evidence(
            reviewer_key="scope|user:b",
            target_identity_key="scope|user:a",
            reply_id="shio-reply-a",
            signal=FeedbackSignal.POSITIVE,
            reply_to_message_id="platform-message-1",
            sent_platform_message_ids=("platform-message-1",),
        )

        self.assertEqual(
            evidence.source,
            FeedbackEvidenceSource.EXPLICIT_REPLY_REFERENCE,
        )
        self.assertEqual(evidence.confidence, FeedbackConfidence.HIGH)
        self.assertFalse(evidence.affects_target_profile)

    def test_unmatched_reference_cannot_be_upgraded(self):
        evidence = classify_feedback_evidence(
            reviewer_key="scope|user:b",
            target_identity_key="scope|user:a",
            reply_id="shio-reply-a",
            signal=FeedbackSignal.POSITIVE,
            reply_to_message_id="some-other-message",
            sent_platform_message_ids=("platform-message-1",),
        )

        self.assertEqual(evidence.confidence, FeedbackConfidence.LOW)

    def test_target_quoting_an_unrelated_message_is_not_target_followup(self):
        evidence = classify_feedback_evidence(
            reviewer_key="scope|user:a",
            target_identity_key="scope|user:a",
            reply_id="shio-reply-a",
            signal=FeedbackSignal.POSITIVE,
            reply_to_message_id="unrelated-message",
            sent_platform_message_ids=("platform-message-1",),
        )

        self.assertEqual(evidence.confidence, FeedbackConfidence.LOW)
        self.assertEqual(
            evidence.source,
            FeedbackEvidenceSource.ADJACENT_GROUP_MESSAGE,
        )
        self.assertFalse(evidence.affects_target_profile)

    def test_missing_internal_reply_id_cannot_be_high_confidence(self):
        evidence = classify_feedback_evidence(
            reviewer_key="scope|user:a",
            target_identity_key="scope|user:a",
            reply_id="",
            signal=FeedbackSignal.POSITIVE,
            reply_to_message_id="platform-message-1",
            sent_platform_message_ids=("platform-message-1",),
        )

        self.assertEqual(evidence.confidence, FeedbackConfidence.LOW)

    def test_exact_platform_reaction_is_high_confidence(self):
        evidence = classify_feedback_evidence(
            reviewer_key="scope|user:b",
            target_identity_key="scope|user:a",
            reply_id="shio-reply-a",
            signal=FeedbackSignal.POSITIVE,
            reaction_to_message_id="platform-message-1",
            sent_platform_message_ids=("platform-message-1",),
        )

        self.assertEqual(evidence.source, FeedbackEvidenceSource.PLATFORM_REACTION)
        self.assertEqual(evidence.confidence, FeedbackConfidence.HIGH)

    def test_trace_does_not_expose_reviewer_or_reply_ids(self):
        evidence = classify_feedback_evidence(
            reviewer_key="private-reviewer-key",
            target_identity_key="private-reviewer-key",
            reply_id="private-reply-id",
            signal=FeedbackSignal.POSITIVE,
        )

        rendered = repr(evidence.trace_metadata())
        self.assertNotIn("private-reviewer-key", rendered)
        self.assertNotIn("private-reply-id", rendered)


if __name__ == "__main__":
    unittest.main()
