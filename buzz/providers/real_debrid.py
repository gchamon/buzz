"""Real-Debrid provider client."""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Sequence
from typing import Any, cast

import httpx
import requests
from rdapi import RD

from buzz.core.events import record_event
from buzz.core.providers import (
    DEFAULT_RESOLUTION_REFRESH_SECS,
    FileSelection,
    FileSelectionResult,
    MagnetResolution,
    MagnetSubmission,
    ProviderDeleteError,
    ProviderErrorCode,
    ProviderFile,
    ProviderKind,
    ProviderOperation,
    ProviderOperationError,
    ProviderStreamError,
    ProviderTorrentDetail,
    ProviderTorrentSummary,
    _as_float,
    _as_int,
    _status,
)

_RETRYABLE_CODES = frozenset({
    ProviderErrorCode.RATE_LIMITED,
    ProviderErrorCode.UPSTREAM_TIMEOUT,
    ProviderErrorCode.UPSTREAM_UNAVAILABLE,
})


class RealDebridProviderClient:
    """Real-Debrid adapter around ``rd-api-py``."""

    kind: ProviderKind = "real_debrid"

    def __init__(self, token: str, raw_client: Any | None = None) -> None:
        self.token = token
        if raw_client is None:
            os.environ["RD_APITOKEN"] = token
            raw_client = RD()
        self.raw_client = raw_client

    def list_torrents(self) -> list[ProviderTorrentSummary]:
        page_size = 100
        offset: int | None = None
        results = []
        while True:
            print('HTTP Request: GET https://api.real-debrid.com/rest/1.0/torrents "HTTP/1.1 200 OK"', flush=True)
            response = self.raw_client.torrents.get(offset=offset, limit=page_size)
            if response.status_code == 204:
                break
            data = response.json()
            if not isinstance(data, list):
                error = data.get("error") if isinstance(data, dict) else response.text
                raise RuntimeError(
                    f"Real-Debrid API error (HTTP {response.status_code}): {error}"
                )
            results.extend(self._summary(item) for item in data if isinstance(item, dict))
            if len(data) < page_size:
                break
            offset = (offset or 0) + page_size
        return results

    def get_torrent(self, torrent_id: str) -> ProviderTorrentDetail:
        """Get detailed information for a specific torrent, retrying on transient failures."""
        max_attempts = 3
        last_error: str = ""
        for attempt in range(max_attempts):
            print(
                f'HTTP Request: GET https://api.real-debrid.com/rest/1.0/torrents/info/{torrent_id} "HTTP/1.1 200 OK"',
                flush=True,
            )
            response = self.raw_client.torrents.info(torrent_id)
            try:
                data = response.json()
            except Exception:
                data = None
            if self._is_valid_torrent_detail(data):
                return self._detail(cast(dict[str, Any], data))
            last_error = self._rd_error_detail(response, data)
            if attempt < max_attempts - 1:
                wait = (2 ** attempt) + (random.random() * 0.5)
                record_event(
                    f"retrying Real-Debrid detail fetch (attempt {attempt + 1}/{max_attempts}): "
                    f"{torrent_id}: {last_error}",
                    level="info",
                    event="rd_detail_retry",
                    provider="real_debrid",
                    torrent_id=torrent_id,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"Real-Debrid transient error for {torrent_id} after {max_attempts} attempts: {last_error}"
        )

    def fetch_details(
        self,
        torrent_ids: list[str],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, ProviderTorrentDetail]:
        """Fetch details for each id with per-call progress reporting."""
        total = len(torrent_ids)
        results: dict[str, ProviderTorrentDetail] = {}
        for i, torrent_id in enumerate(torrent_ids, 1):
            if on_progress is not None:
                on_progress(torrent_id, i, total)
            results[torrent_id] = self.get_torrent(torrent_id)
        return results

    def submit_magnet(self, magnet: str) -> MagnetSubmission:
        """Submit a magnet and return the accepted torrent reference."""
        print(
            'HTTP Request: POST https://api.real-debrid.com/rest/1.0/torrents/addMagnet "HTTP/1.1 200 OK"',
            flush=True,
        )
        try:
            # rd.post keeps the magnet URI intact; Torrents.add_magnet would
            # prefix ``magnet:?xt=urn:btih:`` a second time.
            response = self.raw_client.post("/torrents/addMagnet", magnet=magnet)
        except Exception as exc:
            raise self._transport_error("submit_magnet", exc) from exc
        data = self._safe_json(response)
        torrent_id = (
            str(data.get("id") or "").strip() if isinstance(data, dict) else ""
        )
        if not torrent_id:
            raise self._response_error("submit_magnet", response, data)
        return MagnetSubmission(torrent_id=torrent_id)

    def resolve_magnet(self, torrent_id: str) -> MagnetResolution:
        """Report metadata readiness for an accepted torrent."""
        try:
            detail = self.get_torrent(torrent_id)
        except RuntimeError as exc:
            raise ProviderOperationError(
                "real_debrid",
                "resolve_magnet",
                ProviderErrorCode.UPSTREAM_UNAVAILABLE,
                detail=str(exc),
                retryable=True,
            ) from exc
        if detail.status == "error":
            raise ProviderOperationError(
                "real_debrid",
                "resolve_magnet",
                ProviderErrorCode.TORRENT_NOT_FOUND,
                detail="provider reports the torrent as errored",
            )
        if detail.files:
            return MagnetResolution(
                status="files_ready",
                name=detail.name,
                bytes=detail.bytes,
                files=detail.files,
            )
        return MagnetResolution(
            status="metadata_pending",
            name=detail.name or None,
            next_refresh_secs=DEFAULT_RESOLUTION_REFRESH_SECS,
        )

    def apply_file_selections(
        self, selections: Sequence[FileSelection]
    ) -> list[FileSelectionResult]:
        """Apply selections sequentially; RD has no native batch endpoint."""
        results: list[FileSelectionResult] = []
        for selection in selections:
            try:
                self._select_files_one(selection.torrent_id, list(selection.file_ids))
                results.append(
                    FileSelectionResult(selection.torrent_id, ok=True)
                )
            except ProviderOperationError as exc:
                results.append(
                    FileSelectionResult(
                        selection.torrent_id, ok=False, error=exc
                    )
                )
        return results

    def _select_files_one(self, torrent_id: str, file_ids: list[str]) -> None:
        print(
            "HTTP Request: POST https://api.real-debrid.com/rest/1.0/torrents"
            f'/selectFiles/{torrent_id} "HTTP/1.1 200 OK"',
            flush=True,
        )
        try:
            response = self.raw_client.torrents.select_files(
                torrent_id, ",".join(str(item) for item in file_ids)
            )
        except Exception as exc:
            raise self._transport_error("apply_file_selections", exc) from exc
        # RD's selectFiles is idempotent: a repeated call (incl. RD's implicit
        # selection at add-time) returns HTTP 403 with an error body. Treat
        # ``action_already_done`` (error_code 31) as a successful no-op so the
        # caller can refresh from RD's authoritative selection; surface any other
        # error body as a real failure.
        data = self._safe_json(response)
        if isinstance(data, dict) and (data.get("error") or data.get("error_code")):
            if (
                data.get("error") == "action_already_done"
                or data.get("error_code") == 31
            ):
                return
            raise ProviderOperationError(
                "real_debrid",
                "apply_file_selections",
                ProviderErrorCode.FILE_SELECTION_REJECTED,
                detail=self._rd_error_detail(response, data),
            )
        status = getattr(response, "status_code", None)
        if status not in (200, 204):
            raise self._response_error(
                "apply_file_selections", response, data, detail=getattr(response, "text", "") or ""
            )

    def add_magnet(self, magnet: str) -> str:
        """Legacy wrapper around :meth:`submit_magnet`."""
        return self.submit_magnet(magnet).torrent_id

    def select_files(self, torrent_id: str, file_ids: list[str]) -> None:
        """Legacy wrapper around :meth:`apply_file_selections`."""
        error = self.apply_file_selections(
            [FileSelection(torrent_id, tuple(str(item) for item in file_ids))]
        )[0].error
        if error is not None:
            raise error

    def delete_torrent(self, torrent_id: str) -> None:
        print(
            f'HTTP Request: DELETE https://api.real-debrid.com/rest/1.0/torrents/delete/{torrent_id} "HTTP/1.1 200 OK"',
            flush=True,
        )
        response = self.raw_client.torrents.delete(torrent_id)
        if response.status_code not in (200, 204):
            raise ProviderDeleteError(response.status_code, response.text)

    def is_healthy(self) -> bool:
        """Check if Real-Debrid API is healthy."""
        for _ in range(3):
            try:
                # Use a short timeout to detect degradation quickly
                with httpx.Client(timeout=3.0) as client:
                    resp = client.get(
                        "https://api.real-debrid.com/rest/1.0/user",
                        headers={"Authorization": f"Bearer {self.token}", "User-Agent": "buzz"},
                    )
                    if resp.status_code == 200:
                        return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def resolve_stream(self, stream_ref: str) -> str:
        print('HTTP Request: POST https://api.real-debrid.com/rest/1.0/unrestrict/link "HTTP/1.1 200 OK"', flush=True)
        data = self.raw_client.unrestrict.link(stream_ref).json()
        download_url = str(data.get("download") or "").strip()
        if download_url:
            return download_url
        raise ProviderStreamError(stream_ref, str(data.get("error") or "no download link"))

    @staticmethod
    def _is_valid_torrent_detail(data: Any) -> bool:
        """Return True when data looks like a real torrent detail (not an error response)."""
        if not isinstance(data, dict):
            return False
        if data.get("error") or data.get("error_code"):
            return False
        return bool(data.get("id") or data.get("hash"))

    @staticmethod
    def _rd_error_detail(response: Any, data: Any) -> str:
        """Extract a human-readable error string from an RD response."""
        if isinstance(data, dict):
            error = data.get("error") or data.get("error_code")
            if error:
                return str(error)
        try:
            status = response.status_code
            return f"HTTP {status}"
        except Exception:
            return "unknown error"

    @staticmethod
    def _safe_json(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            return None

    def _transport_error(
        self, operation: ProviderOperation, exc: BaseException
    ) -> ProviderOperationError:
        """Map a ``requests`` transport failure to a typed provider error."""
        if isinstance(exc, requests.exceptions.Timeout):
            return ProviderOperationError(
                "real_debrid",
                operation,
                ProviderErrorCode.UPSTREAM_TIMEOUT,
                detail=str(exc),
                retryable=True,
            )
        if isinstance(exc, requests.exceptions.RequestException):
            return ProviderOperationError(
                "real_debrid",
                operation,
                ProviderErrorCode.UPSTREAM_UNAVAILABLE,
                detail=str(exc),
                retryable=True,
            )
        return ProviderOperationError(
            "real_debrid",
            operation,
            ProviderErrorCode.UNKNOWN,
            detail=str(exc),
        )

    def _response_error(
        self,
        operation: ProviderOperation,
        response: Any,
        data: Any,
        detail: str = "",
    ) -> ProviderOperationError:
        """Map an RD error response to a typed provider error."""
        status = getattr(response, "status_code", None)
        error_text = str(data.get("error") or "") if isinstance(data, dict) else ""
        lowered = error_text.lower()
        if "infringing_file" in lowered or "magnet" in lowered:
            code = ProviderErrorCode.MAGNET_REJECTED
        elif status == 401:
            code = ProviderErrorCode.AUTHENTICATION_FAILED
        elif status == 404:
            code = ProviderErrorCode.TORRENT_NOT_FOUND
        elif status == 403:
            code = ProviderErrorCode.PERMISSION_DENIED
        elif status == 429:
            code = ProviderErrorCode.RATE_LIMITED
        elif isinstance(status, int) and status >= 500:
            code = ProviderErrorCode.UPSTREAM_UNAVAILABLE
        else:
            code = ProviderErrorCode.UPSTREAM_PROTOCOL_ERROR
        if not detail and not error_text:
            detail = f"HTTP {status}" if status is not None else "provider error"
        elif not detail:
            detail = error_text
        return ProviderOperationError(
            "real_debrid",
            operation,
            code,
            detail=detail,
            retryable=code in _RETRYABLE_CODES,
            diagnostic=error_text or None,
        )

    def _summary(self, item: dict[str, Any]) -> ProviderTorrentSummary:
        links = tuple(str(link) for link in item.get("links") or [])
        return ProviderTorrentSummary(
            id=str(item.get("id") or ""),
            name=str(item.get("filename") or item.get("id") or "torrent"),
            bytes=_as_int(item.get("bytes")),
            progress=_as_float(item.get("progress")),
            status=_status(item.get("status")),
            ended=item.get("ended"),
            stream_refs=links,
        )

    def _detail(self, item: dict[str, Any]) -> ProviderTorrentDetail:
        links = [str(link) for link in item.get("links") or []]
        link_iter = iter(links)
        files = tuple(
            ProviderFile(
                id=str(file_item.get("id") or ""),
                path=str(file_item.get("path") or ""),
                bytes=_as_int(file_item.get("bytes")),
                selected=bool(file_item.get("selected")),
                stream_ref=next(link_iter, "") if file_item.get("selected") else "",
            )
            for file_item in item.get("files") or []
            if isinstance(file_item, dict)
        )
        name = str(item.get("filename") or item.get("original_filename") or item.get("id") or "torrent")
        return ProviderTorrentDetail(
            id=str(item.get("id") or ""),
            hash=str(item.get("hash") or "").lower(),
            name=name,
            original_name=str(item.get("original_filename") or name),
            bytes=_as_int(item.get("bytes")),
            progress=_as_float(item.get("progress")),
            status=_status(item.get("status")),
            added=item.get("added"),
            ended=item.get("ended"),
            files=files,
            stream_refs=tuple(links),
        )
