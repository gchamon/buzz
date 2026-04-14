#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import posixpath
import re
import shlex
import subprocess
import threading
import time
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any
from urllib import error, parse, request
from xml.sax.saxutils import escape

import yaml
from buzz.core.constants import DEFAULT_ANIME_PATTERN, SHOW_PATTERNS
from buzz.core.media import (
    is_probably_media_content_type,
    is_video_file,
    looks_like_markup,
)
from buzz.core.utils import (
    format_bytes,
    html_escape,
    http_date,
    normalize_posix_path,
    stable_json,
    utc_now_iso,
)
from pydantic import BaseModel, Field
from rdapi import RD

DEFAULT_CONFIG_PATH = os.environ.get("BUZZ_CONFIG", "/app/buzz.yml")


def dav_rel_path(raw_path: str) -> str:
    path = parse.urlsplit(raw_path).path
    if path.startswith("/dav"):
        path = path[len("/dav") :]
    return normalize_posix_path(parse.unquote(path))


def split_path(value: str) -> tuple[str, ...]:
    normalized = normalize_posix_path(value)
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part)


def canonical_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
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
    verbose: bool = False

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "DavConfig":
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
            anime_patterns=tuple(anime.get("patterns", [DEFAULT_ANIME_PATTERN])),
            enable_all_dir=bool(compat.get("enable_all_dir", True)),
            enable_unplayable_dir=bool(compat.get("enable_unplayable_dir", True)),
            request_timeout_secs=int(raw.get("request_timeout_secs", 30)),
            user_agent=str(raw.get("user_agent", "buzz/0.1")),
            version_label=str(raw.get("version_label", "buzz/0.1")),
            verbose=bool(logging.get("verbose", False)),
        )


class LibraryBuilder:
    def __init__(self, config: DavConfig):
        self.config = config
        self.anime_regexes = tuple(
            re.compile(pattern) for pattern in config.anime_patterns
        )

    def build(self, infos: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
        dirs: set[str] = {""}
        files: dict[str, dict[str, Any]] = {
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

    def _selected_files(self, info: dict[str, Any]) -> list[dict[str, Any]]:
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

    def _torrent_name(self, info: dict[str, Any]) -> str:
        name = str(
            info.get("original_filename")
            or info.get("filename")
            or info.get("id")
            or "torrent"
        ).strip()
        name = name.replace("/", " ").replace("\\", " ").strip(". ")
        return name or str(info.get("id") or "torrent")

    def _category_for(self, entries: list[dict[str, Any]]) -> str:
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
        files: dict[str, dict[str, Any]],
        dirs: set[str],
        prefix: str,
        torrent_name: str,
        entries: list[dict[str, Any]],
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
        files: dict[str, dict[str, Any]],
        dirs: set[str],
        torrent_name: str,
        entries: list[dict[str, Any]],
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
        self, info: dict[str, Any], selected: list[dict[str, Any]]
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
    def __init__(self, config: DavConfig, client: RD):
        self.config = config
        self.client = client
        self.builder = LibraryBuilder(config)
        self.lock = threading.RLock()
        self.state_dir = config.state_dir
        self.cache_path = os.path.join(self.state_dir, "torrent_cache.json")
        self.snapshot_path = os.path.join(self.state_dir, "library_snapshot.json")
        self.cache = self._load_json(self.cache_path, default={})
        self.snapshot_loaded = os.path.exists(self.snapshot_path)
        self.snapshot = self._load_json(
            self.snapshot_path, default={"dirs": [""], "files": {}}
        )
        self.snapshot_digest = stable_json(canonical_snapshot(self.snapshot))
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

    def _load_json(self, path: str, default: Any) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default

    def _write_json(self, path: str, payload: Any) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)

    def _root_for_snapshot_path(self, path: str) -> str | None:
        normalized = normalize_posix_path(path)
        if not normalized:
            return None
        parts = tuple(part for part in normalized.split("/") if part)
        if len(parts) < 2:
            return None
        if parts[0] == "__all__":
            return None
        if parts[0] not in {"movies", "shows", "anime", "__unplayable__"}:
            return None
        return "/".join(parts[:2])

    def _snapshot_root_signatures(self, snapshot: dict[str, Any]) -> dict[str, str]:
        root_entries: dict[str, dict[str, Any]] = {}
        canonical = canonical_snapshot(snapshot)
        for path, node in canonical.get("files", {}).items():
            root = self._root_for_snapshot_path(path)
            if not root:
                continue
            rel = path[len(root) + 1 :]
            entries = root_entries.setdefault(root, {})
            entries[rel] = node
        return {
            root: stable_json(entries) for root, entries in root_entries.items()
        }

    def _classified_changed_roots(
        self, previous_snapshot: dict[str, Any], new_snapshot: dict[str, Any]
    ) -> dict[str, list[str]]:
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

    def sync(self, *, trigger_hook: bool = True) -> dict[str, Any]:
        hook_paths: list[str] = []
        with self.lock:
            self.sync_in_progress = True
        try:
            summaries = self.client.torrents.get().json()
            new_cache: dict[str, dict[str, Any]] = {}
            infos: list[dict[str, Any]] = []
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
                new_cache[torrent_id] = {"signature": signature, "info": info}
                infos.append(info)

            snapshot, _current_roots = self.builder.build(infos)
            digest = stable_json(canonical_snapshot(snapshot))

            with self.lock:
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
                self._write_json(self.cache_path, self.cache)

                if changed:
                    self.snapshot = snapshot
                    self.snapshot_digest = digest
                    self._write_json(self.snapshot_path, self.snapshot)
                    self.snapshot_loaded = True
                    if trigger_hook and self.config.hook_command:
                        hook_paths = changed_paths

                self.last_sync_at = report["timestamp"]
                self.last_report = report
                self.last_error = None
            if hook_paths:
                self._enqueue_hook(hook_paths)
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
            print(f"Hook task failed unexpectedly: {exc}", flush=True)
            with self.hook_lock:
                self.hook_task_active = False

    def manual_rebuild(self) -> None:
        """Manually trigger curator rebuild and library hooks without RD delay."""
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

    def _summary_signature(self, summary: dict[str, Any]) -> dict[str, Any]:
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
        if not skip_delay and self.config.rd_update_delay_secs > 0:
            self.verbose_log(
                f"Waiting {self.config.rd_update_delay_secs}s for Real-Debrid update..."
            )
            time.sleep(self.config.rd_update_delay_secs)

        self._trigger_curator(changed_roots)
        self._run_hook(changed_roots)

    def _trigger_curator(self, changed_roots: list[str]) -> None:
        if not self.config.curator_url:
            return
        self.verbose_log(f"Triggering curator rebuild at {self.config.curator_url}...")
        try:
            req = request.Request(self.config.curator_url, method="POST")
            with request.urlopen(req, timeout=30) as response:
                if response.status not in (200, 204):
                    raise ValueError(f"Curator returned HTTP {response.status}")
                self.verbose_log("Curator rebuild triggered successfully")
        except Exception as exc:
            self.verbose_log(f"Curator rebuild failed: {exc}")
            raise

    def _run_hook(self, changed_roots: list[str]) -> None:
        command = shlex.split(self.config.hook_command)
        if not command:
            return
        self.verbose_log(f"Running hook command: {self.config.hook_command}")
        subprocess.run(command + changed_roots, check=True)

    def lookup(self, rel_path: str) -> dict[str, Any] | None:
        normalized = normalize_posix_path(rel_path)
        with self.lock:
            if normalized in self.snapshot.get("files", {}):
                return self.snapshot["files"][normalized]
            if normalized in set(self.snapshot.get("dirs", [])):
                return {"type": "dir"}
        return None

    def list_children(self, rel_path: str) -> list[str]:
        prefix = normalize_posix_path(rel_path)
        if prefix:
            prefix += "/"
        children: set[str] = set()
        with self.lock:
            for child in self.snapshot.get("dirs", []):
                if not child.startswith(prefix) or child == prefix.rstrip("/"):
                    continue
                suffix = child[len(prefix) :]
                if suffix and "/" not in suffix:
                    children.add(suffix)
            for child in self.snapshot.get("files", {}):
                if not child.startswith(prefix):
                    continue
                suffix = child[len(prefix) :]
                if suffix and "/" not in suffix:
                    children.add(suffix)
        return sorted(children)

    def status(self) -> dict[str, Any]:
        with self.lock:
            payload = {
                "last_sync_at": self.last_sync_at,
                "last_error": self.last_error,
                "last_report": self.last_report,
                "sync_in_progress": self.sync_in_progress,
                "startup_sync_complete": self.startup_sync_complete,
                "snapshot_loaded": self.snapshot_loaded,
                "ready": self.is_ready(),
            }
        with self.hook_lock:
            payload.update(
                {
                    "hook_in_progress": self.hook_in_progress,
                    "hook_pending": bool(self.hook_pending_paths),
                    "hook_last_started_at": self.hook_last_started_at,
                    "hook_last_finished_at": self.hook_last_finished_at,
                    "hook_last_error": self.hook_last_error,
                }
            )
        return payload

    def add_magnet(self, magnet: str) -> dict[str, Any]:
        res = self.client.torrents.add_magnet(magnet).json()
        torrent_id = res.get("id")
        if not torrent_id:
            raise ValueError(f"Failed to add magnet: {res}")

        # Get info to retrieve file list
        info = self.client.torrents.info(torrent_id).json()
        filename = info.get("filename")

        # Check if already exists in cache
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

        return {
            "id": torrent_id,
            "filename": filename,
            "files": info.get("files", []),
            "already_exists": already_exists,
        }

    def delete_torrent(self, torrent_id: str) -> dict[str, Any]:
        res = self.client.torrents.delete(torrent_id)
        if res.status_code not in (200, 204):
            raise ValueError(f"Failed to delete torrent: {res.text}")

        # Remove from cache immediately to reflect in UI
        with self.lock:
            if torrent_id in self.cache:
                del self.cache[torrent_id]
                self._write_json(self.cache_path, self.cache)

        return {"status": "success"}

    def select_files(self, torrent_id: str, file_ids: list[str]) -> dict[str, Any]:
        files_str = ",".join(map(str, file_ids))
        res = self.client.torrents.select_files(torrent_id, files_str)
        # RD API returns 204 No Content on success for select_files
        if res.status_code not in (200, 204):
            raise ValueError(f"Failed to select files: {res.text}")
        return {"status": "success"}

    def resolve_download_url(
        self, source_url: str, *, force_refresh: bool = False
    ) -> str:
        if not source_url:
            raise ValueError("missing source URL")
        with self.lock:
            cached = self.resolved_urls.get(source_url)
            if cached and not force_refresh:
                download_url = cached.get("download_url", "").strip()
                if download_url:
                    return download_url
        if self.client is None:
            raise ValueError("Real-Debrid client unavailable")
        download_url = self.client.unrestrict.link(source_url).json().get("download")
        if not download_url:
            raise ValueError("Missing download URL from /unrestrict/link")
        with self.lock:
            self.resolved_urls[source_url] = {
                "download_url": download_url,
                "resolved_at": utc_now_iso(),
            }
        if force_refresh:
            self.verbose_log(
                json.dumps(
                    {"event": "rd_link_refreshed", "source_url": source_url},
                    sort_keys=True,
                )
            )
        return download_url

    def invalidate_download_url(self, source_url: str) -> None:
        with self.lock:
            self.resolved_urls.pop(source_url, None)

    def verbose_log(self, message: str) -> None:
        if self.config.verbose:
            print(message, flush=True)

    def torrents(self) -> list[dict[str, Any]]:
        with self.lock:
            items = []
            for torrent_id, cached in self.cache.items():
                info = cached.get("info") if isinstance(cached, dict) else None
                if not isinstance(info, dict):
                    continue
                selected_files = [
                    item for item in info.get("files", []) if item.get("selected")
                ]
                items.append(
                    {
                        "id": str(info.get("id") or torrent_id),
                        "name": str(
                            info.get("original_filename")
                            or info.get("filename")
                            or torrent_id
                        ),
                        "status": str(info.get("status") or "unknown"),
                        "progress": int(info.get("progress") or 0),
                        "bytes": int(info.get("bytes") or 0),
                        "selected_files": len(selected_files),
                        "links": len(info.get("links") or []),
                        "ended": str(info.get("ended") or ""),
                    }
                )
        return sorted(
            items,
            key=lambda item: (item["status"] != "downloaded", item["name"].lower()),
        )

    def mark_startup_sync_complete(self) -> None:
        with self.lock:
            self.startup_sync_complete = True

    def is_ready(self) -> bool:
        return self.snapshot_loaded or (
            self.startup_sync_complete and self.last_sync_at is not None
        )


def read_range_header(value: str | None, size: int) -> tuple[int, int] | None:
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


class DavHandler(BaseHTTPRequestHandler):
    state: BuzzState | None = None

    def do_OPTIONS(self) -> None:
        if self.path.startswith("/dav"):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("DAV", "1")
            self.send_header("Allow", "OPTIONS, GET, HEAD, PROPFIND")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        if self.path in {"/", "/torrents"}:
            self._respond_html(HTTPStatus.OK, self._torrents_page())
            return
        if self.path == "/healthz":
            self._respond_json(HTTPStatus.OK, {"status": "ok", **self.state.status()})
            return
        if self.path == "/readyz":
            status = (
                HTTPStatus.OK
                if self.state.is_ready()
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            payload_status = "ready" if status == HTTPStatus.OK else "starting"
            self._respond_json(
                status, {"status": payload_status, **self.state.status()}
            )
            return
        if self.path == "/sync":
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if self.path.startswith("/dav"):
            self._serve_dav(send_body=True)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        if self.path.startswith("/dav"):
            self._serve_dav(send_body=False)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/sync":
            try:
                report = self.state.sync()
                self._respond_json(HTTPStatus.OK, report)
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = str(exc)
                self._respond_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
                )
            return

        if self.path == "/api/torrents/add":
            try:
                data = self._read_json_body()
                magnet = data.get("magnet")
                if not magnet:
                    self._respond_json(
                        HTTPStatus.BAD_REQUEST, {"error": "Missing magnet link"}
                    )
                    return
                result = self.state.add_magnet(magnet)
                self._respond_json(HTTPStatus.OK, result)
            except Exception as exc:  # noqa: BLE001
                self._respond_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
                )
            return

        if self.path == "/api/torrents/select":
            try:
                data = self._read_json_body()
                torrent_id = data.get("torrent_id")
                file_ids = data.get("file_ids")
                if not torrent_id or file_ids is None:
                    self._respond_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "Missing torrent_id or file_ids"},
                    )
                    return
                result = self.state.select_files(torrent_id, file_ids)
                self._respond_json(HTTPStatus.OK, result)
            except Exception as exc:  # noqa: BLE001
                self._respond_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
                )
            return

        if self.path == "/api/torrents/delete":
            try:
                data = self._read_json_body()
                torrent_id = data.get("torrent_id")
                if not torrent_id:
                    self._respond_json(
                        HTTPStatus.BAD_REQUEST, {"error": "Missing torrent_id"}
                    )
                    return
                result = self.state.delete_torrent(torrent_id)
                self._respond_json(HTTPStatus.OK, result)
            except Exception as exc:  # noqa: BLE001
                self._respond_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
                )
            return

        if self.path == "/api/curator/rebuild":
            try:
                self.state.manual_rebuild()
                self._respond_json(HTTPStatus.OK, {"status": "success"})
            except Exception as exc:  # noqa: BLE001
                self._respond_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
                )
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def do_PROPFIND(self) -> None:
        if not self.path.startswith("/dav"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        rel = dav_rel_path(self.path)
        node = self.state.lookup(rel)
        if node is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        depth = self.headers.get("Depth", "0").strip()
        paths = [rel]
        if depth == "1":
            for child in self.state.list_children(rel):
                child_path = "/".join(part for part in (rel, child) if part)
                paths.append(child_path)
        body = self._propfind_body(paths)
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.MULTI_STATUS)
        self.send_header("Content-Type", 'application/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self._write_client(encoded)

    def _serve_dav(self, send_body: bool) -> None:
        rel = dav_rel_path(self.path)
        node = self.state.lookup(rel)
        if node is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if node["type"] == "dir":
            body = b""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            if send_body:
                self._write_client(body)
            return
        if node["type"] == "memory":
            content = node["content"].encode("utf-8")
            range_header = read_range_header(self.headers.get("Range"), len(content))
            if range_header:
                start, end = range_header
                payload = content[start : end + 1]
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
            else:
                payload = content
                self.send_response(HTTPStatus.OK)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", node["mime_type"])
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("ETag", node["etag"])
            self.send_header("Last-Modified", http_date(node.get("modified")))
            self.end_headers()
            if send_body:
                self._write_client(payload)
            return

        size = int(node["size"])
        range_header = read_range_header(self.headers.get("Range"), size)
        if not send_body:
            self.send_response(
                HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK
            )
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", node["mime_type"])
            self.send_header("ETag", node["etag"])
            self.send_header("Last-Modified", http_date(node.get("modified")))
            if range_header:
                start, end = range_header
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
            else:
                self.send_header("Content-Length", str(size))
            self.end_headers()
            return
        try:
            response, first_chunk = self._open_remote_media(node, range_header)
            with response:
                status = HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK
                self.send_response(status)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", node["mime_type"])
                self.send_header("ETag", node["etag"])
                self.send_header("Last-Modified", http_date(node.get("modified")))
                if range_header:
                    start, end = range_header
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Content-Length", str(end - start + 1))
                else:
                    self.send_header("Content-Length", str(size))
                self.end_headers()
                if send_body:
                    if first_chunk:
                        if not self._write_client(first_chunk):
                            return
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        if not self._write_client(chunk):
                            return
        except error.HTTPError as exc:
            self.send_error(exc.code, str(exc))
        except ValueError as exc:
            print(
                json.dumps(
                    {"event": "rd_stream_failed", "path": rel, "error": str(exc)},
                    sort_keys=True,
                ),
                flush=True,
            )
            self.send_error(HTTPStatus.BAD_GATEWAY, str(exc))

    def _propfind_body(self, paths: list[str]) -> str:
        responses = []
        for rel in paths:
            node = self.state.lookup(rel)
            if node is None:
                continue
            href_path = "/dav"
            if rel:
                href_path += "/" + parse.quote(rel)
            if node["type"] == "dir":
                prop = (
                    "<D:resourcetype><D:collection/></D:resourcetype>"
                    "<D:getcontentlength>0</D:getcontentlength>"
                )
            else:
                size = str(int(node.get("size", 0)))
                prop = (
                    "<D:resourcetype/>"
                    f"<D:getcontentlength>{size}</D:getcontentlength>"
                    f"<D:getcontenttype>{escape(node.get('mime_type', 'application/octet-stream'))}</D:getcontenttype>"
                    f"<D:getetag>{escape(node.get('etag', ''))}</D:getetag>"
                    f"<D:getlastmodified>{escape(http_date(node.get('modified')))}</D:getlastmodified>"
                )
            responses.append(
                "<D:response>"
                f"<D:href>{escape(href_path)}</D:href>"
                "<D:propstat>"
                f"<D:prop>{prop}</D:prop>"
                "<D:status>HTTP/1.1 200 OK</D:status>"
                "</D:propstat>"
                "</D:response>"
            )
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>"
        )

    def _respond_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_client(body)

    def _respond_html(self, status: HTTPStatus, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self._write_client(encoded)

    def _write_client(self, payload: bytes) -> bool:
        try:
            self.wfile.write(payload)
            return True
        except (BrokenPipeError, ConnectionResetError) as exc:
            self.state.verbose_log(
                json.dumps(
                    {
                        "event": "client_disconnected",
                        "path": self.path,
                        "error": str(exc),
                    },
                    sort_keys=True,
                )
            )
            return False

    def _open_remote_media(
        self,
        node: dict[str, Any],
        range_header: tuple[int, int] | None,
    ) -> tuple[Any, bytes]:
        source_url = str(node.get("source_url") or node.get("url") or "").strip()
        if not source_url:
            raise ValueError("missing Real-Debrid source URL")
        last_error = "unable to resolve upstream media"
        for attempt in range(2):
            download_url = self.state.resolve_download_url(
                source_url, force_refresh=attempt == 1
            )
            req = request.Request(download_url, method="GET")
            if range_header:
                start, end = range_header
                req.add_header("Range", f"bytes={start}-{end}")
            try:
                response = request.urlopen(req, timeout=60)
            except error.HTTPError as exc:
                self.state.invalidate_download_url(source_url)
                last_error = f"upstream returned HTTP {exc.code}"
                if attempt == 0:
                    continue
                raise ValueError(last_error) from exc
            try:
                first_chunk = self._validate_remote_media_response(
                    response, node, range_header
                )
                return response, first_chunk
            except ValueError as exc:
                response.close()
                self.state.invalidate_download_url(source_url)
                last_error = str(exc)
                if attempt == 0:
                    continue
                raise
        raise ValueError(last_error)

    def _validate_remote_media_response(
        self,
        response: Any,
        node: dict[str, Any],
        range_header: tuple[int, int] | None,
    ) -> bytes:
        content_type = response.headers.get("Content-Type")
        if not is_probably_media_content_type(content_type):
            raise ValueError(
                f"upstream returned non-media content type {content_type!r}"
            )
        should_peek = range_header is None or range_header[0] == 0
        if not should_peek:
            return b""
        first_chunk = response.read(512)
        if first_chunk and looks_like_markup(first_chunk):
            raise ValueError("upstream returned markup instead of media bytes")
        return first_chunk

    def _torrents_page(self) -> str:
        status = self.state.status()
        torrents = self.state.torrents()
        rows = []
        for torrent in torrents:
            torrent_id = torrent["id"]
            rows.append(
                "<tr>"
                f"<td class='name'>{html_escape(torrent['name'])}</td>"
                f'<td><span class="status status-{html_escape(torrent["status"])}">[{html_escape(torrent["status"])}]</span></td>'
                f"<td data-value='{torrent['progress']}'>{html_escape(torrent['progress'])}%</td>"
                f"<td data-value='{torrent['bytes']}'>{html_escape(format_bytes(torrent['bytes']))}</td>"
                f"<td>{html_escape(torrent['selected_files'])}</td>"
                f"<td>{html_escape(torrent['links'])}</td>"
                f"<td class='comment'>{html_escape(torrent['ended'] or '-')}</td>"
                f"<td class='yellow'><code>{html_escape(torrent_id[:8])}</code></td>"
                "<td>"
                f'<div class="delete-container">'
                f'<div class="confirm-opts" id="confirm-{torrent_id}">'
                f'<div class="opt opt-y" onclick="deleteTorrent(\'{torrent_id}\')">[Y]</div>'
                f'<div class="opt opt-n" onclick="toggleDelete(\'{torrent_id}\', false)">[N]</div>'
                "</div>"
                f'<div class="btn-x" id="btn-x-{torrent_id}" onclick="toggleDelete(\'{torrent_id}\', true)">[X]</div>'
                "</div>"
                "</td>"
                "</tr>"
            )
        if not rows:
            rows.append(
                '<tr><td colspan="8" class="empty">No cached torrents yet. '
                "Wait for the first sync or trigger <code>POST /sync</code>.</td></tr>"
            )

        sync_state = "syncing" if status.get("sync_in_progress") else "idle"
        error_html = ""
        if status.get("last_error"):
            error_html = (
                '<div class="error"><span class="label-red">[ERROR]</span> '
                f"{html_escape(status['last_error'])}</div>"
            )

        template_path = os.path.join(
            os.path.dirname(__file__), "templates", "torrents.html"
        )
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template = f.read()
        except Exception:
            return "Error loading template"

        return (
            template.replace("{torrents_count}", str(len(torrents)))
            .replace(
                "{last_sync_at}", html_escape(status.get("last_sync_at") or "never")
            )
            .replace("{sync_state}", html_escape(sync_state))
            .replace(
                "{snapshot_ready}",
                html_escape("true" if status.get("snapshot_loaded") else "false"),
            )
            .replace("{error_html}", error_html)
            .replace("{rows}", "".join(rows))
        )

    def log_message(self, format: str, *args: Any) -> None:
        if not self.state.config.verbose:
            return
        message = "%s - - [%s] %s\n" % (
            self.address_string(),
            self.log_date_time_string(),
            format % args,
        )
        print(message, end="", flush=True)


class Poller(threading.Thread):
    def __init__(self, state: BuzzState):
        super().__init__(daemon=True)
        self.state = state
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.state.config.poll_interval_secs):
            try:
                report = self.state.sync()
                if report.get("changed"):
                    print(
                        json.dumps(
                            {
                                "event": "realdebrid_update",
                                "timestamp": report.get("timestamp"),
                                "synced_torrents": report.get("synced_torrents"),
                                "added_paths": report.get("added_paths", []),
                                "removed_paths": report.get("removed_paths", []),
                                "updated_paths": report.get("updated_paths", []),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = str(exc)
                print(f"background sync failed: {exc}", flush=True)

    def stop(self) -> None:
        self._stop_event.set()


class InitialSync(threading.Thread):
    def __init__(self, state: BuzzState):
        super().__init__(daemon=True)
        self.state = state

    def run(self) -> None:
        try:
            report = self.state.sync(trigger_hook=False)
            print(json.dumps({"startup_sync": report}, sort_keys=True), flush=True)
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = str(exc)
            print(f"startup sync failed: {exc}", flush=True)
        finally:
            self.state.mark_startup_sync_complete()


def run_dav_server(config: DavConfig) -> None:
    os.environ["RD_APITOKEN"] = config.token
    client = RD()
    state = BuzzState(config, client)
    DavHandler.state = state
    server = ThreadingHTTPServer((config.bind, config.port), DavHandler)
    initial_sync = InitialSync(state)
    poller = Poller(state)
    initial_sync.start()
    poller.start()
    print(f"buzz dav listening on {config.bind}:{config.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        poller.stop()
        server.server_close()
