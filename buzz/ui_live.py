"""PyView-backed operator pages for the Buzz management UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, TypedDict, TypeVar, cast
from urllib.parse import ParseResult

import yaml
from markupsafe import Markup
from pyview import (
    ConnectedLiveViewSocket,
    LiveView,
    LiveViewSocket,
    PyView,
    is_connected,
)
from pyview.events import InfoEvent, info
from pyview.template import LiveRender, RenderedContent, template_file
from pyview.vendor import ibis
from pyview.vendor.ibis.loaders import FileReloader

from .core.utils import format_bytes

CSS_STATUS_GREEN = "service-status-green"
CSS_STATUS_RED = "service-status-red"
CSS_STATUS_YELLOW = "service-status-yellow"
TOPIC_STATUS = "buzz:status"
TOPIC_ARCHIVE = "buzz:archive"
TOPIC_LOGS = "buzz:logs"
TOPIC_CONFIG = "buzz:config"
EVENT_NAVIGATE = "navigate"
from .models import (
    FIELD_ANIME_PATTERNS,
    FIELD_SUBTITLES_LANGUAGES,
    HOT_RELOADABLE_FIELDS,
    RESTART_REQUIRED_FIELDS,
    UI_MANAGED_CONFIG_FIELDS,
    deep_merge,
    diff_fields,
    effective_override_field_paths,
    get_nested_value,
    load_base_and_overrides,
    mask_secrets,
    to_nested_dict,
)

_TEMPLATE_DIR = Path(__file__).with_name("pyview_templates")
ibis.loader = FileReloader(str(_TEMPLATE_DIR))
PAGE_NAMES = ("cache", "archive", "logs", "config")
PageName = Literal["cache", "archive", "logs", "config"]

_CONFIG_BOOL_FIELDS = (
    "compat.enable_all_dir",
    "compat.enable_unplayable_dir",
    "logging.verbose",
    "media_server.trigger_lib_scan",
    "subtitles.enabled",
    "subtitles.fetch_on_resync",
    "subtitles.filters.exclude_ai",
    "subtitles.filters.exclude_machine",
)
_CONFIG_NUMBER_FIELDS = (
    "provider.poll_interval_secs",
    "ui.poll_interval_secs",
    "provider.connection_concurrency",
    "server.port",
    "server.stream_buffer_size",
    "hooks.rd_update_delay_secs",
    "request_timeout_secs",
    "subtitles.search_delay_secs",
    "subtitles.download_delay_secs",
)
_LIBRARY_MAP_KEYS = ("movies", "shows", "anime")
_FIELD_LIBRARY_MAP = "media_server.library_map"
_LIBRARY_MAP_FIELDS = tuple(
    f"{_FIELD_LIBRARY_MAP}.{key}" for key in _LIBRARY_MAP_KEYS
)

_CONFIG_TRACKED_FIELDS = (
    "provider.poll_interval_secs",
    "ui.poll_interval_secs",
    "provider.connection_concurrency",
    "server.bind",
    "server.port",
    "server.stream_buffer_size",
    "hooks.on_library_change",
    "hooks.curator_url",
    "hooks.rd_update_delay_secs",
    "compat.enable_all_dir",
    "compat.enable_unplayable_dir",
    FIELD_ANIME_PATTERNS,
    "request_timeout_secs",
    "logging.verbose",
    "version_label",
    "media_server.kind",
    "media_server.trigger_lib_scan",
    "media_server.jellyfin.url",
    "media_server.jellyfin.api_key",
    "media_server.jellyfin.scan_task_id",
    "media_server.plex.url",
    "media_server.plex.token",
    *_LIBRARY_MAP_FIELDS,
    "subtitles.enabled",
    "subtitles.fetch_on_resync",
    FIELD_SUBTITLES_LANGUAGES,
    "subtitles.strategy",
    "subtitles.filters.hearing_impaired",
    "subtitles.filters.exclude_ai",
    "subtitles.filters.exclude_machine",
    "subtitles.search_delay_secs",
    "subtitles.download_delay_secs",
)


class PageItem(TypedDict):
    label: str
    value: str


class PageNav(TypedDict):
    archive_count: int
    cache_active: bool
    archive_active: bool
    logs_active: bool
    config_active: bool
    log_count: int
    log_level: str


class PageContext(TypedDict):
    console_class: str
    console_msg: str
    has_error: bool
    is_ready: bool
    last_error: str
    meta_items: list[PageItem]
    nav: PageNav
    status_class: str
    status_label: str
    token_configured: bool


class CacheFileItem(TypedDict):
    id: str
    path: str
    bytes: int
    size: str
    is_video: bool
    selected: bool


class CacheAnalysisResult(TypedDict):
    torrent_id: str
    filename: str
    files: list[CacheFileItem]


class CacheTorrentItem(TypedDict):
    id: str
    name: str
    status: str
    progress: int
    bytes: int
    size: str
    selected_files: int
    links: int
    ended: str
    short_id: str


class ArchiveItem(TypedDict):
    bytes: int
    deleted_at: str
    file_count: int
    hash: str
    name: str
    size: str


class CacheContext(PageContext):
    analysis_error: str
    analysis_results: list[CacheAnalysisResult]
    analyzing: bool
    caching: bool
    confirm_delete_id: str | None
    has_multiple_analysis_results: bool
    has_torrents: bool
    magnet_inputs: list[str]
    show_overlay: bool
    sort_col: int
    sort_dir: str
    subtitle_enabled: bool
    torrents: list[CacheTorrentItem]


class ArchiveContext(PageContext):
    archive_items: list[ArchiveItem]
    confirm_delete_hash: str | None
    confirm_restore_hash: str | None
    has_items: bool
    sort_col: int
    sort_dir: str


class LogItem(TypedDict):
    copy_text: str
    level: str
    level_class: str
    level_label: str
    message: str
    source: str
    timestamp: str


class LogsContext(PageContext):
    auto_refresh: bool
    log_items: list[LogItem]
    logs_loaded: bool


class ConfigLanguage(TypedDict):
    checked: bool
    code: str
    name: str


class ConfigValues(TypedDict):
    anime_patterns: str
    bind: str
    connection_concurrency: int
    curator_url: str
    download_delay_secs: int
    enable_all_dir: bool
    enable_unplayable_dir: bool
    exclude_ai: bool
    exclude_machine: bool
    fetch_on_resync: bool
    hearing_impaired: str
    jellyfin_api_key: str
    jellyfin_scan_task_id: str
    jellyfin_url: str
    library_map: dict[str, str]
    media_server_kind: str
    provider_poll_interval_secs: int
    ui_poll_interval_secs: int
    port: int
    plex_token: str
    plex_url: str
    on_library_change: str
    request_timeout_secs: int
    rd_update_delay_secs: int
    search_delay_secs: int
    selected_languages: list[str]
    stream_buffer_size: int
    strategy: str
    subtitles_enabled: bool
    trigger_lib_scan: bool
    verbose: bool
    version_label: str


class ConfigFieldState(TypedDict):
    css_class: str
    is_dirty: bool
    is_overridden: bool
    reload_mode: str
    baseline_value: str


class ConfigContext(PageContext):
    draft_payload: dict[str, Any]
    effective_yaml: str
    field_states: dict[str, ConfigFieldState]
    has_overrides: bool
    is_editing: bool
    language_query: str
    languages: list[ConfigLanguage]
    languages_refreshing: bool
    restart_required: bool
    restart_required_fields: list[str]
    subtitles_credentials_ready: bool
    values: ConfigValues


class ShellContext(PageContext):
    active_page: PageName
    archive: ArchiveContext
    cache: CacheContext
    config: ConfigContext
    logs: LogsContext
    page_content: Markup


def _load_template(name: str) -> Any:
    template = template_file(str(_TEMPLATE_DIR / name))
    if template is None:
        raise FileNotFoundError(name)
    return template


def _build_root_template() -> Any:
    favicon = (
        "data:image/svg+xml,"
        "%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 "
        "viewBox=%220 0 100 100%22%3E%3Ctext y=%22.9em%22 "
        "font-size=%2290%22%3E🐝%3C/text%3E%3C/svg%3E"
    )

    def render(context: dict[str, Any]) -> str:
        title = context.get("title") or "buzz"
        additional_head = "\n".join(context["additional_head_elements"])
        session = context["session"]
        return str(
            Markup(
                f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <meta name="csrf-token" content="{context["csrf_token"]}">
  <link rel="icon" href="{favicon}">
  <link rel="stylesheet" href="/static/buzz.css">
  <link rel="stylesheet" href="/static/prism-tomorrow.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <script defer src="/static/pyview_helpers.js"></script>
  <script defer src="/static/buzz.js"></script>
  <script defer src="/static/prism.js"></script>
  <script defer type="text/javascript" src="/pyview/assets/app.js"></script>
  {additional_head}
</head>
<body>
  <div
    data-phx-main="true"
    data-phx-session="{session}"
    data-phx-static=""
    id="phx-{context["id"]}"
  >
    {context["content"]}
  </div>
</body>
</html>"""
            )
        )

    return render


def build_ui(owner: Any) -> PyView:
    """Build the PyView application mounted into the DAV app."""
    app = PyView()
    app.rootTemplate = _build_root_template()
    app.add_live_view("/", lambda: BuzzLiveView(owner))  # pyright: ignore[reportArgumentType]
    app.add_live_view("/cache", lambda: BuzzLiveView(owner))  # pyright: ignore[reportArgumentType]
    app.add_live_view("/archive", lambda: BuzzLiveView(owner))  # pyright: ignore[reportArgumentType]
    app.add_live_view("/logs", lambda: BuzzLiveView(owner))  # pyright: ignore[reportArgumentType]
    app.add_live_view("/config", lambda: BuzzLiveView(owner))  # pyright: ignore[reportArgumentType]
    return app


_TContext = TypeVar("_TContext", bound=PageContext)


class _BaseBuzzLiveView(LiveView[_TContext]):
    page_title = "buzz"
    page_name = "cache"

    def _sort_torrents(
        self,
        torrents: list[CacheTorrentItem],
        col: int,
        dir: str,
    ) -> list[CacheTorrentItem]:
        key_funcs = [
            lambda t: t["name"].lower(),
            lambda t: t["status"].lower(),
            lambda t: t["progress"],
            lambda t: t["bytes"],
            lambda t: t["selected_files"],
            lambda t: t["ended"] or "",
            lambda t: t["short_id"].lower(),
        ]
        if col < 0 or col >= len(key_funcs):
            return torrents
        reverse = dir == "desc"
        return sorted(torrents, key=key_funcs[col], reverse=reverse)

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def _nav(self) -> PageNav:
        return {
            "archive_count": len(self.owner.state.trashcan),
            "cache_active": self.page_name == "cache",
            "archive_active": self.page_name == "archive",
            "logs_active": self.page_name == "logs",
            "config_active": self.page_name == "config",
            "log_count": self.owner.log_count(),
            "log_level": self._highest_log_level(),
        }

    def _highest_log_level(self) -> str:
        from .core.events import registry

        logs = registry.get_recent(limit=50)
        priority = {"error": 3, "warning": 2, "info": 1, "debug": 0}
        highest = priority.get(self.owner._curator_log_level, 0)
        for log in logs:
            level = str(log.get("level", "info")).lower()
            highest = max(highest, priority.get(level, 0))
        for level, p in priority.items():
            if p == highest:
                return level
        return "info"

    def _meta_items(self) -> list[PageItem]:
        status = self.owner.state.status()
        sync_state = "syncing" if status.get("sync_in_progress") else "idle"
        return [
            {"label": "cache", "value": str(len(self.owner.state.torrents()))},
            {
                "label": "last_sync",
                "value": status.get("last_sync_at") or "never",
            },
            {"label": "state", "value": sync_state},
            {"label": "hook", "value": self._hook_status_label(status)},
        ]

    def _hook_status_label(self, status: dict[str, Any]) -> str:
        phase = str(status.get("hook_phase") or "idle")
        active_count = len(status.get("hook_active_paths") or [])
        pending_count = int(status.get("hook_pending_count") or 0)
        if phase in {"idle", "complete"} and pending_count == 0:
            return "idle"
        if phase == "queued":
            return f"queued ({pending_count} roots)"
        if phase == "waiting_vfs":
            return f"waiting_vfs ({active_count} roots)"
        if phase == "waiting_delay":
            return f"waiting_delay ({active_count} roots)"
        if phase == "triggering_curator":
            return f"curator ({active_count} roots)"
        if phase == "running_hook":
            return f"command ({active_count} roots)"
        if phase == "failed":
            return "failed"
        return phase

    def _base_context(
        self,
        console_msg: str = "",
        console_class: str = "",
    ) -> PageContext:
        status = self.owner.state.status()
        restart_required = bool(getattr(self.owner, "restart_required", False))
        status_label = "[ready]"
        status_class = CSS_STATUS_GREEN
        if restart_required:
            status_label = "[restart required]"
            status_class = CSS_STATUS_YELLOW
        elif not self.owner.is_ready():
            status_label = "[starting]"
            status_class = "service-status-orange"
        context: PageContext = {
            "console_class": console_class,
            "console_msg": console_msg,
            "has_error": bool(status.get("last_error")),
            "is_ready": self.owner.is_ready(),
            "last_error": status.get("last_error") or "",
            "token_configured": bool(self.owner.config.token),
            "meta_items": self._meta_items(),
            "nav": self._nav(),
            "status_class": status_class,
            "status_label": status_label,
        }
        return context

    async def mount(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        socket: LiveViewSocket[_TContext],
        session: dict[str, Any],
    ) -> None:
        socket.live_title = self.page_title
        if is_connected(socket):
            await socket.subscribe(TOPIC_STATUS)

    @info(TOPIC_STATUS)
    async def handle_status(self, _event: InfoEvent, _socket: LiveViewSocket[PageContext]) -> None:
        """Re-render nav when curator sends a status update."""
        pass


class CacheLiveView(_BaseBuzzLiveView):
    page_name = "cache"
    page_title = "buzz: cache"

    async def mount(  # pyright: ignore[reportIncompatibleMethodOverride, reportArgumentType]
        self,
        socket: LiveViewSocket[CacheContext],
        session: dict[str, Any],
    ) -> None:
        await super().mount(socket, session)  # pyright: ignore[reportArgumentType]
        socket.context = self._context()
        if is_connected(socket):
            await socket.subscribe(TOPIC_STATUS)
            await socket.subscribe(TOPIC_ARCHIVE)

    async def handle_event(
        self,
        event: str,
        socket: ConnectedLiveViewSocket[CacheContext],
        payload: dict[str, Any] | None = None,
        to: str = "",
        hash: str = "",
        index: str = "",
        torrent_name: str = "",
        torrent_id: str = "",
        file_id: str = "",
        mode: str = "",
        col: str = "",
    ) -> None:
        if event == EVENT_NAVIGATE:
            await socket.push_navigate(to)
            return
        if event == "prompt_delete":
            socket.context["confirm_delete_id"] = hash
            return
        if event == "cancel_delete":
            socket.context["confirm_delete_id"] = None
            return
        if event == "delete":
            self._handle_delete(socket, hash)
            return
        if event == "fetch_subs":
            self._handle_fetch_subs(socket, torrent_name)
            return
        if event == "resync":
            self._handle_resync(socket)
            return
        if event == "add_magnet_input":
            socket.context["magnet_inputs"].append("")
            return
        if event == "remove_magnet_input":
            try:
                idx = int(index) - 1
                if 0 <= idx < len(socket.context["magnet_inputs"]):
                    socket.context["magnet_inputs"].pop(idx)
            except ValueError:
                pass
            return
        if event == "update_magnets":
            raw = (payload or {}).get("magnet", [])
            if isinstance(raw, str):
                raw = [raw]
            socket.context["magnet_inputs"] = [str(v) for v in raw]
            return
        if event == "analyze":
            self._handle_analyze(socket, payload)
            return
        if event == "select_files":
            for result in socket.context["analysis_results"]:
                for file in result["files"]:
                    if mode == "all":
                        file["selected"] = True
                    elif mode == "none":
                        file["selected"] = False
                    elif mode == "video":
                        file["selected"] = file["is_video"]
            return
        if event == "toggle_file":
            for result in socket.context["analysis_results"]:
                if result["torrent_id"] == torrent_id:
                    for file in result["files"]:
                        if file["id"] == file_id:
                            file["selected"] = not file["selected"]
                            break
            return
        if event == "confirm_cache":
            self._handle_confirm_cache(socket)
            return
        if event == "cancel_cache":
            socket.context = self._context(
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
                confirm_delete_id=socket.context["confirm_delete_id"],
                magnet_inputs=socket.context["magnet_inputs"],
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
            )
            return
        if event == "sort":
            self._handle_sort(socket, col)

    def _handle_delete(
        self, socket: ConnectedLiveViewSocket[CacheContext], hash: str
    ) -> None:
        try:
            result = self.owner.state.delete_torrent(hash)
            warning = result.get("warning") if isinstance(result, dict) else None
            socket.context = self._context(
                console_msg=str(warning or "item moved to archive"),
                console_class=CSS_STATUS_YELLOW if warning else CSS_STATUS_GREEN,
                confirm_delete_id=None,
                magnet_inputs=socket.context["magnet_inputs"],
                analysis_results=socket.context["analysis_results"],
                analysis_error=socket.context["analysis_error"],
                analyzing=socket.context["analyzing"],
                caching=socket.context["caching"],
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
            )
        except Exception as exc:
            socket.context["console_msg"] = f"delete failed: {exc}"
            socket.context["console_class"] = CSS_STATUS_RED

    def _handle_fetch_subs(
        self, socket: ConnectedLiveViewSocket[CacheContext], torrent_name: str
    ) -> None:
        result = self.owner.fetch_subtitles(torrent_name)
        if result.get("error"):
            socket.context["console_msg"] = f"subs fetch failed: {result['error']}"
            socket.context["console_class"] = CSS_STATUS_RED
        else:
            socket.context["console_msg"] = f"subs fetch triggered for: {torrent_name}"
            socket.context["console_class"] = CSS_STATUS_GREEN

    def _handle_resync(
        self, socket: ConnectedLiveViewSocket[CacheContext]
    ) -> None:
        socket.context["console_msg"] = "resyncing library..."
        socket.context["console_class"] = "service-status-orange"
        try:
            self.owner.state.manual_rebuild()
            socket.context["console_msg"] = "library resynced!"
            socket.context["console_class"] = CSS_STATUS_GREEN
        except Exception as exc:
            socket.context["console_msg"] = f"resync failed: {exc}"
            socket.context["console_class"] = CSS_STATUS_RED

    def _handle_analyze(
        self,
        socket: ConnectedLiveViewSocket[CacheContext],
        payload: dict[str, Any] | None,
    ) -> None:
        raw = (payload or {}).get("magnet", [])
        if isinstance(raw, str):
            raw = [raw]
        magnets = [str(m).strip() for m in raw if str(m).strip()]
        if not magnets:
            return
        socket.context["analyzing"] = True
        socket.context["analysis_error"] = ""
        results: list[CacheAnalysisResult] = []
        errors: list[str] = []
        import re

        for magnet in magnets:
            try:
                info = self.owner.state.add_magnet(magnet)
                files: list[CacheFileItem] = []
                for f in info.get("files", []):
                    path = str(f.get("path", ""))
                    is_video = bool(
                        re.search(r"\.(mkv|mp4|avi|m4v|mov)$", path, re.I)
                    )
                    b = int(f.get("bytes", 0))
                    files.append(
                        {
                            "id": str(f.get("id", "")),
                            "path": path,
                            "bytes": b,
                            "size": format_bytes(b),
                            "is_video": is_video,
                            "selected": is_video,
                        }
                    )
                results.append(
                    {
                        "torrent_id": str(info["id"]),
                        "filename": str(info.get("filename") or "Torrent Files"),
                        "files": files,
                    }
                )
            except Exception as exc:
                errors.append(str(exc))
        socket.context["analyzing"] = False
        socket.context["analysis_results"] = results
        if errors:
            socket.context["analysis_error"] = f"Failed: {', '.join(errors)}"
            socket.context["console_msg"] = (
                f"Resolved {len(results)} magnets, {len(errors)} failed."
            )
            socket.context["console_class"] = "service-status-orange"
        else:
            socket.context["console_msg"] = (
                f"Resolved {len(results)} magnet(s)."
                if len(results) != 1
                else "Ready to cache."
            )
            socket.context["console_class"] = "service-status-green"

    def _handle_confirm_cache(
        self, socket: ConnectedLiveViewSocket[CacheContext]
    ) -> None:
        socket.context["caching"] = True
        try:
            for result in socket.context["analysis_results"]:
                selected = [f["id"] for f in result["files"] if f["selected"]]
                if selected:
                    self.owner.state.select_files(result["torrent_id"], selected)
            self.owner.state.sync()
            socket.context = self._context(
                console_msg="Items added and synced.",
                console_class=CSS_STATUS_GREEN,
                confirm_delete_id=socket.context["confirm_delete_id"],
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
            )
        except Exception as exc:
            socket.context["caching"] = False
            socket.context["console_msg"] = f"Error: {exc}"
            socket.context["console_class"] = CSS_STATUS_RED

    def _handle_sort(
        self, socket: ConnectedLiveViewSocket[CacheContext], col: str
    ) -> None:
        try:
            new_col = int(col)
        except ValueError:
            return
        if socket.context["sort_col"] == new_col:
            sort_dir = "desc" if socket.context["sort_dir"] == "asc" else "asc"
            sort_col = new_col
        else:
            sort_col = new_col
            sort_dir = "asc"
        socket.context = self._context(
            console_msg=socket.context["console_msg"],
            console_class=socket.context["console_class"],
            confirm_delete_id=socket.context["confirm_delete_id"],
            magnet_inputs=socket.context["magnet_inputs"],
            analysis_results=socket.context["analysis_results"],
            analysis_error=socket.context["analysis_error"],
            analyzing=socket.context["analyzing"],
            caching=socket.context["caching"],
            sort_col=sort_col,
            sort_dir=sort_dir,
        )

    async def handle_info(
        self,
        event: InfoEvent,
        socket: ConnectedLiveViewSocket[CacheContext],
    ) -> None:
        if event.name not in {TOPIC_STATUS, TOPIC_ARCHIVE}:
            return
        socket.context = self._context(
            console_msg=socket.context["console_msg"],
            console_class=socket.context["console_class"],
            confirm_delete_id=socket.context["confirm_delete_id"],
            magnet_inputs=socket.context["magnet_inputs"],
            analysis_results=socket.context["analysis_results"],
            analysis_error=socket.context["analysis_error"],
            analyzing=socket.context["analyzing"],
            caching=socket.context["caching"],
            sort_col=socket.context["sort_col"],
            sort_dir=socket.context["sort_dir"],
        )

    async def render(
        self,
        assigns: CacheContext,
        meta: Any,
    ) -> RenderedContent:
        return LiveRender(_load_template("cache_live.html"), assigns, meta)

    def _context(
        self,
        console_msg: str = "",
        console_class: str = "",
        confirm_delete_id: str | None = None,
        magnet_inputs: list[str] | None = None,
        analysis_results: list[CacheAnalysisResult] | None = None,
        analysis_error: str = "",
        analyzing: bool = False,
        caching: bool = False,
        sort_col: int = 0,
        sort_dir: str = "asc",
    ) -> CacheContext:
        torrents = []
        for torrent in self.owner.state.torrents():
            torrents.append(
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
        torrents = self._sort_torrents(torrents, sort_col, sort_dir)
        base = self._base_context(console_msg, console_class)
        analysis_results = analysis_results or []
        return cast(
            CacheContext,
            {
                **base,
                "analysis_error": analysis_error,
                "analysis_results": analysis_results,
                "analyzing": analyzing,
                "caching": caching,
                "confirm_delete_id": confirm_delete_id,
                "has_multiple_analysis_results": len(analysis_results) > 1,
                "has_torrents": bool(torrents),
                "magnet_inputs": magnet_inputs or [""],
                "show_overlay": analyzing or caching,
                "sort_col": sort_col,
                "sort_dir": sort_dir,
                "subtitle_enabled": self.owner.config.subtitles.enabled,
                "torrents": torrents,
            },
        )


class ArchiveLiveView(_BaseBuzzLiveView):
    page_name = "archive"
    page_title = "buzz: archive"

    async def mount(  # pyright: ignore[reportIncompatibleMethodOverride, reportArgumentType]
        self,
        socket: LiveViewSocket[ArchiveContext],
        session: dict[str, Any],
    ) -> None:
        await super().mount(socket, session)  # pyright: ignore[reportArgumentType]
        socket.context = self._context()
        if is_connected(socket):
            await socket.subscribe(TOPIC_ARCHIVE)
            await socket.subscribe(TOPIC_STATUS)

    async def handle_event(
        self,
        event: str,
        socket: ConnectedLiveViewSocket[ArchiveContext],
        to: str = "",
        hash: str = "",
        col: str = "",
    ) -> None:
        if event == EVENT_NAVIGATE:
            await socket.push_navigate(to)
            return
        if event == "prompt_restore":
            socket.context["confirm_restore_hash"] = hash
            socket.context["confirm_delete_hash"] = None
            return
        if event == "cancel_restore":
            socket.context["confirm_restore_hash"] = None
            return
        if event == "prompt_delete":
            socket.context["confirm_delete_hash"] = hash
            socket.context["confirm_restore_hash"] = None
            return
        if event == "cancel_delete":
            socket.context["confirm_delete_hash"] = None
            return
        if event == "restore":
            self.owner.state.restore_trash(hash)
            self.owner.state.sync()
            socket.context = self._context(
                console_msg="item restored to cache",
                console_class="service-status-green",
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
            )
            return
        if event == "delete":
            self.owner.state.delete_trash_permanently(hash)
            socket.context = self._context(
                console_msg="archive item deleted",
                console_class="service-status-green",
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
            )
            return
        if event == "sort":
            try:
                new_col = int(col)
            except ValueError:
                return
            if socket.context["sort_col"] == new_col:
                sort_dir = "desc" if socket.context["sort_dir"] == "asc" else "asc"
                sort_col = new_col
            else:
                sort_col = new_col
                sort_dir = "asc"
            socket.context = self._context(
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
                confirm_delete_hash=socket.context["confirm_delete_hash"],
                confirm_restore_hash=socket.context["confirm_restore_hash"],
                sort_col=sort_col,
                sort_dir=sort_dir,
            )

    async def handle_info(
        self,
        event: InfoEvent,
        socket: ConnectedLiveViewSocket[ArchiveContext],
    ) -> None:
        if event.name not in {TOPIC_ARCHIVE, TOPIC_STATUS}:
            return
        socket.context = self._context(
            console_msg=socket.context["console_msg"],
            console_class=socket.context["console_class"],
            confirm_delete_hash=socket.context["confirm_delete_hash"],
            confirm_restore_hash=socket.context["confirm_restore_hash"],
            sort_col=socket.context["sort_col"],
            sort_dir=socket.context["sort_dir"],
        )

    async def render(
        self,
        assigns: ArchiveContext,
        meta: Any,
    ) -> RenderedContent:
        return LiveRender(_load_template("archive_live.html"), assigns, meta)

    def _sort_items(
        self,
        items: list[ArchiveItem],
        col: int,
        dir: str,
    ) -> list[ArchiveItem]:
        key_funcs = [
            lambda i: i["name"].lower(),
            lambda i: i["bytes"],
            lambda i: i["file_count"],
            lambda i: i["deleted_at"] or "",
        ]
        if col < 0 or col >= len(key_funcs):
            return items
        return sorted(items, key=key_funcs[col], reverse=dir == "desc")

    def _context(
        self,
        console_msg: str = "",
        console_class: str = "",
        confirm_delete_hash: str | None = None,
        confirm_restore_hash: str | None = None,
        sort_col: int = 0,
        sort_dir: str = "asc",
    ) -> ArchiveContext:
        items: list[ArchiveItem] = []
        for torrent in self.owner.state.archive_torrents():
            items.append(
                {
                    "bytes": torrent["bytes"],
                    "deleted_at": torrent["deleted_at"] or "-",
                    "file_count": torrent["file_count"],
                    "hash": torrent["hash"],
                    "name": torrent["name"],
                    "size": format_bytes(torrent["bytes"]),
                }
            )
        items = self._sort_items(items, sort_col, sort_dir)

        base = self._base_context(console_msg, console_class)
        return cast(
            ArchiveContext,
            {
                **base,
                "archive_items": items,
                "confirm_delete_hash": confirm_delete_hash,
                "confirm_restore_hash": confirm_restore_hash,
                "has_items": bool(items),
                "sort_col": sort_col,
                "sort_dir": sort_dir,
            },
        )


class LogsLiveView(_BaseBuzzLiveView):
    page_name = "logs"
    page_title = "buzz: system logs"

    async def mount(  # pyright: ignore[reportIncompatibleMethodOverride, reportArgumentType]
        self,
        socket: LiveViewSocket[LogsContext],
        session: dict[str, Any],
    ) -> None:
        await super().mount(socket, session)  # pyright: ignore[reportArgumentType]
        self.owner._curator_log_level = "info"
        socket.context = self._context()
        if is_connected(socket):
            await socket.subscribe(TOPIC_STATUS)
            if socket.context["auto_refresh"]:
                await socket.subscribe(TOPIC_LOGS)

    async def handle_event(
        self,
        event: str,
        socket: ConnectedLiveViewSocket[LogsContext],
        to: str = "",
    ) -> None:
        if event == EVENT_NAVIGATE:
            await socket.push_navigate(to)
            return
        if event == "toggle_auto_refresh":
            socket.context["auto_refresh"] = not socket.context["auto_refresh"]
            if socket.context["auto_refresh"]:
                await socket.subscribe(TOPIC_LOGS)
            else:
                await socket.pub_sub.unsubscribe_topic_async(TOPIC_LOGS)

    async def handle_info(
        self,
        event: InfoEvent,
        socket: ConnectedLiveViewSocket[LogsContext],
    ) -> None:
        if event.name not in {TOPIC_LOGS, TOPIC_STATUS}:
            return
        if event.name == TOPIC_STATUS and not socket.context["auto_refresh"]:
            base = self._base_context(
                socket.context["console_msg"],
                socket.context["console_class"],
            )
            socket.context = cast(
                LogsContext,
                {
                    **base,
                    "auto_refresh": socket.context["auto_refresh"],
                    "log_items": socket.context["log_items"],
                    "logs_loaded": socket.context["logs_loaded"],
                },
            )
            return
        socket.context = self._context(
            auto_refresh=socket.context["auto_refresh"],
        )

    async def render(
        self,
        assigns: LogsContext,
        meta: Any,
    ) -> RenderedContent:
        return LiveRender(_load_template("logs_live.html"), assigns, meta)

    def _context(
        self,
        auto_refresh: bool = True,
    ) -> LogsContext:
        base = self._base_context()
        return cast(
            LogsContext,
            {
                **base,
                "auto_refresh": auto_refresh,
                "log_items": self.owner.formatted_logs(limit=100),
                "logs_loaded": True,
            },
        )


class ConfigLiveView(_BaseBuzzLiveView):
    page_name = "config"
    page_title = "buzz: config"

    async def mount(  # pyright: ignore[reportIncompatibleMethodOverride, reportArgumentType]
        self,
        socket: LiveViewSocket[ConfigContext],
        session: dict[str, Any],
    ) -> None:
        await super().mount(socket, session)  # pyright: ignore[reportArgumentType]
        socket.context = self._context()
        if is_connected(socket):
            await socket.subscribe(TOPIC_STATUS)
            await socket.subscribe(TOPIC_CONFIG)

    async def handle_event(
        self,
        event: str,
        socket: ConnectedLiveViewSocket[ConfigContext],
        payload: dict[str, Any] | None = None,
        to: str = "",
        language_query: str = "",
    ) -> None:
        if event == EVENT_NAVIGATE:
            await socket.push_navigate(to)
            return
        if event == "edit":
            socket.context = self._context(
                is_editing=True,
                draft_payload=socket.context["draft_payload"],
                language_query=socket.context["language_query"],
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
            )
            return
        if event == "cancel":
            socket.context = self._context()
            return
        if event in {"filter_languages", "preview"}:
            form_payload = payload or {}
            socket.context = self._context(
                is_editing=True,
                draft_payload=form_payload,
                language_query=form_payload.get("language_query", language_query),
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
            )
            return
        if event == "reload_languages":
            if not self.owner._subtitles_credentials_ready():
                console_msg = (
                    "cannot refresh languages: set subtitles.opensubtitles"
                    " api_key, username, and password in buzz.yml"
                )
                console_class = CSS_STATUS_RED
            elif not self.owner.trigger_language_refresh(force=True):
                console_msg = "language refresh already in progress"
                console_class = CSS_STATUS_YELLOW
            else:
                console_msg = "refreshing languages from opensubtitles..."
                console_class = CSS_STATUS_GREEN
            socket.context = self._context(
                is_editing=socket.context["is_editing"],
                draft_payload=socket.context["draft_payload"],
                language_query=socket.context["language_query"],
                console_msg=console_msg,
                console_class=console_class,
            )
            return
        if event == "restore_defaults":
            result = self.owner.persist_overrides({})
            socket.context = self._context(
                is_editing=False,
                console_msg="defaults restored.",
                console_class="service-status-green",
            )
            if result["restart_required"]:
                socket.context["console_msg"] = (
                    "defaults restored. restart still required for "
                    + ", ".join(result["restart_required_fields"])
                )
                socket.context["console_class"] = "service-status-yellow"
            return
        if event != "save":
            return

        overrides = _config_overrides_from_payload(payload or {})
        result = self.owner.persist_overrides(overrides)
        console_msg = "saved."
        console_class = CSS_STATUS_GREEN
        if result["restart_required"]:
            console_msg = (
                "saved. restart required for "
                + ", ".join(result["restart_required_fields"])
            )
            console_class = CSS_STATUS_YELLOW
        socket.context = self._context(
            is_editing=False,
            console_msg=console_msg,
            console_class=console_class,
        )

    async def handle_info(
        self,
        event: InfoEvent,
        socket: ConnectedLiveViewSocket[ConfigContext],
    ) -> None:
        if event.name not in {TOPIC_STATUS, TOPIC_CONFIG}:
            return
        console_msg = socket.context["console_msg"]
        console_class = socket.context["console_class"]
        if (
            event.name == TOPIC_CONFIG
            and isinstance(event.payload, dict)
            and event.payload.get("languages_refresh_complete")
        ):
            console_msg = "languages updated"
            console_class = CSS_STATUS_GREEN
        socket.context = self._context(
            is_editing=socket.context["is_editing"],
            draft_payload=socket.context["draft_payload"],
            language_query=socket.context["language_query"],
            console_msg=console_msg,
            console_class=console_class,
        )

    async def render(
        self,
        assigns: ConfigContext,
        meta: Any,
    ) -> RenderedContent:
        return LiveRender(_load_template("config_live.html"), assigns, meta)

    def _context(
        self,
        is_editing: bool = False,
        language_query: str = "",
        draft_payload: dict[str, Any] | None = None,
        console_msg: str = "",
        console_class: str = "",
    ) -> ConfigContext:
        base = self._base_context(console_msg, console_class)
        effective_config = getattr(self.owner, "saved_config", self.owner.config)
        effective = to_nested_dict(effective_config)
        masked = mask_secrets(effective)
        config_path = Path(effective_config._config_path)
        baseline_config = effective_config._base_raw
        if config_path.exists():
            _, _, baseline_config, _, _, _ = load_base_and_overrides(
                str(config_path)
            )
        baseline_values = _config_baseline_values(
            effective_config,
            baseline_config,
        )
        effective_yaml = _render_effective_yaml(
            masked,
            baseline_values,
            set(
                effective_override_field_paths(
                    baseline_values,
                    effective_config._raw_overrides,
                )
            ),
        )
        values = _config_values(effective_config, draft_payload)
        current_overrides = effective_config._raw_overrides
        field_states = _field_states(
            effective,
            baseline_values,
            current_overrides,
            _config_overrides_from_payload(draft_payload) if is_editing and draft_payload else {},
        )
        languages = _language_rows(
            self.owner.opensubtitles_languages,
            tuple(values["selected_languages"]),
            language_query,
        )
        subs = effective_config.subtitles
        subtitles_credentials_ready = bool(
            subs.api_key and subs.username and subs.password
        )
        return cast(
            ConfigContext,
            {
                **base,
                "draft_payload": draft_payload or {},
                "effective_yaml": effective_yaml,
                "field_states": field_states,
                "has_overrides": bool(current_overrides),
                "is_editing": is_editing,
                "language_query": language_query,
                "languages": languages,
                "languages_refreshing": self.owner.languages_refreshing,
                "restart_required": self.owner.restart_required,
                "restart_required_fields": self.owner.restart_required_fields(),
                "subtitles_credentials_ready": subtitles_credentials_ready,
                "values": values,
            },
        )


def _page_from_path(path: str) -> PageName:
    page = path.strip("/") or "cache"
    if page in PAGE_NAMES:
        return cast(PageName, page)
    return "cache"


def _page_path(page: PageName) -> str:
    return f"/{page}"


def _extract_page_body(html: str) -> Markup:
    start_marker = "<!-- page-body -->"
    end_marker = "<!-- /page-body -->"
    start = html.find(start_marker)
    end = html.find(end_marker)
    if start == -1 or end == -1 or end < start:
        return Markup(html)
    start += len(start_marker)
    return Markup(html[start:end].strip())


class BuzzLiveView(_BaseBuzzLiveView):
    page_title = "buzz"

    def __init__(self, owner: Any) -> None:
        super().__init__(owner)
        self.cache_view = CacheLiveView(owner)
        self.archive_view = ArchiveLiveView(owner)
        self.logs_view = LogsLiveView(owner)
        self.config_view = ConfigLiveView(owner)

    def _nav_for_page(self, page: PageName) -> PageNav:
        return {
            "archive_count": len(self.owner.state.trashcan),
            "cache_active": page == "cache",
            "archive_active": page == "archive",
            "logs_active": page == "logs",
            "config_active": page == "config",
            "log_count": self.owner.log_count(),
            "log_level": self._highest_log_level(),
        }

    def _base_context_for_page(
        self,
        page: PageName,
        console_msg: str = "",
        console_class: str = "",
    ) -> PageContext:
        base = self._base_context(console_msg, console_class)
        base["nav"] = self._nav_for_page(page)
        return base

    def _page_context(self, page: PageName, context: ShellContext) -> PageContext:
        if page == "archive":
            return context["archive"]
        if page == "logs":
            return context["logs"]
        if page == "config":
            return context["config"]
        return context["cache"]

    def _replace_page_context(
        self,
        context: ShellContext,
        page: PageName,
        page_context: PageContext,
    ) -> None:
        if page == "archive":
            context["archive"] = cast(ArchiveContext, page_context)
        elif page == "logs":
            context["logs"] = cast(LogsContext, page_context)
        elif page == "config":
            context["config"] = cast(ConfigContext, page_context)
        else:
            context["cache"] = cast(CacheContext, page_context)

    def _refresh_shared_context(
        self,
        context: ShellContext,
        *,
        page: PageName | None = None,
        console_msg: str | None = None,
        console_class: str | None = None,
    ) -> None:
        active_page = page or context["active_page"]
        active_context = self._page_context(active_page, context)
        base = self._base_context_for_page(
            active_page,
            active_context["console_msg"] if console_msg is None else console_msg,
            active_context["console_class"] if console_class is None else console_class,
        )
        context["console_class"] = base["console_class"]
        context["console_msg"] = base["console_msg"]
        context["has_error"] = base["has_error"]
        context["is_ready"] = base["is_ready"]
        context["last_error"] = base["last_error"]
        context["token_configured"] = base["token_configured"]
        context["meta_items"] = base["meta_items"]
        context["nav"] = base["nav"]
        context["status_class"] = base["status_class"]
        context["status_label"] = base["status_label"]
        context["active_page"] = active_page
        context["page_content"] = self._render_page_body(active_page, context)

    def _render_page_body(self, page: PageName, context: ShellContext) -> Markup:
        if page == "archive":
            rendered = _load_template("archive_live.html").render(
                context["archive"], None
            )
        elif page == "logs":
            rendered = _load_template("logs_live.html").render(context["logs"], None)
        elif page == "config":
            rendered = _load_template("config_live.html").render(
                context["config"], None
            )
        else:
            rendered = _load_template("cache_live.html").render(context["cache"], None)
        return _extract_page_body(rendered)

    def _context(self, active_page: PageName = "cache") -> ShellContext:
        cache = self.cache_view._context()
        archive = self.archive_view._context()
        logs = self.logs_view._context()
        config = self.config_view._context()
        active_context = {
            "archive": archive,
            "cache": cache,
            "config": config,
            "logs": logs,
        }[active_page]
        base = self._base_context_for_page(
            active_page,
            active_context["console_msg"],
            active_context["console_class"],
        )
        context = cast(
            ShellContext,
            {
                **base,
                "active_page": active_page,
                "archive": archive,
                "cache": cache,
                "config": config,
                "logs": logs,
                "page_content": Markup(""),
            },
        )
        context["page_content"] = self._render_page_body(active_page, context)
        return context

    async def mount(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        socket: LiveViewSocket[ShellContext],
        session: dict[str, Any],
    ) -> None:
        socket.live_title = self.page_title
        socket.context = self._context()
        if is_connected(socket):
            await socket.subscribe(TOPIC_STATUS)
            await socket.subscribe(TOPIC_ARCHIVE)
            await socket.subscribe(TOPIC_CONFIG)
            if socket.context["logs"]["auto_refresh"]:
                await socket.subscribe(TOPIC_LOGS)

    async def handle_params(
        self,
        url: ParseResult,
        socket: LiveViewSocket[ShellContext],
    ) -> None:
        page = _page_from_path(url.path)
        if not getattr(socket, "context", None):
            socket.context = self._context(page)
            return
        self._refresh_shared_context(socket.context, page=page)
        socket.live_title = {
            "archive": "buzz: archive",
            "cache": "buzz: cache",
            "config": "buzz: config",
            "logs": "buzz: system logs",
        }[page]

    async def handle_event(
        self,
        event: str,
        socket: ConnectedLiveViewSocket[ShellContext],
        payload: dict[str, Any] | None = None,
        to: str = "",
        hash: str = "",
        index: str = "",
        torrent_name: str = "",
        torrent_id: str = "",
        file_id: str = "",
        mode: str = "",
        col: str = "",
        language_query: str = "",
    ) -> None:
        if event == EVENT_NAVIGATE:
            page = _page_from_path(to)
            self._refresh_shared_context(socket.context, page=page)
            await socket.push_patch(_page_path(page))
            return

        page = socket.context["active_page"]
        page_context = self._page_context(page, socket.context)
        original_context = socket.context
        socket.context = page_context  # type: ignore[assignment]
        try:
            if page == "archive":
                await self.archive_view.handle_event(
                    event,
                    cast(ConnectedLiveViewSocket[ArchiveContext], socket),
                    to=to,
                    hash=hash,
                    col=col,
                )
            elif page == "logs":
                await self.logs_view.handle_event(
                    event,
                    cast(ConnectedLiveViewSocket[LogsContext], socket),
                    to=to,
                )
            elif page == "config":
                await self.config_view.handle_event(
                    event,
                    cast(ConnectedLiveViewSocket[ConfigContext], socket),
                    payload=payload,
                    to=to,
                    language_query=language_query,
                )
            else:
                await self.cache_view.handle_event(
                    event,
                    cast(ConnectedLiveViewSocket[CacheContext], socket),
                    payload=payload,
                    to=to,
                    hash=hash,
                    index=index,
                    torrent_name=torrent_name,
                    torrent_id=torrent_id,
                    file_id=file_id,
                    mode=mode,
                    col=col,
                )
            new_page_context = cast(PageContext, socket.context)
        finally:
            socket.context = original_context  # type: ignore[assignment]
        self._replace_page_context(socket.context, page, new_page_context)
        self._refresh_shared_context(socket.context, page=page)

    async def handle_info(
        self,
        event: InfoEvent,
        socket: ConnectedLiveViewSocket[ShellContext],
    ) -> None:
        if event.name not in {TOPIC_ARCHIVE, TOPIC_CONFIG, TOPIC_LOGS, TOPIC_STATUS}:
            return
        context = socket.context
        context["cache"] = self.cache_view._context(
            console_msg=context["cache"]["console_msg"],
            console_class=context["cache"]["console_class"],
            confirm_delete_id=context["cache"]["confirm_delete_id"],
            magnet_inputs=context["cache"]["magnet_inputs"],
            analysis_results=context["cache"]["analysis_results"],
            analysis_error=context["cache"]["analysis_error"],
            analyzing=context["cache"]["analyzing"],
            caching=context["cache"]["caching"],
            sort_col=context["cache"]["sort_col"],
            sort_dir=context["cache"]["sort_dir"],
        )
        context["archive"] = self.archive_view._context(
            console_msg=context["archive"]["console_msg"],
            console_class=context["archive"]["console_class"],
            confirm_delete_hash=context["archive"]["confirm_delete_hash"],
            confirm_restore_hash=context["archive"]["confirm_restore_hash"],
            sort_col=context["archive"]["sort_col"],
            sort_dir=context["archive"]["sort_dir"],
        )
        if event.name == TOPIC_LOGS or context["logs"]["auto_refresh"]:
            context["logs"] = self.logs_view._context(
                auto_refresh=context["logs"]["auto_refresh"]
            )
        else:
            logs_base = self.logs_view._base_context(
                context["logs"]["console_msg"],
                context["logs"]["console_class"],
            )
            context["logs"] = cast(
                LogsContext,
                {
                    **logs_base,
                    "auto_refresh": context["logs"]["auto_refresh"],
                    "log_items": context["logs"]["log_items"],
                    "logs_loaded": context["logs"]["logs_loaded"],
                },
            )
        config_console_msg = context["config"]["console_msg"]
        config_console_class = context["config"]["console_class"]
        if (
            event.name == TOPIC_CONFIG
            and isinstance(event.payload, dict)
            and event.payload.get("languages_refresh_complete")
        ):
            config_console_msg = "languages updated"
            config_console_class = CSS_STATUS_GREEN
        context["config"] = self.config_view._context(
            is_editing=context["config"]["is_editing"],
            draft_payload=context["config"]["draft_payload"],
            language_query=context["config"]["language_query"],
            console_msg=config_console_msg,
            console_class=config_console_class,
        )
        self._refresh_shared_context(context)

    async def render(
        self,
        assigns: ShellContext,
        meta: Any,
    ) -> RenderedContent:
        return LiveRender(_load_template("shell_live.html"), assigns, meta)


def _language_rows(
    languages: list[tuple[str, str]],
    selected_codes: tuple[str, ...],
    query: str,
) -> list[ConfigLanguage]:
    selected = set(selected_codes)
    term = query.strip().lower()
    ordered = sorted(
        languages or [],
        key=lambda item: (item[0] not in selected, item[1].lower()),
    )
    rows = []
    for code, name in ordered:
        normalized_name = name.lower()
        normalized_code = code.lower()
        if term and term not in normalized_name and term not in normalized_code:
            continue
        rows.append(
            {"checked": code in selected, "code": code, "name": name}
        )
    return rows


def _config_values(
    config: Any,
    payload: dict[str, Any] | None = None,
) -> ConfigValues:
    draft = _config_overrides_from_payload(payload or {})
    effective = deep_merge(to_nested_dict(config), draft)
    subtitles = effective["subtitles"]
    subtitle_filters = subtitles["filters"]
    return {
        "anime_patterns": "\n".join(
            effective["directories"]["anime"]["patterns"]
        ),
        "bind": effective["server"]["bind"],
        "connection_concurrency": effective["provider"][
            "connection_concurrency"
        ],
        "curator_url": effective["hooks"]["curator_url"],
        "download_delay_secs": subtitles["download_delay_secs"],
        "enable_all_dir": effective["compat"]["enable_all_dir"],
        "enable_unplayable_dir": effective["compat"]["enable_unplayable_dir"],
        "exclude_ai": subtitle_filters["exclude_ai"],
        "exclude_machine": subtitle_filters["exclude_machine"],
        "fetch_on_resync": subtitles["fetch_on_resync"],
        "hearing_impaired": subtitle_filters["hearing_impaired"],
        "jellyfin_api_key": effective["media_server"]["jellyfin"]["api_key"],
        "jellyfin_scan_task_id": effective["media_server"]["jellyfin"][
            "scan_task_id"
        ],
        "jellyfin_url": effective["media_server"]["jellyfin"]["url"],
        "library_map": {
            key: str(
                effective.get("media_server", {})
                .get("library_map", {})
                .get(key, "")
            )
            for key in _LIBRARY_MAP_KEYS
        },
        "media_server_kind": effective["media_server"]["kind"],
        "on_library_change": effective["hooks"]["on_library_change"],
        "provider_poll_interval_secs": effective["provider"]["poll_interval_secs"],
        "ui_poll_interval_secs": effective["ui"]["poll_interval_secs"],
        "port": effective["server"]["port"],
        "plex_token": effective["media_server"]["plex"]["token"],
        "plex_url": effective["media_server"]["plex"]["url"],
        "request_timeout_secs": effective["request_timeout_secs"],
        "rd_update_delay_secs": effective["hooks"]["rd_update_delay_secs"],
        "search_delay_secs": subtitles["search_delay_secs"],
        "selected_languages": subtitles["languages"],
        "stream_buffer_size": effective["server"]["stream_buffer_size"],
        "strategy": subtitles["strategy"],
        "subtitles_enabled": subtitles["enabled"],
        "trigger_lib_scan": effective["media_server"]["trigger_lib_scan"],
        "verbose": effective["logging"]["verbose"],
        "version_label": effective["version_label"],
    }


def _parse_number_value(raw_value: Any) -> int | float:
    value = str(raw_value).strip()
    if "." in value:
        return float(value)
    return int(value)


def _extract_pattern_lines(patterns: list[Any]) -> list[str]:
    return [
        line.strip()
        for line in str(patterns[0]).splitlines()
        if line.strip()
    ]


def _extract_language_values(values: list[Any]) -> list[str]:
    return [str(v) for v in values if str(v).strip()]


def _config_overrides_from_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not payload:
        return {}
    overrides: dict[str, Any] = {}
    normalized = {
        key: value if isinstance(value, list) else [value]
        for key, value in payload.items()
    }

    for field in _CONFIG_NUMBER_FIELDS:
        if field in normalized and normalized[field]:
            _set_nested_value(
                overrides, field, _parse_number_value(normalized[field][0])
            )

    for field in _CONFIG_BOOL_FIELDS:
        _set_nested_value(overrides, field, field in normalized)

    text_fields = (
        "server.bind",
        "hooks.on_library_change",
        "hooks.curator_url",
        "version_label",
        "media_server.kind",
        "media_server.jellyfin.url",
        "media_server.jellyfin.api_key",
        "media_server.jellyfin.scan_task_id",
        "media_server.plex.url",
        "media_server.plex.token",
        "subtitles.strategy",
        "subtitles.filters.hearing_impaired",
    )
    for field in text_fields:
        if field in normalized and normalized[field]:
            _set_nested_value(overrides, field, str(normalized[field][0]))

    patterns = normalized.get(FIELD_ANIME_PATTERNS, [""])
    _set_nested_value(overrides, FIELD_ANIME_PATTERNS, _extract_pattern_lines(patterns))

    languages = _extract_language_values(
        normalized.get(FIELD_SUBTITLES_LANGUAGES, [])
    )
    _set_nested_value(overrides, FIELD_SUBTITLES_LANGUAGES, languages)

    for field in _LIBRARY_MAP_FIELDS:
        if field in normalized and normalized[field]:
            value = str(normalized[field][0]).strip()
            if value:
                _set_nested_value(overrides, field, value)

    return overrides


def _set_nested_value(target: dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    cursor = target
    for key in keys[:-1]:
        cursor = cast(dict[str, Any], cursor.setdefault(key, {}))
    cursor[keys[-1]] = value


def _field_states(
    effective: dict[str, Any],
    baseline: dict[str, Any],
    current_overrides: dict[str, Any],
    draft_overrides: dict[str, Any],
) -> dict[str, ConfigFieldState]:
    current_override_paths = set(
        effective_override_field_paths(baseline, current_overrides)
    )
    dirty_paths = set(
        diff_fields(effective, deep_merge(effective, draft_overrides), _CONFIG_TRACKED_FIELDS)
    )
    states: dict[str, ConfigFieldState] = {}
    for path in _CONFIG_TRACKED_FIELDS:
        alias = _field_alias(path)
        is_dirty = path in dirty_paths
        is_overridden = path in current_override_paths
        css_class = ""
        if is_dirty:
            css_class = "config-field-dirty"
        elif is_overridden:
            css_class = "config-field-overridden"
        reload_mode = (
            "needs restart"
            if path in RESTART_REQUIRED_FIELDS
            else "hot reload"
        )
        if is_overridden:
            default_value = get_nested_value(baseline, path)
            reload_mode = (
                f"{reload_mode} · default: "
                f"{_render_default_comment_value(default_value)}"
            )
        states[alias] = {
            "css_class": css_class,
            "is_dirty": is_dirty,
            "is_overridden": is_overridden,
            "reload_mode": reload_mode,
            "baseline_value": _render_default_comment_value(get_nested_value(baseline, path)),
        }
    return states


def _field_alias(path: str) -> str:
    return path.replace(".", "_")


def _config_baseline_values(config: Any, baseline: dict[str, Any]) -> dict[str, Any]:
    try:
        baseline_config = config.__class__._from_merged_dict(baseline)
    except Exception:
        return baseline
    return to_nested_dict(baseline_config)


def _render_effective_yaml(
    effective: dict[str, Any],
    baseline: dict[str, Any],
    override_paths: set[str],
) -> str:
    lines = _yaml_lines(
        effective,
        baseline=baseline,
        override_paths=override_paths,
    )
    return "\n".join(lines) + "\n"


def _yaml_dict_lines(
    value: dict,
    *,
    baseline: dict,
    indent: int,
    path: str,
    override_paths: set[str],
) -> list[str]:
    prefix = " " * indent
    lines: list[str] = []
    for key, child in value.items():
        child_path = f"{path}.{key}" if path else key
        if child_path in override_paths:
            default_value = get_nested_value(baseline, child_path)
            default_text = _render_default_comment_value(default_value)
            lines.append(f"{prefix}# Overriden via UI. Default: {default_text}")
        if isinstance(child, dict):
            lines.append(f"{prefix}{key}:")
            lines.extend(
                _yaml_lines(
                    child,
                    baseline=baseline,
                    indent=indent + 2,
                    path=child_path,
                    override_paths=override_paths,
                )
            )
        elif isinstance(child, list):
            lines.append(f"{prefix}{key}:")
            if not child:
                lines.append(f"{prefix}  []")
            else:
                for item in child:
                    if isinstance(item, (dict, list)):
                        lines.append(f"{prefix}  -")
                        lines.extend(
                            _yaml_lines(
                                item,
                                baseline=baseline,
                                indent=indent + 4,
                                path=child_path,
                                override_paths=override_paths,
                            )
                        )
                    else:
                        rendered = _render_yaml_scalar(item)
                        lines.append(f"{prefix}  - {rendered}")
        else:
            rendered = _render_yaml_scalar(child)
            lines.append(f"{prefix}{key}: {rendered}")
    return lines


def _yaml_list_lines(value: list, *, indent: int) -> list[str]:
    prefix = " " * indent
    if not value:
        return [f"{prefix}[]"]
    return [f"{prefix}- {_render_yaml_scalar(item)}" for item in value]


def _yaml_lines(
    value: Any,
    *,
    baseline: dict,
    indent: int = 0,
    path: str = "",
    override_paths: set[str],
) -> list[str]:
    if isinstance(value, dict):
        return _yaml_dict_lines(
            value,
            baseline=baseline,
            indent=indent,
            path=path,
            override_paths=override_paths,
        )
    if isinstance(value, list):
        return _yaml_list_lines(value, indent=indent)
    prefix = " " * indent
    return [f"{prefix}{_render_yaml_scalar(value)}"]


def _render_default_comment_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return _render_yaml_scalar(json.dumps(value, sort_keys=True))
    return _render_yaml_scalar(value)


def _render_yaml_scalar(value: Any) -> str:
    """Render a scalar value for inline YAML display without document markers."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    dumped = yaml.safe_dump(
        text,
        default_flow_style=True,
        explicit_end=False,
        explicit_start=False,
    ).strip()
    return dumped.replace("\n...", "")
