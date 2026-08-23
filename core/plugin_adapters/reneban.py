from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from ..contracts import PluginEvidenceStatus


EXPECTED_PLUGIN_NAME = "astrbot_plugin_reneban"
EXPECTED_HANDLER_MODULE = "data.plugins.astrbot_plugin_reneban.main"
EXPECTED_HANDLER_NAME = "filter_banned_users"
EXPECTED_HANDLER_FULL_NAME = (
    "data.plugins.astrbot_plugin_reneban.main_filter_banned_users"
)
EXPECTED_EVENT_TYPE_NAME = "AdapterMessageEvent"
EXPECTED_PRIORITY = 114
SHIO_PRIORITY = 90


class ReNeBanEvidenceReason(str, Enum):
    PLUGIN_MISSING = "plugin_missing"
    PLUGIN_DISABLED = "plugin_disabled"
    CONTEXT_INTERFACE_CHANGED = "context_interface_changed"
    CONTEXT_ERROR = "context_error"
    METADATA_NAME_CHANGED = "metadata_name_changed"
    METADATA_ACTIVATION_CHANGED = "metadata_activation_changed"
    METADATA_HANDLER_CHANGED = "metadata_handler_changed"
    REGISTRY_INTERFACE_CHANGED = "registry_interface_changed"
    REGISTRY_ERROR = "registry_error"
    HANDLER_MISSING = "handler_missing"
    HANDLER_INTERFACE_CHANGED = "handler_interface_changed"
    HANDLER_DISABLED = "handler_disabled"
    HOOK_ORDER_INVALID = "hook_order_invalid"


@dataclass(frozen=True, slots=True)
class ReNeBanHookEvidence:
    """Content-free hook-conformance evidence; never exposes plugin objects."""

    status: PluginEvidenceStatus
    reason_codes: tuple[ReNeBanEvidenceReason, ...]
    plugin_loaded: bool
    plugin_activated: bool
    handler_present: bool
    handler_enabled: bool
    handler_priority: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PluginEvidenceStatus):
            raise TypeError("reneban_status_invalid")
        reasons = tuple(dict.fromkeys(self.reason_codes))
        if any(not isinstance(reason, ReNeBanEvidenceReason) for reason in reasons):
            raise TypeError("reneban_reason_invalid")
        object.__setattr__(self, "reason_codes", reasons)
        for name in (
            "plugin_loaded",
            "plugin_activated",
            "handler_present",
            "handler_enabled",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError("reneban_evidence_flag_invalid")
        if self.handler_priority is not None and (
            isinstance(self.handler_priority, bool)
            or not isinstance(self.handler_priority, int)
        ):
            raise TypeError("reneban_priority_invalid")
        if self.status is PluginEvidenceStatus.VERIFIED:
            if reasons:
                raise ValueError("verified_reneban_reason_forbidden")
            if not all(
                (
                    self.plugin_loaded,
                    self.plugin_activated,
                    self.handler_present,
                    self.handler_enabled,
                    self.handler_priority == EXPECTED_PRIORITY,
                )
            ):
                raise ValueError("verified_reneban_shape_invalid")
        elif not reasons:
            raise ValueError("degraded_reneban_reason_required")

    @property
    def is_verified(self) -> bool:
        return self.status is PluginEvidenceStatus.VERIFIED

    def trace_metadata(self) -> dict[str, str | int | bool | list[str] | None]:
        return {
            "plugin_status": self.status.value,
            "reason_codes": [reason.value for reason in self.reason_codes],
            "plugin_loaded": self.plugin_loaded,
            "plugin_activated": self.plugin_activated,
            "handler_present": self.handler_present,
            "handler_enabled": self.handler_enabled,
            "handler_priority": self.handler_priority,
            "expected_priority": EXPECTED_PRIORITY,
            "shio_priority": SHIO_PRIORITY,
            "runs_before_shio": (
                self.handler_priority is not None
                and self.handler_priority > SHIO_PRIORITY
            ),
        }


RegistryLoader = Callable[[Any], Iterable[Any] | None]


def _default_registry_loader(context: Any) -> Iterable[Any] | None:
    del context
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    get_handlers = getattr(
        star_handlers_registry,
        "get_handlers_by_event_type",
        None,
    )
    event_type = getattr(EventType, EXPECTED_EVENT_TYPE_NAME, None)
    if not callable(get_handlers) or event_type is None:
        return None

    loaded_handlers = get_handlers(
        event_type,
        only_activated=False,
    )
    if isinstance(loaded_handlers, (str, bytes, Mapping)) or not isinstance(
        loaded_handlers,
        Iterable,
    ):
        return None
    handlers = list(loaded_handlers)
    get_handler_by_full_name = getattr(
        star_handlers_registry,
        "get_handler_by_full_name",
        None,
    )
    if callable(get_handler_by_full_name):
        exact_handler = get_handler_by_full_name(EXPECTED_HANDLER_FULL_NAME)
        if exact_handler is not None and all(
            exact_handler is not handler for handler in handlers
        ):
            handlers.append(exact_handler)

    return tuple(handlers)


def _evidence(
    status: PluginEvidenceStatus,
    reason: ReNeBanEvidenceReason | None,
    *,
    plugin_loaded: bool = False,
    plugin_activated: bool = False,
    handler_present: bool = False,
    handler_enabled: bool = False,
    handler_priority: int | None = None,
) -> ReNeBanHookEvidence:
    return ReNeBanHookEvidence(
        status=status,
        reason_codes=(() if reason is None else (reason,)),
        plugin_loaded=plugin_loaded,
        plugin_activated=plugin_activated,
        handler_present=handler_present,
        handler_enabled=handler_enabled,
        handler_priority=handler_priority,
    )


def _interface_changed(
    reason: ReNeBanEvidenceReason,
    *,
    plugin_loaded: bool = False,
    plugin_activated: bool = False,
    handler_present: bool = False,
    handler_enabled: bool = False,
    handler_priority: int | None = None,
) -> ReNeBanHookEvidence:
    return _evidence(
        PluginEvidenceStatus.INTERFACE_CHANGED,
        reason,
        plugin_loaded=plugin_loaded,
        plugin_activated=plugin_activated,
        handler_present=handler_present,
        handler_enabled=handler_enabled,
        handler_priority=handler_priority,
    )


def inspect_reneban_hook(
    context: Any,
    *,
    registry_loader: RegistryLoader | None = None,
) -> ReNeBanHookEvidence:
    """Inspect only public registration metadata; never query ReNeBan ban state."""

    get_registered_star = getattr(context, "get_registered_star", None)
    if not callable(get_registered_star):
        return _interface_changed(ReNeBanEvidenceReason.CONTEXT_INTERFACE_CHANGED)

    try:
        metadata = get_registered_star(EXPECTED_PLUGIN_NAME)
    except Exception:
        return _evidence(
            PluginEvidenceStatus.ERROR,
            ReNeBanEvidenceReason.CONTEXT_ERROR,
        )
    if metadata is None:
        return _evidence(
            PluginEvidenceStatus.MISSING,
            ReNeBanEvidenceReason.PLUGIN_MISSING,
        )

    try:
        metadata_name = getattr(metadata, "name")
        activated = getattr(metadata, "activated")
        registered_full_names = getattr(metadata, "star_handler_full_names")
    except Exception:
        return _evidence(
            PluginEvidenceStatus.ERROR,
            ReNeBanEvidenceReason.CONTEXT_ERROR,
            plugin_loaded=True,
        )
    if metadata_name != EXPECTED_PLUGIN_NAME:
        return _interface_changed(
            ReNeBanEvidenceReason.METADATA_NAME_CHANGED,
            plugin_loaded=True,
        )
    if type(activated) is not bool:
        return _interface_changed(
            ReNeBanEvidenceReason.METADATA_ACTIVATION_CHANGED,
            plugin_loaded=True,
        )
    if not activated:
        return _evidence(
            PluginEvidenceStatus.DISABLED,
            ReNeBanEvidenceReason.PLUGIN_DISABLED,
            plugin_loaded=True,
        )
    if (
        not isinstance(registered_full_names, (list, tuple, set, frozenset))
        or any(not isinstance(value, str) for value in registered_full_names)
        or EXPECTED_HANDLER_FULL_NAME not in registered_full_names
    ):
        return _interface_changed(
            ReNeBanEvidenceReason.METADATA_HANDLER_CHANGED,
            plugin_loaded=True,
            plugin_activated=True,
        )

    loader = registry_loader or _default_registry_loader
    if not callable(loader):
        return _interface_changed(
            ReNeBanEvidenceReason.REGISTRY_INTERFACE_CHANGED,
            plugin_loaded=True,
            plugin_activated=True,
        )
    try:
        loaded_handlers = loader(context)
        if isinstance(loaded_handlers, (str, bytes, Mapping)) or not isinstance(
            loaded_handlers,
            Iterable,
        ):
            return _interface_changed(
                ReNeBanEvidenceReason.REGISTRY_INTERFACE_CHANGED,
                plugin_loaded=True,
                plugin_activated=True,
            )
        handlers = tuple(loaded_handlers)
    except Exception:
        return _evidence(
            PluginEvidenceStatus.ERROR,
            ReNeBanEvidenceReason.REGISTRY_ERROR,
            plugin_loaded=True,
            plugin_activated=True,
        )

    try:
        related = tuple(
            handler
            for handler in handlers
            if getattr(handler, "handler_full_name", None)
            == EXPECTED_HANDLER_FULL_NAME
        )
    except Exception:
        return _evidence(
            PluginEvidenceStatus.ERROR,
            ReNeBanEvidenceReason.REGISTRY_ERROR,
            plugin_loaded=True,
            plugin_activated=True,
        )
    if not related:
        try:
            identity_changed = any(
                getattr(handler, "handler_module_path", None)
                == EXPECTED_HANDLER_MODULE
                and getattr(handler, "handler_name", None)
                == EXPECTED_HANDLER_NAME
                for handler in handlers
            )
        except Exception:
            return _evidence(
                PluginEvidenceStatus.ERROR,
                ReNeBanEvidenceReason.REGISTRY_ERROR,
                plugin_loaded=True,
                plugin_activated=True,
            )
        return _interface_changed(
            (
                ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED
                if identity_changed
                else ReNeBanEvidenceReason.HANDLER_MISSING
            ),
            plugin_loaded=True,
            plugin_activated=True,
        )
    if len(related) != 1:
        return _interface_changed(
            ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED,
            plugin_loaded=True,
            plugin_activated=True,
            handler_present=True,
        )
    handler = related[0]

    try:
        module = getattr(handler, "handler_module_path")
        name = getattr(handler, "handler_name")
        full_name = getattr(handler, "handler_full_name")
        event_type = getattr(handler, "event_type")
        enabled = getattr(handler, "enabled")
        extras = getattr(handler, "extras_configs")
    except Exception:
        return _evidence(
            PluginEvidenceStatus.ERROR,
            ReNeBanEvidenceReason.REGISTRY_ERROR,
            plugin_loaded=True,
            plugin_activated=True,
            handler_present=True,
        )
    event_type_name = getattr(event_type, "name", None)
    if (
        module != EXPECTED_HANDLER_MODULE
        or name != EXPECTED_HANDLER_NAME
        or full_name != EXPECTED_HANDLER_FULL_NAME
        or event_type_name != EXPECTED_EVENT_TYPE_NAME
    ):
        return _interface_changed(
            ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED,
            plugin_loaded=True,
            plugin_activated=True,
            handler_present=True,
        )
    if type(enabled) is not bool:
        return _interface_changed(
            ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED,
            plugin_loaded=True,
            plugin_activated=True,
            handler_present=True,
        )
    if not enabled:
        return _evidence(
            PluginEvidenceStatus.DISABLED,
            ReNeBanEvidenceReason.HANDLER_DISABLED,
            plugin_loaded=True,
            plugin_activated=True,
            handler_present=True,
        )
    if not isinstance(extras, Mapping):
        return _interface_changed(
            ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED,
            plugin_loaded=True,
            plugin_activated=True,
            handler_present=True,
            handler_enabled=True,
        )
    priority = extras.get("priority")
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or priority != EXPECTED_PRIORITY
        or priority <= SHIO_PRIORITY
    ):
        return _evidence(
            PluginEvidenceStatus.HOOK_ORDER_INVALID,
            ReNeBanEvidenceReason.HOOK_ORDER_INVALID,
            plugin_loaded=True,
            plugin_activated=True,
            handler_present=True,
            handler_enabled=True,
            handler_priority=(
                priority
                if isinstance(priority, int) and not isinstance(priority, bool)
                else None
            ),
        )
    return _evidence(
        PluginEvidenceStatus.VERIFIED,
        None,
        plugin_loaded=True,
        plugin_activated=True,
        handler_present=True,
        handler_enabled=True,
        handler_priority=priority,
    )


__all__ = [
    "EXPECTED_EVENT_TYPE_NAME",
    "EXPECTED_HANDLER_FULL_NAME",
    "EXPECTED_HANDLER_MODULE",
    "EXPECTED_HANDLER_NAME",
    "EXPECTED_PLUGIN_NAME",
    "EXPECTED_PRIORITY",
    "SHIO_PRIORITY",
    "ReNeBanEvidenceReason",
    "ReNeBanHookEvidence",
    "inspect_reneban_hook",
]
