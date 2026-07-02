"""DAV filesystem state, library builder, and background sync threads."""

import contextlib
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib import parse, request

import httpx

from ..models import DavConfig, TaskStatus, category_definitions
from . import db
from .constants import SHOW_PATTERNS
from .events import record_event
from .events import registry as event_registry
from .media import is_video_file, parse_movie, parse_show
from .providers import (
    ProviderClient,
    ProviderDeleteError,
    ProviderStreamError,
    ProviderTorrentDetail,
    is_local_stream_ref,
    split_provider_torrent_id,
)
from .providers import (
    ProviderTorrentSummary as ProviderSummary,
)
from .utils import (
    magnet_display_name,
    normalize_posix_path,
    stable_json,
    utc_now_iso,
)

# Named aliases for the opaque dicts flowing through this module.
type SnapshotNode = dict[str, Any]
type Snapshot = dict[str, Any]
type TorrentInfo = dict[str, Any]
type TorrentSummary = dict[str, Any]
type SyncReport = dict[str, Any]
type StatusReport = dict[str, Any]
type ChangeClassification = dict[str, list[str]]
type OperationResult = dict[str, Any]
type CacheSelection = dict[str, list[str]]
type InfringingCandidate = dict[str, Any]
type MigrationCandidate = dict[str, Any]


# Stable epoch used for synthetic nodes (version.txt etc.) whose mtime must
# not churn. Real-Debrid does not expose per-file mtimes, so file nodes use
# the parent torrent's `added` timestamp instead — see _torrent_modified_iso.
STABLE_EPOCH_ISO = "1970-01-01T00:00:00Z"
_HASH_RE = re.compile(r"^[a-fA-F0-9]{32,64}$")
_PROVIDER_ID_RE = re.compile(
    r"\[(imdbid|tmdbid|tvdbid|anidbid)-([^\]]+)\]",
    re.IGNORECASE,
)
_PROVIDER_ID_PRIORITY = ("imdbid", "tmdbid", "tvdbid", "anidbid")
_JELLYFIN_PROVIDER_ID_KEYS = {
    "imdb": "imdbid",
    "imdbid": "imdbid",
    "tmdb": "tmdbid",
    "tmdbid": "tmdbid",
    "tvdb": "tvdbid",
    "tvdbid": "tvdbid",
    "anidb": "anidbid",
    "anidbid": "anidbid",
}


def _is_hash_name(value: object) -> bool:
    return bool(_HASH_RE.fullmatch(str(value or "").strip()))


def _valid_provider_name(value: object) -> str:
    """Return value as a name string, or '' if absent or looks like a raw hash."""
    s = str(value or "").strip()
    return "" if not s or _is_hash_name(s) else s


def _provider_ids_from_text(value: str) -> dict[str, str]:
    """Extract Jellyfin-style provider IDs from a filename or folder name."""
    ids: dict[str, str] = {}
    for match in _PROVIDER_ID_RE.finditer(value):
        provider = match.group(1).strip().lower()
        identifier = match.group(2).strip()
        if provider and identifier:
            ids[provider] = identifier
    return ids


def _safe_file_root_name(info: TorrentInfo) -> str:
    """Infer a readable root name from selected files when it is unambiguous."""
    roots: set[str] = set()
    selected_files = [
        item
        for item in info.get("files", [])
        if isinstance(item, dict) and item.get("selected")
    ]
    if not selected_files:
        return ""
    for item in selected_files:
        path = normalize_posix_path(str(item.get("path") or ""))
        if not path:
            continue
        parts = [part for part in path.split("/") if part]
        if len(parts) > 1 or len(selected_files) == 1:
            roots.add(parts[0])
    if len(roots) != 1:
        return ""
    return _valid_provider_name(next(iter(roots)))


def _torrent_modified_iso(info: TorrentInfo) -> str:
    """Return a stable ISO-8601 mtime derived from the torrent's `added` field.

    Real-Debrid returns `added` as an ISO-8601 timestamp that does not change
    once the torrent is in the user's account. Using it as the per-file mtime
    means Jellyfin's "File changed" detection only fires when content really
    changes, instead of on every DAV snapshot rebuild.

    The value is parsed through `datetime.fromisoformat` and re-emitted in a
    canonical `YYYY-MM-DDTHH:MM:SSZ` form, so any minor format drift from RD
    (offset suffix, fractional seconds, naive form) lands on the same string.
    Falls back to `STABLE_EPOCH_ISO` if the field is missing or unparseable.
    """
    raw = info.get("added")
    if not isinstance(raw, str) or not raw.strip():
        return STABLE_EPOCH_ISO
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return STABLE_EPOCH_ISO
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# RD application errors that don't recover within seconds: caching the
# failure for a TTL prevents pointless API hammering on every client retry.
RD_NON_TRANSIENT_ERRORS = frozenset({
    "hoster_unavailable",
    "hoster_unsupported",
    "hoster_too_many_active_downloads",
    "hoster_not_free",
    "file_unavailable",
    "infringing_file",
})

PROVIDER_TRANSIENT_STREAM_ERRORS = frozenset({
    "http_429",
})

# Local copy pipeline tuning: stream chunk size, how many bytes may be
# written between disk-usage cap re-checks, and how long to wait for a
# restored torrent to become streamable before giving up.
LOCAL_COPY_CHUNK_BYTES = 1024 * 1024
LOCAL_COPY_RECHECK_BYTES = 64 * 1024 * 1024
LOCAL_COPY_RESTORE_WAIT_SECS = 120
LOCAL_COPY_RESTORE_POLL_SECS = 5

PROVIDER_NON_TRANSIENT_STREAM_ERRORS = frozenset({
    "http_422",
})


class HosterUnavailableError(ValueError):
    """RD reported a non-transient hoster/file error.

    Raised by :meth:`BuzzState.resolve_download_url` when Real-Debrid's
    ``unrestrict.link`` endpoint returns a successful response whose
    ``error`` field is one of :data:`RD_NON_TRANSIENT_ERRORS`. The caller
    should not retry within seconds; a negative cache entry is stored so
    repeat calls short-circuit without a fresh API hit.
    """

    def __init__(
        self, source_url: str, code: str, *, cached: bool = False
    ) -> None:
        """Build a HosterUnavailableError tagged with the upstream RD code."""
        super().__init__(
            f"Real-Debrid hoster unavailable for {source_url}: {code}"
        )
        self.source_url = source_url
        self.code = code
        self.cached = cached


class ProviderStreamResolutionError(ValueError):
    """Provider stream resolution failed and should be cached briefly."""

    def __init__(
        self, source_url: str, provider: str, code: str, *, cached: bool = False
    ) -> None:
        """Initialize the error with source URL, provider and error code."""
        super().__init__(
            f"{provider} stream resolution unavailable for {source_url}: {code}"
        )
        self.source_url = source_url
        self.provider = provider
        self.code = code
        self.cached = cached


def raise_if_cancelled(cancel_event: threading.Event) -> None:
    """Raise the task-pool cancellation sentinel when cancellation is set."""
    if cancel_event.is_set():
        raise RuntimeError("cancelled")


@dataclass
class BackgroundTask:
    """Status and cancellation handle for background state work."""

    id: str
    kind: str
    label: str
    cancel_event: threading.Event
    work: Callable[[str, threading.Event], None] | None = None
    status: TaskStatus = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    auto_complete: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return a UI-safe task snapshot."""
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "auto_complete": self.auto_complete,
            "cancellable": self.status in {"pending", "queued", "running"},
            "startable": self.status == "pending",
            "logs": self.logs(),
        }

    def logs(self) -> list[dict[str, Any]]:
        """Return events emitted while this task was active."""
        return [
            event
            for event in event_registry.get_recent(limit=500)
            if event.get("task_id") == self.id
        ]


@dataclass
class BackgroundTaskPool:
    """Small thread-backed task pool with cooperative cancellation."""

    on_change: Callable[[], None] | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _tasks: dict[str, BackgroundTask] = field(default_factory=dict)

    def submit(
        self,
        kind: str,
        label: str,
        work: Callable[[str, threading.Event], None],
        auto_complete: bool = True,
    ) -> str:
        """Run work in the background and return its task id."""
        task_id = uuid.uuid4().hex[:12]
        task = self._new_task(task_id, kind, label, work, auto_complete=auto_complete)
        with self._lock:
            self._tasks[task_id] = task
        self._notify_change()
        thread = threading.Thread(
            target=self._run,
            args=(task_id, work),
            daemon=True,
        )
        thread.start()
        return task_id

    def submit_manual(
        self,
        kind: str,
        label: str,
        work: Callable[[str, threading.Event], None],
        auto_complete: bool = True,
    ) -> str:
        """Register work for explicit user acceptance before it starts."""
        task_id = uuid.uuid4().hex[:12]
        task = self._new_task(task_id, kind, label, work, auto_complete=auto_complete)
        task.status = "pending"
        with self._lock:
            self._tasks[task_id] = task
        self._notify_change()
        return task_id

    def complete(
        self,
        task_id: str,
        error: str | None = None,
        status: TaskStatus | None = None,
    ) -> bool:
        """Manually transition a task to complete or failed status."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in {"running", "cancelling"}:
                return False

            if status:
                task.status = status
            else:
                task.status = "failed" if error else "complete"
            task.error = error
            task.finished_at = utc_now_iso()

        self._notify_change()
        record_event(
            f"thread {task.status}: {task.label}",
            event=f"thread_{task.status}",
            task_kind=task.kind,
            task_id=task_id,
        )
        return True

    def start(self, task_id: str) -> bool:
        """Start a pending manual task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "pending" or task.work is None:
                return False
            task.status = "queued"
            work = task.work
        self._notify_change()
        thread = threading.Thread(
            target=self._run,
            args=(task_id, work),
            daemon=True,
        )
        thread.start()
        return True

    def cancel(self, task_id: str) -> bool:
        """Request cancellation for a queued or running task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in {"pending", "queued", "running"}:
                return False
            if task.status == "pending":
                task.cancel_event.set()
                task.status = "cancelled"
                task.finished_at = utc_now_iso()
                self._notify_change()
                return True
            task.cancel_event.set()
            task.status = "cancelling"
        self._notify_change()
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        """Return active and recent task status."""
        with self._lock:
            tasks = list(self._tasks.values())
        return [task.as_dict() for task in tasks[-20:]]

    def _run(
        self,
        task_id: str,
        work: Callable[[str, threading.Event], None],
    ) -> None:
        with self._lock:
            task = self._tasks[task_id]
            if task.cancel_event.is_set():
                task.status = "cancelled"
                task.finished_at = utc_now_iso()
                self._notify_change()
                return
            task.status = "running"
            task.started_at = utc_now_iso()
        self._notify_change()
        with event_registry.task_context(task_id):
            try:
                try:
                    record_event(
                        f"thread started: {task.label}",
                        event="thread_started",
                        task_kind=task.kind,
                    )
                    work(task_id, task.cancel_event)
                except RuntimeError as exc:
                    with self._lock:
                        if task.cancel_event.is_set():
                            task.status = "cancelled"
                        else:
                            task.status = "failed"
                            task.error = str(exc)
                    level = "info" if task.cancel_event.is_set() else "error"
                    record_event(
                        f"thread {task.status}: {task.label}",
                        level=level,
                        event=f"thread_{task.status}",
                        task_kind=task.kind,
                    )
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        task.status = "failed"
                        task.error = str(exc)
                    record_event(
                        f"thread failed: {task.label}: {exc}",
                        level="error",
                        event="thread_failed",
                        task_kind=task.kind,
                    )
                else:
                    if task.auto_complete:
                        with self._lock:
                            task.status = (
                                "cancelled"
                                if task.cancel_event.is_set()
                                else "complete"
                            )
                        record_event(
                            f"thread {task.status}: {task.label}",
                            event=f"thread_{task.status}",
                            task_kind=task.kind,
                        )
            finally:
                if task.auto_complete or task.status in {"failed", "cancelled"}:
                    with self._lock:
                        task.finished_at = utc_now_iso()
                    self._notify_change()

    @staticmethod
    def _new_task(
        task_id: str,
        kind: str,
        label: str,
        work: Callable[[str, threading.Event], None],
        auto_complete: bool = True,
    ) -> BackgroundTask:
        return BackgroundTask(
            id=task_id,
            kind=kind,
            label=label,
            cancel_event=threading.Event(),
            work=work,
            auto_complete=auto_complete,
        )

    def _notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change()


def dav_rel_path(raw_path: str) -> str:
    """Strip the /dav prefix and URL-decode a raw DAV path."""
    path = parse.urlsplit(raw_path).path
    if path.startswith("/dav"):
        path = path[len("/dav") :]
    return normalize_posix_path(parse.unquote(path))


def split_path(value: str) -> tuple[str, ...]:
    """Split a DAV path into non-empty components."""
    normalized = normalize_posix_path(value)
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part)


def canonical_snapshot(snapshot: Snapshot) -> dict:
    """Return a snapshot with all volatile fields stripped for fingerprinting."""
    files = {}
    for path, node in snapshot.get("files", {}).items():
        if not isinstance(node, dict):
            files[path] = node
            continue
        # Strip volatile fields: 'modified' (timestamps) and 'etag' (which can
        # change if the server labels drift or if it contains volatile fields).
        # Fingerprint strictly on structure and source pointers.
        files[path] = {
            k: v for k, v in node.items() if k not in ("modified", "etag")
        }

    return {
        "dirs": snapshot.get("dirs", []),
        "files": files,
        # Completely exclude the 'report' object from the fingerprint as it
        # contains timestamps and shifting counts that aren't structural.
    }


def is_internal_category(name: str) -> bool:
    """Return True for virtual categories like __all__ and __unplayable__."""
    return name.startswith("__")


class LibraryBuilder:
    """Builds a DAV filesystem snapshot from Real-Debrid torrent info."""

    def __init__(self, config: DavConfig) -> None:
        """Initialize with the DAV configuration."""
        self.config = config
        self.category_definitions = category_definitions(config.categories)
        self.category_kinds = {
            definition["name"]: definition["kind"]
            for definition in self.category_definitions
        }
        self.anime_regexes = tuple(
            re.compile(pattern) for pattern in config.anime_patterns
        )

    def build(
        self, infos: list[TorrentInfo]
    ) -> tuple[Snapshot, list[str]]:
        """Build a snapshot and sorted root list from torrent infos."""
        dirs: set[str] = {""}
        files: dict[str, SnapshotNode] = {
            "version.txt": {
                "type": "memory",
                "content": self.config.version_label + "\n",
                "size": len(self.config.version_label) + 1,
                "mime_type": "text/plain; charset=utf-8",
                "modified": STABLE_EPOCH_ISO,
                "etag": self._etag("version.txt", self.config.version_label),
            }
        }
        report = {
            "movies": 0,
            "show_files": 0,
            "anime_files": 0,
            "unplayable_files": 0,
            "torrents": len(infos),
            "generated_at": utc_now_iso(),
        }
        current_roots: set[str] = set()
        for info in infos:
            torrent_name = self._torrent_name(info)
            selected = self._selected_files(info)
            playable = [item for item in selected if is_video_file(item["path"])]
            linked_playable = [
                item
                for item in playable
                if item.get("url") and info.get("status") == "downloaded"
            ]
            modified = _torrent_modified_iso(info)
            if linked_playable:
                self._add_playable_tree(
                    files,
                    dirs,
                    torrent_name,
                    linked_playable,
                    report,
                    current_roots,
                    modified,
                )
            elif self.config.enable_unplayable_dir:
                self._add_unplayable_entry(
                    files,
                    dirs,
                    torrent_name,
                    selected,
                    info,
                    report,
                    current_roots,
                    modified,
                )

        snapshot = {
            "generated_at": report["generated_at"],
            "dirs": sorted(dirs),
            "files": files,
            "report": report,
        }
        return snapshot, sorted(current_roots)

    def _add_playable_tree(
        self,
        files: dict[str, SnapshotNode],
        dirs: set[str],
        torrent_name: str,
        linked_playable: list[dict],
        report: dict,
        current_roots: set[str],
        modified: str,
    ) -> None:
        category = self._category_for(linked_playable)
        self._add_tree(files, dirs, category, torrent_name, linked_playable, modified)
        if self.config.enable_all_dir:
            self._add_tree(
                files, dirs, "__all__", torrent_name, linked_playable, modified
            )
        current_roots.add(f"{category}/{torrent_name}")
        kind = self.category_kind(category)
        if kind == "movie":
            report["movies"] += len(linked_playable)
        elif kind == "show":
            report["show_files"] += len(linked_playable)
        else:
            report["anime_files"] += len(linked_playable)

    def _add_unplayable_entry(
        self,
        files: dict[str, SnapshotNode],
        dirs: set[str],
        torrent_name: str,
        selected: list[dict],
        info: TorrentInfo,
        report: dict,
        current_roots: set[str],
        modified: str,
    ) -> None:
        reason = self._unplayable_reason(info, selected)
        count = self._add_unplayable_tree(
            files, dirs, torrent_name, selected, reason, modified
        )
        if count:
            current_roots.add(f"__unplayable__/{torrent_name}")
            report["unplayable_files"] += count

    def _selected_files(self, info: TorrentInfo) -> list[TorrentInfo]:
        selected = [item for item in info.get("files", []) if item.get("selected")]
        links = list(info.get("links") or [])
        link_iter = iter(links)
        results = []
        for item in selected:
            entry = {
                "path": str(item.get("path", "")),
                "bytes": int(item.get("bytes", 0)),
                "id": item.get("id"),
                "url": next(link_iter, ""),
                "category_override": info.get("category_override"),
            }
            results.append(entry)
        return results

    def _torrent_name(self, info: TorrentInfo) -> str:
        name = str(
            info.get("display_name")
            or info.get("original_filename")
            or info.get("filename")
            or info.get("id")
            or "torrent"
        ).strip()
        name = name.replace("/", " ").replace("\\", " ").strip(". ")
        return name or str(info.get("id") or "torrent")

    def _category_for(self, entries: list[TorrentInfo]) -> str:
        override = str(
            entries[0].get("category_override") if entries else ""
        ).strip()
        if override in self.category_kinds:
            return override
        for entry in entries:
            rel = entry["path"]
            if any(pattern.search(rel) for pattern in self.anime_regexes):
                return "anime"
        for entry in entries:
            if any(pattern.search(entry["path"]) for pattern in SHOW_PATTERNS):
                return "shows"
        return "movies"

    def category_kind(self, category: str) -> str:
        """Return the behavior kind for a configured category name."""
        return self.category_kinds.get(category, "")

    def category_name_for_kind(self, kind: str) -> str:
        """Return the first configured category name for a given kind."""
        for definition in self.category_definitions:
            if definition["kind"] == kind:
                return definition["name"]
        return ""

    def _add_tree(
        self,
        files: dict[str, SnapshotNode],
        dirs: set[str],
        prefix: str,
        torrent_name: str,
        entries: list[TorrentInfo],
        modified: str,
    ) -> None:
        root = f"{prefix}/{torrent_name}"
        self._ensure_dirs(dirs, root)
        for entry in entries:
            rel = normalize_posix_path(entry["path"])
            if not rel:
                continue
            path = f"{root}/{rel}"
            self._ensure_dirs(dirs, posixpath.dirname(path))
            files[path] = {
                "type": "remote",
                "size": int(entry["bytes"]),
                "source_url": entry["url"],
                "mime_type": mimetypes.guess_type(rel)[0] or "application/octet-stream",
                "modified": modified,
                "etag": self._etag(path, entry["url"], entry["bytes"]),
            }

    def _add_unplayable_tree(
        self,
        files: dict[str, SnapshotNode],
        dirs: set[str],
        torrent_name: str,
        entries: list[TorrentInfo],
        reason: str,
        modified: str,
    ) -> int:
        root = f"__unplayable__/{torrent_name}"
        self._ensure_dirs(dirs, root)
        summary_content = (
            json.dumps(
                {
                    "reason": reason,
                    "status": "unplayable",
                    "files": [normalize_posix_path(item["path"]) for item in entries],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        files[f"{root}/__buzz__.json"] = {
            "type": "memory",
            "content": summary_content,
            "size": len(summary_content.encode("utf-8")),
            "mime_type": "application/json; charset=utf-8",
            "modified": modified,
            "etag": self._etag(root, reason, summary_content),
        }
        count = 1
        for entry in entries:
            rel = normalize_posix_path(entry["path"])
            if not rel:
                continue
            path = f"{root}/{rel}"
            self._ensure_dirs(dirs, posixpath.dirname(path))
            files[path] = {
                "type": "memory",
                "content": "",
                "size": 0,
                "mime_type": "application/octet-stream",
                "modified": modified,
                "etag": self._etag(path, reason, entry["bytes"]),
            }
            count += 1
        return count

    def _ensure_dirs(self, dirs: set[str], path: str) -> None:
        current = normalize_posix_path(path)
        while True:
            dirs.add(current)
            if not current:
                break
            current = posixpath.dirname(current)
            if current == ".":
                current = ""

    def _unplayable_reason(
        self, info: TorrentInfo, selected: list[TorrentInfo]
    ) -> str:
        if not selected:
            return "no_selected_files"
        if info.get("status") != "downloaded":
            return f"status={info.get('status', 'unknown')}"
        if not any(is_video_file(item["path"]) for item in selected):
            return "no_playable_video_files"
        return "missing_download_link"

    def _etag(self, *parts: Any) -> str:
        digest = hashlib.sha256()
        for part in parts:
            digest.update(str(part).encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()


class BuzzState:
    """Thread-safe cache of torrent state, snapshot, and Real-Debrid sync."""

    def __init__(
        self,
        config: DavConfig,
        client: Any,
        on_ui_change: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize state storage and load persisted data from disk."""
        self.config = config
        if isinstance(client, dict):
            self.clients = dict(client)
            self.client = next(iter(self.clients.values()), None)
        else:
            self.client = client
            self.clients = {"real_debrid": client} if client is not None else {}
        self.builder = LibraryBuilder(config)
        self.category_kinds = dict(self.builder.category_kinds)
        self.lock = threading.RLock()
        self._poller: Poller | None = None
        self.state_dir = config.state_dir
        os.makedirs(self.state_dir, exist_ok=True)
        db_path = Path(self.state_dir) / "buzz.sqlite"
        self.conn = db.connect(db_path)
        db.apply_migrations(self.conn)
        db.migrate_legacy_files(self.conn, Path(self.state_dir))
        self.cache = self._load_cache()
        # Full per-provider cache mirroring the persisted ``torrents`` table.
        # ``self.cache`` is deduplicated to one winning entry per hash for
        # serving, which drops losing-provider entries. Cache-hit detection
        # during sync needs every provider's signature, so it consults this
        # full mirror instead (otherwise non-winning providers refetch every
        # cycle in long-lived processes like buzz-dav).
        self._full_cache = dict(self.cache)
        self.archive = self._load_archive()
        snapshot, digest = self._load_snapshot()
        self.snapshot = snapshot
        self._snapshot_index_source_id = 0
        self._rebuild_snapshot_indexes()
        self.snapshot_digest = digest
        self.snapshot_loaded = self._snapshot_exists_in_db()
        self.last_sync_at = None
        self.last_report = {}
        self.last_error = None
        self.provider_degraded = False
        self.sync_in_progress = False
        self.startup_sync_complete = False
        self.hook_pending_paths: list[str] = []
        self.hook_in_progress = False
        self.hook_last_started_at = None
        self.hook_last_finished_at = None
        self.hook_last_error = None
        self.hook_phase = "idle"
        self.hook_active_paths: list[str] = []
        self.hook_wait_started_at = None
        self.resolved_urls: dict[str, dict[str, Any]] = {}
        self.stream_sources: dict[str, list[dict[str, str]]] = {}
        self._resolve_locks: dict[str, threading.Lock] = {}
        self.hook_lock = threading.Lock()
        self.hook_task_active = False
        self.background_tasks = BackgroundTaskPool(
            on_change=lambda: self._notify_ui_change("status")
        )
        # Persistent per-torrent file selection, keyed by torrent hash and
        # holding the set of selected normalized paths. Paths are portable
        # across providers (file ids are not), so this drives both RD and
        # TorBox library views and survives restarts.
        self.file_selections: dict[str, set[str]] = db.load_file_selections(
            self.conn
        )
        self.category_overrides: dict[str, str] = {
            thash: category
            for thash, category in db.load_category_overrides(self.conn).items()
            if category in self.category_kinds
        }
        self.category_names = tuple(self.category_kinds)
        # Per-file subtitle search query overrides, keyed by
        # (torrent hash, normalized in-torrent file path).
        self.subtitle_query_overrides: dict[tuple[str, str], str] = (
            db.load_subtitle_query_overrides(self.conn)
        )
        # Entry-level Curator naming overrides, keyed by torrent hash.
        self.curator_title_overrides: dict[str, dict[str, Any]] = (
            db.load_curator_title_overrides(self.conn)
        )
        # Favorited config sections (pinned to the top of the edit form).
        # The `subtitles` section is seeded by migration 8 on first run.
        self.config_favorites: set[str] = db.load_config_favorites(self.conn)
        self._file_selection_unresolved_warnings: set[tuple[str, str]] = set()
        self._closed = False
        self.on_ui_change = on_ui_change
        self._rebuild_stream_sources_from_cache()

    @staticmethod
    def _detail_to_info(detail: ProviderTorrentDetail) -> TorrentInfo:
        links = [file.stream_ref for file in detail.files if file.selected]
        return {
            "id": detail.id,
            "hash": detail.hash,
            "status": detail.status,
            "progress": detail.progress,
            "filename": detail.name,
            "original_filename": detail.original_name,
            "bytes": detail.bytes,
            "added": detail.added,
            "ended": detail.ended,
            "links": links,
            "files": [
                {
                    "id": file.id,
                    "path": file.path,
                    "bytes": file.bytes,
                    "selected": 1 if file.selected else 0,
                    "stream_ref": file.stream_ref,
                }
                for file in detail.files
            ],
        }

    @staticmethod
    def _summary_to_dict(summary: ProviderSummary) -> TorrentSummary:
        return {
            "id": summary.id,
            "filename": summary.name,
            "bytes": summary.bytes,
            "progress": summary.progress,
            "status": summary.status,
            "ended": summary.ended,
            "links": list(summary.stream_refs),
        }

    def _ordered_clients(self) -> list[tuple[str, Any]]:
        priority = getattr(self.config, "provider_priority", ("real_debrid",))
        ordered = [
            (provider, self.clients[provider])
            for provider in priority
            if provider in self.clients
        ]
        extras = [
            (provider, client)
            for provider, client in self.clients.items()
            if provider not in {item[0] for item in ordered}
        ]
        return ordered + extras

    def _fallback_clients(self) -> list[tuple[str, Any]]:
        """Ordered clients eligible as add/restore targets; never local."""
        return [
            (provider, client)
            for provider, client in self._ordered_clients()
            if provider != "local"
        ]

    @staticmethod
    def _cache_key(provider: str, torrent_id: str) -> str:
        return torrent_id if provider == "real_debrid" else f"{provider}:{torrent_id}"

    def _split_cache_key(self, torrent_id: str) -> tuple[str, str, str]:
        provider, provider_torrent_id = split_provider_torrent_id(torrent_id)
        cache_key = self._cache_key(provider, provider_torrent_id)
        return provider, provider_torrent_id, cache_key

    @staticmethod
    def _local_entry_key(info: TorrentInfo, fallback_id: str) -> str:
        value = str(info.get("hash") or "").strip().lower()
        return value or str(
            info.get("original_filename") or info.get("filename") or fallback_id
        ).strip().lower()

    def apply_config(self, config: DavConfig) -> None:
        """Swap in a refreshed runtime config for future operations."""
        with self.lock:
            self.config = config
            self.builder = LibraryBuilder(config)
            self.category_kinds = dict(self.builder.category_kinds)
            self.category_names = tuple(self.builder.category_kinds)
            self.category_overrides = {
                thash: category
                for thash, category in self.category_overrides.items()
                if category in self.builder.category_kinds
            }

    def category_kind(self, category: str) -> str:
        """Return the behavior kind for a configured category name."""
        return self.builder.category_kind(category)

    def category_name_for_kind(self, kind: str) -> str:
        """Return the first configured category name for a given kind."""
        return self.builder.category_name_for_kind(kind)

    def _rebuild_snapshot_indexes(self) -> None:
        """Rebuild derived lookup indexes for the active snapshot."""
        dirs = set(self.snapshot.get("dirs", []))
        files = self.snapshot.get("files", {})
        children_by_dir: dict[str, set[str]] = {}

        for directory in dirs:
            normalized = normalize_posix_path(directory)
            if not normalized:
                continue
            parent = posixpath.dirname(normalized)
            parent = "" if parent == "." else parent
            name = posixpath.basename(normalized)
            if name:
                children_by_dir.setdefault(parent, set()).add(name)

        for file_path in files:
            normalized = normalize_posix_path(file_path)
            if not normalized:
                continue
            parent = posixpath.dirname(normalized)
            parent = "" if parent == "." else parent
            name = posixpath.basename(normalized)
            if name:
                children_by_dir.setdefault(parent, set()).add(name)

        self._dirs_set = dirs
        self._children_by_dir = {
            directory: tuple(sorted(children))
            for directory, children in children_by_dir.items()
        }
        self._snapshot_index_source_id = id(self.snapshot)

    def _ensure_snapshot_indexes(self) -> None:
        """Refresh indexes after direct ``snapshot`` replacement."""
        if self._snapshot_index_source_id != id(self.snapshot):
            self._rebuild_snapshot_indexes()

    def _snapshot_exists_in_db(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM library_snapshot"
        ).fetchone()
        return bool(row[0])

    def _load_cache(self) -> dict:
        rows = self.conn.execute(
            "SELECT id, signature_json, info_json, magnet FROM torrents"
        ).fetchall()
        return {
            row["id"]: {
                "signature": json.loads(row["signature_json"]),
                "info": json.loads(row["info_json"]),
                "magnet": row["magnet"],
            }
            for row in rows
        }

    def _save_cache_entry(self, torrent_id: str, entry: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO torrents"
                " (id, signature_json, info_json, updated_at, magnet)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    torrent_id,
                    json.dumps(entry.get("signature", {})),
                    json.dumps(entry.get("info", {})),
                    utc_now_iso(),
                    entry.get("magnet"),
                ),
            )

    def _delete_cache_entry(self, torrent_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM torrents WHERE id = ?", (torrent_id,))

    def _save_cache(self, new_cache: dict) -> None:
        """Replace entire torrent cache atomically."""
        with self.conn:
            self.conn.execute("DELETE FROM torrents")
            for torrent_id, entry in new_cache.items():
                self.conn.execute(
                    "INSERT INTO torrents "
                    "(id, signature_json, info_json, updated_at, magnet) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        torrent_id,
                        json.dumps(entry.get("signature", {})),
                        json.dumps(entry.get("info", {})),
                        utc_now_iso(),
                        entry.get("magnet"),
                    ),
                )

    def _load_archive(self) -> dict:
        rows = self.conn.execute(
            "SELECT hash, name, bytes, files_json, deleted_at, magnet FROM archive"
        ).fetchall()
        return {
            row["hash"]: {
                "hash": row["hash"],
                "name": row["name"],
                "bytes": row["bytes"],
                "files": json.loads(row["files_json"] or "[]"),
                "deleted_at": row["deleted_at"],
                "magnet": row["magnet"],
            }
            for row in rows
        }

    def _save_archive_entry(self, thash: str, entry: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO archive"
                " (hash, name, bytes, files_json, deleted_at, magnet)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    thash,
                    entry.get("name"),
                    entry.get("bytes"),
                    json.dumps(entry.get("files", [])),
                    entry.get("deleted_at", utc_now_iso()),
                    entry.get("magnet"),
                ),
            )

    def _delete_archive_entry(self, thash: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM archive WHERE hash = ?", (thash,))

    def _load_snapshot(self) -> tuple[Snapshot, str]:
        row = self.conn.execute(
            "SELECT snapshot_json, digest FROM library_snapshot WHERE singleton = 1"
        ).fetchone()
        if row is None:
            default: dict = {"dirs": [""], "files": {}}
            return default, stable_json(canonical_snapshot(default))
        snapshot = json.loads(row["snapshot_json"])
        return snapshot, row["digest"]

    def _save_snapshot(self, snapshot: Snapshot, digest: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO library_snapshot"
                " (singleton, snapshot_json, digest, generated_at) VALUES (1, ?, ?, ?)",
                (json.dumps(snapshot), digest, snapshot.get("generated_at", utc_now_iso())),
            )

    def _root_for_snapshot_path(self, path: str) -> str | None:
        normalized = normalize_posix_path(path)
        if not normalized:
            return None
        parts = tuple(part for part in normalized.split("/") if part)
        if len(parts) < 2:
            return None
        if is_internal_category(parts[0]):
            return None
        if parts[0] not in self.category_names:
            return None
        return "/".join(parts[:2])

    def _snapshot_root_signatures(self, snapshot: Snapshot) -> dict[str, str]:
        root_entries: dict[str, SnapshotNode] = {}
        canonical = canonical_snapshot(snapshot)
        for path, node in canonical.get("files", {}).items():
            root = self._root_for_snapshot_path(path)
            if not root:
                continue
            rel = path[len(root) + 1 :]
            entries = root_entries.setdefault(root, {})
            entries[rel] = node
        return {root: stable_json(entries) for root, entries in root_entries.items()}

    def _classified_changed_roots(
        self, previous_snapshot: Snapshot, new_snapshot: Snapshot
    ) -> ChangeClassification:
        previous = self._snapshot_root_signatures(previous_snapshot)
        current = self._snapshot_root_signatures(new_snapshot)
        added = sorted(root for root in current if root not in previous)
        removed = sorted(root for root in previous if root not in current)
        updated = sorted(
            root
            for root in set(previous) & set(current)
            if previous[root] != current[root]
        )
        return {
            "added_paths": added,
            "removed_paths": removed,
            "updated_paths": updated,
        }

    @staticmethod
    def _display_provider(provider: str) -> str:
        return provider.replace("_", "-")

    @staticmethod
    def _format_change_message(
        added: list[str],
        removed: list[str],
        updated: list[str],
        synced: int,
        providers: list[str] | None = None,
        root_providers: dict[str, str] | None = None,
    ) -> str:
        _ = providers
        root_providers = root_providers or {}
        changed_count = len({*added, *removed, *updated})
        lines = [f"detected changes ({changed_count or synced} torrents):"]
        annotated_added = [
            BuzzState._format_changed_path(path, root_providers)
            for path in added
        ]
        annotated_removed = [
            BuzzState._format_changed_path(path, root_providers)
            for path in removed
        ]
        annotated_updated = [
            BuzzState._format_changed_path(path, root_providers)
            for path in updated
        ]
        if added:
            lines.append(f"  +{len(added)} added")
            lines.extend(f"    {path}" for path in annotated_added)
        if removed:
            lines.append(f"  -{len(removed)} removed")
            lines.extend(f"    {path}" for path in annotated_removed)
        if updated:
            lines.append(f"  ~{len(updated)} updated")
            lines.extend(f"    {path}" for path in annotated_updated)
        return "\n".join(lines)

    @staticmethod
    def _format_changed_path(
        path: str,
        root_providers: dict[str, str],
    ) -> str:
        provider = (root_providers.get(path) or "").strip()
        if not provider:
            return path
        return f"{path} [{BuzzState._display_provider(provider)}]"

    def _record_sync_change_event(self, report: SyncReport) -> None:
        added = report.get("added_paths", [])
        removed = report.get("removed_paths", [])
        updated = report.get("updated_paths", [])
        if not any((added, removed, updated)):
            return
        record_event(
            self._format_change_message(
                added,
                removed,
                updated,
                int(report.get("synced_torrents", 0)),
                list(report.get("providers") or []),
                cast(dict[str, str], report.get("path_providers") or {}),
            ),
            event="library_update",
        )

    def _snapshot_root_providers(
        self,
        infos: list[TorrentInfo],
    ) -> dict[str, str]:
        torrent_name_to_provider = {
            self.builder._torrent_name(info): str(info.get("provider", ""))
            for info in infos
            if info.get("provider")
        }
        snapshot, _current_roots = self.builder.build(infos)
        providers: dict[str, str] = {}
        for path in snapshot.get("files", {}):
            root = self._root_for_snapshot_path(path)
            if root is None:
                continue
            name = root.split("/", 1)[-1]
            provider = torrent_name_to_provider.get(name)
            if provider:
                providers[root] = provider
        return providers

    @staticmethod
    def _hook_category_counts(paths: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for path in paths:
            category = path.split("/", 1)[0] if path else "unknown"
            counts[category] = counts.get(category, 0) + 1
        return counts

    @classmethod
    def _hook_category_summary(cls, paths: list[str]) -> str:
        counts = cls._hook_category_counts(paths)
        if not counts:
            return "no roots"
        return ", ".join(
            f"{category}={count}" for category, count in sorted(counts.items())
        )

    @staticmethod
    def _hook_path_log_extra(paths: list[str]) -> dict[str, Any]:
        if len(paths) <= 5:
            return {"root_paths": list(paths)}
        return {}

    def _detect_and_enqueue_changes(
        self,
        previous_snapshot: Snapshot,
        new_snapshot: Snapshot,
        infos: list[TorrentInfo],
        previous_infos: list[TorrentInfo],
        trigger_hook: bool,
    ) -> tuple[SyncReport, list[str], bool]:
        """Detect snapshot changes, record events, and enqueue hooks."""
        previous_root_providers = self._snapshot_root_providers(previous_infos)
        current_root_providers = self._snapshot_root_providers(infos)

        digest = stable_json(canonical_snapshot(new_snapshot))
        previous_digest = stable_json(canonical_snapshot(previous_snapshot))
        changed = digest != previous_digest

        classified_changes = (
            self._classified_changed_roots(previous_snapshot, new_snapshot)
            if changed
            else {
                "added_paths": [],
                "removed_paths": [],
                "updated_paths": [],
            }
        )
        changed_paths = sorted(
            {
                *classified_changes["added_paths"],
                *classified_changes["removed_paths"],
                *classified_changes["updated_paths"],
            }
        )
        path_providers = {
            path: (
                previous_root_providers.get(path) or ""
                if path in classified_changes["removed_paths"]
                else current_root_providers.get(path)
                or previous_root_providers.get(path)
                or ""
            )
            for path in changed_paths
        }
        providers_for_changes = sorted(
            {provider for provider in path_providers.values() if provider}
        )

        report = cast(SyncReport, dict(new_snapshot["report"]))
        report["changed"] = changed
        report["changed_paths"] = changed_paths
        report.update(classified_changes)
        report["path_providers"] = path_providers
        report["synced_torrents"] = len(infos)
        report["providers"] = providers_for_changes or [
            p for p, _ in self._ordered_clients()
        ]
        report["timestamp"] = utc_now_iso()

        hook_paths: list[str] = []
        if (
            changed
            and trigger_hook
            and (self.config.hook_command or self.config.curator_url)
        ):
            hook_paths = changed_paths

        return report, hook_paths, changed

    def sync(self, *, trigger_hook: bool = True, resync: bool = False) -> SyncReport:
        """Sync torrent state with the active provider and rebuild snapshot."""
        ordered_clients = self._ordered_clients()
        if not ordered_clients:
            raise RuntimeError("provider token is not configured.")

        # Check health of providers before starting sync
        for i, (name, client) in enumerate(ordered_clients):
            if not client.is_healthy():
                if i == 0:
                    # Primary provider is degraded
                    with self.lock:
                        self.provider_degraded = True
                    msg = f"primary provider {name} is degraded"
                    record_event(msg, level="warning", provider=name)
                    raise RuntimeError("provider_degraded")
                else:
                    # Secondary provider is degraded
                    record_event(
                        f"secondary provider {name} is degraded",
                        level="warning",
                        provider=name
                    )

        with self.lock:
            self.provider_degraded = False
            self.sync_in_progress = True

        try:
            all_infos, all_caches = self._fetch_all_provider_infos(
                ordered_clients, resync=resync
            )
            self._apply_display_names(all_infos)
            new_cache, infos = self._effective_provider_cache(
                all_infos, all_caches
            )
            snapshot, _current_roots = self.builder.build(infos)
            self._index_stream_sources(infos, all_infos)
            provider_library_entries = self._build_provider_library_entries(
                all_infos
            )
            digest = stable_json(canonical_snapshot(snapshot))

            report, hook_paths, should_notify, should_notify_archive, changed = self._commit_sync_to_db(
                new_cache,
                all_caches,
                infos,
                snapshot,
                digest,
                provider_library_entries,
                trigger_hook,
            )

            if changed:
                self._record_sync_change_event(report)
            if hook_paths:
                self._enqueue_hook(hook_paths)
            if should_notify:
                self._notify_ui_change("sync")
            if should_notify_archive:
                self._notify_ui_change("archive")

            return report
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
            raise
        finally:
            with self.lock:
                self.sync_in_progress = False

    def _fetch_all_provider_infos(
        self,
        ordered_clients: list[tuple[str, ProviderClient]],
        *,
        resync: bool = False,
    ) -> tuple[list[tuple[str, str, TorrentInfo]], dict[str, Any]]:
        """Fetch and normalize torrent information from all active providers."""
        all_infos: list[tuple[str, str, TorrentInfo]] = []
        all_caches: dict[str, Any] = {}
        for provider, client in ordered_clients:
            summaries = [
                self._summary_to_dict(summary)
                for summary in client.list_torrents()
            ]
            partial_cache, partial_infos = self._build_torrent_cache(
                summaries, provider=provider, client=client, resync=resync
            )
            all_infos.extend(
                (provider, cache_key, info) for cache_key, info in partial_infos
            )
            all_caches.update(partial_cache)
        return all_infos, all_caches

    def _commit_sync_to_db(
        self,
        effective_cache: dict[str, Any],
        full_provider_cache: dict[str, Any],
        infos: list[TorrentInfo],
        snapshot: dict[str, Any],
        digest: str,
        provider_library_entries: list[dict[str, Any]],
        trigger_hook: bool,
    ) -> tuple[SyncReport, list[str], bool, bool, bool]:
        """Update local cache, DB, and snapshot with the results of a sync.

        effective_cache: deduplicated winner-per-hash cache set as self.cache
            (used for archive/removal detection and in-memory serving).
        full_provider_cache: all providers' entries before deduplication, persisted
            to the torrents table so signatures from every provider survive restarts.
        """
        should_notify = False
        should_notify_archive = False

        with self.lock:
            previous_infos = []
            for cache_key, cached in self.cache.items():
                if not (
                    isinstance(cached, dict)
                    and isinstance(cached.get("info"), dict)
                ):
                    continue
                info = dict(cached["info"])
                if not info.get("provider"):
                    provider, _provider_torrent_id = split_provider_torrent_id(
                        cache_key
                    )
                    info["provider"] = provider
                previous_infos.append(cast(TorrentInfo, info))

            archive_size_before = len(self.archive)
            self._archive_new_torrents(effective_cache)
            self._archive_removed_torrents(effective_cache)
            if len(self.archive) != archive_size_before:
                should_notify_archive = True

            report, hook_paths, changed = self._detect_and_enqueue_changes(
                self.snapshot,
                snapshot,
                infos,
                previous_infos,
                trigger_hook,
            )

            cache_changed = effective_cache != self.cache
            has_non_winning_entries = len(full_provider_cache) > len(effective_cache)
            if cache_changed:
                self.cache = effective_cache
            if cache_changed or has_non_winning_entries:
                # Persist the full per-provider cache so signatures for all
                # providers survive restarts and avoid unnecessary refetches.
                self._save_cache(full_provider_cache)
            # Keep the in-memory full mirror in lockstep with the persisted
            # table so the next in-process sync sees every provider's
            # signature (not just the deduplicated winners in self.cache).
            self._full_cache = full_provider_cache

            db.replace_provider_library(self.conn, provider_library_entries)

            if changed:
                self.snapshot = snapshot
                self._rebuild_snapshot_indexes()
                self.snapshot_digest = digest
                self._save_snapshot(self.snapshot, self.snapshot_digest)
                self.snapshot_loaded = True
                should_notify = True

            self.last_sync_at = report["timestamp"]
            self.last_report = report
            self.last_error = None

        return report, hook_paths, should_notify, should_notify_archive, changed

    def _build_torrent_cache(  # noqa: C901
        self,
        summaries: list[dict],
        *,
        provider: str = "real_debrid",
        client: Any | None = None,
        resync: bool = False,
    ) -> tuple[dict[str, TorrentInfo], list[tuple[str, TorrentInfo]]]:
        client = client or self.client
        if client is None:
            return {}, []
        total = len(summaries)
        print(f"Processing {provider} list ({total} total)", flush=True)

        # Pass 1: classify each summary as a cache hit or a refetch needed.
        # Each hit entry: (torrent_id, cache_key, summary, cached)
        hits: list[tuple[str, str, dict, Any]] = []
        refetch_ids: list[str] = []
        refetch_meta: dict[str, tuple[str, dict, dict | None]] = {}  # torrent_id -> (cache_key, summary, cached)
        for summary in summaries:
            torrent_id = str(summary.get("id", "")).strip()
            if not torrent_id:
                continue
            cache_key = self._cache_key(provider, torrent_id)
            signature = self._summary_signature(summary)
            with self.lock:
                cached = self._full_cache.get(cache_key)
            cached_info: TorrentInfo | None = (
                cast(TorrentInfo, cached["info"])
                if isinstance(cached, dict) and isinstance(cached.get("info"), dict)
                else None
            )
            cached_signature = cached.get("signature") if isinstance(cached, dict) else None
            cached_status = str(cached_info.get("status") or "") if cached_info is not None else ""
            signature_matches = cached_signature == signature
            needs_refresh = cached_info is None or self._should_refresh_cached_info(
                summary, cached_info, signature_matches=signature_matches
            )
            signature_hit = (
                cached_signature == signature
                and cached_info is not None
                and not needs_refresh
            )
            terminal_hit = (
                not resync
                and signature_matches
                and cached_info is not None
                and cached_status in ("downloaded", "error")
                and bool(cached_info.get("links"))
            )
            if signature_hit or terminal_hit:
                hits.append((torrent_id, cache_key, summary, cached))
            else:
                refetch_ids.append(torrent_id)
                refetch_meta[torrent_id] = (cache_key, summary, cached)

        # Pass 2: fetch details for cache misses in one batch call with progress.
        def _on_progress(tid: str, i: int, n: int) -> None:
            if event_registry.stdout_enabled:
                print(f"Fetching entry: {tid} ({i}/{n})", flush=True)

        fetched_details: dict[str, Any] = {}
        if refetch_ids:
            try:
                raw_details = client.fetch_details(refetch_ids, on_progress=_on_progress)
                fetched_details = {
                    tid: self._detail_to_info(detail)
                    for tid, detail in raw_details.items()
                }
            except httpx.HTTPStatusError as exc:
                # Partial fallback: handle per-id below.
                fetched_details = {}
                record_event(
                    f"provider batch detail fetch failed: {provider}: {exc}",
                    level="error",
                    event="provider_detail_refresh_failed",
                    provider=provider,
                )

        # Pass 3: assemble new_cache and infos.
        new_cache: dict[str, TorrentInfo] = {}
        infos: list[tuple[str, TorrentInfo]] = []

        # Cache hits
        for torrent_id, cache_key, summary, cached in hits:
            cached_info = cast(TorrentInfo, cast(dict, cached)["info"])
            info: TorrentInfo = cached_info
            self._log_torrent_sync_decision(summary, info, source="cache")
            self._apply_cached_file_selection(provider, cache_key, info)
            cached_magnet = cached.get("magnet") if cached else None
            info["provider"] = provider
            info["provider_torrent_id"] = torrent_id
            new_cache[cache_key] = {
                "signature": self._summary_signature(summary),
                "info": info,
                "magnet": cached_magnet,
            }
            infos.append((cache_key, info))

        # Refetched entries
        for torrent_id in refetch_ids:
            cache_key, summary, cached = refetch_meta[torrent_id]
            if torrent_id in fetched_details:
                info = fetched_details[torrent_id]
            else:
                # Fall back to cached detail on per-id failure (mirrors original behaviour).
                if not self._can_use_cached_provider_detail_by_kind(provider, cached):
                    raise RuntimeError(
                        f"provider detail fetch failed for {provider}:{torrent_id} and no cached fallback"
                    )
                cached_detail = cast(dict[str, Any], cached)
                info = cast(TorrentInfo, cached_detail["info"])
                record_event(
                    f"provider detail refresh failed; using cached detail: {provider}:{torrent_id}",
                    level="error",
                    event="provider_detail_refresh_failed",
                    provider=provider,
                    torrent_id=torrent_id,
                )
            self._log_torrent_sync_decision(summary, info, source="refetch")
            self._apply_cached_file_selection(provider, cache_key, info)
            cached_magnet = cached.get("magnet") if cached else None
            info["provider"] = provider
            info["provider_torrent_id"] = torrent_id
            new_cache[cache_key] = {
                "signature": self._summary_signature(summary),
                "info": info,
                "magnet": cached_magnet,
            }
            infos.append((cache_key, info))

        return new_cache, infos

    @staticmethod
    def _can_use_cached_provider_detail(
        provider: str,
        exc: httpx.HTTPStatusError,
        cached: Any,
    ) -> bool:
        return (
            provider == "torbox"
            and 500 <= exc.response.status_code < 600
            and isinstance(cached, dict)
            and isinstance(cached.get("info"), dict)
        )

    @staticmethod
    def _can_use_cached_provider_detail_by_kind(provider: str, cached: Any) -> bool:
        """Return True when a cached detail is available as a fallback (no HTTP error required)."""
        return (
            provider == "torbox"
            and isinstance(cached, dict)
            and isinstance(cached.get("info"), dict)
        )

    def _effective_provider_cache(
        self, items: list[tuple[str, str, TorrentInfo]], caches: dict[str, Any]
    ) -> tuple[dict[str, TorrentInfo], list[TorrentInfo]]:
        priority_index: dict[str, int] = {
            provider: index
            for index, provider in enumerate(getattr(self.config, "provider_priority", ()))
        }
        selected: dict[str, tuple[str, str, TorrentInfo]] = {}
        for provider, cache_key, info in items:
            local_key = self._local_entry_key(info, cache_key)
            current = selected.get(local_key)
            if current is None or priority_index.get(provider, 999) < priority_index.get(current[0], 999):
                selected[local_key] = (provider, cache_key, info)
        effective_cache = {
            cache_key: {
                "signature": caches.get(cache_key, {}).get("signature") or {},
                "info": info,
                "magnet": self.cache.get(cache_key, {}).get("magnet"),
            }
            for _provider, cache_key, info in selected.values()
        }
        return effective_cache, [info for _provider, _cache_key, info in selected.values()]

    def _apply_display_names(
        self, items: list[tuple[str, str, TorrentInfo]]
    ) -> None:
        """Attach best-known readable display names to provider info dicts."""
        name_hints = db.load_torrent_name_hints(self.conn)
        priority_index: dict[str, int] = {
            provider: index
            for index, provider in enumerate(
                getattr(self.config, "provider_priority", ())
            )
        }
        names_by_hash: dict[str, tuple[int, str]] = {}
        for provider, _cache_key, info in items:
            thash = str(info.get("hash") or "").strip().lower()
            if not thash:
                continue
            provider_name = (
                _valid_provider_name(info.get("original_filename"))
                or _valid_provider_name(info.get("filename"))
            )
            if not provider_name:
                continue
            priority = priority_index.get(provider, 999)
            current = names_by_hash.get(thash)
            if current is None or priority < current[0]:
                names_by_hash[thash] = (priority, provider_name)

        with self.lock:
            magnets_by_cache = {
                cache_key: str(cached.get("magnet") or "")
                for _provider, cache_key, _info in items
                if isinstance((cached := self.cache.get(cache_key)), dict)
            }

        for _provider, cache_key, info in items:
            thash = str(info.get("hash") or "").strip().lower()
            if not thash:
                continue
            display_name = (
                (names_by_hash.get(thash) or (999, ""))[1]
                or name_hints.get(thash)
                or magnet_display_name(magnets_by_cache.get(cache_key, ""))
                or _safe_file_root_name(info)
            )
            if display_name:
                info["display_name"] = display_name
            else:
                info.pop("display_name", None)
            category_override = self.category_overrides.get(thash)
            if category_override:
                info["category_override"] = category_override
            else:
                info.pop("category_override", None)

    def _index_stream_sources(
        self,
        effective_infos: list[TorrentInfo],
        all_infos: list[tuple[str, str, TorrentInfo]],
    ) -> None:
        sources: dict[str, list[dict[str, str]]] = {}
        by_entry: dict[str, list[tuple[str, TorrentInfo]]] = {}
        for provider, _cache_key, info in all_infos:
            by_entry.setdefault(self._local_entry_key(info, ""), []).append((provider, info))
        for info in effective_infos:
            entry_key = self._local_entry_key(info, "")
            selected = self.builder._selected_files(info)
            for _index, item in enumerate(selected):
                ref = str(item.get("url") or "")
                if not ref:
                    continue
                fallback_sources = []
                item_path = normalize_posix_path(str(item.get("path") or ""))
                for provider, candidate in by_entry.get(entry_key, []):
                    candidate_selected = self.builder._selected_files(candidate)
                    for candidate_item in candidate_selected:
                        candidate_path = normalize_posix_path(
                            str(candidate_item.get("path") or "")
                        )
                        if candidate_path != item_path:
                            continue
                        candidate_ref = str(candidate_item.get("url") or "")
                        if not candidate_ref:
                            continue
                        fallback_sources.append(
                            {"provider": provider, "source_url": candidate_ref}
                        )
                        break
                sources[ref] = fallback_sources
        with self.lock:
            self.stream_sources = sources

    def _build_provider_library_entries(
        self, all_infos: list[tuple[str, str, TorrentInfo]]
    ) -> list[dict[str, Any]]:
        """Build a flat list of provider-library entries from all providers for persistence."""
        entries = []
        for provider, cache_key, info in all_infos:
            _, provider_torrent_id, _ = self._split_cache_key(cache_key)
            thash = str(info.get("hash") or "").strip().lower()
            if not thash:
                continue
            files = [
                {
                    "id": f.get("id"),
                    "path": f.get("path"),
                    "bytes": f.get("bytes"),
                }
                for f in info.get("files", [])
                if isinstance(f, dict) and f.get("selected")
            ]
            with self.lock:
                cached = self.cache.get(cache_key)
            entries.append({
                "hash": thash,
                "name": (
                    _valid_provider_name(info.get("display_name"))
                    or _valid_provider_name(info.get("filename"))
                    or _valid_provider_name(info.get("original_filename"))
                    or magnet_display_name(cached.get("magnet") or "" if cached else "")
                    or _safe_file_root_name(info)
                    or None
                ),
                "bytes": info.get("bytes"),
                "files": files,
                "magnet": cached.get("magnet") if cached else None,
                "provider": provider,
                "provider_torrent_id": provider_torrent_id,
                "status": info.get("status"),
                "progress": info.get("progress"),
                "info_json": json.dumps(dict(info)),
                "signature_json": json.dumps(
                    cached.get("signature", {}) if cached else {}
                ),
            })
        return entries

    def _rebuild_stream_sources_from_cache(self) -> None:
        all_infos: list[tuple[str, str, TorrentInfo]] = []
        repaired: list[tuple[str, dict[str, Any]]] = []
        for cache_key, cached in self.cache.items():
            if not isinstance(cached, dict):
                continue
            info = cached.get("info")
            if not isinstance(info, dict):
                continue
            provider, _provider_torrent_id, normalized_key = (
                self._split_cache_key(cache_key)
            )
            if provider == "torbox":
                before_links = list(info.get("links") or [])
                self._apply_cached_file_selection(
                    provider,
                    normalized_key,
                    cast(TorrentInfo, info),
                )
                if before_links != list(info.get("links") or []):
                    repaired.append((normalized_key, cached))
            all_infos.append(
                (provider, normalized_key, cast(TorrentInfo, info))
            )
        self._apply_display_names(all_infos)
        self._index_stream_sources(
            [info for _provider, _cache_key, info in all_infos],
            all_infos,
        )
        for cache_key, cached in repaired:
            self._save_cache_entry(cache_key, cached)
        if repaired:
            self._refresh_snapshot_from_cache()

    def _refresh_snapshot_from_cache(self) -> None:
        all_infos: list[tuple[str, str, TorrentInfo]] = []
        with self.lock:
            previous_infos = []
            for cache_key, cached in self.cache.items():
                if not isinstance(cached, dict):
                    continue
                info = cached.get("info")
                if not isinstance(info, dict):
                    continue
                provider, _provider_torrent_id, normalized_key = (
                    self._split_cache_key(cache_key)
                )
                info_copy = dict(info)
                if not info_copy.get("provider"):
                    info_copy["provider"] = provider
                all_infos.append(
                    (provider, normalized_key, cast(TorrentInfo, info_copy))
                )
                previous_infos.append(cast(TorrentInfo, info_copy))

            self._apply_display_names(all_infos)
            infos = [info for _provider, _cache_key, info in all_infos]
            snapshot, _current_roots = self.builder.build(infos)

            report, hook_paths, changed = self._detect_and_enqueue_changes(
                self.snapshot,
                snapshot,
                infos,
                previous_infos,
                trigger_hook=True,
            )

            if changed:
                self.snapshot = snapshot
                self._rebuild_snapshot_indexes()
                self.snapshot_digest = stable_json(canonical_snapshot(snapshot))
                self.snapshot_loaded = True
                self._save_snapshot(self.snapshot, self.snapshot_digest)
                self._record_sync_change_event(report)

            self._index_stream_sources(infos, all_infos)

        if hook_paths:
            self._enqueue_hook(hook_paths)

    def _should_refresh_cached_info(
        self,
        summary: TorrentSummary,
        info: TorrentInfo,
        *,
        signature_matches: bool = False,
    ) -> bool:
        """Return True when cached torrent detail is probably stale.

        A detail is stale (worth a refetch) when the summary advertises ready
        links but the cached detail has a selected video file that still lacks a
        resolved link — the per-file links need fetching to become playable.

        A detail that simply has *no* selected playable video has nothing to
        fetch; refetching it on every sync is wasted work. We suppress that
        refetch once the cached summary signature matches the current summary
        (meaning we already fetched the detail for this exact upstream state). A
        genuine upstream change alters the signature and still triggers exactly
        one refetch.
        """
        if not self._summary_has_ready_links(summary):
            return False

        # If we have a selected video but no resolved link yet, it's genuinely stale
        # and we must refetch even if the signature matches (to get the links).
        if self._info_has_selected_playable_media(info) and not self._info_has_linked_playable_media(info):
            return True

        # If the upstream summary state matches our cache, trust it. This
        # prevents endless refetch loops for large RD torrents where links and
        # files never perfectly align.
        return not signature_matches

    def _info_has_selected_playable_media(self, info: TorrentInfo) -> bool:
        selected = self.builder._selected_files(info)
        return any(is_video_file(item["path"]) for item in selected)

    def _summary_has_ready_links(self, summary: TorrentSummary) -> bool:
        status = str(summary.get("status", "")).lower()
        progress = summary.get("progress")
        links = summary.get("links") or []
        return bool(
            links
            and (
                status == "downloaded"
                or progress == 100
                or progress == 100.0
            )
        )

    def _info_has_linked_playable_media(self, info: TorrentInfo) -> bool:
        selected = self.builder._selected_files(info)
        playable = [item for item in selected if is_video_file(item["path"])]
        return bool(
            playable
            and info.get("status") == "downloaded"
            and any(item.get("url") for item in playable)
        )

    def _log_torrent_sync_decision(
        self,
        summary: TorrentSummary,
        info: TorrentInfo,
        *,
        source: str,
    ) -> None:
        selected = self.builder._selected_files(info)
        record_event(
            "torrent detail sync decision",
            level="debug",
            event="torrent_detail_sync",
            torrent_id=str(summary.get("id", "")),
            filename=str(
                info.get("original_filename")
                or info.get("filename")
                or summary.get("filename")
                or ""
            ),
            status=str(info.get("status") or summary.get("status") or ""),
            progress=summary.get("progress"),
            summary_links=len(summary.get("links") or []),
            selected_files=len(selected),
            selected_videos=sum(
                1 for item in selected if is_video_file(item["path"])
            ),
            detail_source=source,
        )

    @staticmethod
    def _apply_file_selection(
        info: TorrentInfo,
        selected_file_ids: set[str],
    ) -> None:
        for file_item in info.get("files", []):
            file_id = str(file_item.get("id") or "")
            file_item["selected"] = 1 if file_id in selected_file_ids else 0

    @staticmethod
    def _normalize_torbox_file_refs(info: TorrentInfo) -> bool:
        changed = False
        for file_item in info.get("files", []):
            file_id = str(file_item.get("id") or "").strip()
            if file_id and not file_id.isdigit():
                file_item["id"] = ""
                changed = True
            stream_ref = str(file_item.get("stream_ref") or "").strip()
            if ":" not in stream_ref:
                continue
            torrent_id, stream_file_id = stream_ref.split(":", 1)
            if torrent_id and not stream_file_id.isdigit():
                file_item["stream_ref"] = ""
                changed = True
        return changed

    @staticmethod
    def _torbox_stream_ref(
        torrent_id: str,
        file_item: TorrentInfo,
        *,
        single_selected: bool = False,
    ) -> str:
        stream_ref = str(file_item.get("stream_ref") or "").strip()
        if stream_ref:
            if ":" in stream_ref:
                _stream_torrent_id, file_id = stream_ref.split(":", 1)
                if not file_id.isdigit():
                    return torrent_id if torrent_id and single_selected else ""
            return stream_ref
        file_id = str(file_item.get("id") or "").strip()
        if torrent_id and file_id.isdigit():
            return f"{torrent_id}:{file_id}"
        if torrent_id and single_selected:
            return torrent_id
        return ""

    def _rebuild_torbox_links(
        self,
        info: TorrentInfo,
        provider_torrent_id: str,
    ) -> None:
        self._normalize_torbox_file_refs(info)
        links = []
        missing = []
        selected_files = [
            file_item
            for file_item in info.get("files", [])
            if file_item.get("selected")
        ]
        single_selected = len(selected_files) == 1
        for file_item in selected_files:
            if not file_item.get("selected"):
                continue
            stream_ref = self._torbox_stream_ref(
                provider_torrent_id,
                cast(TorrentInfo, file_item),
                single_selected=single_selected,
            )
            if stream_ref:
                links.append(stream_ref)
            else:
                missing.append(str(file_item.get("path") or "unknown"))
        info["links"] = links
        if missing:
            record_event(
                "torbox selected files missing stream refs: "
                + ", ".join(missing[:5]),
                level="error",
                event="provider_selection_missing_stream_ref",
                provider="torbox",
                torrent_id=provider_torrent_id,
                missing_count=len(missing),
            )

    def _apply_cached_file_selection(
        self,
        provider: str,
        cache_key: str,
        info: TorrentInfo,
    ) -> None:
        """Apply the persisted per-torrent selection (by path) to ``info``.

        The selection is keyed by torrent hash and stored as a set of selected
        normalized paths, which are portable across providers. If no selection
        is stored yet, seed it from this provider's own current ``selected``
        flags (per-provider seeding) and persist it.
        """
        thash = str(info.get("hash") or "").strip().lower()
        _, provider_torrent_id, _ = self._split_cache_key(cache_key)
        all_paths = {
            normalize_posix_path(str(file_item.get("path") or ""))
            for file_item in info.get("files", [])
            if str(file_item.get("path") or "").strip()
        }
        with self.lock:
            selected_paths = self.file_selections.get(thash) if thash else None
            if selected_paths is None:
                # Seed from this provider's current selection and persist.
                selected_paths = {
                    normalize_posix_path(str(file_item.get("path") or ""))
                    for file_item in info.get("files", [])
                    if file_item.get("selected")
                    and str(file_item.get("path") or "").strip()
                }
                if thash:
                    self.file_selections[thash] = selected_paths
                    db.save_file_selection(
                        self.conn, thash, selected_paths, all_paths
                    )
        selected_file_ids = set(
            self._matching_destination_file_ids(
                info,
                selected_paths,
                thash=thash,
                warned_paths=self._file_selection_unresolved_warnings,
            )
        )
        self._apply_file_selection(info, selected_file_ids)
        if provider == "torbox":
            self._rebuild_torbox_links(info, provider_torrent_id)
        else:
            info["links"] = [
                str(file_item.get("stream_ref") or "")
                for file_item in info.get("files", [])
                if file_item.get("selected")
                and str(file_item.get("stream_ref") or "")
            ]

    def _archive_new_torrents(self, new_cache: dict[str, Any]) -> None:
        """Archive torrents seen for the first time (not previously in self.archive)."""
        for _torrent_id, cached in new_cache.items():
            if not isinstance(cached, dict):
                continue
            info = cached.get("info")
            if not isinstance(info, dict):
                continue
            thash = str(info.get("hash") or "").strip().lower()
            if not thash or thash in self.archive:
                continue
            self._add_to_archive(info, magnet=cached.get("magnet"))

    def _archive_removed_torrents(
        self, new_cache: dict[str, TorrentInfo]
    ) -> None:
        removed_torrent_ids = set(self.cache) - set(new_cache)
        for torrent_id in removed_torrent_ids:
            cached = self.cache.get(torrent_id)
            if not isinstance(cached, dict):
                continue
            info = cached.get("info")
            if isinstance(info, dict) and info.get("hash"):
                self._add_to_archive(info, magnet=cached.get("magnet"))

    def _enqueue_hook(self, changed_roots: list[str]) -> None:
        pending = set(changed_roots)
        if not pending:
            return
        should_submit = False
        with self.hook_lock:
            merged = set(self.hook_pending_paths)
            merged.update(pending)
            self.hook_pending_paths = sorted(merged)
            pending_count = len(self.hook_pending_paths)
            if not self.hook_in_progress:
                self.hook_phase = "queued"
            if not self.hook_task_active:
                self.hook_task_active = True
                should_submit = True
        if should_submit:
            self._submit_hook_batch()
        record_event(
            "media library update queued: "
            f"{len(pending)} library root(s), {pending_count} pending",
            event="hook_queued",
            changed_roots=len(pending),
            pending_roots=pending_count,
            **self._hook_path_log_extra(sorted(pending)),
        )

    def _submit_hook_batch(self) -> None:
        """Submit the next pending hook batch to the background task pool."""
        with self.hook_lock:
            if not self.hook_pending_paths:
                self.hook_task_active = False
                self.hook_phase = "complete" if self.hook_phase != "failed" else "failed"
                return
            paths = self.hook_pending_paths
            self.hook_pending_paths = []
            self.hook_in_progress = True
            self.hook_last_started_at = utc_now_iso()
            self.hook_wait_started_at = self.hook_last_started_at
            self.hook_last_error = None
            self.hook_phase = "queued"
            self.hook_active_paths = list(paths)

        task_id = self.background_tasks.submit(
            kind="hook",
            label=f"update_hook: {len(paths)} roots",
            work=lambda tid, cancel_event: self._work_run_hook_task(tid, paths, cancel_event),
        )
        record_event(
            "media library update started: "
            f"{len(paths)} library root(s) "
            f"({self._hook_category_summary(paths)}) ({task_id})",
            event="hook_batch_started",
            link_to_task_id=task_id,
        )

    def _work_run_hook_task(self, task_id: str, paths: list[str], cancel_event: threading.Event) -> None:
        """Background task worker for the library update hook."""
        try:
            if self.config.rd_update_delay_secs > 0:
                with self.hook_lock:
                    self.hook_phase = "waiting_delay"
                record_event(
                    f"waiting for Real-Debrid update delay: {self.config.rd_update_delay_secs}s",
                    event="hook_waiting_delay",
                    delay_secs=self.config.rd_update_delay_secs,
                )
                # Check for cancellation during wait
                start_wait = time.time()
                while time.time() - start_wait < self.config.rd_update_delay_secs:
                    if cancel_event.is_set():
                        raise RuntimeError("cancelled")
                    time.sleep(1)

                record_event(
                    "provider update delay finished: Real-Debrid",
                    event="hook_delay_finished",
                    delay_secs=self.config.rd_update_delay_secs,
                )

            self._run_hook(paths)

            # Successfully ran the hook, now chain the curator rebuild
            self._submit_curator_rebuild(paths)
        except Exception as exc:
            with self.hook_lock:
                self.hook_last_error = str(exc)
                self.hook_phase = "failed"
                self.hook_in_progress = False
            if str(exc) != "cancelled":
                record_event(f"hook task failed: {exc}", level="error")
            # If it failed, we still want to check for more work eventually
            self._submit_hook_batch()

    def _submit_curator_rebuild(self, paths: list[str], skip_delay: bool = False) -> None:
        """Submit a curator rebuild task to the background task pool."""
        label = (
            "resync_lib"
            if not paths
            else f"resync_lib: {len(paths)} roots"
        )
        task_id = self.background_tasks.submit(
            kind="curator",
            label=label,
            work=lambda tid, cancel_event: self._work_trigger_curator_task(
                tid,
                paths,
                cancel_event,
                skip_delay=skip_delay,
            ),
            auto_complete=False,
        )
        message = (
            f"curator rebuild queued ({task_id})"
            if not paths
            else "curator rebuild queued: "
            f"{len(paths)} library root(s) ({task_id})"
        )
        record_event(
            message,
            event="hook_curator_queued",
            link_to_task_id=task_id,
        )

    def _work_trigger_curator_task(
        self, task_id: str, paths: list[str], cancel_event: threading.Event, skip_delay: bool = False
    ) -> None:
        """Background task worker for curator rebuild."""
        curator_accepted = False
        try:
            # Re-triggering delay if manual rebuild or specific path requested it
            # though usually the hook task already handled the RD delay.
            self._wait_for_vfs_visibility(paths, cancel_event=cancel_event)
            if cancel_event.is_set():
                raise RuntimeError("cancelled")
            curator_accepted = self._trigger_curator(paths, task_id=task_id)
            if not curator_accepted:
                self.background_tasks.complete(task_id)
                return

            # changed_roots look like 'movies/TorrentName'; scope the subtitle
            # fetch to those torrents instead of the whole library. Skip
            # internal categories (e.g. __unplayable__).
            torrent_names = sorted(
                {
                    root.split("/", 1)[-1]
                    for root in paths
                    if not is_internal_category(root.split("/", 1)[0])
                }
            )
            # Subtitle fetch (full or scoped) only runs when fetch_on_resync
            # is enabled in the configuration.
            should_fetch_subs = bool(self.config.subtitles.fetch_on_resync)

            if (
                self.config.subtitles.enabled
                and self.config.curator_url
                and should_fetch_subs
            ):
                def run_subs(tid: str, cancel_event: threading.Event) -> None:
                    # Proxied task
                    pass

                subs_task_id = self.background_tasks.submit(
                    kind="subtitles",
                    label=(
                        f"fetch_subtitles: {len(torrent_names)} torrents"
                        if torrent_names
                        else "fetch_subtitles: full_library"
                    ),
                    work=run_subs,
                    auto_complete=False,
                )

                subs_url = self.config.curator_url.replace(
                    "/rebuild", "/api/subtitles/fetch"
                )
                try:
                    payload: dict[str, Any] = {"task_id": subs_task_id}
                    if torrent_names:
                        payload["torrent_names"] = torrent_names

                    data = json.dumps(payload).encode("utf-8")
                    req = request.Request(
                        subs_url,
                        data=data,
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    # We use urllib here for simplicity as it's consistent with _trigger_curator
                    with request.urlopen(req, timeout=30):
                        pass
                except Exception as exc:
                    record_event(f"automatic subtitle fetch trigger failed: {exc}", level="warning")
                    self.background_tasks.complete(subs_task_id, error=str(exc))

        except Exception as exc:
            with self.hook_lock:
                self.hook_last_error = str(exc)
                self.hook_phase = "failed"
            if str(exc) != "cancelled":
                record_event(f"curator rebuild failed: {exc}", level="error")
                if not curator_accepted:
                    self.background_tasks.complete(task_id, error=str(exc))
            else:
                self.background_tasks.complete(task_id, status="cancelled")
        finally:
            with self.hook_lock:
                self.hook_in_progress = False
                self.hook_last_finished_at = utc_now_iso()
                if self.hook_phase != "failed":
                    self.hook_phase = "complete"

            # Check for more pending work
            self._submit_hook_batch()

    def manual_rebuild(self) -> None:
        """Trigger curator rebuild immediately, skipping RD delay."""
        with self.hook_lock:
            self.hook_in_progress = True
            self.hook_last_started_at = utc_now_iso()
            self.hook_wait_started_at = self.hook_last_started_at
            self.hook_last_error = None
            self.hook_phase = "queued"
            self.hook_active_paths = []

        self._submit_curator_rebuild([], skip_delay=True)

    def _summary_signature(self, summary: TorrentInfo) -> TorrentInfo:
        return {
            "filename": summary.get("filename"),
            "bytes": summary.get("bytes"),
            "progress": summary.get("progress"),
            "status": summary.get("status"),
            "ended": summary.get("ended"),
            "links": len(summary.get("links") or []),
        }
    def _wait_for_vfs_visibility(
        self,
        roots: list[str],
        cancel_event: threading.Event | None = None,
    ) -> None:
        mount = self.config.library_mount
        if not mount or not os.path.isdir(mount):
            return

        timeout = self.config.vfs_wait_timeout_secs
        start_time = time.time()

        with self.lock:
            snapshot_roots = set()
            for path in self.snapshot.get("files", {}):
                root = self._root_for_snapshot_path(path)
                if root:
                    snapshot_roots.add(root)

        to_check = [
            (root, root in snapshot_roots)
            for root in roots
            if root.split("/", 1)[0] in self.category_names
        ]
        if not to_check:
            return

        with self.hook_lock:
            self.hook_phase = "waiting_vfs"
        record_event(
            "waiting for VFS visibility: "
            f"{len(to_check)} library root(s) in {mount} "
            f"(timeout {timeout}s)",
            event="hook_waiting_vfs",
            roots=len(to_check),
            mount=mount,
            timeout_secs=timeout,
        )

        last_missing: list[str] = []
        last_stale: list[str] = []
        while time.time() - start_time < timeout:
            if cancel_event is not None:
                raise_if_cancelled(cancel_event)
            all_visible, missing, stale = self._check_vfs_roots(mount, to_check)
            last_missing = missing
            last_stale = stale
            if all_visible:
                elapsed = int(time.time() - start_time)
                record_event(
                    f"vfs visibility confirmed after {elapsed}s",
                    event="hook_vfs_visible",
                    elapsed_secs=elapsed,
                    roots=len(to_check),
                )
                return
            if int(time.time() - start_time) % 30 == 0:
                self.verbose_log(
                    f"VFS still syncing... (missing: {len(missing)}, stale: {len(stale)})"
                )
            if cancel_event is None:
                time.sleep(2)
            elif cancel_event.wait(timeout=2):
                raise RuntimeError("cancelled")

        record_event(
            "vfs visibility timeout reached after "
            f"{timeout}s; proceeding with sync",
            level="warning",
            event="hook_vfs_timeout",
            timeout_secs=timeout,
            roots=len(to_check),
            missing_roots=len(last_missing),
            stale_roots=len(last_stale),
        )

    def _check_vfs_roots(
        self, mount: str, to_check: list[tuple[str, bool]]
    ) -> tuple[bool, list[str], list[str]]:
        missing: list[str] = []
        stale: list[str] = []
        for root, expected in to_check:
            path = os.path.join(mount, root)
            exists = os.path.exists(path)
            if expected and not exists:
                missing.append(root)
            elif not expected and exists:
                stale.append(root)
        return not (missing or stale), missing, stale

    def _trigger_curator(
        self, changed_roots: list[str], task_id: str = ""
    ) -> bool:
        if not self.config.curator_url:
            record_event(
                "curator rebuild skipped: curator URL is not configured",
                event="hook_curator_skipped",
                changed_roots=len(changed_roots),
            )
            return False
        with self.hook_lock:
            self.hook_phase = "triggering_curator"

        record_event(
            "triggering curator rebuild: "
            f"{len(changed_roots)} library root(s); "
            "waiting for curator completion callback",
            event="hook_triggering_curator",
            changed_roots=len(changed_roots),
        )
        try:
            payload = {"changed_roots": changed_roots, "task_id": task_id}
            data = json.dumps(payload).encode("utf-8")
            req = request.Request(
                self.config.curator_url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with request.urlopen(req, timeout=30) as response:
                if response.status not in (200, 204):
                    raise ValueError(f"Curator returned HTTP {response.status}")
                record_event(
                    "curator rebuild accepted",
                    event="hook_curator_accepted",
                    status=response.status,
                )
                return True
        except Exception as exc:
            msg = f"failed to trigger Curator rebuild: {exc}"
            record_event(msg, level="error")
            raise RuntimeError(msg) from exc

    def _run_hook(self, changed_roots: list[str]) -> None:
        if not self.config.hook_command:
            return
        filtered_roots = [
            r for r in changed_roots if not is_internal_category(r.split("/", 1)[0])
        ]
        if not filtered_roots:
            return

        cmd = shlex.split(self.config.hook_command)
        cmd.extend(filtered_roots)
        command_text = self._format_hook_command(
            cmd,
            library_root_count=len(filtered_roots),
        )
        with self.hook_lock:
            self.hook_phase = "running_hook"
        record_event(
            "running configured media update command: "
            f"{len(filtered_roots)} library root(s)",
            event="hook_running_command",
            changed_roots=len(filtered_roots),
            command=command_text,
        )
        try:
            result = subprocess.run(
                cmd,
                check=True,
                timeout=60,
                capture_output=True,
                text=True,
            )
            record_event(
                "configured media update command completed: "
                f"{len(filtered_roots)} library root(s)\n"
                f"{self._format_command_output(command_text, result.stdout, result.stderr)}",
                event="hook_command_finished",
                changed_roots=len(filtered_roots),
                command=command_text,
            )
        except subprocess.TimeoutExpired as exc:
            self._log_hook_error(
                "configured media update command timed out after "
                f"{exc.timeout}s: {exc.cmd}",
                command_text,
                exc.stdout,
                exc.stderr,
            )
        except subprocess.CalledProcessError as exc:
            self._log_hook_error(
                "configured media update command failed with exit code "
                f"{exc.returncode}: {exc.cmd}",
                command_text,
                exc.stdout,
                exc.stderr,
            )
        except Exception as exc:
            record_event(
                f"configured media update command failed: {exc}",
                level="error",
            )

    @staticmethod
    def _format_hook_command(
        cmd: list[str],
        *,
        library_root_count: int,
    ) -> str:
        base_count = max(1, len(cmd) - library_root_count)
        base = shlex.join(cmd[:base_count])
        args = [shlex.quote(arg) for arg in cmd[base_count:]]
        if not args:
            return base

        lines = [f"{base} \\"]
        for index, arg in enumerate(args):
            suffix = " \\" if index < len(args) - 1 else ""
            lines.append(f"    {arg}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _format_block_scalar(label: str, value: str | bytes | None) -> str:
        text = (
            value.decode(errors="replace")
            if isinstance(value, bytes)
            else value or ""
        )
        text = text.rstrip("\n")
        if not text:
            return f"    {label}: \"\""
        lines = [f"    {label}: |"]
        lines.extend(f"        {line}" for line in text.splitlines())
        return "\n".join(lines)

    @classmethod
    def _format_command_output(
        cls,
        command: str,
        stdout: str | bytes | None = None,
        stderr: str | bytes | None = None,
    ) -> str:
        parts = [
            cls._format_block_scalar("command", command),
        ]
        if stdout is not None or stderr is not None:
            parts.append(cls._format_block_scalar("stdout", stdout))
            parts.append(cls._format_block_scalar("stderr", stderr))
        return "\n".join(parts)

    def _log_hook_error(
        self,
        message: str,
        command: str,
        stdout: str | bytes | None,
        stderr: str | bytes | None,
    ) -> None:
        record_event(
            f"{message}\n{self._format_command_output(command, stdout, stderr)}",
            level="error",
        )

    def mark_startup_sync_complete(self) -> None:
        """Flag that the initial startup sync has finished."""
        with self.lock:
            self.startup_sync_complete = True
        self._notify_ui_change("sync")

    def is_ready(self) -> bool:
        """Return True when the library is ready to serve DAV requests."""
        return self.snapshot_loaded or (
            self.startup_sync_complete and self.last_sync_at is not None
        )

    def lookup(self, path: str) -> SnapshotNode | None:
        """Return the snapshot node for a path, or None if not found."""
        normalized = normalize_posix_path(path)
        with self.lock:
            self._ensure_snapshot_indexes()
            if not normalized:
                return {"type": "dir", "modified": self.last_sync_at}
            if normalized in self.snapshot.get("files", {}):
                return self.snapshot["files"][normalized]
            if normalized in self._dirs_set:
                return {"type": "dir", "modified": self.last_sync_at}
        return None

    def list_children(self, path: str) -> list[str]:
        """Return sorted names of immediate children for a directory path."""
        normalized = normalize_posix_path(path)
        with self.lock:
            self._ensure_snapshot_indexes()
            return list(self._children_by_dir.get(normalized, ()))

    def status(self) -> StatusReport:
        """Return current sync and hook status as a plain dict."""
        with self.lock, self.hook_lock:
            file_selection_pending_count = sum(
                1
                for cached in self.cache.values()
                if isinstance(cached, dict)
                and self._file_selection_pending(cached.get("info"))
            )
            return {
                "last_sync_at": self.last_sync_at,
                "sync_in_progress": self.sync_in_progress,
                "provider_degraded": self.provider_degraded,
                "file_selection_pending": file_selection_pending_count > 0,
                "file_selection_pending_count": file_selection_pending_count,
                "last_error": self.last_error,
                "snapshot_loaded": self.snapshot_loaded,
                "hook_pending": bool(self.hook_pending_paths),
                "hook_in_progress": self.hook_in_progress,
                "hook_last_started_at": self.hook_last_started_at,
                "hook_last_finished_at": self.hook_last_finished_at,
                "hook_last_error": self.hook_last_error,
                "hook_phase": self.hook_phase,
                "hook_active_paths": list(self.hook_active_paths),
                "hook_pending_count": len(self.hook_pending_paths),
                "hook_wait_started_at": self.hook_wait_started_at,
                "background_tasks": self.background_tasks.snapshot(),
            }

    @staticmethod
    def _file_selection_pending(info: object) -> bool:
        """Return True when a cached torrent has no selected files."""
        if not isinstance(info, dict):
            return False
        return not any(
            file_item.get("selected")
            for file_item in info.get("files", [])
            if isinstance(file_item, dict)
        )

    def torrents(self) -> list[TorrentSummary]:
        """Return a sorted list of cached torrent summaries."""
        name_hints = db.load_torrent_name_hints(self.conn)
        results = []
        with self.lock:
            for torrent_id, cached in self.cache.items():
                info = cached.get("info") if isinstance(cached, dict) else None
                if not isinstance(info, dict):
                    continue

                name = self.builder._torrent_name(info)
                thash = str(info.get("hash") or "").strip().lower()
                if _is_hash_name(name):
                    name = (
                        name_hints.get(thash)
                        or magnet_display_name(
                            cached.get("magnet") or "" if isinstance(cached, dict) else ""
                        )
                        or _safe_file_root_name(info)
                        or name
                    )

                selected_files = sum(
                    1 for f in info.get("files", []) if f.get("selected")
                )
                results.append(
                    {
                        "id": torrent_id,
                        "provider": str(info.get("provider") or ""),
                        "provider_torrent_id": str(
                            info.get("provider_torrent_id")
                            or info.get("id")
                            or torrent_id
                        ),
                        "name": name,
                        "status": info.get("status", "unknown"),
                        "progress": info.get("progress", 0),
                        "bytes": info.get("bytes", 0),
                        "selected_files": selected_files,
                        "file_selection_pending": selected_files == 0,
                        "links": len(info.get("links") or []),
                        "ended": info.get("ended"),
                        "category": self._effective_category_for_info(info),
                        "category_override": self.category_overrides.get(thash),
                    }
                )
        return sorted(results, key=lambda x: x["name"])

    def _effective_category_for_info(self, info: TorrentInfo) -> str:
        thash = str(info.get("hash") or "").strip().lower()
        override = self.category_overrides.get(thash)
        if override:
            return override
        selected = self.builder._selected_files(info)
        for item in selected:
            item.pop("category_override", None)
        return (
            self.builder._category_for(selected)
            if selected
            else self.builder.category_name_for_kind("movie") or "movies"
        )

    def torrent_files(self, cache_key: str) -> list[dict[str, Any]]:
        """Return the file list for a cached torrent, for the UI selector."""
        with self.lock:
            cached = self.cache.get(cache_key)
            info = cached.get("info") if isinstance(cached, dict) else None
            if not isinstance(info, dict):
                return []
            files = list(info.get("files", []))
        results: list[dict[str, Any]] = []
        for file_item in files:
            if not isinstance(file_item, dict):
                continue
            path = str(file_item.get("path") or "")
            results.append(
                {
                    "id": str(file_item.get("id") or ""),
                    "path": path,
                    "bytes": int(file_item.get("bytes") or 0),
                    "is_video": is_video_file(path),
                    "selected": bool(file_item.get("selected")),
                }
            )
        results.sort(key=lambda item: item["path"])
        return results

    def torrent_category(self, cache_key: str | None) -> dict[str, str]:
        """Return override and effective category for a cached torrent."""
        if not cache_key:
            return {
                "override": "",
                "effective": self.builder.category_name_for_kind("movie")
                or "movies",
            }
        with self.lock:
            cached = self.cache.get(cache_key)
            info = cached.get("info") if isinstance(cached, dict) else None
            if not isinstance(info, dict):
                return {
                    "override": "",
                    "effective": self.builder.category_name_for_kind("movie")
                    or "movies",
                }
            thash = str(info.get("hash") or "").strip().lower()
            override = self.category_overrides.get(thash, "")
            effective = self._effective_category_for_info(cast(TorrentInfo, info))
        return {"override": override, "effective": effective}

    def set_torrent_category(
        self, cache_key: str, category: str | None
    ) -> OperationResult:
        """Set or clear a per-torrent category override."""
        normalized = str(category or "").strip()
        if normalized == "auto":
            normalized = ""
        if normalized and normalized not in self.category_kinds:
            raise ValueError(f"invalid category override: {category}")
        with self.lock:
            cached = self.cache.get(cache_key)
            info = cached.get("info") if isinstance(cached, dict) else None
            if not isinstance(info, dict):
                raise ValueError("torrent not found")
            thash = str(info.get("hash") or "").strip().lower()
            if not thash:
                raise ValueError("torrent hash is missing")
            db.save_category_override(self.conn, thash, normalized or None)
            if normalized:
                self.category_overrides[thash] = normalized
                info["category_override"] = normalized
            else:
                self.category_overrides.pop(thash, None)
                info.pop("category_override", None)
            self._save_cache_entry(cache_key, cast(dict[str, Any], cached))
        self._refresh_snapshot_from_cache()
        return {"status": "success"}

    def toggle_config_favorite(self, section: str) -> bool:
        """Flip the favorite state of a config section, returning the new state."""
        section = section.strip()
        if not section:
            return False
        with self.lock:
            now_favorite = section not in self.config_favorites
            db.save_config_favorite(self.conn, section, now_favorite)
            if now_favorite:
                self.config_favorites.add(section)
            else:
                self.config_favorites.discard(section)
        return now_favorite

    def subtitle_query_override(self, cache_key: str | None, path: str) -> str:
        """Return the per-file subtitle query override (empty if unset)."""
        normalized_path = normalize_posix_path(path)
        if not cache_key or not normalized_path:
            return ""
        with self.lock:
            cached = self.cache.get(cache_key)
            info = cached.get("info") if isinstance(cached, dict) else None
            if not isinstance(info, dict):
                return ""
            thash = str(info.get("hash") or "").strip().lower()
            if not thash:
                return ""
            return self.subtitle_query_overrides.get((thash, normalized_path), "")

    def set_subtitle_query_override(
        self, cache_key: str, path: str, query: str | None
    ) -> OperationResult:
        """Set or clear a per-file subtitle search query override."""
        normalized_path = normalize_posix_path(path)
        if not normalized_path:
            raise ValueError("file path is missing")
        normalized_query = str(query or "").strip()
        with self.lock:
            cached = self.cache.get(cache_key)
            info = cached.get("info") if isinstance(cached, dict) else None
            if not isinstance(info, dict):
                raise ValueError("torrent not found")
            thash = str(info.get("hash") or "").strip().lower()
            if not thash:
                raise ValueError("torrent hash is missing")
            db.save_subtitle_query_override(
                self.conn, thash, normalized_path, normalized_query or None
            )
            if normalized_query:
                self.subtitle_query_overrides[(thash, normalized_path)] = (
                    normalized_query
                )
            else:
                self.subtitle_query_overrides.pop((thash, normalized_path), None)
        return {"status": "success"}

    def curator_title_override(
        self, cache_key: str | None
    ) -> dict[str, Any]:
        """Return the entry-level Curator naming override (empty if unset)."""
        if not cache_key:
            return {}
        with self.lock:
            cached = self.cache.get(cache_key)
            info = cached.get("info") if isinstance(cached, dict) else None
            if not isinstance(info, dict):
                return {}
            thash = str(info.get("hash") or "").strip().lower()
            if not thash:
                return {}
            override = self.curator_title_overrides.get(thash, {})
            return dict(override)

    @staticmethod
    def _normalized_provider_ids(override: dict[str, Any]) -> dict[str, str]:
        provider_ids: dict[str, str] = {}
        raw_provider_ids = override.get("provider_ids")
        if isinstance(raw_provider_ids, dict):
            for provider in _PROVIDER_ID_PRIORITY:
                value = str(raw_provider_ids.get(provider) or "").strip()
                if value:
                    provider_ids[provider] = value
        for provider in _PROVIDER_ID_PRIORITY:
            value = str(override.get(provider) or "").strip()
            if value:
                provider_ids[provider] = value
        return provider_ids

    @staticmethod
    def _normalized_curator_title_override(
        override: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not override:
            return {}
        kind = str(override.get("kind") or "").strip()
        if kind not in {"movie", "show", "anime"}:
            raise ValueError(f"invalid curator title override kind: {kind}")

        result: dict[str, Any] = {"kind": kind}
        if kind == "movie":
            title = str(override.get("title") or "").strip()
            if title:
                result["title"] = title
            year = str(override.get("year") or "").strip()
            if year:
                result["year"] = int(year)
        else:
            # ``show`` and ``anime`` share the same series-shaped fields.
            series = str(override.get("series") or "").strip()
            if series:
                result["series"] = series
            year = str(override.get("year") or "").strip()
            if year:
                result["year"] = int(year)

        external_id = str(override.get("id") or "").strip()
        if external_id:
            result["id"] = external_id
        provider_ids = BuzzState._normalized_provider_ids(override)
        if provider_ids:
            result["provider_ids"] = provider_ids
        parse_regex = str(override.get("parse_regex") or "").strip()
        if parse_regex:
            try:
                re.compile(parse_regex)
            except re.error as exc:
                raise ValueError(f"invalid parse regex: {exc}") from exc
            result["parse_regex"] = parse_regex
        return result

    @staticmethod
    def _primary_provider_id(provider_ids: dict[str, str]) -> str:
        for provider in _PROVIDER_ID_PRIORITY:
            value = provider_ids.get(provider)
            if value:
                return f"{provider}-{value}"
        return ""

    @staticmethod
    def _representative_video_path(info: dict[str, Any]) -> str:
        """Return a video file path to seed entry-level title suggestions."""
        files = info.get("files")
        if isinstance(files, list):
            for file in files:
                path = str((file or {}).get("path") or "")
                if path and is_video_file(path):
                    return path
        return ""

    def _local_curator_title_suggestion(
        self, cache_key: str, kind: str
    ) -> dict[str, Any]:
        with self.lock:
            cached = self.cache.get(cache_key)
            info = cached.get("info") if isinstance(cached, dict) else None
            if not isinstance(info, dict):
                raise ValueError("torrent not found")
            torrent_name = self.builder._torrent_name(cast(TorrentInfo, info))
            path = self._representative_video_path(info)

        normalized_path = normalize_posix_path(path)
        stem = Path(normalized_path).stem
        provider_ids = {
            **_provider_ids_from_text(torrent_name),
            **_provider_ids_from_text(normalized_path),
        }
        if kind == "movie":
            parsed = parse_movie(stem, folder=torrent_name) or {}
            suggestion: dict[str, Any] = {
                "kind": "movie",
                "title": parsed.get("title") or "",
                "year": parsed.get("year") or "",
            }
        else:
            # ``show`` and ``anime`` are both series-shaped: derive a series
            # name/year via ``parse_show`` and preserve the incoming kind so an
            # anime override is recorded as ``kind=anime``.
            parsed = parse_show(stem) or {}
            series = parsed.get("series") or _valid_provider_name(torrent_name)
            suggestion = {
                "kind": kind if kind == "anime" else "show",
                "series": series,
                "year": parsed.get("year") or "",
            }
        if provider_ids:
            suggestion["provider_ids"] = provider_ids
        return self._normalized_curator_title_override(suggestion)

    def _jellyfin_curator_title_suggestion(
        self, local: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.config.jellyfin_url or not self.config.jellyfin_api_key:
            return {}
        kind = str(local.get("kind") or "")
        endpoint = "Movie" if kind == "movie" else "Series"
        name = str(local.get("title") or local.get("series") or "").strip()
        if not name:
            return {}
        payload: dict[str, Any] = {"Name": name}
        if local.get("year"):
            payload["Year"] = int(local["year"])
        url = f"{self.config.jellyfin_url.rstrip('/')}/Items/RemoteSearch/{endpoint}"
        try:
            response = httpx.post(
                url,
                headers={
                    "Authorization": (
                        f"MediaBrowser Token={self.config.jellyfin_api_key}"
                    )
                },
                json=payload,
                timeout=5.0,
            )
            response.raise_for_status()
            results = response.json()
        except Exception as exc:
            record_event(
                f"jellyfin metadata suggestion failed: {exc}",
                level="warning",
                event="jellyfin_metadata_suggestion_failed",
            )
            return {}
        if not isinstance(results, list) or not results:
            return {}
        first = results[0]
        if not isinstance(first, dict):
            return {}
        return self._jellyfin_result_to_curator_title(local, first, kind)

    def _jellyfin_result_to_curator_title(
        self, local: dict[str, Any], result: dict[str, Any], kind: str
    ) -> dict[str, Any]:
        suggestion = dict(local)
        if result.get("Name"):
            target_field = "title" if kind == "movie" else "series"
            suggestion[target_field] = str(result["Name"])
        if result.get("ProductionYear"):
            suggestion["year"] = int(result["ProductionYear"])
        provider_ids = self._jellyfin_provider_ids(result.get("ProviderIds"))
        if provider_ids:
            suggestion["provider_ids"] = provider_ids
        return self._normalized_curator_title_override(suggestion)

    @staticmethod
    def _jellyfin_provider_ids(raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        normalized = {}
        for key, value in raw.items():
            provider = _JELLYFIN_PROVIDER_ID_KEYS.get(str(key).lower())
            identifier = str(value).strip()
            if provider and identifier:
                normalized[provider] = identifier
        return normalized

    def local_curator_title_suggestion(
        self, cache_key: str, kind: str
    ) -> dict[str, Any]:
        """Locally derived (no network) identity suggestion for prefill."""
        return self._local_curator_title_suggestion(cache_key, kind)

    def suggest_curator_title_override(
        self, cache_key: str, kind: str
    ) -> dict[str, Any]:
        """Suggest Jellyfin-oriented Curator override fields for an entry."""
        local = self._local_curator_title_suggestion(cache_key, kind)
        remote = self._jellyfin_curator_title_suggestion(local)
        return remote or local

    def set_curator_title_override(
        self,
        cache_key: str,
        override: dict[str, Any] | None,
    ) -> OperationResult:
        """Set or clear an entry-level Curator naming override."""
        normalized = self._normalized_curator_title_override(override)
        changed_root = ""
        with self.lock:
            cached = self.cache.get(cache_key)
            info = cached.get("info") if isinstance(cached, dict) else None
            if not isinstance(info, dict):
                raise ValueError("torrent not found")
            thash = str(info.get("hash") or "").strip().lower()
            if not thash:
                raise ValueError("torrent hash is missing")
            db.save_curator_title_override(
                self.conn, thash, normalized or None
            )
            if normalized:
                self.curator_title_overrides[thash] = normalized
            else:
                self.curator_title_overrides.pop(thash, None)
            category = self._effective_category_for_info(cast(TorrentInfo, info))
            torrent_name = self.builder._torrent_name(cast(TorrentInfo, info))
            changed_root = f"{category}/{torrent_name}"
        if changed_root:
            self._enqueue_hook([changed_root])
        return {"status": "success"}

    def add_magnet(self, magnet: str, provider: str | None = None) -> TorrentInfo:
        """Add a magnet link using provider priority and fallback."""
        errors: list[str] = []
        if provider and provider != "auto":
            if provider == "local":
                raise ValueError("local provider cannot receive magnet adds")
            if provider not in self.clients:
                raise ValueError(f"provider '{provider}' is not enabled")
            _index = 0
            client = self.clients[provider]
            torrent_id = client.add_magnet(magnet)
        else:
            for _index, (provider, client) in enumerate(self._fallback_clients()):
                try:
                    torrent_id = client.add_magnet(magnet)
                    break
                except Exception as exc:
                    errors.append(f"{provider}: {exc}")
            else:
                raise ValueError(f"Failed to add magnet: {'; '.join(errors)}")

        info = self._detail_to_info(client.get_torrent(torrent_id))
        info["provider"] = provider
        info["provider_torrent_id"] = torrent_id
        cache_key = self._cache_key(provider, torrent_id)
        self._apply_display_names([(provider, cache_key, info)])
        filename = self.builder._torrent_name(info)
        warning = None
        if _index > 0:
            warning = f"magnet add fell back to {provider}: {'; '.join(errors)}"
            record_event(warning, level="warning", event="provider_add_fallback")

        already_exists = False
        self._apply_cached_file_selection(provider, cache_key, info)
        with self.lock:
            for cached in self.cache.values():
                cached_info = cached.get("info", {})
                if (
                    cached_info.get("filename") == filename
                    or cached_info.get("original_filename") == filename
                ):
                    already_exists = True
                    break

            self.cache[cache_key] = {
                "signature": {},
                "info": info,
                "magnet": magnet,
            }
            self._save_cache_entry(cache_key, self.cache[cache_key])

        result = {
            "id": torrent_id,
            "cache_key": cache_key,
            "filename": filename,
            "already_exists": already_exists,
            "files": info.get("files", []),
            "provider": provider,
        }
        if warning:
            result["warning"] = warning
        return result

    def attach_poller(self, poller: Poller | None) -> None:
        """Attach a background poller to the state."""
        self._poller = poller

    def request_sync(self) -> None:
        """Ask the background poller to run a sync at its next opportunity."""
        if self._poller is not None:
            self._poller.wake()

    def _submit_background_task(
        self,
        *,
        kind: str,
        label: str,
        run: Callable[[str, threading.Event], None],
        manual: bool = False,
    ) -> str:
        """Submit state-owned work to the UI-visible background task pool."""
        if manual:
            return self.background_tasks.submit_manual(kind, label, run)
        return self.background_tasks.submit(kind, label, run)

    def submit_provider_migration_scan(
        self, source_provider: str, destination_provider: str
    ) -> str:
        """Queue a provider-to-provider migration candidate scan."""
        source_provider = source_provider.strip().lower()
        destination_provider = destination_provider.strip().lower()
        if destination_provider == "local":
            raise ValueError(
                "local provider is not a migration destination; "
                "use the archive copy-to-local action"
            )
        if source_provider == destination_provider:
            raise ValueError("source and destination providers must differ")
        if self._client_for_provider(source_provider) is None:
            raise ValueError(f"{source_provider} provider is not configured")
        if self._client_for_provider(destination_provider) is None:
            raise ValueError(
                f"{destination_provider} provider is not configured"
            )

        def run_scan(task_id: str, cancel_event: threading.Event) -> None:
            candidates = self._collect_migration_candidates(
                source_provider,
                destination_provider,
                cancel_event,
            )
            if not candidates:
                record_event(
                    "provider migration scan complete: no matches",
                    event="provider_migration_scan_complete",
                    source_provider=source_provider,
                    destination_provider=destination_provider,
                    candidate_count=0,
                )
                return
            task_id = self._register_provider_migration_commit(
                source_provider,
                destination_provider,
                candidates,
            )
            record_event(
                "provider migration scan complete: "
                f"{len(candidates)} candidate(s); commit pending: {task_id}",
                level="warning",
                event="provider_migration_scan_complete",
                source_provider=source_provider,
                destination_provider=destination_provider,
                candidate_count=len(candidates),
                commit_task_id=task_id,
            )

        return self._submit_background_task(
            kind="maintenance",
            label=(
                "scan_provider_migration: "
                f"{self._provider_label(source_provider)} -> "
                f"{self._provider_label(destination_provider)}"
            ),
            run=run_scan,
        )

    def _collect_migration_candidates(
        self,
        source_provider: str,
        destination_provider: str,
        cancel_event: threading.Event,
    ) -> list[MigrationCandidate]:
        with self.lock:
            cache_items = list(self.cache.items())
        destination_hashes = {
            str(info.get("hash") or "").strip().lower()
            for provider, _cache_key, info in self._cache_infos(cache_items)
            if provider == destination_provider
        }
        candidates: list[MigrationCandidate] = []
        for provider, cache_key, info in self._cache_infos(cache_items):
            raise_if_cancelled(cancel_event)
            if provider != source_provider:
                continue
            thash = str(info.get("hash") or "").strip().lower()
            if not thash or thash in destination_hashes:
                continue
            cached = dict(self.cache.get(cache_key) or {})
            magnet = str(cached.get("magnet") or "").strip()
            if not magnet:
                magnet = f"magnet:?xt=urn:btih:{thash}"
            candidates.append(
                {
                    "cache_key": cache_key,
                    "torrent_id": str(
                        info.get("provider_torrent_id")
                        or info.get("id")
                        or ""
                    ),
                    "hash": thash,
                    "name": str(
                        info.get("filename")
                        or info.get("original_filename")
                        or cache_key
                    ),
                    "magnet": magnet,
                    "selected_paths": self._selected_file_paths(info),
                }
            )
        record_event(
            "scanned provider migration candidates: "
            f"{source_provider} -> {destination_provider}: {len(candidates)}",
            event="provider_migration_scan_progress",
            source_provider=source_provider,
            destination_provider=destination_provider,
            candidate_count=len(candidates),
        )
        return candidates

    def _cache_infos(
        self, cache_items: list[tuple[str, Any]]
    ) -> list[tuple[str, str, TorrentInfo]]:
        infos: list[tuple[str, str, TorrentInfo]] = []
        for cache_key, cached in cache_items:
            provider, _provider_torrent_id, normalized_key = (
                self._split_cache_key(cache_key)
            )
            if not isinstance(cached, dict):
                continue
            info = cached.get("info")
            if not isinstance(info, dict):
                continue
            infos.append((provider, normalized_key, cast(TorrentInfo, info)))
        return infos

    def _register_provider_migration_commit(
        self,
        source_provider: str,
        destination_provider: str,
        candidates: list[MigrationCandidate],
    ) -> str:
        unique = {str(item["hash"]): item for item in candidates}

        def run_commit(task_id: str, cancel_event: threading.Event) -> None:
            self._commit_provider_migration(
                source_provider,
                destination_provider,
                list(unique.values()),
                cancel_event,
            )

        return self._submit_background_task(
            kind="maintenance",
            label=(
                f"commit_provider_migration: {len(unique)} torrent(s) "
                f"{self._provider_label(source_provider)} -> "
                f"{self._provider_label(destination_provider)}"
            ),
            run=run_commit,
            manual=True,
        )

    def submit_archive_provider_transfer(
        self, thash: str, destination_provider: str
    ) -> str:
        """Register a manual provider transfer for an archived torrent."""
        clean_hash = thash.strip().lower()
        destination_provider = destination_provider.strip().lower()
        if self._client_for_provider(destination_provider) is None:
            raise ValueError(f"{destination_provider} provider is not configured")
        with self.lock:
            entry = self.archive.get(clean_hash)
            if not entry:
                raise ValueError("Torrent not found in archive")
            name = str(entry.get("name") or clean_hash)
            magnet = str(
                entry.get("magnet") or f"magnet:?xt=urn:btih:{clean_hash}"
            )
            selected_paths = [
                normalize_posix_path(str(item.get("path") or ""))
                for item in entry.get("files", [])
                if isinstance(item, dict) and str(item.get("path") or "").strip()
            ]
        if destination_provider == "local":
            return self._register_local_copy(clean_hash, name, magnet)
        links = db.load_provider_links_by_hash(self.conn, clean_hash)
        source_provider = next(
            (
                provider
                for provider, _provider_torrent_id in links
                if provider != destination_provider
            ),
            "",
        )
        candidates: list[MigrationCandidate] = [
            {
                "hash": clean_hash,
                "magnet": magnet,
                "name": name,
                "source_provider": source_provider,
                "selected_paths": selected_paths,
            }
        ]
        return self._register_provider_migration_commit(
            source_provider or "archive",
            destination_provider,
            candidates,
        )

    def _commit_provider_migration(
        self,
        source_provider: str,
        destination_provider: str,
        candidates: list[MigrationCandidate],
        cancel_event: threading.Event,
    ) -> None:
        client = self._client_for_provider(destination_provider)
        if client is None:
            raise ValueError(
                f"{destination_provider} provider is not configured"
            )
        added = 0
        skipped = 0
        failed = 0
        for item in candidates:
            raise_if_cancelled(cancel_event)
            thash = str(item["hash"]).strip().lower()
            if self._provider_has_hash(destination_provider, thash):
                skipped += 1
                record_event(
                    "provider migration skipped existing destination hash: "
                    f"{item['name']}",
                    event="provider_migration_skipped",
                    source_provider=str(item.get("source_provider") or source_provider),
                    destination_provider=destination_provider,
                    hash=thash,
                )
                continue
            try:
                torrent_id = client.add_magnet(str(item["magnet"]))
                detail = client.get_torrent(torrent_id)
                info = self._detail_to_info(detail)
                info["provider"] = destination_provider
                info["provider_torrent_id"] = torrent_id
                source_paths = {
                    normalize_posix_path(p)
                    for p in cast(list[str], item.get("selected_paths") or [])
                    if str(p).strip()
                }
                selected_ids = self._matching_destination_file_ids(
                    info,
                    source_paths,
                    thash=thash,
                    warned_paths=self._file_selection_unresolved_warnings,
                )
                cache_key = self._cache_key(destination_provider, torrent_id)
                if source_paths:
                    # The migration transfers an explicit selection; persist it
                    # by hash+path so it drives both providers and survives
                    # restarts, then re-fetch to reflect the applied selection.
                    all_paths = {
                        normalize_posix_path(str(f.get("path") or ""))
                        for f in info.get("files", [])
                        if str(f.get("path") or "").strip()
                    }
                    with self.lock:
                        self.file_selections[thash] = source_paths
                        db.save_file_selection(
                            self.conn, thash, source_paths, all_paths
                        )
                    if selected_ids:
                        client.select_files(torrent_id, selected_ids)
                        detail = client.get_torrent(torrent_id)
                        info = self._detail_to_info(detail)
                        info["provider"] = destination_provider
                        info["provider_torrent_id"] = torrent_id

                self._apply_cached_file_selection(
                    destination_provider, cache_key, info
                )
                with self.lock:
                    self.cache[cache_key] = {
                        "signature": {},
                        "info": info,
                        "magnet": item["magnet"],
                    }
                    self._save_cache_entry(cache_key, self.cache[cache_key])
                added += 1
                record_event(
                    "provider migration added torrent: "
                    f"{item['name']} -> {destination_provider}",
                    event="provider_migration_added",
                    source_provider=str(item.get("source_provider") or source_provider),
                    destination_provider=destination_provider,
                    hash=thash,
                    torrent_id=torrent_id,
                )
            except Exception as exc:
                failed += 1
                record_event(
                    f"provider migration failed for {item['name']}: {exc}",
                    level="warning",
                    event="provider_migration_item_failed",
                    source_provider=str(item.get("source_provider") or source_provider),
                    destination_provider=destination_provider,
                    hash=thash,
                )
        self._queue_sync_after_task("sync provider migration")
        record_event(
            "provider migration commit complete: "
            f"{added} added, {skipped} skipped, {failed} failed",
            event="provider_migration_commit_complete",
            source_provider=source_provider,
            destination_provider=destination_provider,
            added_torrents=added,
            skipped_torrents=skipped,
            failed_torrents=failed,
        )

    def _register_local_copy(self, thash: str, name: str, magnet: str) -> str:
        """Register a manual copy-to-local task for an archived entry."""
        if "local" not in self.clients:
            raise ValueError("local provider is not configured")
        if db.load_local_torrent(self.conn, thash) is not None:
            raise ValueError("local copy already exists")

        def run_copy(task_id: str, cancel_event: threading.Event) -> None:
            self._execute_local_copy(thash, name, magnet, cancel_event)

        return self._submit_background_task(
            kind="maintenance",
            label=f"copy_to_local: {name}",
            run=run_copy,
            manual=True,
        )

    def _execute_local_copy(
        self,
        thash: str,
        name: str,
        magnet: str,
        cancel_event: threading.Event,
    ) -> None:
        """Copy an entry's selected files from a debrid provider onto disk."""
        if "local" not in self.clients:
            raise ValueError("local provider is not configured")
        if db.load_local_torrent(self.conn, thash) is not None:
            record_event(
                f"local copy already exists: {name}",
                event="local_copy_skipped",
                hash=thash,
            )
            return
        source = self._local_copy_source(thash)
        if source is None:
            source = self._restore_source_for_local_copy(
                thash, magnet, cancel_event
            )
        provider, client, files = source
        total_bytes = sum(size for _path, size, _ref in files)
        self._ensure_local_capacity(total_bytes, name)

        store = Path(self.config.local_path)
        temp_dir = store / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        copied: list[dict[str, Any]] = []
        try:
            for path, size, ref in files:
                raise_if_cancelled(cancel_event)
                download_url = client.resolve_stream(ref)
                self._copy_stream_to_store(
                    download_url,
                    temp_dir,
                    store / thash / path,
                    cancel_event,
                    name,
                )
                copied.append(
                    {
                        "path": path,
                        "bytes": size,
                        "stored_rel_path": f"{thash}/{path}",
                    }
                )
            db.save_local_torrent(self.conn, thash, name, total_bytes, copied)
        except BaseException:
            shutil.rmtree(store / thash, ignore_errors=True)
            raise
        record_event(
            f"local copy complete: {name}: {len(copied)} file(s) "
            f"from {self._provider_label(provider)}",
            event="local_copy_complete",
            hash=thash,
            provider=provider,
            file_count=len(copied),
            bytes=total_bytes,
        )
        self._queue_sync_after_task("sync local copy")

    def _copy_stream_to_store(
        self,
        download_url: str,
        temp_dir: Path,
        final_path: Path,
        cancel_event: threading.Event,
        name: str,
    ) -> None:
        """Stream one file to a temp path and atomically move it into place."""
        temp_path = temp_dir / f"{uuid.uuid4().hex}.part"
        bytes_since_check = 0
        timeout = max(1, int(self.config.request_timeout_secs))
        try:
            with (
                httpx.Client(timeout=timeout, follow_redirects=True) as http_client,
                http_client.stream("GET", download_url) as response,
            ):
                response.raise_for_status()
                with open(temp_path, "wb") as handle:
                    for chunk in response.iter_bytes(LOCAL_COPY_CHUNK_BYTES):
                        raise_if_cancelled(cancel_event)
                        handle.write(chunk)
                        bytes_since_check += len(chunk)
                        if bytes_since_check >= LOCAL_COPY_RECHECK_BYTES:
                            bytes_since_check = 0
                            self._ensure_local_capacity(0, name)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, final_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _local_copy_source(
        self, thash: str
    ) -> tuple[str, Any, list[tuple[str, int, str]]] | None:
        """Return the highest-priority provider with a live streamable copy."""
        with self.lock:
            cache_items = list(self._full_cache.items())
        infos = self._cache_infos(cache_items)
        for provider, client in self._fallback_clients():
            for prov, _cache_key, info in infos:
                if prov != provider:
                    continue
                if str(info.get("hash") or "").strip().lower() != thash:
                    continue
                if str(info.get("status") or "") != "downloaded":
                    continue
                files = self._streamable_files(info)
                if files:
                    return provider, client, files
        return None

    def _restore_source_for_local_copy(
        self,
        thash: str,
        magnet: str,
        cancel_event: threading.Event,
    ) -> tuple[str, Any, list[tuple[str, int, str]]]:
        """Restore via magnet, then wait until the entry is streamable."""
        _ = magnet
        record_event(
            "no live provider link; restoring via magnet before local copy",
            event="local_copy_restore",
            hash=thash,
        )
        result = self.restore_archive(thash)
        provider = str(result.get("provider") or "")
        torrent_id = str(
            result.get("provider_torrent_id") or result.get("id") or ""
        )
        client = self._client_for_provider(provider)
        if client is None:
            raise RuntimeError(f"no client configured for {provider}")
        deadline = time.monotonic() + LOCAL_COPY_RESTORE_WAIT_SECS
        while True:
            raise_if_cancelled(cancel_event)
            info = self._detail_to_info(client.get_torrent(torrent_id))
            files = self._streamable_files(info)
            if str(info.get("status") or "") == "downloaded" and files:
                return provider, client, files
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"restored torrent is not streamable yet on "
                    f"{self._provider_label(provider)}; retry the local copy later"
                )
            time.sleep(LOCAL_COPY_RESTORE_POLL_SECS)

    def _streamable_files(
        self, info: TorrentInfo
    ) -> list[tuple[str, int, str]]:
        """Return (normalized path, bytes, stream ref) for linked files."""
        results: list[tuple[str, int, str]] = []
        for item in self.builder._selected_files(info):
            path = normalize_posix_path(str(item.get("path") or ""))
            ref = str(item.get("url") or "")
            if path and ref:
                results.append((path, int(item.get("bytes") or 0), ref))
        return results

    def _local_disk_budget(self) -> tuple[int, int]:
        """Return (used_bytes, cap_bytes) for the local store filesystem."""
        store = Path(self.config.local_path)
        store.mkdir(parents=True, exist_ok=True)
        stats = os.statvfs(store)
        capacity = stats.f_blocks * stats.f_frsize
        used = (stats.f_blocks - stats.f_bavail) * stats.f_frsize
        percent = max(
            1, min(100, int(self.config.local_max_fs_usage_percent))
        )
        return used, capacity * percent // 100

    def local_copy_budget_remaining(self) -> int | None:
        """Return bytes available under the disk-usage cap, or None."""
        if not self.config.local_path:
            return None
        try:
            used, cap = self._local_disk_budget()
        except OSError:
            return None
        return max(0, cap - used)

    def _ensure_local_capacity(self, additional_bytes: int, name: str) -> None:
        """Refuse or abort a copy that would breach the disk-usage cap."""
        used, cap = self._local_disk_budget()
        if used + max(0, additional_bytes) <= cap:
            return
        message = (
            f"local copy aborted: {name}: projected disk usage exceeds "
            f"{self.config.local_max_fs_usage_percent}% of the store filesystem"
        )
        record_event(message, level="warning", event="local_copy_limit")
        raise RuntimeError(message)

    def delete_local_copy(self, thash: str) -> OperationResult:
        """Remove a local copy's files and records, keeping debrid links."""
        clean_hash = thash.strip().lower()
        client = self.clients.get("local")
        if client is None:
            raise ValueError("local provider is not configured")
        client.delete_torrent(clean_hash)
        cache_key = self._cache_key("local", clean_hash)
        local_prefix = f"local://{clean_hash}/"
        with self.lock:
            removed = self.cache.pop(cache_key, None)
            self._full_cache.pop(cache_key, None)
            if removed is not None:
                self._delete_cache_entry(cache_key)
            for ref in [
                ref
                for ref in self.resolved_urls
                if ref.startswith(local_prefix)
            ]:
                del self.resolved_urls[ref]
        record_event(
            f"local copy deleted: {clean_hash}",
            event="local_copy_deleted",
            hash=clean_hash,
        )
        self._queue_sync_after_task("sync local delete")
        self._notify_ui_change("archive")
        return {"status": "success"}

    def _client_for_provider(self, provider: str) -> Any | None:
        if provider == "real_debrid":
            return self.clients.get(provider) or self.client
        return self.clients.get(provider)

    def _provider_has_hash(self, provider_name: str, thash: str) -> bool:
        with self.lock:
            cache_items = list(self.cache.items())
        return any(
            provider == provider_name
            and str(info.get("hash") or "").strip().lower() == thash
            for provider, _cache_key, info in self._cache_infos(cache_items)
        )

    def _selected_file_paths(self, info: TorrentInfo) -> list[str]:
        return [
            normalize_posix_path(str(item.get("path") or ""))
            for item in self.builder._selected_files(info)
            if str(item.get("path") or "").strip()
        ]

    @staticmethod
    def _matching_destination_file_ids(
        info: TorrentInfo,
        source_paths: set[str],
        thash: str = "",
        warned_paths: set[tuple[str, str]] | None = None,
    ) -> list[str]:
        if not source_paths:
            return []
        path_to_ids: dict[str, list[str]] = {}
        file_segments: list[tuple[str, tuple[str, ...]]] = []
        for item in info.get("files", []):
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            path = normalize_posix_path(str(item.get("path") or ""))
            if not path:
                continue
            file_id = str(item.get("id"))
            path_to_ids.setdefault(path, []).append(file_id)
            file_segments.append((file_id, split_path(path)))

        selected_ids: list[str] = []
        unresolved_paths: list[str] = []
        for source_path in source_paths:
            normalized_source_path = normalize_posix_path(source_path)
            if not normalized_source_path:
                continue
            exact_ids = path_to_ids.get(normalized_source_path)
            if exact_ids is not None:
                selected_ids.extend(exact_ids)
                continue

            source_segments = split_path(normalized_source_path)
            matches = BuzzState._matching_suffix_file_ids(
                file_segments, source_segments
            )
            if matches:
                selected_ids.extend(matches)
            else:
                unresolved_paths.append(normalized_source_path)

        for unresolved_path in unresolved_paths:
            warning_key = (thash, unresolved_path)
            if warned_paths is not None and warning_key in warned_paths:
                continue
            if warned_paths is not None:
                warned_paths.add(warning_key)
            record_event(
                "file selection path unresolved: " + unresolved_path,
                level="warning",
                event="file_selection_path_unresolved",
                hash=thash,
                path=unresolved_path,
            )
        return selected_ids

    @staticmethod
    def _matching_suffix_file_ids(
        file_segments: list[tuple[str, tuple[str, ...]]],
        source_segments: tuple[str, ...],
    ) -> list[str]:
        matches = [
            file_id
            for file_id, destination_segments in file_segments
            if BuzzState._segments_suffix_match(
                source_segments, destination_segments
            )
        ]
        if matches:
            return matches
        return [
            file_id
            for file_id, destination_segments in file_segments
            if BuzzState._segments_alias_suffix_match(
                source_segments, destination_segments
            )
        ]

    @staticmethod
    def _segments_suffix_match(
        a_segments: tuple[str, ...], b_segments: tuple[str, ...]
    ) -> bool:
        if not a_segments or not b_segments:
            return False
        if len(a_segments) <= len(b_segments):
            return b_segments[-len(a_segments) :] == a_segments
        return a_segments[-len(b_segments) :] == b_segments

    @staticmethod
    def _segments_alias_suffix_match(
        a_segments: tuple[str, ...], b_segments: tuple[str, ...]
    ) -> bool:
        return BuzzState._segments_suffix_match(
            tuple(BuzzState._path_alias_key(part) for part in a_segments),
            tuple(BuzzState._path_alias_key(part) for part in b_segments),
        )

    @staticmethod
    def _path_alias_key(value: str) -> str:
        cleaned = value.casefold().replace("&", " and ")
        return " ".join(re.sub(r"[^a-z0-9]+", " ", cleaned).split())

    @staticmethod
    def _provider_label(provider: str) -> str:
        return {
            "real_debrid": "Real-Debrid",
            "torbox": "TorBox",
            "local": "Local",
        }.get(provider, provider)

    def submit_infringing_scan(self) -> str:
        """Queue a Real-Debrid infringement scan maintenance task."""
        if "real_debrid" not in self.clients and self.client is None:
            raise ValueError("Real-Debrid provider is not configured")

        def run_scan(task_id: str, cancel_event: threading.Event) -> None:
            candidates = self._scan_realdebrid_infringing_files(cancel_event)
            if not candidates:
                record_event(
                    "infringing file scan complete: no matches",
                    event="rd_infringing_scan_complete",
                    flagged_files=0,
                    affected_torrents=0,
                )
                return
            task_id = self._register_infringing_cleanup(candidates)
            record_event(
                "infringing file scan complete: "
                f"{len(candidates)} file(s), "
                f"{len({item['cache_key'] for item in candidates})} torrent(s); "
                f"cleanup pending: {task_id}",
                level="warning",
                event="rd_infringing_scan_complete",
                flagged_files=len(candidates),
                affected_torrents=len({item["cache_key"] for item in candidates}),
                cleanup_task_id=task_id,
            )

        return self._submit_background_task(
            kind="maintenance",
            label="scan_rd_infringing",
            run=run_scan,
        )

    def start_background_task(self, task_id: str) -> OperationResult:
        """Start a pending manual background task."""
        if not self.background_tasks.start(task_id):
            raise ValueError(f"background task not startable: {task_id}")
        return {"status": "success"}

    def _scan_realdebrid_infringing_files(
        self,
        cancel_event: threading.Event,
    ) -> list[InfringingCandidate]:
        candidates: list[InfringingCandidate] = []
        files = self._realdebrid_probe_files()
        client = self.clients.get("real_debrid") or self.client
        if client is None:
            raise ValueError("Real-Debrid provider is not configured")
        record_event(
            f"scanning Real-Debrid files for infringement: {len(files)} file(s)",
            event="rd_infringing_scan_started",
            file_count=len(files),
        )
        for index, item in enumerate(files, start=1):
            raise_if_cancelled(cancel_event)
            source_url = str(item["source_url"])
            try:
                client.resolve_stream(source_url)
            except ProviderStreamError as exc:
                code = self._provider_error_code(exc.code)
                if code != "infringing_file":
                    continue
                candidates.append(item)
                record_event(
                    "infringing file detected: "
                    f"{item['name']} :: {item['path']}",
                    level="warning",
                    event="rd_infringing_file_detected",
                    torrent_id=item["torrent_id"],
                    cache_key=item["cache_key"],
                    source_url=source_url,
                    file_path=item["path"],
                )
            if index % 50 == 0:
                record_event(
                    f"infringing file scan progress: {index}/{len(files)}",
                    event="rd_infringing_scan_progress",
                    scanned_files=index,
                    file_count=len(files),
                )
        return candidates

    def _realdebrid_probe_files(self) -> list[InfringingCandidate]:
        files: list[InfringingCandidate] = []
        with self.lock:
            cache_items = list(self.cache.items())
        for cache_key, cached in cache_items:
            provider, provider_torrent_id, normalized_key = self._split_cache_key(
                cache_key
            )
            if provider != "real_debrid" or not isinstance(cached, dict):
                continue
            info = cached.get("info")
            if not isinstance(info, dict):
                continue
            for item in self.builder._selected_files(cast(TorrentInfo, info)):
                source_url = str(item.get("url") or "").strip()
                if not source_url:
                    continue
                files.append(
                    {
                        "cache_key": normalized_key,
                        "torrent_id": provider_torrent_id,
                        "source_url": source_url,
                        "path": normalize_posix_path(
                            str(item.get("path") or "")
                        ),
                        "name": str(
                            info.get("filename")
                            or info.get("original_filename")
                            or normalized_key
                        ),
                    }
                )
        return files

    def _register_infringing_cleanup(
        self,
        candidates: list[InfringingCandidate],
    ) -> str:
        unique = {str(item["cache_key"]): item for item in candidates}

        def run_cleanup(task_id: str, cancel_event: threading.Event) -> None:
            self._cleanup_infringing_torrents(list(unique.values()), cancel_event)

        return self._submit_background_task(
            kind="maintenance",
            label=f"cleanup_rd_infringing: {len(unique)} torrent(s)",
            run=run_cleanup,
            manual=True,
        )

    def _cleanup_infringing_torrents(
        self,
        candidates: list[InfringingCandidate],
        cancel_event: threading.Event,
    ) -> None:
        client = self.clients.get("real_debrid") or self.client
        if client is None:
            raise ValueError("Real-Debrid provider is not configured")
        deleted = 0
        for item in candidates:
            raise_if_cancelled(cancel_event)
            cache_key = str(item["cache_key"])
            torrent_id = str(item["torrent_id"])
            with self.lock:
                cached = self.cache.get(cache_key)
            if not isinstance(cached, dict):
                continue
            try:
                client.delete_torrent(torrent_id)
            except ProviderDeleteError as exc:
                if not self._is_already_deleted_response(exc):
                    raise RuntimeError(
                        f"failed to delete {torrent_id}: {exc.text}"
                    ) from exc
                record_event(
                    "infringing torrent already missing from Real-Debrid; "
                    "archiving locally",
                    level="warning",
                    event="rd_infringing_cleanup_missing",
                    torrent_id=torrent_id,
                )
            with self.lock:
                current = self.cache.get(cache_key)
                if isinstance(current, dict):
                    info = current.get("info")
                    if isinstance(info, dict) and info.get("hash"):
                        self._add_to_archive(
                            cast(TorrentInfo, info),
                            magnet=current.get("magnet"),
                        )
                    if cache_key in self.cache:
                        del self.cache[cache_key]
                        self._delete_cache_entry(cache_key)
            deleted += 1
            record_event(
                f"removed infringing Real-Debrid torrent: {item['name']}",
                level="warning",
                event="rd_infringing_torrent_removed",
                torrent_id=torrent_id,
                file_path=item["path"],
            )
        self._notify_ui_change("archive")
        self._queue_sync_after_task("sync infringing cleanup")
        record_event(
            f"infringing cleanup complete: {deleted} torrent(s)",
            event="rd_infringing_cleanup_complete",
            deleted_torrents=deleted,
        )

    @staticmethod
    def _provider_error_code(code: str) -> str:
        return str(code).strip().split(None, 1)[0]

    def submit_cache_selection(self, selections: CacheSelection) -> str:
        """Queue selected-file application and provider sync work."""
        clean_selections = {
            str(torrent_id): [str(file_id) for file_id in file_ids]
            for torrent_id, file_ids in selections.items()
            if file_ids
        }
        selection_count = len(clean_selections or selections)

        def run_cache_selection(task_id: str, cancel_event: threading.Event) -> None:
            for torrent_id, file_ids in clean_selections.items():
                raise_if_cancelled(cancel_event)
                self.select_files(torrent_id, file_ids)
            raise_if_cancelled(cancel_event)
            self._notify_ui_change("sync")
            if self._poller is not None:
                self.request_sync()
            else:
                self._submit_background_task(
                    kind="sync",
                    label="sync_cache_files_selected",
                    run=self._sync_task,
                )

        return self._submit_background_task(
            kind="cache",
            label=f"cache_selections: {selection_count} torrent(s)",
            run=run_cache_selection,
        )

    def _queue_sync_after_task(self, label: str) -> None:
        self._notify_ui_change("sync")
        if self._poller is not None:
            self.request_sync()
        else:
            self._submit_background_task(
                kind="sync",
                label=label,
                run=self._sync_task,
            )

    def submit_archive_restore(self, thash: str) -> str:
        """Queue archive restore and provider sync work."""
        clean_hash = str(thash).strip()
        with self.lock:
            entry = self.archive.get(clean_hash)
            if not entry:
                raise ValueError("Torrent not found in archive")
            name = str(entry.get("name") or clean_hash)

        def run_restore(task_id: str, cancel_event: threading.Event) -> None:
            raise_if_cancelled(cancel_event)
            result = self.restore_archive(clean_hash)
            provider = self._provider_label(str(result.get("provider") or ""))
            selected_files = int(result.get("selected_files") or 0)
            total_files = int(result.get("total_files") or 0)
            provider_torrent_id = str(
                result.get("provider_torrent_id") or result.get("id") or ""
            )
            record_event(
                "restored archive: "
                f"provider={provider}, "
                f"files_marked={selected_files}/{total_files}, "
                f"torrent_id={provider_torrent_id}",
                event="archive_restore_complete",
                provider=result.get("provider"),
                provider_torrent_id=provider_torrent_id,
                selected_files=selected_files,
                total_files=total_files,
            )
            raise_if_cancelled(cancel_event)
            self._queue_sync_after_task("sync_after_restore")

        task_id = self._submit_background_task(
            kind="restore",
            label=f"restore_archive: {name}",
            run=run_restore,
        )
        record_event(
            f"restore queued: {task_id}",
            event="archive_restore_queued",
            link_to_task_id=task_id,
        )
        return task_id

    def cancel_background_task(self, task_id: str) -> OperationResult:
        """Request cancellation for a background task."""
        task_kind = next(
            (
                str(task.get("kind") or "")
                for task in self.background_tasks.snapshot()
                if task.get("id") == task_id
            ),
            "",
        )
        if not self.background_tasks.cancel(task_id):
            raise ValueError(f"background task not cancellable: {task_id}")
        if task_kind == "subtitles":
            self._cancel_curator_subtitle_task(task_id)
        return {"status": "success"}

    def _cancel_curator_subtitle_task(self, task_id: str) -> None:
        """Best-effort cancellation signal for a proxied Curator subtitle task."""
        if not self.config.curator_url:
            return
        url = self.config.curator_url.replace(
            "/rebuild", "/api/subtitles/cancel"
        )
        try:
            data = json.dumps({"task_id": task_id}).encode("utf-8")
            req = request.Request(
                url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with request.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            record_event(
                f"subtitle cancellation signal failed: {exc}",
                level="warning",
            )

    def complete_background_task(
        self, task_id: str, error: str | None = None, status: TaskStatus | None = None
    ) -> OperationResult:
        """Manually transition a task to complete or failed status."""
        if not self.background_tasks.complete(task_id, error=error, status=status):
            raise ValueError(f"background task not found or not running: {task_id}")
        return {"status": "success"}

    def _sync_task(self, task_id: str, cancel_event: threading.Event) -> None:
        raise_if_cancelled(cancel_event)
        self.sync()

    def delete_torrent(self, torrent_id: str) -> str:
        """Queue provider removal and local archive for a torrent; returns task id."""
        provider, provider_torrent_id, cache_key = self._split_cache_key(
            torrent_id
        )
        with self.lock:
            cached = self.cache.get(cache_key)

        # Derive the canonical hash for multi-provider lookup.
        thash: str = ""
        if cached:
            info = cached.get("info")
            if isinstance(info, dict):
                thash = str(info.get("hash") or "").strip().lower()

        targets = self._resolve_delete_targets(
            provider, provider_torrent_id, thash, cached, torrent_id
        )
        label = self._derive_delete_label(torrent_id, cached)

        def run_delete(task_id: str, cancel_event: threading.Event) -> None:
            self._execute_background_delete(
                cancel_event,
                targets,
                (provider, provider_torrent_id),
                thash,
                cached,
                cache_key,
            )

        return self._submit_background_task(
            kind="delete",
            label=f"remove_from_cache: {label}",
            run=run_delete,
        )

    def _resolve_delete_targets(
        self,
        provider: str,
        provider_torrent_id: str,
        thash: str,
        cached: dict | None,
        torrent_id: str,
    ) -> list[tuple[str, str]]:
        """Build the full target set from DB: every provider copy sharing this hash."""
        primary = (provider, provider_torrent_id)
        if thash:
            db_targets = db.load_provider_links_by_hash(self.conn, thash)
            return list(dict.fromkeys([primary] + db_targets))
        if cached or ":" in torrent_id:
            return [primary]
        return [(p, torrent_id) for p in self.clients]

    def _derive_delete_label(self, torrent_id: str, cached: dict | None) -> str:
        """Derive a human-friendly label for the delete task."""
        if cached:
            info = cached.get("info")
            if isinstance(info, dict):
                return str(info.get("filename") or info.get("id") or torrent_id)
        return torrent_id

    def _execute_background_delete(
        self,
        cancel_event: threading.Event,
        targets: list[tuple[str, str]],
        primary: tuple[str, str],
        thash: str,
        cached: dict | None,
        cache_key: str,
    ) -> None:
        """Execute the actual removal from providers and local state."""
        primary_errors: list[str] = []
        already_missing = False
        # Extract torrent_id for error reporting
        torrent_id = cache_key if ":" in cache_key else primary[1]

        for prov, pid in targets:
            is_primary = (prov, pid) == primary
            raise_if_cancelled(cancel_event)
            client = self.clients.get(prov) or self.client
            if client is None:
                continue
            try:
                client.delete_torrent(pid)
            except ProviderDeleteError as exc:
                if self._is_already_deleted_response(exc):
                    already_missing = True
                    record_event(
                        "torrent already missing from provider; archived locally",
                        level="warning",
                    )
                else:
                    self._handle_delete_error(exc, prov, pid, is_primary, torrent_id)
                    if is_primary:
                        primary_errors.append(f"{prov}: {exc.text}")

        if primary_errors:
            raise RuntimeError(
                f"Failed to delete torrent: {'; '.join(primary_errors)}"
            )

        raise_if_cancelled(cancel_event)
        with self.lock:
            if cached:
                info = cached.get("info")
                if isinstance(info, dict) and info.get("hash"):
                    self._add_to_archive(info, magnet=cached.get("magnet"))
                    db.delete_library_entry(self.conn, str(info["hash"]))
            if cache_key in self.cache:
                del self.cache[cache_key]
                self._delete_cache_entry(cache_key)
        self._notify_ui_change("archive")
        if already_missing:
            self.request_sync()

    def _handle_delete_error(
        self,
        exc: ProviderDeleteError,
        prov: str,
        pid: str,
        is_primary: bool,
        torrent_id: str,
    ) -> None:
        """Log and track provider-specific deletion errors."""
        attempts = getattr(exc, "attempts", 1)
        msg = f"gave up removing {pid} from {prov} after {attempts} attempt(s): {exc.text}"
        record_event(
            msg,
            level="error" if is_primary else "warning",
            event="provider_delete_failed",
            torrent_id=torrent_id,
        )

    def _is_already_deleted_response(self, response: object) -> bool:
        status_code = getattr(response, "status_code", None)
        if status_code in (404, 410):
            return True
        text = str(getattr(response, "text", "")).lower()
        return any(
            phrase in text
            for phrase in (
                "not found",
                "not_found",
                "does not exist",
                "unknown torrent",
                "invalid torrent id",
            )
        )

    def _add_to_archive(self, info: TorrentInfo, magnet: str | None = None) -> None:
        thash = info.get("hash")
        if not thash:
            return
        self.archive[thash] = {
            "hash": thash,
            "name": (
                info.get("display_name")
                or info.get("filename")
                or info.get("original_filename")
                or magnet_display_name(magnet or "")
                or _safe_file_root_name(info)
                or "Unknown"
            ),
            "bytes": info.get("bytes", 0),
            "files": [
                {
                    "id": f.get("id"),
                    "path": f.get("path"),
                    "bytes": f.get("bytes"),
                }
                for f in info.get("files", [])
                if f.get("selected")
            ],
            "deleted_at": utc_now_iso(),
            "magnet": magnet,
        }
        self._save_archive_entry(thash, self.archive[thash])

    def archive_torrents(self) -> list[TorrentSummary]:
        """Return archived (deleted) torrents sorted by deletion time."""
        name_hints = db.load_torrent_name_hints(self.conn)
        with self.lock:
            results = []
            for thash, entry in self.archive.items():
                name = str(entry.get("name") or "Unknown")
                if _is_hash_name(name):
                    name = (
                        name_hints.get(thash.lower())
                        or magnet_display_name(entry.get("magnet") or "")
                        or name
                    )
                results.append(
                    {
                        "hash": thash,
                        "name": name,
                        "bytes": entry.get("bytes", 0),
                        "file_count": len(entry.get("files", [])),
                        "deleted_at": entry.get("deleted_at"),
                        "magnet": entry.get("magnet"),
                    }
                )
            return sorted(results, key=lambda x: x["deleted_at"] or "", reverse=True)

    def restore_archive(self, thash: str) -> OperationResult:
        """Re-add an archived torrent using provider priority and fallback."""
        with self.lock:
            entry = self.archive.get(thash)
            if not entry:
                raise ValueError("Torrent not found in archive")

        magnet = entry.get("magnet") or f"magnet:?xt=urn:btih:{thash}"
        errors: list[str] = []
        for _index, (provider, client) in enumerate(self._fallback_clients()):
            try:
                torrent_id = client.add_magnet(magnet)
                break
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
        else:
            raise ValueError(f"Failed to restore torrent: {'; '.join(errors)}")

        file_ids = [str(f["id"]) for f in entry.get("files", []) if f.get("id")]
        total_files = 0
        with contextlib.suppress(Exception):
            detail = client.get_torrent(torrent_id)
            total_files = len(self._detail_to_info(detail).get("files", []))
        if file_ids:
            try:
                self.select_files(self._cache_key(provider, torrent_id), file_ids)
            except Exception as exc:
                record_event(f"failed to auto-select files during restore: {exc}", level="error")

        with self.lock:
            if thash in self.archive:
                del self.archive[thash]
                self._delete_archive_entry(thash)
        self._notify_ui_change("archive")

        result = {
            "status": "success",
            "id": torrent_id,
            "provider": provider,
            "provider_torrent_id": torrent_id,
            "selected_files": len(file_ids),
            "total_files": total_files,
        }
        if _index > 0:
            result["warning"] = f"restore fell back to {provider}: {'; '.join(errors)}"
            record_event(result["warning"], level="warning")
        return result

    def delete_archive_permanently(self, thash: str) -> OperationResult:
        """Remove an archived torrent entry, deleting provider copies if any exist."""
        provider_targets = db.load_provider_links_by_hash(self.conn, thash)
        for prov, pid in provider_targets:
            client = self.clients.get(prov) or self.client
            if client is None:
                continue
            try:
                client.delete_torrent(pid)
            except ProviderDeleteError as exc:
                if not self._is_already_deleted_response(exc):
                    record_event(
                        f"failed to delete {pid} from {prov} during archive purge: {exc.text}",
                        level="warning",
                        event="provider_delete_failed",
                    )
        with self.lock:
            if thash in self.archive:
                del self.archive[thash]
                self._delete_archive_entry(thash)
        if provider_targets:
            db.delete_library_entry(self.conn, thash)
        self._notify_ui_change("archive")
        return {"status": "success"}

    def _notify_ui_change(self, topic: str) -> None:
        if self.on_ui_change is None:
            return
        with contextlib.suppress(Exception):
            self.on_ui_change(topic)

    def select_files(
        self, torrent_id: str, file_ids: list[str]
    ) -> OperationResult:
        """Select which files to download for a torrent."""
        provider, provider_torrent_id, cache_key = self._split_cache_key(
            torrent_id
        )

        client = self.clients.get(provider) or self.client
        if client is None:
            raise RuntimeError("provider token is not configured.")

        requested_file_ids = {str(file_id) for file_id in file_ids}
        skip_provider_call = False
        provider_file_ids = requested_file_ids
        refreshed_info: TorrentInfo | None = None

        if provider == "real_debrid":
            # Fetch authoritative selection from the provider to compute the union.
            # RD only allows marking new files (additive selection).
            detail = client.get_torrent(provider_torrent_id)
            current_info = self._detail_to_info(detail)
            already_selected_ids = {
                str(f.get("id"))
                for f in current_info.get("files", [])
                if f.get("selected")
            }
            if requested_file_ids.issubset(already_selected_ids):
                # All requested files are already marked at the provider.
                # Skip the call to prevent the `action_already_done` error (HTTP 403).
                skip_provider_call = True
                refreshed_info = current_info
            else:
                provider_file_ids = requested_file_ids.union(already_selected_ids)
        if not skip_provider_call:
            client.select_files(provider_torrent_id, list(provider_file_ids))

            # For Real-Debrid, re-fetch the authoritative detail after selection so
            # per-file stream links are populated.
            if provider == "real_debrid":
                detail = client.get_torrent(provider_torrent_id)
                refreshed_info = self._detail_to_info(detail)

        selected_file_ids = requested_file_ids

        refresh_snapshot = False
        with self.lock:
            cached = self.cache.get(cache_key)
            if refreshed_info is not None:
                # RD: replace the cached detail with fresh file/link metadata,
                # but keep the user's requested file ids authoritative.
                if isinstance(cached, dict):
                    cached_entry = cast(dict[str, Any], cached)
                else:
                    cached_entry = {"signature": {}, "info": {}, "magnet": None}
                self._apply_file_selection(refreshed_info, selected_file_ids)
                refreshed_info["links"] = [
                    str(file_item.get("stream_ref") or "")
                    for file_item in refreshed_info.get("files", [])
                    if file_item.get("selected")
                    and str(file_item.get("stream_ref") or "")
                ]
                cached_entry["info"] = refreshed_info
                self.cache[cache_key] = cached_entry
                info = refreshed_info
                selected_paths = {
                    normalize_posix_path(str(f.get("path") or ""))
                    for f in info.get("files", [])
                    if str(f.get("id") or "") in selected_file_ids
                    and str(f.get("path") or "").strip()
                }
            else:
                # Other providers (e.g. TorBox): apply the requested selection to
                # the cached detail in place and rebuild links locally.
                info = cached.get("info") if isinstance(cached, dict) else None
                if not isinstance(info, dict):
                    return {"status": "success"}
                cached_entry = cast(dict[str, Any], cached)
                selected_paths = {
                    normalize_posix_path(str(f.get("path") or ""))
                    for f in info.get("files", [])
                    if str(f.get("id") or "") in selected_file_ids
                    and str(f.get("path") or "").strip()
                }
                self._apply_file_selection(info, selected_file_ids)
                if provider == "torbox":
                    self._rebuild_torbox_links(info, provider_torrent_id)

            # Persist the selection by hash+path so it survives restarts and
            # applies symmetrically across providers.
            thash = str(info.get("hash") or "").strip().lower()
            all_paths = {
                normalize_posix_path(str(f.get("path") or ""))
                for f in info.get("files", [])
                if str(f.get("path") or "").strip()
            }
            if thash:
                self.file_selections[thash] = selected_paths
                db.save_file_selection(
                    self.conn, thash, selected_paths, all_paths
                )
            self._save_cache_entry(cache_key, cached_entry)
            refresh_snapshot = True
        if refresh_snapshot:
            self._refresh_snapshot_from_cache()
        return {"status": "success"}

    def resolve_download_url(
        self, source_url: str, force_refresh: bool = False
    ) -> str:
        """Resolve a provider stream ref to a direct download link."""
        resolve_lock = self._download_url_resolve_lock(source_url)
        with resolve_lock:
            return self._resolve_download_url_locked(
                source_url, force_refresh=force_refresh
            )

    def _download_url_resolve_lock(self, source_url: str) -> threading.Lock:
        """Return the per-source lock used to coordinate provider calls."""
        with self.lock:
            resolve_lock = self._resolve_locks.get(source_url)
            if resolve_lock is None:
                resolve_lock = threading.Lock()
                self._resolve_locks[source_url] = resolve_lock
            return resolve_lock

    def _resolve_download_url_locked(
        self, source_url: str, *, force_refresh: bool = False
    ) -> str:
        """Resolve a provider stream ref while holding its source lock."""
        cached_url = self._check_resolved_url_cache(source_url, force_refresh)
        if cached_url:
            return cached_url

        sources = self._stream_sources_for(source_url)
        last_error = "no provider source available"
        for index, source in enumerate(sources):
            try:
                download_url, provider = self._attempt_provider_resolution(
                    source, source_url, index, len(sources)
                )
                break
            except (
                ProviderStreamResolutionError,
                HosterUnavailableError,
            ):
                raise
            except Exception as exc:
                last_error = str(exc)
                continue
        else:
            raise ValueError(last_error)

        with self.lock:
            self.resolved_urls[source_url] = {
                "download_url": download_url,
                "provider": provider,
            }
        return download_url

    def _check_resolved_url_cache(
        self, source_url: str, force_refresh: bool
    ) -> str | None:
        """Check if a resolved URL (or failure) is already in the local cache."""
        with self.lock:
            cached = self.resolved_urls.get(source_url)
            if cached and not force_refresh:
                download_url = str(cached.get("download_url", "")).strip()
                if download_url:
                    return download_url
                self._raise_cached_resolution_error(source_url, cached)
        return None

    def _raise_cached_resolution_error(
        self, source_url: str, cached: dict
    ) -> None:
        """Raise a cached resolution error if it hasn't expired yet."""
        error_code = cached.get("error")
        error_provider = str(cached.get("provider") or "")
        expires_at = cached.get("expires_at", 0.0)
        if error_code and time.monotonic() < float(expires_at):
            if error_provider and error_provider != "real_debrid":
                raise ProviderStreamResolutionError(
                    source_url,
                    error_provider,
                    str(error_code),
                    cached=True,
                )
            raise HosterUnavailableError(
                source_url, str(error_code), cached=True
            )

    def _attempt_provider_resolution(
        self,
        source: dict[str, str],
        source_url: str,
        index: int,
        total_sources: int,
    ) -> tuple[str, str]:
        """Attempt to resolve a stream via a specific provider source."""
        provider = source["provider"]
        provider_source_url = source["source_url"]

        if (
            self._is_http_source(provider_source_url)
            and provider != "real_debrid"
        ):
            raise ValueError(
                f"refusing to resolve HTTP source with {provider}: "
                f"{provider_source_url}"
            )

        client = self.clients.get(provider) or self.client
        if client is None:
            raise ValueError(f"no client configured for {provider}")

        try:
            download_url = client.resolve_stream(provider_source_url)
            if index > 0:
                record_event(
                    f"provider stream fallback used: {provider}",
                    level="warning",
                    event="provider_stream_fallback",
                    path=source_url,
                    provider=provider,
                )
            return download_url, provider
        except ProviderStreamError as exc:
            self._handle_provider_resolution_error(
                exc, source_url, provider, provider_source_url, index, total_sources
            )
            raise  # Should be unreachable due to handle method raising

    def _handle_provider_resolution_error(
        self,
        exc: ProviderStreamError,
        source_url: str,
        provider: str,
        provider_source_url: str,
        index: int,
        total_sources: int,
    ) -> None:
        """Handle and potentially cache provider-specific resolution failures."""
        error_msg = self._provider_error_code(exc.code)
        is_last = index == total_sources - 1
        provider_cacheable = error_msg in (
            PROVIDER_TRANSIENT_STREAM_ERRORS
            | PROVIDER_NON_TRANSIENT_STREAM_ERRORS
        )

        if is_last and (
            (provider != "real_debrid" and provider_cacheable)
            or (error_msg in RD_NON_TRANSIENT_ERRORS)
        ):
            ttl = max(1, int(self.config.rd_hoster_failure_cache_secs))
            with self.lock:
                self.resolved_urls[source_url] = {
                    "provider": provider,
                    "error": error_msg,
                    "expires_at": time.monotonic() + ttl,
                }
            if provider != "real_debrid" and provider_cacheable:
                raise ProviderStreamResolutionError(
                    source_url, provider, error_msg
                ) from exc
            raise HosterUnavailableError(source_url, error_msg) from exc

        raise RuntimeError(
            f"Failed to resolve download link for {provider_source_url}: "
            f"{error_msg}"
        )

    def _stream_sources_for(self, source_url: str) -> list[dict[str, str]]:
        sources = self.stream_sources.get(source_url)
        if sources:
            return sources
        if is_local_stream_ref(source_url):
            if "local" not in self.clients:
                return []
            return [{"provider": "local", "source_url": source_url}]
        if self._is_http_source(source_url):
            if "real_debrid" not in self.clients and (
                self.clients or self.client is None
            ):
                return []
            return [{"provider": "real_debrid", "source_url": source_url}]
        return [
            {
                "provider": next(iter(self.clients), "real_debrid"),
                "source_url": source_url,
            }
        ]

    def resolved_url_provider(self, source_url: str) -> str:
        """Return the provider associated with a stream source when known."""
        with self.lock:
            cached = self.resolved_urls.get(source_url)
            if isinstance(cached, dict):
                provider = str(cached.get("provider") or "").strip()
                if provider:
                    return provider
        sources = self._stream_sources_for(source_url)
        if sources:
            return str(sources[0].get("provider") or "real_debrid")
        return "real_debrid"

    @staticmethod
    def _is_http_source(source_url: str) -> bool:
        scheme = parse.urlsplit(source_url).scheme.lower()
        return scheme in {"http", "https"}

    def invalidate_download_url(self, source_url: str) -> None:
        """Remove any cached entry (positive or negative) for *source_url*."""
        with self.lock:
            self.resolved_urls.pop(source_url, None)

    def verbose_log(self, message: str) -> None:
        """Log a message at debug level when verbose mode is enabled."""
        if self.config.verbose:
            record_event(message, level="debug")

    def close(self) -> None:
        """Close the SQLite connection owned by this state instance."""
        if self._closed:
            return
        self.conn.close()
        self._closed = True

    def __del__(self) -> None:
        """Best-effort cleanup for tests and short-lived app instances."""
        with contextlib.suppress(Exception):
            self.close()


class Poller(threading.Thread):
    """Background thread that polls Real-Debrid on a configurable interval."""

    def __init__(self, state: BuzzState) -> None:
        """Initialize with a BuzzState to sync against."""
        super().__init__(daemon=True)
        self.state = state
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    def _format_change_message(
        self,
        added: list[str],
        removed: list[str],
        updated: list[str],
        synced: int,
        providers: list[str] | None = None,
        root_providers: dict[str, str] | None = None,
    ) -> str:
        return self.state._format_change_message(
            added,
            removed,
            updated,
            synced,
            providers,
            root_providers,
        )

    def run(self) -> None:
        """Poll Real-Debrid and let state sync emit change events."""
        while True:
            self._wake_event.wait(
                self.state.config.provider_poll_interval_secs
            )
            if self._stop_event.is_set():
                return
            self._wake_event.clear()
            try:
                self.state.sync()
            except Exception as exc:  # noqa: BLE001
                err_str = str(exc).lower()
                is_degraded = "provider_degraded" in err_str
                is_timeout = "time" in err_str and "out" in err_str

                with self.state.lock:
                    self.state.last_error = str(exc)
                    if is_timeout:
                        self.state.provider_degraded = True

                if is_timeout or is_degraded:
                    self.state._notify_ui_change("sync")

                if is_timeout:
                    record_event(f"background sync degraded: {exc}", level="warning")
                elif not is_degraded:
                    record_event(f"background sync failed: {exc}", level="error")

    def wake(self) -> None:
        """Request an immediate sync at the next loop iteration."""
        self._wake_event.set()

    def stop(self) -> None:
        """Signal the polling thread to stop."""
        self._stop_event.set()
        self._wake_event.set()


class InitialSync(threading.Thread):
    """One-shot thread that runs the startup sync and marks it complete."""

    def __init__(self, state: BuzzState) -> None:
        """Initialize with a BuzzState to sync against."""
        super().__init__(daemon=True)
        self.state = state

    def run(self) -> None:
        """Run a single sync without triggering hooks, then mark startup done."""
        try:
            report = self.state.sync(trigger_hook=False)
            record_event("startup sync complete", event="startup_sync", report=report)
        except Exception as exc:  # noqa: BLE001
            err_str = str(exc).lower()
            is_degraded = "provider_degraded" in err_str
            is_timeout = "time" in err_str and "out" in err_str

            with self.state.lock:
                self.state.last_error = str(exc)
                if is_timeout:
                    self.state.provider_degraded = True

            if is_timeout or is_degraded:
                self.state._notify_ui_change("sync")

            if is_timeout:
                record_event(f"startup sync degraded: {exc}", level="warning")
            elif not is_degraded:
                record_event(f"startup sync failed: {exc}", level="error")
        finally:
            self.state.mark_startup_sync_complete()


def read_range_header(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse a Range header and return (start, end) byte offsets, or None."""
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match:
        return None
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None
    if not start_text:
        length = min(size, int(end_text))
        return max(0, size - length), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size:
        return None
    return start, min(end, size - 1)
