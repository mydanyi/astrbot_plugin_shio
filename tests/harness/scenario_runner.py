from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from .assertions import compare_semantic_observation
from .model_adapter import ExternalModelAdapter
from .schema import FixtureCase, FixtureSuite
from .stubs import (
    FixedClock,
    HarnessContractError,
    ScriptedProvider,
    ScriptedStep,
    SideEffectLedger,
    StrictStubRegistry,
    StubStep,
)


class EvaluationTier(str, Enum):
    DETERMINISTIC = "deterministic"
    STUB_INTEGRATION = "stub_integration"
    EXTERNAL_MODEL = "external_model"


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    case_id: str
    required_phase: str
    tier: EvaluationTier
    passed: bool
    reason_codes: tuple[str, ...]
    assertion_count: int
    model_call_count: int
    stub_call_count: int
    side_effect_count: int
    elapsed_ms: int


ObservationFactory = Callable[[FixtureCase], Mapping[str, Any]]


def _result(
    case: FixtureCase,
    *,
    tier: EvaluationTier,
    observed: Mapping[str, Any],
    model_calls: int,
    stub_calls: int,
    side_effect_count: int,
    elapsed_ms: int = 0,
) -> ScenarioResult:
    assertion = compare_semantic_observation(case.expectation, observed)
    return ScenarioResult(
        case_id=case.case_id,
        required_phase=case.required_phase,
        tier=tier,
        passed=assertion.passed,
        reason_codes=assertion.reason_codes,
        assertion_count=assertion.assertion_count,
        model_call_count=model_calls,
        stub_call_count=stub_calls,
        side_effect_count=side_effect_count,
        elapsed_ms=elapsed_ms,
    )


def _contract_observation(case: FixtureCase) -> Mapping[str, Any]:
    # P1 establishes the harness itself. Later phases replace this oracle with
    # their production-bound observer while continuing to consume this fixture.
    return dict(case.expectation)


def run_deterministic_suite(
    suite: FixtureSuite,
    *,
    observe: ObservationFactory | None = None,
) -> tuple[ScenarioResult, ...]:
    observer = observe or _contract_observation
    results: list[ScenarioResult] = []
    for case in suite.cases:
        observed = observer(case)
        results.append(
            _result(
                case,
                tier=EvaluationTier.DETERMINISTIC,
                observed=observed,
                model_calls=0,
                stub_calls=0,
                side_effect_count=0,
            )
        )
    return tuple(results)


def _provider_operation(stub_name: str) -> str:
    return "repair" if stub_name == "repair_provider" else "compose"


def _run_stub_case(case: FixtureCase) -> ScenarioResult:
    provider_names = {"provider", "repair_provider"}
    provider_steps = tuple(
        ScriptedStep(
            operation=_provider_operation(name),
            case_id=case.case_id,
            result={"state_code": value},
        )
        for name, value in case.stub_spec.items()
        if name in provider_names
    )
    stub_steps = tuple(
        StubStep(name=name, result=value)
        for name, value in case.stub_spec.items()
        if name not in provider_names
    )
    clock = FixedClock(start_ms=1000)
    ledger = SideEffectLedger(
        clock=clock,
        budgets={
            "model_call": len(provider_steps),
            "stub_call": len(stub_steps),
        },
    )
    provider = ScriptedProvider(
        steps=provider_steps,
        call_budget=len(provider_steps),
        ledger=ledger,
    )
    registry = StrictStubRegistry(steps=stub_steps, ledger=ledger)

    try:
        for name in case.stub_spec:
            if name in provider_names:
                provider.invoke(
                    case_id=case.case_id,
                    operation=_provider_operation(name),
                )
            else:
                registry.invoke(name)
            clock.advance_ms(1)
        provider.assert_exhausted()
        registry.assert_exhausted()
        observed: Mapping[str, Any] = _contract_observation(case)
        return _result(
            case,
            tier=EvaluationTier.STUB_INTEGRATION,
            observed=observed,
            model_calls=provider.call_count,
            stub_calls=registry.call_count,
            side_effect_count=len(ledger.records),
            elapsed_ms=clock.now_ms() - 1000,
        )
    except HarnessContractError as exc:
        return ScenarioResult(
            case_id=case.case_id,
            required_phase=case.required_phase,
            tier=EvaluationTier.STUB_INTEGRATION,
            passed=False,
            reason_codes=(exc.reason_code,),
            assertion_count=0,
            model_call_count=provider.call_count,
            stub_call_count=registry.call_count,
            side_effect_count=len(ledger.records),
            elapsed_ms=clock.now_ms() - 1000,
        )


def run_stub_integration_suite(suite: FixtureSuite) -> tuple[ScenarioResult, ...]:
    return tuple(_run_stub_case(case) for case in suite.cases)


def run_external_model_suite(
    suite: FixtureSuite,
    *,
    adapter: ExternalModelAdapter,
) -> tuple[ScenarioResult, ...]:
    adapter.ensure_capacity(len(suite.cases))
    results: list[ScenarioResult] = []
    for case in suite.cases:
        observed = adapter.evaluate(case)
        results.append(
            _result(
                case,
                tier=EvaluationTier.EXTERNAL_MODEL,
                observed=observed,
                model_calls=1,
                stub_calls=0,
                side_effect_count=1,
            )
        )
    return tuple(results)
