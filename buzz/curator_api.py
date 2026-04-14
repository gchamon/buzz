import json
import signal
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core.curator import Curator, PresentationConfig, RebuildError, build_library


class CuratorHandler(BaseHTTPRequestHandler):
    curator = None

    def do_GET(self):
        if self.path == "/healthz":
            self.respond(HTTPStatus.OK, {"status": "ok"})
            return
        self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if self.path != "/rebuild":
            self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        try:
            report = self.curator.handle_rebuild()
        except Exception as exc:
            payload = {"error": str(exc)}
            if isinstance(exc, RebuildError):
                payload.update(exc.payload)
            print(
                f"curator rebuild failed: {exc}\n{traceback.format_exc()}",
                file=sys.stderr,
                flush=True,
            )
            self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, payload)
            return
        self.respond(HTTPStatus.OK, report)

    def log_message(self, format, *args):
        sys.stdout.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), format % args)
        )

    def respond(self, status: HTTPStatus, payload: dict):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_curator_server(config: PresentationConfig):
    curator = Curator(config)
    CuratorHandler.curator = curator
    if config.build_on_start:
        try:
            startup_report = build_library(config)
            print(
                "initial presentation build complete: "
                f"{startup_report['movies']} movies, "
                f"{startup_report['show_files']} show files, "
                f"{startup_report['anime_files']} anime files",
                flush=True,
            )
        except Exception as exc:
            print(
                f"initial presentation build failed: {exc}", file=sys.stderr, flush=True
            )
    server = ThreadingHTTPServer((config.bind, config.port), CuratorHandler)

    def stop_handler(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    print(f"curator listening on {config.bind}:{config.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        curator.cleanup()
        server.server_close()
        print(f"curator cleaned up {config.target_root}", flush=True)
