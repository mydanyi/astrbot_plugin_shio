from __future__ import annotations

import unittest
from pathlib import Path

from astrbot_plugin_shio.tests.harness.loader import load_fixture_suite
from astrbot_plugin_shio.tests.harness.privacy import scan_fixture_tree


ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "p1"


class P1FixtureMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = load_fixture_suite(FIXTURE_ROOT)

    def test_manifest_is_versioned_synthetic_and_has_unique_cases(self):
        self.assertEqual(self.suite.schema_version, 1)
        self.assertEqual(self.suite.privacy, "synthetic_redacted_only")
        self.assertGreaterEqual(len(self.suite.cases), 24)
        ids = [case.case_id for case in self.suite.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_master_plan_dimension_value_has_a_real_case(self):
        coverage = self.suite.coverage()
        self.assertEqual(set(coverage), set(self.suite.dimension_values))
        for dimension, allowed in self.suite.dimension_values.items():
            with self.subTest(dimension=dimension):
                self.assertEqual(coverage[dimension], frozenset(allowed))

    def test_required_cross_cases_have_executable_inputs_stubs_and_expectations(self):
        cases = {case.case_id: case for case in self.suite.cases}
        for case_id in self.suite.required_cross_case_ids:
            with self.subTest(case_id=case_id):
                case = cases[case_id]
                self.assertTrue(case.input_spec)
                self.assertTrue(case.stub_spec)
                self.assertTrue(case.expectation)

    def test_future_phase_cases_are_explicit_not_hidden_expected_failures(self):
        for case in self.suite.cases:
            with self.subTest(case=case.case_id):
                self.assertRegex(case.required_phase, r"^P(?:[2-9]|10)$")
                self.assertNotIn("expected_failure", case.expectation)
                self.assertNotIn("xfail", case.expectation)

    def test_fixture_tree_passes_strict_privacy_gate(self):
        report = scan_fixture_tree(FIXTURE_ROOT)
        self.assertTrue(report.is_safe, report.issue_codes)
        self.assertEqual(report.file_count, 7)
        self.assertGreater(report.scalar_count, 0)


if __name__ == "__main__":
    unittest.main()
