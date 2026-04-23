import os
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, PrivateAttr

from .core.constants import DEFAULT_ANIME_PATTERN

DEFAULT_DAV_CONFIG_PATH = os.environ.get("BUZZ_CONFIG", "/app/buzz.yml")


# ---------------------------------------------------------------------------
# Config merge / mask / persist helpers
# ---------------------------------------------------------------------------


def deep_merge(base: dict, overrides: dict) -> dict:
    result = dict(base)
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


_SECRET_PATHS = [
    ("provider", "token"),
    ("subtitles", "opensubtitles", "api_key"),
    ("subtitles", "opensubtitles", "username"),
    ("subtitles", "opensubtitles", "password"),
]


def mask_secrets(d: dict) -> dict:
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


def _strip_secrets(d: dict) -> dict:
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            nested = _strip_secrets(value)
            if nested:
                result[key] = nested
        else:
            result[key] = value
    if "provider" in result and isinstance(result["provider"], dict):
        result["provider"].pop("token", None)
        if not result["provider"]:
            del result["provider"]
    if "subtitles" in result and isinstance(result["subtitles"], dict):
        opensubs = result["subtitles"].get("opensubtitles")
        if isinstance(opensubs, dict):
            for k in ("api_key", "username", "password"):
                opensubs.pop(k, None)
            if not opensubs:
                result["subtitles"].pop("opensubtitles", None)
        if not result["subtitles"]:
            del result["subtitles"]
    return result


_OVERRIDE_SCHEMA = {
    "poll_interval_secs": True,
    "server": {"bind": True, "port": True, "stream_buffer_size": True},
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
    "user_agent": True,
    "version_label": True,
    "ui": {"poll_interval_secs": True},
    "logging": {"verbose": True, "max_entries": True},
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
}


def _validate_override_keys(overrides: dict, schema: dict | None = None, path: str = "") -> list[str]:
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
    invalid = _validate_override_keys(overrides)
    if invalid:
        raise ValueError(f"Invalid override keys: {', '.join(invalid)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(overrides, handle, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, path)


def to_nested_dict(config: "DavConfig") -> dict:
    return {
        "provider": {"token": config.token},
        "poll_interval_secs": config.poll_interval_secs,
        "server": {
            "bind": config.bind,
            "port": config.port,
            "stream_buffer_size": config.stream_buffer_size,
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
        "user_agent": config.user_agent,
        "version_label": config.version_label,
        "ui": {"poll_interval_secs": config.ui_poll_interval_secs},
        "logging": {
            "verbose": config.verbose,
            "max_entries": config.log_max_entries,
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
    }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SubtitleFilters(BaseModel):
    # "exclude" drops HI tracks; "include" allows them; "prefer" ranks them first
    hearing_impaired: str = "exclude"
    exclude_ai: bool = True
    exclude_machine: bool = True


class SubtitleConfig(BaseModel):
    enabled: bool = False
    fetch_on_resync: bool = False
    api_key: str = ""
    username: str = ""
    password: str = ""
    languages: list[str] = ["en"]
    strategy: str = "most-downloaded"  # best-match | most-downloaded | best-rated | trusted | latest
    filters: SubtitleFilters = Field(default_factory=SubtitleFilters)
    search_delay_secs: float = 0.5
    download_delay_secs: float = 1.0


class DavConfig(BaseModel):
    token: str
    poll_interval_secs: int = 10
    bind: str = "0.0.0.0"
    port: int = 9999
    stream_buffer_size: int = 0
    state_dir: str = "/app/data"
    hook_command: str = ""
    anime_patterns: tuple[str, ...] = (DEFAULT_ANIME_PATTERN,)
    enable_all_dir: bool = True
    enable_unplayable_dir: bool = True
    request_timeout_secs: int = 30
    user_agent: str = "buzz/0.1"
    version_label: str = "buzz/0.1"
    curator_url: str = "http://buzz-curator:8400/rebuild"
    rd_update_delay_secs: int = 15
    vfs_wait_timeout_secs: int = 300
    library_mount: str = ""
    verbose: bool = False
    log_max_entries: int = 1000
    ui_poll_interval_secs: int = 3
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)

    _overrides_path: Path = PrivateAttr(default=Path("/app/data/buzz.overrides.yml"))
    _raw_merged: dict = PrivateAttr(default_factory=dict)

    @classmethod
    def _from_merged_dict(cls, raw: dict) -> "DavConfig":
        provider = raw.get("provider", {})
        server = raw.get("server", {})
        hooks = raw.get("hooks", {})
        directories = raw.get("directories", {})
        anime = directories.get("anime", {})
        compat = raw.get("compat", {})
        logging_raw = raw.get("logging", {})
        subs_raw = raw.get("subtitles", {})
        opensubs = subs_raw.get("opensubtitles", {})
        subs_filters = subs_raw.get("filters", {})
        ui_raw = raw.get("ui", {})

        token = provider.get("token", "").strip()
        if not token:
            raise ValueError("provider.token is required.")

        return cls(
            token=token,
            poll_interval_secs=int(raw.get("poll_interval_secs", 10)),
            bind=str(server.get("bind", "0.0.0.0")),
            port=int(server.get("port", 9999)),
            stream_buffer_size=int(server.get("stream_buffer_size", 0)),
            state_dir=str(raw.get("state_dir", "/app/data")),
            hook_command=str(hooks.get("on_library_change", "")).strip(),
            curator_url=str(
                hooks.get("curator_url", "http://buzz-curator:8400/rebuild")
            ),
            rd_update_delay_secs=int(hooks.get("rd_update_delay_secs", 15)),
            vfs_wait_timeout_secs=int(hooks.get("vfs_wait_timeout_secs", 300)),
            library_mount=os.environ.get("LIBRARY_MOUNT", ""),
            anime_patterns=tuple(anime.get("patterns", [DEFAULT_ANIME_PATTERN])),
            enable_all_dir=bool(compat.get("enable_all_dir", True)),
            enable_unplayable_dir=bool(compat.get("enable_unplayable_dir", True)),
            request_timeout_secs=int(raw.get("request_timeout_secs", 30)),
            user_agent=str(raw.get("user_agent", "buzz/0.1")),
            version_label=str(raw.get("version_label", "buzz/0.1")),
            verbose=bool(logging_raw.get("verbose", False)),
            log_max_entries=int(logging_raw.get("max_entries", 1000)),
            ui_poll_interval_secs=int(ui_raw.get("poll_interval_secs", 3)),
            subtitles=SubtitleConfig(
                enabled=bool(subs_raw.get("enabled", False)),
                fetch_on_resync=bool(subs_raw.get("fetch_on_resync", False)),
                api_key=str(opensubs.get("api_key", "")),
                username=str(opensubs.get("username", "")),
                password=str(opensubs.get("password", "")),
                languages=list(subs_raw.get("languages", ["en"])),
                strategy=str(subs_raw.get("strategy", "most-downloaded")),
                filters=SubtitleFilters(
                    hearing_impaired=str(subs_filters.get("hearing_impaired", "exclude")),
                    exclude_ai=bool(subs_filters.get("exclude_ai", True)),
                    exclude_machine=bool(subs_filters.get("exclude_machine", True)),
                ),
                search_delay_secs=float(subs_raw.get("search_delay_secs", 0.5)),
                download_delay_secs=float(subs_raw.get("download_delay_secs", 1.0)),
            ),
        )

    @classmethod
    def load(cls, path: str = DEFAULT_DAV_CONFIG_PATH) -> "DavConfig":
        with open(path, "r", encoding="utf-8") as handle:
            base = yaml.safe_load(handle) or {}

        state_dir = str(base.get("state_dir", "/app/data"))
        overrides_env = os.environ.get("BUZZ_OVERRIDES", "")
        overrides_path = Path(overrides_env) if overrides_env else Path(state_dir) / "buzz.overrides.yml"

        overrides = {}
        if overrides_path.exists():
            with open(overrides_path, "r", encoding="utf-8") as handle:
                overrides = yaml.safe_load(handle) or {}

        merged = deep_merge(base, overrides)
        config = cls._from_merged_dict(merged)
        config._overrides_path = overrides_path.resolve()
        config._raw_merged = merged
        return config


class PresentationConfig(BaseModel):
    bind: str = Field(
        default_factory=lambda: os.environ.get("PRESENTATION_BIND", "0.0.0.0")
    )
    port: int = Field(
        default_factory=lambda: int(os.environ.get("PRESENTATION_PORT", "8400"))
    )
    source_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PRESENTATION_SOURCE_ROOT", "/mnt/buzz/raw")
        )
    )
    target_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PRESENTATION_TARGET_ROOT", "/mnt/buzz/curated")
        )
    )
    state_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PRESENTATION_STATE_ROOT", "/state")
        )
    )
    overrides_path: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PRESENTATION_OVERRIDES", "/config/overrides.yml")
        )
    )
    jellyfin_url: str = Field(
        default_factory=lambda: os.environ.get(
            "JELLYFIN_URL", "http://jellyfin:8096"
        ).rstrip("/")
    )
    jellyfin_api_key: str = Field(
        default_factory=lambda: os.environ.get("JELLYFIN_API_KEY", "")
    )
    jellyfin_scan_task_id: str = Field(
        default_factory=lambda: os.environ.get("JELLYFIN_SCAN_TASK_ID", "")
    )
    jellyfin_library_map: dict[str, str] = Field(
        default_factory=lambda: json.loads(
            os.environ.get(
                "JELLYFIN_LIBRARY_MAP",
                '{"movies": "Movies", "shows": "TV Shows", "anime": "Anime"}',
            )
        )
    )
    skip_jellyfin_scan: bool = Field(
        default_factory=lambda: (
            os.environ.get("PRESENTATION_SKIP_JELLYFIN_SCAN", "").lower()
            in {"1", "true", "yes"}
        )
    )
    build_on_start: bool = Field(
        default_factory=lambda: (
            os.environ.get("PRESENTATION_BUILD_ON_START", "true").lower()
            in {"1", "true", "yes"}
        )
    )
    verbose: bool = Field(
        default_factory=lambda: (
            os.environ.get("PRESENTATION_VERBOSE", "").lower() in {"1", "true", "yes"}
        )
    )
    log_max_entries: int = Field(
        default_factory=lambda: int(os.environ.get("PRESENTATION_LOG_MAX_ENTRIES", "1000"))
    )
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)
    subtitle_root: Path = Field(
        default_factory=lambda: Path(os.environ.get("SUBTITLE_ROOT", "/mnt/buzz/subs"))
    )

    def __init__(self, **data):
        # If subtitles aren't explicitly passed, try loading from buzz.yml
        if "subtitles" not in data:
            try:
                path = os.environ.get("BUZZ_CONFIG", "/app/buzz.yml")
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        raw = yaml.safe_load(handle) or {}
                    
                    subs_raw = raw.get("subtitles")
                    if subs_raw:
                        opensubs = subs_raw.get("opensubtitles", {})
                        subs_filters = subs_raw.get("filters", {})
                        data["subtitles"] = SubtitleConfig(
                            enabled=bool(subs_raw.get("enabled", False)),
                            fetch_on_resync=bool(subs_raw.get("fetch_on_resync", False)),
                            api_key=str(opensubs.get("api_key", "")),
                            username=str(opensubs.get("username", "")),
                            password=str(opensubs.get("password", "")),
                            languages=list(subs_raw.get("languages", ["en"])),
                            strategy=str(subs_raw.get("strategy", "most-downloaded")),
                            filters=SubtitleFilters(
                                hearing_impaired=str(subs_filters.get("hearing_impaired", "exclude")),
                                exclude_ai=bool(subs_filters.get("exclude_ai", True)),
                                exclude_machine=bool(subs_filters.get("exclude_machine", True)),
                            ),
                            search_delay_secs=float(subs_raw.get("search_delay_secs", 0.5)),
                            download_delay_secs=float(subs_raw.get("download_delay_secs", 1.0)),
                        )
            except Exception as exc:
                print(f"Warning: Failed to load subtitles from buzz.yml: {exc}")

        # Fallback to environment variables if still not set
        if "subtitles" not in data:
            data["subtitles"] = SubtitleConfig(
                enabled=os.environ.get("SUBTITLE_ENABLED", "").lower() in {"1", "true", "yes"},
                fetch_on_resync=os.environ.get("SUBTITLE_FETCH_ON_RESYNC", "").lower() in {"1", "true", "yes"},
                api_key=os.environ.get("OPENSUBTITLES_API_KEY", ""),
                username=os.environ.get("OPENSUBTITLES_USERNAME", ""),
                password=os.environ.get("OPENSUBTITLES_PASSWORD", ""),
                languages=[
                    lang.strip()
                    for lang in os.environ.get("SUBTITLE_LANGUAGES", "en").split(",")
                    if lang.strip()
                ],
                strategy=os.environ.get("SUBTITLE_STRATEGY", "most-downloaded"),
            )
        super().__init__(**data)


class ErrorResponse(BaseModel):
    error: str


class AddTorrentRequest(BaseModel):
    magnet: str

    @field_validator("magnet")
    @classmethod
    def validate_magnet(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Missing magnet link")
        return value


class SelectFilesRequest(BaseModel):
    torrent_id: str
    file_ids: list[str]

    @field_validator("torrent_id")
    @classmethod
    def validate_torrent_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Missing torrent_id")
        return value


class DeleteTorrentRequest(BaseModel):
    torrent_id: str

    @field_validator("torrent_id")
    @classmethod
    def validate_torrent_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Missing torrent_id")
        return value


class RestoreTrashRequest(BaseModel):
    hash: str

    @field_validator("hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Missing hash")
        return value


class DeleteTrashRequest(BaseModel):
    hash: str

    @field_validator("hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Missing hash")
        return value
