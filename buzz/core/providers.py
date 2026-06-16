"""Provider-neutral torrent client contracts and adapters."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

ProviderKind = Literal["real_debrid", "torbox"]


@dataclass(frozen=True)
class ProviderFile:
    """A normalized file entry from an upstream provider."""

    id: str
    path: str
    bytes: int
    selected: bool = False
    stream_ref: str = ""


@dataclass(frozen=True)
class ProviderTorrentSummary:
    """A normalized torrent summary from an upstream provider."""

    id: str
    name: str
    bytes: int
    progress: float
    status: str
    ended: str | None = None
    stream_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderTorrentDetail:
    """A normalized torrent detail from an upstream provider."""

    id: str
    hash: str
    name: str
    original_name: str
    bytes: int
    progress: float
    status: str
    added: str | None = None
    ended: str | None = None
    files: tuple[ProviderFile, ...] = ()
    stream_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderRequestPolicy:
    """Rate-limit and retry policy for provider API operations."""

    min_interval_secs: float = 0.0
    concurrency: int = 4
    max_attempts: int = 1
    backoff_initial_secs: float = 0.5
    backoff_max_secs: float = 5.0


class ProviderRequestLimiter:
    """Thread-safe limiter/backoff wrapper for provider API calls."""

    retryable_status_codes = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        default_policy: ProviderRequestPolicy | None = None,
        overrides: dict[str, ProviderRequestPolicy] | None = None,
    ) -> None:
        """Initialize a limiter with a default policy and operation overrides."""
        self.default_policy = default_policy or ProviderRequestPolicy()
        self.overrides = dict(overrides or {})
        self._lock = threading.Lock()
        self._last_started_at = 0.0
        self._semaphores: dict[int, threading.BoundedSemaphore] = {}

    def run[T](self, operation: str, call: Callable[[], T]) -> T:
        """Run *call* under the configured policy for *operation*."""
        policy = self.overrides.get(operation, self.default_policy)
        attempts = max(1, int(policy.max_attempts))
        semaphore = self._semaphore_for(policy)
        last_exc: BaseException | None = None

        for attempt in range(attempts):
            retry_exc: Exception | None = None
            with semaphore:
                self._wait_for_interval(policy)
                try:
                    return call()
                except Exception as exc:
                    last_exc = exc
                    if attempt >= attempts - 1 or not self._should_retry(exc):
                        raise
                    retry_exc = exc
            if retry_exc is not None:
                self._sleep_before_retry(retry_exc, attempt, policy)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("provider request failed without an exception")

    def _semaphore_for(
        self, policy: ProviderRequestPolicy
    ) -> threading.BoundedSemaphore:
        concurrency = max(1, int(policy.concurrency))
        with self._lock:
            semaphore = self._semaphores.get(concurrency)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(concurrency)
                self._semaphores[concurrency] = semaphore
            return semaphore

    def _wait_for_interval(self, policy: ProviderRequestPolicy) -> None:
        min_interval = max(0.0, float(policy.min_interval_secs))
        if min_interval <= 0:
            with self._lock:
                self._last_started_at = time.monotonic()
            return

        with self._lock:
            now = time.monotonic()
            wait = self._last_started_at + min_interval - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_started_at = now

    def _should_retry(self, exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in self.retryable_status_codes
        return isinstance(
            exc,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.WriteError,
                httpx.RemoteProtocolError,
                httpx.PoolTimeout,
            ),
        )

    def _sleep_before_retry(
        self, exc: Exception, attempt: int, policy: ProviderRequestPolicy
    ) -> None:
        retry_after = self._retry_after_secs(exc)
        if retry_after is not None:
            time.sleep(retry_after)
            return
        base = max(0.0, float(policy.backoff_initial_secs)) * (2 ** attempt)
        capped = min(max(0.0, float(policy.backoff_max_secs)), base)
        if capped <= 0:
            return
        time.sleep(capped * (0.75 + random.random() * 0.5))

    @staticmethod
    def _retry_after_secs(exc: Exception) -> float | None:
        if not isinstance(exc, httpx.HTTPStatusError):
            return None
        raw = exc.response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None


class ProviderClient(Protocol):
    """Provider contract used by Buzz state and streaming code."""

    kind: ProviderKind

    def list_torrents(self) -> list[ProviderTorrentSummary]:
        """Return normalized torrent summaries."""
        ...

    def get_torrent(self, torrent_id: str) -> ProviderTorrentDetail:
        """Return normalized torrent details."""
        ...

    def add_magnet(self, magnet: str) -> str:
        """Add a magnet and return the provider torrent id."""
        ...

    def select_files(self, torrent_id: str, file_ids: list[str]) -> None:
        """Select files for download when the provider supports it."""
        ...

    def delete_torrent(self, torrent_id: str) -> None:
        """Delete a torrent from the provider account."""
        ...

    def fetch_details(
        self,
        torrent_ids: list[str],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, ProviderTorrentDetail]:
        """Fetch details for the given ids, reporting progress per network call."""
        ...

    def resolve_stream(self, stream_ref: str) -> str:
        """Resolve a provider stream ref to a direct download URL."""
        ...

    def is_healthy(self) -> bool:
        """Check if the provider API is healthy and reachable."""
        ...


def _status(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"downloaded", "completed", "complete", "cached"}:
        return "downloaded"
    if raw in {"error", "dead", "failed"}:
        return "error"
    if raw:
        return raw
    return "unknown"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def split_provider_torrent_id(torrent_id: str) -> tuple[str, str]:
    """Split a prefixed torrent ID into (provider, id) or default to real_debrid."""
    if ":" in torrent_id:
        provider, provider_id = torrent_id.split(":", 1)
        return provider, provider_id
    return "real_debrid", torrent_id


class ProviderDeleteError(ValueError):
    """Provider delete failure with status metadata."""

    def __init__(self, status_code: int | None, text: str, attempts: int = 1) -> None:
        """Initialize the error with status code and response text."""
        super().__init__(text)
        self.status_code = status_code
        self.text = text
        self.attempts = attempts


class ProviderStreamError(ValueError):
    """Provider stream resolution failure."""

    def __init__(self, stream_ref: str, code: str) -> None:
        """Initialize the error with stream reference and error code."""
        super().__init__(f"provider stream unavailable for {stream_ref}: {code}")
        self.stream_ref = stream_ref
        self.code = code
