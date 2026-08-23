import copy
import dataclasses
import gc
import pickle
import threading
import unittest
import weakref

from astrbot_plugin_shio.core.contracts import ContractViolation
from astrbot_plugin_shio.core.proactive_trigger import (
    ProactiveCandidateDisposition,
    ProactiveGroupTarget,
    ProactiveSchedulerObservation,
    ProactiveSourceKind,
    ProactiveTriggerAuthority,
    ProactiveTriggerCandidate,
    ProactiveTurnSource,
)


class ProactiveTriggerTests(unittest.TestCase):
    @staticmethod
    def _chain(
        authority: ProactiveTriggerAuthority,
        *,
        group_id: str = "group-a",
        observed_at: float = 1000.0,
    ):
        observation = authority.observe_group(
            platform_id="platform-a",
            bot_id="bot-a",
            group_id=group_id,
            unified_msg_origin=f"group:platform-a:{group_id}",
            observed_at=observed_at,
        )
        source = authority.issue_source(observation)
        candidate = authority.issue_candidate(source)
        return observation, source, candidate

    def test_source_is_group_only_and_never_models_an_inbound_human(self):
        authority = ProactiveTriggerAuthority()
        observation, source, candidate = self._chain(authority)

        self.assertIs(authority.inspect_observation(observation), observation)
        self.assertIs(authority.inspect_source(source), source)
        self.assertIs(authority.inspect_candidate(candidate), candidate)
        self.assertEqual(candidate.disposition, ProactiveCandidateDisposition.TYPED_ONLY)
        self.assertFalse(candidate.model_authorized)
        self.assertFalse(candidate.send_authorized)
        self.assertEqual(candidate.proactive_generation, 1)
        self.assertIs(source.source_kind, ProactiveSourceKind.SCHEDULER_OBSERVATION)
        self.assertEqual(source.target.group_id, "group-a")

        public_fields = {
            field.name
            for cls in (
                ProactiveGroupTarget,
                ProactiveSchedulerObservation,
                ProactiveTurnSource,
                ProactiveTriggerCandidate,
            )
            for field in dataclasses.fields(cls)
            if not field.name.startswith("_")
        }
        for forbidden in (
            "sender_id",
            "sender_key",
            "principal",
            "is_owner",
            "message",
            "message_id",
            "reply_to_message_id",
            "admission",
            "ticket",
        ):
            self.assertNotIn(forbidden, public_fields)

    def test_generation_is_independent_per_group_and_stales_only_same_group(self):
        authority = ProactiveTriggerAuthority()
        _, _, first = self._chain(authority, group_id="group-a", observed_at=1000.0)
        _, _, other = self._chain(authority, group_id="group-b", observed_at=1001.0)
        _, _, second = self._chain(authority, group_id="group-a", observed_at=1002.0)

        first_status = authority.validate_candidate(first)
        other_status = authority.validate_candidate(other)
        second_status = authority.validate_candidate(second)
        self.assertFalse(first_status.is_current)
        self.assertEqual(first_status.reason_code, "proactive_candidate_stale")
        self.assertTrue(other_status.is_current)
        self.assertTrue(second_status.is_current)
        self.assertEqual(second.proactive_generation, 2)
        self.assertEqual(other.proactive_generation, 1)

    def test_observation_and_source_are_one_shot(self):
        authority = ProactiveTriggerAuthority()
        observation = authority.observe_group(
            platform_id="platform-a",
            bot_id="bot-a",
            group_id="group-a",
            unified_msg_origin="group:platform-a:group-a",
            observed_at=1000.0,
        )
        source = authority.issue_source(observation)
        with self.assertRaisesRegex(ContractViolation, "proactive_observation_consumed"):
            authority.issue_source(observation)

        authority.issue_candidate(source)
        with self.assertRaisesRegex(ContractViolation, "proactive_source_consumed"):
            authority.issue_candidate(source)

    def test_public_copy_cross_authority_and_mutation_fail_closed(self):
        first = ProactiveTriggerAuthority()
        second = ProactiveTriggerAuthority()
        observation, source, candidate = self._chain(first)

        for builder, inspector in (
            (lambda: copy.copy(observation), first.inspect_observation),
            (lambda: copy.deepcopy(source), first.inspect_source),
            (lambda: pickle.loads(pickle.dumps(candidate)), first.inspect_candidate),
        ):
            with self.assertRaises((ContractViolation, TypeError, pickle.PickleError)):
                inspector(builder())
        with self.assertRaises(ContractViolation):
            second.inspect_candidate(candidate)

        original_generation = candidate.proactive_generation
        object.__setattr__(candidate, "proactive_generation", 999)
        with self.assertRaisesRegex(ContractViolation, "proactive_candidate_corrupt"):
            first.inspect_candidate(candidate)
        object.__setattr__(candidate, "proactive_generation", original_generation)
        self.assertIs(first.inspect_candidate(candidate), candidate)

    def test_raw_constructors_and_object_shells_are_not_authority(self):
        authority = ProactiveTriggerAuthority()
        for cls, inspector in (
            (ProactiveGroupTarget, authority.inspect_target),
            (ProactiveSchedulerObservation, authority.inspect_observation),
            (ProactiveTurnSource, authority.inspect_source),
            (ProactiveTriggerCandidate, authority.inspect_candidate),
        ):
            with self.assertRaises(TypeError):
                cls()
            shell = object.__new__(cls)
            with self.assertRaises(ContractViolation):
                inspector(shell)

    def test_trace_and_repr_are_content_free_even_after_corruption(self):
        authority = ProactiveTriggerAuthority()
        observation, source, candidate = self._chain(authority)
        marker = "PRIVATE-GROUP-MARKER"
        object.__setattr__(candidate, "candidate_digest", marker)

        rendered = repr(candidate) + repr(candidate.trace_metadata())
        self.assertNotIn(marker, rendered)
        self.assertNotIn("group-a", rendered)
        self.assertFalse(candidate.trace_metadata()["proactive_candidate_canonical"])

        object.__setattr__(candidate, "candidate_digest", authority.inspect_source(source).source_digest)
        with self.assertRaises(ContractViolation):
            authority.inspect_candidate(candidate)
        self.assertNotIn("group-a", repr(observation) + repr(source))

    def test_weak_records_release_without_granting_model_or_send(self):
        authority = ProactiveTriggerAuthority(max_records=2)
        observation, source, candidate = self._chain(authority)
        observation_ref = weakref.ref(observation)
        source_ref = weakref.ref(source)
        candidate_ref = weakref.ref(candidate)
        del observation, source, candidate
        gc.collect()

        self.assertIsNone(observation_ref())
        self.assertIsNone(source_ref())
        self.assertIsNone(candidate_ref())
        self.assertEqual(authority.trace_metadata()["proactive_candidate_count"], 0)
        for index in range(4):
            current_observation, current_source, current = self._chain(
                authority,
                group_id=f"group-{index}",
                observed_at=2000.0 + index,
            )
            self.assertFalse(current.model_authorized)
            self.assertFalse(current.send_authorized)
            del current_observation, current_source, current
            gc.collect()

    def test_concurrent_candidate_issue_has_exactly_one_winner(self):
        authority = ProactiveTriggerAuthority()
        observation = authority.observe_group(
            platform_id="platform-a",
            bot_id="bot-a",
            group_id="group-a",
            unified_msg_origin="group:platform-a:group-a",
            observed_at=1000.0,
        )
        source = authority.issue_source(observation)
        barrier = threading.Barrier(8)
        results: list[object] = []
        result_lock = threading.Lock()

        def run() -> None:
            barrier.wait()
            try:
                value: object = authority.issue_candidate(source)
            except ContractViolation as exc:
                value = str(exc)
            with result_lock:
                results.append(value)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [value for value in results if type(value) is ProactiveTriggerCandidate]
        self.assertEqual(len(winners), 1)
        self.assertEqual(results.count("proactive_source_consumed"), 7)
        self.assertTrue(authority.validate_candidate(winners[0]).is_current)


if __name__ == "__main__":
    unittest.main()
