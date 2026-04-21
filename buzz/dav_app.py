import hashlib
import json
import os
import queue
import threading
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any
from urllib import error

import jinja2
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from rdapi import RD

from .dav_protocol import open_remote_media, propfind_body
from .core.events import record_event
from .core.utils import (
    format_bytes,
    http_date,
)
from .models import (
    AddTorrentRequest,
    DavConfig,
    DeleteTorrentRequest,
    RestoreTrashRequest,
    DeleteTrashRequest,
    ErrorResponse,
    SelectFilesRequest,
)
from .core.state import (
    BuzzState,
    InitialSync,
    Poller,
    dav_rel_path,
    read_range_header,
)


class DavApp:
    def __init__(self, config: DavConfig):
        from .core.events import registry
        registry.default_source = "dav"
        registry.reconfigure(config.log_max_entries)

        self.config = config
        os.environ["RD_APITOKEN"] = config.token
        self.client = RD()
        self.state = BuzzState(config, self.client)

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            initial_sync = InitialSync(self.state)
            poller = Poller(self.state)
            initial_sync.start()
            poller.start()
            yield
            poller.stop()

        self.app = FastAPI(lifespan=lifespan)
        self.app.add_exception_handler(
            RequestValidationError, self._handle_validation_error
        )
        self.templates = jinja2.Environment(
            loader=jinja2.FileSystemLoader(
                os.path.join(os.path.dirname(__file__), "templates")
            ),
            autoescape=jinja2.select_autoescape(["html", "xml"]),
        )
        self.app.mount(
            "/static",
            StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
            name="static",
        )

        self._setup_routes()

    def _setup_routes(self):
        @self.app.get("/logs", response_class=HTMLResponse)
        def logs_page(request: Request):
            return self._logs_page()

        @self.app.get("/", response_class=HTMLResponse)
        @self.app.get("/torrents", response_class=HTMLResponse)
        def index():
            return self._torrents_page()

        @self.app.get("/trashcan", response_class=HTMLResponse)
        def trashcan():
            return self._trashcan_page()

        @self.app.get("/healthz")
        def healthz():
            from .core.events import registry
            import httpx

            dav_count = len(registry.events)
            curator_count = 0
            if self.config.curator_url:
                try:
                    count_url = self.config.curator_url.replace("/rebuild", "/api/logs/count")
                    with httpx.Client(timeout=1.0) as client:
                        resp = client.get(count_url)
                        if resp.status_code == 200:
                            curator_count = resp.json().get("count", 0)
                except Exception:  # noqa: BLE001
                    pass

            return {
                "status": "ok",
                "log_count": dav_count + curator_count,
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
            "/api/torrents/add",
            responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
        )
        def add_torrent(payload: AddTorrentRequest):
            try:
                result = self.state.add_magnet(payload.magnet)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/torrents/select",
            responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
        )
        def select_files(payload: SelectFilesRequest):
            try:
                result = self.state.select_files(payload.torrent_id, payload.file_ids)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/torrents/delete",
            responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
        )
        def delete_torrent(payload: DeleteTorrentRequest):
            try:
                result = self.state.delete_torrent(payload.torrent_id)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/torrents/restore",
            responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
        )
        def restore_trash(payload: RestoreTrashRequest):
            try:
                result = self.state.restore_trash(payload.hash)
                return result
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

        @self.app.post(
            "/api/torrents/delete_permanently",
            responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
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
                return JSONResponse(status_code=400, content={"error": "torrent_name is required"})

            if not self.config.curator_url:
                return JSONResponse(status_code=400, content={"error": "No curator configured"})

            subs_url = self.config.curator_url.replace("/rebuild", "/api/subtitles/fetch")
            try:
                with httpx.Client(timeout=5.0) as client:
                    resp = client.post(subs_url, json={"torrent_name": torrent_name})
                    return JSONResponse(status_code=resp.status_code, content=resp.json())
            except Exception as exc:
                return JSONResponse(status_code=502, content={"error": f"Curator unreachable: {exc}"})

        @self.app.get("/api/logs")
        def get_logs(limit: int = 100):
            from .core.events import registry
            import httpx

            logs = registry.get_recent(limit)
            for log in logs:
                log.setdefault("source", "dav")

            # Try to fetch from curator
            if self.config.curator_url:
                try:
                    curator_logs_url = self.config.curator_url.replace("/rebuild", "/api/logs")
                    with httpx.Client(timeout=2.0) as client:
                        resp = client.get(f"{curator_logs_url}?limit={limit}")
                        if resp.status_code == 200:
                            curator_logs = resp.json()
                            for log in curator_logs:
                                log.setdefault("source", "curator")
                            logs.extend(curator_logs)
                except Exception:  # noqa: BLE001
                    pass

            logs.sort(key=lambda x: x.get("timestamp", ""))
            return logs[-limit:]

        @self.app.post("/api/restart")
        def restart_service():
            import signal

            record_event("Restart requested via API", level="warning")
            os.kill(os.getpid(), signal.SIGTERM)
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
                                    {"event": "buffer_reader_error", "error": str(exc)},
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

    def _torrents_page(self) -> str:
        from .core.events import registry

        status = self.state.status()
        torrents = self.state.torrents()
        page_torrents = []
        for torrent in torrents:
            page_torrents.append(
                {
                    "id": torrent["id"],
                    "name": torrent["name"],
                    "status": torrent["status"],
                    "progress": torrent["progress"],
                    "bytes": torrent["bytes"],
                    "size": format_bytes(torrent["bytes"]),
                    "selected_files": torrent["selected_files"],
                    "links": torrent["links"],
                    "ended": torrent["ended"] or "-",
                    "short_id": torrent["id"][:8],
                }
            )

        sync_state = "syncing" if status.get("sync_in_progress") else "idle"

        template = self.templates.get_template("torrents.html")
        return template.render(
            torrents_count=len(torrents),
            last_sync_at=status.get("last_sync_at") or "never",
            sync_state=sync_state,
            snapshot_ready="true" if status.get("snapshot_loaded") else "false",
            last_error=status.get("last_error"),
            torrents=page_torrents,
            trash_count=len(self.state.trashcan),
            log_count=len(registry.events),
            subtitle_enabled=self.config.subtitles.enabled,
        )

    def _trashcan_page(self) -> str:
        from .core.events import registry

        status = self.state.status()
        torrents = self.state.trash_torrents()
        trash_torrents = []
        for torrent in torrents:
            trash_torrents.append(
                {
                    "hash": torrent["hash"],
                    "name": torrent["name"],
                    "bytes": torrent["bytes"],
                    "size": format_bytes(torrent["bytes"]),
                    "file_count": torrent["file_count"],
                    "deleted_at": torrent["deleted_at"] or "-",
                }
            )

        sync_state = "syncing" if status.get("sync_in_progress") else "idle"

        template = self.templates.get_template("trashcan.html")
        return template.render(
            torrents_count=len(self.state.torrents()),
            last_sync_at=status.get("last_sync_at") or "never",
            sync_state=sync_state,
            snapshot_ready="true" if status.get("snapshot_loaded") else "false",
            last_error=status.get("last_error"),
            trash_torrents=trash_torrents,
            trash_count=len(trash_torrents),
            log_count=len(registry.events),
        )

    def _logs_page(self) -> str:
        from .core.events import registry

        status = self.state.status()
        sync_state = "syncing" if status.get("sync_in_progress") else "idle"

        template = self.templates.get_template("logs.html")
        return template.render(
            torrents_count=len(self.state.torrents()),
            last_sync_at=status.get("last_sync_at") or "never",
            sync_state=sync_state,
            snapshot_ready="true" if status.get("snapshot_loaded") else "false",
            last_error=status.get("last_error"),
            trash_count=len(self.state.trashcan),
            log_count=len(registry.events),
        )

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


def run_dav_server(config: DavConfig):
    import uvicorn

    dav_app = DavApp(config)
    uvicorn.run(dav_app.app, host=config.bind, port=config.port, log_level="info")
