import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .core.curator import Curator, PresentationConfig, RebuildError, build_library
from .core.events import record_event


class CuratorApp:
    def __init__(self, config: PresentationConfig):
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
                        "initial presentation build complete: "
                        f"{startup_report['movies']} movies, "
                        f"{startup_report['show_files']} show files, "
                        f"{startup_report['anime_files']} anime files"
                    )
                except Exception as exc:
                    record_event(f"initial presentation build failed: {exc}", level="error")
            yield
            self.curator.cleanup()
            record_event(f"curator cleaned up {self.config.target_root}")

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
        async def rebuild(payload: dict = None):
            changed_roots = (payload or {}).get("changed_roots", [])
            try:
                report = self.curator.handle_rebuild(changed_roots)
                return report
            except Exception as exc:
                payload = {"error": str(exc)}
                if isinstance(exc, RebuildError):
                    payload.update(exc.payload)

                from urllib.error import HTTPError

                if isinstance(exc.__cause__, HTTPError) and exc.__cause__.code in (
                    401,
                    403,
                ):
                    record_event(
                        "curator rebuild failed: Jellyfin API Token is invalid or unauthorized",
                        level="error",
                    )
                    payload["error"] = "Jellyfin API Token is invalid or unauthorized"
                    return JSONResponse(status_code=403, content=payload)

                record_event(
                    f"curator rebuild failed: {exc}\n{traceback.format_exc()}",
                    level="error",
                )
                return JSONResponse(status_code=500, content=payload)


def run_curator_server(config: PresentationConfig):
    import uvicorn

    curator_app = CuratorApp(config)
    uvicorn.run(curator_app.app, host=config.bind, port=config.port, log_level="info")
