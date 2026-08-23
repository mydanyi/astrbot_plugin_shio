from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .schema import FixtureCase, FixtureSuite, readonly_mapping


_CASE_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,95}$")
_PHASE = re.compile(r"^P(?:[2-9]|10)$")


class FixtureSchemaError(ValueError):
    pass


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise FixtureSchemaError(f"{field}_object_required")
    return dict(value)


def _dimension_selection(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list):
        values = tuple(str(item or "").strip() for item in value)
    else:
        raise FixtureSchemaError("dimension_selection_invalid")
    values = tuple(dict.fromkeys(str(item or "").strip() for item in values))
    if not values or any(not item for item in values):
        raise FixtureSchemaError("dimension_selection_empty")
    return values


def _load_case(raw: Any, allowed: dict[str, tuple[str, ...]]) -> FixtureCase:
    data = _object(raw, "case")
    case_id = str(data.get("id", "") or "").strip()
    if not _CASE_ID.fullmatch(case_id):
        raise FixtureSchemaError("case_id_invalid")
    phase = str(data.get("required_phase", "") or "").strip()
    if not _PHASE.fullmatch(phase):
        raise FixtureSchemaError(f"case_phase_invalid:{case_id}")
    raw_dimensions = _object(data.get("dimensions"), f"case_dimensions:{case_id}")
    unknown = set(raw_dimensions).difference(allowed)
    if unknown:
        raise FixtureSchemaError(f"case_dimension_unknown:{case_id}")
    dimensions: dict[str, tuple[str, ...]] = {}
    for dimension, value in raw_dimensions.items():
        selected = _dimension_selection(value)
        if not set(selected).issubset(allowed[dimension]):
            raise FixtureSchemaError(f"case_dimension_value_unknown:{case_id}:{dimension}")
        dimensions[dimension] = selected
    if set(dimensions) != set(allowed):
        raise FixtureSchemaError(f"case_dimension_incomplete:{case_id}")
    return FixtureCase(
        case_id=case_id,
        required_phase=phase,
        dimensions=MappingProxyType(dimensions),
        input_spec=readonly_mapping(_object(data.get("input"), f"case_input:{case_id}")),
        stub_spec=readonly_mapping(_object(data.get("stubs"), f"case_stubs:{case_id}")),
        expectation=readonly_mapping(_object(data.get("expect"), f"case_expect:{case_id}")),
    )


def load_fixture_suite(root: Path) -> FixtureSuite:
    manifest_path = Path(root) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FixtureSchemaError("manifest_unreadable") from exc
    if not isinstance(manifest, dict):
        raise FixtureSchemaError("manifest_object_required")
    if manifest.get("schema_version") != 1:
        raise FixtureSchemaError("manifest_schema_version_unsupported")
    if manifest.get("privacy") != "synthetic_redacted_only":
        raise FixtureSchemaError("manifest_privacy_invalid")
    raw_dimensions = _object(manifest.get("dimension_values"), "dimension_values")
    allowed: dict[str, tuple[str, ...]] = {}
    for dimension, raw_values in raw_dimensions.items():
        if not isinstance(raw_values, list):
            raise FixtureSchemaError(f"dimension_values_invalid:{dimension}")
        values = _dimension_selection(raw_values)
        if len(values) != len(raw_values):
            raise FixtureSchemaError(f"dimension_values_duplicate:{dimension}")
        allowed[str(dimension)] = values

    scenario_files = manifest.get("scenario_files")
    if not isinstance(scenario_files, list) or not scenario_files:
        raise FixtureSchemaError("scenario_files_required")
    cases: list[FixtureCase] = []
    for filename in scenario_files:
        normalized = str(filename or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*\.json", normalized):
            raise FixtureSchemaError("scenario_filename_invalid")
        path = Path(root) / normalized
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise FixtureSchemaError(f"scenario_unreadable:{normalized}") from exc
        if not isinstance(payload, dict) or payload.get("privacy") != "synthetic_redacted_only":
            raise FixtureSchemaError(f"scenario_privacy_invalid:{normalized}")
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise FixtureSchemaError(f"scenario_cases_required:{normalized}")
        cases.extend(_load_case(raw, allowed) for raw in raw_cases)

    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise FixtureSchemaError("case_id_duplicate")
    required = tuple(str(value or "").strip() for value in manifest.get("required_cross_case_ids", []))
    if not required or not set(required).issubset(case_ids):
        raise FixtureSchemaError("required_cross_case_missing")
    suite = FixtureSuite(
        schema_version=1,
        privacy="synthetic_redacted_only",
        dimension_values=MappingProxyType(allowed),
        required_cross_case_ids=required,
        cases=tuple(cases),
    )
    for dimension, values in suite.coverage().items():
        if values != frozenset(allowed[dimension]):
            raise FixtureSchemaError(f"dimension_coverage_incomplete:{dimension}")
    return suite

