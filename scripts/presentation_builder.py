#!/usr/bin/env python3

import json
import os
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request


VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
    ".wmv",
}
SIDECAR_EXTENSIONS = {
    ".ass",
    ".idx",
    ".nfo",
    ".smi",
    ".srt",
    ".ssa",
    ".sub",
    ".sup",
    ".txt",
    ".vtt",
}
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
SHOW_PATTERNS = (
    re.compile(r"(?i)\bS(?P<season>\d{1,2})E(?P<episode>\d{1,2})\b"),
    re.compile(r"(?i)\b(?P<season>\d{1,2})x(?P<episode>\d{1,2})\b"),
)
NOISE_RE = re.compile(
    r"(?i)\b("
    r"1080p|2160p|720p|480p|4k|bluray|brrip|bdrip|dvdrip|dvd|webrip|web[- ]?dl|"
    r"hdr|hdr10|remux|proper|repack|extended|unrated|criterion|x264|x265|h\.?264|"
    r"h\.?265|hevc|av1|aac|ac3|dts|truehd|atmos|yts|rarbg|amzn|nf|dsnp|hmax"
    r")\b"
)


@dataclass
class Config:
    bind: str
    port: int
    source_root: Path
    target_root: Path
    state_root: Path
    overrides_path: Path
    jellyfin_url: str
    jellyfin_api_key: str
    jellyfin_scan_task_id: str
    skip_jellyfin_scan: bool
    build_on_start: bool


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def load_config() -> Config:
    return Config(
        bind=os.environ.get("PRESENTATION_BIND", "0.0.0.0"),
        port=int(os.environ.get("PRESENTATION_PORT", "8400")),
        source_root=env_path("PRESENTATION_SOURCE_ROOT", "/mnt/zurg"),
        target_root=env_path("PRESENTATION_TARGET_ROOT", "/mnt/jellyfin-library"),
        state_root=env_path("PRESENTATION_STATE_ROOT", "/state"),
        overrides_path=env_path("PRESENTATION_OVERRIDES", "/config/overrides.yml"),
        jellyfin_url=os.environ.get("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/"),
        jellyfin_api_key=os.environ.get("JELLYFIN_API_KEY", ""),
        jellyfin_scan_task_id=os.environ.get("JELLYFIN_SCAN_TASK_ID", ""),
        skip_jellyfin_scan=os.environ.get("PRESENTATION_SKIP_JELLYFIN_SCAN", "").lower() in {"1", "true", "yes"},
        build_on_start=os.environ.get("PRESENTATION_BUILD_ON_START", "true").lower() in {"1", "true", "yes"},
    )


def canonical_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .-_")


def pretty_title(raw: str) -> str:
    cleaned = canonical_spaces(raw.replace(".", " ").replace("_", " "))
    words = []
    for word in cleaned.split():
        if word.isupper() and len(word) <= 4:
            words.append(word)
        elif re.fullmatch(r"[ivxlcdm]+", word, re.IGNORECASE):
            words.append(word.upper())
        else:
            words.append(word.capitalize())
    return " ".join(words)


def sanitize_path_component(value: str) -> str:
    value = value.replace("/", " ").replace("\\", " ")
    return canonical_spaces(value)


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_sidecar(path: Path) -> bool:
    return path.suffix.lower() in SIDECAR_EXTENSIONS


def iter_files(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def source_relpath(source_root: Path, path: Path) -> str:
    return path.relative_to(source_root).as_posix()


def load_overrides(path: Path) -> dict:
    if not path.exists():
        return {"movies": {}, "shows": {}}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {"movies": {}, "shows": {}}
    overrides = json.loads(raw)
    if not isinstance(overrides, dict):
        raise ValueError("Overrides file must contain a top-level object.")
    overrides.setdefault("movies", {})
    overrides.setdefault("shows", {})
    return overrides


def parse_movie(stem: str):
    if re.search(r"(?i)\bS\d{1,2}E\d{1,2}\b", stem):
        return None
    cleaned = canonical_spaces(stem.replace(".", " ").replace("_", " ").replace("-", " "))
    match = YEAR_RE.search(cleaned)
    if not match:
        return None
    title_part = cleaned[: match.start()]
    title_part = NOISE_RE.sub("", title_part)
    title = sanitize_path_component(pretty_title(title_part))
    if not title:
        return None
    return {"title": title, "year": int(match.group(1))}


def parse_show(stem: str):
    cleaned = canonical_spaces(stem.replace(".", " ").replace("_", " ").replace("-", " "))
    for pattern in SHOW_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue
        series = cleaned[: match.start()]
        series = NOISE_RE.sub("", series)
        series = sanitize_path_component(pretty_title(series))
        if not series:
            return None
        return {
            "series": series,
            "season": int(match.group("season")),
            "episode": int(match.group("episode")),
        }
    return None


def find_companion_files(path: Path):
    parent = path.parent
    stem = path.stem
    companions = []
    for sibling in parent.iterdir():
        if not sibling.is_file() or sibling == path:
            continue
        if not is_sidecar(sibling):
            continue
        if sibling.name == f"{stem}{sibling.suffix}" or sibling.name.startswith(f"{stem}."):
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
    backup_root = target_root.parent / f".{target_root.name}.backup"
    if backup_root.exists():
        shutil.rmtree(backup_root)
    if target_root.exists():
        target_root.rename(backup_root)
    tmp_root.rename(target_root)
    if backup_root.exists():
        shutil.rmtree(backup_root)


def build_library(config: Config):
    overrides = load_overrides(config.overrides_path)
    movies_source = config.source_root / "movies"
    shows_source = config.source_root / "shows"
    anime_source = config.source_root / "anime"

    if not config.source_root.exists():
        raise FileNotFoundError(f"Source root does not exist: {config.source_root}")

    config.state_root.mkdir(parents=True, exist_ok=True)
    target_parent = config.target_root.parent
    target_parent.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix=".jellyfin-library-", dir=target_parent))
    mapping = []
    report = {
        "skipped_movies": [],
        "skipped_shows": [],
        "anime_files": 0,
        "movies": 0,
        "show_files": 0,
    }

    try:
        build_movies(movies_source, tmp_root / "movies", overrides.get("movies", {}), mapping, report, config.source_root)
        build_shows(shows_source, tmp_root / "shows", overrides.get("shows", {}), mapping, report, config.source_root)
        build_anime(anime_source, tmp_root / "animes", mapping, report, config.source_root)
        replace_root(tmp_root, config.target_root)
    except Exception:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise

    report["mapping_entries"] = len(mapping)
    (config.state_root / "mapping.json").write_text(json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8")
    (config.state_root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def build_movies(source_root: Path, target_root: Path, overrides: dict, mapping: list, report: dict, all_source_root: Path):
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return
    used_targets = set()
    for path in iter_files(source_root):
        if not is_video(path):
            continue
        rel_path = source_relpath(all_source_root, path)
        parsed = parse_movie(path.stem)
        override = overrides.get(rel_path, {})
        if parsed is None and not override:
            report["skipped_movies"].append({"source": rel_path, "reason": "unable to parse movie title/year"})
            continue
        if parsed is None:
            parsed = {"title": "", "year": 0}
        apply_movie_override(parsed, override)
        if not parsed.get("title") or not parsed.get("year"):
            report["skipped_movies"].append({"source": rel_path, "reason": "movie override missing title/year"})
            continue
        folder_name = movie_folder_name(parsed)
        target_file = target_root / folder_name / f"{folder_name}{path.suffix.lower()}"
        target_key = target_file.as_posix()
        if target_key in used_targets:
            report["skipped_movies"].append({"source": rel_path, "reason": "duplicate canonical movie target"})
            continue
        ensure_symlink(path, target_file)
        used_targets.add(target_key)
        mapping.append({"source": rel_path, "target": target_file.relative_to(target_root.parent).as_posix(), "type": "movie"})
        report["movies"] += 1

        for companion in find_companion_files(path):
            extra = companion.name[len(path.stem) :]
            companion_target = target_root / folder_name / f"{folder_name}{extra}"
            ensure_symlink(companion, companion_target)


def build_shows(source_root: Path, target_root: Path, overrides: dict, mapping: list, report: dict, all_source_root: Path):
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return
    grouped = {}
    global_targets = set()
    for path in iter_files(source_root):
        if not is_video(path):
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
                group_errors.append({"source": rel_path, "reason": "unable to parse show season/episode"})
                continue
            if parsed is None:
                parsed = {"series": "", "season": 0, "episode": 0}
            apply_show_override(parsed, override)
            if not parsed.get("series") or not parsed.get("season") or not parsed.get("episode"):
                group_errors.append({"source": rel_path, "reason": "show override missing series/season/episode"})
                continue
            if group_series is None:
                group_series = show_series_name(parsed)
            elif group_series != show_series_name(parsed):
                group_errors.append({"source": rel_path, "reason": "inconsistent parsed show name within torrent"})
                continue
            season_dir = f"Season {int(parsed['season']):02d}"
            base_name = f"{show_series_name(parsed)} S{int(parsed['season']):02d}E{int(parsed['episode']):02d}"
            target_file = target_root / show_series_name(parsed) / season_dir / f"{base_name}{path.suffix.lower()}"
            target_key = target_file.as_posix()
            if target_key in used_targets or target_key in global_targets:
                group_errors.append({"source": rel_path, "reason": "duplicate season/episode target"})
                continue
            used_targets.add(target_key)
            planned.append((path, rel_path, target_file))

        if group_errors:
            report["skipped_shows"].append({"group": group_name, "errors": group_errors})
            continue

        for path, rel_path, target_file in planned:
            ensure_symlink(path, target_file)
            global_targets.add(target_file.as_posix())
            mapping.append({"source": rel_path, "target": target_file.relative_to(target_root.parent).as_posix(), "type": "show"})
            report["show_files"] += 1
            base_name = target_file.stem
            for companion in find_companion_files(path):
                extra = companion.name[len(path.stem) :]
                companion_target = target_file.parent / f"{base_name}{extra}"
                ensure_symlink(companion, companion_target)


def build_anime(source_root: Path, target_root: Path, mapping: list, report: dict, all_source_root: Path):
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return
    for path in iter_files(source_root):
        rel_path = source_relpath(all_source_root, path)
        target_file = target_root / path.relative_to(source_root)
        ensure_symlink(path, target_file)
        mapping.append({"source": rel_path, "target": target_file.relative_to(target_root.parent).as_posix(), "type": "anime"})
        report["anime_files"] += 1


def discover_scan_task_id(config: Config) -> str:
    if config.jellyfin_scan_task_id:
        return config.jellyfin_scan_task_id
    if not config.jellyfin_api_key:
        raise RuntimeError("JELLYFIN_API_KEY is required to trigger a Jellyfin scan.")
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


def trigger_jellyfin_scan(config: Config):
    task_id = discover_scan_task_id(config)
    req = request.Request(
        f"{config.jellyfin_url}/ScheduledTasks/Running/{task_id}",
        method="POST",
        headers={"Authorization": f"MediaBrowser Token={config.jellyfin_api_key}"},
    )
    with request.urlopen(req, timeout=30):
        return


def rebuild_and_trigger(config: Config):
    report = build_library(config)
    if config.skip_jellyfin_scan:
        report["jellyfin_scan_triggered"] = False
        return report
    trigger_jellyfin_scan(config)
    report["jellyfin_scan_triggered"] = True
    return report


class App:
    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.Lock()

    def handle_rebuild(self):
        with self.lock:
            return rebuild_and_trigger(self.config)


class Handler(BaseHTTPRequestHandler):
    app = None

    def do_GET(self):
        if self.path == "/healthz":
            self.respond(HTTPStatus.OK, {"status": "ok"})
            return
        self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if self.path != "/rebuild":
            self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        try:
            report = self.app.handle_rebuild()
        except Exception as exc:
            self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self.respond(HTTPStatus.OK, report)

    def log_message(self, format, *args):
        sys.stdout.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def respond(self, status: HTTPStatus, payload: dict):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(config: Config):
    app = App(config)
    Handler.app = app
    if config.build_on_start:
        try:
            startup_report = build_library(config)
            print(
                "initial presentation build complete: "
                f"{startup_report['movies']} movies, "
                f"{startup_report['show_files']} show files, "
                f"{startup_report['anime_files']} anime files",
                flush=True,
            )
        except Exception as exc:
            print(f"initial presentation build failed: {exc}", file=sys.stderr, flush=True)
    server = ThreadingHTTPServer((config.bind, config.port), Handler)
    print(f"presentation-builder listening on {config.bind}:{config.port}", flush=True)
    server.serve_forever()


def main():
    config = load_config()
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        report = rebuild_and_trigger(config)
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    run_server(config)


if __name__ == "__main__":
    main()
