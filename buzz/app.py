#!/usr/bin/env python3

from __future__ import annotations

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
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any
from urllib import error, parse, request
from xml.sax.saxutils import escape


VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
    ".wmv",
}
DEFAULT_CONFIG_PATH = os.environ.get("BUZZ_CONFIG", "/app/buzz.yml")
SHOW_PATTERNS = (
    re.compile(r"(?i)\bS(?P<season>\d{1,2})E(?P<episode>\d{1,2})\b"),
    re.compile(r"(?i)\b(?P<season>\d{1,2})x(?P<episode>\d{1,2})\b"),
)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_date(value: str | None) -> str:
    if value:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            return formatdate(timestamp, usegmt=True)
        except ValueError:
            pass
    return formatdate(time.time(), usegmt=True)


def normalize_posix_path(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned == "/":
        return ""
    normalized = posixpath.normpath("/" + cleaned.lstrip("/"))
    if normalized == "/":
        return ""
    return normalized.lstrip("/")


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


def is_video_file(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in VIDEO_EXTENSIONS


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonical_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    files = {}
    for path, node in snapshot.get("files", {}).items():
        if not isinstance(node, dict):
            files[path] = node
            continue
        canonical_node = {key: value for key, value in node.items() if key != "modified"}
        files[path] = canonical_node

    report = snapshot.get("report", {})
    canonical_report = report
    if isinstance(report, dict):
        canonical_report = {key: value for key, value in report.items() if key != "generated_at"}

    return {
        "dirs": snapshot.get("dirs", []),
        "files": files,
        "report": canonical_report,
    }


def parse_json_compat_yaml(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = parse_simple_yaml(raw)
    if not isinstance(data, dict):
        raise ValueError("Buzz config must be a mapping.")
    return data


def parse_simple_yaml(raw: str) -> object:
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        lines.append((indent, stripped))
    if not lines:
        return {}
    value, next_index = _parse_yaml_block(lines, 0, lines[0][0])
    if next_index != len(lines):
        raise ValueError("Unexpected trailing YAML content.")
    return value


def _parse_yaml_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[object, int]:
    if index >= len(lines):
        return {}, index
    _, first = lines[index]
    if first.startswith("- "):
        result = []
        while index < len(lines):
            current_indent, stripped = lines[index]
            if current_indent != indent or not stripped.startswith("- "):
                break
            item_text = stripped[2:].strip()
            if not item_text:
                child, index = _parse_yaml_block(lines, index + 1, indent + 2)
                result.append(child)
                continue
            if ":" in item_text:
                key, value_text = item_text.split(":", 1)
                key = key.strip()
                value_text = value_text.strip()
                item = {key: _parse_yaml_scalar(value_text)} if value_text else {key: None}
                index += 1
                if index < len(lines) and lines[index][0] > indent:
                    child, index = _parse_yaml_block(lines, index, indent + 2)
                    if value_text:
                        raise ValueError("Unsupported YAML list structure.")
                    item[key] = child
                result.append(item)
                continue
            result.append(_parse_yaml_scalar(item_text))
            index += 1
        return result, index

    result = {}
    while index < len(lines):
        current_indent, stripped = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError("Invalid YAML indentation.")
        if ":" not in stripped:
            raise ValueError("Expected YAML mapping entry.")
        key, value_text = stripped.split(":", 1)
        key = key.strip()
        value_text = value_text.strip()
        index += 1
        if value_text:
            result[key] = _parse_yaml_scalar(value_text)
            continue
        if index < len(lines) and lines[index][0] > current_indent:
            child, index = _parse_yaml_block(lines, index, current_indent + 2)
            result[key] = child
        else:
            result[key] = {}
    return result, index


def _parse_yaml_scalar(value: str) -> object:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "~"}:
        return None
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


@dataclass(frozen=True)
class Config:
    token: str
    poll_interval_secs: int
    bind: str
    port: int
    state_dir: str
    hook_command: str
    anime_patterns: tuple[str, ...]
    enable_all_dir: bool
    enable_unplayable_dir: bool
    request_timeout_secs: int
    user_agent: str
    version_label: str

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "Config":
        with open(path, "r", encoding="utf-8") as handle:
            payload = parse_json_compat_yaml(handle.read())
        provider = payload.get("provider", {})
        directories = payload.get("directories", {})
        anime = directories.get("anime", {})
        hooks = payload.get("hooks", {})
        compat = payload.get("compat", {})
        server = payload.get("server", {})
        token = provider.get("token", "").strip()
        if not token:
            raise ValueError("provider.token is required.")
        poll_interval = int(payload.get("poll_interval_secs", 10))
        if poll_interval < 1:
            raise ValueError("poll_interval_secs must be positive.")
        return cls(
            token=token,
            poll_interval_secs=poll_interval,
            bind=str(server.get("bind", "0.0.0.0")),
            port=int(server.get("port", 9999)),
            state_dir=str(payload.get("state_dir", "/app/data")),
            hook_command=str(hooks.get("on_library_change", "")).strip(),
            anime_patterns=tuple(anime.get("patterns", [r"\b[a-fA-F0-9]{8}\b"])),
            enable_all_dir=bool(compat.get("enable_all_dir", True)),
            enable_unplayable_dir=bool(compat.get("enable_unplayable_dir", True)),
            request_timeout_secs=int(payload.get("request_timeout_secs", 30)),
            user_agent=str(payload.get("user_agent", "buzz/0.1")),
            version_label=str(payload.get("version_label", "buzz/0.1")),
        )


class RealDebridClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = "https://api.real-debrid.com/rest/1.0"

    def _request_json(self, path: str) -> Any:
        req = request.Request(
            self.base_url + path,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "User-Agent": self.config.user_agent,
                "Accept": "application/json",
            },
        )
        with request.urlopen(req, timeout=self.config.request_timeout_secs) as response:
            return json.load(response)

    def list_torrents(self) -> list[dict[str, Any]]:
        payload = self._request_json("/torrents")
        if not isinstance(payload, list):
            raise ValueError("Unexpected response for /torrents")
        return payload

    def torrent_info(self, torrent_id: str) -> dict[str, Any]:
        payload = self._request_json(f"/torrents/info/{parse.quote(torrent_id)}")
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected response for torrent {torrent_id}")
        return payload


class LibraryBuilder:
    def __init__(self, config: Config):
        self.config = config
        self.anime_regexes = tuple(re.compile(pattern) for pattern in config.anime_patterns)

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
                item for item in playable if item.get("url") and info.get("status") == "downloaded"
            ]
            if linked_playable:
                category = self._category_for(linked_playable)
                self._add_tree(files, dirs, category, torrent_name, linked_playable)
                if self.config.enable_all_dir:
                    self._add_tree(files, dirs, "__all__", torrent_name, linked_playable)
                changed_roots.add(f"{category}/{torrent_name}")
                if category == "movies":
                    report["movies"] += len(linked_playable)
                elif category == "shows":
                    report["show_files"] += len(linked_playable)
                else:
                    report["anime_files"] += len(linked_playable)
            elif self.config.enable_unplayable_dir:
                reason = self._unplayable_reason(info, selected)
                count = self._add_unplayable_tree(files, dirs, torrent_name, selected, reason)
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
        name = str(info.get("original_filename") or info.get("filename") or info.get("id") or "torrent").strip()
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
                "url": entry["url"],
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
        summary_content = json.dumps(
            {
                "reason": reason,
                "status": "unplayable",
                "files": [normalize_posix_path(item["path"]) for item in entries],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
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

    def _unplayable_reason(self, info: dict[str, Any], selected: list[dict[str, Any]]) -> str:
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
    def __init__(self, config: Config, client: RealDebridClient):
        self.config = config
        self.client = client
        self.builder = LibraryBuilder(config)
        self.lock = threading.RLock()
        self.state_dir = config.state_dir
        self.cache_path = os.path.join(self.state_dir, "torrent_cache.json")
        self.snapshot_path = os.path.join(self.state_dir, "library_snapshot.json")
        self.cache = self._load_json(self.cache_path, default={})
        self.snapshot_loaded = os.path.exists(self.snapshot_path)
        self.snapshot = self._load_json(self.snapshot_path, default={"dirs": [""], "files": {}})
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
        self.hook_worker = None
        if self.config.hook_command:
            self.hook_worker = threading.Thread(target=self._hook_worker_loop, daemon=True)
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
            summaries = self.client.list_torrents()
            new_cache: dict[str, dict[str, Any]] = {}
            infos: list[dict[str, Any]] = []
            for summary in summaries:
                torrent_id = str(summary.get("id", "")).strip()
                if not torrent_id:
                    continue
                signature = self._summary_signature(summary)
                with self.lock:
                    cached = self.cache.get(torrent_id)
                if cached and cached.get("signature") == signature and isinstance(cached.get("info"), dict):
                    info = cached["info"]
                else:
                    info = self.client.torrent_info(torrent_id)
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

    def mark_startup_sync_complete(self) -> None:
        with self.lock:
            self.startup_sync_complete = True

    def is_ready(self) -> bool:
        return self.snapshot_loaded or (self.startup_sync_complete and self.last_sync_at is not None)


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


class Handler(BaseHTTPRequestHandler):
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
        if self.path == "/healthz":
            self._respond_json(HTTPStatus.OK, {"status": "ok", **self.state.status()})
            return
        if self.path == "/readyz":
            status = HTTPStatus.OK if self.state.is_ready() else HTTPStatus.SERVICE_UNAVAILABLE
            payload_status = "ready" if status == HTTPStatus.OK else "starting"
            self._respond_json(status, {"status": payload_status, **self.state.status()})
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
        self.wfile.write(encoded)

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
                self.wfile.write(body)
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
                self.wfile.write(payload)
            return

        size = int(node["size"])
        range_header = read_range_header(self.headers.get("Range"), size)
        if not send_body:
            self.send_response(HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK)
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
        req = request.Request(node["url"], method="GET")
        if range_header:
            start, end = range_header
            req.add_header("Range", f"bytes={start}-{end}")
        try:
            with request.urlopen(req, timeout=60) as response:
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
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except error.HTTPError as exc:
            self.send_error(exc.code, str(exc))

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
        return '<?xml version="1.0" encoding="utf-8"?>' "<D:multistatus xmlns:D=\"DAV:\">" + "".join(responses) + "</D:multistatus>"

    def _respond_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        message = "%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args)
        print(message, end="", flush=True)


class Poller(threading.Thread):
    def __init__(self, state: BuzzState):
        super().__init__(daemon=True)
        self.state = state
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.state.config.poll_interval_secs):
            try:
                self.state.sync()
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


def main() -> None:
    config = Config.load()
    client = RealDebridClient(config)
    state = BuzzState(config, client)
    Handler.state = state
    server = ThreadingHTTPServer((config.bind, config.port), Handler)
    initial_sync = InitialSync(state)
    poller = Poller(state)
    initial_sync.start()
    poller.start()
    print(f"buzz listening on {config.bind}:{config.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        poller.stop()
        server.server_close()


if __name__ == "__main__":
    main()
