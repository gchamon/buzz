"""Provider-neutral torrent client contracts and adapters."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

ProviderKind = Literal["real_debrid", "torbox", "local"]

ProviderOperation = Literal[
    "list_torrents",
    "get_torrent",
    "submit_magnet",
    "resolve_magnet",
    "apply_file_selections",
    "delete_torrent",
    "resolve_stream",
]

# How often a pending magnet resolution is re-checked by default.
DEFAULT_RESOLUTION_REFRESH_SECS = 30.0


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


class ProviderErrorCode:
    """Stable error codes for provider ingest operations."""

    INVALID_MAGNET = "invalid_magnet"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    ACCOUNT_LIMIT_REACHED = "account_limit_reached"
    MAGNET_REJECTED = "magnet_rejected"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_PROTOCOL_ERROR = "upstream_protocol_error"
    TORRENT_NOT_FOUND = "torrent_not_found"
    FILE_SELECTION_REJECTED = "file_selection_rejected"
    UNKNOWN = "unknown"


# Codes for which buzz may try the next provider in priority order.
# Everything else (invalid local input, authentication, permission,
# protocol) stops the entry without fallback.
FALLBACK_ELIGIBLE_ERROR_CODES = frozenset({
    ProviderErrorCode.UNSUPPORTED_OPERATION,
    ProviderErrorCode.ACCOUNT_LIMIT_REACHED,
    ProviderErrorCode.MAGNET_REJECTED,
    ProviderErrorCode.RATE_LIMITED,
    ProviderErrorCode.UPSTREAM_TIMEOUT,
    ProviderErrorCode.UPSTREAM_UNAVAILABLE,
})

_RETRYABLE_ERROR_CODES = frozenset({
    ProviderErrorCode.RATE_LIMITED,
    ProviderErrorCode.UPSTREAM_TIMEOUT,
    ProviderErrorCode.UPSTREAM_UNAVAILABLE,
})


class ProviderOperationError(ValueError):
    """Typed provider failure with a stable code and safe UI detail.

    ``detail`` is safe to surface in the UI. ``diagnostic`` carries the
    raw provider-native message for structured logs only; orchestration
    must branch exclusively on ``code``, ``retryable`` and
    ``fallback_eligible``.
    """

    def __init__(
        self,
        provider: str,
        operation: ProviderOperation,
        code: str,
        detail: str = "",
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        diagnostic: str | None = None,
    ) -> None:
        message = f"{provider} {operation} failed: {code}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.provider = provider
        self.operation = operation
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.retry_after = retry_after
        self.diagnostic = diagnostic

    @property
    def fallback_eligible(self) -> bool:
        """Return True when the next provider may be tried for this failure."""
        return self.code in FALLBACK_ELIGIBLE_ERROR_CODES


@dataclass(frozen=True)
class MagnetSubmission:
    """Provider torrent reference returned when a magnet is accepted.

    Acceptance only establishes the torrent reference; it does not imply
    metadata readiness (see :class:`MagnetResolution`).
    """

    torrent_id: str
    reused: bool = False


@dataclass(frozen=True)
class MagnetResolution:
    """Metadata readiness for an accepted provider torrent.

    ``files_ready`` carries normalized metadata and files for selection.
    ``metadata_pending`` is a successful state: the provider accepted the
    torrent but has not resolved its file list yet.
    """

    status: Literal["files_ready", "metadata_pending"]
    name: str | None = None
    bytes: int | None = None
    files: tuple[ProviderFile, ...] = ()
    next_refresh_secs: float | None = None


@dataclass(frozen=True)
class FileSelection:
    """A requested file selection for one provider torrent."""

    torrent_id: str
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class FileSelectionResult:
    """Outcome of a file selection for one provider torrent."""

    torrent_id: str
    ok: bool
    error: ProviderOperationError | None = None


def retry_after_value(raw: str | None) -> float | None:
    """Parse a Retry-After header value into seconds, or None."""
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def httpx_exception_error(
    provider: str, operation: ProviderOperation, exc: BaseException
) -> ProviderOperationError:
    """Map a transport or HTTP exception to a typed provider error."""
    code = ProviderErrorCode.UNKNOWN
    detail = str(exc)
    retryable = False
    retry_after: float | None = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retry_after = retry_after_value(
            exc.response.headers.get("Retry-After")
        )
        if status == 401:
            code = ProviderErrorCode.AUTHENTICATION_FAILED
        elif status == 403:
            code = ProviderErrorCode.PERMISSION_DENIED
        elif status == 404:
            code = ProviderErrorCode.TORRENT_NOT_FOUND
        elif status == 408:
            code = ProviderErrorCode.UPSTREAM_TIMEOUT
            retryable = True
        elif status == 429:
            code = ProviderErrorCode.RATE_LIMITED
            retryable = True
        elif status >= 500:
            code = ProviderErrorCode.UPSTREAM_UNAVAILABLE
            retryable = True
        else:
            code = ProviderErrorCode.UPSTREAM_PROTOCOL_ERROR
        detail = f"HTTP {status}"
    elif isinstance(exc, httpx.TimeoutException):
        code = ProviderErrorCode.UPSTREAM_TIMEOUT
        retryable = True
        detail = "request timed out"
    elif isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError)):
        code = ProviderErrorCode.UPSTREAM_UNAVAILABLE
    elif isinstance(exc, Exception):
        code = ProviderErrorCode.UPSTREAM_UNAVAILABLE
        retryable = True
    return ProviderOperationError(
        provider,
        operation,
        code,
        detail=detail or code,
        retryable=retryable,
        retry_after=retry_after,
    )


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

    def submit_magnet(self, magnet: str) -> MagnetSubmission:
        """Submit a magnet and return the accepted torrent reference.

        Acceptance only establishes the provider torrent reference; it
        does not imply metadata readiness. Raises
        :class:`ProviderOperationError` with a stable code on failure.
        """
        ...

    def resolve_magnet(self, torrent_id: str) -> MagnetResolution:
        """Report metadata readiness for an accepted provider torrent.

        Returns ``files_ready`` with normalized metadata and files, or
        ``metadata_pending`` when the provider has not resolved the file
        list yet. Pending is a success state, not an error.
        """
        ...

    def apply_file_selections(
        self, selections: Sequence[FileSelection]
    ) -> list[FileSelectionResult]:
        """Apply file selections grouped for one provider.

        Adapters use a native batch API where available and perform their
        own sequential requests otherwise; outcomes are reported per
        torrent so partial failures stay visible.
        """
        ...

    def add_magnet(self, magnet: str) -> str:
        """Deprecated: add a magnet and return the provider torrent id.

        Superseded by :meth:`submit_magnet`; no longer called by buzz and
        slated for removal.
        """
        ...

    def select_files(self, torrent_id: str, file_ids: list[str]) -> None:
        """Deprecated: select files for download when the provider supports it.

        Superseded by :meth:`apply_file_selections`; no longer called by
        buzz and slated for removal.
        """
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


LOCAL_STREAM_PREFIX = "local://"


def local_stream_ref(thash: str, path: str) -> str:
    """Return the stream reference for a local file copy."""
    return f"{LOCAL_STREAM_PREFIX}{thash.strip().lower()}/{path.lstrip('/')}"


def split_local_stream_ref(stream_ref: str) -> tuple[str, str]:
    """Split a ``local://<hash>/<path>`` reference into (hash, path)."""
    rest = stream_ref.removeprefix(LOCAL_STREAM_PREFIX)
    thash, _, path = rest.partition("/")
    return thash.strip().lower(), path


def is_local_stream_ref(stream_ref: str) -> bool:
    """Return True for ``local://`` stream references."""
    return stream_ref.startswith(LOCAL_STREAM_PREFIX)


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
