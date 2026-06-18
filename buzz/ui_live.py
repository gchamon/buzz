"""PyView-backed operator pages for the Buzz management UI."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, TypeVar, cast
from urllib.parse import ParseResult, parse_qs, quote, urlparse

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

from . import console, events
from .core.utils import format_bytes
from .models import (
    FIELD_ANIME_PATTERNS,
    FIELD_SUBTITLES_LANGUAGES,
    RESTART_REQUIRED_FIELDS,
    TaskStatus,
    deep_merge,
    diff_fields,
    effective_override_field_paths,
    get_nested_value,
    load_base_and_overrides,
    mask_secrets,
    to_nested_dict,
)

_TASK_STATUSES: set[TaskStatus] = {
    "pending",
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "complete",
    "failed",
    "aborted",
}
_LOG_LEVEL_SEVERITY = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
}

TOPIC_STATUS = "buzz:status"
TOPIC_ARCHIVE = "buzz:archive"
TOPIC_LOGS = "buzz:logs"
TOPIC_CONFIG = "buzz:config"
EVENT_NAVIGATE = "navigate"

_TEMPLATE_DIR = Path(__file__).with_name("pyview_templates")
ibis.loader = FileReloader(str(_TEMPLATE_DIR))
PAGE_NAMES = ("cache", "archive", "logs", "threads", "config")
PageName = Literal["cache", "archive", "logs", "threads", "config"]

_CONFIG_BOOL_FIELDS = (
    "provider.real_debrid.enabled",
    "provider.torbox.enabled",
    "compat.enable_all_dir",
    "compat.enable_unplayable_dir",
    "logging.verbose",
    "media_server.trigger_lib_scan",
    "media_server.scan_probe.enabled",
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
    "hooks.rd_update_delay_secs",
    "media_server.scan_probe.sample_ratio_percent",
    "media_server.scan_probe.min_files",
    "media_server.scan_probe.max_attempts",
    "media_server.scan_probe.read_bytes",
    "media_server.scan_probe.retry_delay_secs",
    "media_server.scan_probe.concurrency",
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
    "provider.priority",
    "provider.real_debrid.enabled",
    "provider.real_debrid.token",
    "provider.torbox.enabled",
    "provider.torbox.token",
    "provider.poll_interval_secs",
    "ui.poll_interval_secs",
    "provider.connection_concurrency",
    "server.bind",
    "server.port",
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
    "media_server.scan_probe.enabled",
    "media_server.scan_probe.sample_ratio_percent",
    "media_server.scan_probe.min_files",
    "media_server.scan_probe.max_attempts",
    "media_server.scan_probe.read_bytes",
    "media_server.scan_probe.retry_delay_secs",
    "media_server.scan_probe.concurrency",
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
    """A single item in a navigation list."""
    label: str
    value: str
    css_class: NotRequired[str]
    cycle_classes_json: NotRequired[str]
    cycle_values_json: NotRequired[str]


class PageNav(TypedDict):
    """Navigation state for the main shell."""
    archive_count: int
    cache_active: bool
    archive_active: bool
    logs_active: bool
    threads_active: bool
    config_active: bool
    log_count: int
    log_level: str
    thread_count: int


class PageContext(TypedDict):
    """Shared context for all Buzz operator pages."""
    active_provider: str
    active_provider_label: str
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
    """A file within a torrent being analyzed for cache."""
    id: str
    path: str
    bytes: int
    size: str
    is_video: bool
    selected: bool
    subtitle_query: str
    subtitle_default_query: str


class CacheFolderItem(TypedDict):
    """A selectable folder prefix in an expanded torrent."""
    path: str
    selected: bool
    selected_files: int
    total_files: int


class CacheAnalysisResult(TypedDict):
    """The result of a cache analysis for a specific torrent."""
    torrent_id: str
    filename: str
    files: list[CacheFileItem]
    provider: str
    provider_label: str


class CacheTorrentItem(TypedDict):
    """A torrent entry in the cache view."""
    id: str
    provider_torrent_id: str
    name: str
    category: str
    category_override: str
    status: str
    progress: int
    bytes: int
    size: str
    selected_files: int
    file_selection_pending: bool
    links: int
    ended: str
    short_id: str
    has_override: bool


class ArchiveProviderTag(TypedDict):
    """A UI tag representing a provider in the archive."""
    label: str
    css_class: str


class ArchiveItem(TypedDict):
    """An archived torrent entry."""
    bytes: int
    deleted_at: str
    file_count: int
    hash: str
    magnet: str | None
    name: str
    size: str
    provider_tags: list[ArchiveProviderTag]
    transfer_disabled: bool
    transfer_label: str
    transfer_show: bool
    transfer_target_provider: str
    transfer_title: str


class ArchiveTransferAction(TypedDict):
    """Provider transfer action rendered for an archive row."""
    disabled: bool
    label: str
    show: bool
    target_provider: str
    title: str


class CacheContext(PageContext):
    """Context for the cache operator page."""
    analysis_error: str
    analysis_results: list[CacheAnalysisResult]
    analyzing: bool
    caching: bool
    confirm_delete_id: str | None
    expanded_category: str
    expanded_category_override: str
    expanded_id: str | None
    expanded_files: list[CacheFileItem]
    expanded_folders: list[CacheFolderItem]
    title_override: dict[str, Any]
    title_override_kind: str
    title_override_active: bool
    title_override_provider_id: str
    title_override_fields: dict[str, Any]
    title_override_defaults: dict[str, Any]
    title_override_saved: dict[str, Any]
    parse_regex_placeholder: str
    parse_regex_test_url: str
    has_torrents: bool
    show_overlay: bool
    sort_col: int
    sort_dir: str
    subtitle_enabled: bool
    torrents: list[CacheTorrentItem]
    enabled_providers: list[tuple[str, str]]
    add_provider: str
    single_provider: bool


class ArchiveContext(PageContext):
    """Context for the archive operator page."""
    archive_items: list[ArchiveItem]
    confirm_delete_hash: str | None
    confirm_restore_hash: str | None
    has_items: bool
    sort_col: int
    sort_dir: str


class LogItem(TypedDict):
    """A single log entry for the UI."""
    copy_text: str
    level: str
    level_class: str
    level_label: str
    message: str
    message_prefix: str
    message_suffix: str
    source: str
    timestamp: str
    link_to_task_id: str
    task_link_text: str
    task_status: TaskStatus | str
    task_status_class: str


class LogsContext(PageContext):
    """Context for the logs operator page."""
    log_items: list[LogItem]
    logs_loaded: bool



class ThreadItem(TypedDict):
    """A background thread entry."""
    id: str
    short_id: str
    kind: str
    kind_class: str
    label: str
    detail: str
    status: TaskStatus
    status_class: str
    row_class: str
    started_at: str
    finished_at: str
    error: str
    cancellable: bool
    startable: bool
    expanded: bool
    logs: list[LogItem]
    log_order_label: str
    log_severity_class: str
    log_severity_title: str
    show_log_severity: bool
    status_group_title: str


class ThreadsContext(PageContext):
    """Context for the threads operator page."""
    has_threads: bool
    selected_thread_id: str
    logs_newest_first: bool
    thread_items: list[ThreadItem]
    real_debrid_enabled: bool
    torbox_enabled: bool


class ConfigLanguage(TypedDict):
    """A language selection for subtitles."""
    checked: bool
    code: str
    name: str


class ConfigValues(TypedDict):
    """Raw configuration values for the UI."""
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
    provider_priority: str
    real_debrid_enabled: bool
    real_debrid_token: str
    scan_probe_concurrency: int
    scan_probe_enabled: bool
    scan_probe_max_attempts: int
    scan_probe_min_files: int
    scan_probe_read_bytes: int
    scan_probe_retry_delay_secs: int | float
    scan_probe_sample_ratio_percent: int
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
    strategy: str
    subtitles_enabled: bool
    torbox_enabled: bool
    torbox_token: str
    trigger_lib_scan: bool
    verbose: bool
    version_label: str


class ConfigFieldState(TypedDict):
    css_class: str
    is_dirty: bool
    is_overridden: bool
    reload_mode: str
    baseline_value: str


# Config edit-form sections, in their natural (source) order. The key is the
# section's legend text and stable favorite identifier; the slug (key with
# non-alphanumerics replaced by "_") is how the template addresses per-section
# state, since ibis treats dots as nested access.
_CONFIG_SECTIONS: tuple[str, ...] = (
    "provider",
    "server",
    "hooks",
    "compat",
    "directories.anime",
    "request",
    "ui",
    "logging",
    "media_server",
    "subtitles",
)


def _section_slug(section: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", section.lower())


class ConfigSectionState(TypedDict):
    favorite: str
    order: int
    star_class: str
    star_icon: str


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
    section_states: dict[str, ConfigSectionState]
    focus_section: str
    subtitles_credentials_ready: bool
    values: ConfigValues


class ShellContext(PageContext):
    active_page: PageName
    archive: ArchiveContext
    cache: CacheContext
    config: ConfigContext
    logs: LogsContext
    threads: ThreadsContext
    page_content: Markup
    real_debrid_enabled: bool
    torbox_enabled: bool


_PROVIDER_SHORT_LABELS: dict[str, str] = {
    "real_debrid": "rd",
    "torbox": "tb",
}

_PROVIDER_CSS_CLASSES: dict[str, str] = {
    "real_debrid": "label-blue",
    "torbox": "label-green",
}


def _first_value(value: Any) -> str:
    """Extract a single string from a form payload value (may be a list)."""
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _dom_safe_id(value: str) -> str:
    """Return a string safe for use in HTML id attributes."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)


def _torrent_short_id(torrent: dict[str, Any]) -> str:
    """Return a display-friendly short ID for a torrent cache entry."""
    cache_id: str = torrent.get("id", "")
    provider_torrent_id: str = torrent.get("provider_torrent_id", "")
    # For RD the cache key is the bare RD ID (long hex); take first 8 chars.
    # For other providers the cache key is "provider:id"; use a short label instead.
    if ":" not in cache_id:
        return cache_id[:8]
    provider = cache_id.split(":", 1)[0]
    label = _PROVIDER_SHORT_LABELS.get(provider, provider[:2])
    return f"{label}:{provider_torrent_id}" if provider_torrent_id else cache_id[:8]


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
    app.add_live_view("/threads", lambda: BuzzLiveView(owner))  # pyright: ignore[reportArgumentType]
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
            "archive_count": len(self.owner.state.archive),
            "cache_active": self.page_name == "cache",
            "archive_active": self.page_name == "archive",
            "logs_active": self.page_name == "logs",
            "threads_active": self.page_name == "threads",
            "config_active": self.page_name == "config",
            "log_count": self.owner.log_count(),
            "log_level": self._highest_log_level(),
            "thread_count": len(self.owner.state.background_tasks.snapshot()),
        }

    def _highest_log_level(self) -> str:
        from .core.events import registry

        priority = {"error": 3, "warning": 2, "info": 1, "debug": 0}
        override = str(getattr(self.owner, "_nav_log_level_override", "") or "").lower()
        if override in priority:
            return override

        logs = registry.get_recent(limit=50)
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
        return [
            {"label": "cache", "value": str(len(self.owner.state.torrents()))},
            {
                "label": "last_sync",
                "value": status.get("last_sync_at") or "never",
            },
            self._state_meta_item(status),
        ]

    def _state_meta_item(self, status: dict[str, Any]) -> PageItem:
        values = []
        if status.get("sync_in_progress"):
            values.append("syncing")
        if status.get("file_selection_pending"):
            values.append("file_selection_pending")
        hook_label = self._hook_status_label(status)
        if hook_label != "idle":
            values.append(hook_label)
        if not values:
            values.append("idle")

        item: PageItem = {"label": "state", "value": values[0]}
        if values == ["file_selection_pending"]:
            item["css_class"] = console.Level.ERROR
        elif "file_selection_pending" in values:
            item["cycle_classes_json"] = json.dumps({
                "file_selection_pending": console.Level.ERROR
            })
        if len(values) > 1:
            item["cycle_values_json"] = json.dumps(values)
        return item

    def _hook_status_label(self, status: dict[str, Any]) -> str:
        phase = str(status.get("hook_phase") or "idle")
        pending_count = int(status.get("hook_pending_count") or 0)
        if phase in {"idle", "complete"} and pending_count == 0:
            return "idle"
        return phase

    def _base_context(
        self,
        console_msg: str = "",
        console_class: str = "",
    ) -> PageContext:
        status = self.owner.state.status()
        restart_required = bool(getattr(self.owner, "restart_required", False))

        if restart_required:
            status_label = "[restart required]"
            status_class = console.Level.RESTART
        elif status.get("provider_degraded"):
            status_label = "[degraded]"
            status_class = console.Level.WARNING
        elif not self.owner.is_ready():
            status_label = "[starting]"
            status_class = console.Level.PENDING
        else:
            status_label = "[ready]"
            status_class = console.Level.SUCCESS

        provider = self.owner.config.provider_active
        context: PageContext = {
            "active_provider": provider,
            "active_provider_label": provider.replace("_", " ").upper(),
            "console_class": console_class,
            "console_msg": console_msg,
            "has_error": bool(status.get("last_error")),
            "is_ready": self.owner.is_ready(),
            "last_error": status.get("last_error") or "",
            "token_configured": bool(self.owner.client),
            "meta_items": self._meta_items(),
            "nav": self._nav(),
            "status_class": status_class,
            "status_label": status_label,
        }
        return context

    def _resync_library(self) -> tuple[str, str]:
        try:
            self.owner.state.manual_rebuild()
        except Exception as exc:
            return f"resync failed: {exc}", console.Level.ERROR
        return "library resynced!", console.Level.SUCCESS

    async def mount(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        socket: LiveViewSocket[_TContext],
        session: dict[str, Any],
    ) -> None:
        socket.live_title = self.page_title
        if is_connected(socket):
            await socket.subscribe(TOPIC_STATUS)

    @info(TOPIC_STATUS)
    async def handle_status(
        self, _event: InfoEvent, _socket: LiveViewSocket[PageContext]
    ) -> None:
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

    async def handle_event(  # noqa: C901
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
        id: str = "",
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
            for result in socket.context["analysis_results"]:
                with contextlib.suppress(Exception):
                    self.owner.state.delete_torrent(result["torrent_id"])
            socket.context = self._context(
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
                confirm_delete_id=socket.context["confirm_delete_id"],
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
                add_provider=socket.context["add_provider"],
            )
            return
        if event == "toggle_expand":
            current = socket.context["expanded_id"]
            socket.context = self._context(
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
                confirm_delete_id=socket.context["confirm_delete_id"],
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
                expanded_id=None if current == id else id,
                add_provider=socket.context["add_provider"],
            )
            return
        if event == "toggle_selected_file":
            for file in socket.context["expanded_files"]:
                if file["id"] == file_id:
                    file["selected"] = not file["selected"]
                    break
            self._refresh_expanded_folders(socket)
            return
        if event == "select_expanded":
            for file in socket.context["expanded_files"]:
                if mode == "all":
                    file["selected"] = True
                elif mode == "none":
                    file["selected"] = False
                elif mode == "video":
                    file["selected"] = file["is_video"]
            self._refresh_expanded_folders(socket)
            return
        if event == "toggle_folder_selection":
            self._toggle_folder_selection(socket, id)
            self._refresh_expanded_folders(socket)
            return
        if event == "apply_selection":
            self._handle_apply_selection(socket, id)
            return
        if event == "set_category":
            self._handle_set_category(socket, id, mode)
            return
        if event == "set_subtitle_query":
            self._handle_set_subtitle_query(socket, payload)
            return
        if event == "set_curator_title":
            self._handle_set_curator_title(socket, payload)
            return
        if event == "sort":
            self._handle_sort(socket, col)

    def _handle_apply_selection(
        self, socket: ConnectedLiveViewSocket[CacheContext], cache_id: str
    ) -> None:
        selected_ids = [
            file["id"]
            for file in socket.context["expanded_files"]
            if file["selected"]
        ]
        try:
            self.owner.state.select_files(cache_id, selected_ids)
            console_msg = "file selection updated"
            console_class = console.Level.SUCCESS
        except Exception as exc:  # noqa: BLE001
            console_msg = f"file selection failed: {exc}"
            console_class = console.Level.ERROR
        socket.context = self._context(
            console_msg=console_msg,
            console_class=console_class,
            confirm_delete_id=socket.context["confirm_delete_id"],
            sort_col=socket.context["sort_col"],
            sort_dir=socket.context["sort_dir"],
            expanded_id=cache_id,
            add_provider=socket.context["add_provider"],
        )

    def _handle_set_category(
        self,
        socket: ConnectedLiveViewSocket[CacheContext],
        cache_id: str,
        category: str,
    ) -> None:
        try:
            self.owner.state.set_torrent_category(cache_id, category)
            console_msg = "category updated"
            console_class = console.Level.SUCCESS
        except Exception as exc:  # noqa: BLE001
            console_msg = f"category update failed: {exc}"
            console_class = console.Level.ERROR
        socket.context = self._context(
            console_msg=console_msg,
            console_class=console_class,
            confirm_delete_id=socket.context["confirm_delete_id"],
            sort_col=socket.context["sort_col"],
            sort_dir=socket.context["sort_dir"],
            expanded_id=cache_id,
            add_provider=socket.context["add_provider"],
        )

    def _handle_set_subtitle_query(
        self,
        socket: ConnectedLiveViewSocket[CacheContext],
        payload: dict[str, Any] | None,
    ) -> None:
        data = payload or {}
        cache_id = _first_value(data.get("id"))
        path = _first_value(data.get("path"))
        query = _first_value(data.get("query"))
        try:
            self.owner.state.set_subtitle_query_override(cache_id, path, query)
            console_msg = "subtitle query updated"
            console_class = console.Level.SUCCESS
        except Exception as exc:  # noqa: BLE001
            console_msg = f"subtitle query update failed: {exc}"
            console_class = console.Level.ERROR
        socket.context = self._context(
            console_msg=console_msg,
            console_class=console_class,
            confirm_delete_id=socket.context["confirm_delete_id"],
            sort_col=socket.context["sort_col"],
            sort_dir=socket.context["sort_dir"],
            expanded_id=cache_id or socket.context["expanded_id"],
            add_provider=socket.context["add_provider"],
        )

    def _handle_set_curator_title(
        self,
        socket: ConnectedLiveViewSocket[CacheContext],
        payload: dict[str, Any] | None,
    ) -> None:
        data = payload or {}
        cache_id = _first_value(data.get("cache_id")) or _first_value(
            data.get("id")
        )
        override = {
            "kind": _first_value(data.get("kind")),
            "title": _first_value(data.get("title")),
            "series": _first_value(data.get("series")),
            "year": _first_value(data.get("year")),
            "id": _first_value(data.get("external_id")),
            "imdbid": _first_value(data.get("imdbid")),
            "tmdbid": _first_value(data.get("tmdbid")),
            "tvdbid": _first_value(data.get("tvdbid")),
            "anidbid": _first_value(data.get("anidbid")),
            "parse_regex": _first_value(data.get("parse_regex")),
        }
        try:
            self.owner.state.set_curator_title_override(cache_id, override)
            console_msg = "curator title override updated"
            console_class = console.Level.SUCCESS
        except Exception as exc:  # noqa: BLE001
            console_msg = f"curator title override failed: {exc}"
            console_class = console.Level.ERROR
        socket.context = self._context(
            console_msg=console_msg,
            console_class=console_class,
            confirm_delete_id=socket.context["confirm_delete_id"],
            sort_col=socket.context["sort_col"],
            sort_dir=socket.context["sort_dir"],
            expanded_id=cache_id or socket.context["expanded_id"],
            add_provider=socket.context["add_provider"],
        )

    def _handle_delete(
        self, socket: ConnectedLiveViewSocket[CacheContext], hash: str
    ) -> None:
        try:
            self.owner.state.delete_torrent(hash)
            socket.context = self._context(
                console_msg="removing from cache...",
                console_class=console.Level.PENDING,
                confirm_delete_id=None,
                analysis_results=socket.context["analysis_results"],
                analysis_error=socket.context["analysis_error"],
                analyzing=socket.context["analyzing"],
                caching=socket.context["caching"],
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
                add_provider=socket.context["add_provider"],
            )
        except Exception as exc:
            socket.context = self._context(
                console_msg=f"delete failed: {exc}",
                console_class=console.Level.ERROR,
                confirm_delete_id=None,
                analysis_results=socket.context["analysis_results"],
                analysis_error=socket.context["analysis_error"],
                analyzing=socket.context["analyzing"],
                caching=socket.context["caching"],
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
                add_provider=socket.context["add_provider"],
            )

    def _handle_fetch_subs(
        self, socket: ConnectedLiveViewSocket[CacheContext], torrent_name: str
    ) -> None:
        result = self.owner.fetch_subtitles(torrent_name)
        if result.get("error"):
            console.log(socket.context, f"subs fetch failed: {result['error']}", console.Level.ERROR)
        else:
            console.log(socket.context, f"subs fetch triggered for: {torrent_name}", console.Level.SUCCESS)

    def _handle_resync(
        self, socket: ConnectedLiveViewSocket[CacheContext]
    ) -> None:
        console.log(socket.context, "resyncing library...", console.Level.PENDING)
        msg, css_class = self._resync_library()
        socket.context["console_msg"] = msg
        socket.context["console_class"] = css_class

    def _handle_analyze(  # noqa: C901
        self,
        socket: ConnectedLiveViewSocket[CacheContext],
        payload: dict[str, Any] | None,
    ) -> None:
        raw = (payload or {}).get("magnet", [])
        if isinstance(raw, str):
            raw = raw.splitlines()
        lines: list[str] = []
        for item in raw:
            lines.extend(str(item).splitlines())
        magnets = [m.strip() for m in lines if m.strip()]
        if not magnets:
            return
        chosen_provider = (payload or {}).get("provider", "auto")
        if isinstance(chosen_provider, list):
            chosen_provider = chosen_provider[0] if chosen_provider else "auto"
        chosen_provider = str(chosen_provider).strip() or "auto"
        if chosen_provider == "auto":
            chosen_provider = "auto"
        socket.context["add_provider"] = chosen_provider
        socket.context["analyzing"] = True
        socket.context["analysis_error"] = ""
        results: list[CacheAnalysisResult] = []
        errors: list[str] = []
        import re

        for magnet in magnets:
            try:
                info = self.owner.state.add_magnet(
                    magnet,
                    None if chosen_provider == "auto" else chosen_provider,
                )
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
                            "subtitle_query": "",
                            "subtitle_default_query": Path(path).stem,
                        }
                    )
                p = str(info.get("provider", "unknown"))
                torrent_id = str(info.get("cache_key") or info["id"])
                results.append(
                    {
                        "torrent_id": torrent_id,
                        "filename": str(
                            info.get("filename") or "Torrent Files"
                        ),
                        "files": files,
                        "provider": p,
                        "provider_label": p.replace("_", " ").upper(),
                    }
                )
            except Exception as exc:
                import traceback
                events.log(
                    f"magnet analyze traceback: {traceback.format_exc()}",
                    level=events.Level.ERROR,
                )
                errors.append(str(exc))
        socket.context["analyzing"] = False
        socket.context["analysis_results"] = results
        n_ok = len(results)
        n_err = len(errors)
        is_single = len(magnets) == 1
        if errors:
            for err_msg in errors:
                events.log(
                    f"magnet add failed: {err_msg}",
                    level=events.Level.ERROR,
                    event=events.Event.MAGNET_ADD_FAILED,
                )
            for res in results:
                events.log(
                    f"magnet resolved: {res['filename']} via {res['provider']}",
                    level=events.Level.INFO,
                    event=events.Event.MAGNET_ADD_OK,
                )
            if is_single:
                socket.context["analysis_error"] = f"Failed: {errors[0]}"
                console.log(
                    socket.context,
                    f"failed to resolve magnet: {errors[0]}",
                    console.Level.ERROR,
                )
            else:
                socket.context["analysis_error"] = f"Failed: {'; '.join(errors)}"
                console.log(
                    socket.context,
                    f"Resolved {n_ok} magnets, {n_err} failed. See logs for detail.",
                    console.Level.ERROR if n_ok == 0 else console.Level.WARNING,
                )
        else:
            if len(magnets) > 1:
                for res in results:
                    events.log(
                        (
                            "magnet resolved: "
                            f"{res['filename']} via {res['provider']}"
                        ),
                        level=events.Level.INFO,
                        event=events.Event.MAGNET_ADD_OK,
                    )
            console.log(
                socket.context,
                f"Resolved {n_ok} magnet{'s' if n_ok != 1 else ''}.",
                console.Level.SUCCESS,
            )

    def _handle_confirm_cache(
        self, socket: ConnectedLiveViewSocket[CacheContext]
    ) -> None:
        try:
            selections: dict[str, list[str]] = {}
            for result in socket.context["analysis_results"]:
                selected = [f["id"] for f in result["files"] if f["selected"]]
                if selected:
                    selections[result["torrent_id"]] = selected
            task_id = self.owner.state.submit_cache_selection(selections)
            socket.context = self._context(
                console_msg=f"cache job queued: {task_id}",
                console_class=console.Level.SUCCESS,
                confirm_delete_id=socket.context["confirm_delete_id"],
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
                add_provider=socket.context["add_provider"],
            )
        except Exception as exc:
            socket.context["caching"] = False
            console.log(socket.context, f"Error: {exc}", console.Level.ERROR)

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
            analysis_results=socket.context["analysis_results"],
            analysis_error=socket.context["analysis_error"],
            analyzing=socket.context["analyzing"],
            caching=socket.context["caching"],
            sort_col=sort_col,
            sort_dir=sort_dir,
            add_provider=socket.context["add_provider"],
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
            analysis_results=socket.context["analysis_results"],
            analysis_error=socket.context["analysis_error"],
            analyzing=socket.context["analyzing"],
            caching=socket.context["caching"],
            sort_col=socket.context["sort_col"],
            sort_dir=socket.context["sort_dir"],
            add_provider=socket.context["add_provider"],
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
        analysis_results: list[CacheAnalysisResult] | None = None,
        analysis_error: str = "",
        analyzing: bool = False,
        caching: bool = False,
        sort_col: int = 0,
        sort_dir: str = "asc",
        expanded_id: str | None = None,
        add_provider: str = "auto",
    ) -> CacheContext:
        torrents = []
        for torrent in self.owner.state.torrents():
            category_override = torrent["category_override"] or ""
            title_override = self.owner.state.curator_title_override(
                torrent["id"]
            )
            torrents.append(
                {
                    "id": torrent["id"],
                    "name": torrent["name"],
                    "category": torrent["category"],
                    "category_override": category_override,
                    "status": torrent["status"],
                    "progress": torrent["progress"],
                    "bytes": torrent["bytes"],
                    "size": format_bytes(torrent["bytes"]),
                    "selected_files": torrent["selected_files"],
                    "file_selection_pending": torrent["file_selection_pending"],
                    "links": torrent["links"],
                    "ended": torrent["ended"] or "-",
                    "short_id": _torrent_short_id(torrent),
                    # Any entry-level override (category or Curator identity)
                    # marks the row so the collapsed name can glow.
                    "has_override": bool(category_override)
                    or bool(title_override),
                }
            )
        torrents = self._sort_torrents(torrents, sort_col, sort_dir)
        base = self._base_context(console_msg, console_class)
        analysis_results = analysis_results or []
        expanded_files = self._expanded_files(expanded_id)
        expanded_category = self.owner.state.torrent_category(expanded_id)
        expanded_folders = self._expanded_folders(expanded_files)
        title_override_context = self._title_override_context(expanded_id)
        expanded_identity = self._parse_regex_identity(
            title_override_context["title_override_fields"]
        )
        enabled_providers = [
            (name, name.replace("_", " ").upper())
            for name, _ in self.owner.state._ordered_clients()
        ]
        return cast(
            CacheContext,
            {
                **base,
                "analysis_error": analysis_error,
                "analysis_results": analysis_results,
                "analyzing": analyzing,
                "caching": caching,
                "confirm_delete_id": confirm_delete_id,
                "expanded_category": expanded_category["effective"],
                "expanded_category_override": expanded_category["override"],
                "expanded_id": expanded_id,
                "expanded_files": expanded_files,
                "expanded_folders": expanded_folders,
                **title_override_context,
                "parse_regex_placeholder": self._parse_regex_placeholder(
                    expanded_id
                ),
                "parse_regex_test_url": self._parse_regex_test_url(
                    expanded_id, expanded_files, expanded_identity
                ),
                "has_torrents": bool(torrents),
                "show_overlay": analyzing or caching,
                "sort_col": sort_col,
                "sort_dir": sort_dir,
                "subtitle_enabled": self.owner.config.subtitles.enabled,
                "torrents": torrents,
                "enabled_providers": enabled_providers,
                "add_provider": add_provider,
                "single_provider": len(enabled_providers) == 1,
            },
        )

    def _parse_regex_placeholder(self, expanded_id: str | None) -> str:
        kind = self._title_override_kind(expanded_id)
        if kind == "movie":
            return r"(?P<title>.+?) (?P<year>19\d{2}|20\d{2})"
        return (
            r"(?P<series>.+?) S(?P<season>\d+) - "
            r"(?P<episode>\d+)(?:v\d+)?"
        )

    def _parse_regex_test_url(
        self,
        expanded_id: str | None,
        expanded_files: list[CacheFileItem],
        expanded_identity: str = "",
    ) -> str:
        regex = self.owner.state.curator_title_override(expanded_id).get(
            "parse_regex"
        ) or self._parse_regex_placeholder(expanded_id)
        test_string = "\n".join(
            self._parse_regex_test_line(file, expanded_identity)
            for file in expanded_files
            if file["is_video"]
        )
        return (
            "https://pythex.org/?regex="
            f"{quote(str(regex))}&test_string={quote(test_string)}"
            "&mode=finditer"
        )

    @staticmethod
    def _parse_regex_test_line(
        file: CacheFileItem, expanded_identity: str = ""
    ) -> str:
        stem = Path(str(file["path"])).stem
        if expanded_identity:
            return f"{expanded_identity}/{stem}"
        return stem

    @staticmethod
    def _parse_regex_identity(fields: dict[str, Any]) -> str:
        return str(fields.get("title") or fields.get("series") or "").strip()

    def _expanded_files(
        self, expanded_id: str | None
    ) -> list[CacheFileItem]:
        if not expanded_id:
            return []
        files: list[CacheFileItem] = []
        kind = self._title_override_kind(expanded_id)
        defaults = self._title_override_defaults(expanded_id, kind)
        for file_item in self.owner.state.torrent_files(expanded_id):
            files.append(
                {
                    "id": file_item["id"],
                    "path": file_item["path"],
                    "bytes": file_item["bytes"],
                    "size": format_bytes(file_item["bytes"]),
                    "is_video": file_item["is_video"],
                    "selected": file_item["selected"],
                    "subtitle_query": self.owner.state.subtitle_query_override(
                        expanded_id, file_item["path"]
                    ),
                    "subtitle_default_query": self._subtitle_default_query(
                        file_item["path"], kind, defaults
                    ),
                }
            )
        return files

    @staticmethod
    def _query_with_series(series: str, filename: str) -> str:
        """Return a filename query, adding series only when it is absent."""
        if not series:
            return filename
        if not filename:
            return series
        series_tokens = set(re.split(r"[\s._\-]+", series.lower()))
        filename_tokens = set(re.split(r"[\s._\-]+", filename.lower()))
        series_tokens.discard("")
        filename_tokens.discard("")
        if series_tokens and series_tokens <= filename_tokens:
            return filename
        return f"{series} {filename}"

    @staticmethod
    def _subtitle_default_query(
        path: str, kind: str, defaults: dict[str, Any]
    ) -> str:
        """Return the visible default subtitle search query for a file."""
        filename_query = Path(path).stem
        if kind in {"show", "anime"}:
            query = CacheLiveView._query_with_series(
                str(defaults.get("series") or ""), filename_query
            )
        elif kind == "movie":
            query = defaults.get("title")
        else:
            query = ""
        return str(query or filename_query)

    def _title_override_context(
        self, expanded_id: str | None
    ) -> dict[str, Any]:
        """Build entry-level Curator title override fields for the context."""
        kind = self._title_override_kind(expanded_id)
        saved = self.owner.state.curator_title_override(expanded_id)
        active = bool(saved)
        saved.setdefault("provider_ids", {})
        defaults = self._title_override_defaults(expanded_id, kind)
        # Effective form values: saved override when present, else the
        # locally derived default. ``fields`` feeds the input ``value`` and
        # ``defaults`` feeds ``data-default`` for the client-side Revert.
        # ``saved_fields`` feeds ``data-saved`` for the dirty baseline: empty
        # strings when nothing is saved, actual saved values otherwise.
        fields = self._merge_title_override_fields(defaults, saved, active)
        saved_fields = self._title_override_field_dict(saved)
        return {
            "title_override": saved,
            "title_override_kind": kind,
            "title_override_active": active,
            "title_override_provider_id": (
                self._title_override_provider_id(saved)
            ),
            "title_override_fields": fields,
            "title_override_defaults": defaults,
            "title_override_saved": saved_fields,
            # Provider-prefixed cache ids contain ':' (e.g. "torbox:38618447"),
            # which is not a valid CSS identifier and breaks LiveView morphdom's
            # id matching (it appends duplicate forms on each re-render). Use a
            # sanitized slug for DOM ids; keep phx-value-id as the real id.
            "expanded_dom_id": _dom_safe_id(expanded_id or ""),
        }

    def _title_override_defaults(
        self, expanded_id: str | None, kind: str
    ) -> dict[str, Any]:
        """Locally derived identity defaults used to prefill the form.

        Uses the local (no-network) suggestion only: this runs on every render
        while an entry is expanded, so it must never make a remote Jellyfin
        lookup (which would flood Jellyfin's RemoteSearch endpoint).
        """
        if not expanded_id or kind not in {"movie", "show", "anime"}:
            return self._title_override_field_dict({})
        try:
            suggestion = self.owner.state.local_curator_title_suggestion(
                expanded_id, kind
            )
        except Exception:  # noqa: BLE001 - suggestion is best-effort
            suggestion = {}
        return self._title_override_field_dict(suggestion)

    @staticmethod
    def _title_override_field_dict(override: dict[str, Any]) -> dict[str, Any]:
        """Normalize an override dict to flat string form fields."""
        provider_ids = override.get("provider_ids")
        if not isinstance(provider_ids, dict):
            provider_ids = {}
        return {
            "title": str(override.get("title") or ""),
            "series": str(override.get("series") or ""),
            "year": str(override.get("year") or ""),
            "parse_regex": str(override.get("parse_regex") or ""),
            "provider_ids": {
                provider: str(provider_ids.get(provider) or "")
                for provider in ("imdbid", "tmdbid", "tvdbid", "anidbid")
            },
        }

    @staticmethod
    def _merge_title_override_fields(
        defaults: dict[str, Any], saved: dict[str, Any], active: bool
    ) -> dict[str, Any]:
        """Build form field values from saved override and auto-derived defaults.

        When an override is active, saved values are shown as-is — an absent
        key means the user intentionally cleared that field. When no override
        is active, defaults are used as prefill values.
        """
        saved_provider_ids = saved.get("provider_ids")
        if not isinstance(saved_provider_ids, dict):
            saved_provider_ids = {}
        default_provider_ids = defaults["provider_ids"]
        if active:
            return {
                "title": str(saved.get("title") or ""),
                "series": str(saved.get("series") or ""),
                "year": str(saved.get("year") or ""),
                "parse_regex": str(saved.get("parse_regex") or ""),
                "provider_ids": {
                    provider: str(saved_provider_ids.get(provider) or "")
                    for provider in ("imdbid", "tmdbid", "tvdbid", "anidbid")
                },
            }
        return {
            "title": str(saved.get("title") or "") or defaults["title"],
            "series": str(saved.get("series") or "") or defaults["series"],
            "year": str(saved.get("year") or "") or defaults["year"],
            "parse_regex": (
                str(saved.get("parse_regex") or "")
                or defaults["parse_regex"]
            ),
            "provider_ids": {
                provider: (
                    str(saved_provider_ids.get(provider) or "")
                    or default_provider_ids[provider]
                )
                for provider in ("imdbid", "tmdbid", "tvdbid", "anidbid")
            },
        }

    @staticmethod
    def _title_override_provider_id(title_override: dict[str, Any]) -> str:
        provider_ids = title_override.get("provider_ids")
        if not isinstance(provider_ids, dict):
            return str(title_override.get("id") or "")
        for provider in ("imdbid", "tmdbid", "tvdbid", "anidbid"):
            value = str(provider_ids.get(provider) or "").strip()
            if value:
                return f"{provider}-{value}"
        return ""

    def _title_override_kind(self, expanded_id: str | None) -> str:
        category = self.owner.state.torrent_category(expanded_id)["effective"]
        if category == "movies":
            return "movie"
        if category == "shows":
            return "show"
        if category == "anime":
            return "anime"
        return ""

    @staticmethod
    def _expanded_folders(
        files: Sequence[Mapping[str, Any]],
    ) -> list[CacheFolderItem]:
        folders: dict[str, dict[str, int]] = {}
        for file_item in files:
            path = str(file_item["path"]).strip("/")
            parts = [part for part in path.split("/") if part]
            for index in range(1, len(parts)):
                folder_path = "/".join(parts[:index])
                counts = folders.setdefault(
                    folder_path, {"selected_files": 0, "total_files": 0}
                )
                counts["total_files"] += 1
                if file_item["selected"]:
                    counts["selected_files"] += 1
        return [
            {
                "path": path,
                "selected": counts["selected_files"] == counts["total_files"],
                "selected_files": counts["selected_files"],
                "total_files": counts["total_files"],
            }
            for path, counts in sorted(folders.items())
        ]

    @staticmethod
    def _toggle_folder_selection(
        socket: ConnectedLiveViewSocket[CacheContext],
        folder_path: str,
    ) -> None:
        prefix = folder_path.rstrip("/") + "/"
        matching_files = [
            file
            for file in socket.context["expanded_files"]
            if str(file["path"]).lstrip("/").startswith(prefix)
        ]
        selected = not (
            matching_files and all(file["selected"] for file in matching_files)
        )
        for file in matching_files:
            file["selected"] = selected

    @staticmethod
    def _refresh_expanded_folders(
        socket: ConnectedLiveViewSocket[CacheContext],
    ) -> None:
        socket.context["expanded_folders"] = CacheLiveView._expanded_folders(
            socket.context["expanded_files"]
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

    async def handle_event(  # noqa: C901
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
            task_id = self.owner.state.submit_archive_restore(hash)
            socket.context = self._context(
                console_msg=f"restore queued: {task_id}",
                console_class=console.Level.RESTART,
                sort_col=socket.context["sort_col"],
                sort_dir=socket.context["sort_dir"],
            )
            return
        if event == "transfer_provider":
            try:
                item = next(
                    item
                    for item in socket.context["archive_items"]
                    if item["hash"] == hash
                )
                task_id = self.owner.state.submit_archive_provider_transfer(
                    hash,
                    item["transfer_target_provider"],
                )
                socket.context = self._context(
                    console_msg=f"provider transfer pending: {task_id}",
                    console_class=console.Level.RESTART,
                    sort_col=socket.context["sort_col"],
                    sort_dir=socket.context["sort_dir"],
                )
            except Exception as exc:
                socket.context = self._context(
                    console_msg=f"provider transfer failed: {exc}",
                    console_class=console.Level.ERROR,
                    sort_col=socket.context["sort_col"],
                    sort_dir=socket.context["sort_dir"],
                )
            return
        if event == "delete":
            self.owner.state.delete_archive_permanently(hash)
            socket.context = self._context(
                console_msg="archive item deleted",
                console_class=console.Level.SUCCESS,
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
                sort_dir = (
                    "desc" if socket.context["sort_dir"] == "asc" else "asc"
                )
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
        from .core import db as buzz_db
        items: list[ArchiveItem] = []
        for torrent in self.owner.state.archive_torrents():
            thash = torrent["hash"]
            links = buzz_db.load_provider_links_by_hash(
                self.owner.state.conn, thash
            )
            unique_providers = sorted({prov for prov, _ in links})
            transfer = self._archive_transfer_action(unique_providers)
            provider_tags: list[ArchiveProviderTag] = [
                {
                    "label": _PROVIDER_SHORT_LABELS.get(p, p),
                    "css_class": _PROVIDER_CSS_CLASSES.get(p, "label-grey"),
                }
                for p in unique_providers
            ]
            items.append(
                {
                    "bytes": torrent["bytes"],
                    "deleted_at": torrent["deleted_at"] or "-",
                    "file_count": torrent["file_count"],
                    "hash": thash,
                    "magnet": torrent["magnet"],
                    "name": torrent["name"],
                    "size": format_bytes(torrent["bytes"]),
                    "provider_tags": provider_tags,
                    "transfer_disabled": transfer["disabled"],
                    "transfer_label": transfer["label"],
                    "transfer_show": transfer["show"],
                    "transfer_target_provider": transfer["target_provider"],
                    "transfer_title": transfer["title"],
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

    def _archive_transfer_action(
        self,
        unique_providers: list[str],
    ) -> ArchiveTransferAction:
        enabled = [name for name, _client in self.owner.state._ordered_clients()]
        primary = enabled[0] if enabled else ""
        present = set(unique_providers)
        missing = [provider for provider in enabled if provider not in present]

        # No enabled providers or nothing to transfer to/from: omit button.
        if not enabled or (not present and not missing):
            return {"disabled": True, "label": "C", "show": False, "target_provider": "", "title": ""}

        # Already in all enabled providers: show grayed [C].
        if len(present) >= 2 or not missing:
            return {
                "disabled": True,
                "label": "C",
                "show": True,
                "target_provider": "",
                "title": "Already in all enabled providers",
            }

        # Pure archive (no provider) with magnet: omit [M], restore via [R].
        if not present:
            return {"disabled": True, "label": "M", "show": False, "target_provider": "", "title": ""}

        # Single provider present, transfer possible (by hash, no magnet needed).
        source = next(iter(present))
        if source == primary:
            target = missing[0]
            return {
                "disabled": False,
                "label": "C",
                "show": True,
                "target_provider": target,
                "title": f"Copy to {self.owner.state._provider_label(target)}",
            }
        target = primary
        if not target or target not in missing:
            return {
                "disabled": True,
                "label": "M",
                "show": True,
                "target_provider": "",
                "title": "Provider move unavailable: primary provider already has this torrent",
            }
        return {
            "disabled": False,
            "label": "M",
            "show": True,
            "target_provider": target,
            "title": f"Move to {self.owner.state._provider_label(target)}",
        }


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
            await socket.subscribe(TOPIC_LOGS)

    async def handle_event(  # noqa: C901
        self,
        event: str,
        socket: ConnectedLiveViewSocket[LogsContext],
        to: str = "",
    ) -> None:
        if event == EVENT_NAVIGATE:
            await socket.push_navigate(to)
            return
        if event == "clear_logs":
            self.owner.clear_logs()
            socket.context = self._context()
            return
        if event == "resync":
            console.log(socket.context, "resyncing library...", console.Level.PENDING)
            msg, css_class = self._resync_library()
            console.log(socket.context, msg, css_class)

    async def handle_info(
        self,
        event: InfoEvent,
        socket: ConnectedLiveViewSocket[LogsContext],
    ) -> None:
        if event.name not in {TOPIC_LOGS, TOPIC_STATUS}:
            return
        socket.context = self._context()

    async def render(
        self,
        assigns: LogsContext,
        meta: Any,
    ) -> RenderedContent:
        return LiveRender(_load_template("logs_live.html"), assigns, meta)

    def _context(self) -> LogsContext:
        base = self._base_context()
        return cast(
            LogsContext,
            {
                **base,
                "log_items": self.owner.formatted_logs(limit=100),
                "logs_loaded": True,
            },
        )


class ThreadsLiveView(_BaseBuzzLiveView):
    page_name = "threads"
    page_title = "buzz: threads"

    async def mount(  # pyright: ignore[reportIncompatibleMethodOverride, reportArgumentType]
        self,
        socket: LiveViewSocket[ThreadsContext],
        session: dict[str, Any],
    ) -> None:
        await super().mount(socket, session)  # pyright: ignore[reportArgumentType]
        socket.context = self._context()
        if is_connected(socket):
            await socket.subscribe(TOPIC_STATUS)

    async def handle_event(
        self,
        event: str,
        socket: ConnectedLiveViewSocket[ThreadsContext],
        to: str = "",
        task_id: str = "",
    ) -> None:
        if event == EVENT_NAVIGATE:
            await socket.push_navigate(to)
            return
        if event == "scan_rd":
            try:
                new_task_id = self.owner.state.submit_infringing_scan()
                socket.context = self._context(
                    console_msg=f"Real-Debrid scan queued: {new_task_id}",
                    console_class=console.Level.RESTART,
                    selected_thread_id=new_task_id,
                    logs_newest_first=socket.context["logs_newest_first"],
                )
            except Exception as exc:
                socket.context = self._context(
                    console_msg=f"scan failed: {exc}",
                    console_class=console.Level.ERROR,
                    selected_thread_id=socket.context["selected_thread_id"],
                    logs_newest_first=socket.context["logs_newest_first"],
                )
            return
        if event in {"migrate_rd_tb", "migrate_tb_rd"}:
            source_provider = (
                "real_debrid" if event == "migrate_rd_tb" else "torbox"
            )
            destination_provider = (
                "torbox" if event == "migrate_rd_tb" else "real_debrid"
            )
            try:
                new_task_id = self.owner.state.submit_provider_migration_scan(
                    source_provider,
                    destination_provider,
                )
                socket.context = self._context(
                    console_msg=f"migration scan queued: {new_task_id}",
                    console_class=console.Level.RESTART,
                    selected_thread_id=new_task_id,
                    logs_newest_first=socket.context["logs_newest_first"],
                )
            except Exception as exc:
                socket.context = self._context(
                    console_msg=f"migration scan failed: {exc}",
                    console_class=console.Level.ERROR,
                    selected_thread_id=socket.context["selected_thread_id"],
                    logs_newest_first=socket.context["logs_newest_first"],
                )
            return
        if event == "start_thread":
            try:
                self.owner.state.start_background_task(task_id)
                socket.context = self._context(
                    console_msg=f"starting thread: {task_id}",
                    console_class=console.Level.RESTART,
                    selected_thread_id=task_id,
                    logs_newest_first=socket.context["logs_newest_first"],
                )
            except Exception as exc:
                socket.context = self._context(
                    console_msg=f"start failed: {exc}",
                    console_class=console.Level.ERROR,
                    selected_thread_id=socket.context["selected_thread_id"],
                    logs_newest_first=socket.context["logs_newest_first"],
                )
            return
        if event == "cancel_thread":
            try:
                self.owner.state.cancel_background_task(task_id)
                socket.context = self._context(
                    console_msg=f"cancelling thread: {task_id}",
                    console_class=console.Level.RESTART,
                    selected_thread_id=socket.context["selected_thread_id"],
                    logs_newest_first=socket.context["logs_newest_first"],
                )
            except Exception as exc:
                socket.context = self._context(
                    console_msg=f"cancel failed: {exc}",
                    console_class=console.Level.ERROR,
                    selected_thread_id=socket.context["selected_thread_id"],
                    logs_newest_first=socket.context["logs_newest_first"],
                )
            return
        if event == "invert_thread_log_order":
            socket.context = self._context(
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
                selected_thread_id=socket.context["selected_thread_id"],
                logs_newest_first=not socket.context["logs_newest_first"],
            )
            return
        if event == "toggle_thread":
            selected_thread_id = (
                "" if socket.context["selected_thread_id"] == task_id else task_id
            )
            socket.context = self._context(
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
                selected_thread_id=selected_thread_id,
                logs_newest_first=socket.context["logs_newest_first"],
            )
            return

    async def handle_info(
        self,
        event: InfoEvent,
        socket: ConnectedLiveViewSocket[ThreadsContext],
    ) -> None:
        if event.name != TOPIC_STATUS:
            return
        socket.context = self._context(
            console_msg=socket.context["console_msg"],
            console_class=socket.context["console_class"],
            selected_thread_id=socket.context["selected_thread_id"],
            logs_newest_first=socket.context["logs_newest_first"],
        )

    async def render(
        self,
        assigns: ThreadsContext,
        meta: Any,
    ) -> RenderedContent:
        return LiveRender(_load_template("threads_live.html"), assigns, meta)

    def _context(
        self,
        console_msg: str = "",
        console_class: str = "",
        selected_thread_id: str = "",
        logs_newest_first: bool = True,
    ) -> ThreadsContext:
        status = self.owner.state.status()
        tasks = self._sort_tasks(status.get("background_tasks") or [])
        thread_items = [
            self._thread_item(
                task,
                selected_thread_id=selected_thread_id,
                logs_newest_first=logs_newest_first,
            )
            for task in tasks
        ]
        base = self._base_context(console_msg, console_class)
        config = self.owner.config
        return cast(
            ThreadsContext,
            {
                **base,
                "has_threads": bool(thread_items),
                "selected_thread_id": selected_thread_id,
                "logs_newest_first": logs_newest_first,
                "thread_items": thread_items,
                "real_debrid_enabled": bool(
                    getattr(config, "real_debrid_enabled", False)
                    and (getattr(config, "real_debrid_token", "") or getattr(config, "token", ""))
                ),
                "torbox_enabled": bool(
                    getattr(config, "torbox_enabled", False)
                    and getattr(config, "torbox_token", "")
                ),
            },
        )

    @staticmethod
    def _sort_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def key(task: dict[str, Any]) -> tuple[int, int, str]:
            timestamp = str(
                task.get("started_at")
                or task.get("finished_at")
                or ""
            )
            manual_rank = 0 if task.get("status") == "pending" else 1
            # Tasks without a timestamp sort last; otherwise newest first.
            return (
                manual_rank,
                0 if timestamp else 1,
                _reverse_sort_text(timestamp),
            )

        return sorted(tasks, key=key)

    @staticmethod
    def _thread_item(
        task: dict[str, Any],
        *,
        selected_thread_id: str = "",
        logs_newest_first: bool = True,
    ) -> ThreadItem:
        task_id = str(task.get("id") or "")
        raw_status = str(task.get("status") or "")
        status: TaskStatus = (
            cast(TaskStatus, raw_status)
            if raw_status in _TASK_STATUSES
            else "failed"
        )
        logs = [
            _format_log_item(log)
            for log in task.get("logs") or []
            if isinstance(log, dict)
        ]
        if logs_newest_first:
            logs.reverse()
        worst_log_level = _worst_log_level(logs)
        show_log_severity = bool(status == "complete" and worst_log_level)
        raw_label = str(task.get("label") or "-")
        label_parts = raw_label.split(": ", 1)
        label = label_parts[0]
        detail = label_parts[1] if len(label_parts) > 1 else ""
        return {
            "id": task_id,
            "short_id": task_id[:8],
            "kind": str(task.get("kind") or "-"),
            "kind_class": f"thread-kind-{task.get('kind')}" if task.get("kind") else "comment",
            "label": label,
            "detail": detail,
            "status": status,
            "status_class": _task_status_class(status),
            "row_class": "thread-row-pending" if status == "pending" else "",
            "started_at": str(task.get("started_at") or "-"),
            "finished_at": str(task.get("finished_at") or "-"),
            "error": str(task.get("error") or ""),
            "cancellable": bool(task.get("cancellable")),
            "startable": bool(task.get("startable")),
            "expanded": bool(task_id and task_id == selected_thread_id),
            "logs": logs,
            "log_order_label": "NEWEST" if logs_newest_first else "OLDEST",
            "log_severity_class": (
                f"thread-log-severity-{worst_log_level}"
                if worst_log_level
                else ""
            ),
            "log_severity_title": (
                f"worst task log level: {worst_log_level}"
                if worst_log_level
                else ""
            ),
            "show_log_severity": show_log_severity,
            "status_group_title": (
                f"[{status}] · worst log: {worst_log_level}"
                if show_log_severity
                else f"[{status}]"
            ),
        }


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

    async def handle_event(  # noqa: C901
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
        if event == "toggle_favorite":
            section = (payload or {}).get("section", "")
            focus_section = ""
            if section:
                now_favorite = self.owner.state.toggle_config_favorite(section)
                # Only scroll-follow when a section was favorited (moved up).
                focus_section = section if now_favorite else ""
            socket.context = self._context(
                is_editing=True,
                draft_payload=socket.context["draft_payload"],
                language_query=socket.context["language_query"],
                console_msg=socket.context["console_msg"],
                console_class=socket.context["console_class"],
                focus_section=focus_section,
            )
            return
        if event in {"filter_languages", "preview"}:
            form_payload = payload or {}
            socket.context = self._context(
                is_editing=True,
                draft_payload=form_payload,
                language_query=form_payload.get(
                    "language_query", language_query
                ),
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
                console_class = console.Level.ERROR
            elif not self.owner.trigger_language_refresh(force=True):
                console_msg = "language refresh already in progress"
                console_class = console.Level.RESTART
            else:
                console_msg = "refreshing languages from opensubtitles..."
                console_class = console.Level.SUCCESS
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
                console_class=console.Level.SUCCESS,
            )
            if result["restart_required"]:
                console.log(
                    socket.context,
                    "defaults restored. restart still required for "
                    + ", ".join(result["restart_required_fields"]),
                    console.Level.RESTART,
                )
            return
        if event != "save":
            return

        overrides = _config_overrides_from_payload(payload or {})
        result = self.owner.persist_overrides(overrides)
        if result["restart_required"]:
            console_msg = "saved. restart required for " + ", ".join(
                result["restart_required_fields"]
            )
            console_class = console.Level.RESTART
        else:
            console_msg = "saved."
            console_class = console.Level.SUCCESS
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
        if (
            event.name == TOPIC_CONFIG
            and isinstance(event.payload, dict)
            and event.payload.get("languages_refresh_complete")
        ):
            console_msg = "languages updated"
            console_class = console.Level.SUCCESS
        else:
            console_msg = socket.context["console_msg"]
            console_class = socket.context["console_class"]
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
        focus_section: str = "",
    ) -> ConfigContext:
        base = self._base_context(console_msg, console_class)
        effective_config = getattr(
            self.owner, "saved_config", self.owner.config
        )
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
            _config_overrides_from_payload(draft_payload)
            if is_editing and draft_payload
            else {},
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
                "section_states": _section_states(
                    self.owner.state.config_favorites
                ),
                "focus_section": _section_slug(focus_section)
                if focus_section
                else "",
                "subtitles_credentials_ready": subtitles_credentials_ready,
                "values": values,
            },
        )


def _page_from_path(path: str) -> PageName:
    parsed = urlparse(path)
    page = parsed.path.strip("/") or "cache"
    if page in PAGE_NAMES:
        return cast(PageName, page)
    return "cache"


def _page_path(page: PageName) -> str:
    return f"/{page}"


def _reverse_sort_text(value: str) -> str:
    return "".join(chr(255 - ord(char)) for char in value)


def _task_status_class(status: str) -> str:
    return console.Level.task_status_class(status)


def _worst_log_level(logs: list[LogItem]) -> str:
    worst = ""
    worst_score = -1
    for log in logs:
        level = log["level"]
        score = _LOG_LEVEL_SEVERITY.get(level, -1)
        if score > worst_score:
            worst = level
            worst_score = score
    return worst


def _split_task_link_message(
    message: str,
    task_id: str,
) -> tuple[str, str, str]:
    if not task_id:
        return message, "", ""
    index = message.find(task_id)
    if index < 0:
        return f"{message} ", task_id, ""
    return (
        message[:index],
        message[index:index + len(task_id)],
        message[index + len(task_id):],
    )


def _display_log_timestamp(timestamp: str) -> str:
    if "T" not in timestamp or len(timestamp) < 19:
        return timestamp
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp[11:19]
    return parsed.astimezone().strftime("%H:%M:%S")


def _format_log_item(log: dict[str, Any]) -> LogItem:
    display_timestamp = _display_log_timestamp(str(log.get("timestamp", "")))
    level = str(log.get("level", "info")).lower()
    level_label = f"[{level.upper()}]"
    source = "buzz-curator" if log.get("source") == "curator" else "buzz-dav"
    message = str(log.get("message", ""))
    count = int(log.get("count", 1))
    message = f"{message} ({count})" if count > 1 else message
    link_to_task_id = str(log.get("link_to_task_id") or "")
    message_prefix, task_link_text, message_suffix = _split_task_link_message(
        message,
        link_to_task_id,
    )
    copy_text = f"{source} {display_timestamp} {level_label} {message}"
    return {
        "copy_text": copy_text,
        "level": level,
        "level_class": f"log-level-{level}",
        "level_label": level_label,
        "message": message,
        "message_prefix": message_prefix,
        "message_suffix": message_suffix,
        "source": source,
        "timestamp": display_timestamp,
        "link_to_task_id": link_to_task_id,
        "task_link_text": task_link_text,
        "task_status": "",
        "task_status_class": "",
    }


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
        self.threads_view = ThreadsLiveView(owner)
        self.config_view = ConfigLiveView(owner)

    def _nav_for_page(self, page: PageName) -> PageNav:
        return {
            "archive_count": len(self.owner.state.archive),
            "cache_active": page == "cache",
            "archive_active": page == "archive",
            "logs_active": page == "logs",
            "threads_active": page == "threads",
            "config_active": page == "config",
            "log_count": self.owner.log_count(),
            "log_level": self._highest_log_level(),
            "thread_count": len(self.owner.state.background_tasks.snapshot()),
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

    def _page_context(
        self, page: PageName, context: ShellContext
    ) -> PageContext:
        if page == "archive":
            return context["archive"]
        if page == "logs":
            return context["logs"]
        if page == "threads":
            return context["threads"]
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
        elif page == "threads":
            context["threads"] = cast(ThreadsContext, page_context)
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
        task_id: str = "",
    ) -> None:
        active_page = page or context["active_page"]
        if task_id:
            context["threads"] = self.threads_view._context(
                selected_thread_id=task_id,
                logs_newest_first=context["threads"]["logs_newest_first"],
            )
        active_context = self._page_context(active_page, context)
        base = self._base_context_for_page(
            active_page,
            active_context["console_msg"]
            if console_msg is None
            else console_msg,
            active_context["console_class"]
            if console_class is None
            else console_class,
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

    def _render_page_body(
        self, page: PageName, context: ShellContext
    ) -> Markup:
        if page == "archive":
            rendered = _load_template("archive_live.html").render(
                context["archive"], None
            )
        elif page == "logs":
            rendered = _load_template("logs_live.html").render(
                context["logs"], None
            )
        elif page == "threads":
            rendered = _load_template("threads_live.html").render(
                context["threads"], None
            )
        elif page == "config":
            rendered = _load_template("config_live.html").render(
                context["config"], None
            )
        else:
            rendered = _load_template("cache_live.html").render(
                context["cache"], None
            )
        return _extract_page_body(rendered)

    def _context(self, active_page: PageName = "cache", task_id: str = "") -> ShellContext:
        cache = self.cache_view._context()
        archive = self.archive_view._context()
        logs = self.logs_view._context()
        threads = self.threads_view._context(selected_thread_id=task_id)
        config = self.config_view._context()
        active_context = {
            "archive": archive,
            "cache": cache,
            "config": config,
            "logs": logs,
            "threads": threads,
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
                "threads": threads,
                "page_content": Markup(""),
                "real_debrid_enabled": threads["real_debrid_enabled"],
                "torbox_enabled": threads["torbox_enabled"],
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
            await socket.subscribe(TOPIC_LOGS)

    async def handle_params(
        self,
        url: ParseResult,
        socket: LiveViewSocket[ShellContext],
    ) -> None:
        page = _page_from_path(url.path)
        query = parse_qs(url.query)
        task_id = query.get("task_id", [""])[0]

        if not getattr(socket, "context", None):
            socket.context = self._context(page, task_id=task_id)
            return
        self._refresh_shared_context(socket.context, page=page, task_id=task_id)
        socket.live_title = {
            "archive": "buzz: archive",
            "cache": "buzz: cache",
            "config": "buzz: config",
            "logs": "buzz: system logs",
            "threads": "buzz: threads",
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
        task_id: str = "",
        language_query: str = "",
        id: str = "",
    ) -> None:
        if event == EVENT_NAVIGATE:
            parsed = urlparse(to)
            page = _page_from_path(parsed.path)
            query = parse_qs(parsed.query)
            query_task_id = query.get("task_id", [""])[0]

            self._refresh_shared_context(socket.context, page=page, task_id=query_task_id)
            await socket.push_patch(to)
            return

        if event == "clear_console":
            page = socket.context["active_page"]
            page_context = self._page_context(page, socket.context)
            page_context["console_msg"] = ""
            page_context["console_class"] = ""
            self._refresh_shared_context(socket.context, page=page)
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
            elif page == "threads":
                await self.threads_view.handle_event(
                    event,
                    cast(ConnectedLiveViewSocket[ThreadsContext], socket),
                    to=to,
                    task_id=task_id,
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
                    id=id,
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
        if event.name not in {
            TOPIC_ARCHIVE,
            TOPIC_CONFIG,
            TOPIC_LOGS,
            TOPIC_STATUS,
        }:
            return
        context = socket.context
        context["cache"] = self.cache_view._context(
            console_msg=context["cache"]["console_msg"],
            console_class=context["cache"]["console_class"],
            confirm_delete_id=context["cache"]["confirm_delete_id"],
            analysis_results=context["cache"]["analysis_results"],
            analysis_error=context["cache"]["analysis_error"],
            analyzing=context["cache"]["analyzing"],
            caching=context["cache"]["caching"],
            sort_col=context["cache"]["sort_col"],
            sort_dir=context["cache"]["sort_dir"],
            expanded_id=context["cache"]["expanded_id"],
        )
        context["archive"] = self.archive_view._context(
            console_msg=context["archive"]["console_msg"],
            console_class=context["archive"]["console_class"],
            confirm_delete_hash=context["archive"]["confirm_delete_hash"],
            confirm_restore_hash=context["archive"]["confirm_restore_hash"],
            sort_col=context["archive"]["sort_col"],
            sort_dir=context["archive"]["sort_dir"],
        )
        context["logs"] = self.logs_view._context()
        context["threads"] = self.threads_view._context(
            console_msg=context["threads"]["console_msg"],
            console_class=context["threads"]["console_class"],
            selected_thread_id=context["threads"]["selected_thread_id"],
            logs_newest_first=context["threads"]["logs_newest_first"],
        )
        if (
            event.name == TOPIC_CONFIG
            and isinstance(event.payload, dict)
            and event.payload.get("languages_refresh_complete")
        ):
            config_console_msg = "languages updated"
            config_console_class = console.Level.SUCCESS
        else:
            config_console_msg = context["config"]["console_msg"]
            config_console_class = context["config"]["console_class"]
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
        if (
            term
            and term not in normalized_name
            and term not in normalized_code
        ):
            continue
        rows.append({"checked": code in selected, "code": code, "name": name})
    return rows


def _config_values(
    config: Any,
    payload: dict[str, Any] | None = None,
) -> ConfigValues:
    draft = _config_overrides_from_payload(payload or {})
    effective = deep_merge(to_nested_dict(config), draft)
    subtitles = effective["subtitles"]
    subtitle_filters = subtitles["filters"]
    scan_probe = effective["media_server"]["scan_probe"]
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
        "provider_priority": "\n".join(effective["provider"]["priority"]),
        "provider_poll_interval_secs": effective["provider"][
            "poll_interval_secs"
        ],
        "real_debrid_enabled": effective["provider"]["real_debrid"][
            "enabled"
        ],
        "real_debrid_token": effective["provider"]["real_debrid"]["token"],
        "ui_poll_interval_secs": effective["ui"]["poll_interval_secs"],
        "port": effective["server"]["port"],
        "plex_token": effective["media_server"]["plex"]["token"],
        "plex_url": effective["media_server"]["plex"]["url"],
        "request_timeout_secs": effective["request_timeout_secs"],
        "rd_update_delay_secs": effective["hooks"]["rd_update_delay_secs"],
        "scan_probe_concurrency": scan_probe["concurrency"],
        "scan_probe_enabled": scan_probe["enabled"],
        "scan_probe_max_attempts": scan_probe["max_attempts"],
        "scan_probe_min_files": scan_probe["min_files"],
        "scan_probe_read_bytes": scan_probe["read_bytes"],
        "scan_probe_retry_delay_secs": scan_probe["retry_delay_secs"],
        "scan_probe_sample_ratio_percent": scan_probe["sample_ratio_percent"],
        "search_delay_secs": subtitles["search_delay_secs"],
        "selected_languages": subtitles["languages"],
        "strategy": subtitles["strategy"],
        "subtitles_enabled": subtitles["enabled"],
        "torbox_enabled": effective["provider"]["torbox"]["enabled"],
        "torbox_token": effective["provider"]["torbox"]["token"],
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
        line.strip() for line in str(patterns[0]).splitlines() if line.strip()
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
        "provider.priority",
        "provider.real_debrid.token",
        "provider.torbox.token",
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
    _set_nested_value(
        overrides, FIELD_ANIME_PATTERNS, _extract_pattern_lines(patterns)
    )
    priority = _extract_pattern_lines(normalized.get("provider.priority", [""]))
    if priority:
        _set_nested_value(overrides, "provider.priority", priority)

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


def _section_states(
    favorites: set[str],
) -> dict[str, ConfigSectionState]:
    """Per-section favorite state keyed by slug for the edit-form template.

    Favorited sections get order 0 (pinned to the top via CSS flex order);
    everything else keeps order 1, and flexbox preserves source order within a
    tie.
    """
    states: dict[str, ConfigSectionState] = {}
    for section in _CONFIG_SECTIONS:
        is_favorite = section in favorites
        states[_section_slug(section)] = {
            "favorite": "true" if is_favorite else "false",
            "order": 0 if is_favorite else 1,
            "star_class": "config-fav-star is-favorite"
            if is_favorite
            else "config-fav-star",
            "star_icon": "fa-solid fa-star"
            if is_favorite
            else "fa-regular fa-star",
        }
    return states


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
        diff_fields(
            effective,
            deep_merge(effective, draft_overrides),
            _CONFIG_TRACKED_FIELDS,
        )
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
            "baseline_value": _render_default_comment_value(
                get_nested_value(baseline, path)
            ),
        }
    return states


def _field_alias(path: str) -> str:
    return path.replace(".", "_")


def _config_baseline_values(
    config: Any, baseline: dict[str, Any]
) -> dict[str, Any]:
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
            lines.append(
                f"{prefix}# Overriden via UI. Default: {default_text}"
            )
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
