"""TorBox provider client."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from buzz.core.events import record_event
from buzz.core.providers import (
    ProviderDeleteError,
    ProviderFile,
    ProviderKind,
    ProviderRequestLimiter,
    ProviderRequestPolicy,
    ProviderStreamError,
    ProviderTorrentDetail,
    ProviderTorrentSummary,
    _as_float,
    _as_int,
    _status,
)


class TorBoxProviderClient:
    """TorBox adapter using the public HTTP API."""

    kind: ProviderKind = "torbox"
    base_url = "https://api.torbox.app"
    default_request_policy = ProviderRequestPolicy(
        min_interval_secs=0.5,
        concurrency=2,
        max_attempts=3,
        backoff_initial_secs=0.5,
        backoff_max_secs=5.0,
    )
    stream_request_policy = ProviderRequestPolicy(
        min_interval_secs=1.0,
        concurrency=1,
        max_attempts=3,
        backoff_initial_secs=0.5,
        backoff_max_secs=5.0,
    )

    def __init__(
        self,
        token: str,
        timeout_secs: int = 30,
        request_policy: ProviderRequestPolicy | None = None,
        operation_policies: dict[str, ProviderRequestPolicy] | None = None,
        limiter: ProviderRequestLimiter | None = None,
    ) -> None:
        self.token = token
        self.timeout_secs = timeout_secs
        self._list_cache: list[dict[str, Any]] | None = None
        overrides = {
            "resolve_stream": self.stream_request_policy,
            **(operation_policies or {}),
        }
        self._limiter = limiter or ProviderRequestLimiter(
            request_policy or self.default_request_policy,
            overrides,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        operation = str(kwargs.pop("_operation", "request"))
        if method != "GET":
            self._list_cache = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "buzz",
        }

        def send() -> Any:
            with httpx.Client(timeout=self.timeout_secs) as client:
                response = client.request(
                    method, f"{self.base_url}{path}", headers=headers, **kwargs
                )
                response.raise_for_status()
                return response.json()

        data = self._limiter.run(operation, send)
        if isinstance(data, dict) and data.get("success") is False:
            message = str(data.get("error") or data)
            if data.get("detail"):
                message = f"{message} ({data['detail']})"
            raise RuntimeError(message)
        return data.get("data") if isinstance(data, dict) and "data" in data else data

    def list_torrents(self) -> list[ProviderTorrentSummary]:
        data = self._request(
            "GET", "/v1/api/torrents/mylist", _operation="list_torrents"
        )
        items = data if isinstance(data, list) else []
        self._list_cache = [item for item in items if isinstance(item, dict)]
        return [self._summary(item) for item in self._list_cache]

    def get_torrent(self, torrent_id: str) -> ProviderTorrentDetail:
        if self._list_cache:
            for item in self._list_cache:
                item_id = str(item.get("torrent_id") or item.get("id") or "")
                if item_id == torrent_id:
                    detail = self._detail(item)
                    if detail.files:
                        return detail

        for bypass_cache in (None, True):
            params = {"id": torrent_id}
            if bypass_cache is not None:
                params["bypass_cache"] = str(bypass_cache).lower()
            data = self._request(
                "GET",
                "/v1/api/torrents/mylist",
                _operation="get_torrent",
                params=params,
            )
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("torrent_id") or item.get("id") or "")
                if item_id != torrent_id:
                    continue
                detail = self._detail(item)
                if bypass_cache or detail.files:
                    return detail
        raise RuntimeError(f"TorBox torrent not found: {torrent_id}")

    def fetch_details(
        self,
        torrent_ids: list[str],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, ProviderTorrentDetail]:
        """Fetch details, using _list_cache for cache hits (no progress) and network for misses."""
        results: dict[str, ProviderTorrentDetail] = {}
        misses: list[str] = []
        if self._list_cache:
            cached_by_id = {
                str(item.get("torrent_id") or item.get("id") or ""): item
                for item in self._list_cache
            }
            for torrent_id in torrent_ids:
                item = cached_by_id.get(torrent_id)
                if item is not None:
                    detail = self._detail(item)
                    if detail.files:
                        results[torrent_id] = detail
                        continue
                misses.append(torrent_id)
        else:
            misses = list(torrent_ids)
        total = len(misses)
        for i, torrent_id in enumerate(misses, 1):
            if on_progress is not None:
                on_progress(torrent_id, i, total)
            results[torrent_id] = self.get_torrent(torrent_id)
        return results

    def add_magnet(self, magnet: str) -> str:
        data = self._request(
            "POST",
            "/v1/api/torrents/createtorrent",
            _operation="add_magnet",
            data={"magnet": magnet},
        )
        torrent_id = str(
            (data or {}).get("torrent_id")
            or (data or {}).get("id")
            or ""
        ).strip()
        if not torrent_id:
            raise ValueError(f"Failed to add TorBox magnet: {data}")
        return torrent_id

    def select_files(self, torrent_id: str, file_ids: list[str]) -> None:
        _ = torrent_id, file_ids

    def delete_torrent(self, torrent_id: str) -> None:
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                self._request(
                    "POST",
                    "/v1/api/torrents/controltorrent",
                    _operation="delete_torrent",
                    json={
                        "torrent_id": self._coerce_torrent_id(torrent_id),
                        "operation": "delete",
                    },
                )
                return
            except (RuntimeError, httpx.HTTPStatusError) as exc:
                status_code = None
                error_text = str(exc)
                if isinstance(exc, httpx.HTTPStatusError):
                    status_code = exc.response.status_code
                    error_text = exc.response.text
                    try:
                        data = exc.response.json()
                        if isinstance(data, dict) and data.get("success") is False:
                            error_text = str(data.get("error") or "TorBox error")
                            if data.get("detail"):
                                error_text = f"{error_text} ({data['detail']})"
                    except Exception:
                        pass

                if "DATABASE_ERROR" in error_text and attempt < max_attempts - 1:
                    wait = (2 ** attempt) + (random.random() * 0.5)
                    record_event(
                        f"retrying TorBox delete (attempt {attempt + 1}/{max_attempts}): {error_text}",
                        level="info",
                        provider="torbox",
                        torrent_id=torrent_id,
                    )
                    time.sleep(wait)
                    continue

                if status_code is not None:
                    raise ProviderDeleteError(status_code, error_text, attempts=attempt + 1) from exc
                raise

    def is_healthy(self) -> bool:
        """Check if TorBox API is healthy."""
        for _ in range(3):
            try:
                # Use a short timeout to detect degradation quickly
                def check() -> httpx.Response:
                    with httpx.Client(timeout=3.0) as client:
                        return client.get(
                            f"{self.base_url}/v1/api/user/me",
                            headers={
                                "Authorization": f"Bearer {self.token}",
                                "User-Agent": "buzz",
                            },
                        )

                resp = self._limiter.run("is_healthy", check)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def resolve_stream(self, stream_ref: str) -> str:
        torrent_id, file_id = self._split_stream_ref(stream_ref)
        params = {"token": self.token, "torrent_id": torrent_id}
        if file_id.isdigit():
            params["file_id"] = file_id
        try:
            data = self._request(
                "GET",
                "/v1/api/torrents/requestdl",
                _operation="resolve_stream",
                params=params,
            )
        except httpx.HTTPStatusError as exc:
            raise ProviderStreamError(stream_ref, self._http_stream_error_code(exc)) from exc
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            url = str(data.get("download") or data.get("url") or "").strip()
            if url:
                return url
        raise ProviderStreamError(stream_ref, "no download link")

    @staticmethod
    def _coerce_torrent_id(torrent_id: str) -> int | str:
        try:
            return int(torrent_id)
        except ValueError:
            return torrent_id

    @staticmethod
    def _split_stream_ref(stream_ref: str) -> tuple[str, str]:
        if ":" in stream_ref:
            torrent_id, file_id = stream_ref.split(":", 1)
            return torrent_id, file_id
        return stream_ref, ""

    @staticmethod
    def _http_stream_error_code(exc: httpx.HTTPStatusError) -> str:
        status_code = exc.response.status_code
        text = exc.response.text.strip()
        try:
            data = exc.response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            detail = str(data.get("detail") or data.get("error") or "").strip()
            if detail:
                return f"http_{status_code} {detail}"
        return f"http_{status_code}" + (f" {text}" if text else "")

    def _summary(self, item: dict[str, Any]) -> ProviderTorrentSummary:
        torrent_id = str(item.get("torrent_id") or item.get("id") or "")
        name = str(item.get("name") or item.get("filename") or torrent_id or "torrent")
        progress = _as_float(item.get("progress") or item.get("download_present"))
        if progress <= 1 and progress > 0:
            progress *= 100
        return ProviderTorrentSummary(
            id=torrent_id,
            name=name,
            bytes=_as_int(item.get("size") or item.get("bytes")),
            progress=progress,
            status=_status(item.get("download_state") or item.get("status")),
            ended=item.get("updated_at") or item.get("created_at"),
            stream_refs=(torrent_id,) if torrent_id else (),
        )

    def _detail(self, item: dict[str, Any]) -> ProviderTorrentDetail:
        summary = self._summary(item)
        files = [
            self._file(summary.id, file_item)
            for file_item in item.get("files") or []
            if isinstance(file_item, dict)
        ]
        selected_indexes = [i for i, f in enumerate(files) if f.selected]
        if len(selected_indexes) == 1 and not files[selected_indexes[0]].stream_ref:
            index = selected_indexes[0]
            file_item = files[index]
            files[index] = ProviderFile(
                id=file_item.id,
                path=file_item.path,
                bytes=file_item.bytes,
                selected=file_item.selected,
                stream_ref=summary.id,
            )
        return ProviderTorrentDetail(
            id=summary.id,
            hash=str(item.get("hash") or "").lower(),
            name=summary.name,
            original_name=str(item.get("original_filename") or summary.name),
            bytes=summary.bytes,
            progress=summary.progress,
            status=summary.status,
            added=item.get("created_at"),
            ended=summary.ended,
            files=tuple(files),
            stream_refs=tuple(
                f.stream_ref for f in files if f.selected and f.stream_ref
            ),
        )

    @staticmethod
    def _file(torrent_id: str, item: dict[str, Any]) -> ProviderFile:
        file_id = TorBoxProviderClient._file_id(item)
        path = str(
            item.get("path")
            or item.get("short_name")
            or item.get("name")
            or file_id
        )
        selected = bool(
            item.get("selected", True)
            or item.get("download_present")
            or item.get("active")
        )
        return ProviderFile(
            id=file_id,
            path=path,
            bytes=_as_int(item.get("size") or item.get("bytes")),
            selected=selected,
            stream_ref=f"{torrent_id}:{file_id}" if file_id else "",
        )

    @staticmethod
    def _file_id(item: dict[str, Any]) -> str:
        for key in ("id", "file_id"):
            value = item.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text.isdigit():
                return text
        return ""
