"""Curator module for building and maintaining the media library.

This module handles symlink-based library construction from raw source
directories, applying metadata overrides, detecting changes, and
triggering downstream media server scans.
"""

import math
import os
import random
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path

import yaml

from ..models import CuratorConfig
from . import db
from .events import record_event
from .media import (
    is_sidecar_file,
    is_video_file,
    parse_movie,
    parse_show,
)
from .media_server import (
    probe_jellyfin_auth,
    trigger_jellyfin_scan,
    trigger_jellyfin_selective_refresh,
    validate_jellyfin_auth,
)
from .subtitles import apply_subtitle_overlay, background_fetch_subtitles
from .utils import (
    sanitize_path_component,
)


class RebuildError(RuntimeError):
    """Raised when a library rebuild fails with structured context."""

    def __init__(self, message: str, payload: dict) -> None:
        """Initialize with an error message and structured payload."""
        super().__init__(message)
        self.payload = payload


class MediaServerAuthError(RuntimeError):
    """Raised when startup media server auth validation fails."""


class ScanProbeError(RuntimeError):
    """Raised when media files cannot be read before a server scan."""


def load_overrides(path: Path) -> dict:
    """Load YAML override rules for movies and shows."""
    if not path.exists():
        return {"movies": {}, "shows": {}}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {"movies": {}, "shows": {}}
    overrides = yaml.safe_load(raw) or {}
    overrides.setdefault("movies", {})
    overrides.setdefault("shows", {})
    return overrides


def iter_files(root: Path) -> Iterator[Path]:
    """Yield all files under *root* in sorted order."""
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def source_relpath(source_root: Path, path: Path) -> str:
    """Return the POSIX relative path from *source_root* to *path*."""
    return path.relative_to(source_root).as_posix()


type CompanionIndex = dict[Path, tuple[Path, ...]]


def build_companion_index(files: list[Path]) -> CompanionIndex:
    """Index sidecar files by parent directory for one rebuild pass."""
    index: dict[Path, list[Path]] = {}
    for path in files:
        if is_sidecar_file(path):
            index.setdefault(path.parent, []).append(path)
    return {
        parent: tuple(sorted(paths))
        for parent, paths in index.items()
    }


def find_companion_files(
    path: Path,
    companion_index: CompanionIndex | None = None,
) -> list[Path]:
    """Return sorted sidecar files sharing *path*'s stem."""
    parent = path.parent
    stem = path.stem
    companions = []
    siblings = (
        companion_index.get(parent, ())
        if companion_index is not None
        else tuple(parent.iterdir())
    )
    for sibling in siblings:
        if sibling == path:
            continue
        if companion_index is None and (
            not sibling.is_file() or not is_sidecar_file(sibling)
        ):
            continue
        if (
            sibling.name == f"{stem}{sibling.suffix}"
            or sibling.name.startswith(f"{stem}.")
        ):
            companions.append(sibling)
    return sorted(companions)


def ensure_symlink(source: Path, target: Path) -> None:
    """Create parent directories and a symlink from *target* to *source*."""
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(source, target)


def apply_movie_override(entry: dict, override: dict) -> None:
    """Apply override fields to a movie entry in place."""
    if override.get("title"):
        entry["title"] = sanitize_path_component(override["title"])
    if override.get("year"):
        entry["year"] = int(override["year"])
    if override.get("id"):
        entry["id"] = sanitize_path_component(override["id"])


def apply_show_override(entry: dict, override: dict) -> None:
    """Apply override fields to a show entry in place."""
    if override.get("series"):
        entry["series"] = sanitize_path_component(override["series"])
    if override.get("season") is not None:
        entry["season"] = int(override["season"])
    if override.get("episode") is not None:
        entry["episode"] = int(override["episode"])
    if override.get("id"):
        entry["id"] = sanitize_path_component(override["id"])


def movie_folder_name(entry: dict) -> str:
    """Return the canonical folder name for a movie entry."""
    folder = f"{entry['title']} ({entry['year']})"
    if entry.get("id"):
        folder = f"{folder} [{entry['id']}]"
    return sanitize_path_component(folder)


def show_series_name(entry: dict) -> str:
    """Return the canonical series name for a show entry."""
    series = entry["series"]
    if entry.get("id"):
        series = f"{series} [{entry['id']}]"
    return sanitize_path_component(series)


def _merge_tree(src: Path, dst: Path) -> None:
    """Merge *src* into *dst*, preserving unchanged symlinks by inode.

    Symlinks whose recorded target string is unchanged are left in place so
    Jellyfin does not see inode/ctime churn for unmodified content.
    Entries present in *dst* but absent from *src* are removed.
    """
    src_names = {item.name for item in src.iterdir()}

    # Remove entries in dst that are no longer in src
    for item in list(dst.iterdir()):
        if item.name not in src_names:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()

    # Merge entries from src into dst
    for src_item in src.iterdir():
        dst_item = dst / src_item.name
        if src_item.is_symlink():
            new_target = os.readlink(src_item)
            if dst_item.is_symlink() and os.readlink(dst_item) == new_target:
                src_item.unlink()  # existing symlink is already correct
            else:
                if dst_item.exists() or dst_item.is_symlink():
                    if dst_item.is_dir() and not dst_item.is_symlink():
                        shutil.rmtree(dst_item)
                    else:
                        dst_item.unlink()
                shutil.move(str(src_item), str(dst_item))
        elif src_item.is_dir():
            dst_item.mkdir(exist_ok=True)
            _merge_tree(src_item, dst_item)
            src_item.rmdir()
        else:
            if dst_item.exists() or dst_item.is_symlink():
                dst_item.unlink()
            shutil.move(str(src_item), str(dst_item))


def replace_root(tmp_root: Path, target_root: Path) -> None:
    """Merge *tmp_root* into *target_root*, preserving unchanged symlinks.

    Operates on contents to avoid needing write permissions on
    *target_root*'s parent. Skips in-flight .curator-tmp-* directories.
    """
    # Move top-level dirs/files through merge, skipping in-flight tmp dirs
    for item in list(target_root.iterdir()):
        if item.is_dir() and item.name.startswith(".curator-tmp-"):
            continue
        if item.name not in {i.name for i in tmp_root.iterdir()}:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()

    for src_item in tmp_root.iterdir():
        dst_item = target_root / src_item.name
        if src_item.is_dir():
            dst_item.mkdir(exist_ok=True)
            _merge_tree(src_item, dst_item)
            src_item.rmdir()
        elif src_item.is_symlink():
            new_target = os.readlink(src_item)
            if dst_item.is_symlink() and os.readlink(dst_item) == new_target:
                src_item.unlink()
            else:
                if dst_item.exists() or dst_item.is_symlink():
                    if dst_item.is_dir() and not dst_item.is_symlink():
                        shutil.rmtree(dst_item)
                    else:
                        dst_item.unlink()
                shutil.move(str(src_item), str(dst_item))
        else:
            if dst_item.exists() or dst_item.is_symlink():
                dst_item.unlink()
            shutil.move(str(src_item), str(dst_item))

    shutil.rmtree(tmp_root, ignore_errors=True)


def load_previous_mapping(conn) -> list[dict]:
    """Load the previous mapping from the SQLite state store."""
    return db.load_curator_mapping(conn)


def mapping_index(entries: list[dict]) -> dict[str, dict]:
    """Build a lookup dict mapping target paths to entries."""
    return {
        target: entry
        for entry in entries
        if isinstance((target := entry.get("target")), str)
    }


def mapping_diff(previous: list[dict], current: list[dict]) -> dict:
    """Compare two mappings and return added, removed, and changed items."""
    previous_index = mapping_index(previous)
    current_index = mapping_index(current)

    added = [
        current_index[target]
        for target in sorted(current_index.keys() - previous_index.keys())
    ]
    removed = [
        previous_index[target]
        for target in sorted(previous_index.keys() - current_index.keys())
    ]
    changed = []
    for target in sorted(previous_index.keys() & current_index.keys()):
        if previous_index[target] != current_index[target]:
            changed.append(
                {
                    "before": previous_index[target],
                    "after": current_index[target],
                }
            )

    return {"added": added, "removed": removed, "changed": changed}


def log_mapping_event(diff: dict, report: dict, mapping_entries: int) -> None:
    """Record a Curator mapping diff event."""
    record_event(
        "Curator mapping updated",
        event="curator_mapping_diff",
        mapping_entries=mapping_entries,
        movies=report["movies"],
        show_files=report["show_files"],
        anime_files=report["anime_files"],
        added=diff["added"],
        removed=diff["removed"],
        changed=diff["changed"],
    )


def scan_probe_sample_size(pool_size: int, ratio_percent: int, min_files: int) -> int:
    """Return the number of files to sample from a probe pool."""
    if pool_size <= 0:
        return 0
    ratio_size = math.ceil(pool_size * max(0, ratio_percent) / 100)
    return min(pool_size, max(max(0, min_files), ratio_size))


def _changed_root_matches(source: str, changed_roots: list[str]) -> bool:
    return any(
        source == root or source.startswith(f"{root}/")
        for root in changed_roots
    )


def _scan_probe_pool(mapping: list[dict], changed_roots: list[str] | None) -> list[str]:
    roots = [root.strip("/") for root in changed_roots or [] if root.strip("/")]
    sources = [
        source
        for entry in mapping
        if isinstance((source := entry.get("source")), str)
    ]
    if not roots:
        return sorted(dict.fromkeys(sources))
    return sorted(
        dict.fromkeys(
            source for source in sources if _changed_root_matches(source, roots)
        )
    )


def _read_probe_file(path: Path, read_bytes: int) -> None:
    with path.open("rb") as handle:
        data = handle.read(read_bytes)
    if not data:
        raise ScanProbeError(f"probe read returned no bytes for {path}")


def validate_scan_probe(
    config: CuratorConfig,
    mapping: list[dict],
    changed_roots: list[str] | None,
) -> None:
    """Read a sample of source files before triggering a media-server scan."""
    probe = config.scan_probe
    if not probe.enabled:
        return

    pool = _scan_probe_pool(mapping, changed_roots)
    sample_size = scan_probe_sample_size(
        len(pool), probe.sample_ratio_percent, probe.min_files
    )
    if sample_size <= 0:
        raise ScanProbeError("no media files available for scan probe")

    record_event(
        f"starting Jellyfin scan probe: {sample_size} of {len(pool)} file(s)",
        event="jellyfin_scan_probe_started",
        sample_size=sample_size,
        pool_size=len(pool),
    )

    workers = max(1, min(probe.concurrency, sample_size))

    last_error: BaseException | None = None
    for attempt in range(probe.max_attempts):
        sample = random.sample(pool, sample_size)
        attempt_error: BaseException | None = None
        with ThreadPoolExecutor(max_workers=workers) as pool_exec:
            futures = [
                pool_exec.submit(
                    _read_probe_file,
                    config.source_root / source,
                    probe.read_bytes,
                )
                for source in sample
            ]
            done, not_done = wait(futures, return_when=FIRST_EXCEPTION)
            for future in not_done:
                future.cancel()
            for future in done:
                exc = future.exception()
                if exc is not None:
                    attempt_error = exc
                    break
        if attempt_error is not None:
            last_error = attempt_error
            if attempt < probe.max_attempts - 1:
                record_event(
                    f"retrying Jellyfin scan probe after failure: {attempt_error}",
                    level="warning",
                    event="jellyfin_scan_probe_retry",
                    attempt=attempt + 1,
                    sample_size=sample_size,
                )
                time.sleep(probe.retry_delay_secs)
                continue
            break
        record_event(
            "jellyfin scan probe succeeded",
            event="jellyfin_scan_probe_succeeded",
            sample_size=sample_size,
            pool_size=len(pool),
        )
        return

    record_event(
        "jellyfin scan probe failed",
        level="error",
        event="jellyfin_scan_probe_exhausted",
        sample_size=sample_size,
        pool_size=len(pool),
    )
    raise ScanProbeError(str(last_error) if last_error else "scan probe failed")


def build_library(config: CuratorConfig) -> dict:
    """Build the curated library from source directories."""
    overrides = load_overrides(config.overrides_path)
    movies_source = config.source_root / "movies"
    shows_source = config.source_root / "shows"
    anime_source = config.source_root / "anime"

    if not config.source_root.exists():
        raise FileNotFoundError(
            f"Source root does not exist: {config.source_root}"
        )

    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.target_root.mkdir(parents=True, exist_ok=True)

    conn = db.connect(config.state_dir / "buzz.sqlite")
    db.apply_migrations(conn)
    previous_mapping = load_previous_mapping(conn)
    mapping = []
    report = {
        "skipped_movies": [],
        "skipped_shows": [],
        "anime_files": 0,
        "movies": 0,
        "show_files": 0,
    }

    tmp_root: Path | None = None
    try:
        if config.subtitles.enabled:
            db.migrate_subtitle_sidecars(conn, config.subtitle_root)

        tmp_root = Path(
            tempfile.mkdtemp(prefix=".curator-tmp-", dir=config.target_root)
        )
        build_movies(
            movies_source,
            tmp_root / "movies",
            overrides.get("movies", {}),
            mapping,
            report,
            config.source_root,
        )
        build_shows(
            shows_source,
            tmp_root / "shows",
            overrides.get("shows", {}),
            mapping,
            report,
            config.source_root,
        )
        build_anime(
            anime_source,
            tmp_root / "animes",
            mapping,
            report,
            config.source_root,
        )

        if config.subtitles.enabled:
            apply_subtitle_overlay(tmp_root, config.subtitle_root)

        replace_root(tmp_root, config.target_root)
    except Exception:
        if tmp_root is not None and tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
        conn.close()
        raise

    report["mapping_entries"] = len(mapping)
    db.replace_curator_mapping(conn, mapping)
    db.save_curator_report(conn, report)
    conn.close()

    log_mapping_event(
        mapping_diff(previous_mapping, mapping), report, len(mapping)
    )
    return report


def _process_movie_file(
    path: Path,
    source_root: Path,
    target_root: Path,
    all_source_root: Path,
    overrides: dict,
    used_targets: set[str],
    report: dict,
    mapping: list[dict],
    companion_index: CompanionIndex,
) -> bool:
    rel_path = source_relpath(all_source_root, path)
    source_rel = path.relative_to(source_root)
    folder = source_rel.parts[0] if len(source_rel.parts) > 1 else ""

    parsed = parse_movie(path.stem, folder=folder)
    override = overrides.get(rel_path, {})
    if parsed is None and not override:
        report["skipped_movies"].append(
            {"source": rel_path, "reason": "unable to parse movie title/year"}
        )
        return False
    if parsed is None:
        parsed = {"title": "", "year": 0}
    apply_movie_override(parsed, override)
    if not parsed.get("title") or not parsed.get("year"):
        report["skipped_movies"].append(
            {"source": rel_path, "reason": "movie override missing title/year"}
        )
        return False

    folder_name = movie_folder_name(parsed)
    target_file = target_root / folder_name / f"{folder_name}{path.suffix.lower()}"
    target_key = target_file.as_posix()
    if target_key in used_targets:
        report["skipped_movies"].append(
            {"source": rel_path, "reason": "duplicate canonical movie target"}
        )
        return False

    ensure_symlink(path, target_file)
    used_targets.add(target_key)
    mapping.append(
        {
            "source": rel_path,
            "target": target_file.relative_to(target_root.parent).as_posix(),
            "type": "movie",
        }
    )
    report["movies"] += 1

    for companion in find_companion_files(path, companion_index):
        extra = companion.name[len(path.stem) :]
        companion_target = target_root / folder_name / f"{folder_name}{extra}"
        ensure_symlink(companion, companion_target)
    return True


def build_movies(
    source_root: Path,
    target_root: Path,
    overrides: dict,
    mapping: list[dict],
    report: dict,
    all_source_root: Path,
) -> None:
    """Symlink movie files into canonical folder structures."""
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return
    files = list(iter_files(source_root))
    companion_index = build_companion_index(files)
    used_targets: set[str] = set()
    for path in files:
        if not is_video_file(path):
            continue
        _process_movie_file(
            path, source_root, target_root, all_source_root,
            overrides, used_targets, report, mapping, companion_index,
        )


def _plan_show_group(
    files: list[Path],
    source_root: Path,
    target_root: Path,
    all_source_root: Path,
    overrides: dict,
    global_targets: set[str],
) -> tuple[list[dict], list[dict]]:
    planned = []
    group_errors = []
    group_series = None
    used_targets: set[str] = set()
    for path in sorted(files):
        rel_path = source_relpath(all_source_root, path)
        parsed = parse_show(path.stem)
        override = overrides.get(rel_path, {})
        if parsed is None and not override:
            group_errors.append(
                {"source": rel_path, "reason": "unable to parse show season/episode"}
            )
            continue
        if parsed is None:
            parsed = {"series": "", "season": 0, "episode": 0}
        apply_show_override(parsed, override)
        if (
            not parsed.get("series")
            or parsed.get("season") is None
            or parsed.get("episode") is None
        ):
            group_errors.append(
                {
                    "source": rel_path,
                    "reason": "show override missing series/season/episode",
                }
            )
            continue
        series_name = show_series_name(parsed)
        if group_series is None:
            group_series = series_name
        elif group_series != series_name:
            group_errors.append(
                {
                    "source": rel_path,
                    "reason": "inconsistent parsed show name within torrent",
                }
            )
            continue
        season_dir = f"Season {int(parsed['season']):02d}"
        base_name = (
            f"{series_name} S{int(parsed['season']):02d}"
            f"E{int(parsed['episode']):02d}"
        )
        target_file = (
            target_root
            / series_name
            / season_dir
            / f"{base_name}{path.suffix.lower()}"
        )
        target_key = target_file.as_posix()
        if target_key in used_targets or target_key in global_targets:
            group_errors.append(
                {"source": rel_path, "reason": "duplicate season/episode target"}
            )
            continue
        used_targets.add(target_key)
        planned.append(
            {
                "path": path,
                "rel_path": rel_path,
                "target_file": target_file,
                "base_name": base_name,
            }
        )
    return planned, group_errors


def _apply_show_planned(
    planned: list[dict],
    target_root: Path,
    global_targets: set[str],
    mapping: list[dict],
    report: dict,
    companion_index: CompanionIndex,
) -> None:
    for item in planned:
        path = item["path"]
        rel_path = item["rel_path"]
        target_file = item["target_file"]
        base_name = item["base_name"]
        ensure_symlink(path, target_file)
        global_targets.add(target_file.as_posix())
        mapping.append(
            {
                "source": rel_path,
                "target": target_file.relative_to(
                    target_root.parent
                ).as_posix(),
                "type": "show",
            }
        )
        report["show_files"] += 1
        for companion in find_companion_files(path, companion_index):
            extra = companion.name[len(path.stem) :]
            companion_target = target_file.parent / f"{base_name}{extra}"
            ensure_symlink(companion, companion_target)


def build_shows(
    source_root: Path,
    target_root: Path,
    overrides: dict,
    mapping: list[dict],
    report: dict,
    all_source_root: Path,
) -> None:
    """Symlink show files into canonical series/season structures."""
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return
    files = list(iter_files(source_root))
    companion_index = build_companion_index(files)
    grouped: dict[str, list[Path]] = {}
    global_targets: set[str] = set()
    for path in files:
        if not is_video_file(path):
            continue
        rel = path.relative_to(source_root)
        group_key = rel.parts[0] if len(rel.parts) > 1 else path.stem
        grouped.setdefault(group_key, []).append(path)

    for group_name, files in sorted(grouped.items()):
        planned, group_errors = _plan_show_group(
            files, source_root, target_root, all_source_root,
            overrides, global_targets,
        )
        if group_errors:
            report["skipped_shows"].append(
                {"group": group_name, "errors": group_errors}
            )
            continue
        _apply_show_planned(
            planned,
            target_root,
            global_targets,
            mapping,
            report,
            companion_index,
        )


def build_anime(
    source_root: Path,
    target_root: Path,
    mapping: list[dict],
    report: dict,
    all_source_root: Path,
) -> None:
    """Symlink anime files while preserving their relative paths."""
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return
    for path in iter_files(source_root):
        rel_path = source_relpath(all_source_root, path)
        target_file = target_root / path.relative_to(source_root)
        ensure_symlink(path, target_file)
        mapping.append(
            {
                "source": rel_path,
                "target": target_file.relative_to(
                    target_root.parent
                ).as_posix(),
                "type": "anime",
            }
        )
        report["anime_files"] += 1


def validate_media_server_startup_auth(
    config: CuratorConfig,
    timeout_secs: float = 300,
    retry_interval_secs: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Validate startup media server auth when scan triggering is enabled."""
    if not config.trigger_lib_scan or _media_server_kind(config) != "jellyfin":
        return
    if not config.jellyfin_api_key:
        raise MediaServerAuthError(
            "media_server.jellyfin.api_key is required when "
            "media_server.trigger_lib_scan is true."
        )

    deadline = monotonic() + timeout_secs
    last_error = ""
    while True:
        probe = probe_jellyfin_auth(config)
        if probe.valid:
            return
        if probe.invalid_token:
            raise MediaServerAuthError(
                "Jellyfin API Token is invalid or unauthorized"
            )

        last_error = probe.error
        now = monotonic()
        if now >= deadline:
            if probe.unreachable:
                raise MediaServerAuthError(
                    f"jellyfin is unreachable at {config.jellyfin_url}."
                )
            detail = f": {last_error}" if last_error else "."
            raise MediaServerAuthError(
                f"Could not validate Jellyfin API token{detail}"
            )
        sleep(min(retry_interval_secs, max(0, deadline - now)))


def rebuild_and_trigger(
    config: CuratorConfig,
    changed_roots: list[str] | None = None,
) -> dict:
    """Rebuild the library and optionally trigger a Jellyfin scan."""
    report = build_library(config)
    if not config.trigger_lib_scan:
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "skipped_configured"
        report["jellyfin_scan_error"] = None
        return report
    missing_token_warning = _missing_media_server_token_warning(config)
    if missing_token_warning:
        record_event(missing_token_warning, level="warning")
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "skipped_missing_auth"
        report["jellyfin_scan_error"] = None
        return report
    media_server_kind = _media_server_kind(config)
    if media_server_kind != "jellyfin":
        msg = (
            f"media server kind '{media_server_kind}' refresh is not "
            "implemented by curator."
        )
        record_event(msg, level="warning")
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "skipped_unsupported"
        report["jellyfin_scan_error"] = None
        return report

    conn = db.connect(config.state_dir / "buzz.sqlite")
    try:
        mapping = db.load_curator_mapping(conn)
    finally:
        conn.close()

    try:
        validate_scan_probe(config, mapping, changed_roots)
    except ScanProbeError as exc:
        msg = f"jellyfin scan skipped: Real-Debrid probe failed: {exc}"
        record_event(msg, level="error", event="jellyfin_scan_probe_failed")
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "skipped_probe_failed"
        report["jellyfin_scan_error"] = str(exc)
        return report

    # Validate auth first to avoid cascading failures
    if not validate_jellyfin_auth(config):
        msg = "Jellyfin API Token is invalid or unauthorized"
        record_event(msg, level="error")
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "failed_auth"
        report["jellyfin_scan_error"] = msg
        return report

    try:
        if changed_roots:
            trigger_jellyfin_selective_refresh(config, changed_roots)
            report["jellyfin_scan_status"] = "selective_triggered"
        else:
            trigger_jellyfin_scan(config)
            report["jellyfin_scan_status"] = "full_triggered"
    except Exception as exc:
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "failed"
        report["jellyfin_scan_error"] = str(exc)
        # We don't raise RebuildError here anymore to ensure the curator
        # process doesn't think the whole rebuild failed just because the
        # scan trigger failed. The symlinks (build_library) were already
        # successfully swapped.
        record_event(f"jellyfin scan trigger failed: {exc}", level="error")
    else:
        report["jellyfin_scan_triggered"] = True
        report["jellyfin_scan_error"] = None

    if config.subtitles.enabled and config.subtitles.fetch_on_resync:
        background_fetch_subtitles(config)

    return report


def _missing_media_server_token_warning(config: CuratorConfig) -> str:
    kind = _media_server_kind(config)
    if kind == "jellyfin" and not config.jellyfin_api_key:
        return (
            "Jellyfin scan skipped: media_server.jellyfin.api_key is empty "
            "for media_server.kind jellyfin."
        )
    if kind == "plex" and not config.plex_token:
        return (
            "Plex refresh skipped: media_server.plex.token is empty "
            "for media_server.kind plex."
        )
    return ""


def _media_server_kind(config: CuratorConfig) -> str:
    return config.media_server_kind.strip().lower() or "jellyfin"


class Curator:
    """Thread-safe wrapper around library rebuild operations."""

    def __init__(self, config: CuratorConfig) -> None:
        """Initialize with the curator configuration."""
        self.config = config
        self.lock = threading.Lock()

    def handle_rebuild(
        self,
        changed_roots: list[str] | None = None,
    ) -> dict:
        """Rebuild the library and trigger Jellyfin scan."""
        with self.lock:
            return rebuild_and_trigger(self.config, changed_roots)
