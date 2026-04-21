import os
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .core.constants import DEFAULT_ANIME_PATTERN

DEFAULT_DAV_CONFIG_PATH = os.environ.get("BUZZ_CONFIG", "/app/buzz.yml")


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
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)

    @classmethod
    def load(cls, path: str = DEFAULT_DAV_CONFIG_PATH) -> "DavConfig":
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        provider = raw.get("provider", {})
        server = raw.get("server", {})
        hooks = raw.get("hooks", {})
        directories = raw.get("directories", {})
        anime = directories.get("anime", {})
        compat = raw.get("compat", {})
        logging = raw.get("logging", {})
        subs_raw = raw.get("subtitles", {})
        opensubs = subs_raw.get("opensubtitles", {})
        subs_filters = subs_raw.get("filters", {})

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
            verbose=bool(logging.get("verbose", False)),
            log_max_entries=int(logging.get("max_entries", 1000)),
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
