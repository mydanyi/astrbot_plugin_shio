from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Protocol

from .schema import FixtureCase


_SAFE_ALIAS = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")


class ExternalModelError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


class ModelTransport(Protocol):
    def evaluate(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ExternalModelConfig:
    endpoint: str = field(repr=False)
    model_alias: str
    api_key_env: str
    call_budget: int
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not str(self.endpoint).startswith("https://"):
            raise ValueError("external_model_endpoint_invalid")
        if not _SAFE_ALIAS.fullmatch(str(self.model_alias or "")):
            raise ValueError("external_model_alias_invalid")
        if not _ENV_NAME.fullmatch(str(self.api_key_env or "")):
            raise ValueError("external_model_api_key_env_invalid")
        if isinstance(self.call_budget, bool) or not isinstance(self.call_budget, int):
            raise ValueError("external_model_call_budget_invalid")
        if self.call_budget < 1 or self.call_budget > 10000:
            raise ValueError("external_model_call_budget_invalid")
        if not isinstance(self.timeout_seconds, (int, float)):
            raise ValueError("external_model_timeout_invalid")
        if not 0 < float(self.timeout_seconds) <= 300:
            raise ValueError("external_model_timeout_invalid")


def load_external_model_config(path: Path) -> ExternalModelConfig:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ExternalModelError("external_model_config_unreadable") from exc
    if not isinstance(payload, dict):
        raise ExternalModelError("external_model_config_invalid")
    allowed = {
        "endpoint",
        "model_alias",
        "api_key_env",
        "call_budget",
        "timeout_seconds",
    }
    if set(payload).difference(allowed):
        raise ExternalModelError("external_model_config_unknown_field")
    try:
        return ExternalModelConfig(
            endpoint=str(payload.get("endpoint", "")),
            model_alias=str(payload.get("model_alias", "")),
            api_key_env=str(payload.get("api_key_env", "")),
            call_budget=payload.get("call_budget", 0),
            timeout_seconds=payload.get("timeout_seconds", 30.0),
        )
    except (TypeError, ValueError) as exc:
        raise ExternalModelError("external_model_config_invalid") from exc


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(child) for child in value]
    if isinstance(value, list):
        return [_plain(child) for child in value]
    return value


class OpenAICompatibleJsonTransport:
    """Optional real transport; constructed only after explicit CLI opt-in."""

    def __init__(
        self,
        *,
        config: ExternalModelConfig,
        environ: Mapping[str, str] | None = None,
        opener: Any = None,
    ):
        self._config = config
        self._environ = environ if environ is not None else os.environ
        self._opener = opener or urllib.request.urlopen
        if not str(self._environ.get(config.api_key_env, "")).strip():
            raise ExternalModelError("external_model_credential_missing")

    def evaluate(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        instruction = (
            "Return one JSON object containing only the requested typed observation "
            "fields. Do not add prose, hidden reasoning, IDs, URLs, or tool arguments."
        )
        body = {
            "model": self._config.model_alias,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": (
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": json.dumps(_plain(request), ensure_ascii=False),
                },
            ),
        }
        credential = str(self._environ[self._config.api_key_env]).strip()
        http_request = urllib.request.Request(
            self._config.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(
                http_request,
                timeout=float(self._config.timeout_seconds),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            observation = json.loads(content)
        except Exception as exc:
            raise ExternalModelError("external_model_transport_failed") from exc
        if not isinstance(observation, dict) or not observation:
            raise ExternalModelError("external_model_observation_invalid")
        return observation


class ExternalModelAdapter:
    def __init__(
        self,
        *,
        allow_external_model: bool,
        config: ExternalModelConfig | None,
        transport: ModelTransport | None,
    ):
        self._allowed = bool(allow_external_model)
        self._config = config
        self._transport = transport
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def _require_ready(self) -> tuple[ExternalModelConfig, ModelTransport]:
        if not self._allowed:
            raise ExternalModelError("external_model_opt_in_required")
        if self._config is None:
            raise ExternalModelError("external_model_config_required")
        if self._transport is None:
            raise ExternalModelError("external_model_transport_required")
        return self._config, self._transport

    def ensure_capacity(self, planned_calls: int) -> None:
        config, _ = self._require_ready()
        if self._call_count + planned_calls > config.call_budget:
            raise ExternalModelError("external_model_call_budget_insufficient")

    def evaluate(self, case: FixtureCase) -> Mapping[str, Any]:
        config, transport = self._require_ready()
        if self._call_count >= config.call_budget:
            raise ExternalModelError("external_model_call_budget_exceeded")
        request: MutableMapping[str, Any] = {
            "schema_version": 1,
            "case_id": case.case_id,
            "required_phase": case.required_phase,
            "dimensions": {
                name: list(values) for name, values in case.dimensions.items()
            },
            "input_codes": _plain(case.input_spec),
            "stub_state_codes": _plain(case.stub_spec),
            "required_observation_fields": tuple(case.expectation.keys()),
        }
        self._call_count += 1
        try:
            observation = transport.evaluate(request)
        except ExternalModelError:
            raise
        except Exception as exc:
            raise ExternalModelError("external_model_transport_failed") from exc
        if not isinstance(observation, Mapping) or not observation:
            raise ExternalModelError("external_model_observation_invalid")
        return dict(observation)


def create_http_external_model_adapter(
    *,
    allow_external_model: bool,
    config_path: Path | None,
    environ: Mapping[str, str] | None = None,
) -> ExternalModelAdapter:
    if not allow_external_model:
        raise ExternalModelError("external_model_opt_in_required")
    if config_path is None:
        raise ExternalModelError("external_model_config_required")
    config = load_external_model_config(config_path)
    transport = OpenAICompatibleJsonTransport(
        config=config,
        environ=environ,
    )
    return ExternalModelAdapter(
        allow_external_model=True,
        config=config,
        transport=transport,
    )
