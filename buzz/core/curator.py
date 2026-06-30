"""Curator module for building and maintaining the media library.

This module handles symlink-based library construction from raw source
directories, applying metadata overrides, detecting changes, and
triggering downstream media server scans.
"""

import json
import math
import os
import random
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path

from ..models import CuratorConfig, category_definitions
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




def _db_override_source_paths(
    conn,
) -> dict:
    """Load DB Curator title overrides in source-path override format."""
    title_overrides = db.load_curator_title_overrides(conn)
    if not title_overrides:
        return {"movies": {}, "shows": {}, "anime": {}}

    entries = db.load_library_entry_files(conn)
    provider_names = _db_provider_source_names(conn)
    by_source = {"movies": {}, "shows": {}, "anime": {}}
    for thash, override in title_overrides.items():
        entry = entries.get(thash)
        if entry is None:
            continue
        torrent_name, files = entry
        kind = override.get("kind")
        if kind == "movie":
            category = "movies"
        elif kind == "show":
            category = "shows"
        elif kind == "anime":
            category = "anime"
        else:
            continue
        sources = {f"{category}/{torrent_name}"}
        for provider_name in provider_names.get(thash, ()):
            sources.add(f"{category}/{provider_name}")
        for file in files:
            file_path = str(file.get("path") or "").strip("/")
            if not file_path:
                continue
            first_part = Path(file_path).parts[0]
            sources.add(f"{category}/{first_part}")
        source_override = {
            key: value
            for key, value in override.items()
            if key != "kind"
        }
        for source in sources:
            by_source[category][source] = source_override
    return by_source


def _db_provider_source_names(conn) -> dict[str, set[str]]:
    """Return provider-backed root names keyed by torrent hash."""
    names: dict[str, set[str]] = {}
    rows = conn.execute("SELECT hash, info_json FROM provider_links").fetchall()
    for row in rows:
        thash = str(row["hash"] or "").strip().lower()
        if not thash:
            continue
        try:
            info = json.loads(row["info_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(info, dict):
            continue
        for key in ("display_name", "original_filename", "filename"):
            name = _clean_metadata_name(info.get(key))
            if name:
                names.setdefault(thash, set()).add(name)
    return names


def load_effective_overrides(conn) -> dict:
    """Load identity overrides from the DB cache."""
    return _db_override_source_paths(conn)


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


def entry_source_key(all_source_root: Path, path: Path) -> str:
    """Return the entry-level override key ``{category}/{torrent_name}``.

    Title overrides are scoped per entry (top-level folder), so a file's
    override is looked up by its category plus torrent root rather than its
    full source path.
    """
    parts = path.relative_to(all_source_root).parts
    return "/".join(parts[:2])


type CompanionIndex = dict[Path, tuple[Path, ...]]

HASH_RE = re.compile(r"^[a-fA-F0-9]{32,64}$")
PROVIDER_ID_PRIORITY = ("imdbid", "tmdbid", "tvdbid", "anidbid")


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
    if override.get("provider_ids"):
        entry["provider_ids"] = override["provider_ids"]


def apply_show_override(entry: dict, override: dict) -> None:
    """Apply override fields to a show entry in place."""
    if override.get("series"):
        entry["series"] = sanitize_path_component(override["series"])
    if "year" in override:
        entry["year"] = int(override["year"]) if override["year"] is not None else None
    if override.get("id"):
        entry["id"] = sanitize_path_component(override["id"])
    if override.get("provider_ids"):
        entry["provider_ids"] = override["provider_ids"]


def _coerce_int(value: object, default: int | None = None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def parse_with_override_regex(
    text: str, override: dict, kind: str
) -> dict | None:
    """Parse *text* with an entry-level named-group regex override."""
    pattern = str(override.get("parse_regex") or "").strip()
    if not pattern:
        return None
    try:
        match = re.search(pattern, text)
    except re.error:
        return None
    if not match:
        return None
    groups = {
        key: str(value).strip()
        for key, value in match.groupdict().items()
        if value is not None and str(value).strip()
    }
    if kind == "movie":
        title = groups.get("title") or groups.get("series")
        if not title:
            return None
        parsed: dict = {
            "title": sanitize_path_component(pretty_regex_title(title)),
        }
        if year := _coerce_int(groups.get("year")):
            parsed["year"] = year
        return parsed

    series = groups.get("series") or groups.get("title")
    episode = _coerce_int(groups.get("episode"))
    if not series or episode is None:
        return None
    parsed = {
        "series": sanitize_path_component(pretty_regex_title(series)),
        "season": _coerce_int(groups.get("season"), 1),
        "episode": episode,
    }
    if year := _coerce_int(groups.get("year")):
        parsed["year"] = year
    return parsed


def pretty_regex_title(value: str) -> str:
    """Return a readable title from a regex capture."""
    return re.sub(r"\s+", " ", value.replace(".", " ").replace("_", " ")).strip()


def parse_source_with_override_regex(
    path: Path,
    source_root: Path,
    override: dict,
    kind: str,
) -> dict | None:
    """Parse a file using identity/filename text, then filename alone."""
    pattern = str(override.get("parse_regex") or "").strip()
    rel = path.relative_to(source_root)
    raw_identity = rel.parts[0] if len(rel.parts) > 1 else path.parent.name
    if "/" not in pattern:
        return parse_with_override_regex(path.stem, override, kind)
    identities = [
        identity
        for identity in (
            _override_identity_name(override),
            raw_identity,
        )
        if identity
    ]
    for identity in dict.fromkeys(identities):
        parsed = parse_with_override_regex(
            f"{identity}/{path.stem}", override, kind
        )
        if parsed is not None:
            return parsed
    return parse_with_override_regex(path.stem, override, kind)


def _override_identity_name(override: dict) -> str:
    """Return the user-facing Curator identity name, if overridden."""
    return str(override.get("title") or override.get("series") or "").strip()


def _provider_id_suffix(entry: dict) -> str:
    provider_ids = entry.get("provider_ids")
    if not isinstance(provider_ids, dict):
        return sanitize_path_component(str(entry.get("id") or ""))
    for provider in PROVIDER_ID_PRIORITY:
        value = str(provider_ids.get(provider) or "").strip()
        if value:
            return sanitize_path_component(f"{provider}-{value}")
    return ""


def movie_folder_name(entry: dict) -> str:
    """Return the canonical folder name for a movie entry."""
    folder = f"{entry['title']} ({entry['year']})"
    if suffix := _provider_id_suffix(entry):
        folder = f"{folder} [{suffix}]"
    return sanitize_path_component(folder)


def show_series_name(entry: dict) -> str:
    """Return the canonical series name for a show entry."""
    series = entry["series"]
    if entry.get("year"):
        series = f"{series} ({int(entry['year'])})"
    if suffix := _provider_id_suffix(entry):
        series = f"{series} [{suffix}]"
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


def _sweep_orphaned_dirs(target_root: Path, mapping: list[dict]) -> None:
    """Remove real dirs in target_root that have no entries in mapping."""
    known: dict[str, set[str]] = {}
    for entry in mapping:
        target_parts = Path(entry["target"]).parts
        if len(target_parts) >= 2:
            category, title_dir = target_parts[0], target_parts[1]
            known.setdefault(category, set()).add(title_dir)

    for category_dir in target_root.iterdir():
        if not category_dir.is_dir() or category_dir.is_symlink():
            continue
        if category_dir.name.startswith(".curator-tmp-"):
            continue
        known_names = known.get(category_dir.name, set())
        for item in list(category_dir.iterdir()):
            if item.is_dir() and not item.is_symlink() and item.name not in known_names:
                shutil.rmtree(item)


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


def is_hash_name(value: str) -> bool:
    """Return True when *value* looks like a torrent info hash."""
    return bool(HASH_RE.fullmatch(value.strip()))


def _clean_metadata_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or is_hash_name(name):
        return ""
    return sanitize_path_component(name)


def _series_hint_from_name(name: str) -> str:
    """Return a best-effort show series name from provider metadata."""
    parsed = parse_show(Path(name).stem)
    if parsed is not None:
        return show_series_name(parsed)
    stem = Path(name).stem
    year = None
    year_matches = list(re.finditer(r"\b(19|20)\d{2}\b", stem))
    if year_matches:
        year_match = year_matches[-1]
        year = int(year_match.group(0))
        stem = stem[: year_match.start()]
    stem = re.sub(r"[\(\[\s-]+$", "", stem)
    stem = re.sub(r"\bseason\s+\d{1,2}\s*-\s*\d{1,2}\b.*$", " ", stem, flags=re.I)
    stem = re.sub(r"\bs\d{1,2}\s*-\s*s\d{1,2}\b.*$", " ", stem, flags=re.I)
    stem = re.sub(r"\[[^\]]+\]", " ", stem)
    stem = re.sub(r"\([^)]*\)", " ", stem)
    stem = re.sub(
        r"\b(complete|specials?|extras?|season)\b",
        " ",
        stem,
        flags=re.I,
    )
    stem = re.sub(r"\b(s\d{1,2}|series)\b.*$", " ", stem, flags=re.I)
    stem = re.sub(r"[._-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        return ""
    series = sanitize_path_component(stem)
    if year is not None:
        series = f"{series} ({year})"
    return series


def _split_series_hint(series_hint: str) -> tuple[str, int | None]:
    match = re.search(r"\s+\((19|20)\d{2}\)$", series_hint)
    if match is None:
        return series_hint, None
    return series_hint[: match.start()].strip(), int(match.group(0).strip(" ()"))


def load_torrent_name_hints(conn) -> dict[str, str]:
    """Load local torrent hash/name hints from buzz SQLite metadata."""
    hints: dict[str, str] = {}
    rows = conn.execute(
        "SELECT hash, name FROM library_entries WHERE name IS NOT NULL"
    ).fetchall()
    for row in rows:
        thash = str(row["hash"] or "").strip().lower()
        name = _clean_metadata_name(row["name"])
        if thash and name:
            hints[thash] = name

    rows = conn.execute(
        "SELECT hash, info_json FROM provider_links"
    ).fetchall()
    for row in rows:
        thash = str(row["hash"] or "").strip().lower()
        if not thash or thash in hints:
            continue
        try:
            info = json.loads(row["info_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(info, dict):
            continue
        name = _clean_metadata_name(
            info.get("original_filename") or info.get("filename")
        )
        if name:
            hints[thash] = name
    return hints


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
    """Record a Curator mapping diff event.

    The message summarizes the library size and what changed since the previous
    build so it is meaningful on its own in the startup log, e.g.
    ``Curator mapping updated: 1447 entries (+3 -0 ~1)`` or, when nothing moved,
    ``Curator mapping unchanged: 1447 entries``.
    """
    added = len(diff["added"])
    removed = len(diff["removed"])
    changed = len(diff["changed"])
    if added or removed or changed:
        message = (
            f"Curator mapping updated: {mapping_entries} entries "
            f"(+{added} -{removed} ~{changed})"
        )
    else:
        message = f"Curator mapping unchanged: {mapping_entries} entries"
    record_event(
        message,
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


def _changed_root_matches(path: str, changed_roots: list[str]) -> bool:
    return any(
        path == root or path.startswith(f"{root}/")
        for root in changed_roots
    )


def _scan_probe_pool(mapping: list[dict], changed_roots: list[str] | None) -> list[str]:
    roots = [root.strip("/") for root in changed_roots or [] if root.strip("/")]
    sources = []
    matched_sources = []
    for entry in mapping:
        source = entry.get("source")
        if not isinstance(source, str):
            continue
        sources.append(source)
        if not roots:
            continue
        target = entry.get("target")
        candidates = [source]
        if isinstance(target, str):
            candidates.append(target)
        if any(_changed_root_matches(path, roots) for path in candidates):
            matched_sources.append(source)
    if not roots:
        return sorted(dict.fromkeys(sources))
    if matched_sources:
        return sorted(dict.fromkeys(matched_sources))
    if sources:
        record_event(
            "scan probe changed roots matched no mapped files; "
            "probing full mapped library",
            level="warning",
            event="jellyfin_scan_probe_full_pool_fallback",
            changed_roots=roots,
            mapping_entries=len(mapping),
            source_pool_size=len(set(sources)),
        )
    return sorted(dict.fromkeys(sources))


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
    if not config.source_root.exists():
        raise FileNotFoundError(
            f"Source root does not exist: {config.source_root}"
        )

    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.target_root.mkdir(parents=True, exist_ok=True)

    conn = db.connect(config.state_dir / "buzz.sqlite")
    try:
        db.apply_migrations(conn)
        overrides = load_effective_overrides(conn)
        previous_mapping = load_previous_mapping(conn)
        torrent_name_hints = load_torrent_name_hints(conn)
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
            for definition in category_definitions(config.categories):
                category = definition["name"]
                kind = definition["kind"]
                source_root = config.source_root / category
                target_root = tmp_root / category
                category_overrides = overrides.get(category, {})
                if kind == "movie":
                    build_movies(
                        source_root,
                        target_root,
                        category_overrides,
                        mapping,
                        report,
                        config.source_root,
                    )
                elif kind == "show":
                    build_shows(
                        source_root,
                        target_root,
                        category_overrides,
                        mapping,
                        report,
                        config.source_root,
                        torrent_name_hints,
                        mapping_type="show",
                        report_key="show_files",
                    )
                else:
                    build_anime(
                        source_root,
                        target_root,
                        category_overrides,
                        mapping,
                        report,
                        config.source_root,
                    )

            if config.subtitles.enabled:
                apply_subtitle_overlay(tmp_root, config.subtitle_root, mapping)

            replace_root(tmp_root, config.target_root)
        except Exception:
            if tmp_root is not None and tmp_root.exists():
                shutil.rmtree(tmp_root, ignore_errors=True)
            raise

        _sweep_orphaned_dirs(config.target_root, mapping)
        report["mapping_entries"] = len(mapping)
        db.replace_curator_mapping(conn, mapping)
        db.save_curator_report(conn, report)

        log_mapping_event(
            mapping_diff(previous_mapping, mapping), report, len(mapping)
        )
        return report
    finally:
        conn.close()


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

    override = overrides.get(entry_source_key(all_source_root, path), {})
    parsed = (
        parse_source_with_override_regex(path, source_root, override, "movie")
        or parse_movie(path.stem, folder=folder)
    )

    parsed = parsed or {"title": "", "year": 0}
    apply_movie_override(parsed, override)
    if not parsed.get("title") or not parsed.get("year"):
        return _process_passthrough_file(
            path, source_root, target_root, all_source_root, used_targets,
            report, mapping, "movie", "movies", override,
        )

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


def _passthrough_target(
    path: Path,
    source_root: Path,
    target_root: Path,
    used_targets: set[str],
    override: dict | None = None,
    mapping_type: str = "",
) -> Path:
    rel = path.relative_to(source_root)
    group_name = rel.parts[0] if len(rel.parts) > 1 else path.parent.name
    folder = _passthrough_folder_name(group_name, override or {}, mapping_type)
    filename = path.name
    candidate = target_root / folder / filename
    if candidate.as_posix() not in used_targets:
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 2
    while True:
        candidate = target_root / folder / f"{stem} ({index}){suffix}"
        if candidate.as_posix() not in used_targets:
            return candidate
        index += 1


def _passthrough_folder_name(
    group_name: str, override: dict, mapping_type: str
) -> str:
    """Return the top-level folder for unparsed passthrough files."""
    del mapping_type
    name = str(override.get("title") or override.get("series") or "").strip()
    if name:
        folder = sanitize_path_component(name)
        if override.get("year") is not None:
            folder = f"{folder} ({int(override['year'])})"
        if suffix := _provider_id_suffix(override):
            folder = f"{folder} [{suffix}]"
        return sanitize_path_component(folder)
    sanitized = sanitize_path_component(group_name)
    if re.search(r"[\[\(]|[._-]\d{3,4}p\b|s\d{2}-s\d{2}", group_name, re.I):
        hint = _series_hint_from_name(group_name)
        if hint:
            return hint
    return sanitized or "unparsed"


def _process_passthrough_file(
    path: Path,
    source_root: Path,
    target_root: Path,
    all_source_root: Path,
    used_targets: set[str],
    report: dict,
    mapping: list[dict],
    mapping_type: str,
    report_key: str,
    override: dict | None = None,
) -> bool:
    rel_path = source_relpath(all_source_root, path)
    target_file = _passthrough_target(
        path, source_root, target_root, used_targets, override, mapping_type
    )
    ensure_symlink(path, target_file)
    used_targets.add(target_file.as_posix())
    mapping.append(
        {
            "source": rel_path,
            "target": target_file.relative_to(target_root.parent).as_posix(),
            "type": mapping_type,
        }
    )
    report[report_key] += 1
    return True


def _plan_show_group(
    files: list[Path],
    source_root: Path,
    target_root: Path,
    all_source_root: Path,
    overrides: dict,
    global_targets: set[str],
    series_hint: str = "",
    mapping_type: str = "show",
) -> tuple[list[dict], list[dict]]:
    planned = []
    group_errors = []
    group_series = series_hint or None
    hint_series, hint_year = _split_series_hint(series_hint)
    used_targets: set[str] = set()
    for path in sorted(files):
        rel_path = source_relpath(all_source_root, path)
        override = overrides.get(entry_source_key(all_source_root, path), {})
        parsed = (
            parse_source_with_override_regex(
                path, source_root, override, mapping_type
            )
            or parse_show(path.stem)
        )
        if parsed is None:
            if not override and hint_series:
                hint_override: dict = {"series": hint_series}
                if hint_year is not None:
                    hint_override["year"] = hint_year
                effective_override = hint_override
            else:
                effective_override = override
            target_file = _passthrough_target(
                path,
                source_root,
                target_root,
                global_targets | used_targets,
                effective_override,
                mapping_type,
            )
            used_targets.add(target_file.as_posix())
            planned.append(
                {
                    "path": path,
                    "rel_path": rel_path,
                    "target_file": target_file,
                    "base_name": target_file.stem,
                }
            )
            continue

        parsed = parsed or {"series": "", "season": 0, "episode": 0}
        apply_show_override(parsed, override)
        if series_hint and not override.get("series"):
            parsed["series"] = hint_series
            if hint_year is not None:
                parsed["year"] = hint_year
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
        group_series = group_series or series_name
        if group_series != series_name:
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
    mapping_type: str = "show",
    report_key: str = "show_files",
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
                "type": mapping_type,
            }
        )
        report[report_key] += 1
        for companion in find_companion_files(path, companion_index):
            extra = companion.name[len(path.stem) :]
            companion_target = target_file.parent / f"{base_name}{extra}"
            ensure_symlink(companion, companion_target)


def _series_hint_for_group(
    group_name: str, torrent_name_hints: dict[str, str]
) -> str:
    """Return a provider-backed series hint for hash-named show roots."""
    if not is_hash_name(group_name):
        return ""
    hint = torrent_name_hints.get(group_name.lower(), "")
    if not hint:
        return ""
    series = _series_hint_from_name(hint)
    if series:
        record_event(
            "resolved hash-named show root from local metadata: "
            f"{group_name} -> {series}",
            event="curator_hash_show_root_resolved",
            hash=group_name.lower(),
            series=series,
        )
    return series


def build_shows(
    source_root: Path,
    target_root: Path,
    overrides: dict,
    mapping: list[dict],
    report: dict,
    all_source_root: Path,
    torrent_name_hints: dict[str, str] | None = None,
    mapping_type: str = "show",
    report_key: str = "show_files",
    error_key: str = "skipped_shows",
) -> None:
    """Symlink show files into canonical series/season structures."""
    torrent_name_hints = torrent_name_hints or {}
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
            _series_hint_for_group(group_name, torrent_name_hints),
            mapping_type,
        )
        if group_errors:
            report[error_key].append(
                {"group": group_name, "errors": group_errors}
            )
        if not planned:
            continue
        _apply_show_planned(
            planned,
            target_root,
            global_targets,
            mapping,
            report,
            companion_index,
            mapping_type,
            report_key,
        )


def build_anime(
    source_root: Path,
    target_root: Path,
    overrides: dict,
    mapping: list[dict],
    report: dict,
    all_source_root: Path,
) -> None:
    """Symlink anime files into show-style series/season structures."""
    build_shows(
        source_root,
        target_root,
        overrides,
        mapping,
        report,
        all_source_root,
        mapping_type="anime",
        report_key="anime_files",
        error_key="skipped_shows",
    )


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
        msg = f"jellyfin scan skipped: media probe failed: {exc}"
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
        if not self.lock.acquire(blocking=False):
            raise RebuildError("aborted: rebuild already in progress", {})
        try:
            return rebuild_and_trigger(self.config, changed_roots)
        finally:
            self.lock.release()
