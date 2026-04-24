"""WebDAV XML response generation and remote media validation."""

import time
from typing import Any
from urllib import error, parse, request
from xml.sax.saxutils import escape

from .core.events import record_event

from .core.media import is_probably_media_content_type, looks_like_markup
from .core.state import BuzzState
from .core.utils import http_date


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
        return state.resolve_download_url(source_url, force_refresh=attempt > 0)
    except Exception as exc:
        state.verbose_log(f"Failed to resolve download URL: {exc}")
        if attempt < max_attempts - 1:
            record_event(
                f"Retrying Real-Debrid stream resolution after failure: {exc}",
                level="warning",
                event="rd_stream_retry",
                path=source_url,
                attempt=attempt + 1,
            )
            time.sleep(0.5 * (attempt + 1))
        raise


def _try_open_stream(
    state: BuzzState,
    download_url: str,
    source_url: str,
    range_header: tuple[int, int] | None,
    attempt: int,
    max_attempts: int,
) -> Any:
    req = request.Request(download_url, method="GET")
    if range_header:
        start, end = range_header
        req.add_header("Range", f"bytes={start}-{end}")
    try:
        return request.urlopen(
            req,
            timeout=max(1, int(state.config.request_timeout_secs)),
        )
    except error.HTTPError as exc:
        state.invalidate_download_url(source_url)
        state.verbose_log(
            f"HTTP Error {exc.code} on attempt {attempt + 1}: {exc.reason}"
        )
        if attempt < max_attempts - 1:
            record_event(
                f"Retrying Real-Debrid stream after upstream HTTP {exc.code}",
                level="warning",
                event="rd_stream_retry",
                path=source_url,
                attempt=attempt + 1,
            )
            time.sleep(0.5 * (attempt + 1))
        raise ValueError(
            f"upstream returned HTTP {exc.code} for {download_url}"
        ) from exc
    except Exception as exc:
        state.invalidate_download_url(source_url)
        state.verbose_log(f"Connection error on attempt {attempt + 1}: {exc}")
        if attempt < max_attempts - 1:
            record_event(
                f"Retrying Real-Debrid stream after connection error: {exc}",
                level="warning",
                event="rd_stream_retry",
                path=source_url,
                attempt=attempt + 1,
            )
            time.sleep(0.5 * (attempt + 1))
        raise ValueError(f"failed to connect to upstream: {exc}") from exc


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
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            download_url = _try_resolve_download_url(
                state, source_url, attempt, max_attempts
            )
        except Exception as exc:
            last_error = str(exc)
            last_exception = exc
            if attempt == max_attempts - 1:
                raise
            continue

        state.verbose_log(
            f"Resolved to {download_url!r} (attempt {attempt + 1}/{max_attempts})"
        )
        try:
            response = _try_open_stream(
                state, download_url, source_url, range_header, attempt, max_attempts
            )
        except ValueError as exc:
            last_error = str(exc)
            last_exception = exc
            if attempt == max_attempts - 1:
                raise
            continue

        try:
            first_chunk = validate_remote_media_response(response, range_header)
            return response, first_chunk
        except ValueError as exc:
            response.close()
            state.invalidate_download_url(source_url)
            last_error = str(exc)
            last_exception = exc
            state.verbose_log(f"Validation failed on attempt {attempt + 1}: {exc}")
            if attempt < max_attempts - 1:
                record_event(
                    f"Retrying Real-Debrid stream after validation error: {exc}",
                    level="warning",
                    event="rd_stream_retry",
                    path=source_url,
                    attempt=attempt + 1,
                )
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
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
