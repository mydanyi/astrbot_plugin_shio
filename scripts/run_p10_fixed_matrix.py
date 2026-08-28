from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = REPO_ROOT.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "p10" / "acceptance_matrix.json"


def _load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def main() -> int:
    sys.path.insert(0, str(PACKAGE_PARENT))
    fixture = _load_fixture()
    capabilities = tuple(fixture["capabilities"])
    replays = tuple(fixture["runtime_replays"])
    evidence_ids = tuple(
        sorted(
            {
                str(case["evidence_test_id"])
                for case in (*capabilities, *replays)
            }
        )
    )
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        (
            loader.loadTestsFromName(
                "astrbot_plugin_shio.tests.test_p10_fixed_matrix"
            ),
            *(loader.loadTestsFromName(test_id) for test_id in evidence_ids),
        )
    )
    if loader.errors:
        report = {
            "schema_version": 1,
            "passed": False,
            "failure_code": "p10_test_resolution_failed",
            "loader_error_count": len(loader.errors),
        }
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 1

    suppressed_runtime_output = io.StringIO()
    with redirect_stdout(suppressed_runtime_output), redirect_stderr(
        suppressed_runtime_output
    ):
        result = unittest.TextTestRunner(
            stream=io.StringIO(),
            verbosity=0,
        ).run(suite)
    report = {
        "schema_version": 1,
        "privacy": "content_free_counts_only",
        "capability_count": len(capabilities),
        "runtime_outcome_count": len(replays),
        "evidence_test_count": len(evidence_ids),
        "executed_test_count": result.testsRun,
        "failure_count": len(result.failures),
        "error_count": len(result.errors),
        "skipped_count": len(result.skipped),
        "passed": result.wasSuccessful(),
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
