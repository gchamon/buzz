"""FastAPI application for the curator service."""

import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .core.curator import Curator, RebuildError, build_library
from .core.events import record_event
from .core.subtitles import (
    background_fetch_subtitles,
)
from .core.subtitles import (
    state as subtitle_state,
)
from .models import CuratorConfig


class CuratorApp:
    """FastAPI wrapper that exposes curator rebuild and subtitle endpoints."""

    def __init__(self, config: CuratorConfig) -> None:
        """Set up the FastAPI app, event registry, and Curator."""
        from .core.events import registry

        registry.default_source = "curator"
        registry.reconfigure(config.log_max_entries)

        self.config = config
        self.curator = Curator(config)

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            if self.config.build_on_start:
                try:
                    startup_report = build_library(self.config)
                    record_event(
                        "initial curator build complete: "
                        f"{startup_report['movies']} movies, "
                        f"{startup_report['show_files']} show files, "
                        f"{startup_report['anime_files']} anime files"
                    )
                except Exception as exc:
                    record_event(
                        f"initial curator build failed: {exc}",
                        level="error",
                    )
            yield
            self.curator.cleanup()
            record_event(
                f"curator cleaned up {self.config.target_root}"
            )

        self.app = FastAPI(lifespan=lifespan)

        @self.app.get("/healthz")
        def healthz():
            return {"status": "ok"}

        @self.app.get("/api/logs")
        def get_logs(limit: int = 100):
            from .core.events import registry

            return registry.get_recent(limit)

        @self.app.get("/api/logs/count")
        def get_logs_count():
            from .core.events import registry

            with registry.lock:
                return {"count": len(registry.events)}

        @self.app.post("/rebuild")
        async def rebuild(payload: dict | None = None):
            changed_roots = (payload or {}).get("changed_roots", [])
            try:
                report = self.curator.handle_rebuild(changed_roots)
                return report
            except Exception as exc:
                payload = {"error": str(exc)}
                if isinstance(exc, RebuildError):
                    payload.update(exc.payload)

                from urllib.error import HTTPError

                cause = exc.__cause__
                if isinstance(cause, HTTPError) and cause.code in (401, 403):
                    record_event(
                        "curator rebuild failed: "
                        "Jellyfin API Token is invalid or unauthorized",
                        level="error",
                    )
                    payload["error"] = (
                        "Jellyfin API Token is invalid or unauthorized"
                    )
                    return JSONResponse(
                        status_code=403, content=payload
                    )

                record_event(
                    f"curator rebuild failed: {exc}\n"
                    f"{traceback.format_exc()}",
                    level="error",
                )
                return JSONResponse(status_code=500, content=payload)

        @self.app.get("/api/subtitles/status")
        def get_subtitles_status():
            if not self.config.subtitles.enabled:
                return {"enabled": False}
            return {
                "enabled": True,
                **subtitle_state.status()
            }

        @self.app.post("/api/subtitles/fetch")
        def trigger_subtitles_fetch(payload: dict | None = None):
            if not self.config.subtitles.enabled:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Subtitles are disabled"},
                )

            if subtitle_state.status()["is_running"]:
                return JSONResponse(
                    status_code=409,
                    content={"error": "Subtitle fetch is already running"},
                )

            torrent_name = (payload or {}).get("torrent_name")
            background_fetch_subtitles(
                self.config, torrent_name=torrent_name
            )
            return {"status": "triggered"}


def run_curator_server(config: CuratorConfig) -> None:
    """Start the curator HTTP server."""
    import uvicorn

    curator_app = CuratorApp(config)
    uvicorn.run(
        curator_app.app,
        host=config.bind,
        port=config.port,
        log_level="info",
    )
