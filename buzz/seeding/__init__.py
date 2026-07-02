"""Seed-client contracts used by the reseed pipeline.

buzz does not embed a BitTorrent engine. Reseeding stages file bytes onto a
shared filesystem and hands the torrent to an external client behind the
small ``SeedClient`` interface below; qBittorrent is the first
implementation (see ``buzz.seeding.qbittorrent``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SeedFile:
    """A file entry reported by the seed client for one torrent."""

    index: int
    path: str
    priority: int


@dataclass(frozen=True)
class SeedStatus:
    """Normalized per-torrent status reported by the seed client."""

    hash: str
    state: str
    progress: float
    label: str


class SeedClientError(RuntimeError):
    """Raised when a seed client operation fails."""


class SeedClient(Protocol):
    """Torrent-client contract used by the reseed pipeline."""

    def add_torrent(
        self, magnet: str, save_path: str, category: str = ""
    ) -> None:
        """Add a torrent by magnet, stopped, with an explicit save path."""
        ...

    def torrent_files(self, torrent_hash: str) -> list[SeedFile]:
        """Return the client's file list for a torrent (empty pre-metadata)."""
        ...

    def set_file_priorities(
        self, torrent_hash: str, file_indexes: list[int], priority: int
    ) -> None:
        """Set the download priority for the given file indexes."""
        ...

    def recheck(self, torrent_hash: str) -> None:
        """Force a recheck of the torrent's on-disk data."""
        ...

    def resume(self, torrent_hash: str) -> None:
        """Resume (start) the torrent so it seeds verified pieces."""
        ...

    def statuses(self, hashes: list[str]) -> dict[str, SeedStatus]:
        """Return normalized statuses for the given hashes, keyed by hash."""
        ...

    def delete(self, torrent_hash: str, delete_files: bool = False) -> None:
        """Remove the torrent, optionally deleting its staged data."""
        ...
