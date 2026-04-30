"""FastAPI application for the WebDAV / Real-Debrid front-end."""

import json
import os
import queue
import threading
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from urllib import error
from typing import Any, Protocol, cast

from fastapi import FastAPI, Request, Response
from starlette.types import ASGIApp
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pyview.live_socket import pub_sub_hub
from pyview.pyview import liveview_container
from rdapi import RD

from .core import db
from .core.events import record_event
from .core.state import (
    BuzzState,
    HosterUnavailableError,
    InitialSync,
    Poller,
    dav_rel_path,
    read_range_header,
)
from .core.tls import ensure_tls_certificate
from .core.utils import (
    format_bytes,
    http_date,
)
from .dav_protocol import (
    _is_transient_connection_error,
    open_remote_media,
    propfind_body,
)
from .models import (
    AddTorrentRequest,
    DavConfig,
    DEFAULT_DAV_CONFIG_PATH,
    DeleteTorrentRequest,
    DeleteTrashRequest,
    FIELD_ANIME_PATTERNS,
    HOT_RELOADABLE_FIELDS,
    RESTART_REQUIRED_FIELDS,
    ErrorResponse,
    RestoreTrashRequest,
    SelectFilesRequest,
    UiNotifyRequest,
    _strip_secrets,
    diff_fields,
    deep_merge,
    filter_paths,
    get_nested_value,
    mask_secrets,
    save_overrides,
    set_nested_value,
    to_nested_dict,
    unknown_config_keys,
)
from .ui_live import build_ui

PATH_REBUILD = "/rebuild"
MSG_NO_CURATOR = "No curator configured"
MSG_INVALID_REQUEST = "Invalid request"
TOPIC_LOGS = "buzz:logs"
TOPIC_CONFIG = "buzz:config"
HTTPS_PORT = 9443
TLS_RENEWAL_CHECK_SECS = 7 * 24 * 60 * 60

UI_REDIRECT_EXACT_PATHS = frozenset({
    "/",
    "/cache",
    "/archive",
    "/logs",
    "/config",
})
UI_REDIRECT_PREFIXES = ("/static/", "/pyview/")
HTTP_TLS_PASSTHROUGH_EXACT_PATHS = frozenset({
    "/api/ui/notify",
    "/healthz",
    "/readyz",
})
HTTP_TLS_PASSTHROUGH_PREFIXES = ("/dav/",)

SNAPSHOT_RELOAD_FIELDS = (
    FIELD_ANIME_PATTERNS,
    "compat.enable_all_dir",
    "compat.enable_unplayable_dir",
    "version_label",
)


class DavOwner(Protocol):
    """Minimal protocol the TLS HTTP companion needs from a DAV owner."""

    @property
    def app(self) -> ASGIApp: ...


def is_ui_redirect_path(path: str) -> bool:
    """Return True if a browser-facing UI route should redirect to HTTPS."""
    return path in UI_REDIRECT_EXACT_PATHS or path.startswith(
        UI_REDIRECT_PREFIXES
    )


def is_http_tls_passthrough_path(path: str) -> bool:
    """Return True if HTTP TLS companion should pass path to DAV app."""
    return (
        path in HTTP_TLS_PASSTHROUGH_EXACT_PATHS
        or path == "/dav"
        or path.startswith(HTTP_TLS_PASSTHROUGH_PREFIXES)
    )


def build_http_tls_companion_app(
    https_port: int,
    dav_owner: DavOwner | None = None,
) -> ASGIApp:
    """Build the HTTP app used while the UI runs on HTTPS."""
    app = dav_owner.app if dav_owner is not None else None
    return _HttpTlsCompanionApp(app, https_port)


class _HttpTlsCompanionApp:
    """HTTP-side ASGI app for TLS mode."""

    def __init__(self, app: ASGIApp | None, https_port: int) -> None:
        self.app = app
        self.https_port = https_port

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            if self.app is not None:
                await self.app(scope, receive, send)
                return
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                    return
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        if scope["type"] != "http":
            if self.app is not None:
                await self.app(scope, receive, send)
                return
            response = Response(status_code=HTTPStatus.NOT_FOUND)
            await response(scope, receive, send)
            return

        path = scope.get("path", "")
        if self.app is None and path in {"/healthz", "/readyz"}:
            status = "ok" if path == "/healthz" else "ready"
            response = JSONResponse({"status": status})
            await response(scope, receive, send)
            return

        if self.app is not None and is_http_tls_passthrough_path(path):
            await self.app(scope, receive, send)
            return

        if is_ui_redirect_path(path):
            headers = {
                key.decode("latin-1"): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            host = headers.get("host", "localhost").split(":", 1)[0]
            query = scope.get("query_string", b"").decode("latin-1")
            path_and_query = f"{path}?{query}" if query else path
            target = f"https://{host}:{self.https_port}{path_and_query}"
            status_code = (
                HTTPStatus.FOUND
                if scope["method"] in ("GET", "HEAD")
                else HTTPStatus.TEMPORARY_REDIRECT
            )
            response = RedirectResponse(target, status_code=status_code)
            await response(scope, receive, send)
            return

        response = Response(status_code=HTTPStatus.NOT_FOUND)
        await response(scope, receive, send)



def build_ui_https_redirect_app(
    https_port: int,
    dav_owner: DavOwner | None = None,
) -> ASGIApp:
    """Build the HTTP companion app for TLS mode."""
    return build_http_tls_companion_app(https_port, dav_owner)


async def _maintain_tls_certificate(
    cert_path: str,
    key_path: str,
    servers: tuple[Any, ...],
    check_interval_secs: int = TLS_RENEWAL_CHECK_SECS,
) -> None:
    """Renew TLS material periodically and stop servers when it changes."""
    while True:
        await asyncio.sleep(check_interval_secs)
        try:
            result = ensure_tls_certificate(cert_path, key_path)
        except Exception as exc:
            record_event(
                f"TLS certificate renewal check failed: {exc}",
                level="error",
                category="error",
            )
            continue
        if not result.generated:
            continue
        record_event(
            "TLS certificate renewed; stopping buzz-dav so it can restart",
            level="warning",
        )
        for server in servers:
            server.should_exit = True
        return


def _fetch_opensubtitles_languages(api_key: str) -> list[tuple[str, str]]:
    try:
        import httpx

        resp = httpx.get(
            "https://api.opensubtitles.com/api/v1/infos/languages",
            timeout=10.0,
            headers={"Api-Key": api_key, "User-Agent": "buzz/1.0"},
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [(item["language_code"], item["language_name"]) for item in data]
    except Exception as exc:
        record_event(
            f"opensubtitles languages fetch failed: {exc}",
            level="warning",
        )
        return []


class DavApp:
    """FastAPI wrapper that exposes the RD cache as a WebDAV tree."""

    def __init__(self, config: DavConfig) -> None:
        """Set up the FastAPI app, event registry, and BuzzState."""
        from .core.events import registry
        registry.default_source = "dav"
        registry.reconfigure(config.log_max_entries)
        registry.verbose = config.verbose

        self.config = config
        self.saved_config = config
        self.config_path = getattr(
            config,
            "_config_path",
            os.environ.get("BUZZ_CONFIG", DEFAULT_DAV_CONFIG_PATH),
        )
        for key in unknown_config_keys(config._file_raw, to_nested_dict(config)):
            record_event(f"unknown config key: {key}", level="warning")
        os.environ["RD_APITOKEN"] = config.token
        self.client = RD() if config.token else None
        self.ui_loop: asyncio.AbstractEventLoop | None = None
        self.state = BuzzState(config, self.client, on_ui_change=self._notify_ui_change)
        self.curator_ready = not bool(config.curator_url)
        self.opensubtitles_languages: list[tuple[str, str]] = []
        self.languages_refreshing = False
        self._language_refresh_lock = threading.Lock()
        self._language_refresh_running = False
        self.ui = build_ui(self)
        self._curator_log_level: str = "info"
        registry.add_listener(self._handle_recorded_event)

        if config.token:
            initial_sync = InitialSync(self.state)
            self._poller: Poller | None = Poller(self.state)
            initial_sync.start()
            self._poller.start()
        else:
            self._poller = None
            self.state.last_error = "Real-Debrid token is not configured."
            self.state.mark_startup_sync_complete()
            record_event("real-Debrid token is not configured", level="error")

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            self.ui_loop = asyncio.get_running_loop()
            self._load_opensubtitles_languages()
            yield
            if self._poller is not None:
                self._poller.stop()
            self.ui_loop = None
            self.state.close()

        self.app = FastAPI(lifespan=lifespan)
        self.app.add_exception_handler(
            RequestValidationError, self._handle_validation_error
        )
        self.app.mount(
            "/static",
            StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
            name="static",
        )
        self.app.mount(
            "/pyview",
            StaticFiles(packages=[("pyview", "static")]),
            name="pyview",
        )

        self._setup_routes()
        websocket_route = next(
            route
            for route in self.ui.routes
            if route.__class__.__name__ == "WebSocketRoute"
        )
        self.app.router.routes.append(websocket_route)

    def _load_opensubtitles_languages(self) -> None:
        """Populate the cache from SQLite; refresh in the background if stale."""
        cached, fetched_at = db.load_opensubtitles_languages(self.state.conn)
        self.opensubtitles_languages = cached
        if self._languages_cache_is_stale(fetched_at):
            self.trigger_language_refresh(force=False)

    def _languages_cache_is_stale(self, fetched_at: str | None) -> bool:
        if not fetched_at:
            return True
        try:
            stamp = datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return True
        return datetime.now(timezone.utc) - stamp > timedelta(days=30)

    def _subtitles_credentials_ready(self) -> bool:
        subs = self.saved_config.subtitles
        return bool(subs.api_key and subs.username and subs.password)

    def trigger_language_refresh(self, force: bool = False) -> bool:
        """Spawn a background fetch if credentials are set.

        Returns ``True`` when a refresh thread was started, ``False`` when
        credentials are missing, the cache is still fresh and *force* is
        ``False``, or a refresh is already in progress.
        """
        if not self._subtitles_credentials_ready():
            return False
        with self._language_refresh_lock:
            # The lock is re-entrant in CPython but we use a simple bool flag
            # checked under the lock to implement single-flight.
            if getattr(self, "_language_refresh_running", False):
                return False
            if not force and not self._languages_cache_is_stale(
                db.load_opensubtitles_languages(self.state.conn)[1]
            ):
                return False
            self._language_refresh_running = True
        self.languages_refreshing = True
        record_event("openSubtitles language refresh started")
        self._notify_ui_change("config", {"languages_refreshing": True})
        threading.Thread(
            target=self._refresh_opensubtitles_languages,
            name="buzz-opensubtitles-languages",
            daemon=True,
        ).start()
        return True

    def _refresh_opensubtitles_languages(self) -> None:
        try:
            api_key = self.saved_config.subtitles.api_key
            languages = _fetch_opensubtitles_languages(api_key)
            if not languages:
                return
            db.save_opensubtitles_languages(self.state.conn, languages)
            self.opensubtitles_languages = languages
            self._notify_ui_change("config")
        finally:
            self._language_refresh_running = False
            self.languages_refreshing = False
            record_event("openSubtitles language refresh finished")
            self._notify_ui_change("config", {"languages_refresh_complete": True})

    def is_ready(self) -> bool:
        return self.state.is_ready() and self.curator_ready

    def is_service_ready(self) -> bool:
        return self.state.is_ready()

    def _setup_routes(self):
        @self.app.get("/", response_class=HTMLResponse)
        @self.app.get("/cache", response_class=HTMLResponse)
        async def cache_page(request: Request):
            return await liveview_container(
                self.ui.rootTemplate,
                self.ui.view_lookup,
                request,
            )

        @self.app.get("/archive", response_class=HTMLResponse)
        async def archive_page(request: Request):
            return await liveview_container(
                self.ui.rootTemplate,
                self.ui.view_lookup,
                request,
            )

        @self.app.get("/logs", response_class=HTMLResponse)
        async def logs_page(request: Request):
            return await liveview_container(
                self.ui.rootTemplate,
                self.ui.view_lookup,
                request,
            )

        @self.app.get("/config", response_class=HTMLResponse)
        async def config_page(request: Request):
            return await liveview_container(
                self.ui.rootTemplate,
                self.ui.view_lookup,
                request,
            )

        @self.app.get("/api/config")
        def get_config():
            return self.config_payload()

        @self.app.post("/api/config")
        def post_config(payload: dict[str, Any]):
            try:
                overrides = payload.get("overrides", {})
                overrides = _strip_secrets(overrides)
                return self.persist_overrides(overrides)
            except Exception as exc:
                return JSONResponse(status_code=400, content={"error": str(exc)})

        @self.app.post("/api/config/restore-defaults")
        def restore_defaults():
            try:
                return self.persist_overrides({})
            except Exception as exc:
                return JSONResponse(status_code=400, content={"error": str(exc)})

        @self.app.post("/api/ui/notify")
        def ui_notify(payload: UiNotifyRequest):
            msg = str(payload.message.get("message", ""))
            level = str(payload.message.get("level", "info")).lower()
            source = str(payload.message.get("source", "dav"))
            event_name = str(payload.message.get("event", ""))
            if source == "curator" and event_name == "curator_ready":
                self.curator_ready = True
            priority = {"error": 3, "warning": 2, "info": 1, "debug": 0}
            if priority.get(level, 0) > priority.get(self._curator_log_level, 0):
                self._curator_log_level = level
            record_event(
                msg,
                level=level,
                source=source,
                event=event_name or None,
            )
            for topic in payload.topics:
                self._notify_ui_topic(
                    f"buzz:{topic}",
                    dict(payload.message),
                )
            return {"status": "ok"}

        @self.app.get("/healthz")
        def healthz():
            return self.healthz_payload()

        @self.app.get("/readyz")
        def readyz():
            return self.readyz_response()

        @self.app.post("/sync")
        def sync():
            try:
                report = self.state.sync()
                return report
            except Exception as exc:
                self.state.last_error = str(exc)
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/cache/add",
            responses={
                400: {"model": ErrorResponse},
                500: {"model": ErrorResponse},
            },
        )
        def add_torrent(payload: AddTorrentRequest):
            try:
                result = self.state.add_magnet(payload.magnet)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/cache/select",
            responses={
                400: {"model": ErrorResponse},
                500: {"model": ErrorResponse},
            },
        )
        def select_files(payload: SelectFilesRequest):
            try:
                result = self.state.select_files(payload.torrent_id, payload.file_ids)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/cache/delete",
            responses={
                400: {"model": ErrorResponse},
                500: {"model": ErrorResponse},
            },
        )
        def delete_torrent(payload: DeleteTorrentRequest):
            try:
                result = self.state.delete_torrent(payload.torrent_id)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/cache/restore",
            responses={
                400: {"model": ErrorResponse},
                500: {"model": ErrorResponse},
            },
        )
        def restore_trash(payload: RestoreTrashRequest):
            try:
                result = self.state.restore_trash(payload.hash)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/cache/delete_permanently",
            responses={
                400: {"model": ErrorResponse},
                500: {"model": ErrorResponse},
            },
        )
        def delete_trash_permanently(payload: DeleteTrashRequest):
            try:
                result = self.state.delete_trash_permanently(payload.hash)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post("/api/curator/rebuild")
        def curator_rebuild():
            try:
                record_event("manual library resync triggered")
                self.state.manual_rebuild()
                record_event("manual library resync completed")
                return {"status": "success"}
            except Exception as exc:
                record_event(f"manual library resync failed: {exc}", level="error")
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post("/api/subtitles/fetch-torrent")
        def fetch_subs_for_torrent(payload: dict[str, Any]):
            import httpx

            torrent_name = payload.get("torrent_name", "").strip()
            if not torrent_name:
                return JSONResponse(
                    status_code=400,
                    content={"error": "torrent_name is required"},
                )

            if not self.config.curator_url:
                return JSONResponse(
                    status_code=400,
                    content={"error": MSG_NO_CURATOR},
                )

            subs_url = self.config.curator_url.replace(
                PATH_REBUILD, "/api/subtitles/fetch"
            )
            try:
                with httpx.Client(timeout=5.0) as client:
                    resp = client.post(
                        subs_url, json={"torrent_name": torrent_name}
                    )
                    return JSONResponse(
                        status_code=resp.status_code,
                        content=resp.json(),
                    )
            except Exception as exc:
                return JSONResponse(
                    status_code=502,
                    content={"error": f"Curator unreachable: {exc}"},
                )

        @self.app.get("/api/logs")
        def get_logs(limit: int = 100):
            return self.get_logs(limit)

        @self.app.options("/dav/{path:path}")
        def options_dav(path: str):
            return Response(
                status_code=204,
                headers={
                    "DAV": "1",
                    "Allow": "OPTIONS, GET, HEAD, PROPFIND",
                    "Content-Length": "0",
                },
            )

        @self.app.api_route("/dav/{path:path}", methods=["PROPFIND"])
        def propfind_dav(path: str, request: Request):
            rel = dav_rel_path(request.url.path)
            node = self.state.lookup(rel)
            if node is None:
                return Response(status_code=404)
            depth = request.headers.get("Depth", "0").strip()
            paths = [rel]
            if depth == "1":
                for child in self.state.list_children(rel):
                    child_path = "/".join(part for part in (rel, child) if part)
                    paths.append(child_path)
            body = propfind_body(self.state, paths)
            return Response(
                content=body,
                status_code=207,
                media_type='application/xml; charset="utf-8"',
            )

        @self.app.get("/dav/{path:path}")
        @self.app.head("/dav/{path:path}")
        def serve_dav(path: str, request: Request):
            send_body = request.method == "GET"
            rel = dav_rel_path(request.url.path)
            node = self.state.lookup(rel)
            if node is None:
                return Response(status_code=404)

            if node["type"] == "dir":
                return self._dav_dir_response()

            if node["type"] == "memory":
                return self._dav_memory_response(
                    node, send_body, request.headers.get("Range")
                )

            return self._dav_remote_response(
                node, send_body, request.headers.get("Range"), rel
            )

    def _dav_dir_response(self) -> Response:
        return Response(
            status_code=200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": "0",
            },
        )

    def _dav_memory_response(
        self, node: dict, send_body: bool, range_str: str | None
    ) -> Response:
        content = node["content"].encode("utf-8")
        size = len(content)
        range_header = read_range_header(range_str, size)

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": node["mime_type"],
            "ETag": node["etag"],
            "Last-Modified": http_date(node.get("modified")),
        }

        if range_header:
            start, end = range_header
            payload = content[start : end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            status_code = 206
        else:
            payload = content
            status_code = 200

        headers["Content-Length"] = str(len(payload))
        return Response(
            content=payload if send_body else None,
            status_code=status_code,
            headers=headers,
        )

    def _dav_remote_response(
        self, node: dict, send_body: bool, range_str: str | None, rel: str
    ) -> Response:
        size = int(node["size"])
        range_header = read_range_header(range_str, size)

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": node["mime_type"],
            "ETag": node["etag"],
            "Last-Modified": http_date(node.get("modified")),
        }

        if range_header:
            start, end = range_header
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(end - start + 1)
            status_code = 206
        else:
            headers["Content-Length"] = str(size)
            status_code = 200

        if not send_body:
            return Response(status_code=status_code, headers=headers)

        try:
            response, first_chunk = open_remote_media(
                self.state, node, range_header
            )
            return StreamingResponse(
                self._stream_remote(
                    response,
                    first_chunk,
                    self.config.stream_buffer_size,
                    node,
                    range_header,
                ),
                status_code=status_code,
                headers=headers,
            )
        except error.HTTPError as exc:
            return Response(status_code=exc.code, content=str(exc))
        except HosterUnavailableError as exc:
            if not exc.cached:
                record_event(
                    f"Real-Debrid hoster unavailable: {exc.code}",
                    event="rd_hoster_unavailable",
                    path=rel,
                    level="warning",
                )
            return Response(
                status_code=503,
                content=str(exc),
                headers={
                    "Retry-After": str(
                        self.config.rd_hoster_failure_cache_secs
                    ),
                },
            )
        except ValueError as exc:
            record_event(
                f"Real-Debrid stream failed: {exc}",
                event="rd_stream_failed",
                path=rel,
                level="warning",
            )
            return Response(status_code=502, content=str(exc))

    def _stream_remote(
        self,
        response: Any,
        first_chunk: bytes,
        buffer_size: int,
        node: dict | None = None,
        range_header: tuple[int, int] | None = None,
    ):
        chunk_size = 64 * 1024

        if buffer_size < chunk_size:
            try:
                if first_chunk:
                    yield first_chunk
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                response.close()
            return

        max_resume_attempts = 3
        size = int(node["size"]) if node is not None else 0
        range_start = range_header[0] if range_header else 0
        range_end = range_header[1] if range_header else max(0, size - 1)
        bytes_sent = 0

        def start_buffer_reader(current_response: Any):
            q = queue.Queue(maxsize=max(1, buffer_size // chunk_size))
            stop_event = threading.Event()

            def buffer_reader():
                try:
                    while not stop_event.is_set():
                        chunk = current_response.read(chunk_size)
                        if not chunk:
                            break
                        while not stop_event.is_set():
                            try:
                                q.put(chunk, timeout=1)
                                break
                            except queue.Full:
                                continue
                except Exception as exc:
                    record_event(
                        f"Real-Debrid stream read interrupted: {exc}",
                        level="debug",
                        event="buffer_reader_error",
                        error=str(exc),
                    )
                    while not stop_event.is_set():
                        try:
                            q.put(exc, timeout=1)
                            break
                        except queue.Full:
                            continue
                    return
                finally:
                    while not stop_event.is_set():
                        try:
                            q.put(None, timeout=1)
                            break
                        except queue.Full:
                            continue

            t = threading.Thread(target=buffer_reader, daemon=True)
            t.start()
            return q, stop_event, t

        q, stop_event, t = start_buffer_reader(response)

        def stop_current_reader() -> None:
            stop_event.set()
            t.join(timeout=5)
            response.close()

        resume_attempts = 0
        try:
            if first_chunk:
                bytes_sent += len(first_chunk)
                yield first_chunk
            while True:
                try:
                    item = q.get(timeout=1)
                except queue.Empty:
                    if not t.is_alive():
                        break
                    continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    stop_current_reader()
                    if (
                        node is None
                        or not _is_transient_connection_error(item)
                        or resume_attempts >= max_resume_attempts
                    ):
                        raise item
                    resume_from = range_start + bytes_sent
                    if resume_from > range_end:
                        break
                    resume_attempts += 1
                    response, first_chunk = open_remote_media(
                        self.state,
                        node,
                        (resume_from, range_end),
                    )
                    q, stop_event, t = start_buffer_reader(response)
                    if first_chunk:
                        bytes_sent += len(first_chunk)
                        yield first_chunk
                    continue
                bytes_sent += len(item)
                yield item
        finally:
            stop_current_reader()

    def fetch_subtitles(self, torrent_name: str) -> dict:
        """Request subtitle fetch for a torrent from the curator."""
        import httpx

        if not self.config.curator_url:
            return {"error": MSG_NO_CURATOR}

        subs_url = self.config.curator_url.replace(
            PATH_REBUILD, "/api/subtitles/fetch"
        )
        try:
            with httpx.Client(timeout=self.config.request_timeout_secs) as client:
                resp = client.post(
                    subs_url, json={"torrent_name": torrent_name}
                )
                return {"status_code": resp.status_code, "data": resp.json()}
        except Exception as exc:
            return {"error": f"Curator unreachable: {exc}"}

    def config_payload(self) -> dict:
        """Return effective and override config data for the UI/API."""
        return {
            "effective": mask_secrets(to_nested_dict(self.saved_config)),
            "overrides": self.saved_config._raw_overrides,
            "restart_required": self.restart_required,
            "restart_required_fields": self.restart_required_fields(),
            "hot_reloaded_fields": [],
        }

    @property
    def restart_required(self) -> bool:
        """Return whether saved config differs from the running process."""
        return bool(self.restart_required_fields())

    def restart_required_fields(self) -> list[str]:
        """Return restart-bound fields that differ from current runtime config."""
        return diff_fields(
            to_nested_dict(self.config),
            to_nested_dict(self.saved_config),
            RESTART_REQUIRED_FIELDS,
        )

    def _curator_reload_url(self) -> str:
        if not self.config.curator_url:
            return ""
        return self.config.curator_url.replace(PATH_REBUILD, "/api/config/reload")

    def _notify_curator_config_reload(self) -> None:
        import httpx

        reload_url = self._curator_reload_url()
        if not reload_url:
            return
        try:
            with httpx.Client(timeout=self.config.request_timeout_secs) as client:
                response = client.post(reload_url)
                response.raise_for_status()
        except Exception as exc:
            record_event(
                f"Curator config reload failed: {exc}",
                level="warning",
                event="curator_config_reload_failed",
            )

    def _apply_runtime_config(
        self,
        new_config: DavConfig,
        hot_fields: list[str],
    ) -> None:
        from .core.events import registry

        snapshot_changed = any(
            field in SNAPSHOT_RELOAD_FIELDS for field in hot_fields
        )
        self.saved_config = new_config
        runtime_effective = to_nested_dict(self.config)
        saved_effective = to_nested_dict(new_config)
        for field in hot_fields:
            value = get_nested_value(saved_effective, field)
            if value is not None:
                set_nested_value(runtime_effective, field, value)
        runtime_config = DavConfig._from_merged_dict(runtime_effective)
        runtime_config._config_path = new_config._config_path
        runtime_config._overrides_path = new_config._overrides_path
        runtime_config._default_raw = new_config._default_raw
        runtime_config._base_raw = new_config._base_raw
        runtime_config._raw_overrides = new_config._raw_overrides
        runtime_config._raw_merged = to_nested_dict(runtime_config)
        self.config = runtime_config
        registry.reconfigure(runtime_config.log_max_entries)
        registry.verbose = runtime_config.verbose
        self.state.apply_config(runtime_config)
        if snapshot_changed:
            self.state.sync(trigger_hook=False)
        self._notify_ui_change("config")
        self._notify_ui_change("sync")

    def persist_overrides(self, overrides: dict) -> dict:
        """Save overrides, hot-apply live-safe fields, and report status."""
        save_overrides(overrides, self.config._overrides_path)
        previous_effective = to_nested_dict(self.config)
        hot_override_subset = filter_paths(overrides, HOT_RELOADABLE_FIELDS)
        hot_fields = diff_fields(
            previous_effective,
            deep_merge(previous_effective, hot_override_subset),
            HOT_RELOADABLE_FIELDS,
        )
        new_config = DavConfig.load(self.config_path)
        self._apply_runtime_config(new_config, hot_fields)
        self._notify_curator_config_reload()
        restart_fields = self.restart_required_fields()
        return {
            "status": "saved",
            "restart_required": bool(restart_fields),
            "restart_required_fields": restart_fields,
            "hot_reloaded_fields": hot_fields,
        }

    def _handle_validation_error(
        self, request: Request, exc: Exception
    ) -> JSONResponse:
        first_error = {"msg": MSG_INVALID_REQUEST}
        if isinstance(exc, RequestValidationError):
            validation_error = cast(RequestValidationError, exc)
            errors = validation_error.errors()
            first_error = errors[0] if errors else {"msg": MSG_INVALID_REQUEST}
        return JSONResponse(
            status_code=400,
            content={"error": str(first_error.get("msg", MSG_INVALID_REQUEST))},
        )

    def get_logs(self, limit: int = 100) -> list[dict]:
        from .core.events import registry

        logs = registry.get_recent(limit)
        for log in logs:
            log.setdefault("source", "dav")

        logs.sort(key=lambda item: item.get("timestamp", ""))
        return logs[-limit:]

    def formatted_logs(self, limit: int = 100) -> list[dict[str, str]]:
        formatted = []
        for log in self.get_logs(limit):
            timestamp = log.get("timestamp", "")
            display_timestamp = timestamp
            if "T" in timestamp and len(timestamp) >= 19:
                display_timestamp = timestamp[11:19]
            level = str(log.get("level", "info")).lower()
            level_label = f"[{level.upper()}]"
            source = "buzz-curator" if log.get("source") == "curator" else "buzz-dav"
            message = str(log.get("message", ""))
            count = int(log.get("count", 1))
            if count > 1:
                message = f"{message} ({count})"
            copy_text = f"{source} {display_timestamp} {level_label} {message}"
            formatted.append(
                {
                    "copy_text": copy_text,
                    "level": level,
                    "level_class": f"log-level-{level}",
                    "level_label": level_label,
                    "message": message,
                    "source": source,
                    "timestamp": display_timestamp,
                }
            )
        return formatted

    def log_count(self) -> int:
        from .core.events import registry

        return len(registry.events)

    def healthz_payload(self) -> dict:
        """Return the health payload used by both HTTP and HTTPS ports."""
        return {
            "status": "ok",
            "log_count": self.log_count(),
            "archive_count": len(self.state.trashcan),
            **self.state.status(),
        }

    def readyz_response(self) -> JSONResponse:
        """Return the readiness response used by both HTTP and HTTPS ports."""
        from .core.events import registry

        is_ready = self.is_service_ready()
        status_code = HTTPStatus.OK if is_ready else HTTPStatus.SERVICE_UNAVAILABLE
        payload_status = "ready" if is_ready else "starting"
        return JSONResponse(
            status_code=status_code,
            content={
                "status": payload_status,
                "log_count": len(registry.events),
                "curator_ready": self.curator_ready,
                "ui_status": "ready" if self.is_ready() else "starting",
                **self.state.status(),
            },
        )

    def _handle_recorded_event(self, event: dict) -> None:
        self._notify_ui_topic(TOPIC_LOGS, event)
        self._notify_ui_topic("buzz:status", event)

    def _notify_ui_change(
        self, topic: str, payload: dict | None = None
    ) -> None:
        message: dict = {"topic": topic}
        if payload:
            message.update(payload)
        self._notify_ui_topic("buzz:status", message)
        if topic == "archive":
            self._notify_ui_topic("buzz:archive", message)
        elif topic == "sync":
            self._notify_ui_topic("buzz:archive", message)
            self._notify_ui_topic(TOPIC_LOGS, message)
        elif topic == "config":
            self._notify_ui_topic(TOPIC_CONFIG, message)

    def _notify_ui_topic(self, topic: str, message: dict) -> None:
        if self.ui_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                pub_sub_hub.send_all_on_topic_async(topic, message),
                self.ui_loop,
            )
        except Exception:
            pass


def run_dav_server(config: DavConfig) -> None:
    """Start the uvicorn server for the DAV application.

    When TLS is enabled, runs two servers concurrently:
    - HTTPS server on HTTPS_PORT serving the full DAV application
    - HTTP server on config.port serving only UI redirects
    """
    import uvicorn
    from uvicorn import Config, Server

    dav_app = DavApp(config)

    if config.tls.cert_path and config.tls.key_path:
        tls_result = ensure_tls_certificate(
            config.tls.cert_path,
            config.tls.key_path,
        )
        if tls_result.generated:
            record_event(
                f"TLS certificate generated at {tls_result.cert_path}",
                level="info",
            )
        redirect_app = build_ui_https_redirect_app(HTTPS_PORT, dav_app)

        async def _run_dual_servers() -> None:
            https_config = Config(
                app=dav_app.app,
                host=config.bind,
                port=HTTPS_PORT,
                ssl_certfile=str(tls_result.cert_path),
                ssl_keyfile=str(tls_result.key_path),
                log_level="info",
            )
            http_config = Config(
                app=redirect_app,
                host=config.bind,
                port=config.port,
                log_level="info",
            )
            https_server = Server(https_config)
            http_server = Server(http_config)
            renewal_task = asyncio.create_task(
                _maintain_tls_certificate(
                    config.tls.cert_path,
                    config.tls.key_path,
                    (https_server, http_server),
                )
            )
            server_task = asyncio.ensure_future(
                asyncio.gather(https_server.serve(), http_server.serve())
            )
            try:
                done, _ = await asyncio.wait(
                    {server_task, renewal_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if renewal_task in done:
                    await server_task
            finally:
                renewal_task.cancel()

        asyncio.run(_run_dual_servers())
    else:
        uvicorn.run(dav_app.app, host=config.bind, port=config.port, log_level="info")
