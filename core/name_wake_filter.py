from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import Any

try:
    from astrbot.core.star.filter.custom_filter import CustomFilter
except ImportError:  # Repository-only test stub.
    class CustomFilter:  # type: ignore[no-redef]
        def __init__(self, raise_error: bool = True, **kwargs: Any) -> None:
            self.raise_error = raise_error


_active_plugin: weakref.ReferenceType[Any] | None = None


@dataclass(frozen=True, slots=True)
class IngressWakeCandidate:
    """Event-local wake evidence collected before external admission gates.

    The candidate deliberately contains no message body, sender identifier, or
    mutable runtime reference.  WakingCheck may create it, but only the later
    StarRequest admission handler may promote the event or mutate Shio state.
    """

    should_observe: bool
    was_native_wake: bool
    natural_direct: bool
    alias: str = ""
    reason_code: str = "none"

    @property
    def should_promote(self) -> bool:
        return self.natural_direct and not self.was_native_wake

    def trace_metadata(self) -> dict[str, str | bool]:
        return {
            "ingress_candidate": self.should_observe,
            "native_wake": self.was_native_wake,
            "natural_direct": self.natural_direct,
            "has_alias": bool(self.alias),
            "wake_reason": self.reason_code,
        }


def bind_name_wake_plugin(plugin: Any | None) -> None:
    global _active_plugin
    _active_plugin = weakref.ref(plugin) if plugin is not None else None


def unbind_name_wake_plugin(plugin: Any) -> None:
    global _active_plugin
    current = _active_plugin() if _active_plugin is not None else None
    if current is plugin:
        _active_plugin = None


class NaturalNameWakeFilter(CustomFilter):
    """Collect an event-local candidate without committing any Shio state."""

    def filter(self, event: Any, cfg: Any) -> bool:
        plugin = _active_plugin() if _active_plugin is not None else None
        if plugin is None:
            return False
        try:
            return bool(plugin.prepare_ingress_candidate(event))
        except Exception as exc:
            plugin.log_name_wake_filter_error(exc)
            return False


__all__ = [
    "IngressWakeCandidate",
    "NaturalNameWakeFilter",
    "bind_name_wake_plugin",
    "unbind_name_wake_plugin",
]
