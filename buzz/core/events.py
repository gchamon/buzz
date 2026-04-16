import json
import threading
from collections import deque
from typing import Any

from .utils import utc_now_iso


class EventRegistry:
    def __init__(self, maxlen: int = 1000, default_source: str = None):
        self.events = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.default_source = default_source

    def record(self, message: str, level: str = "info", **extra: Any):
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

        # Also print to stdout for legacy logging and visibility
        prefix = f"[{level.upper()}]" if level != "info" else ""
        out = f"{prefix} {message}".strip()
        if extra:
            out += f" {json.dumps(extra, sort_keys=True)}"
        print(out, flush=True)

    def get_recent(self, limit: int = 100) -> list[dict]:
        with self.lock:
            return list(self.events)[-limit:]

    def reconfigure(self, maxlen: int) -> None:
        with self.lock:
            self.events = deque(self.events, maxlen=maxlen)


# Global registry for the process
registry = EventRegistry()


def record_event(message: str, level: str = "info", **extra: Any):
    registry.record(message, level, **extra)
