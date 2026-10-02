"""Derive the current version and CHANGELOG.md from branch commit history.

Version rule (target-branch baseline, squashed merge request):
- The baseline version is the ``version`` field in ``pyproject.toml`` on the
  target branch. Outside a merge-request pipeline the target is ``main``
  and the source is ``HEAD``; pass ``--target-branch REF`` to derive
  against another target branch. In a GitLab merge-request pipeline the
  source and target are read from
  ``CI_MERGE_REQUEST_SOURCE_BRANCH_NAME``/``CI_MERGE_REQUEST_TARGET_BRANCH_NAME``
  and resolved as ``origin/<branch>`` refs.
- Every non-merge commit in ``TARGET..SOURCE`` is a changelog entry. The
  version applies the single highest conventional bump in that set, as a
  squash commit would: any ``feat:`` (or ``feat(scope):``) raises the minor
  version by one and resets the patch version; otherwise any ``fix:``
  raises the patch version by one. All other subjects appear in the
  changelog but never bump.
- Writing the derived version into ``pyproject.toml`` and regenerating
  ``CHANGELOG.md`` is the default behavior; ``--check`` only verifies.
- Re-running the script is idempotent while no new commits land: the
  derived version is deterministic.

Usage:
    uv run python maint-scripts/changelog.py [--repo PATH] [--out PATH]
    uv run python maint-scripts/changelog.py --target-branch main
    uv run python maint-scripts/changelog.py --check
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REPO_DEFAULT = Path(".")
DEFAULT_OUT = "CHANGELOG.md"
PYPROJECT_VERSION_RE = re.compile(
    r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE
)
CONVENTIONAL_RE = re.compile(r"^(feat|fix)(\(.+\))?:\s")


@dataclass(frozen=True)
class ChangelogEntry:
    sha: str
    date: str
    subject: str


@dataclass(frozen=True)
class ResolvedRevision:
    full_sha: str
    short_sha: str
    date: str


@dataclass(frozen=True)
class RevisionRange:
    target: str
    source: str


@dataclass(frozen=True)
class VersionDerivation:
    version: str
    baseline_version: str
    baseline_sha: str
    baseline_date: str
    entries: list[ChangelogEntry]
    baseline_url: str | None


def run_git(repo: Path, *args: str) -> str:
    """Run a git command in the repository and return stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def resolve_target(repo: Path, target: str) -> ResolvedRevision:
    """Return metadata for the resolved target revision."""
    full = run_git(repo, "rev-parse", "--verify", f"{target}^{{commit}}").strip()
    short = run_git(repo, "rev-parse", "--short=7", full).strip()
    date = run_git(
        repo, "show", "--no-patch", "--format=%ad", "--date=iso", full
    ).strip()
    return ResolvedRevision(full, short, date)


def target_baseline_version(repo: Path, target_sha: str) -> str:
    """Return the semantic version pinned in the target's pyproject.toml."""
    blob = run_git(repo, "show", f"{target_sha}:pyproject.toml")
    match = PYPROJECT_VERSION_RE.search(blob)
    if match is None:
        raise ValueError(f"version field not found in {target_sha}:pyproject.toml")
    return match.group(1)


def branch_commits(
    repo: Path, target: str, source: str
) -> list[ChangelogEntry]:
    """Return non-merge commits in target..source."""
    out = run_git(
        repo,
        "log",
        "--no-merges",
        "--format=%H|%ad|%s",
        "--date=short",
        f"{target}..{source}",
    )
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, date, subject = line.split("|", 2)
        entries.append(ChangelogEntry(sha, date, subject))
    return list(reversed(entries))


def classify_bump(subjects: list[str]) -> str:
    """Return the single squashed-MR bump level implied by the subjects.

    ``minor`` if any subject is a feature, otherwise ``patch`` if any subject
    is a fix, otherwise ``none``. A feature wins because a squash commit can
    only carry the highest conventional bump in the set.
    """
    kinds = {
        CONVENTIONAL_RE.match(subject).group(1)
        for subject in subjects
        if CONVENTIONAL_RE.match(subject)
    }
    if "feat" in kinds:
        return "minor"
    if "fix" in kinds:
        return "patch"
    return "none"


def bump_version(
    current: tuple[int, int, int], subjects: list[str]
) -> tuple[int, int, int]:
    """Apply the squashed-MR bump level to the baseline version."""
    major, minor, patch = current
    level = classify_bump(subjects)
    if level == "minor":
        minor += 1
        patch = 0
    elif level == "patch":
        patch += 1
    return major, minor, patch


def baseline_permalink(repo: Path, baseline_sha: str) -> str | None:
    """Return the GitLab permalink to the baseline CHANGELOG.md blob.

    Credentials in GitLab HTTPS remotes are removed before matching. Other
    hosts or an unrecognized/missing ``origin`` yield no permalink.
    """
    url = origin_remote_summary(repo)
    match = re.match(
        r"(?:https?://|ssh://)gitlab\.com/"
        r"([^/]+/[^/#]+?)(?:\.git)?/?$",
        url,
    )
    if match is None:
        return None
    return (
        f"https://gitlab.com/{match.group(1)}"
        f"/-/blob/{baseline_sha}/CHANGELOG.md"
    )


def format_changelog(
    version: str,
    baseline_version: str,
    baseline_sha: str,
    baseline_date: str,
    entries: list[ChangelogEntry],
    baseline_url: str | None = None,
) -> str:
    """Render the CHANGELOG.md document."""
    baseline = (
        f"{baseline_version} ({baseline_sha}, {baseline_date})"
        if baseline_url is None
        else f"[{baseline_version} ({baseline_sha}, {baseline_date})]"
        f"({baseline_url})"
    )
    lines = [
        "# Changelog",
        "",
        f"## {version} - {datetime.date.today().isoformat()}",
        "",
    ]
    for entry in entries:
        lines.append(f"- {entry.subject}")
    lines += ["", f"Previous baseline: {baseline}.", ""]
    return "\n".join(lines)


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse a strict ``MAJOR.MINOR.PATCH`` version string."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", text)
    if match is None:
        raise ValueError(f"unsupported version format: {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def update_pyproject_version(repo: Path, version: str) -> None:
    """Write the derived version back into pyproject.toml."""
    pyproject = repo / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    updated, count = PYPROJECT_VERSION_RE.subn(
        f'version = "{version}"', text, count=1
    )
    if count == 0:
        raise ValueError("version field not found in pyproject.toml")
    pyproject.write_text(updated, encoding="utf-8")


def resolve_revisions(
    env: dict[str, str], target_branch: str | None
) -> RevisionRange:
    """Return the target and source revisions for derivation."""
    source_branch = env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", "").strip()
    target_from_env = env.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "").strip()
    if source_branch and target_from_env:
        target = target_branch if target_branch is not None else target_from_env
        return RevisionRange(f"origin/{target}", f"origin/{source_branch}")
    target = target_branch or "main"
    return RevisionRange(target, "HEAD")


def derive(repo: Path, target: str, source: str) -> VersionDerivation:
    """Derive the version and baseline metadata for the branch."""
    resolved = resolve_target(repo, target)
    baseline_version = target_baseline_version(repo, resolved.full_sha)
    entries = branch_commits(repo, target, source)
    derived = bump_version(
        parse_version(baseline_version), [entry.subject for entry in entries]
    )
    version = ".".join(str(part) for part in derived)
    return VersionDerivation(
        version,
        baseline_version,
        resolved.short_sha,
        resolved.date,
        entries,
        baseline_permalink(repo, resolved.short_sha),
    )


def first_text_difference(expected: str, actual: str) -> str:
    """Describe the first differing line without dumping whole metadata files."""
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    for index in range(max(len(expected_lines), len(actual_lines))):
        expected_line = (
            expected_lines[index] if index < len(expected_lines) else "<EOF>"
        )
        actual_line = (
            actual_lines[index] if index < len(actual_lines) else "<EOF>"
        )
        if expected_line != actual_line:
            return (
                f"line {index + 1}: expected {expected_line[:200]!r}; "
                f"found {actual_line[:200]!r}"
            )
    return "content differs (likely line endings)"


def short_revision(repo: Path, revision: str) -> str:
    """Resolve a ref to a short commit ID for diagnostics."""
    return run_git(
        repo, "rev-parse", "--short", "--verify", f"{revision}^{{commit}}"
    ).strip()


def origin_remote_summary(repo: Path) -> str:
    """Describe origin without exposing credentials embedded in its URL."""
    try:
        remote = run_git(repo, "remote", "get-url", "origin").strip()
    except subprocess.CalledProcessError:
        return "<unavailable>"
    if remote.startswith("git@") and ":" in remote:
        host, path = remote.split("@", 1)[1].split(":", 1)
        return f"ssh://{host}/{path}"
    parsed = urlsplit(remote)
    if parsed.scheme and parsed.hostname:
        return urlunsplit(
            (parsed.scheme, parsed.hostname, parsed.path, "", "")
        )
    return "<unrecognized URL format>"


def render_command(args: argparse.Namespace) -> str:
    """Return the invocation that reproduces the current check."""
    command = "uv run python maint-scripts/changelog.py"
    if args.target_branch is not None:
        command += f" --target-branch {args.target_branch}"
    if args.repo != REPO_DEFAULT:
        command += f" --repo {args.repo}"
    if args.out != Path(DEFAULT_OUT):
        command += f" --out {args.out}"
    return command


def report_stale_metadata(
    args: argparse.Namespace,
    revisions: RevisionRange,
    derivation: VersionDerivation,
    stale: list[str],
    expected: str,
    actual: str | None,
    lock_detail: str | None,
) -> None:
    """Log the inputs and first content mismatch behind a failed check."""
    source_sha = short_revision(args.repo, revisions.source)
    print(
        "changelog: stale metadata: " + ", ".join(stale),
        file=sys.stderr,
    )
    print(
        "changelog: revisions: "
        f"target={revisions.target}@{derivation.baseline_sha}, "
        f"source={revisions.source}@{source_sha}",
        file=sys.stderr,
    )
    print(
        "changelog: origin remote: "
        f"{origin_remote_summary(args.repo)}",
        file=sys.stderr,
    )
    print(
        "changelog: derived version "
        f"{derivation.version} from baseline "
        f"{derivation.baseline_version}; "
        f"{len(derivation.entries)} non-merge commits; "
        f"baseline link "
        f"{derivation.baseline_url or '<omitted>'}",
        file=sys.stderr,
    )
    if actual is not None and actual != expected:
        print(
            "changelog: " + first_text_difference(expected, actual),
            file=sys.stderr,
        )
    if lock_detail:
        print(f"changelog: uv.lock check: {lock_detail}", file=sys.stderr)
    print(
        "changelog: regenerate with: "
        f"{render_command(args)} && uv lock",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """Derive the version and write or check CHANGELOG.md."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_DEFAULT)
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    parser.add_argument(
        "--target-branch",
        default=None,
        help="target branch to derive from (default: main, or the "
        "merge-request target branch on GitLab)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify pyproject.toml and CHANGELOG.md are up to date; "
        "modify nothing",
    )
    args = parser.parse_args(argv)
    revisions = resolve_revisions(os.environ, args.target_branch)
    try:
        derivation = derive(args.repo, revisions.target, revisions.source)
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(
            "changelog: revisions: "
            f"target={revisions.target}, source={revisions.source}",
            file=sys.stderr,
        )
        if isinstance(exc, subprocess.CalledProcessError):
            print(
                f"changelog: command failed (exit {exc.returncode}): "
                f"{exc.cmd}",
                file=sys.stderr,
            )
            if exc.stderr:
                print(
                    "changelog: command stderr: "
                    + exc.stderr.strip().replace("\n", " "),
                    file=sys.stderr,
                )
        else:
            message = str(exc).replace("\n", " ").strip()
            print(f"changelog: {message}", file=sys.stderr)
        return 1
    changelog = format_changelog(
        derivation.version,
        derivation.baseline_version,
        derivation.baseline_sha,
        derivation.baseline_date,
        derivation.entries,
        derivation.baseline_url,
    )
    if args.repo != REPO_DEFAULT and not args.out.is_absolute():
        args.out = args.repo / args.out

    if args.check:
        stale = []
        pyproject = args.repo / "pyproject.toml"
        current_version = PYPROJECT_VERSION_RE.search(
            pyproject.read_text(encoding="utf-8")
        ).group(1)
        if current_version != derivation.version:
            stale.append(
                f"pyproject.toml (has {current_version}, wants {derivation.version})"
            )
        existing = None
        if args.out.exists():
            existing = args.out.read_text(encoding="utf-8")
            if existing != changelog:
                stale.append(str(args.out))
        else:
            stale.append(str(args.out) + " (missing)")
        lock_detail = None
        if (args.repo / "uv.lock").exists():
            lock = subprocess.run(
                ["uv", "lock", "--check", "--directory", str(args.repo)],
                capture_output=True,
                text=True,
            )
            if lock.returncode != 0:
                stale.append("uv.lock (out of sync with pyproject.toml)")
                lock_detail = (lock.stderr or lock.stdout).strip()
        if stale:
            report_stale_metadata(
                args,
                revisions,
                derivation,
                stale,
                changelog,
                existing,
                lock_detail,
            )
            return 1
        print(derivation.version)
        return 0

    update_pyproject_version(args.repo, derivation.version)
    args.out.write_text(changelog, encoding="utf-8")
    print(derivation.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
