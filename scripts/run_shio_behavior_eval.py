from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = REPO_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from astrbot_plugin_shio.tests.harness.loader import (  # noqa: E402
    FixtureSchemaError,
    load_fixture_suite,
)
from astrbot_plugin_shio.tests.harness.model_adapter import (  # noqa: E402
    ExternalModelError,
    create_http_external_model_adapter,
)
from astrbot_plugin_shio.tests.harness.reporting import render_report  # noqa: E402
from astrbot_plugin_shio.tests.harness.scenario_runner import (  # noqa: E402
    EvaluationTier,
    run_deterministic_suite,
    run_external_model_suite,
    run_stub_integration_suite,
)


DEFAULT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "p1"


def _refusal(reason_code: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "status": "refused",
            "reason_code": reason_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Shio's synthetic behavior contract evaluation.",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help="Versioned synthetic fixture directory.",
    )
    parser.add_argument(
        "--tier",
        choices=("deterministic", "stub", "external"),
        default="deterministic",
        help="Evaluation tier; deterministic and stub never use network.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Run a selected synthetic case; repeat to select more than one.",
    )
    parser.add_argument(
        "--allow-external-model",
        action="store_true",
        help="Required opt-in before the external tier can create a transport.",
    )
    parser.add_argument(
        "--external-model-config",
        type=Path,
        help="Local JSON config; credentials are read only from its named env var.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        suite = load_fixture_suite(args.fixtures)
        if args.case_id:
            selected = set(args.case_id)
            cases = tuple(case for case in suite.cases if case.case_id in selected)
            if len(cases) != len(selected):
                print(_refusal("case_selection_invalid"))
                return 2
            suite = replace(suite, cases=cases)

        if args.tier == "deterministic":
            tier = EvaluationTier.DETERMINISTIC
            results = run_deterministic_suite(suite)
        elif args.tier == "stub":
            tier = EvaluationTier.STUB_INTEGRATION
            results = run_stub_integration_suite(suite)
        else:
            tier = EvaluationTier.EXTERNAL_MODEL
            adapter = create_http_external_model_adapter(
                allow_external_model=args.allow_external_model,
                config_path=args.external_model_config,
            )
            results = run_external_model_suite(suite, adapter=adapter)
    except ExternalModelError as exc:
        print(_refusal(exc.reason_code))
        return 2
    except FixtureSchemaError:
        print(_refusal("fixture_schema_invalid"))
        return 2
    except (OSError, TypeError, ValueError):
        print(_refusal("evaluation_configuration_invalid"))
        return 2

    print(render_report(tier=tier, results=results, generated_at_ms=0))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
