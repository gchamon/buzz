"""WebDAV XML response generation and remote media validation."""

import contextlib
import errno
import random
import ssl
import threading
import time
from email.message import Message
from typing import Any
from urllib import error, parse
from xml.sax.saxutils import escape

import httpx

from .core.events import record_event
from .core.media import is_probably_media_content_type, looks_like_markup
from .core.state import BuzzState, HosterUnavailableError
from .core.utils import http_date

_TRANSIENT_ERRNOS = {
    errno.ECONNRESET,
    errno.EPIPE,
    errno.ETIMEDOUT,
    errno.ECONNABORTED,
}


def _is_transient_connection_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like a retryable TLS/TCP transient."""
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(
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
    ):
        return True
    if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_transient_connection_error(cause)
    return False


def is_transient_stream_error(exc: BaseException) -> bool:
    """Return True when a stream failure is a transient upstream transport error."""
    return _is_transient_connection_error(exc)


def _build_upstream_ssl_context() -> ssl.SSLContext:
    """Return a defensive default SSL context for upstream streaming."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with contextlib.suppress(NotImplementedError, ssl.SSLError):
        ctx.set_alpn_protocols(["http/1.1"])
    return ctx


class _HttpxStreamAdapter:
    """Adapt an httpx streaming response to the urllib-style read/close API.

    The validation and streaming code expects an object with ``.headers``,
    ``.read(n)`` and ``.close()``. httpx exposes ``iter_raw`` instead, so we
    bridge with a small internal buffer.
    """

    def __init__(
        self,
        client: httpx.Client,
        response: httpx.Response,
        chunk_size: int = 64 * 1024,
    ) -> None:
        self._client = client
        self._response = response
        self.headers = response.headers
        self._iter = response.iter_raw(chunk_size)
        self._buffer = bytearray()
        self._exhausted = False
        self._closed = False

    def _fill(self, target: int) -> None:
        while not self._exhausted and (
            target < 0 or len(self._buffer) < target
        ):
            try:
                chunk = next(self._iter)
            except StopIteration:
                self._exhausted = True
                break
            if not chunk:
                continue
            self._buffer.extend(chunk)

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            self._fill(-1)
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        if amount == 0:
            return b""
        self._fill(amount)
        if not self._buffer:
            return b""
        take = min(amount, len(self._buffer))
        data = bytes(self._buffer[:take])
        del self._buffer[:take]
        return data

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._client.close()


_upstream_lock = threading.Lock()
_upstream_semaphore: threading.BoundedSemaphore | None = None
_upstream_limit: int = 0


def _get_upstream_semaphore(limit: int) -> threading.BoundedSemaphore:
    """Return the module-level semaphore, rebuilding it if *limit* changed."""
    global _upstream_semaphore, _upstream_limit
    target = max(1, int(limit))
    with _upstream_lock:
        if _upstream_semaphore is None or _upstream_limit != target:
            _upstream_semaphore = threading.BoundedSemaphore(target)
            _upstream_limit = target
        return _upstream_semaphore


def _retry_sleep(attempt: int) -> None:
    """Sleep with exponential backoff and ±25% jitter before the next attempt."""
    base = min(15.0, 0.5 * (2 ** attempt))
    time.sleep(base * (0.75 + random.random() * 0.5))


def _acquire_upstream_slot(state: BuzzState) -> threading.BoundedSemaphore:
    """Acquire a short-lived Real-Debrid setup slot or fail fast."""
    semaphore = _get_upstream_semaphore(state.config.connection_concurrency)
    timeout = max(1, int(state.config.request_timeout_secs))
    if semaphore.acquire(timeout=timeout):
        return semaphore
    raise ValueError(
        "upstream connection limit reached while opening Real-Debrid media"
    )


def propfind_body(state: BuzzState, paths: list[str]) -> str:
    """Build a WebDAV PROPFIND multistatus XML body for the given paths."""
    responses = []
    for rel in paths:
        node = state.lookup(rel)
        if node is None:
            continue
        href_path = "/dav"
        if rel:
            href_path += "/" + parse.quote(rel)
        if node["type"] == "dir":
            prop = "<D:resourcetype><D:collection/></D:resourcetype>" \
                   "<D:getcontentlength>0</D:getcontentlength>"
        else:
            size = str(int(node.get("size", 0)))
            mime = escape(node.get("mime_type", "application/octet-stream"))
            etag = escape(node.get("etag", ""))
            modified = escape(http_date(node.get("modified")))
            prop = "<D:resourcetype/>" \
                   f"<D:getcontentlength>{size}</D:getcontentlength>" \
                   f"<D:getcontenttype>{mime}</D:getcontenttype>" \
                   f"<D:getetag>{etag}</D:getetag>" \
                   f"<D:getlastmodified>{modified}</D:getlastmodified>"
        responses.append(
            "<D:response>"
            f"<D:href>{escape(href_path)}</D:href>"
            "<D:propstat>"
            f"<D:prop>{prop}</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status>"
            "</D:propstat>"
            "</D:response>"
        )
    return '<?xml version="1.0" encoding="utf-8"?>' \
           '<D:multistatus xmlns:D="DAV:">' \
           + "".join(responses) \
           + "</D:multistatus>"


def _try_resolve_download_url(
    state: BuzzState,
    source_url: str,
    attempt: int,
    max_attempts: int,
) -> str:
    try:
        return state.resolve_download_url(source_url)
    except HosterUnavailableError as exc:
        state.verbose_log(f"Hoster unavailable: {exc}")
        raise
    except Exception as exc:
        state.verbose_log(f"Failed to resolve download URL: {exc}")
        if attempt < max_attempts - 1:
            record_event(
                f"retrying Real-Debrid stream resolution after failure: {exc}",
                level="debug",
                event="rd_stream_retry",
                path=source_url,
                attempt=attempt + 1,
            )
            _retry_sleep(attempt)
        raise


def _open_upstream_response(
    url: str,
    headers: dict[str, str],
    timeout_secs: int,
) -> Any:
    """Open an upstream streaming GET request.

    Returns an object with a urllib-style ``.headers/.read/.close`` API. This
    is the single seam that the streaming code relies on; tests patch this
    function to inject fakes.
    """
    timeout = httpx.Timeout(
        connect=10.0,
        read=float(timeout_secs),
        write=10.0,
        pool=5.0,
    )
    client = httpx.Client(
        http2=False,
        timeout=timeout,
        verify=_build_upstream_ssl_context(),
        follow_redirects=True,
    )
    try:
        response = client.send(
            client.build_request("GET", url, headers=headers),
            stream=True,
        )
    except BaseException:
        client.close()
        raise
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        try:
            response.close()
        finally:
            client.close()
        raise error.HTTPError(
            url,
            code,
            exc.response.reason_phrase or "",
            hdrs=Message(),
            fp=None,
        ) from exc
    except BaseException:
        try:
            response.close()
        finally:
            client.close()
        raise
    return _HttpxStreamAdapter(client, response)


def _try_open_stream(
    state: BuzzState,
    download_url: str,
    source_url: str,
    range_header: tuple[int, int] | None,
    attempt: int,
    max_attempts: int,
) -> Any:
    headers: dict[str, str] = {}
    if range_header:
        start, end = range_header
        headers["Range"] = f"bytes={start}-{end}"
    try:
        return _open_upstream_response(
            download_url,
            headers,
            max(1, int(state.config.request_timeout_secs)),
        )
    except error.HTTPError as exc:
        state.invalidate_download_url(source_url)
        state.verbose_log(
            f"HTTP Error {exc.code} on attempt {attempt + 1}: {exc.reason}"
        )
        if attempt < max_attempts - 1:
            record_event(
                f"retrying Real-Debrid stream after upstream HTTP {exc.code}",
                level="debug",
                event="rd_stream_retry",
                path=source_url,
                attempt=attempt + 1,
            )
        raise ValueError(
            f"upstream returned HTTP {exc.code} for {download_url}"
        ) from exc
    except Exception as exc:
        transient = _is_transient_connection_error(exc)
        state.verbose_log(
            f"Connection error on attempt {attempt + 1} "
            f"(transient={transient}): {exc}"
        )
        if attempt < max_attempts - 1:
            record_event(
                f"retrying Real-Debrid stream after connection error: {exc}",
                level="debug",
                event="rd_stream_retry",
                path=source_url,
                attempt=attempt + 1,
            )
        raise ValueError(
            f"failed to connect to upstream: {exc}"
        ) from exc


def open_remote_media(
    state: BuzzState,
    node: dict[str, Any],
    range_header: tuple[int, int] | None,
) -> tuple[Any, bytes]:
    """Resolve and open a remote media stream with retry logic."""
    source_url = str(node.get("source_url") or node.get("url") or "").strip()
    if not source_url:
        raise ValueError("missing Real-Debrid source URL")
    last_error = "unable to resolve upstream media"
    last_exception: Exception | None = None
    state.verbose_log(f"Opening remote media from {source_url!r}")
    max_attempts = 6
    resolution_max_attempts = 3
    resolution_failures = 0
    for attempt in range(max_attempts):
        try:
            download_url = _try_resolve_download_url(
                state, source_url, attempt, resolution_max_attempts
            )
        except HosterUnavailableError:
            raise
        except Exception as exc:
            last_error = str(exc)
            last_exception = exc
            resolution_failures += 1
            if resolution_failures >= resolution_max_attempts:
                raise
            continue

        state.verbose_log(
            f"Resolved to {download_url!r} (attempt {attempt + 1}/{max_attempts})"
        )
        response = None
        semaphore = None
        try:
            semaphore = _acquire_upstream_slot(state)
            response = _try_open_stream(
                state, download_url, source_url, range_header, attempt, max_attempts
            )
            first_chunk = validate_remote_media_response(response, range_header)
            semaphore.release()
            semaphore = None
            return response, first_chunk
        except ValueError as exc:
            if response is not None:
                response.close()
                state.invalidate_download_url(source_url)
            if semaphore is not None:
                semaphore.release()
                semaphore = None
            last_error = str(exc)
            last_exception = exc
            if response is not None:
                state.verbose_log(
                    f"Validation failed on attempt {attempt + 1}: {exc}"
                )
            if attempt == max_attempts - 1:
                raise
            if response is not None:
                record_event(
                    f"retrying Real-Debrid stream after validation error: {exc}",
                    level="debug",
                    event="rd_stream_retry",
                    path=source_url,
                    attempt=attempt + 1,
                )
            _retry_sleep(attempt)
            continue
        finally:
            if semaphore is not None:
                semaphore.release()
    record_event(
        f"Real-Debrid stream exhausted retries ({max_attempts} attempts); "
        f"last error: {last_error}",
        level="debug" if (
            last_exception is not None
            and is_transient_stream_error(last_exception)
        ) else "error",
        event="rd_stream_exhausted",
        path=source_url,
    )
    raise ValueError(last_error) from last_exception


def validate_remote_media_response(
    response: Any,
    range_header: tuple[int, int] | None,
) -> bytes:
    """Validate that a remote response is actually media, not markup.

    Returns the first chunk of the body if the response passes validation.
    """
    content_type = response.headers.get("Content-Type")
    if not is_probably_media_content_type(content_type):
        raise ValueError(
            f"upstream returned non-media content type {content_type!r}"
        )
    should_peek = range_header is None or range_header[0] == 0
    if not should_peek:
        return b""
    first_chunk = response.read(512)
    if first_chunk and looks_like_markup(first_chunk):
        raise ValueError("upstream returned markup instead of media bytes")
    return first_chunk
