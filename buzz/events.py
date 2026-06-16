"""Structured event log write API.

Events recorded here are stored in the in-memory ring buffer defined in
``buzz.core.events`` and surfaced in the **Logs UI** (``/logs``).  Use this
module rather than importing ``record_event`` from ``buzz.core.events``
directly.
"""

from typing import Any

from .core.events import record_event as _record_event


class Level:
    DEBUG   = "debug"
    INFO    = "info"
    WARNING = "warning"
    ERROR   = "error"


class Event:
    MAGNET_ADD_FAILED     = "magnet_add_failed"
    MAGNET_ADD_OK         = "magnet_add_ok"
    PROVIDER_ADD_FALLBACK = "provider_add_fallback"


def log(message: str, level: str = Level.INFO, **extra: Any) -> None:
    """Record a structured event in the global registry."""
    _record_event(message, level, **extra)
