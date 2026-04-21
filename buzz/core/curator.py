import json
import os
import shutil
import tempfile
import threading
from typing import Optional
from pathlib import Path
from urllib import error, request
import yaml

from ..models import PresentationConfig
from .constants import (
    NOISE_RE,
    SHOW_PATTERNS,
    SIDECAR_EXTENSIONS,
    VIDEO_EXTENSIONS,
    YEAR_RE,
)
from .events import record_event
from .media_server import (
    discover_jellyfin_libraries,
    trigger_jellyfin_scan,
    trigger_jellyfin_selective_refresh,
    validate_jellyfin_auth,
)
from .subtitles import apply_subtitle_overlay, background_fetch_subtitles
from .state import is_internal_category
from .media import (
    is_sidecar_file,
    is_video_file,
    parse_movie,
    parse_show,
)
from .utils import (
    canonical_spaces,
    pretty_title,
    sanitize_path_component,
)


class RebuildError(RuntimeError):
    def __init__(self, message: str, payload: dict):
        super().__init__(message)
        self.payload = payload


def load_overrides(path: Path) -> dict:
    if not path.exists():
        return {"movies": {}, "shows": {}}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {"movies": {}, "shows": {}}
    overrides = yaml.safe_load(raw) or {}
    overrides.setdefault("movies", {})
    overrides.setdefault("shows", {})
    return overrides


def iter_files(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def source_relpath(source_root: Path, path: Path) -> str:
    return path.relative_to(source_root).as_posix()


def find_companion_files(path: Path):
    parent = path.parent
    stem = path.stem
    companions = []
    for sibling in parent.iterdir():
        if not sibling.is_file() or sibling == path:
            continue
        if not is_sidecar_file(sibling):
            continue
        if sibling.name == f"{stem}{sibling.suffix}" or sibling.name.startswith(
            f"{stem}."
        ):
            companions.append(sibling)
    return sorted(companions)


def ensure_symlink(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(source, target)


def apply_movie_override(entry: dict, override: dict):
    if override.get("title"):
        entry["title"] = sanitize_path_component(override["title"])
    if override.get("year"):
        entry["year"] = int(override["year"])
    if override.get("id"):
        entry["id"] = sanitize_path_component(override["id"])


def apply_show_override(entry: dict, override: dict):
    if override.get("series"):
        entry["series"] = sanitize_path_component(override["series"])
    if override.get("season") is not None:
        entry["season"] = int(override["season"])
    if override.get("episode") is not None:
        entry["episode"] = int(override["episode"])
    if override.get("id"):
        entry["id"] = sanitize_path_component(override["id"])


def movie_folder_name(entry: dict) -> str:
    folder = f"{entry['title']} ({entry['year']})"
    if entry.get("id"):
        folder = f"{folder} [{entry['id']}]"
    return sanitize_path_component(folder)


def show_series_name(entry: dict) -> str:
    series = entry["series"]
    if entry.get("id"):
        series = f"{series} [{entry['id']}]"
    return sanitize_path_component(series)


def replace_root(tmp_root: Path, target_root: Path):
    """
    Swaps the contents of target_root with those in tmp_root.
    Operates on contents to avoid needing write permissions on target_root's parent.
    """
    # 1. Remove existing contents (except the tmp_root itself)
    for item in target_root.iterdir():
        if item.is_dir() and item.name.startswith(".curator-tmp-"):
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # 2. Move new contents in
    for item in tmp_root.iterdir():
        shutil.move(str(item), str(target_root / item.name))


def load_previous_mapping(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    payload = json.loads(raw)
    return payload if isinstance(payload, list) else []


def mapping_index(entries: list[dict]) -> dict[str, dict]:
    indexed = {}
    for entry in entries:
        target = entry.get("target")
        if isinstance(target, str):
            indexed[target] = entry
    return indexed


def mapping_diff(previous: list[dict], current: list[dict]) -> dict:
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
                {"before": previous_index[target], "after": current_index[target]}
            )

    return {"added": added, "removed": removed, "changed": changed}


def log_mapping_event(diff: dict, report: dict, mapping_entries: int):
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


def build_library(config: PresentationConfig):
    overrides = load_overrides(config.overrides_path)
    movies_source = config.source_root / "movies"
    shows_source = config.source_root / "shows"
    anime_source = config.source_root / "anime"

    if not config.source_root.exists():
        raise FileNotFoundError(f"Source root does not exist: {config.source_root}")

    config.state_root.mkdir(parents=True, exist_ok=True)
    config.target_root.mkdir(parents=True, exist_ok=True)

    mapping_path = config.state_root / "mapping.json"
    previous_mapping = load_previous_mapping(mapping_path)
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
            anime_source, tmp_root / "animes", mapping, report, config.source_root
        )
        
        if config.subtitles.enabled:
            apply_subtitle_overlay(tmp_root, config.subtitle_root)
            
        replace_root(tmp_root, config.target_root)
    except Exception:
        if tmp_root is not None and tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
        raise

    report["mapping_entries"] = len(mapping)
    mapping_path.write_text(
        json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8"
    )
    (config.state_root / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    
    log_mapping_event(mapping_diff(previous_mapping, mapping), report, len(mapping))
    return report


def build_movies(
    source_root: Path,
    target_root: Path,
    overrides: dict,
    mapping: list,
    report: dict,
    all_source_root: Path,
):
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return
    used_targets = set()
    for path in iter_files(source_root):
        if not is_video_file(path):
            continue
        rel_path = source_relpath(all_source_root, path)
        
        # Determine torrent folder name if file is in a subdirectory
        source_rel = path.relative_to(source_root)
        folder = source_rel.parts[0] if len(source_rel.parts) > 1 else ""
        
        parsed = parse_movie(path.stem, folder=folder)
        override = overrides.get(rel_path, {})
        if parsed is None and not override:
            report["skipped_movies"].append(
                {"source": rel_path, "reason": "unable to parse movie title/year"}
            )
            continue
        if parsed is None:
            parsed = {"title": "", "year": 0}
        apply_movie_override(parsed, override)
        if not parsed.get("title") or not parsed.get("year"):
            report["skipped_movies"].append(
                {"source": rel_path, "reason": "movie override missing title/year"}
            )
            continue
        folder_name = movie_folder_name(parsed)
        target_file = target_root / folder_name / f"{folder_name}{path.suffix.lower()}"
        target_key = target_file.as_posix()
        if target_key in used_targets:
            report["skipped_movies"].append(
                {"source": rel_path, "reason": "duplicate canonical movie target"}
            )
            continue
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

        for companion in find_companion_files(path):
            extra = companion.name[len(path.stem) :]
            companion_target = target_root / folder_name / f"{folder_name}{extra}"
            ensure_symlink(companion, companion_target)


def build_shows(
    source_root: Path,
    target_root: Path,
    overrides: dict,
    mapping: list,
    report: dict,
    all_source_root: Path,
):
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return
    grouped = {}
    global_targets = set()
    for path in iter_files(source_root):
        if not is_video_file(path):
            continue
        rel = path.relative_to(source_root)
        group_key = rel.parts[0] if len(rel.parts) > 1 else path.stem
        grouped.setdefault(group_key, []).append(path)

    for group_name, files in sorted(grouped.items()):
        planned = []
        group_errors = []
        group_series = None
        used_targets = set()
        for path in sorted(files):
            rel_path = source_relpath(all_source_root, path)
            parsed = parse_show(path.stem)
            override = overrides.get(rel_path, {})
            if parsed is None and not override:
                group_errors.append(
                    {
                        "source": rel_path,
                        "reason": "unable to parse show season/episode",
                    }
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
            if group_series is None:
                group_series = show_series_name(parsed)
            elif group_series != show_series_name(parsed):
                group_errors.append(
                    {
                        "source": rel_path,
                        "reason": "inconsistent parsed show name within torrent",
                    }
                )
                continue
            season_dir = f"Season {int(parsed['season']):02d}"
            base_name = f"{show_series_name(parsed)} S{int(parsed['season']):02d}E{int(parsed['episode']):02d}"
            target_file = (
                target_root
                / show_series_name(parsed)
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
            planned.append((path, rel_path, target_file))

        if group_errors:
            report["skipped_shows"].append(
                {"group": group_name, "errors": group_errors}
            )
            continue

        for path, rel_path, target_file in planned:
            ensure_symlink(path, target_file)
            global_targets.add(target_file.as_posix())
            mapping.append(
                {
                    "source": rel_path,
                    "target": target_file.relative_to(target_root.parent).as_posix(),
                    "type": "show",
                }
            )
            report["show_files"] += 1
            base_name = target_file.stem
            for companion in find_companion_files(path):
                extra = companion.name[len(path.stem) :]
                companion_target = target_file.parent / f"{base_name}{extra}"
                ensure_symlink(companion, companion_target)


def build_anime(
    source_root: Path,
    target_root: Path,
    mapping: list,
    report: dict,
    all_source_root: Path,
):
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
                "target": target_file.relative_to(target_root.parent).as_posix(),
                "type": "anime",
            }
        )
        report["anime_files"] += 1


def rebuild_and_trigger(config: PresentationConfig, changed_roots: Optional[list[str]] = None):
    report = build_library(config)
    if config.skip_jellyfin_scan:
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "skipped_configured"
        report["jellyfin_scan_error"] = None
        return report
    if not config.jellyfin_api_key:
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "skipped_missing_auth"
        report["jellyfin_scan_error"] = None
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
        # We don't raise RebuildError here anymore to ensure the curator process
        # doesn't think the whole rebuild failed just because the scan trigger failed.
        # The symlinks (build_library) were already successfully swapped.
        record_event(f"Jellyfin scan trigger failed: {exc}", level="error")
    else:
        report["jellyfin_scan_triggered"] = True
        report["jellyfin_scan_error"] = None
    
    if config.subtitles.enabled and config.subtitles.fetch_on_resync:
        background_fetch_subtitles(config)
        
    return report


class Curator:
    def __init__(self, config: PresentationConfig):
        self.config = config
        self.lock = threading.Lock()

    def handle_rebuild(self, changed_roots: Optional[list[str]] = None):
        with self.lock:
            return rebuild_and_trigger(self.config, changed_roots)

    def cleanup(self):
        with self.lock:
            shutil.rmtree(self.config.target_root, ignore_errors=True)
