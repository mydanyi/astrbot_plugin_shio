from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class FixtureCase:
    case_id: str
    required_phase: str
    dimensions: Mapping[str, tuple[str, ...]]
    input_spec: Mapping[str, Any]
    stub_spec: Mapping[str, Any]
    expectation: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FixtureSuite:
    schema_version: int
    privacy: str
    dimension_values: Mapping[str, tuple[str, ...]]
    required_cross_case_ids: tuple[str, ...]
    cases: tuple[FixtureCase, ...]

    def coverage(self) -> dict[str, frozenset[str]]:
        values: dict[str, set[str]] = {
            dimension: set() for dimension in self.dimension_values
        }
        for case in self.cases:
            for dimension, selected in case.dimensions.items():
                values[dimension].update(selected)
        return {
            dimension: frozenset(selected)
            for dimension, selected in values.items()
        }


def readonly_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))

