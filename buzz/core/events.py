"""In-memory event registry with thread-safe ring-buffer storage."""

import json
import queue
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

import httpx

from .tls import httpx_verify
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
        # When False, events are still stored and dispatched to listeners (e.g.
        # the web UI logs panel) but not echoed to stdout. The web server turns
        # this off to keep its console quiet; CLI tools like sync.py leave it on.
        self.stdout_enabled = True
        self._local = threading.local()
        self.forward_url: str | None = None
        self.forward_verify: str | bool = True
        self._forward_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._forward_thread: threading.Thread | None = None

    def _start_forward_thread(self) -> None:
        if self._forward_thread:
            return
        self._forward_thread = threading.Thread(
            target=self._forward_loop, daemon=True
        )
        self._forward_thread.start()

    def _forward_loop(self) -> None:
        while True:
            event = self._forward_queue.get()
            if not self.forward_url:
                continue
            try:
                with httpx.Client(
                    timeout=5.0,
                    verify=httpx_verify(self.forward_verify),
                ) as client:
                    client.post(self.forward_url, json=event)
            except Exception:
                # Silently fail on forwarding errors to avoid log loops
                pass

    @contextmanager
    def task_context(self, task_id: str) -> Iterator[None]:
        """Attach a task id to events recorded in this thread."""
        previous = getattr(self._local, "task_id", None)
        self._local.task_id = task_id
        try:
            yield
        finally:
            if previous is None:
                with suppress(AttributeError):
                    del self._local.task_id
            else:
                self._local.task_id = previous

    def record_raw(self, event: dict[str, Any]) -> None:
        """Store a pre-formatted event without triggering listeners or forwarding."""
        with self.lock:
            self.events.append(event)
            listeners = list(self.listeners)

        for listener in listeners:
            with suppress(Exception):
                listener(event)

    def record(
        self,
        message: str,
        level: str = "info",
        **extra: Any,
    ) -> None:
        """Store an event and print it to stdout."""
        if level == "debug" and not self.verbose:
            return

        # Explicit task_id in extra takes precedence, then thread-local
        task_id = extra.get("task_id")
        if task_id is None:
            task_id = getattr(self._local, "task_id", None)

        event = {
            "timestamp": utc_now_iso(),
            "message": message,
            "level": level,
            "source": extra.get("source") or self.default_source,
            **extra,
        }

        # Preserve task_id if it exists and is not None
        if task_id is not None:
            event["task_id"] = str(task_id)

        event["count"] = 1
        if not event["source"]:
            del event["source"]

        should_print = True
        should_forward = bool(self.forward_url)

        with self.lock:
            last_event = self.events[-1] if self.events else None
            if (
                level in {"warning", "error"}
                and last_event
                and last_event.get("level") == level
                and last_event.get("message") == message
                and last_event.get("source") == event.get("source")
                and last_event.get("task_id") == event.get("task_id")
            ):
                last_event["count"] = int(last_event.get("count", 1)) + 1
                event = last_event
                should_print = False
                # If we're coalescing, we don't re-forward the same line
                should_forward = False
            else:
                self.events.append(event)
            listeners = list(self.listeners)

        for listener in listeners:
            with suppress(Exception):
                listener(event)

        if should_forward:
            if not self._forward_thread:
                self._start_forward_thread()
            self._forward_queue.put(event)

        if should_print and self.stdout_enabled:
            # Also print to stdout for legacy logging and visibility.
            prefix = f"[{level.upper()}]" if level != "info" else ""
            out = f"{prefix} {message}".strip()
            if extra:
                out += f" {json.dumps(extra, sort_keys=True)}"
            print(out, flush=True)

    def get_recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent events, oldest first."""
        with self.lock:
            return list(self.events)[-limit:]

    def clear(self, *, listeners: bool = False) -> None:
        """Remove stored events, optionally dropping registered listeners."""
        with self.lock:
            self.events.clear()
            if listeners:
                self.listeners.clear()

    def reconfigure(self, maxlen: int) -> None:
        """Resize the ring buffer, preserving existing events."""
        with self.lock:
            self.events = deque(self.events, maxlen=maxlen)

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback invoked after each event is recorded."""
        with self.lock:
            self.listeners.append(listener)

    def remove_listener(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> None:
        """Unregister a callback if it is currently registered."""
        with self.lock, suppress(ValueError):
            self.listeners.remove(listener)


# Global registry for the process
registry = EventRegistry()


def record_event(
    message: str, level: str = "info", **extra: Any
) -> None:
    """Record an event in the global registry."""
    registry.record(message, level, **extra)
