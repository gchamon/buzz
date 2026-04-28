"""Jellyfin media server integration helpers."""

import json
from dataclasses import dataclass
from urllib import error, request

from ..models import CuratorConfig
from .events import record_event
from .state import is_internal_category


@dataclass(frozen=True)
class JellyfinAuthProbe:
    """Result of a Jellyfin API auth/reachability probe."""

    valid: bool
    invalid_token: bool = False
    unreachable: bool = False
    error: str = ""


def discover_scan_task_id(config: CuratorConfig) -> str:
    """Return the Jellyfin scan task ID, discovering it if necessary."""
    if config.jellyfin_scan_task_id:
        return config.jellyfin_scan_task_id
    req = request.Request(
        f"{config.jellyfin_url}/ScheduledTasks?IsHidden=false&IsEnabled=true",
        headers={
            "Authorization": f"MediaBrowser Token={config.jellyfin_api_key}"
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            tasks = json.load(response)
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(
                "Jellyfin API Token is invalid or unauthorized"
            ) from exc
        raise
    for task in tasks:
        if task.get("Name") == "Scan Media Library":
            return task.get("Id", "")
    raise RuntimeError(
        "Unable to find the Jellyfin Scan Media Library task ID."
    )


def validate_jellyfin_auth(config: CuratorConfig) -> bool:
    """Verify that the Jellyfin API key is valid."""
    return probe_jellyfin_auth(config).valid


def probe_jellyfin_auth(config: CuratorConfig) -> JellyfinAuthProbe:
    """Check Jellyfin API auth while preserving failure type."""
    req = request.Request(
        f"{config.jellyfin_url}/System/Info",
        headers={
            "Authorization": f"MediaBrowser Token={config.jellyfin_api_key}"
        },
    )
    try:
        with request.urlopen(req, timeout=10):
            return JellyfinAuthProbe(valid=True)
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            return JellyfinAuthProbe(
                valid=False,
                invalid_token=True,
                error="Jellyfin API Token is invalid or unauthorized",
            )
        return JellyfinAuthProbe(valid=False, error=str(exc))
    except error.URLError as exc:
        return JellyfinAuthProbe(
            valid=False,
            unreachable=True,
            error=str(exc.reason),
        )
    except Exception as exc:
        return JellyfinAuthProbe(valid=False, error=str(exc))


def discover_jellyfin_libraries(config: CuratorConfig) -> dict[str, str]:
    """Return a map of library Name -> ItemId."""
    req = request.Request(
        f"{config.jellyfin_url}/Library/VirtualFolders",
        headers={
            "Authorization": f"MediaBrowser Token={config.jellyfin_api_key}"
        },
    )
    with request.urlopen(req, timeout=30) as response:
        libraries = json.load(response)
    return {
        lib.get("Name"): lib.get("ItemId")
        for lib in libraries
        if lib.get("Name") and lib.get("ItemId")
    }


def trigger_jellyfin_scan(config: CuratorConfig) -> None:
    """Trigger a full Jellyfin media library scan."""
    task_id = discover_scan_task_id(config)
    record_event("triggering full Jellyfin media library scan...", level="info")
    req = request.Request(
        f"{config.jellyfin_url}/ScheduledTasks/Running/{task_id}",
        method="POST",
        headers={
            "Authorization": f"MediaBrowser Token={config.jellyfin_api_key}"
        },
    )
    with request.urlopen(req, timeout=30):
        return


def trigger_jellyfin_selective_refresh(
    config: CuratorConfig, changed_roots: list[str]
) -> None:
    """Trigger selective refreshes for Jellyfin libraries matching changed roots."""
    if not changed_roots:
        return

    categories = {root.split("/")[0] for root in changed_roots if "/" in root}
    # Filter out internal/virtual categories like __unplayable__ that
    # shouldn't trigger scans.
    categories = {cat for cat in categories if not is_internal_category(cat)}

    if not categories:
        return

    library_names = {
        config.jellyfin_library_map.get(cat)
        for cat in categories
        if cat in config.jellyfin_library_map
    }
    library_names = {name for name in library_names if name}

    # If all categories are known but none map to a library
    # (e.g. __unplayable__), just skip instead of falling back to a full
    # scan.
    if not library_names and all(
        cat in config.jellyfin_library_map for cat in categories
    ):
        record_event(
            "no Jellyfin libraries mapped for categories: "
            f"{categories}. skipping refresh.",
            level="info",
        )
        return

    if not library_names:
        record_event(
            "unknown categories "
            f"{categories} (not in media_server.library_map). "
            "falling back to full scan.",
            level="warning",
        )
        trigger_jellyfin_scan(config)
        return

    libraries = discover_jellyfin_libraries(config)
    for name in library_names:
        library_id = libraries.get(name)
        if not library_id:
            record_event(
                f"Jellyfin library '{name}' not found. "
                "falling back to full scan.",
                level="warning",
            )
            trigger_jellyfin_scan(config)
            return

        record_event(
            f"triggering selective refresh for Jellyfin library "
            f"'{name}' ({library_id})...",
            level="info",
        )
        query = (
            "Recursive=true&ImageRefreshMode=Default"
            "&MetadataRefreshMode=Default&ReplaceAllImages=false"
            "&ReplaceAllMetadata=false"
        )
        req = request.Request(
            f"{config.jellyfin_url}/Items/{library_id}/Refresh?{query}",
            method="POST",
            headers={
                "Authorization": f"MediaBrowser Token={config.jellyfin_api_key}"
            },
        )
        try:
            with request.urlopen(req, timeout=30) as resp:
                _ = resp.read()
        except Exception as exc:
            msg = f"failed to refresh Jellyfin library '{name}': {exc}"
            record_event(msg, level="error")
            raise RuntimeError(msg) from exc
