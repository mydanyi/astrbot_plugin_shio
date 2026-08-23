from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_SAFE_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class AssertionOutcome:
    passed: bool
    reason_codes: tuple[str, ...]
    assertion_count: int


def _safe_path(path: tuple[str, ...]) -> str:
    safe = tuple(part if _SAFE_FIELD.fullmatch(part) else "field" for part in path)
    return ".".join(safe) or "root"


def _compare(
    expected: Any,
    observed: Any,
    *,
    path: tuple[str, ...],
    reasons: list[str],
) -> int:
    if isinstance(expected, Mapping):
        if not isinstance(observed, Mapping):
            reasons.append(f"type_mismatch:{_safe_path(path)}")
            return 1
        assertions = 0
        for key, expected_value in expected.items():
            key_text = str(key)
            child_path = (*path, key_text)
            if key not in observed:
                reasons.append(f"missing_field:{_safe_path(child_path)}")
                assertions += 1
                continue
            assertions += _compare(
                expected_value,
                observed[key],
                path=child_path,
                reasons=reasons,
            )
        return assertions

    if isinstance(expected, (list, tuple)):
        matches = isinstance(observed, (list, tuple)) and list(expected) == list(observed)
    else:
        matches = type(expected) is type(observed) and expected == observed
    if not matches:
        reasons.append(f"value_mismatch:{_safe_path(path)}")
    return 1


def compare_semantic_observation(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> AssertionOutcome:
    """Compare typed behavior fields without comparing or reporting visible wording."""

    reasons: list[str] = []
    count = _compare(expected, observed, path=(), reasons=reasons)
    unique_reasons = tuple(dict.fromkeys(reasons))
    return AssertionOutcome(
        passed=not unique_reasons,
        reason_codes=unique_reasons,
        assertion_count=count,
    )
