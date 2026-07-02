"""Filesystem helpers for staging reseed bytes onto the seed path.

The disk-usage limit checks real filesystem usage via ``os.statvfs`` (not
per-feature bookkeeping), so it composes safely with anything else writing
to the same filesystem.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx

from ..core.utils import normalize_posix_path

STAGE_CHUNK_BYTES = 1 << 20
# Re-check the disk-usage limit after this many staged bytes.
USAGE_RECHECK_INTERVAL_BYTES = 64 << 20
PARTIAL_SUFFIX = ".buzz-partial"


class SeedUsageLimitError(RuntimeError):
    """Raised when staging would breach the seed-path usage limit."""


def _statvfs_probe_path(save_path: str) -> str:
    """Return the nearest existing ancestor of *save_path* for statvfs."""
    current = Path(save_path).absolute()
    while not current.exists() and current.parent != current:
        current = current.parent
    return str(current)


def fs_usage_percent(save_path: str, additional_bytes: int = 0) -> float:
    """Return projected usage percent after writing *additional_bytes*."""
    stats = os.statvfs(_statvfs_probe_path(save_path))
    total = stats.f_frsize * stats.f_blocks
    if total <= 0:
        return 100.0
    used = total - stats.f_frsize * stats.f_bavail
    return (used + max(0, additional_bytes)) / total * 100.0


def check_fs_usage(
    save_path: str, additional_bytes: int, max_percent: int
) -> str:
    """Return '' when the projected usage fits, else a readable reason."""
    try:
        projected = fs_usage_percent(save_path, additional_bytes)
    except OSError as exc:
        return f"seed path unavailable: {exc}"
    if projected > max_percent:
        return (
            f"projected seed filesystem usage {projected:.1f}% exceeds "
            f"seeding.max_fs_usage_percent ({max_percent}%)"
        )
    return ""


def stage_target_path(save_path: str, file_path: str) -> Path:
    """Return the staged destination for a torrent-relative file path."""
    relative = normalize_posix_path(file_path)
    if not relative or relative.startswith(".."):
        raise ValueError(f"unsafe stage path: {file_path!r}")
    return Path(save_path) / relative


def remove_staged_files(save_path: str, targets: list[Path]) -> None:
    """Delete staged files and prune now-empty directories under save_path."""
    root = Path(save_path).absolute()
    for target in targets:
        absolute_target = target.absolute()
        absolute_target.unlink(missing_ok=True)
        parent = absolute_target.parent
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def open_stage_stream(url: str, timeout_secs: int) -> Iterator[bytes]:
    """Yield response chunks for a staging download.

    This is the seam the staging pipeline uses for provider byte
    transfers; tests patch this function to inject fake content.
    """
    timeout = httpx.Timeout(
        connect=10.0,
        read=float(timeout_secs),
        write=10.0,
        pool=5.0,
    )
    with (
        httpx.Client(timeout=timeout, follow_redirects=True) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        yield from response.iter_bytes(STAGE_CHUNK_BYTES)
