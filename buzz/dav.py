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
        changed_roots: set[str] = set()
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
                changed_roots.add(f"{category}/{torrent_name}")
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
                    changed_roots.add(f"__unplayable__/{torrent_name}")
                    report["unplayable_files"] += count

        snapshot = {
            "generated_at": report["generated_at"],
            "dirs": sorted(dirs),
            "files": files,
            "report": report,
        }
        return snapshot, sorted(changed_roots)

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
        self.hook_condition = threading.Condition()
        self.hook_pending_paths: list[str] = []
        self.hook_in_progress = False
        self.hook_last_started_at = None
        self.hook_last_finished_at = None
        self.hook_last_error = None
        self.resolved_urls: dict[str, dict[str, str]] = {}
        self.hook_worker = None
        if self.config.hook_command:
            self.hook_worker = threading.Thread(
                target=self._hook_worker_loop, daemon=True
            )
            self.hook_worker.start()

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

            snapshot, changed_roots = self.builder.build(infos)
            digest = stable_json(canonical_snapshot(snapshot))

            with self.lock:
                changed = digest != self.snapshot_digest
                report = dict(snapshot["report"])
                report["changed"] = changed
                report["changed_paths"] = changed_roots if changed else []
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
                        hook_paths = changed_roots

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
        with self.hook_condition:
            merged = set(self.hook_pending_paths)
            merged.update(pending)
            self.hook_pending_paths = sorted(merged)
            self.hook_condition.notify()

    def _hook_worker_loop(self) -> None:
        while True:
            with self.hook_condition:
                while not self.hook_pending_paths:
                    self.hook_condition.wait()
                paths = self.hook_pending_paths
                self.hook_pending_paths = []
                self.hook_in_progress = True
                self.hook_last_started_at = utc_now_iso()
                self.hook_last_error = None
            try:
                self._run_hook(paths)
            except Exception as exc:  # noqa: BLE001
                with self.hook_condition:
                    self.hook_last_error = str(exc)
            finally:
                with self.hook_condition:
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

    def _run_hook(self, changed_roots: list[str]) -> None:
        command = shlex.split(self.config.hook_command)
        if not command:
            return
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
        with self.hook_condition:
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
        if self.path != "/sync":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            report = self.state.sync()
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = str(exc)
            self._respond_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._respond_json(HTTPStatus.OK, report)

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
            rows.append(
                "<tr>"
                f"<td>{html_escape(torrent['name'])}</td>"
                f'<td><span class="status status-{html_escape(torrent["status"])}">{html_escape(torrent["status"])}</span></td>'
                f"<td>{html_escape(torrent['progress'])}%</td>"
                f"<td>{html_escape(format_bytes(torrent['bytes']))}</td>"
                f"<td>{html_escape(torrent['selected_files'])}</td>"
                f"<td>{html_escape(torrent['links'])}</td>"
                f"<td>{html_escape(torrent['ended'] or '-')}</td>"
                f"<td><code>{html_escape(torrent['id'])}</code></td>"
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
                '<p class="error"><strong>Last error:</strong> '
                f"{html_escape(status['last_error'])}</p>"
            )

        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Buzz Torrents</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4efe6;
      --panel: #fffaf2;
      --ink: #1d1a17;
      --muted: #6c6257;
      --line: #d8cbb8;
      --accent: #0e6b5c;
      --accent-soft: #dff2ed;
      --danger: #9b2d30;
      --danger-soft: #f9dfdf;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
      background:
        radial-gradient(circle at top left, #fffaf2 0, #f4efe6 45%, #ebe1d3 100%);
      color: var(--ink);
    }}
    main {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1 {{ margin: 0 0 8px; font-size: clamp(2rem, 4vw, 3.4rem); }}
    p {{ margin: 0; color: var(--muted); }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin: 24px 0;
    }}
    .card {{
      padding: 16px 18px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: color-mix(in srgb, var(--panel) 88%, white);
      box-shadow: 0 8px 24px rgba(29, 26, 23, 0.06);
    }}
    .label {{
      display: block;
      margin-bottom: 6px;
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .value {{ font-size: 1.1rem; }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel);
      box-shadow: 0 10px 30px rgba(29, 26, 23, 0.08);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 860px;
    }}
    th, td {{
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 0.95rem;
    }}
    th {{
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      background: rgba(216, 203, 184, 0.18);
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .status {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.82rem;
      font-weight: 700;
      text-transform: lowercase;
    }}
    .status-error {{ background: var(--danger-soft); color: var(--danger); }}
    .empty {{ color: var(--muted); text-align: center; }}
    .error {{
      margin: 0 0 20px;
      padding: 12px 14px;
      border-radius: 12px;
      background: var(--danger-soft);
      color: var(--danger);
      border: 1px solid rgba(155, 45, 48, 0.2);
    }}
    code {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.85em;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Real-Debrid Torrents</h1>
    <p>Server-rendered from Buzz's cached torrent metadata.</p>
    <section class="meta">
      <div class="card"><span class="label">Cached Torrents</span><span class="value">{len(torrents)}</span></div>
      <div class="card"><span class="label">Last Sync</span><span class="value">{html_escape(status.get("last_sync_at") or "never")}</span></div>
      <div class="card"><span class="label">Sync State</span><span class="value">{html_escape(sync_state)}</span></div>
      <div class="card"><span class="label">Snapshot Ready</span><span class="value">{html_escape("yes" if status.get("snapshot_loaded") else "no")}</span></div>
    </section>
    {error_html}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Status</th>
            <th>Progress</th>
            <th>Size</th>
            <th>Selected</th>
            <th>Links</th>
            <th>Ended</th>
            <th>ID</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows)}
        </tbody>
      </table>
    </div>
  </main>
</body>
</html>"""

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
                                "changed_paths": report.get("changed_paths", []),
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
