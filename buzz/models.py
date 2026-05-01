"""Buzz configuration models and persistence helpers."""

import os
from copy import deepcopy
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator

from .core.constants import DEFAULT_ANIME_PATTERN

DEFAULT_DAV_CONFIG_PATH = os.environ.get("BUZZ_CONFIG", "/app/buzz.yml")
DEFAULT_DIST_CONFIG_NAME = "buzz.dist.yml"
DEFAULT_STATE_DIR = "/app/data"
DEFAULT_APP_VERSION = "buzz/0.1"
DEFAULT_TLS_CERT_PATH = "data/tls/buzz.crt"
DEFAULT_TLS_KEY_PATH = "data/tls/buzz.key"
FIELD_ANIME_PATTERNS = "directories.anime.patterns"
FIELD_SUBTITLES_LANGUAGES = "subtitles.languages"
RESTART_REQUIRED_FIELDS = (
    "server.bind",
    "server.port",
    "tls.cert_path",
    "tls.key_path",
)
UI_MANAGED_CONFIG_FIELDS = (
    "provider.poll_interval_secs",
    "ui.poll_interval_secs",
    "server.bind",
    "server.port",
    "provider.connection_concurrency",
    "hooks.on_library_change",
    "hooks.curator_url",
    "hooks.rd_update_delay_secs",
    FIELD_ANIME_PATTERNS,
    "compat.enable_all_dir",
    "compat.enable_unplayable_dir",
    "request_timeout_secs",
    "version_label",
    "logging.verbose",
    "media_server.kind",
    "media_server.trigger_lib_scan",
    "media_server.jellyfin.url",
    "media_server.jellyfin.api_key",
    "media_server.jellyfin.scan_task_id",
    "media_server.plex.url",
    "media_server.plex.token",
    "media_server.library_map.movies",
    "media_server.library_map.shows",
    "media_server.library_map.anime",
    "subtitles.enabled",
    "subtitles.fetch_on_resync",
    "subtitles.opensubtitles.api_key",
    "subtitles.opensubtitles.username",
    "subtitles.opensubtitles.password",
    FIELD_SUBTITLES_LANGUAGES,
    "subtitles.strategy",
    "subtitles.filters.hearing_impaired",
    "subtitles.filters.exclude_ai",
    "subtitles.filters.exclude_machine",
    "subtitles.search_delay_secs",
    "subtitles.download_delay_secs",
    "subtitles.root",
    "tls.cert_path",
    "tls.key_path",
)
HOT_RELOADABLE_FIELDS = tuple(
    field for field in UI_MANAGED_CONFIG_FIELDS
    if field not in RESTART_REQUIRED_FIELDS
)


# ---------------------------------------------------------------------------
# Config merge / mask / persist helpers
# ---------------------------------------------------------------------------


def deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge *overrides* into *base*, returning a new dict."""
    result = dict(base)
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def delete_nested_key(target: dict, path: str) -> None:
    """Delete *path* from *target* and prune empty parent dictionaries."""
    keys = path.split(".")
    cursor = target
    parents: list[tuple[dict, str]] = []
    for key in keys[:-1]:
        child = cursor.get(key)
        if not isinstance(child, dict):
            return
        parents.append((cursor, key))
        cursor = child
    cursor.pop(keys[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break


def get_nested_value(source: dict, path: str) -> object | None:
    """Return the nested value at *path*, or ``None`` when missing."""
    cursor: object = source
    for key in path.split("."):
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return deepcopy(cursor)


def set_nested_value(target: dict, path: str, value: object) -> None:
    """Assign *value* at *path* within *target*."""
    cursor = target
    keys = path.split(".")
    for key in keys[:-1]:
        child = cursor.get(key)
        if not isinstance(child, dict):
            child = {}
            cursor[key] = child
        cursor = child
    cursor[keys[-1]] = deepcopy(value)


def _normalize_for_diff(value: object) -> object:
    """Normalize nested config values so tuple/list mismatches diff cleanly."""
    if isinstance(value, tuple):
        return [_normalize_for_diff(item) for item in value]
    if isinstance(value, list):
        return [_normalize_for_diff(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalize_for_diff(item)
            for key, item in value.items()
        }
    return value


def diff_fields(source: dict, target: dict, fields: tuple[str, ...]) -> list[str]:
    """Return configured field paths whose values differ between two dicts."""
    changed = []
    for field in fields:
        source_value = _normalize_for_diff(get_nested_value(source, field))
        target_value = _normalize_for_diff(get_nested_value(target, field))
        if source_value != target_value:
            changed.append(field)
    return changed


def unknown_config_keys(user: dict, schema: dict, _prefix: str = "") -> list[str]:
    """Return dotted paths present in *user* but absent from *schema*."""
    unknown: list[str] = []
    for key, value in user.items():
        path = f"{_prefix}.{key}" if _prefix else key
        if key not in schema:
            unknown.append(path)
        elif isinstance(value, dict) and isinstance(schema.get(key), dict):
            unknown.extend(unknown_config_keys(value, schema[key], path))
    return sorted(unknown)


def override_field_paths(overrides: dict) -> list[str]:
    """Return flattened dotted field paths present in *overrides*."""
    results: list[str] = []

    def visit(node: dict, prefix: str = "") -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                visit(value, path)
                continue
            results.append(path)

    visit(overrides)
    return sorted(results)


def effective_override_field_paths(base: dict, overrides: dict) -> list[str]:
    """Return override field paths whose values differ from the base config."""
    changed: list[str] = []
    for path in override_field_paths(overrides):
        base_value = _normalize_for_diff(get_nested_value(base, path))
        override_value = _normalize_for_diff(get_nested_value(overrides, path))
        if base_value != override_value:
            changed.append(path)
    return changed


def filter_paths(source: dict, paths: tuple[str, ...]) -> dict:
    """Return a nested dict containing only the selected field *paths*."""
    filtered: dict = {}
    for path in paths:
        value = get_nested_value(source, path)
        if value is not None:
            set_nested_value(filtered, path, value)
    return filtered


_SECRET_PATHS = [
    ("provider", "token"),
    ("media_server", "jellyfin", "api_key"),
    ("media_server", "plex", "token"),
    ("subtitles", "opensubtitles", "api_key"),
    ("subtitles", "opensubtitles", "username"),
    ("subtitles", "opensubtitles", "password"),
]


def mask_secrets(d: dict) -> dict:
    """Return a copy of *d* with secret-looking keys replaced by '***'."""
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = mask_secrets(value)
        else:
            result[key] = value
    for path in _SECRET_PATHS:
        current = result
        for part in path[:-1]:
            if part not in current or not isinstance(current[part], dict):
                break
            current = current[part]
        else:
            if path[-1] in current:
                current[path[-1]] = "***"
    return result


def _strip_provider_token(d: dict) -> None:
    provider = d.get("provider")
    if isinstance(provider, dict):
        provider.pop("token", None)
        if not provider:
            del d["provider"]


def _strip_opensubtitles_secrets(d: dict) -> None:
    subtitles = d.get("subtitles")
    if not isinstance(subtitles, dict):
        return
    opensubs = subtitles.get("opensubtitles")
    if isinstance(opensubs, dict):
        for k in ("api_key", "username", "password"):
            opensubs.pop(k, None)
        if not opensubs:
            subtitles.pop("opensubtitles", None)
    if not subtitles:
        del d["subtitles"]


def _strip_secrets(d: dict) -> dict:
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            nested = _strip_secrets(value)
            if nested:
                result[key] = nested
        else:
            result[key] = value
    _strip_provider_token(result)
    _strip_opensubtitles_secrets(result)
    return result


_OVERRIDE_SCHEMA = {
    "provider": {
        "token": True,
        "connection_concurrency": True,
        "poll_interval_secs": True,
    },
    "server": {
        "bind": True,
        "port": True,
    },
    "state_dir": True,
    "hooks": {
        "on_library_change": True,
        "curator_url": True,
        "rd_update_delay_secs": True,
        "vfs_wait_timeout_secs": True,
    },
    "directories": {"anime": {"patterns": True}},
    "compat": {"enable_all_dir": True, "enable_unplayable_dir": True},
    "request_timeout_secs": True,
    "rd_hoster_failure_cache_secs": True,
    "user_agent": True,
    "version_label": True,
    "ui": {"poll_interval_secs": True},
    "logging": {"verbose": True, "max_entries": True},
    "media_server": {
        "kind": True,
        "trigger_lib_scan": True,
        "jellyfin": {
            "url": True,
            "api_key": True,
            "scan_task_id": True,
        },
        "plex": {
            "url": True,
            "token": True,
        },
        "library_map": True,
    },
    "subtitles": {
        "enabled": True,
        "fetch_on_resync": True,
        "languages": True,
        "strategy": True,
        "filters": {
            "hearing_impaired": True,
            "exclude_ai": True,
            "exclude_machine": True,
        },
        "search_delay_secs": True,
        "download_delay_secs": True,
    },
    "tls": {
        "cert_path": True,
        "key_path": True,
    },
}


def _validate_override_keys(
    overrides: dict,
    schema: dict | None = None,
    path: str = "",
) -> list[str]:
    if schema is None:
        schema = _OVERRIDE_SCHEMA
    errors = []
    for key, value in overrides.items():
        current_path = f"{path}.{key}" if path else key
        if key not in schema:
            errors.append(current_path)
        elif isinstance(value, dict) and schema[key] is not True:
            errors.extend(_validate_override_keys(value, schema[key], current_path))
    return errors


def save_overrides(overrides: dict, path: Path) -> None:
    """Validate and write override rules to a YAML file atomically."""
    invalid = _validate_override_keys(overrides)
    if invalid:
        raise ValueError(f"Invalid override keys: {', '.join(invalid)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not overrides:
        path.unlink(missing_ok=True)
        return
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(overrides, handle, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, path)


def _load_default_dist_config(path: str) -> dict:
    """Load the sibling ``buzz.dist.yml`` when present, else return an empty dict."""
    dist_path = Path(path).with_name(DEFAULT_DIST_CONFIG_NAME)
    if not dist_path.exists():
        return {}
    with open(dist_path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_base_and_overrides(
    path: str = DEFAULT_DAV_CONFIG_PATH,
) -> tuple[dict, dict, dict, dict, dict, Path]:
    """Load base YAML, overrides YAML, merged config, and overrides path."""
    default_dist = _load_default_dist_config(path)
    with open(path, encoding="utf-8") as handle:
        file_base = yaml.safe_load(handle) or {}
    base = deep_merge(default_dist, file_base)

    state_dir = str(file_base.get("state_dir", base.get("state_dir", DEFAULT_STATE_DIR)))
    overrides_env = os.environ.get("BUZZ_OVERRIDES", "")
    overrides_path = (
        Path(overrides_env)
        if overrides_env
        else Path(state_dir) / "buzz.overrides.yml"
    )

    overrides = {}
    if overrides_path.exists():
        with open(overrides_path, encoding="utf-8") as handle:
            overrides = yaml.safe_load(handle) or {}

    merged = deep_merge(base, overrides)
    return default_dist, file_base, base, overrides, merged, overrides_path.resolve()


def to_nested_dict(config: DavConfig) -> dict:
    """Serialize a DavConfig to the nested dict structure used in buzz.yml."""
    return {
        "provider": {
            "token": config.token,
            "connection_concurrency": config.connection_concurrency,
            "poll_interval_secs": config.provider_poll_interval_secs,
        },
        "server": {
            "bind": config.bind,
            "port": config.port,
        },
        "state_dir": config.state_dir,
        "hooks": {
            "on_library_change": config.hook_command,
            "curator_url": config.curator_url,
            "rd_update_delay_secs": config.rd_update_delay_secs,
            "vfs_wait_timeout_secs": config.vfs_wait_timeout_secs,
        },
        "directories": {
            "anime": {"patterns": list(config.anime_patterns)},
        },
        "compat": {
            "enable_all_dir": config.enable_all_dir,
            "enable_unplayable_dir": config.enable_unplayable_dir,
        },
        "request_timeout_secs": config.request_timeout_secs,
        "rd_hoster_failure_cache_secs": config.rd_hoster_failure_cache_secs,
        "user_agent": config.user_agent,
        "version_label": config.version_label,
        "ui": {"poll_interval_secs": config.ui_poll_interval_secs},
        "logging": {
            "verbose": config.verbose,
            "max_entries": config.log_max_entries,
        },
        "media_server": {
            "kind": config.media_server_kind,
            "trigger_lib_scan": config.trigger_lib_scan,
            "jellyfin": {
                "url": config.jellyfin_url,
                "api_key": config.jellyfin_api_key,
                "scan_task_id": config.jellyfin_scan_task_id,
            },
            "plex": {
                "url": config.plex_url,
                "token": config.plex_token,
            },
            "library_map": dict(config.library_map),
        },
        "subtitles": {
            "enabled": config.subtitles.enabled,
            "fetch_on_resync": config.subtitles.fetch_on_resync,
            "opensubtitles": {
                "api_key": config.subtitles.api_key,
                "username": config.subtitles.username,
                "password": config.subtitles.password,
            },
            "languages": config.subtitles.languages,
            "strategy": config.subtitles.strategy,
            "filters": {
                "hearing_impaired": config.subtitles.filters.hearing_impaired,
                "exclude_ai": config.subtitles.filters.exclude_ai,
                "exclude_machine": config.subtitles.filters.exclude_machine,
            },
            "search_delay_secs": config.subtitles.search_delay_secs,
            "download_delay_secs": config.subtitles.download_delay_secs,
        },
        "tls": {
            "cert_path": config.tls.cert_path,
            "key_path": config.tls.key_path,
        },
    }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SubtitleFilters(BaseModel):
    """Filter rules for subtitle search results."""

    # "exclude" drops HI tracks; "include" allows them; "prefer" ranks them first
    hearing_impaired: str = "exclude"
    exclude_ai: bool = True
    exclude_machine: bool = True


class SubtitleConfig(BaseModel):
    """OpenSubtitles integration configuration."""

    enabled: bool = False
    fetch_on_resync: bool = False
    api_key: str = ""
    username: str = ""
    password: str = ""
    languages: list[str] = ["en"]
    # best-match | most-downloaded | best-rated | trusted | latest
    strategy: str = "most-downloaded"
    filters: SubtitleFilters = Field(default_factory=SubtitleFilters)
    search_delay_secs: float = 0.5
    download_delay_secs: float = 1.0

    @classmethod
    def from_raw(cls, raw: dict | None) -> SubtitleConfig:
        """Build a SubtitleConfig from a plain dict (e.g. parsed YAML)."""
        if not raw:
            return cls()
        opensubs = raw.get("opensubtitles", {})
        filters_raw = raw.get("filters", {})
        return cls(
            enabled=bool(raw.get("enabled", False)),
            fetch_on_resync=bool(raw.get("fetch_on_resync", False)),
            api_key=str(opensubs.get("api_key", "")),
            username=str(opensubs.get("username", "")),
            password=str(opensubs.get("password", "")),
            languages=list(raw.get("languages", ["en"])),
            strategy=str(raw.get("strategy", "most-downloaded")),
            filters=SubtitleFilters(
                hearing_impaired=str(
                    filters_raw.get("hearing_impaired", "exclude")
                ),
                exclude_ai=bool(filters_raw.get("exclude_ai", True)),
                exclude_machine=bool(filters_raw.get("exclude_machine", True)),
            ),
            search_delay_secs=float(raw.get("search_delay_secs", 0.5)),
            download_delay_secs=float(raw.get("download_delay_secs", 1.0)),
        )

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


class TlsConfig(BaseModel):
    """TLS certificate and key configuration."""

    cert_path: str = DEFAULT_TLS_CERT_PATH
    key_path: str = DEFAULT_TLS_KEY_PATH


class DavConfig(BaseModel):
    """Configuration for the WebDAV / Real-Debrid front-end."""

    token: str = ""
    provider_poll_interval_secs: int = 10
    bind: str = "0.0.0.0"
    port: int = 9999
    connection_concurrency: int = 4
    state_dir: str = DEFAULT_STATE_DIR
    hook_command: str = ""
    anime_patterns: tuple[str, ...] = (DEFAULT_ANIME_PATTERN,)
    enable_all_dir: bool = True
    enable_unplayable_dir: bool = True
    request_timeout_secs: int = 30
    rd_hoster_failure_cache_secs: int = 60
    user_agent: str = DEFAULT_APP_VERSION
    version_label: str = DEFAULT_APP_VERSION
    curator_url: str = "http://buzz-curator:8400/rebuild"
    rd_update_delay_secs: int = 15
    vfs_wait_timeout_secs: int = 300
    library_mount: str = ""
    verbose: bool = False
    log_max_entries: int = 1000
    ui_poll_interval_secs: int = 3
    media_server_kind: str = "jellyfin"
    trigger_lib_scan: bool = False
    jellyfin_url: str = "http://jellyfin:8096"
    jellyfin_api_key: str = ""
    jellyfin_scan_task_id: str = ""
    plex_url: str = ""
    plex_token: str = ""
    library_map: dict[str, str] = Field(default_factory=dict)
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)
    subtitle_root: str = "/mnt/buzz/subs"
    tls: TlsConfig = Field(default_factory=TlsConfig)

    _overrides_path: Path = PrivateAttr(
        default=Path("/app/data/buzz.overrides.yml")
    )
    _config_path: str = PrivateAttr(default=DEFAULT_DAV_CONFIG_PATH)
    _default_raw: dict = PrivateAttr(default_factory=dict)
    _file_raw: dict = PrivateAttr(default_factory=dict)
    _base_raw: dict = PrivateAttr(default_factory=dict)
    _raw_overrides: dict = PrivateAttr(default_factory=dict)
    _raw_merged: dict = PrivateAttr(default_factory=dict)

    @classmethod
    def _from_merged_dict(cls, raw: dict) -> DavConfig:
        provider = raw.get("provider", {})
        server = raw.get("server", {})
        hooks = raw.get("hooks", {})
        directories = raw.get("directories", {})
        anime = directories.get("anime", {})
        compat = raw.get("compat", {})
        logging_raw = raw.get("logging", {})
        ui_raw = raw.get("ui", {})
        media_server_raw = raw.get("media_server", {})
        tls_raw = raw.get("tls", {})

        token = provider.get("token", "").strip()

        return cls(
            token=token,
            provider_poll_interval_secs=int(provider.get("poll_interval_secs", 10)),
            bind=str(server.get("bind", "0.0.0.0")),
            port=int(server.get("port", 9999)),
            connection_concurrency=max(
                1, int(provider.get("connection_concurrency", 4))
            ),
            state_dir=str(raw.get("state_dir", DEFAULT_STATE_DIR)),
            hook_command=str(hooks.get("on_library_change", "")).strip(),
            curator_url=str(
                hooks.get("curator_url", "http://buzz-curator:8400/rebuild")
            ),
            rd_update_delay_secs=int(
                hooks.get("rd_update_delay_secs", 15)
            ),
            vfs_wait_timeout_secs=int(
                hooks.get("vfs_wait_timeout_secs", 300)
            ),
            library_mount="/mnt/buzz/raw",
            anime_patterns=tuple(anime.get("patterns", [DEFAULT_ANIME_PATTERN])),
            enable_all_dir=bool(compat.get("enable_all_dir", True)),
            enable_unplayable_dir=bool(
                compat.get("enable_unplayable_dir", True)
            ),
            request_timeout_secs=int(raw.get("request_timeout_secs", 30)),
            rd_hoster_failure_cache_secs=max(
                1, int(raw.get("rd_hoster_failure_cache_secs", 60))
            ),
            user_agent=str(raw.get("user_agent", DEFAULT_APP_VERSION)),
            version_label=str(raw.get("version_label", DEFAULT_APP_VERSION)),
            verbose=bool(logging_raw.get("verbose", False)),
            log_max_entries=int(logging_raw.get("max_entries", 1000)),
            ui_poll_interval_secs=int(
                ui_raw.get("poll_interval_secs", 3)
            ),
            media_server_kind=str(
                media_server_raw.get("kind", "jellyfin")
            ).strip().lower() or "jellyfin",
            trigger_lib_scan=bool(
                media_server_raw.get("trigger_lib_scan", False)
            ),
            jellyfin_url=str(
                (media_server_raw.get("jellyfin") or {}).get(
                    "url", "http://jellyfin:8096"
                )
            ).rstrip("/"),
            jellyfin_api_key=str(
                (media_server_raw.get("jellyfin") or {}).get("api_key", "")
            ),
            jellyfin_scan_task_id=str(
                (media_server_raw.get("jellyfin") or {}).get("scan_task_id", "")
            ),
            plex_url=str(
                (media_server_raw.get("plex") or {}).get("url", "")
            ).rstrip("/"),
            plex_token=str(
                (media_server_raw.get("plex") or {}).get("token", "")
            ),
            library_map={
                str(k): str(v)
                for k, v in (media_server_raw.get("library_map") or {}).items()
            },
            subtitles=SubtitleConfig.from_raw(raw.get("subtitles")),
            subtitle_root=str(
                (raw.get("subtitles") or {}).get("root", "/mnt/buzz/subs")
            ),
            tls=TlsConfig(
                cert_path=str(
                    tls_raw.get("cert_path", DEFAULT_TLS_CERT_PATH)
                ),
                key_path=str(
                    tls_raw.get("key_path", DEFAULT_TLS_KEY_PATH)
                ),
            ),
        )

    @classmethod
    def load(cls, path: str = DEFAULT_DAV_CONFIG_PATH) -> DavConfig:
        """Load and validate configuration from a YAML file."""
        default_raw, file_raw, base, overrides, merged, overrides_path = load_base_and_overrides(path)
        config = cls._from_merged_dict(merged)
        config._config_path = path
        config._overrides_path = overrides_path
        config._default_raw = default_raw
        config._file_raw = file_raw
        config._base_raw = base
        config._raw_overrides = overrides
        config._raw_merged = to_nested_dict(config)
        return config


class CuratorConfig(BaseModel):
    """Configuration for the curator service."""

    bind: str = Field(
        default_factory=lambda: os.environ.get(
            "CURATOR_BIND",
            os.environ.get("PRESENTATION_BIND", "0.0.0.0"),
        )
    )
    port: int = Field(
        default_factory=lambda: int(
            os.environ.get(
                "CURATOR_PORT",
                os.environ.get("PRESENTATION_PORT", "8400"),
            )
        )
    )
    source_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "CURATOR_SOURCE_ROOT",
                os.environ.get("PRESENTATION_SOURCE_ROOT", "/mnt/buzz/raw"),
            )
        )
    )
    target_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "CURATOR_TARGET_ROOT",
                os.environ.get("PRESENTATION_TARGET_ROOT", "/mnt/buzz/curated"),
            )
        )
    )
    state_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "CURATOR_STATE_DIR",
                os.environ.get(
                    "PRESENTATION_STATE_DIR",
                    os.environ.get("PRESENTATION_STATE_ROOT", DEFAULT_STATE_DIR),
                ),
            )
        )
    )
    overrides_path: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "CURATOR_OVERRIDES",
                os.environ.get("PRESENTATION_OVERRIDES", "/config/overrides.yml"),
            )
        )
    )
    jellyfin_url: str = "http://jellyfin:8096"
    jellyfin_api_key: str = ""
    jellyfin_scan_task_id: str = ""
    jellyfin_library_map: dict[str, str] = Field(default_factory=dict)
    media_server_kind: str = "jellyfin"
    trigger_lib_scan: bool = False
    plex_url: str = ""
    plex_token: str = ""
    build_on_start: bool = Field(
        default_factory=lambda: (
            os.environ.get(
                "CURATOR_BUILD_ON_START",
                os.environ.get("PRESENTATION_BUILD_ON_START", "true"),
            ).lower()
            in {"1", "true", "yes"}
        )
    )
    verbose: bool = Field(
        default_factory=lambda: _env_flag("CURATOR_VERBOSE")
        or _env_flag("PRESENTATION_VERBOSE")
    )
    dav_ui_notify_url: str = Field(
        default_factory=lambda: os.environ.get(
            "DAV_UI_NOTIFY_URL",
            "http://buzz-dav:9999/api/ui/notify",
        ).rstrip("/")
    )
    log_max_entries: int = Field(
        default_factory=lambda: int(
            os.environ.get(
                "CURATOR_LOG_MAX_ENTRIES",
                os.environ.get("PRESENTATION_LOG_MAX_ENTRIES", "1000"),
            )
        )
    )
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)
    subtitle_root: Path = Field(default_factory=lambda: Path("/mnt/buzz/subs"))
    _base_raw: dict = PrivateAttr(default_factory=dict)
    _raw_overrides: dict = PrivateAttr(default_factory=dict)
    _raw_merged: dict = PrivateAttr(default_factory=dict)
    _overrides_path: Path = PrivateAttr(
        default=Path("/app/data/buzz.overrides.yml")
    )
    _config_path: str = PrivateAttr(default=DEFAULT_DAV_CONFIG_PATH)
    _default_raw: dict = PrivateAttr(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> CuratorConfig:
        """Load from env defaults and optional buzz.yml subtitle config."""
        data: dict = {}
        config_path = path or os.environ.get("BUZZ_CONFIG", "/app/buzz.yml")

        if config_path and os.path.exists(config_path):
            try:
                default_raw, _file_raw, base, overrides, merged, overrides_path = load_base_and_overrides(
                    config_path
                )
                if "state_dir" in merged:
                    data["state_dir"] = Path(str(merged["state_dir"]))
                subtitles_raw = merged.get("subtitles")
                if subtitles_raw is not None:
                    data["subtitles"] = SubtitleConfig.from_raw(subtitles_raw)
                    if isinstance(subtitles_raw, dict) and "root" in subtitles_raw:
                        data["subtitle_root"] = Path(str(subtitles_raw["root"]))
                media_server = merged.get("media_server") or {}
                if "kind" in media_server:
                    data["media_server_kind"] = str(
                        media_server["kind"]
                    ).strip().lower() or "jellyfin"
                if "trigger_lib_scan" in media_server:
                    data["trigger_lib_scan"] = bool(
                        media_server["trigger_lib_scan"]
                    )
                jellyfin = media_server.get("jellyfin") or {}
                if "url" in jellyfin:
                    data["jellyfin_url"] = str(jellyfin["url"]).rstrip("/")
                if "api_key" in jellyfin:
                    data["jellyfin_api_key"] = str(jellyfin["api_key"])
                if "scan_task_id" in jellyfin:
                    data["jellyfin_scan_task_id"] = str(jellyfin["scan_task_id"])
                plex = media_server.get("plex") or {}
                if "url" in plex:
                    data["plex_url"] = str(plex["url"]).rstrip("/")
                if "token" in plex:
                    data["plex_token"] = str(plex["token"])
                library_map = media_server.get("library_map") or {}
                if library_map:
                    data["jellyfin_library_map"] = {
                        str(k): str(v) for k, v in library_map.items()
                    }
                config = cls(**data)
                config._config_path = config_path
                config._default_raw = default_raw
                config._base_raw = base
                config._raw_overrides = overrides
                config._raw_merged = merged
                config._overrides_path = overrides_path
                return config
            except Exception as exc:
                print(
                    f"Warning: Failed to load buzz.yml from {config_path}: "
                    f"{exc}"
                )

        config = cls(**data)
        config._config_path = config_path
        return config


PresentationConfig = CuratorConfig


class ErrorResponse(BaseModel):
    """Standard error response payload."""

    error: str


class UiNotifyRequest(BaseModel):
    """Request body for backend-driven UI websocket notifications."""

    topics: list[str]
    message: dict[str, object] = Field(default_factory=dict)

    @field_validator("topics")
    @classmethod
    def validate_topics(cls, value: list[str]) -> list[str]:
        """Normalize topics and reject empty payloads."""
        normalized = [topic.strip() for topic in value if topic.strip()]
        if not normalized:
            raise ValueError("Missing topics")
        return normalized


class AddTorrentRequest(BaseModel):
    """Request body for adding a torrent by magnet link."""

    magnet: str

    @field_validator("magnet")
    @classmethod
    def validate_magnet(cls, value: str) -> str:
        """Strip whitespace and reject empty magnet links."""
        value = value.strip()
        if not value:
            raise ValueError("Missing magnet link")
        return value


class SelectFilesRequest(BaseModel):
    """Request body for selecting files inside a torrent."""

    torrent_id: str
    file_ids: list[str]

    @field_validator("torrent_id")
    @classmethod
    def validate_torrent_id(cls, value: str) -> str:
        """Strip whitespace and reject empty torrent IDs."""
        value = value.strip()
        if not value:
            raise ValueError("Missing torrent_id")
        return value


class DeleteTorrentRequest(BaseModel):
    """Request body for deleting a torrent."""

    torrent_id: str

    @field_validator("torrent_id")
    @classmethod
    def validate_torrent_id(cls, value: str) -> str:
        """Strip whitespace and reject empty torrent IDs."""
        value = value.strip()
        if not value:
            raise ValueError("Missing torrent_id")
        return value


class RestoreTrashRequest(BaseModel):
    """Request body for restoring a deleted torrent from trash."""

    hash: str

    @field_validator("hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        """Strip whitespace and reject empty hashes."""
        value = value.strip()
        if not value:
            raise ValueError("Missing hash")
        return value


class DeleteTrashRequest(BaseModel):
    """Request body for permanently deleting a trashed torrent."""

    hash: str

    @field_validator("hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        """Strip whitespace and reject empty hashes."""
        value = value.strip()
        if not value:
            raise ValueError("Missing hash")
        return value
