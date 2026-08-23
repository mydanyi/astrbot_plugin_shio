from __future__ import annotations

import json
import re
from typing import Iterable

from .scenario_runner import EvaluationTier, ScenarioResult


_SAFE_CASE_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,95}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_.:-]{0,159}$")


def _case_report(result: ScenarioResult) -> dict[str, object]:
    if not _SAFE_CASE_ID.fullmatch(result.case_id):
        raise ValueError("report_case_id_invalid")
    if any(not _SAFE_REASON.fullmatch(reason) for reason in result.reason_codes):
        raise ValueError("report_reason_code_invalid")
    return {
        "case_id": result.case_id,
        "required_phase": result.required_phase,
        "passed": result.passed,
        "reason_codes": list(result.reason_codes),
        "assertion_count": result.assertion_count,
        "model_call_count": result.model_call_count,
        "stub_call_count": result.stub_call_count,
        "side_effect_count": result.side_effect_count,
        "elapsed_ms": result.elapsed_ms,
    }


def render_report(
    *,
    tier: EvaluationTier,
    results: Iterable[ScenarioResult],
    generated_at_ms: int,
) -> str:
    frozen = tuple(results)
    if isinstance(generated_at_ms, bool) or not isinstance(generated_at_ms, int):
        raise ValueError("report_time_invalid")
    if generated_at_ms < 0:
        raise ValueError("report_time_invalid")
    report = {
        "schema_version": 1,
        "tier": tier.value,
        "generated_at_ms": generated_at_ms,
        "summary": {
            "case_count": len(frozen),
            "passed_count": sum(result.passed for result in frozen),
            "failed_count": sum(not result.passed for result in frozen),
            "assertion_count": sum(result.assertion_count for result in frozen),
            "model_call_count": sum(result.model_call_count for result in frozen),
            "stub_call_count": sum(result.stub_call_count for result in frozen),
            "side_effect_count": sum(result.side_effect_count for result in frozen),
        },
        "cases": [_case_report(result) for result in frozen],
    }
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
