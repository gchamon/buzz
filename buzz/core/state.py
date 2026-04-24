"""DAV filesystem state, library builder, and background sync threads."""

import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib import parse, request

from ..models import DavConfig
from . import db
from .constants import SHOW_PATTERNS
from .events import record_event
from .media import is_video_file
from .utils import (
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


def canonical_snapshot(snapshot: Snapshot) -> Snapshot:
    """Return a snapshot with volatile fields (modified, generated_at) stripped."""
    files = {}
    for path, node in snapshot.get("files", {}).items():
        if not isinstance(node, dict):
            files[path] = node
            continue
        canonical_node = {
            key: value for key, value in node.items() if key != "modified"
        }
        files[path] = canonical_node

    report = snapshot.get("report", {})
    canonical_report = report
    if isinstance(report, dict):
        canonical_report = {
            key: value for key, value in report.items() if key != "generated_at"
        }

    return {
        "dirs": snapshot.get("dirs", []),
        "files": files,
        "report": canonical_report,
    }


def is_internal_category(name: str) -> bool:
    """Return True for virtual categories like __all__ and __unplayable__."""
    return name.startswith("__")


class LibraryBuilder:
    """Builds a DAV filesystem snapshot from Real-Debrid torrent info."""

    def __init__(self, config: DavConfig) -> None:
        """Initialize with the DAV configuration."""
        self.config = config
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
                "modified": utc_now_iso(),
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
            if linked_playable:
                category = self._category_for(linked_playable)
                self._add_tree(files, dirs, category, torrent_name, linked_playable)
                if self.config.enable_all_dir:
                    self._add_tree(
                        files, dirs, "__all__", torrent_name, linked_playable
                    )
                current_roots.add(f"{category}/{torrent_name}")
                if category == "movies":
                    report["movies"] += len(linked_playable)
                elif category == "shows":
                    report["show_files"] += len(linked_playable)
                else:
                    report["anime_files"] += len(linked_playable)
            elif self.config.enable_unplayable_dir:
                reason = self._unplayable_reason(info, selected)
                count = self._add_unplayable_tree(
                    files, dirs, torrent_name, selected, reason
                )
                if count:
                    current_roots.add(f"__unplayable__/{torrent_name}")
                    report["unplayable_files"] += count

        snapshot = {
            "generated_at": report["generated_at"],
            "dirs": sorted(dirs),
            "files": files,
            "report": report,
        }
        return snapshot, sorted(current_roots)

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
            }
            results.append(entry)
        return results

    def _torrent_name(self, info: TorrentInfo) -> str:
        name = str(
            info.get("original_filename")
            or info.get("filename")
            or info.get("id")
            or "torrent"
        ).strip()
        name = name.replace("/", " ").replace("\\", " ").strip(". ")
        return name or str(info.get("id") or "torrent")

    def _category_for(self, entries: list[TorrentInfo]) -> str:
        for entry in entries:
            rel = entry["path"]
            if any(pattern.search(rel) for pattern in self.anime_regexes):
                return "anime"
        for entry in entries:
            if any(pattern.search(entry["path"]) for pattern in SHOW_PATTERNS):
                return "shows"
        return "movies"

    def _add_tree(
        self,
        files: dict[str, SnapshotNode],
        dirs: set[str],
        prefix: str,
        torrent_name: str,
        entries: list[TorrentInfo],
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
                "modified": utc_now_iso(),
                "etag": self._etag(path, entry["url"], entry["bytes"]),
            }

    def _add_unplayable_tree(
        self,
        files: dict[str, SnapshotNode],
        dirs: set[str],
        torrent_name: str,
        entries: list[TorrentInfo],
        reason: str,
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
            "modified": utc_now_iso(),
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
                "modified": utc_now_iso(),
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
        on_ui_change: Any | None = None,
    ) -> None:
        """Initialize state storage and load persisted data from disk."""
        self.config = config
        self.client = client
        self.builder = LibraryBuilder(config)
        self.lock = threading.RLock()
        self.state_dir = config.state_dir
        os.makedirs(self.state_dir, exist_ok=True)
        db_path = Path(self.state_dir) / "buzz.sqlite"
        self.conn = db.connect(db_path)
        db.apply_migrations(self.conn)
        db.migrate_legacy_files(self.conn, Path(self.state_dir))
        self.cache = self._load_cache()
        self.trashcan = self._load_archive()
        snapshot, digest = self._load_snapshot()
        self.snapshot = snapshot
        self.snapshot_digest = digest
        self.snapshot_loaded = self._snapshot_exists_in_db()
        self.last_sync_at = None
        self.last_report = {}
        self.last_error = None
        self.sync_in_progress = False
        self.startup_sync_complete = False
        self.hook_pending_paths: list[str] = []
        self.hook_in_progress = False
        self.hook_last_started_at = None
        self.hook_last_finished_at = None
        self.hook_last_error = None
        self.resolved_urls: dict[str, dict[str, str]] = {}
        self.hook_lock = threading.Lock()
        self.hook_task_active = False
        self._closed = False
        self.on_ui_change = on_ui_change

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

    def _load_snapshot(self) -> tuple[dict, str]:
        row = self.conn.execute(
            "SELECT snapshot_json, digest FROM library_snapshot WHERE singleton = 1"
        ).fetchone()
        if row is None:
            default: dict = {"dirs": [""], "files": {}}
            return default, stable_json(canonical_snapshot(default))
        snapshot = json.loads(row["snapshot_json"])
        return snapshot, row["digest"]

    def _save_snapshot(self, snapshot: dict, digest: str) -> None:
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
        if parts[0] not in {"movies", "shows", "anime"}:
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

    def sync(self, *, trigger_hook: bool = True) -> SyncReport:
        """Sync torrent state with Real-Debrid and rebuild the snapshot."""
        hook_paths: list[str] = []
        should_notify = False
        with self.lock:
            self.sync_in_progress = True
        try:
            summaries = self.client.torrents.get().json()
            new_cache: dict[str, TorrentInfo] = {}
            infos: list[TorrentInfo] = []
            for summary in summaries:
                torrent_id = str(summary.get("id", "")).strip()
                if not torrent_id:
                    continue
                signature = self._summary_signature(summary)
                with self.lock:
                    cached = self.cache.get(torrent_id)
                if (
                    cached
                    and cached.get("signature") == signature
                    and isinstance(cached.get("info"), dict)
                ):
                    info = cached["info"]
                else:
                    info = self.client.torrents.info(torrent_id).json()
                cached_magnet = cached.get("magnet") if cached else None
                new_cache[torrent_id] = {
                    "signature": signature,
                    "info": info,
                    "magnet": cached_magnet,
                }
                infos.append(info)

            snapshot, _current_roots = self.builder.build(infos)
            digest = stable_json(canonical_snapshot(snapshot))

            with self.lock:
                removed_torrent_ids = set(self.cache) - set(new_cache)
                for torrent_id in removed_torrent_ids:
                    cached = self.cache.get(torrent_id)
                    if not isinstance(cached, dict):
                        continue
                    info = cached.get("info")
                    if isinstance(info, dict) and info.get("hash"):
                        self._add_to_archive(info, magnet=cached.get("magnet"))

                changed = digest != self.snapshot_digest
                classified_changes = (
                    self._classified_changed_roots(self.snapshot, snapshot)
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
                report = dict(snapshot["report"])
                report["changed"] = changed
                report["changed_paths"] = changed_paths
                report.update(classified_changes)
                report["synced_torrents"] = len(infos)
                report["timestamp"] = utc_now_iso()

                self.cache = new_cache
                self._save_cache(self.cache)

                if changed:
                    self.snapshot = snapshot
                    self.snapshot_digest = digest
                    self._save_snapshot(self.snapshot, self.snapshot_digest)
                    self.snapshot_loaded = True
                    should_notify = True
                    if trigger_hook and (
                        self.config.hook_command or self.config.curator_url
                    ):
                        hook_paths = changed_paths

                self.last_sync_at = report["timestamp"]
                self.last_report = report
                self.last_error = None
            if hook_paths:
                self._enqueue_hook(hook_paths)
            if should_notify:
                self._notify_ui_change("sync")
            return report
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
            raise
        finally:
            with self.lock:
                self.sync_in_progress = False

    def _enqueue_hook(self, changed_roots: list[str]) -> None:
        pending = set(changed_roots)
        if not pending:
            return
        with self.hook_lock:
            merged = set(self.hook_pending_paths)
            merged.update(pending)
            self.hook_pending_paths = sorted(merged)
            if not self.hook_task_active:
                self.hook_task_active = True
                threading.Thread(target=self._run_hook_task, daemon=True).start()

    def _run_hook_task(self) -> None:
        try:
            while True:
                with self.hook_lock:
                    if not self.hook_pending_paths:
                        self.hook_task_active = False
                        return
                    paths = self.hook_pending_paths
                    self.hook_pending_paths = []
                    self.hook_in_progress = True
                    self.hook_last_started_at = utc_now_iso()
                    self.hook_last_error = None

                try:
                    self._trigger_curator_and_hooks(paths, skip_delay=False)
                except Exception as exc:  # noqa: BLE001
                    with self.hook_lock:
                        self.hook_last_error = str(exc)
                finally:
                    with self.hook_lock:
                        self.hook_in_progress = False
                        self.hook_last_finished_at = utc_now_iso()
        except Exception as exc:  # noqa: BLE001
            record_event(f"Hook task failed unexpectedly: {exc}", level="error")
            with self.hook_lock:
                self.hook_task_active = False

    def manual_rebuild(self) -> None:
        """Trigger curator rebuild and hooks immediately, skipping RD delay."""
        with self.hook_lock:
            self.hook_in_progress = True
            self.hook_last_started_at = utc_now_iso()
            self.hook_last_error = None
        try:
            self._trigger_curator_and_hooks([], skip_delay=True)
        except Exception as exc:
            with self.hook_lock:
                self.hook_last_error = str(exc)
            raise
        finally:
            with self.hook_lock:
                self.hook_in_progress = False
                self.hook_last_finished_at = utc_now_iso()

    def _summary_signature(self, summary: TorrentInfo) -> TorrentInfo:
        return {
            "filename": summary.get("filename"),
            "bytes": summary.get("bytes"),
            "progress": summary.get("progress"),
            "status": summary.get("status"),
            "ended": summary.get("ended"),
            "links": len(summary.get("links") or []),
        }

    def _trigger_curator_and_hooks(
        self, changed_roots: list[str], *, skip_delay: bool = False
    ) -> None:
        if not skip_delay:
            if self.config.library_mount and changed_roots:
                self._wait_for_vfs_visibility(changed_roots)
            elif self.config.rd_update_delay_secs > 0:
                self.verbose_log(
                    f"Waiting {self.config.rd_update_delay_secs}s for Real-Debrid update..."
                )
                time.sleep(self.config.rd_update_delay_secs)

        self._trigger_curator(changed_roots)
        self._run_hook(changed_roots)

    def _wait_for_vfs_visibility(self, roots: list[str]) -> None:
        mount = self.config.library_mount
        timeout = self.config.vfs_wait_timeout_secs
        start_time = time.time()

        # Determine current state of each root in our internal snapshot
        with self.lock:
            snapshot_roots = set()
            for path in self.snapshot.get("files", {}):
                root = self._root_for_snapshot_path(path)
                if root:
                    snapshot_roots.add(root)

        to_check = []
        for root in roots:
            # We only care about visibility of media roots
            if not any(root.startswith(p) for p in ["movies/", "shows/", "anime/"]):
                continue
            expected = root in snapshot_roots
            to_check.append((root, expected))

        if not to_check:
            return

        self.verbose_log(
            f"Waiting for VFS visibility of {len(to_check)} roots in {mount} (timeout {timeout}s)..."
        )

        while time.time() - start_time < timeout:
            all_visible = True
            missing = []
            stale = []

            for root, expected in to_check:
                path = os.path.join(mount, root)
                exists = os.path.exists(path)
                if expected and not exists:
                    all_visible = False
                    missing.append(root)
                elif not expected and exists:
                    all_visible = False
                    stale.append(root)

            if all_visible:
                elapsed = int(time.time() - start_time)
                self.verbose_log(f"VFS visibility confirmed after {elapsed}s")
                return

            # Periodically log progress if there are many items or we've waited a bit
            if int(time.time() - start_time) % 30 == 0:
                self.verbose_log(
                    f"VFS still syncing... (missing: {len(missing)}, stale: {len(stale)})"
                )

            time.sleep(2)

        self.verbose_log(
            f"VFS visibility timeout reached after {timeout}s. Proceeding with sync."
        )

    def _trigger_curator(self, changed_roots: list[str]) -> None:
        if not self.config.curator_url:
            return
        self.verbose_log(f"Triggering curator rebuild at {self.config.curator_url}...")
        try:
            payload = {"changed_roots": changed_roots}
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
                self.verbose_log("Curator rebuild triggered successfully")
        except Exception as exc:
            record_event(f"Failed to trigger curator rebuild: {exc}", level="error")

    def _run_hook(self, changed_roots: list[str]) -> None:
        if not self.config.hook_command:
            return
        # Filter out internal/virtual categories like __unplayable__ and __all__.
        filtered_roots = [
            r for r in changed_roots if not is_internal_category(r.split("/", 1)[0])
        ]
        if not filtered_roots:
            return

        self.verbose_log(f"Running library update hook: {self.config.hook_command}...")
        try:
            cmd = shlex.split(self.config.hook_command)
            cmd.extend(filtered_roots)
            subprocess.run(
                cmd,
                check=True,
                timeout=60,
                capture_output=True,
                text=True,
            )
            self.verbose_log("Library update hook completed successfully")
        except subprocess.TimeoutExpired as exc:
            details = [f"Library update hook timed out after {exc.timeout}s: {exc.cmd}"]
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            if stdout:
                details.append(f"stdout:\n{stdout}")
            if stderr:
                details.append(f"stderr:\n{stderr}")
            record_event("\n".join(details), level="error")
        except subprocess.CalledProcessError as exc:
            details = [
                f"Library update hook failed with exit code {exc.returncode}: {exc.cmd}"
            ]
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            if stdout:
                details.append(f"stdout:\n{stdout}")
            if stderr:
                details.append(f"stderr:\n{stderr}")
            record_event("\n".join(details), level="error")
        except Exception as exc:
            record_event(f"Library update hook failed: {exc}", level="error")

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
            if not normalized:
                return {"type": "dir", "modified": self.last_sync_at}
            if normalized in self.snapshot.get("files", {}):
                return self.snapshot["files"][normalized]
            if normalized in set(self.snapshot.get("dirs", [])):
                return {"type": "dir", "modified": self.last_sync_at}
        return None

    def list_children(self, path: str) -> list[str]:
        """Return sorted names of immediate children for a directory path."""
        normalized = normalize_posix_path(path)
        children = set()
        with self.lock:
            prefix = normalized + "/" if normalized else ""
            for child in self.snapshot.get("dirs", []):
                if child.startswith(prefix):
                    rel = child[len(prefix) :].split("/", 1)[0]
                    if rel:
                        children.add(rel)
            for child in self.snapshot.get("files", {}):
                if child.startswith(prefix):
                    rel = child[len(prefix) :].split("/", 1)[0]
                    if rel:
                        children.add(rel)
        return sorted(children)

    def status(self) -> StatusReport:
        """Return current sync and hook status as a plain dict."""
        with self.lock, self.hook_lock:
            return {
                "last_sync_at": self.last_sync_at,
                "sync_in_progress": self.sync_in_progress,
                "last_error": self.last_error,
                "snapshot_loaded": self.snapshot_loaded,
                "hook_pending": bool(self.hook_pending_paths),
                "hook_in_progress": self.hook_in_progress,
                "hook_last_started_at": self.hook_last_started_at,
                "hook_last_finished_at": self.hook_last_finished_at,
                "hook_last_error": self.hook_last_error,
            }

    def torrents(self) -> list[TorrentSummary]:
        """Return a sorted list of cached torrent summaries."""
        results = []
        with self.lock:
            for torrent_id, cached in self.cache.items():
                info = cached.get("info") if isinstance(cached, dict) else None
                if not isinstance(info, dict):
                    continue

                results.append(
                    {
                        "id": str(info.get("id") or torrent_id),
                        "name": self.builder._torrent_name(info),
                        "status": info.get("status", "unknown"),
                        "progress": info.get("progress", 0),
                        "bytes": info.get("bytes", 0),
                        "selected_files": sum(
                            1 for f in info.get("files", []) if f.get("selected")
                        ),
                        "links": len(info.get("links") or []),
                        "ended": info.get("ended"),
                    }
                )
        return sorted(results, key=lambda x: x["name"])

    def add_magnet(self, magnet: str) -> TorrentInfo:
        """Add a magnet link to Real-Debrid and return torrent metadata."""
        res = self.client.torrents.add_magnet(magnet).json()
        torrent_id = res.get("id")
        if not torrent_id:
            raise ValueError(f"Failed to add magnet: {res}")

        info = self.client.torrents.info(torrent_id).json()
        filename = (info.get("filename") or info.get("original_filename") or "").strip()

        already_exists = False
        with self.lock:
            for cached in self.cache.values():
                cached_info = cached.get("info", {})
                if (
                    cached_info.get("filename") == filename
                    or cached_info.get("original_filename") == filename
                ):
                    already_exists = True
                    break

            self.cache[torrent_id] = {
                "signature": {},
                "info": info,
                "magnet": magnet,
            }
            self._save_cache_entry(torrent_id, self.cache[torrent_id])

        return {
            "id": torrent_id,
            "filename": filename,
            "already_exists": already_exists,
            "files": info.get("files", []),
        }

    def delete_torrent(self, torrent_id: str) -> OperationResult:
        """Delete a torrent from Real-Debrid and archive it locally."""
        with self.lock:
            cached = self.cache.get(torrent_id)
            if cached:
                info = cached.get("info")
                if isinstance(info, dict) and info.get("hash"):
                    self._add_to_archive(info, magnet=cached.get("magnet"))

        res = self.client.torrents.delete(torrent_id)
        if res.status_code not in (200, 204):
            raise ValueError(f"Failed to delete torrent: {res.text}")

        with self.lock:
            if torrent_id in self.cache:
                del self.cache[torrent_id]
                self._delete_cache_entry(torrent_id)
        self._notify_ui_change("archive")
        return {"status": "success"}

    def _add_to_archive(self, info: TorrentInfo, magnet: str | None = None) -> None:
        thash = info.get("hash")
        if not thash:
            return
        self.trashcan[thash] = {
            "hash": thash,
            "name": info.get("filename") or info.get("original_filename") or "Unknown",
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
        self._save_archive_entry(thash, self.trashcan[thash])

    def archive_torrents(self) -> list[TorrentSummary]:
        """Return archived (deleted) torrents sorted by deletion time."""
        with self.lock:
            results = []
            for thash, entry in self.trashcan.items():
                results.append(
                    {
                        "hash": thash,
                        "name": entry.get("name", "Unknown"),
                        "bytes": entry.get("bytes", 0),
                        "file_count": len(entry.get("files", [])),
                        "deleted_at": entry.get("deleted_at"),
                        "magnet": entry.get("magnet"),
                    }
                )
            return sorted(results, key=lambda x: x["deleted_at"] or "", reverse=True)

    def restore_trash(self, thash: str) -> OperationResult:
        """Re-add an archived torrent to Real-Debrid by its hash."""
        with self.lock:
            entry = self.trashcan.get(thash)
            if not entry:
                raise ValueError("Torrent not found in trashcan")

        magnet = entry.get("magnet") or f"magnet:?xt=urn:btih:{thash}"
        res = self.client.torrents.add_magnet(magnet).json()
        torrent_id = res.get("id")
        if not torrent_id:
            raise ValueError(f"Failed to restore torrent: {res}")

        file_ids = [str(f["id"]) for f in entry.get("files", []) if f.get("id")]
        if file_ids:
            try:
                self.select_files(torrent_id, file_ids)
            except Exception as exc:
                record_event(f"Failed to auto-select files during restore: {exc}", level="error")

        with self.lock:
            if thash in self.trashcan:
                del self.trashcan[thash]
                self._delete_archive_entry(thash)
        self._notify_ui_change("archive")

        return {"status": "success", "id": torrent_id}

    def delete_trash_permanently(self, thash: str) -> OperationResult:
        """Remove an archived torrent entry without restoring it."""
        with self.lock:
            if thash in self.trashcan:
                del self.trashcan[thash]
                self._delete_archive_entry(thash)
        self._notify_ui_change("archive")
        return {"status": "success"}

    def _notify_ui_change(self, topic: str) -> None:
        if self.on_ui_change is None:
            return
        try:
            self.on_ui_change(topic)
        except Exception:
            pass

    def select_files(
        self, torrent_id: str, file_ids: list[str]
    ) -> OperationResult:
        """Select which files to download for a torrent."""
        files_str = ",".join(str(f) for f in file_ids)
        res = self.client.torrents.select_files(torrent_id, files_str)
        if res.status_code not in (200, 204):
            raise ValueError(f"Failed to select files: {res.text}")
        return {"status": "success"}

    def resolve_download_url(
        self, source_url: str, force_refresh: bool = False
    ) -> str:
        """Unrestrict a Real-Debrid source URL to a direct download link."""
        with self.lock:
            cached = self.resolved_urls.get(source_url)
            if cached and not force_refresh:
                download_url = cached.get("download_url", "").strip()
                if download_url:
                    return download_url

        try:
            res = self.client.unrestrict.link(source_url)
            data = res.json()
        except Exception as exc:
            raise ValueError(f"Failed to unrestrict {source_url}: {exc}") from exc

        download_url = data.get("download")
        if not download_url:
            error_msg = data.get("error") or "no download link in response"
            raise ValueError(
                f"Failed to resolve download link for {source_url}: {error_msg}"
            )

        with self.lock:
            self.resolved_urls[source_url] = {"download_url": download_url}
        return download_url

    def invalidate_download_url(self, source_url: str) -> None:
        """Remove a cached resolved URL so the next request re-unrestricts it."""
        with self.lock:
            if source_url in self.resolved_urls:
                del self.resolved_urls[source_url]

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
        try:
            self.close()
        except Exception:
            pass


class Poller(threading.Thread):
    """Background thread that polls Real-Debrid on a configurable interval."""

    def __init__(self, state: BuzzState) -> None:
        """Initialize with a BuzzState to sync against."""
        super().__init__(daemon=True)
        self.state = state
        self._stop_event = threading.Event()

    def _format_change_message(
        self,
        added: list[str],
        removed: list[str],
        updated: list[str],
        synced: int,
    ) -> str:
        lines = [f"Real-Debrid library changed ({synced} torrents):"]
        if added:
            lines.append(f"  +{len(added)} added")
            lines.extend(f"    {path}" for path in added)
        if removed:
            lines.append(f"  -{len(removed)} removed")
            lines.extend(f"    {path}" for path in removed)
        if updated:
            lines.append(f"  ~{len(updated)} updated")
            lines.extend(f"    {path}" for path in updated)
        return "\n".join(lines)

    def run(self) -> None:
        """Poll Real-Debrid and emit events when the library changes."""
        while not self._stop_event.wait(self.state.config.poll_interval_secs):
            try:
                report = self.state.sync()
                if report.get("changed"):
                    added = report.get("added_paths", [])
                    removed = report.get("removed_paths", [])
                    updated = report.get("updated_paths", [])
                    synced = report.get("synced_torrents", 0)
                    if not any((added, removed, updated)):
                        continue
                    record_event(
                        self._format_change_message(added, removed, updated, synced),
                        event="realdebrid_update",
                    )
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = str(exc)
                record_event(f"background sync failed: {exc}", level="error")

    def stop(self) -> None:
        """Signal the polling thread to stop."""
        self._stop_event.set()


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
            record_event("Startup sync complete", event="startup_sync", report=report)
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = str(exc)
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
