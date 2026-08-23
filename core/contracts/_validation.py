from __future__ import annotations

import re
from collections.abc import Iterable


_HEX_16 = re.compile(r"^[0-9a-f]{16}$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")


class ContractViolation(ValueError):
    """Raised when a trusted behavior contract has an impossible shape."""


def require_text(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ContractViolation(f"{field}_required")
    return normalized


def require_safe_name(value: str, field: str) -> str:
    normalized = require_text(value, field).lower()
    if not _SAFE_NAME.fullmatch(normalized):
        raise ContractViolation(f"{field}_invalid")
    return normalized


def require_hex(value: str, field: str, *, lengths: tuple[int, ...]) -> str:
    normalized = str(value or "").strip().lower()
    patterns = {16: _HEX_16, 32: _HEX_32, 64: _HEX_64}
    if not any(patterns[length].fullmatch(normalized) for length in lengths):
        raise ContractViolation(f"{field}_invalid")
    return normalized


def require_score(value: float, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(f"{field}_invalid") from exc
    if not 0.0 <= parsed <= 1.0:
        raise ContractViolation(f"{field}_out_of_range")
    return parsed


def require_nonnegative(value: int | float, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(f"{field}_invalid") from exc
    if parsed < 0:
        raise ContractViolation(f"{field}_negative")
    return parsed


def normalize_reason_codes(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(str(value or "").strip().lower() for value in values))
    if any(not value or not _REASON.fullmatch(value) for value in normalized):
        raise ContractViolation("reason_code_invalid")
    return normalized

