"""In-memory event registry with thread-safe ring-buffer storage."""

import json
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from .utils import utc_now_iso


class EventRegistry:
    """Thread-safe ring buffer for structured log-style events."""

    def __init__(
        self,
        maxlen: int = 1000,
        default_source: str | None = None,
    ) -> None:
        """Initialize the ring buffer with capacity *maxlen*."""
        self.events = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.default_source = default_source
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self.verbose = False

    def record(
        self,
        message: str,
        level: str = "info",
        **extra: Any,
    ) -> None:
        """Store an event and print it to stdout."""
        if level == "debug" and not self.verbose:
            return
        event = {
            "timestamp": utc_now_iso(),
            "message": message,
            "level": level,
            "source": extra.get("source") or self.default_source,
            **extra,
        }
        if not event["source"]:
            del event["source"]
        with self.lock:
            self.events.append(event)
            listeners = list(self.listeners)

        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass

        # Also print to stdout for legacy logging and visibility
        prefix = f"[{level.upper()}]" if level != "info" else ""
        out = f"{prefix} {message}".strip()
        if extra:
            out += f" {json.dumps(extra, sort_keys=True)}"
        print(out, flush=True)

    def get_recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent events, oldest first."""
        with self.lock:
            return list(self.events)[-limit:]

    def reconfigure(self, maxlen: int) -> None:
        """Resize the ring buffer, preserving existing events."""
        with self.lock:
            self.events = deque(self.events, maxlen=maxlen)

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback invoked after each event is recorded."""
        with self.lock:
            self.listeners.append(listener)


# Global registry for the process
registry = EventRegistry()


def record_event(
    message: str, level: str = "info", **extra: Any
) -> None:
    """Record an event in the global registry."""
    registry.record(message, level, **extra)
