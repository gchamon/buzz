"""Derive the current version and CHANGELOG.md from git commit history.

Version rule (conventional baseline):
- The baseline version and date come from the newest commit that changed the
  ``version`` field in ``pyproject.toml`` (an explicit version bump).
- Every non-merge commit reachable from HEAD after that commit is a changelog
  entry. Commits with conventional subjects bump the version:
  ``feat:`` (or ``feat(scope):``) increments the minor, ``fix:`` the patch.
  Non-conventional subjects are listed in the changelog but never bump.
- Re-running the script is idempotent while no new commits or version bumps
  land: the derived version is deterministic.

Usage:
    uv run python maint-scripts/changelog.py [--repo PATH] [--out PATH]
    uv run python maint-scripts/changelog.py --update-pyproject
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

REPO_DEFAULT = Path(".")
DEFAULT_OUT = "CHANGELOG.md"
PYPROJECT_VERSION_RE = re.compile(
    r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE
)
CONVENTIONAL_RE = re.compile(r"^(feat|fix)(\(.+\))?:\s")


def run_git(repo: Path, *args: str) -> str:
    """Run a git command in the repository and return stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def version_bump_commits(repo: Path) -> list[tuple[str, str, str]]:
    """Return (sha, date, version) for every commit that changed the version."""
    out = run_git(
        repo,
        "log",
        "--no-merges",
        "--format=%H|%ad",
        "--date=iso",
        "-S",
        'version = "',
        "--",
        "pyproject.toml",
    )
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, date = line.split("|", 1)
        blob = run_git(repo, "show", f"{sha}:pyproject.toml")
        match = PYPROJECT_VERSION_RE.search(blob)
        version = match.group(1) if match else "unknown"
        commits.append((sha, date, version))
    return commits


def commits_since(repo: Path, anchor: str) -> list[tuple[str, str, str]]:
    """Return (sha, date, subject) of non-merge commits after the anchor."""
    out = run_git(
        repo,
        "log",
        "--no-merges",
        "--format=%H|%ad|%s",
        "--date=short",
        f"{anchor}..HEAD",
    )
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, date, subject = line.split("|", 2)
        entries.append((sha, date, subject))
    # Chronological order (git log is newest first).
    return list(reversed(entries))


def bump_version(
    current: tuple[int, int, int], subjects: list[str]
) -> tuple[int, int, int]:
    """Apply conventional-commit bumps to the baseline version."""
    major, minor, patch = current
    for subject in subjects:
        match = CONVENTIONAL_RE.match(subject)
        if match is None:
            continue
        if match.group(1) == "feat":
            minor += 1
            patch = 0
        else:
            patch += 1
    return major, minor, patch


def format_changelog(
    version: str,
    anchor_date: str,
    entries: list[tuple[str, str, str]],
) -> str:
    """Render the CHANGELOG.md document."""
    lines = [
        "# Changelog",
        "",
        f"## {version} - {datetime.date.today().isoformat()}",
        "",
    ]
    for entry in entries:
        lines.append(f"- {entry[2]}")
    lines += [
        "",
        f"Previous baseline: {anchor_date} (last explicit version bump).",
        "",
    ]
    return "\n".join(lines)


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse a strict ``MAJOR.MINOR.PATCH`` version string."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", text)
    if match is None:
        raise ValueError(f"unsupported version format: {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def main(argv: list[str] | None = None) -> int:
    """Derive the version and write CHANGELOG.md."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_DEFAULT)
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    parser.add_argument(
        "--update-pyproject",
        action="store_true",
        help="write the derived version back into pyproject.toml",
    )
    args = parser.parse_args(argv)

    bumps = version_bump_commits(args.repo)
    if not bumps:
        print("no explicit version bump commit found", file=sys.stderr)
        return 1
    anchor_sha, anchor_date, anchor_version = bumps[0]
    entries = commits_since(args.repo, anchor_sha)
    derived = bump_version(
        parse_version(anchor_version), [entry[2] for entry in entries]
    )
    version = ".".join(str(part) for part in derived)

    if args.update_pyproject:
        pyproject = args.repo / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        updated, count = PYPROJECT_VERSION_RE.subn(
            f'version = "{version}"', text, count=1
        )
        if count == 0:
            print("version field not found in pyproject.toml", file=sys.stderr)
            return 1
        pyproject.write_text(updated, encoding="utf-8")

    args.out.write_text(
        format_changelog(version, anchor_date, entries), encoding="utf-8"
    )
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
