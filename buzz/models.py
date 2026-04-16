import os
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .core.constants import DEFAULT_ANIME_PATTERN

DEFAULT_DAV_CONFIG_PATH = os.environ.get("BUZZ_CONFIG", "/app/buzz.yml")


class DavConfig(BaseModel):
    token: str
    poll_interval_secs: int = 10
    bind: str = "0.0.0.0"
    port: int = 9999
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

        token = provider.get("token", "").strip()
        if not token:
            raise ValueError("provider.token is required.")

        return cls(
            token=token,
            poll_interval_secs=int(raw.get("poll_interval_secs", 10)),
            bind=str(server.get("bind", "0.0.0.0")),
            port=int(server.get("port", 9999)),
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
