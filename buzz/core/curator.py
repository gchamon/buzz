import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from urllib import error, request
from pydantic import BaseModel, Field
import yaml

from .constants import (
    NOISE_RE,
    SHOW_PATTERNS,
    SIDECAR_EXTENSIONS,
    VIDEO_EXTENSIONS,
    YEAR_RE,
)
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


class PresentationConfig(BaseModel):
    bind: str = Field(
        default_factory=lambda: os.environ.get("PRESENTATION_BIND", "0.0.0.0")
    )
    port: int = Field(
        default_factory=lambda: int(os.environ.get("PRESENTATION_PORT", "8400"))
    )
    source_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PRESENTATION_SOURCE_ROOT", "/mnt/buzz")
        )
    )
    target_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PRESENTATION_TARGET_ROOT", "/mnt/jellyfin-library")
        )
    )
    state_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PRESENTATION_STATE_ROOT", "/state")
        )
    )
    overrides_path: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PRESENTATION_OVERRIDES", "/config/overrides.yml")
        )
    )
    jellyfin_url: str = Field(
        default_factory=lambda: os.environ.get(
            "JELLYFIN_URL", "http://jellyfin:8096"
        ).rstrip("/")
    )
    jellyfin_api_key: str = Field(
        default_factory=lambda: os.environ.get("JELLYFIN_API_KEY", "")
    )
    jellyfin_scan_task_id: str = Field(
        default_factory=lambda: os.environ.get("JELLYFIN_SCAN_TASK_ID", "")
    )
    skip_jellyfin_scan: bool = Field(
        default_factory=lambda: (
            os.environ.get("PRESENTATION_SKIP_JELLYFIN_SCAN", "").lower()
            in {"1", "true", "yes"}
        )
    )
    build_on_start: bool = Field(
        default_factory=lambda: (
            os.environ.get("PRESENTATION_BUILD_ON_START", "true").lower()
            in {"1", "true", "yes"}
        )
    )
    verbose: bool = Field(
        default_factory=lambda: (
            os.environ.get("PRESENTATION_VERBOSE", "").lower() in {"1", "true", "yes"}
        )
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
    print(
        json.dumps(
            {
                "event": "curator_mapping_diff",
                "mapping_entries": mapping_entries,
                "movies": report["movies"],
                "show_files": report["show_files"],
                "anime_files": report["anime_files"],
                "added": diff["added"],
                "removed": diff["removed"],
                "changed": diff["changed"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
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

    try:
        with tempfile.TemporaryDirectory(
            prefix=".curator-tmp-", dir=config.target_root
        ) as tmp_dir:
            tmp_root = Path(tmp_dir)
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
            replace_root(tmp_root, config.target_root)
    except Exception:
        raise

    report["mapping_entries"] = len(mapping)
    mapping_path.write_text(
        json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8"
    )
    (config.state_root / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    if config.verbose:
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
        parsed = parse_movie(path.stem)
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


def discover_scan_task_id(config: PresentationConfig) -> str:
    if config.jellyfin_scan_task_id:
        return config.jellyfin_scan_task_id
    req = request.Request(
        f"{config.jellyfin_url}/ScheduledTasks?IsHidden=false&IsEnabled=true",
        headers={"Authorization": f"MediaBrowser Token={config.jellyfin_api_key}"},
    )
    with request.urlopen(req, timeout=30) as response:
        tasks = json.load(response)
    for task in tasks:
        if task.get("Name") == "Scan Media Library":
            return task.get("Id", "")
    raise RuntimeError("Unable to find the Jellyfin Scan Media Library task ID.")


def trigger_jellyfin_scan(config: PresentationConfig):
    task_id = discover_scan_task_id(config)
    req = request.Request(
        f"{config.jellyfin_url}/ScheduledTasks/Running/{task_id}",
        method="POST",
        headers={"Authorization": f"MediaBrowser Token={config.jellyfin_api_key}"},
    )
    with request.urlopen(req, timeout=30):
        return


def rebuild_and_trigger(config: PresentationConfig):
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
    try:
        trigger_jellyfin_scan(config)
    except Exception as exc:
        report["jellyfin_scan_triggered"] = False
        report["jellyfin_scan_status"] = "failed"
        report["jellyfin_scan_error"] = str(exc)
        raise RebuildError(str(exc), report) from exc
    report["jellyfin_scan_triggered"] = True
    report["jellyfin_scan_status"] = "triggered"
    report["jellyfin_scan_error"] = None
    return report


class Curator:
    def __init__(self, config: PresentationConfig):
        self.config = config
        self.lock = threading.Lock()

    def handle_rebuild(self):
        with self.lock:
            return rebuild_and_trigger(self.config)

    def cleanup(self):
        with self.lock:
            shutil.rmtree(self.config.target_root, ignore_errors=True)
