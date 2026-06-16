"""FastAPI application for the curator service."""

import json
import logging
import os
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib import request

import httpx
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from watchfiles import Change, watch

from .core.curator import (
    Curator,
    RebuildError,
    build_library,
    validate_media_server_startup_auth,
)
from .core.events import record_event
from .core.subtitles import (
    state as subtitle_state,
)
from .core.tls import httpx_verify
from .models import CuratorConfig

logger = logging.getLogger(__name__)

WATCHED_SOURCE_CATEGORIES = frozenset({"movies", "shows", "anime"})


def changed_source_roots(
    source_root: Path,
    paths: list[str],
) -> list[str]:
    """Return changed media roots relative to the raw source tree."""
    roots: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        try:
            rel = path.relative_to(source_root)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2 or parts[0] not in WATCHED_SOURCE_CATEGORIES:
            continue
        roots.add(f"{parts[0]}/{parts[1]}")
    return sorted(roots)


class SourceRootWatcher(threading.Thread):
    """Watch the raw source mount and trigger curator rebuilds."""

    def __init__(self, app: CuratorApp) -> None:
        """Initialize a daemon watcher bound to a curator app."""
        super().__init__(daemon=True)
        self.app = app
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Watch the source root until stopped or the watcher fails."""
        source_root = self.app.config.source_root
        if not source_root.exists():
            record_event(
                f"source watcher skipped; source root missing: {source_root}",
                level="warning",
                event="curator_source_watch_skipped",
            )
            return

        try:
            for changes in watch(
                source_root,
                stop_event=self._stop_event,
                debounce=1500,
            ):
                roots = changed_source_roots(
                    source_root,
                    [
                        path
                        for change, path in changes
                        if change
                        in {Change.added, Change.modified, Change.deleted}
                    ],
                )
                if not roots:
                    continue
                record_event(
                    f"source changes detected: {len(roots)} roots",
                    event="curator_source_changes_detected",
                    changed_roots=len(roots),
                    root_paths=roots if len(roots) <= 5 else [],
                )
                self.app._run_rebuild(roots)
        except Exception as exc:  # noqa: BLE001
            record_event(
                f"source watcher failed: {exc}",
                level="error",
                event="curator_source_watch_failed",
            )

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._stop_event.set()


class CuratorApp:
    """FastAPI wrapper that exposes curator rebuild and subtitle endpoints."""

    def __init__(self, config: CuratorConfig) -> None:
        """Set up the FastAPI app, event registry, and Curator."""
        from .core.events import registry

        registry.default_source = "curator"
        registry.reconfigure(config.log_max_entries)
        registry.verbose = config.verbose
        registry.stdout_enabled = False

        self.config = config
        if config.dav_url:
            registry.forward_url = f"{config.dav_url.rstrip('/')}/api/logs/ingest"
            if config.dav_tls_cert:
                registry.forward_verify = config.dav_tls_cert

        self.config_path = getattr(
            config,
            "_config_path",
            os.environ.get("BUZZ_CONFIG", "/app/buzz.yml"),
        )
        self.curator = Curator(config)
        self._source_watcher: SourceRootWatcher | None = None
        self._event_listener = self._notify_dav_ui
        registry.add_listener(self._event_listener)

        self.app = FastAPI(lifespan=self._create_lifespan())
        self._setup_routes()

    def _create_lifespan(self) -> Any:
        """Create a lifespan async context manager for the FastAPI app."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            from .core.events import registry

            try:
                self._startup_tasks()
                yield
            finally:
                if self._source_watcher is not None:
                    self._source_watcher.stop()
                    self._source_watcher.join(timeout=2)
                    self._source_watcher = None
                registry.remove_listener(self._event_listener)

        return lifespan

    def _startup_tasks(self) -> None:
        """Perform Curator-specific startup operations like auth and initial build."""
        try:
            validate_media_server_startup_auth(self.config)
            if (
                self.config.trigger_lib_scan
                and self.config.media_server_kind.strip().lower() == "jellyfin"
            ):
                record_event(
                    "Jellyfin API token validated",
                    event="jellyfin_auth_validated",
                )
        except Exception as exc:
            record_event(f"Curator startup failed: {exc}", level="error")

        ready_emitted = False
        if self.config.build_on_start:
            try:
                startup_report = build_library(self.config)
                record_event(
                    "Curator startup complete: "
                    f"{startup_report['movies']} movies, "
                    f"{startup_report['show_files']} show files, "
                    f"{startup_report['anime_files']} anime files",
                    event="curator_ready",
                )
                ready_emitted = True
            except Exception as exc:
                record_event(f"Curator startup failed: {exc}", level="error")

        if self.config.watch_source_root:
            self._source_watcher = SourceRootWatcher(self)
            self._source_watcher.start()

        if not ready_emitted:
            record_event("Curator startup complete", event="curator_ready")

    def _setup_routes(self) -> None:
        """Register all curator HTTP routes on the internal FastAPI app."""

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
        async def rebuild(
            background_tasks: BackgroundTasks,
            payload: dict | None = None,
        ):
            changed_roots = (payload or {}).get("changed_roots", [])
            task_id = (payload or {}).get("task_id")
            background_tasks.add_task(self._run_rebuild, changed_roots, task_id)
            return {"status": "rebuilding"}

        @self.app.post("/api/config/reload")
        def reload_config():
            self.reload_config()
            return {"status": "reloaded"}

        @self.app.get("/api/subtitles/status")
        def get_subtitles_status():
            if not self.config.subtitles.enabled:
                return {"enabled": False}
            return {"enabled": True, **subtitle_state.status()}

        @self.app.post("/api/subtitles/fetch")
        def trigger_subtitles_fetch(
            background_tasks: BackgroundTasks,
            payload: dict[str, Any] | None = None,
        ):
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
            torrent_names = (payload or {}).get("torrent_names")
            task_id = (payload or {}).get("task_id")
            background_tasks.add_task(
                self._run_subtitle_fetch, torrent_name, task_id, torrent_names
            )
            return {"status": "triggered"}

    def _run_subtitle_fetch(
        self,
        torrent_name: str | None = None,
        task_id: str | None = None,
        torrent_names: list[str] | None = None,
    ) -> None:
        """Background task worker for subtitle fetching."""
        from .core.events import registry
        from .core.subtitles import fetch_subtitles_for_library

        with registry.task_context(task_id or ""):
            error = None
            try:
                # We need a dummy event for fetch_subtitles_for_library if not using task pool locally
                # but curator app uses fastapi background tasks, so we'll pass None and it will handle it.
                # Actually, better to create an event so it doesn't crash on None access if we added it.
                # But our core logic has `if cancel_event: raise_if_cancelled(cancel_event)`
                fetch_subtitles_for_library(
                    self.config,
                    torrent_name=torrent_name,
                    torrent_names=torrent_names,
                )
            except Exception as exc:
                error = str(exc)
                record_event(f"subtitle fetch failed: {exc}", level="error")
            finally:
                if task_id:
                    self._signal_completion(task_id, error=error)

    def reload_config(self) -> None:
        """Reload curator config from disk for future operations."""
        from .core.events import registry

        self.config = CuratorConfig.load(self.config_path)
        self.curator.config = self.config
        registry.reconfigure(self.config.log_max_entries)
        registry.verbose = self.config.verbose
        record_event(
            "curator config reloaded from disk",
            event="curator_config_reloaded",
        )

    def _run_rebuild(self, changed_roots: list[str], task_id: str | None = None) -> None:
        from .core.events import registry

        with registry.task_context(task_id or ""):
            error = None
            status = None
            try:
                self.curator.handle_rebuild(changed_roots)
                record_event("curator rebuild complete", event="curator_rebuild_complete")
            except Exception as exc:
                error = str(exc)
                if "aborted" in error:
                    status = "aborted"

                if isinstance(exc, RebuildError):
                    cause = exc.__cause__
                    from urllib.error import HTTPError

                    if isinstance(cause, HTTPError) and cause.code in (401, 403):
                        record_event(
                            "curator rebuild failed: "
                            "Jellyfin API Token is invalid or unauthorized",
                            level="error",
                        )
                        if task_id:
                            self._signal_completion(task_id, error=error, status=status)
                        return

                record_event(
                    f"curator rebuild failed: {exc}\n"
                    f"{traceback.format_exc()}",
                    level="error",
                )
            finally:
                if task_id:
                    self._signal_completion(task_id, error=error, status=status)

    def _signal_completion(
        self, task_id: str, error: str | None = None, status: str | None = None
    ) -> None:
        """Signal task completion to the DAV service."""
        if not task_id or not self.config.dav_url:
            return

        record_event(
            f"signaling task completion to DAV: {task_id}", level="debug"
        )
        url = f"{self.config.dav_url.rstrip('/')}/api/tasks/{task_id}/complete"
        payload = {}
        if error:
            payload["error"] = error
        if status:
            payload["status"] = status

        # A missing/unreadable cert is a misconfiguration, not a runtime
        # condition: build the verify value outside the try so it propagates
        # and crashes hard instead of being swallowed as a network warning.
        verify = httpx_verify(self.config.dav_tls_cert)

        try:
            with httpx.Client(timeout=10.0, verify=verify) as client:
                response = client.post(url, json=payload)
                if response.status_code not in (200, 204):
                    record_event(
                        f"DAV completion signal returned HTTP {response.status_code}",
                        level="warning",
                    )
        except Exception as exc:
            record_event(f"DAV completion signal failed: {exc}", level="warning")

    def _notify_dav_ui(self, event: dict) -> None:
        if not self.config.dav_ui_notify_url:
            return

        payload = {
            "topics": ["logs", "status"],
            "message": {
                "source": "curator",
                "event": event.get("event"),
                "level": event.get("level"),
                "message": event.get("message", ""),
                "task_id": event.get("task_id", ""),
            },
            "task_id": event.get("task_id", ""),
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.config.dav_ui_notify_url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=2) as response:
                if response.status not in (200, 204):
                    logger.debug(
                        "dav UI notify returned HTTP %s",
                        response.status,
                    )
        except Exception as exc:
            logger.debug("dav UI notify failed: %s", exc)


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
