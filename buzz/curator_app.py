import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .core.curator import Curator, PresentationConfig, RebuildError, build_library


class CuratorApp:
    def __init__(self, config: PresentationConfig):
        self.config = config
        self.curator = Curator(config)

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            if self.config.build_on_start:
                try:
                    startup_report = build_library(self.config)
                    print(
                        "initial presentation build complete: "
                        f"{startup_report['movies']} movies, "
                        f"{startup_report['show_files']} show files, "
                        f"{startup_report['anime_files']} anime files",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"initial presentation build failed: {exc}", flush=True)
            yield
            self.curator.cleanup()
            print(f"curator cleaned up {self.config.target_root}", flush=True)

        self.app = FastAPI(lifespan=lifespan)

        @self.app.get("/healthz")
        def healthz():
            return {"status": "ok"}

        @self.app.post("/rebuild")
        def rebuild():
            try:
                report = self.curator.handle_rebuild()
                return report
            except Exception as exc:
                payload = {"error": str(exc)}
                if isinstance(exc, RebuildError):
                    payload.update(exc.payload)
                print(
                    f"curator rebuild failed: {exc}\n{traceback.format_exc()}",
                    flush=True,
                )
                return JSONResponse(status_code=500, content=payload)


def run_curator_server(config: PresentationConfig):
    import uvicorn

    curator_app = CuratorApp(config)
    uvicorn.run(curator_app.app, host=config.bind, port=config.port, log_level="info")
