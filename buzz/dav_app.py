"""FastAPI application for the WebDAV / Real-Debrid front-end."""

import json
import os
import queue
import signal
import threading
import asyncio
from contextlib import asynccontextmanager
from http import HTTPStatus
from urllib import error

import yaml
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pyview.live_socket import pub_sub_hub
from pyview.pyview import liveview_container
from rdapi import RD

from .core.events import record_event
from .core.state import (
    BuzzState,
    InitialSync,
    Poller,
    dav_rel_path,
    read_range_header,
)
from .core.utils import (
    format_bytes,
    http_date,
)
from .dav_protocol import open_remote_media, propfind_body
from .models import (
    AddTorrentRequest,
    DavConfig,
    DeleteTorrentRequest,
    DeleteTrashRequest,
    ErrorResponse,
    RestoreTrashRequest,
    SelectFilesRequest,
    UiNotifyRequest,
    _strip_secrets,
    mask_secrets,
    save_overrides,
    to_nested_dict,
)
from .ui_live import build_ui


def _fetch_opensubtitles_languages() -> list[tuple[str, str]]:
    try:
        import httpx
        resp = httpx.get("https://api.opensubtitles.com/api/v1/infos/languages", timeout=10.0)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [(item["language_code"], item["language_name"]) for item in data]
    except Exception:
        return []


class DavApp:
    """FastAPI wrapper that exposes the RD cache as a WebDAV tree."""

    def __init__(self, config: DavConfig) -> None:
        """Set up the FastAPI app, event registry, and BuzzState."""
        from .core.events import registry
        registry.default_source = "dav"
        registry.reconfigure(config.log_max_entries)

        self.config = config
        os.environ["RD_APITOKEN"] = config.token
        self.client = RD()
        self.ui_loop: asyncio.AbstractEventLoop | None = None
        self.state = BuzzState(config, self.client, on_ui_change=self._notify_ui_change)
        self.opensubtitles_languages = _fetch_opensubtitles_languages()
        self.ui = build_ui(self)
        self._curator_log_level: str = "info"
        registry.add_listener(self._handle_recorded_event)

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            self.ui_loop = asyncio.get_running_loop()
            initial_sync = InitialSync(self.state)
            poller = Poller(self.state)
            initial_sync.start()
            poller.start()
            yield
            poller.stop()
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
            effective = to_nested_dict(self.config)
            masked = mask_secrets(effective)
            overrides = {}
            if self.config._overrides_path.exists():
                try:
                    with open(self.config._overrides_path, encoding="utf-8") as handle:
                        overrides = yaml.safe_load(handle) or {}
                except Exception:  # noqa: BLE001
                    pass
            return {"effective": masked, "overrides": overrides}

        @self.app.post("/api/config")
        def post_config(payload: dict):
            try:
                overrides = payload.get("overrides", {})
                overrides = _strip_secrets(overrides)
                save_overrides(overrides, self.config._overrides_path)
                return {"status": "saved", "restart_required": True}
            except Exception as exc:
                return JSONResponse(status_code=400, content={"error": str(exc)})

        @self.app.post("/api/ui/notify")
        def ui_notify(payload: UiNotifyRequest):
            msg = str(payload.message.get("message", ""))
            level = str(payload.message.get("level", "info")).lower()
            source = str(payload.message.get("source", "dav"))
            event_name = str(payload.message.get("event", ""))
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
            return {
                "status": "ok",
                "log_count": self.log_count(),
                "archive_count": len(self.state.trashcan),
                **self.state.status(),
            }

        @self.app.get("/readyz")
        def readyz():
            from .core.events import registry

            is_ready = self.state.is_ready()
            status_code = HTTPStatus.OK if is_ready else HTTPStatus.SERVICE_UNAVAILABLE
            payload_status = "ready" if is_ready else "starting"
            return JSONResponse(
                status_code=status_code,
                content={
                    "status": payload_status,
                    "log_count": len(registry.events),
                    **self.state.status(),
                },
            )

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
                record_event("Manual library resync triggered")
                self.state.manual_rebuild()
                record_event("Manual library resync completed")
                return {"status": "success"}
            except Exception as exc:
                record_event(f"Manual library resync failed: {exc}", level="error")
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post("/api/subtitles/fetch-torrent")
        def fetch_subs_for_torrent(payload: dict):
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
                    content={"error": "No curator configured"},
                )

            subs_url = self.config.curator_url.replace(
                "/rebuild", "/api/subtitles/fetch"
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

        @self.app.post("/api/restart")
        def restart_service():
            self.restart_service()
            return {"status": "restarting"}

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
                return Response(
                    status_code=200,
                    headers={
                        "Content-Type": "text/plain; charset=utf-8",
                        "Content-Length": "0",
                    },
                )

            if node["type"] == "memory":
                content = node["content"].encode("utf-8")
                size = len(content)
                range_header = read_range_header(request.headers.get("Range"), size)

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

            # Remote media
            size = int(node["size"])
            range_header = read_range_header(request.headers.get("Range"), size)

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
                # We need a generator for StreamingResponse
                def stream_generator():
                    response, first_chunk = open_remote_media(
                        self.state, node, range_header
                    )

                    chunk_size = 64 * 1024
                    buffer_size = self.config.stream_buffer_size

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

                    # Buffered path: background thread reads ahead into a bounded queue.
                    q = queue.Queue(maxsize=max(1, buffer_size // chunk_size))
                    stop_event = threading.Event()

                    def buffer_reader():
                        try:
                            while not stop_event.is_set():
                                chunk = response.read(chunk_size)
                                if not chunk:
                                    break
                                while not stop_event.is_set():
                                    try:
                                        q.put(chunk, timeout=1)
                                        break
                                    except queue.Full:
                                        continue
                        except Exception as exc:
                            print(
                                json.dumps(
                                    {
                                        "event": "buffer_reader_error",
                                        "error": str(exc),
                                    },
                                    sort_keys=True,
                                ),
                                flush=True,
                            )
                        finally:
                            # Signal end-of-stream; use timeout to avoid hanging
                            # if the queue is full and the consumer is gone.
                            while not stop_event.is_set():
                                try:
                                    q.put(None, timeout=1)
                                    break
                                except queue.Full:
                                    continue

                    t = threading.Thread(target=buffer_reader, daemon=True)
                    t.start()

                    try:
                        if first_chunk:
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
                            yield item
                    finally:
                        stop_event.set()
                        t.join(timeout=5)
                        response.close()

                return StreamingResponse(
                    stream_generator(), status_code=status_code, headers=headers
                )
            except error.HTTPError as exc:
                return Response(status_code=exc.code, content=str(exc))
            except ValueError as exc:
                record_event(
                    f"Real-Debrid stream failed: {exc}",
                    event="rd_stream_failed",
                    path=rel,
                    level="error",
                )
                return Response(status_code=502, content=str(exc))

    def fetch_subtitles(self, torrent_name: str) -> dict:
        """Request subtitle fetch for a torrent from the curator."""
        import httpx

        if not self.config.curator_url:
            return {"error": "No curator configured"}

        subs_url = self.config.curator_url.replace(
            "/rebuild", "/api/subtitles/fetch"
        )
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(
                    subs_url, json={"torrent_name": torrent_name}
                )
                return {"status_code": resp.status_code, "data": resp.json()}
        except Exception as exc:
            return {"error": f"Curator unreachable: {exc}"}

    async def _handle_validation_error(
        self, request: Request, exc: Exception
    ) -> JSONResponse:
        first_error = {"msg": "Invalid request"}
        if isinstance(exc, RequestValidationError):
            first_error = (
                exc.errors()[0] if exc.errors() else {"msg": "Invalid request"}
            )
        return JSONResponse(
            status_code=400,
            content={"error": str(first_error.get("msg", "Invalid request"))},
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

    def restart_service(self) -> None:
        record_event("Restart requested via API", level="warning")
        os.kill(os.getpid(), signal.SIGTERM)

    def _handle_recorded_event(self, event: dict) -> None:
        self._notify_ui_topic("buzz:logs", event)
        self._notify_ui_topic("buzz:status", event)

    def _notify_ui_change(self, topic: str) -> None:
        self._notify_ui_topic("buzz:status", {"topic": topic})
        if topic == "archive":
            self._notify_ui_topic("buzz:archive", {"topic": topic})
        elif topic == "sync":
            self._notify_ui_topic("buzz:archive", {"topic": topic})
            self._notify_ui_topic("buzz:logs", {"topic": topic})
        elif topic == "config":
            self._notify_ui_topic("buzz:config", {"topic": topic})

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
    """Start the uvicorn server for the DAV application."""
    import uvicorn

    dav_app = DavApp(config)
    uvicorn.run(dav_app.app, host=config.bind, port=config.port, log_level="info")
