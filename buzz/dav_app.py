import hashlib
import json
import os
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any
from urllib import error

import jinja2
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from rdapi import RD

from .dav_protocol import open_remote_media, propfind_body
from .core.utils import (
    format_bytes,
    html_escape,
    http_date,
)
from .models import (
    AddTorrentRequest,
    DavConfig,
    DeleteTorrentRequest,
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
            )
        )

        self._setup_routes()

    def _setup_routes(self):
        @self.app.get("/", response_class=HTMLResponse)
        @self.app.get("/torrents", response_class=HTMLResponse)
        def index():
            return self._torrents_page()

        @self.app.get("/healthz")
        def healthz():
            return {"status": "ok", **self.state.status()}

        @self.app.get("/readyz")
        def readyz():
            is_ready = self.state.is_ready()
            status_code = HTTPStatus.OK if is_ready else HTTPStatus.SERVICE_UNAVAILABLE
            payload_status = "ready" if is_ready else "starting"
            return JSONResponse(
                status_code=status_code,
                content={"status": payload_status, **self.state.status()},
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

        @self.app.post("/api/curator/rebuild")
        def curator_rebuild():
            try:
                self.state.manual_rebuild()
                return {"status": "success"}
            except Exception as exc:
                return JSONResponse(status_code=500, content={"error": str(exc)})

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
                    try:
                        if first_chunk:
                            yield first_chunk
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            yield chunk
                    finally:
                        response.close()

                return StreamingResponse(
                    stream_generator(), status_code=status_code, headers=headers
                )
            except error.HTTPError as exc:
                return Response(status_code=exc.code, content=str(exc))
            except ValueError as exc:
                print(
                    json.dumps(
                        {"event": "rd_stream_failed", "path": rel, "error": str(exc)},
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return Response(status_code=502, content=str(exc))

    def _torrents_page(self) -> str:
        status = self.state.status()
        torrents = self.state.torrents()
        rows = []
        for torrent in torrents:
            torrent_id = torrent["id"]
            rows.append(
                "<tr>"
                f"<td class='name'>{html_escape(torrent['name'])}</td>"
                f'<td><span class="status status-{html_escape(torrent["status"])}">[{html_escape(torrent["status"])}]</span></td>'
                f"<td data-value='{torrent['progress']}'>{html_escape(torrent['progress'])}%</td>"
                f"<td data-value='{torrent['bytes']}'>{html_escape(format_bytes(torrent['bytes']))}</td>"
                f"<td>{html_escape(torrent['selected_files'])}</td>"
                f"<td>{html_escape(torrent['links'])}</td>"
                f"<td class='comment'>{html_escape(torrent['ended'] or '-')}</td>"
                f"<td class='yellow'><code>{html_escape(torrent_id[:8])}</code></td>"
                "<td>"
                f'<div class="delete-container">'
                f'<div class="confirm-opts" id="confirm-{torrent_id}">'
                f'<div class="opt opt-y" onclick="deleteTorrent(\'{torrent_id}\')">[Y]</div>'
                f'<div class="opt opt-n" onclick="toggleDelete(\'{torrent_id}\', false)">[N]</div>'
                "</div>"
                f'<div class="btn-x" id="btn-x-{torrent_id}" onclick="toggleDelete(\'{torrent_id}\', true)">[X]</div>'
                "</div>"
                "</td>"
                "</tr>"
            )
        if not rows:
            rows.append(
                '<tr><td colspan="8" class="empty">No cached torrents yet. '
                "Wait for the first sync or trigger <code>POST /sync</code>.</td></tr>"
            )

        sync_state = "syncing" if status.get("sync_in_progress") else "idle"
        error_html = ""
        if status.get("last_error"):
            error_html = (
                '<div class="error"><span class="label-red">[ERROR]</span> '
                f"{html_escape(status['last_error'])}</div>"
            )

        template = self.templates.get_template("torrents.html")
        return template.render(
            torrents_count=len(torrents),
            last_sync_at=html_escape(status.get("last_sync_at") or "never"),
            sync_state=html_escape(sync_state),
            snapshot_ready=html_escape(
                "true" if status.get("snapshot_loaded") else "false"
            ),
            error_html=error_html,
            rows="".join(rows),
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
