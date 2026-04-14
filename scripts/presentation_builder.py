import json
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

from buzz.core import curator as curator_mod
from buzz.core.curator import Curator, PresentationConfig as Config, RebuildError


def _log_mapping_event(diff: dict, report: dict, mapping_entries: int):
    print(
        json.dumps(
            {
                "event": "presentation_builder_mapping_diff",
                "mapping_entries": mapping_entries,
                "movies": report["movies"],
                "show_files": report["show_files"],
                "anime_files": report["anime_files"],
                "added": diff["added"],
                "removed": diff["removed"],
                "changed": diff["changed"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


def build_library(config: Config):
    original_log_mapping_event = curator_mod.log_mapping_event
    curator_mod.log_mapping_event = _log_mapping_event
    try:
        return curator_mod.build_library(config)
    finally:
        curator_mod.log_mapping_event = original_log_mapping_event


def trigger_jellyfin_scan(config: Config):
    return curator_mod.trigger_jellyfin_scan(config)


def rebuild_and_trigger(config: Config):
    report = build_library(config)
    if config.skip_jellyfin_scan:
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "skipped_configured"
        report["jellyfin_scan_error"] = None
        return report
    if not config.jellyfin_api_key:
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "skipped_missing_auth"
        report["jellyfin_scan_error"] = None
        return report
    try:
        trigger_jellyfin_scan(config)
    except Exception as exc:
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "failed"
        report["jellyfin_scan_error"] = str(exc)
        raise RebuildError(str(exc), report) from exc
    report["jellyfin_scan_triggered"] = True
    report["jellyfin_scan_status"] = "triggered"
    report["jellyfin_scan_error"] = None
    return report


class Handler(BaseHTTPRequestHandler):
    app = None

    def do_POST(self):
        if self.path != "/rebuild":
            self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        try:
            report = self.app.handle_rebuild()
        except Exception as exc:
            payload = {"error": str(exc)}
            if isinstance(exc, RebuildError):
                payload.update(exc.payload)
            print(
                f"presentation-builder rebuild failed: {exc}\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
                flush=True,
            )
            self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, payload)
            return
        self.respond(HTTPStatus.OK, report)

    def log_message(self, format, *args):
        return

    def respond(self, status: HTTPStatus, payload: dict):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


__all__ = [
    "build_library",
    "Config",
    "Curator",
    "Handler",
    "RebuildError",
    "rebuild_and_trigger",
    "trigger_jellyfin_scan",
]
