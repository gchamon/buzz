from typing import Any
from urllib import error, parse, request
from xml.sax.saxutils import escape

from .core.media import is_probably_media_content_type, looks_like_markup
from .core.state import BuzzState
from .core.utils import http_date


def propfind_body(state: BuzzState, paths: list[str]) -> str:
    responses = []
    for rel in paths:
        node = state.lookup(rel)
        if node is None:
            continue
        href_path = "/dav"
        if rel:
            href_path += "/" + parse.quote(rel)
        if node["type"] == "dir":
            prop = (
                "<D:resourcetype><D:collection/></D:resourcetype>"
                "<D:getcontentlength>0</D:getcontentlength>"
            )
        else:
            size = str(int(node.get("size", 0)))
            prop = (
                "<D:resourcetype/>"
                f"<D:getcontentlength>{size}</D:getcontentlength>"
                f"<D:getcontenttype>{escape(node.get('mime_type', 'application/octet-stream'))}</D:getcontenttype>"
                f"<D:getetag>{escape(node.get('etag', ''))}</D:getetag>"
                f"<D:getlastmodified>{escape(http_date(node.get('modified')))}</D:getlastmodified>"
            )
        responses.append(
            "<D:response>"
            f"<D:href>{escape(href_path)}</D:href>"
            "<D:propstat>"
            f"<D:prop>{prop}</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status>"
            "</D:propstat>"
            "</D:response>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>"
    )


def open_remote_media(
    state: BuzzState,
    node: dict[str, Any],
    range_header: tuple[int, int] | None,
) -> tuple[Any, bytes]:
    source_url = str(node.get("source_url") or node.get("url") or "").strip()
    if not source_url:
        raise ValueError("missing Real-Debrid source URL")
    last_error = "unable to resolve upstream media"
    state.verbose_log(f"Opening remote media from {source_url!r}")
    for attempt in range(2):
        try:
            download_url = state.resolve_download_url(
                source_url, force_refresh=attempt == 1
            )
        except Exception as exc:
            last_error = str(exc)
            state.verbose_log(f"Failed to resolve download URL: {exc}")
            if attempt == 0:
                continue
            raise

        state.verbose_log(f"Resolved to {download_url!r} (attempt {attempt + 1}/2)")
        req = request.Request(download_url, method="GET")
        if range_header:
            start, end = range_header
            req.add_header("Range", f"bytes={start}-{end}")
        try:
            response = request.urlopen(req, timeout=60)
        except error.HTTPError as exc:
            state.invalidate_download_url(source_url)
            last_error = f"upstream returned HTTP {exc.code} for {download_url}"
            state.verbose_log(
                f"HTTP Error {exc.code} on attempt {attempt + 1}: {exc.reason}"
            )
            if attempt == 0:
                continue
            raise ValueError(last_error) from exc
        except Exception as exc:
            state.invalidate_download_url(source_url)
            last_error = f"failed to connect to upstream: {exc}"
            state.verbose_log(f"Connection error on attempt {attempt + 1}: {exc}")
            if attempt == 0:
                continue
            raise ValueError(last_error) from exc

        try:
            first_chunk = validate_remote_media_response(response, range_header)
            return response, first_chunk
        except ValueError as exc:
            response.close()
            state.invalidate_download_url(source_url)
            last_error = str(exc)
            state.verbose_log(f"Validation failed on attempt {attempt + 1}: {exc}")
            if attempt == 0:
                continue
            raise
    raise ValueError(last_error)


def validate_remote_media_response(
    response: Any,
    range_header: tuple[int, int] | None,
) -> bytes:
    content_type = response.headers.get("Content-Type")
    if not is_probably_media_content_type(content_type):
        raise ValueError(f"upstream returned non-media content type {content_type!r}")
    should_peek = range_header is None or range_header[0] == 0
    if not should_peek:
        return b""
    first_chunk = response.read(512)
    if first_chunk and looks_like_markup(first_chunk):
        raise ValueError("upstream returned markup instead of media bytes")
    return first_chunk
