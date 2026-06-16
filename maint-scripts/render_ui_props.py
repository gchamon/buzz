#!/usr/bin/env python3
"""Render deterministic Buzz UI props for README assets."""

from __future__ import annotations

import argparse
import base64
import contextlib
import html
import json
import logging
import math
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.request import urlopen

import uvicorn
import yaml
from PIL import Image, ImageFilter, ImageStat
from playwright.sync_api import Page, ViewportSize, sync_playwright

from buzz.core.events import registry
from buzz.core.state import BackgroundTask
from buzz.dav_app import DavApp
from buzz.models import DavConfig, TaskStatus

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PROPS_CONFIG = SCRIPT_DIR / "props.yml"
LOGGER = logging.getLogger("render_ui_props")
TOP_BAR_HEIGHT = 48
TASK_STATUSES: set[str] = {
    "pending",
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "complete",
    "failed",
    "aborted",
}


class FakeProvider:
    """Provider stub used only by the screenshot app."""

    def list_torrents(self) -> list[dict[str, Any]]:
        return []

    def get_torrent(self, torrent_id: str) -> dict[str, Any]:
        return {"id": torrent_id, "filename": torrent_id}

    def fetch_details(
        self,
        torrent_ids: list[str],
        on_progress: Any | None = None,
    ) -> dict[str, dict[str, Any]]:
        total = len(torrent_ids)
        for index, torrent_id in enumerate(torrent_ids, 1):
            if on_progress is not None:
                on_progress(torrent_id, index, total)
        return {torrent_id: self.get_torrent(torrent_id) for torrent_id in torrent_ids}


def main() -> None:
    """Render all configured UI props."""
    args = _parse_args()
    _configure_logging(args.verbose)
    LOGGER.info("loading props config: %s", args.config)
    props = _load_props_config(args.config)
    output_dir = _output_dir(props)
    LOGGER.info("writing UI props to: %s", output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="buzz-readme-ui-") as tmp:
        temp_dir = Path(tmp)
        LOGGER.debug("using temporary workspace: %s", temp_dir)
        background_source = (
            args.background_source
            or str(_props_get(props, ("background", "source"), ""))
        )
        if background_source:
            LOGGER.info("extracting background palette from: %s", background_source)
        else:
            LOGGER.info("using configured default background palette")
        palette = _background_palette(props, background_source, temp_dir)
        background_size = _background_size(props)
        LOGGER.info(
            "rendering Gaussian background: %sx%s palette=%s",
            background_size[0],
            background_size[1],
            palette,
        )
        background = _render_background(props, palette, background_size)
        background.save(output_dir / "background.png")
        LOGGER.info("wrote background: %s", output_dir / "background.png")
        hyprland_style = _load_hyprland_style(props)
        LOGGER.info(
            "loaded frame style: border=%s->%s radius=%s source=%s",
            hyprland_style["active_border_end"],
            hyprland_style["active_border_start"],
            hyprland_style["rounding"],
            hyprland_style.get("source"),
        )
        if args.background_only:
            LOGGER.info("background-only mode enabled, skipping UI capture")
            _write_manifest(props, palette, hyprland_style, background_only=True)
            return
        state_dir = Path(tmp)
        LOGGER.info("building seeded Buzz app")
        config = _write_config(state_dir)
        with _patched_network_clients():
            app = DavApp(config)
            _seed_app(app, props)
            LOGGER.info("starting local screenshot server")
            server, thread, port = _start_server(app)
            LOGGER.info("screenshot server listening on http://127.0.0.1:%s", port)
            try:
                raw_dir = temp_dir / "raw"
                raw_dir.mkdir()
                LOGGER.info("capturing and composing framed UI props")
                _capture_and_compose(
                    app,
                    props,
                    port,
                    raw_dir,
                    output_dir / "background.png",
                    hyprland_style,
                    output_dir,
                )
                _write_manifest(props, palette, hyprland_style, background_only=False)
            finally:
                LOGGER.info("stopping screenshot server")
                server.should_exit = True
                thread.join(timeout=5)
                app.state.close()
    LOGGER.info("UI prop rendering complete")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render deterministic Buzz UI props.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_PROPS_CONFIG),
        help="YAML props config path.",
    )
    parser.add_argument(
        "--background-source",
        default="",
        help=(
            "Optional local image path or URL to sample for the generated "
            "Gaussian background. Defaults to the extracted Arch wallpaper "
            "palette."
        ),
    )
    parser.add_argument(
        "--background-only",
        action="store_true",
        help="Only generate docs/assets/ui/background.png and manifest.json.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show debug logs while rendering props.",
    )
    return parser.parse_args()


def _configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOGGER.propagate = False


def _load_props_config(path: str) -> dict[str, Any]:
    with open(Path(path).expanduser(), encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"props config must be a mapping: {path}")
    return raw


def _props_get(
    props: dict[str, Any],
    path: tuple[str, ...],
    default: Any,
) -> Any:
    current: Any = props
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _output_dir(props: dict[str, Any]) -> Path:
    raw = str(_props_get(props, ("output_dir",), "docs/assets/ui"))
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


def _viewport(
    props: dict[str, Any],
    hyprland_style: dict[str, Any] | None = None,
) -> ViewportSize:
    border_size = int((hyprland_style or _hyprland_fallback_style(props))["border_size"])
    target_width, target_height = _screenshot_target_size(
        props,
        _background_size(props),
        border_size,
    )
    raw_width = _props_get(props, ("viewport", "width"), "auto")
    raw_height = _props_get(props, ("viewport", "height"), "auto")
    width = (
        target_width
        if str(raw_width).strip().lower() == "auto"
        else int(raw_width)
    )
    if str(raw_height).strip().lower() == "auto":
        height = round(width * target_height / target_width)
    else:
        height = int(raw_height)
    return {
        "width": width,
        "height": height,
    }


def _routes(props: dict[str, Any]) -> tuple[str, ...]:
    raw = _props_get(props, ("routes",), ["cache", "archive", "logs", "threads", "config"])
    if not isinstance(raw, list):
        return ("cache", "archive", "logs", "threads", "config")
    return tuple(str(route) for route in raw)


def _background_size(props: dict[str, Any]) -> tuple[int, int]:
    return (
        int(_props_get(props, ("background", "size", "width"), 1600)),
        int(_props_get(props, ("background", "size", "height"), 1000)),
    )


def _write_config(state_dir: Path) -> DavConfig:
    config_path = state_dir / "buzz.yml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "provider": {
                    "active": "real_debrid",
                    "priority": ["real_debrid", "torbox"],
                    "real_debrid": {
                        "enabled": True,
                        "token": "readme-real-debrid-token",
                    },
                    "torbox": {
                        "enabled": True,
                        "token": "readme-torbox-token",
                    },
                    "connection_concurrency": 6,
                    "poll_interval_secs": 12,
                },
                "server": {"bind": "127.0.0.1", "port": 9999},
                "state_dir": str(state_dir),
                "hooks": {
                    "on_library_change": "",
                    "curator_url": "",
                    "rd_update_delay_secs": 15,
                },
                "compat": {
                    "enable_all_dir": True,
                    "enable_unplayable_dir": True,
                },
                "directories": {
                    "anime": {"patterns": [r"\b[0-9A-F]{8}\b"]},
                },
                "request_timeout_secs": 30,
                "version_label": "buzz/readme",
                "ui": {"poll_interval_secs": 3},
                "logging": {"verbose": True, "max_entries": 200},
                "media_server": {
                    "kind": "jellyfin",
                    "trigger_lib_scan": True,
                    "jellyfin": {
                        "url": "http://jellyfin.local:8096",
                        "api_key": "readme-jellyfin-key",
                        "scan_task_id": "readme-scan-task",
                    },
                    "plex": {
                        "url": "http://plex.local:32400",
                        "token": "readme-plex-token",
                    },
                    "library_map": {
                        "movies": "Movies",
                        "shows": "TV Shows",
                        "anime": "Anime",
                    },
                    "scan_probe": {
                        "enabled": True,
                        "sample_ratio_percent": 25,
                        "min_files": 2,
                        "max_attempts": 4,
                        "read_bytes": 262144,
                        "retry_delay_secs": 3,
                        "concurrency": 2,
                    },
                },
                "subtitles": {
                    "enabled": True,
                    "fetch_on_resync": True,
                    "opensubtitles": {
                        "api_key": "readme-opensubtitles-key",
                        "username": "readme-user",
                        "password": "readme-password",
                    },
                    "languages": ["en", "pt-br", "es"],
                    "strategy": "best-rated",
                    "filters": {
                        "hearing_impaired": "prefer",
                        "exclude_ai": True,
                        "exclude_machine": True,
                    },
                    "search_delay_secs": 1,
                    "download_delay_secs": 2,
                },
                "tls": {"cert_path": "", "key_path": ""},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    overrides_path = state_dir / "buzz.overrides.yml"
    overrides_path.write_text(
        yaml.safe_dump(
            {
                "logging": {"verbose": True},
                "media_server": {"trigger_lib_scan": True},
                "subtitles": {"strategy": "best-rated"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return DavConfig.load(str(config_path))


@contextlib.contextmanager
def _patched_network_clients():
    with (
        patch("buzz.dav_app.DavApp._build_provider_client", return_value=None),
        patch("buzz.dav_app._fetch_opensubtitles_languages", return_value=[]),
    ):
        yield


def _seed_app(app: DavApp, props: dict[str, Any]) -> None:
    registry.clear(listeners=True)
    registry.default_source = "dav"
    global_state = _ui_view_mapping(props, "global")
    config_state = _ui_view_mapping(props, "config")
    providers = _providers_from_props(global_state)
    app._curator_log_level = str(global_state.get("curator_log_level", "error"))
    app.clients = {provider: FakeProvider() for provider in providers}
    app.client = app.clients[providers[0]]
    app.state.clients = app.clients
    app.state.client = app.client
    _apply_global_ui_state(app, global_state)
    app.languages_refreshing = bool(config_state.get("languages_refreshing", False))
    app.opensubtitles_languages = _language_rows_from_props(config_state)
    app.state.config_favorites = {
        str(item)
        for item in _as_list(
            config_state.get("favorites", ["provider", "media_server", "subtitles"])
        )
    }
    app.state.cache = _cache_entries(props)
    _seed_cache_overrides(app, props)
    app.state.archive = _archive_entries(props)
    _seed_provider_links(app, props)
    _seed_tasks(app, props)
    _seed_logs(props)


def _apply_global_ui_state(app: DavApp, global_state: dict[str, Any]) -> None:
    app._curator_log_level = str(global_state.get("curator_log_level", "error"))
    nav_log_level = str(global_state.get("nav_log_level", "") or "").strip().lower()
    app._nav_log_level_override = nav_log_level
    app.state.snapshot_loaded = bool(global_state.get("snapshot_loaded", True))
    app.state.last_error = str(global_state.get("last_error", ""))
    app.state.provider_degraded = bool(global_state.get("provider_degraded", False))
    app.state.last_sync_at = str(global_state.get("last_sync_at", "2026-06-15T12:00:00Z"))
    app.state.sync_in_progress = bool(global_state.get("sync_in_progress", False))
    app.state.hook_phase = str(global_state.get("hook_phase", "idle"))
    app.state.hook_pending_paths = [
        str(path) for path in _as_list(global_state.get("hook_pending_paths", []))
    ]
    app.state.hook_active_paths = [
        str(path) for path in _as_list(global_state.get("hook_active_paths", []))
    ]
    app.state.hook_in_progress = bool(global_state.get("hook_in_progress", False))


def _route_global_ui_state(props: dict[str, Any], route: str) -> dict[str, Any]:
    return _ui_view_mapping(props, "global") | _ui_view_route_mapping(
        props,
        route,
        "global",
    )


def _providers_from_props(global_state: dict[str, Any]) -> list[str]:
    providers = [str(item) for item in _as_list(global_state.get("providers", []))]
    return providers or ["real_debrid", "torbox"]


def _language_rows_from_props(config_state: dict[str, Any]) -> list[tuple[str, str]]:
    raw_languages = _as_list(
        config_state.get(
            "languages",
            [
                {"code": "en", "name": "English"},
                {"code": "pt-br", "name": "Portuguese (Brazil)"},
                {"code": "es", "name": "Spanish"},
                {"code": "ja", "name": "Japanese"},
                {"code": "fr", "name": "French"},
            ],
        )
    )
    languages: list[tuple[str, str]] = []
    for item in raw_languages:
        if isinstance(item, dict):
            languages.append((str(item.get("code", "")), str(item.get("name", ""))))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            languages.append((str(item[0]), str(item[1])))
    return [(code, name) for code, name in languages if code and name]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _ui_views(props: dict[str, Any]) -> dict[str, Any]:
    raw = _props_get(props, ("ui_views",), None)
    if raw is None:
        raw = _props_get(props, ("ui_state",), {})
    return raw if isinstance(raw, dict) else {}


def _ui_view_mapping(props: dict[str, Any], key: str) -> dict[str, Any]:
    raw = _ui_views(props).get(key, {})
    return raw if isinstance(raw, dict) else {}


def _ui_view_route_mapping(
    props: dict[str, Any],
    route: str,
    key: str,
) -> dict[str, Any]:
    route_view = _ui_view_mapping(props, route)
    raw = route_view.get(key, {})
    return raw if isinstance(raw, dict) else {}


def _ui_view_items(props: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = _ui_views(props).get(key, [])
    if isinstance(raw, dict):
        raw = raw.get("entries", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _ui_view_item(
    props: dict[str, Any],
    key: str,
    index: int,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    items = _ui_view_items(props, key)
    if index >= len(items):
        return defaults
    return defaults | items[index]


def _task_status(value: Any) -> TaskStatus:
    status = str(value)
    if status not in TASK_STATUSES:
        raise ValueError(f"unsupported screenshot task status: {status}")
    return cast(TaskStatus, status)


def _cache_file(item: dict[str, Any], index: int, torrent_name: str) -> dict[str, Any]:
    path = str(
        item.get("path")
        or f"/{torrent_name}/{torrent_name.replace(' ', '.')}.{index}.mkv"
    )
    return {
        "id": str(item.get("id", index)),
        "path": path,
        "bytes": int(item.get("bytes", 1_073_741_824)),
        "selected": bool(item.get("selected", False)),
    }


def _cache_entry(defaults: dict[str, Any], item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    entry = defaults | item
    name = str(entry["name"])
    files = [
        _cache_file(file_item, index, name)
        for index, file_item in enumerate(_as_list(entry.get("files")), 1)
        if isinstance(file_item, dict)
    ]
    if not files:
        selected_count = int(entry.get("selected_files", 0))
        file_count = max(int(entry.get("file_count", 1)), selected_count, 1)
        files = [
            _cache_file(
                {
                    "id": str(index),
                    "path": f"/{name}/{name.replace(' ', '.')}.{index}.mkv",
                    "bytes": int(entry.get("bytes", 1_073_741_824)) // file_count,
                    "selected": index <= selected_count,
                },
                index,
                name,
            )
            for index in range(1, file_count + 1)
        ]
    cache_key = str(entry["cache_key"])
    info = {
        "id": str(entry.get("id", cache_key.split(":", 1)[-1])),
        "provider_torrent_id": str(entry.get("provider_torrent_id", entry.get("id", cache_key))),
        "hash": str(entry["hash"]),
        "filename": name,
        "status": str(entry.get("status", "downloaded")),
        "progress": int(entry.get("progress", 100)),
        "bytes": int(entry.get("bytes", sum(int(file_item["bytes"]) for file_item in files))),
        "links": [str(link) for link in _as_list(entry.get("links"))],
        "ended": str(entry.get("ended", "")),
        "files": files,
    }
    provider = str(entry.get("provider", "real_debrid"))
    if provider != "real_debrid":
        info["provider"] = provider
    if entry.get("original_name"):
        info["original_filename"] = str(entry["original_name"])
    return cache_key, {"signature": {}, "info": info}


def _default_cache_entries() -> list[dict[str, Any]]:
    return [
        {
            "cache_key": "rd-feature-001",
            "id": "rd-feature-001",
            "provider": "real_debrid",
            "provider_torrent_id": "2K3ZTCX5PXXC4",
            "hash": "aaaa1111",
            "name": "Night of the Living Dead (1968)",
            "original_name": "Night.of.the.Living.Dead.1968.1080p.BluRay.x264",
            "status": "downloaded",
            "progress": 100,
            "bytes": 18_253_611_008,
            "links": ["https://example.invalid/night.mkv"],
            "ended": "2026-06-15T11:54:00Z",
            "files": [
                {
                    "id": "1",
                    "path": "/Night of the Living Dead (1968)/Night.of.the.Living.Dead.1968.mkv",
                    "bytes": 18_253_611_008,
                    "selected": True,
                },
                {
                    "id": "2",
                    "path": "/Night of the Living Dead (1968)/sample.mkv",
                    "bytes": 188_743_680,
                    "selected": False,
                },
            ],
        },
        {
            "cache_key": "torbox:anime-042",
            "id": "anime-042",
            "provider": "torbox",
            "provider_torrent_id": "37310778",
            "hash": "bbbb2222",
            "name": "His Girl Friday (1940)",
            "status": "downloaded",
            "progress": 100,
            "bytes": 9_126_805_504,
            "links": ["https://example.invalid/his-girl-friday.mkv"],
            "ended": "2026-06-15T10:22:00Z",
            "files": [
                {
                    "id": "1",
                    "path": "/His Girl Friday/His.Girl.Friday.1940.mkv",
                    "bytes": 9_126_805_504,
                    "selected": False,
                }
            ],
        },
        {
            "cache_key": "rd-pending-003",
            "id": "rd-pending-003",
            "provider": "real_debrid",
            "provider_torrent_id": "2OTNC4A4JD6IU",
            "hash": "cccc3333",
            "name": "Plan 9 from Outer Space (1959)",
            "status": "downloaded",
            "progress": 100,
            "bytes": 4_831_838_208,
            "links": [],
            "ended": "2026-06-14T22:15:00Z",
            "category_override": "movies",
            "identity_override": {
                "kind": "movie",
                "title": "Plan 9 from Outer Space",
                "year": "1959",
                "provider_ids": {"imdbid": "tt0052077"},
            },
            "files": [
                {
                    "id": "1",
                    "path": "/Plan 9 from Outer Space/Plan.9.1959.mkv",
                    "bytes": 4_831_838_208,
                    "selected": True,
                }
            ],
        },
        {
            "cache_key": "torbox:sync-884",
            "id": "sync-884",
            "provider": "torbox",
            "provider_torrent_id": "37310781",
            "hash": "dddd4444",
            "name": "Charade (1963)",
            "status": "downloading",
            "progress": 64,
            "bytes": 7_516_192_768,
            "links": [],
            "ended": "",
            "files": [],
        },
    ]


def _cache_entries(props: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = _ui_view_items(props, "cache")
    defaults = _default_cache_entries()
    if not items:
        items = defaults
    entries: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        default = defaults[index] if index < len(defaults) else {}
        cache_key, entry = _cache_entry(default, item)
        entries[cache_key] = entry
    return entries


def _seed_cache_overrides(app: DavApp, props: dict[str, Any]) -> None:
    defaults = _default_cache_entries()
    items = _ui_view_items(props, "cache") or defaults
    for index, item in enumerate(items):
        entry = (defaults[index] if index < len(defaults) else {}) | item
        thash = str(entry.get("hash", "")).strip().lower()
        if not thash:
            continue
        category_override = entry.get("category_override")
        if category_override:
            app.state.category_overrides[thash] = str(category_override)
        identity_override = entry.get("identity_override")
        if isinstance(identity_override, dict):
            app.state.curator_title_overrides[thash] = identity_override
        elif entry.get("title_override"):
            app.state.curator_title_overrides[thash] = {
                "kind": str(entry.get("kind", "movie")),
                "title": str(entry["title_override"]),
                "year": str(entry.get("year", "")),
                "provider_ids": entry.get("provider_ids", {}),
            }


def _archive_entries(props: dict[str, Any]) -> dict[str, dict[str, Any]]:
    defaults = [
        {
            "hash": "arch1111",
            "name": "Metropolis (1927)",
            "bytes": 6_442_450_944,
            "files": [{"path": "/Metropolis.1927.mkv"}],
            "deleted_at": "2026-06-12T09:30:00Z",
            "magnet": "magnet:?xt=urn:btih:arch1111",
            "providers": [{"provider": "real_debrid", "provider_torrent_id": "2P9ZTCX5PXXC5"}],
        },
        {
            "hash": "arch2222",
            "name": "The Phantom Creeps (1965)",
            "bytes": 12_884_901_888,
            "files": [{"path": "/The.Phantom.Creeps.Part1.mkv"}, {"path": "/The.Phantom.Creeps.Part2.mkv"}],
            "deleted_at": "2026-06-10T18:05:00Z",
            "magnet": "magnet:?xt=urn:btih:arch2222",
            "providers": [{"provider": "torbox", "provider_torrent_id": "37310787"}],
        },
        {
            "hash": "arch3333",
            "name": "House on Haunted Hill (1959)",
            "bytes": 3_221_225_472,
            "files": [{"path": "/House.on.Haunted.Hill.1959.mkv"}],
            "deleted_at": "2026-06-08T14:45:00Z",
            "magnet": "magnet:?xt=urn:btih:arch3333",
            "providers": [
                {"provider": "real_debrid", "provider_torrent_id": "2T9ZTCX5PXXC6"},
                {"provider": "torbox", "provider_torrent_id": "37310789"},
            ],
        },
        {
            "hash": "arch4444",
            "name": "Detour (1963)",
            "bytes": 1_610_612_736,
            "files": [{"path": "/Detour.1963.mkv"}],
            "deleted_at": "2026-06-05T11:20:00Z",
            "magnet": "magnet:?xt=urn:btih:arch4444",
        },
    ]
    items = _ui_view_items(props, "archive") or defaults
    entries: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        entry = (defaults[index] if index < len(defaults) else {}) | item
        thash = str(entry["hash"])
        entries[thash] = {
            "hash": thash,
            "name": str(entry["name"]),
            "bytes": int(entry.get("bytes", 0)),
            "files": _as_list(entry.get("files")),
            "deleted_at": str(entry.get("deleted_at", "")),
            "magnet": str(entry.get("magnet", "")),
            "providers": _as_list(entry.get("providers")),
        }
    return entries


def _provider_link_rows(props: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for thash, entry in _archive_entries(props).items():
        for provider_entry in _as_list(entry.get("providers")):
            if not isinstance(provider_entry, dict):
                continue
            rows.append(
                (
                    str(provider_entry.get("provider", "")),
                    str(provider_entry.get("provider_torrent_id", "")),
                    thash,
                )
            )
    return [
        (provider, provider_torrent_id, thash)
        for provider, provider_torrent_id, thash in rows
        if provider and provider_torrent_id
    ]


def _seed_provider_links(app: DavApp, props: dict[str, Any]) -> None:
    rows = _provider_link_rows(props)
    with app.state.conn:
        for thash, entry in app.state.archive.items():
            app.state.conn.execute(
                "INSERT INTO library_entries "
                "(hash, name, bytes, files_json, magnet, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(hash) DO UPDATE SET "
                "name = excluded.name, "
                "bytes = excluded.bytes, "
                "files_json = excluded.files_json, "
                "magnet = excluded.magnet, "
                "updated_at = excluded.updated_at",
                (
                    thash,
                    str(entry.get("name") or "Archived item"),
                    int(entry.get("bytes") or 0),
                    json.dumps(entry.get("files") or []),
                    entry.get("magnet"),
                    "2026-06-15T12:00:00Z",
                ),
            )
        for provider, provider_torrent_id, thash in rows:
            app.state.conn.execute(
                "INSERT OR REPLACE INTO provider_links "
                "(provider, provider_torrent_id, hash, status, progress, "
                "info_json, signature_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    provider_torrent_id,
                    thash,
                    "downloaded",
                    100,
                    "{}",
                    "{}",
                    "2026-06-15T12:00:00Z",
                ),
            )


def _seed_tasks(app: DavApp, props: dict[str, Any]) -> None:
    defaults = [
        {
            "id": "cache-queue-01",
            "kind": "cache",
            "label": "cache selection: Night of the Living Dead",
            "status": "pending",
            "started_at": "",
        },
        {
            "id": "sync-running",
            "kind": "sync",
            "label": "library sync: provider refresh",
            "status": "running",
            "started_at": "2026-06-15T11:58:00Z",
        },
        {
            "id": "scan-complete",
            "kind": "scan",
            "label": "infringing scan: Real-Debrid",
            "status": "complete",
            "started_at": "2026-06-15T11:40:00Z",
            "finished_at": "2026-06-15T11:44:30Z",
            "logs": [
                {"message": "scan started for Real-Debrid", "level": "info", "source": "dav"},
                {
                    "message": "found 1 archived duplicate candidate",
                    "level": "warning",
                    "source": "dav",
                },
            ],
        },
        {
            "id": "restore-failed",
            "kind": "archive",
            "label": "archive restore: Metropolis",
            "status": "failed",
            "started_at": "2026-06-15T10:10:00Z",
            "finished_at": "2026-06-15T10:11:15Z",
            "error": "provider rejected duplicate hash",
            "logs": [
                {"message": "restore requested for arch1111", "level": "info", "source": "dav"},
                {"message": "restore failed: duplicate hash", "level": "error", "source": "dav"},
            ],
        },
    ]
    items = _ui_view_items(props, "threads") or defaults
    tasks = []
    for index, item in enumerate(items):
        task_data = (defaults[index] if index < len(defaults) else {}) | item
        tasks.append(
            BackgroundTask(
                id=str(task_data["id"]),
                kind=str(task_data.get("kind", "")),
                label=str(task_data.get("label", "")),
                cancel_event=threading.Event(),
                status=_task_status(task_data.get("status", "queued")),
                started_at=str(task_data.get("started_at", "")) or None,
                finished_at=str(task_data.get("finished_at", "")) or None,
                error=str(task_data.get("error", "")) or None,
                auto_complete=bool(task_data.get("auto_complete", True)),
            )
        )
    app.state.background_tasks._tasks = {task.id: task for task in tasks}
    for index, item in enumerate(items):
        task_data = (defaults[index] if index < len(defaults) else {}) | item
        with registry.task_context(str(task_data["id"])):
            for log in _as_list(task_data.get("logs")):
                if not isinstance(log, dict):
                    continue
                registry.record(
                    str(log.get("message", "")),
                    level=str(log.get("level", "info")),
                    source=str(log.get("source", "dav")),
                )


def _seed_logs(props: dict[str, Any]) -> None:
    defaults = [
        {
            "timestamp": "2026-06-15T11:56:00Z",
            "message": "buzz startup complete",
            "level": "info",
            "source": "dav",
            "count": 1,
        },
        {
            "timestamp": "2026-06-15T11:57:10Z",
            "message": "curator ready: 3 libraries mapped",
            "level": "info",
            "source": "curator",
            "count": 1,
        },
        {
            "timestamp": "2026-06-15T11:58:20Z",
            "message": "provider degraded: TorBox detail refresh delayed",
            "level": "warning",
            "source": "dav",
            "count": 2,
        },
        {
            "timestamp": "2026-06-15T11:59:00Z",
            "message": "thread failed: archive restore: Metropolis: provider rejected duplicate hash",
            "level": "error",
            "source": "dav",
            "count": 1,
            "link_to_task_id": "restore-failed",
        },
    ]
    log_items = _ui_view_items(props, "logs") or defaults
    events = []
    for index, item in enumerate(log_items):
        event = (defaults[index] if index < len(defaults) else {}) | item
        events.append(event)
    for event in events:
        registry.record_raw(event)


def _start_server(app: DavApp) -> tuple[uvicorn.Server, threading.Thread, int]:
    port = _free_port()
    config = uvicorn.Config(
        app.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("timed out waiting for screenshot server")
    return server, thread, port


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _background_palette(
    props: dict[str, Any],
    source: str,
    temp_dir: Path,
) -> list[str]:
    """Resolve one color per configured center, indexed by center order.

    Each center may carry its own ``color``; this list supplies the fallback
    when a center omits it. With no ``source`` the per-center ``color`` values
    are used directly; with a ``source`` image, colors are sampled from the
    top-left, top-right, and lower regions for the first three centers.
    """
    centers = _center_configs(props)
    if not source:
        defaults = ["#210c24", "#1f0d27", "#32c6a4"]
        return [
            str(center.get("color") or defaults[index % len(defaults)])
            for index, center in enumerate(centers)
        ]

    image_path = _background_source_path(source, temp_dir)
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    samples = [
        _average_hex(image.crop((0, 0, width // 3, height // 3))),
        _average_hex(image.crop((width * 2 // 3, 0, width, height // 3))),
        _saturated_cluster_hex(image.crop((0, height * 2 // 3, width, height))),
    ]
    return [samples[index % len(samples)] for index in range(len(centers))]


def _background_source_path(source: str, temp_dir: Path) -> Path:
    if source.startswith(("http://", "https://")):
        target = temp_dir / "background-source"
        LOGGER.info("downloading background source")
        with urlopen(source, timeout=20) as response:
            target.write_bytes(response.read())
        LOGGER.debug("downloaded background source to: %s", target)
        return target
    return Path(source).expanduser().resolve()


def _average_hex(image: Image.Image) -> str:
    stat = ImageStat.Stat(image.convert("RGB"))
    red = int(stat.mean[0])
    green = int(stat.mean[1])
    blue = int(stat.mean[2])
    return _rgb_to_hex((red, green, blue))


def _saturated_cluster_hex(image: Image.Image) -> str:
    small = image.convert("RGB").resize((96, 96))
    buckets: dict[tuple[int, int, int], tuple[int, int]] = {}
    for y in range(small.height):
        for x in range(small.width):
            red, green, blue = _rgb_pixel(small, x, y)
            lightness = (red + green + blue) / 3
            saturation = max(red, green, blue) - min(red, green, blue)
            if lightness < 18 or lightness > 235 or saturation < 24:
                continue
            key = (
                int(round(red / 16) * 16),
                int(round(green / 16) * 16),
                int(round(blue / 16) * 16),
            )
            score, count = buckets.get(key, (0, 0))
            buckets[key] = (score + saturation, count + 1)
    if not buckets:
        return _average_hex(image)
    color = max(
        buckets.items(),
        key=lambda item: (item[1][0] / item[1][1], item[1][1]),
    )[0]
    red, green, blue = _clamp_rgb(color)
    return _rgb_to_hex((red, green, blue))


def _render_background(
    props: dict[str, Any],
    palette: list[str],
    size: tuple[int, int],
) -> Image.Image:
    width, height = size
    centers = _resolve_centers(props, palette, size)
    image = Image.new("RGB", size)
    for y in range(height):
        if y and y % 200 == 0:
            LOGGER.debug("background rows rendered: %s/%s", y, height)
        for x in range(width):
            weighted = [0.0, 0.0, 0.0]
            total = 0.0
            for cx, cy, sigma_x, sigma_y, color in centers:
                dx = (x - cx) / sigma_x
                dy = (y - cy) / sigma_y
                weight = math.exp(-0.5 * (dx * dx + dy * dy))
                total += weight
                weighted[0] += color[0] * weight
                weighted[1] += color[1] * weight
                weighted[2] += color[2] * weight
            image.putpixel(
                (x, y),
                (
                    max(0, min(255, int(weighted[0] / total))),
                    max(0, min(255, int(weighted[1] / total))),
                    max(0, min(255, int(weighted[2] / total))),
                ),
            )
    blur_radius = float(_props_get(props, ("background", "blur_radius"), 0.6))
    LOGGER.debug("applying background blur radius: %s", blur_radius)
    return image.filter(ImageFilter.GaussianBlur(radius=blur_radius))


DEFAULT_CENTERS: tuple[dict[str, float], ...] = (
    {"polar_delta": 225.0, "sigma_x": 0.70, "sigma_y": 0.75},
    {"polar_delta": 315.0, "sigma_x": 0.70, "sigma_y": 0.75},
    {"polar_delta": 71.5616, "sigma_x": 0.45, "sigma_y": 0.38},
)


def _center_configs(props: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of raw center mappings, falling back to the defaults."""
    raw = _props_get(props, ("background", "centers"), None)
    if not isinstance(raw, list) or not raw:
        return [dict(center) for center in DEFAULT_CENTERS]
    return [center for center in raw if isinstance(center, dict)]


def _resolve_centers(
    props: dict[str, Any],
    palette: list[str],
    size: tuple[int, int],
) -> list[tuple[float, float, float, float, tuple[int, int, int]]]:
    """Resolve each configured center to (cx, cy, sigma_x, sigma_y, rgb).

    Each center's position is the point where a ray leaving the canvas center
    first meets the canvas border. The ray angle (degrees) is the center's own
    ``polar_delta`` plus the global ``polar_delta``, so the global value rotates
    the whole arrangement around the canvas center.
    """
    width, height = size
    global_delta = float(_props_get(props, ("background", "polar_delta"), 0.0))
    centers = _center_configs(props)
    resolved = []
    for index, center in enumerate(centers):
        angle = math.radians(float(center.get("polar_delta", 0.0)) + global_delta)
        nx, ny = _border_point(math.cos(angle), math.sin(angle))
        sigma_x = float(center.get("sigma_x", 0.5))
        sigma_y = float(center.get("sigma_y", 0.5))
        color = str(center.get("color", "")) or _palette_color(palette, index)
        resolved.append(
            (
                nx * width,
                ny * height,
                sigma_x * width,
                sigma_y * height,
                _hex_to_rgb(color),
            )
        )
    return resolved


def _palette_color(palette: list[str], index: int) -> str:
    if not palette:
        return "#000000"
    return palette[index] if index < len(palette) else palette[-1]


def _border_point(dir_x: float, dir_y: float) -> tuple[float, float]:
    """First intersection of a ray from the canvas center with the unit border."""
    if dir_x == 0 and dir_y == 0:
        return 0.5, 0.5
    t = math.inf
    if dir_x > 0:
        t = min(t, 0.5 / dir_x)
    elif dir_x < 0:
        t = min(t, -0.5 / dir_x)
    if dir_y > 0:
        t = min(t, 0.5 / dir_y)
    elif dir_y < 0:
        t = min(t, -0.5 / dir_y)
    return (
        max(0.0, min(1.0, 0.5 + t * dir_x)),
        max(0.0, min(1.0, 0.5 + t * dir_y)),
    )


def _load_hyprland_style(props: dict[str, Any]) -> dict[str, Any]:
    style = _hyprland_fallback_style(props)
    path = Path(str(_props_get(props, ("frame", "hyprland_config"), "~/.config/hypr/hyprland.conf"))).expanduser()
    style["source"] = str(path)
    style["source_found"] = False
    if not bool(_props_get(props, ("frame", "use_hyprland_values"), True)):
        LOGGER.info("Hyprland config loading disabled, using frame fallback values")
        return style
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        LOGGER.info("Hyprland config not found, using frame fallback values")
        return style

    style["source_found"] = True
    LOGGER.info("reading Hyprland frame values from: %s", path)
    for line in text.splitlines():
        _apply_hyprland_style_line(style, line)
    return style


def _apply_hyprland_style_line(style: dict[str, Any], line: str) -> None:
    body = line.split("#", 1)[0].strip()
    if body.startswith("col.active_border"):
        colors = _hyprland_rgba_colors(body)
        if colors:
            style["active_border_start"] = colors[0]
        if len(colors) > 1:
            style["active_border_end"] = colors[1]
    elif body.startswith("col.inactive_border"):
        colors = _hyprland_rgba_colors(body)
        if colors:
            style["inactive_border"] = colors[0]
    elif body.startswith("border_size"):
        style["border_size"] = _config_int(body, style["border_size"])
    elif body.startswith("rounding"):
        style["rounding"] = _config_int(body, style["rounding"])
    elif body.startswith("color") and "rgba(" in body:
        colors = _hyprland_rgba_colors(body)
        if colors:
            style["shadow"] = colors[0]


def _hyprland_fallback_style(props: dict[str, Any]) -> dict[str, Any]:
    fallback = _props_get(props, ("frame", "fallback"), {})
    if not isinstance(fallback, dict):
        fallback = {}
    return {
        "active_border_start": str(fallback.get("active_border_start", "#ff30c7")),
        "active_border_end": str(fallback.get("active_border_end", "#00ff99")),
        "inactive_border": str(fallback.get("inactive_border", "#5959b4")),
        "border_size": int(fallback.get("border_size", 2)),
        "rounding": int(fallback.get("rounding", 5)),
        "shadow": str(fallback.get("shadow", "#1a1a1a")),
    }


def _hyprland_rgba_colors(line: str) -> list[str]:
    colors = []
    start = 0
    while True:
        index = line.find("rgba(", start)
        if index < 0:
            return colors
        end = line.find(")", index)
        if end < 0:
            return colors
        raw = line[index + len("rgba("):end].strip()
        if len(raw) >= 6:
            colors.append(f"#{raw[:6]}")
        start = end + 1


def _config_int(line: str, default: int) -> int:
    try:
        return int(line.split("=", 1)[1].strip())
    except (IndexError, ValueError):
        return default


def _png_data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _fit_size(
    width: int, height: int, max_width: int, max_height: int
) -> tuple[int, int]:
    """Scaled (width, height) that fits the box without upscaling."""
    scale = min(max_width / width, max_height / height, 1.0)
    if scale >= 1:
        return width, height
    return int(width * scale), int(height * scale)


def _frame_outer_size(
    props: dict[str, Any],
    background_size: tuple[int, int],
) -> tuple[int, int]:
    bg_w, bg_h = background_size
    width_margin = int(_props_get(props, ("frame", "max_width_margin"), 20))
    height_margin = int(_props_get(props, ("frame", "max_height_margin"), 20))
    return max(1, bg_w - (width_margin * 2)), max(1, bg_h - (height_margin * 2))


def _screenshot_target_size(
    props: dict[str, Any],
    background_size: tuple[int, int],
    border_size: int,
) -> tuple[int, int]:
    frame_w, frame_h = _frame_outer_size(props, background_size)
    top_bar_height = _browser_top_bar_height(props)
    shot_w = max(1, frame_w - (border_size * 2))
    shot_h = max(1, frame_h - (border_size * 2) - top_bar_height)
    return shot_w, shot_h


def _browser_top_bar_height(props: dict[str, Any]) -> int:
    return int(_props_get(props, ("frame", "browser", "top_bar_height"), TOP_BAR_HEIGHT))


def _compose_prop_html(
    props: dict[str, Any],
    screenshot_uri: str,
    raw_size: tuple[int, int],
    background_uri: str,
    background_size: tuple[int, int],
    hyprland_style: dict[str, Any],
    route: str,
) -> str:
    """Build a self-contained HTML page that frames a screenshot over the background.

    The browser renders the rounded corners, gradient ring, and drop shadow
    natively, so no alpha-mask compositing (and no corner seam) is involved.
    """
    border_size = int(hyprland_style["border_size"])
    radius = int(hyprland_style["rounding"])
    bg_w, bg_h = background_size
    target_w, target_h = _screenshot_target_size(props, background_size, border_size)
    shot_w, shot_h = _fit_size(raw_size[0], raw_size[1], target_w, target_h)

    grad_start, grad_end = _border_gradient_colors(props, hyprland_style)
    shadow_r, shadow_g, shadow_b = _hex_to_rgb(str(hyprland_style["shadow"]))
    shadow_alpha = int(_props_get(props, ("frame", "shadow_alpha"), 120)) / 255
    shadow_blur = float(_props_get(props, ("frame", "shadow_blur_radius"), 14))
    shadow_dy = int(_props_get(props, ("frame", "shadow_offset_y"), 8))
    browser = _props_get(props, ("frame", "browser"), {})
    if not isinstance(browser, dict):
        browser = {}
    top_bar_height = int(browser.get("top_bar_height", TOP_BAR_HEIGHT))
    top_bar_background = str(browser.get("top_bar_background", "#11111b"))
    top_bar_border = str(browser.get("top_bar_border", "#1e1e2e"))
    top_bar_foreground = str(browser.get("top_bar_foreground", "#a6adc8"))
    address_background = str(browser.get("address_background", "#1e1e2e"))
    address_border = str(browser.get("address_border", "#313244"))
    address_foreground = str(browser.get("address_foreground", "#cdd6f4"))
    address_width = int(browser.get("address_width", 600))
    address_height = int(browser.get("address_height", 32))
    address_font_size = int(browser.get("address_font_size", 13))
    icon_size = int(browser.get("icon_size", 18))
    lock_icon_size = int(browser.get("lock_icon_size", 14))
    sidebar_icon_size = int(browser.get("sidebar_icon_size", 20))
    address_origin = str(browser.get("address_origin", "groundstation.lan:9443")).rstrip("/")
    address_text = html.escape(f"{address_origin}/{route}")

    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;}"
        f"body{{width:{bg_w}px;height:{bg_h}px;"
        f"background-image:url('{background_uri}');"
        f"background-size:{bg_w}px {bg_h}px;background-repeat:no-repeat;"
        "display:flex;align-items:center;justify-content:center;}"
        f".frame{{padding:{border_size}px;"
        f"border-radius:{radius + border_size}px;"
        f"background:linear-gradient(135deg,{grad_start},{grad_end});"
        f"box-shadow:0 {shadow_dy}px {shadow_blur}px "
        f"rgba({shadow_r},{shadow_g},{shadow_b},{shadow_alpha:.4f});}}"
        f".window-contents{{border-radius:{radius}px;overflow:hidden;"
        f"display:flex;flex-direction:column;background-color:{address_background};}}"
        f".window-contents>img{{display:block;width:{shot_w}px;height:{shot_h}px;}}"
        f".top-bar{{box-sizing:border-box;height:{top_bar_height}px;"
        f"background-color:{top_bar_background};border-bottom:1px solid {top_bar_border};"
        "display:flex;align-items:center;padding:0 12px;gap:12px;"
        f"color:{top_bar_foreground};font-family:system-ui,-apple-system,sans-serif;}}"
        ".top-bar-left{display:flex;flex:1;gap:12px;align-items:center;"
        "justify-content:flex-start;}"
        f".top-bar-center{{display:flex;justify-content:center;}}"
        f".address-bar{{background-color:{address_background};"
        f"border:1px solid {address_border};"
        f"border-radius:8px;width:{address_width}px;height:{address_height}px;"
        "display:flex;align-items:center;justify-content:center;gap:8px;"
        f"color:{address_foreground};font-size:{address_font_size}px;}}"
        ".top-bar-right{display:flex;flex:1;gap:12px;align-items:center;"
        "justify-content:flex-end;}"
        f".icon{{width:{icon_size}px;height:{icon_size}px;}}"
        f".icon-lock{{width:{lock_icon_size}px;height:{lock_icon_size}px;}}"
        f".sidebar-toggle{{width:{sidebar_icon_size}px;height:{sidebar_icon_size}px;}}"
        "</style></head><body>"
        f"<div class='frame'>"
        f"<div class='window-contents'>"
        f"<div class='top-bar'>"
        f"<div class='top-bar-left'>"
        "<svg class='sidebar-toggle' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round'><rect x='3' y='3' width='18' height='18' "
        "rx='2' ry='2'></rect><line x1='9' y1='3' x2='9' y2='21'></line>"
        "</svg>"
        "<svg class='icon' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' style='margin-left:12px;'>"
        "<line x1='19' y1='12' x2='5' y2='12'></line>"
        "<polyline points='12 19 5 12 12 5'></polyline></svg>"
        "<svg class='icon' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' style='opacity:0.5;'>"
        "<line x1='5' y1='12' x2='19' y2='12'></line>"
        "<polyline points='12 5 19 12 12 19'></polyline></svg>"
        "<svg class='icon' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round'><polyline points='23 4 23 10 17 10'>"
        "</polyline><path d='M20.49 15a9 9 0 1 1-2.12-9.36L23 10'>"
        "</path></svg>"
        "<svg class='icon' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round'><path d='M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5"
        "a2 2 0 0 1-2-2z'></path><polyline points='9 22 9 12 15 12 15 22'>"
        "</polyline></svg>"
        f"</div>"
        f"<div class='top-bar-center'>"
        f"<div class='address-bar'>"
        "<svg class='icon-lock' viewBox='0 0 24 24' fill='none' "
        f"stroke='{top_bar_foreground}' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round'><rect x='3' y='11' width='18' height='11' "
        "rx='2' ry='2'></rect><path d='M7 11V7a5 5 0 0 1 10 0v4'>"
        "</path></svg>"
        f"{address_text}"
        f"</div>"
        f"</div>"
        f"<div class='top-bar-right'>"
        "<svg class='icon' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round'><circle cx='12' cy='12' r='1'></circle>"
        "<circle cx='19' cy='12' r='1'></circle><circle cx='5' cy='12' r='1'>"
        "</circle></svg>"
        "<svg class='icon' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' style='margin-left:8px;'>"
        "<line x1='18' y1='6' x2='6' y2='18'></line>"
        "<line x1='6' y1='6' x2='18' y2='18'></line></svg>"
        f"</div>"
        f"</div>"
        f"<img src='{screenshot_uri}'>"
        f"</div>"
        f"</div>"
        "</body></html>"
    )


def _capture_and_compose(
    app: DavApp,
    props: dict[str, Any],
    port: int,
    raw_dir: Path,
    background_path: Path,
    hyprland_style: dict[str, Any],
    output_dir: Path,
) -> None:
    """Capture each route's app screenshot, then frame it in the browser."""
    background_uri = _png_data_uri(background_path)
    background_size = _background_size(props)
    with sync_playwright() as playwright:
        browser = playwright.firefox.launch()
        capture_page = browser.new_page(
            viewport=_viewport(props, hyprland_style), device_scale_factor=1
        )
        capture_page.emulate_media(reduced_motion="reduce")
        compose_page = browser.new_page(
            viewport={"width": background_size[0], "height": background_size[1]},
            device_scale_factor=1,
        )
        compose_page.emulate_media(reduced_motion="reduce")
        for route in _routes(props):
            _apply_global_ui_state(app, _route_global_ui_state(props, route))
            raw_path = raw_dir / f"{route}.png"
            _capture_route(props, capture_page, port, route, raw_path)
            raw_size = Image.open(raw_path).size
            LOGGER.info("composing prop: %s", route)
            html = _compose_prop_html(
                props,
                _png_data_uri(raw_path),
                raw_size,
                background_uri,
                background_size,
                hyprland_style,
                route,
            )
            compose_page.set_content(html, wait_until="load")
            out_path = output_dir / f"{route}.png"
            compose_page.screenshot(
                path=str(out_path),
                clip={
                    "x": 0,
                    "y": 0,
                    "width": background_size[0],
                    "height": background_size[1],
                },
            )
            LOGGER.info("wrote prop: %s", out_path)
        browser.close()


def _selected_thread_id(props: dict[str, Any]) -> str:
    threads_state = _ui_view_mapping(props, "threads")
    raw = threads_state.get(
        "selected_task_id",
        _props_get(props, ("ui_state", "threads_selected_task_id"), ""),
    )
    if raw:
        return str(raw)
    thread = _ui_view_item(props, "threads", 2, {"id": "scan-complete"})
    return str(thread["id"])


def _expanded_cache_id(props: dict[str, Any]) -> str:
    cache_state = _ui_view_mapping(props, "cache")
    raw = cache_state.get("expanded_id", "")
    if raw:
        return str(raw)
    item = _ui_view_item(props, "cache", 2, {"cache_key": "rd-pending-003"})
    return str(item.get("cache_key", ""))


def _capture_route(
    props: dict[str, Any],
    page: Page,
    port: int,
    route: str,
    raw_path: Path,
) -> None:
    LOGGER.info("capturing route: /%s", route)
    page.goto(f"http://127.0.0.1:{port}/{route}", wait_until="domcontentloaded")
    page.wait_for_selector("main")
    page.wait_for_timeout(500)
    if route == "cache":
        expanded_id = _expanded_cache_id(props)
        if expanded_id:
            page.locator(
                f"[phx-click='toggle_expand'][phx-value-id='{expanded_id}']"
            ).first.click()
            page.wait_for_timeout(500)
    if route == "threads":
        LOGGER.debug("capturing threads route with selected task")
        page.goto(
            f"http://127.0.0.1:{port}/threads?task_id={_selected_thread_id(props)}",
            wait_until="domcontentloaded",
        )
        page.wait_for_selector("main")
        page.wait_for_timeout(500)
    page.screenshot(path=str(raw_path))


def _border_gradient_colors(
    props: dict[str, Any],
    hyprland_style: dict[str, Any],
) -> tuple[str, str]:
    mode = str(_props_get(props, ("frame", "border_gradient"), "active_inverted"))
    start = str(hyprland_style["active_border_start"])
    end = str(hyprland_style["active_border_end"])
    if mode == "active":
        return start, end
    if mode == "active_inverted":
        return end, start
    return end, start


def _rgb_pixel(image: Image.Image, x: int, y: int) -> tuple[int, int, int]:
    pixel = image.getpixel((x, y))
    if not isinstance(pixel, tuple):
        return 0, 0, 0
    return int(pixel[0]), int(pixel[1]), int(pixel[2])


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = value.strip().lstrip("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    red, green, blue = _clamp_rgb(rgb)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _clamp_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    red, green, blue = rgb
    return (
        max(0, min(255, int(red))),
        max(0, min(255, int(green))),
        max(0, min(255, int(blue))),
    )


def _write_manifest(
    props: dict[str, Any],
    palette: list[str],
    hyprland_style: dict[str, Any],
    *,
    background_only: bool,
) -> None:
    background_size = _background_size(props)
    resolved = _resolve_centers(props, palette, background_size)
    manifest = {
        "generated_by": "maint-scripts/render_ui_props.py",
        "config": str(DEFAULT_PROPS_CONFIG),
        "viewport": _viewport(props, hyprland_style),
        "routes": list(_routes(props)),
        "background": {
            "size": list(background_size),
            "palette": palette,
            "polar_delta": float(_props_get(props, ("background", "polar_delta"), 0.0)),
            "centers": [
                {
                    "x": round(cx),
                    "y": round(cy),
                    "sigma_x": round(sigma_x),
                    "sigma_y": round(sigma_y),
                    "color": _rgb_to_hex(rgb),
                }
                for cx, cy, sigma_x, sigma_y, rgb in resolved
            ],
        },
        "hyprland": hyprland_style,
        "background_only": background_only,
    }
    manifest_path = _output_dir(props) / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    LOGGER.info("wrote manifest: %s", manifest_path)


if __name__ == "__main__":
    main()
