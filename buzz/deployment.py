"""Deployment identity and process age for health endpoints.

Build identity (software version and git hash) is immutable for the lifetime
of a process; start time and uptime are captured when the owning app instance
is constructed.
"""

from __future__ import annotations

import os
import re
import time
from importlib import metadata
from pathlib import Path

from .core.utils import utc_now_iso

_UNKNOWN = "unknown"

GIT_SHA_ENV = "BUZZ_GIT_SHA"
_PACKAGE_NAME = "buzz"
_PYPROJECT_VERSION_RE = re.compile(
    r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE
)


def _project_version() -> str:
    """Return the released software version, or "unknown" when unavailable."""
    try:
        return metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        pass
    for candidate in (
        Path.cwd() / "pyproject.toml",
        Path(__file__).resolve().parent.parent / "pyproject.toml",
    ):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        match = _PYPROJECT_VERSION_RE.search(text)
        if match:
            return match.group(1)
    return _UNKNOWN


class DeploymentInfo:
    """Immutable build identity plus per-process start time and uptime."""

    def __init__(
        self,
        *,
        version: str | None = None,
        git_sha: str | None = None,
        started_at: str | None = None,
        started_monotonic: float | None = None,
    ) -> None:
        self._version = version if version is not None else _project_version()
        self._git_sha = (
            git_sha if git_sha is not None else os.environ.get(GIT_SHA_ENV, "").strip()
        )
        if not self._git_sha:
            self._git_sha = _UNKNOWN
        self._started_at = started_at or utc_now_iso()
        self._started_monotonic = (
            started_monotonic if started_monotonic is not None else time.monotonic()
        )

    @property
    def version(self) -> str:
        return self._version

    @property
    def git_hash(self) -> str:
        return self._git_sha

    @property
    def started_at(self) -> str:
        return self._started_at

    def uptime_seconds(self) -> int:
        return max(0, int(time.monotonic() - self._started_monotonic))

    def payload(self) -> dict[str, str | int]:
        """Return the JSON-safe deployment object for health endpoints."""
        return {
            "version": self._version,
            "git_hash": self._git_sha,
            "started_at": self._started_at,
            "uptime_seconds": self.uptime_seconds(),
        }
