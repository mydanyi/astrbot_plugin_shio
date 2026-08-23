from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")


class HarnessContractError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


class EffectBudgetExceeded(HarnessContractError):
    pass


class ScriptViolation(HarnessContractError):
    pass


class FixedClock:
    """A manually advanced clock; it never reads wall or monotonic time."""

    def __init__(self, *, start_ms: int = 0):
        if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
            raise ValueError("fixed_clock_start_invalid")
        self._current_ms = start_ms

    def now_ms(self) -> int:
        return self._current_ms

    def advance_ms(self, delta_ms: int) -> int:
        if isinstance(delta_ms, bool) or not isinstance(delta_ms, int) or delta_ms < 0:
            raise ValueError("fixed_clock_delta_invalid")
        self._current_ms += delta_ms
        return self._current_ms


@dataclass(frozen=True, slots=True)
class EffectRecord:
    kind: str
    reason_code: str
    at_ms: int


class SideEffectLedger:
    """Count-only ledger. It deliberately has no payload, ID, URL or arguments field."""

    def __init__(self, *, clock: FixedClock, budgets: Mapping[str, int]):
        normalized: dict[str, int] = {}
        for kind, budget in budgets.items():
            kind_text = str(kind or "").strip()
            if not _SAFE_CODE.fullmatch(kind_text):
                raise ValueError("effect_kind_invalid")
            if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
                raise ValueError("effect_budget_invalid")
            normalized[kind_text] = budget
        if not normalized:
            raise ValueError("effect_budgets_required")
        self._clock = clock
        self._budgets = MappingProxyType(normalized)
        self._records: list[EffectRecord] = []

    @property
    def records(self) -> tuple[EffectRecord, ...]:
        return tuple(self._records)

    def counts(self) -> dict[str, int]:
        counts = {kind: 0 for kind in self._budgets}
        for record in self._records:
            counts[record.kind] += 1
        return counts

    def record(self, kind: str, *, reason_code: str) -> EffectRecord:
        kind_text = str(kind or "").strip()
        reason_text = str(reason_code or "").strip()
        if not _SAFE_CODE.fullmatch(reason_text):
            raise ValueError("effect_reason_invalid")
        if kind_text not in self._budgets:
            raise EffectBudgetExceeded("effect_not_budgeted")
        if self.counts()[kind_text] >= self._budgets[kind_text]:
            raise EffectBudgetExceeded("effect_budget_exceeded")
        record = EffectRecord(
            kind=kind_text,
            reason_code=reason_text,
            at_ms=self._clock.now_ms(),
        )
        self._records.append(record)
        return record


@dataclass(frozen=True, slots=True)
class ScriptedStep:
    operation: str
    result: Mapping[str, Any]
    case_id: str = ""

    def __post_init__(self) -> None:
        operation = str(self.operation or "").strip()
        if not _SAFE_CODE.fullmatch(operation):
            raise ValueError("script_operation_invalid")
        if self.case_id and not _SAFE_CODE.fullmatch(str(self.case_id)):
            raise ValueError("script_case_id_invalid")
        if not isinstance(self.result, Mapping):
            raise ValueError("script_result_mapping_required")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "result", MappingProxyType(dict(self.result)))


class ScriptedProvider:
    """Provider substitute with an exact operation sequence and exact call budget."""

    def __init__(
        self,
        *,
        steps: Sequence[ScriptedStep],
        call_budget: int,
        ledger: SideEffectLedger | None = None,
    ):
        self._steps = tuple(steps)
        if isinstance(call_budget, bool) or not isinstance(call_budget, int):
            raise ValueError("provider_call_budget_invalid")
        if call_budget != len(self._steps):
            raise ValueError("provider_call_budget_must_match_script")
        self._call_budget = call_budget
        self._call_count = 0
        self._ledger = ledger or SideEffectLedger(
            clock=FixedClock(start_ms=0),
            budgets={"model_call": call_budget},
        )

    @property
    def call_count(self) -> int:
        return self._call_count

    def invoke(self, *, case_id: str, operation: str) -> dict[str, Any]:
        if self._call_count >= self._call_budget:
            raise ScriptViolation("provider_call_budget_exceeded")
        expected = self._steps[self._call_count]
        if expected.operation != str(operation or "").strip():
            raise ScriptViolation("provider_call_order_mismatch")
        if expected.case_id and expected.case_id != case_id:
            raise ScriptViolation("provider_case_mismatch")
        self._ledger.record("model_call", reason_code="scripted_provider")
        self._call_count += 1
        return dict(expected.result)

    def assert_exhausted(self) -> None:
        if self._call_count != len(self._steps):
            raise ScriptViolation("provider_script_not_exhausted")


@dataclass(frozen=True, slots=True)
class StubStep:
    name: str
    result: Any

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not _SAFE_CODE.fullmatch(name):
            raise ValueError("stub_name_invalid")
        object.__setattr__(self, "name", name)


class StrictStubRegistry:
    """Non-model substitute with exact lookup order and no real plugin/tool calls."""

    def __init__(
        self,
        *,
        steps: Sequence[StubStep],
        ledger: SideEffectLedger,
    ):
        self._steps = tuple(steps)
        self._call_count = 0
        self._ledger = ledger

    @property
    def call_count(self) -> int:
        return self._call_count

    def invoke(self, name: str) -> Any:
        if self._call_count >= len(self._steps):
            raise ScriptViolation("stub_call_budget_exceeded")
        expected = self._steps[self._call_count]
        if expected.name != str(name or "").strip():
            raise ScriptViolation("stub_call_order_mismatch")
        self._ledger.record("stub_call", reason_code="scripted_stub")
        self._call_count += 1
        return expected.result

    def assert_exhausted(self) -> None:
        if self._call_count != len(self._steps):
            raise ScriptViolation("stub_script_not_exhausted")
