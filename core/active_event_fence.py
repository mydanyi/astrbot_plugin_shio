"""Current-plugin-instance ownership of live public AstrBot events."""

from __future__ import annotations

import weakref
from typing import Any


_OWNER_EXTRA = "shio.bug003.active_event_owner"


class ActiveEventFence:
    """Weakly retain only events observed by one live plugin instance."""

    __slots__ = ("__weakref__", "_closed", "_events", "_owner_token")

    def __init__(self) -> None:
        self._closed = False
        self._events: dict[int, weakref.ReferenceType[Any]] = {}
        self._owner_token = object()

    def register(self, event: Any) -> bool:
        """Register one event, or stop it immediately after close."""
        if self._closed:
            event.stop_event()
            return False
        owner = event.get_extra(_OWNER_EXTRA, None)
        if owner is not None and owner is not self._owner_token:
            event.stop_event()
            return False
        key = id(event)
        owner_ref = weakref.ref(self)

        def retire(reference: weakref.ReferenceType[Any]) -> None:
            owner = owner_ref()
            if owner is not None and owner._events.get(key) is reference:
                owner._events.pop(key, None)

        self._events[key] = weakref.ref(event, retire)
        event.set_extra(_OWNER_EXTRA, self._owner_token)
        return True

    def discard(self, event: Any) -> None:
        """Retire the exact event without affecting another identity."""
        key = id(event)
        reference = self._events.get(key)
        if reference is not None and reference() is event:
            self._events.pop(key, None)
            if event.get_extra(_OWNER_EXTRA, None) is self._owner_token:
                event.set_extra(_OWNER_EXTRA, None)

    @staticmethod
    def is_managed(event: Any) -> bool:
        """Return whether any Shio instance has provenance for this event."""
        return event.get_extra(_OWNER_EXTRA, None) is not None

    def owns(self, event: Any) -> bool:
        """Return whether this live instance owns the exact active event."""
        if self._closed:
            return False
        reference = self._events.get(id(event))
        return (
            reference is not None
            and reference() is event
            and event.get_extra(_OWNER_EXTRA, None) is self._owner_token
        )

    def close(self) -> None:
        """Fence registration, stop the current snapshot, and clear ownership."""
        if self._closed:
            return
        self._closed = True
        references = tuple(self._events.values())
        self._events.clear()
        for reference in references:
            event = reference()
            if event is not None:
                event.stop_event()
