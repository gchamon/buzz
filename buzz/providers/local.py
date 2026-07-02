"""Local-disk provider client backed by buzz-managed SQLite records."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from buzz.core import db
from buzz.core.providers import (
    ProviderFile,
    ProviderKind,
    ProviderStreamError,
    ProviderTorrentDetail,
    ProviderTorrentSummary,
    local_stream_ref,
    split_local_stream_ref,
)


class LocalProviderClient:
    """Serve buzz-managed on-disk copies through the provider contract.

    The inventory is the local store (SQLite ``local_torrents``/``local_files``
    rows); ``resolve_stream`` returns a ``file://`` reference into the store
    instead of an upstream URL. The local provider is never an add/restore
    target, so ``add_magnet`` and ``select_files`` report unsupported
    operations.
    """

    kind: ProviderKind = "local"

    def __init__(
        self,
        store_path: str | Path,
        db_path: str | Path | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Initialize with the store directory and a DB path or connection."""
        self.store_path = Path(store_path)
        self._db_path = Path(db_path) if db_path is not None else None
        self._conn = conn
        self._conn_lock = threading.Lock()

    def _connection(self) -> sqlite3.Connection:
        with self._conn_lock:
            if self._conn is None:
                if self._db_path is None:
                    raise RuntimeError("local provider has no database configured")
                self._conn = db.connect(self._db_path)
                db.apply_migrations(self._conn)
            return self._conn

    def list_torrents(self) -> list[ProviderTorrentSummary]:
        """Return one summary per completed local copy."""
        return [
            self._summary(entry)
            for entry in db.load_local_torrents(self._connection())
        ]

    def get_torrent(self, torrent_id: str) -> ProviderTorrentDetail:
        """Return the detail for a local copy; torrent id is the entry hash."""
        entry = db.load_local_torrent(self._connection(), torrent_id)
        if entry is None:
            raise RuntimeError(f"local copy not found: {torrent_id}")
        return self._detail(entry)

    def fetch_details(
        self,
        torrent_ids: list[str],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, ProviderTorrentDetail]:
        """Fetch details for the given hashes from the local store."""
        total = len(torrent_ids)
        results: dict[str, ProviderTorrentDetail] = {}
        for i, torrent_id in enumerate(torrent_ids, 1):
            if on_progress is not None:
                on_progress(torrent_id, i, total)
            results[torrent_id] = self.get_torrent(torrent_id)
        return results

    def add_magnet(self, magnet: str) -> str:
        """Reject magnet adds; the local store is populated by explicit copies."""
        raise RuntimeError("local provider does not support magnet add")

    def select_files(self, torrent_id: str, file_ids: list[str]) -> None:
        """Reject file selection; local copies are immutable snapshots."""
        raise RuntimeError("local provider does not support file selection")

    def delete_torrent(self, torrent_id: str) -> None:
        """Remove a local copy's files from disk and its records."""
        thash = torrent_id.strip().lower()
        if not thash:
            return
        shutil.rmtree(self.store_path / thash, ignore_errors=True)
        db.delete_local_torrent(self._connection(), thash)

    def resolve_stream(self, stream_ref: str) -> str:
        """Resolve a ``local://`` reference to a ``file://`` store path."""
        thash, path = split_local_stream_ref(stream_ref)
        stored_rel = db.get_local_file_stored_path(
            self._connection(), thash, path
        )
        if stored_rel is None:
            raise ProviderStreamError(stream_ref, "file_not_recorded")
        stored = self.store_path / stored_rel
        if not stored.is_file():
            raise ProviderStreamError(stream_ref, "file_missing")
        return f"file://{stored.resolve()}"

    def is_healthy(self) -> bool:
        """Return True when the store directory exists and is writable."""
        try:
            self.store_path.mkdir(parents=True, exist_ok=True)
            probe = self.store_path / ".buzz-health"
            probe.touch()
            probe.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    @staticmethod
    def _summary(entry: dict) -> ProviderTorrentSummary:
        thash = str(entry["hash"])
        return ProviderTorrentSummary(
            id=thash,
            name=str(entry.get("name") or thash),
            bytes=int(entry.get("bytes") or 0),
            progress=100.0,
            status="downloaded",
            ended=entry.get("added_at"),
            stream_refs=tuple(
                local_stream_ref(thash, str(item["path"]))
                for item in entry.get("files", [])
            ),
        )

    @staticmethod
    def _detail(entry: dict) -> ProviderTorrentDetail:
        thash = str(entry["hash"])
        name = str(entry.get("name") or thash)
        files = tuple(
            ProviderFile(
                id=str(index),
                path=str(item["path"]),
                bytes=int(item.get("bytes") or 0),
                selected=True,
                stream_ref=local_stream_ref(thash, str(item["path"])),
            )
            for index, item in enumerate(entry.get("files", []), 1)
        )
        return ProviderTorrentDetail(
            id=thash,
            hash=thash,
            name=name,
            original_name=name,
            bytes=int(entry.get("bytes") or 0),
            progress=100.0,
            status="downloaded",
            added=entry.get("added_at"),
            ended=entry.get("added_at"),
            files=files,
            stream_refs=tuple(file.stream_ref for file in files),
        )
