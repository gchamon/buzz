"""qBittorrent Web API implementation of the SeedClient contract."""

from __future__ import annotations

from typing import Any

import httpx

from . import SeedClientError, SeedFile, SeedStatus

# qBittorrent state -> compact UI label. Grouped by operator meaning rather
# than client internals: anything actively serving pieces is "seeding".
_STATE_LABELS = {
    "uploading": "seeding",
    "stalledUP": "seeding",
    "forcedUP": "seeding",
    "queuedUP": "seeding",
    "checkingUP": "checking",
    "checkingDL": "checking",
    "checkingResumeData": "checking",
    "allocating": "checking",
    "moving": "checking",
    "pausedUP": "paused",
    "pausedDL": "paused",
    "stoppedUP": "paused",
    "stoppedDL": "paused",
    "error": "error",
    "missingFiles": "error",
    "downloading": "downloading",
    "stalledDL": "downloading",
    "metaDL": "downloading",
    "forcedMetaDL": "downloading",
    "queuedDL": "downloading",
    "forcedDL": "downloading",
}


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, "unknown")


class QBittorrentSeedClient:
    """SeedClient backed by the qBittorrent Web API (v2)."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        timeout_secs: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Initialize the client; authentication happens lazily."""
        self.base_url = url.rstrip("/")
        self.username = username
        self._password = password
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_secs,
            transport=transport,
            follow_redirects=True,
        )
        self._authenticated = False

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # -- session ------------------------------------------------------------

    def _login(self) -> None:
        try:
            response = self._client.post(
                "/api/v2/auth/login",
                data={"username": self.username, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise SeedClientError(
                f"qBittorrent unreachable at {self.base_url}: {exc}"
            ) from exc
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise SeedClientError(
                "qBittorrent login failed: check seeding.qbittorrent "
                "credentials"
            )
        self._authenticated = True

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_status: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        if not self._authenticated:
            self._login()
        response = self._send(method, path, data=data, params=params)
        if response.status_code == 403:
            # Session cookie expired; log in again and retry once.
            self._login()
            response = self._send(method, path, data=data, params=params)
        if response.status_code >= 400 and (
            response.status_code not in allow_status
        ):
            raise SeedClientError(
                f"qBittorrent API error {response.status_code} on {path}: "
                f"{response.text.strip()[:200]}"
            )
        return response

    def _send(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(method, path, data=data, params=params)
        except httpx.HTTPError as exc:
            raise SeedClientError(
                f"qBittorrent request failed on {path}: {exc}"
            ) from exc

    # -- SeedClient ----------------------------------------------------------

    def add_torrent(
        self, magnet: str, save_path: str, category: str = ""
    ) -> None:
        """Add a magnet stopped with an explicit save path and category."""
        data: dict[str, Any] = {
            "urls": magnet,
            "savepath": save_path,
            # Keep the torrent's own layout so staged paths line up.
            "contentLayout": "Original",
            # qBittorrent 4.x uses "paused"; 5.x renamed it to "stopped".
            "paused": "true",
            "stopped": "true",
        }
        if category:
            data["category"] = category
        response = self._request("POST", "/api/v2/torrents/add", data=data)
        if response.text.strip() == "Fails.":
            raise SeedClientError(
                "qBittorrent rejected the torrent add (invalid magnet or "
                "duplicate)"
            )

    def torrent_files(self, torrent_hash: str) -> list[SeedFile]:
        """Return the file list; empty while metadata is unavailable."""
        response = self._request(
            "GET",
            "/api/v2/torrents/files",
            params={"hash": torrent_hash},
            allow_status=frozenset({404}),
        )
        if response.status_code == 404:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        files: list[SeedFile] = []
        for position, item in enumerate(payload):
            if not isinstance(item, dict):
                continue
            files.append(
                SeedFile(
                    index=int(item.get("index", position)),
                    path=str(item.get("name") or ""),
                    priority=int(item.get("priority", 1)),
                )
            )
        return files

    def set_file_priorities(
        self, torrent_hash: str, file_indexes: list[int], priority: int
    ) -> None:
        """Set the priority for the given file indexes."""
        if not file_indexes:
            return
        self._request(
            "POST",
            "/api/v2/torrents/filePrio",
            data={
                "hash": torrent_hash,
                "id": "|".join(str(index) for index in file_indexes),
                "priority": str(priority),
            },
        )

    def recheck(self, torrent_hash: str) -> None:
        """Force a recheck of the torrent's on-disk data."""
        self._request(
            "POST",
            "/api/v2/torrents/recheck",
            data={"hashes": torrent_hash},
        )

    def resume(self, torrent_hash: str) -> None:
        """Start the torrent (``start`` on 5.x, ``resume`` on 4.x)."""
        response = self._request(
            "POST",
            "/api/v2/torrents/start",
            data={"hashes": torrent_hash},
            allow_status=frozenset({404}),
        )
        if response.status_code == 404:
            self._request(
                "POST",
                "/api/v2/torrents/resume",
                data={"hashes": torrent_hash},
            )

    def statuses(self, hashes: list[str]) -> dict[str, SeedStatus]:
        """Return normalized statuses for the given hashes."""
        if not hashes:
            return {}
        response = self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": "|".join(hashes)},
        )
        try:
            payload = response.json()
        except ValueError:
            return {}
        if not isinstance(payload, list):
            return {}
        statuses: dict[str, SeedStatus] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            torrent_hash = str(item.get("hash") or "").strip().lower()
            if not torrent_hash:
                continue
            state = str(item.get("state") or "")
            try:
                progress = float(item.get("progress") or 0.0)
            except (TypeError, ValueError):
                progress = 0.0
            statuses[torrent_hash] = SeedStatus(
                hash=torrent_hash,
                state=state,
                progress=progress,
                label=_state_label(state),
            )
        return statuses

    def delete(self, torrent_hash: str, delete_files: bool = False) -> None:
        """Remove the torrent, optionally deleting its staged data."""
        self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": torrent_hash,
                "deleteFiles": "true" if delete_files else "false",
            },
        )
