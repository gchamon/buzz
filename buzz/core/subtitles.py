"""Subtitle search, download, and overlay management for Buzz."""

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from ..models import CuratorConfig, SubtitleConfig, SubtitleFilters
from . import db
from .events import record_event
from .media import VIDEO_EXTENSIONS, parse_show
from .media_server import trigger_jellyfin_selective_refresh
from .state import raise_if_cancelled


class SubtitleState:
    """Thread-safe state tracker for background subtitle operations."""

    def __init__(self) -> None:
        """Initialize state with idle defaults."""
        self.is_running = False
        self.last_run_at = None
        self.error_count = 0
        self.current_file = None
        self.lock = threading.Lock()

    def start(self) -> None:
        """Mark a fetch as started and reset the error count."""
        with self.lock:
            self.is_running = True
            self.error_count = 0

    def stop(self, error: bool = False) -> None:
        """Mark a fetch as finished, optionally incrementing the error count."""
        with self.lock:
            self.is_running = False
            self.last_run_at = time.time()
            if error:
                self.error_count += 1
            self.current_file = None

    def set_current(self, filename: str) -> None:
        """Update the filename currently being processed."""
        with self.lock:
            self.current_file = filename

    def status(self) -> dict:
        """Return a snapshot of current state as a plain dict."""
        with self.lock:
            return {
                "is_running": self.is_running,
                "last_run_at": self.last_run_at,
                "error_count": self.error_count,
                "current_file": self.current_file,
            }


state = SubtitleState()


def _tokenize(text: str) -> set[str]:
    """Split *text* into lowercase tokens."""
    return set(re.split(r"[\s._\-]+", text.lower()))


class OpenSubtitlesClient:
    """Client for the OpenSubtitles.com API v1."""

    BASE_URL = "https://api.opensubtitles.com/api/v1"

    def __init__(self, config: SubtitleConfig):
        """Initialize the client with subtitle configuration."""
        self.config = config
        self.api_key = config.api_key
        self.username = config.username
        self.password = config.password
        self.token = None
        self.client = httpx.Client(
            headers={
                "Api-Key": self.api_key,
                "User-Agent": "buzz/0.1",
            },
            timeout=30.0,
            follow_redirects=True,
        )

    def login(self) -> str:
        """Authenticate and return a bearer token."""
        if self.token:
            return self.token

        if not self.username or not self.password:
            raise ValueError(
                "OpenSubtitles username/password required for downloads"
            )

        record_event("logging in to OpenSubtitles...", level="info")
        resp = self.client.post(
            f"{self.BASE_URL}/login",
            json={
                "username": self.username,
                "password": self.password,
            },
        )
        resp.raise_for_status()
        self.token = resp.json().get("token")
        self.client.headers["Authorization"] = f"Bearer {self.token}"
        return self.token

    def search(
        self,
        query: str,
        year: int | None = None,
        languages: str = "en",
        season: int | None = None,
        episode: int | None = None,
        type: str | None = None,
    ) -> list[dict]:
        """Search for subtitles matching the given criteria."""
        params = {
            "query": query,
            "languages": languages,
        }
        if year:
            params["year"] = str(year)
        if season:
            params["season_number"] = str(season)
        if episode:
            params["episode_number"] = str(episode)
        if type:
            params["type"] = type

        resp = self.client.get(
            f"{self.BASE_URL}/subtitles", params=params
        )

        # Rate limit check
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            record_event(
                "OpenSubtitles search rate limit reached",
                level="warning",
            )

        resp.raise_for_status()
        return resp.json().get("data", [])

    def download(self, file_id: int) -> str:
        """Request a download link for a subtitle file."""
        self.login()
        resp = self.client.post(
            f"{self.BASE_URL}/download",
            json={"file_id": file_id},
        )

        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            record_event(
                "OpenSubtitles download rate limit reached",
                level="warning",
            )

        resp.raise_for_status()
        return resp.json().get("link")

    def fetch_content(self, url: str) -> bytes:
        """Download subtitle content from a CDN URL."""
        resp = httpx.get(url)
        resp.raise_for_status()
        return resp.content

    def __enter__(self) -> OpenSubtitlesClient:
        """Enter context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Close the HTTP client on exit."""
        self.client.close()


def release_similarity(source_name: str, release_name: str) -> float:
    """Compute Jaccard similarity between two release names."""
    s_tokens = _tokenize(source_name)
    r_tokens = _tokenize(release_name)

    if not s_tokens or not r_tokens:
        return 0.0

    intersection = s_tokens.intersection(r_tokens)
    union = s_tokens.union(r_tokens)
    return len(intersection) / len(union)


def _apply_filters(
    results: list[dict], filters: SubtitleFilters
) -> list[dict]:
    """Filter subtitle results according to user preferences."""
    filtered = []
    for item in results:
        attr = item.get("attributes", {})

        if filters.hearing_impaired == "exclude" and attr.get(
            "hearing_impaired"
        ):
            continue

        # OpenSubtitles v2 doesn't have a direct ai_translated/
        # machine_translated boolean in attributes sometimes,
        # but it can be in features or tags. We'll check common fields.
        if filters.exclude_ai and attr.get("ai_translated"):
            continue

        if filters.exclude_machine and attr.get("machine_translated"):
            continue

        filtered.append(item)
    return filtered


def _result_matches_query(
    result: dict, query: str, year: int | None = None
) -> bool:
    """Check if a search result actually belongs to the queried movie/show."""
    attr = result.get("attributes", {})
    feature = attr.get("feature_details", {})

    # If feature_details has a title, check similarity with query
    feature_title = feature.get("title") or feature.get("movie_name") or ""
    if feature_title:
        # Normalize and compare
        query_tokens = _tokenize(query)
        title_tokens = _tokenize(feature_title)

        if query_tokens and title_tokens:
            overlap = len(query_tokens & title_tokens) / len(query_tokens)
            if overlap < 0.5:
                return False

    # If we searched with a year, validate the result's year matches (±1).
    if year and feature.get("year"):
        return abs(feature["year"] - year) <= 1

    return True


def rank_subtitles(
    results: list[dict],
    strategy: str,
    filters: SubtitleFilters,
    source_filename: str,
    query: str = "",
    year: int | None = None,
) -> dict | None:
    """Rank filtered subtitles using the chosen strategy."""
    # Filter results that don't match the queried movie/show first
    results = [
        r for r in results if _result_matches_query(r, query, year)
    ]

    filtered = _apply_filters(results, filters)
    if not filtered:
        return None

    # Ranking logic
    if strategy == "best-match":
        ranked = sorted(
            filtered,
            key=lambda x: release_similarity(
                source_filename,
                x.get("attributes", {}).get("release", ""),
            ),
            reverse=True,
        )
    elif strategy == "most-downloaded":
        ranked = sorted(
            filtered,
            key=lambda x: (
                x.get("attributes", {}).get("download_count", 0)
                + x.get("attributes", {}).get("new_download_count", 0)
            ),
            reverse=True,
        )
    elif strategy == "best-rated":
        # Filter items with at least one vote
        rated = [
            x for x in filtered
            if x.get("attributes", {}).get("votes", 0) > 0
        ]
        if not rated:
            return None
        ranked = sorted(
            rated,
            key=lambda x: (
                x.get("attributes", {}).get("ratings", 0.0),
                x.get("attributes", {}).get("download_count", 0),
            ),
            reverse=True,
        )
    elif strategy == "trusted":
        ranked = sorted(
            filtered,
            key=lambda x: (
                x.get("attributes", {}).get("from_trusted", False),
                x.get("attributes", {}).get("download_count", 0),
            ),
            reverse=True,
        )
    elif strategy == "latest":
        ranked = sorted(
            filtered,
            key=lambda x: x.get("attributes", {}).get(
                "upload_date", ""
            ),
            reverse=True,
        )
    else:
        ranked = filtered

    # Handle "prefer" HI filter
    if filters.hearing_impaired == "prefer":
        ranked = sorted(
            ranked,
            key=lambda x: x.get("attributes", {}).get(
                "hearing_impaired", False
            ),
            reverse=True,
        )

    best = ranked[0] if ranked else None

    # Minimum similarity threshold for sanity check
    if best and strategy == "most-downloaded":
        similarity = release_similarity(
            source_filename,
            best.get("attributes", {}).get("release", ""),
        )
        if similarity < 0.15:
            release = best["attributes"].get("release")
            print(
                f"[SUBS] WARNING: Best result '{release}' has "
                f"very low relevance (sim={similarity:.2f}), skipping",
                flush=True,
            )
            return None

    return best


def get_search_params(entry: dict) -> dict:
    """Extract search parameters from a library mapping entry."""
    target = entry.get("target", "")
    target_path = Path(target)

    if entry["type"] == "movie":
        # movies/Movie Name (2024)/Movie Name (2024).mkv
        folder_name = target_path.parent.name
        match = re.search(r"^(.*)\s\((\d{4})\)", folder_name)
        if match:
            return {"query": match.group(1), "year": int(match.group(2))}
        return {"query": folder_name}

    elif entry["type"] in {"show", "anime"}:
        # shows/Series Name/Season 01/Series Name S01E01.mkv
        series_name = re.sub(r"\s+\[[^\]]+\]$", "", target_path.parts[1])
        series_name = re.sub(r"\s+\((19|20)\d{2}\)$", "", series_name)
        source_stem = Path(str(entry.get("source") or "")).stem
        parsed = parse_show(source_stem) or parse_show(target_path.stem)
        if parsed:
            return {
                "query": series_name,
                "season": parsed["season"],
                "episode": parsed["episode"],
            }
        return {"query": series_name}

    return {"query": target_path.stem}


def _filename_query(entry: dict) -> str:
    """Return the original source filename stem for fallback searches."""
    source_name = Path(str(entry.get("source") or "")).stem
    return source_name or Path(str(entry.get("target") or "")).stem


def _series_filename_query(series: str, filename: str) -> str:
    """Return a show/anime search query combining series and source filename."""
    if not series:
        return filename
    if not filename:
        return series
    series_tokens = _tokenize(series)
    filename_tokens = _tokenize(filename)
    if series_tokens and series_tokens <= filename_tokens:
        return filename
    return f"{series} {filename}"


def _subtitle_search_attempts(
    entry: dict, params: dict, query_override: str
) -> list[dict]:
    """Return ordered OpenSubtitles search attempts for a mapping entry."""
    if query_override:
        attempt = dict(params)
        attempt["query"] = query_override
        return [attempt]

    if entry.get("type") not in {"show", "anime"}:
        return [dict(params)]

    filename_query = _filename_query(entry)
    if params.get("season") is None or params.get("episode") is None:
        attempt = dict(params)
        attempt["query"] = filename_query
        return [attempt]

    primary = dict(params)
    primary["query"] = _series_filename_query(
        str(params.get("query") or ""), filename_query
    )
    attempts = [primary]
    if filename_query and filename_query != primary.get("query"):
        fallback = dict(params)
        fallback["query"] = filename_query
        attempts.append(fallback)
    return attempts


def _sleep_or_cancel(seconds: float, cancel_event: threading.Event | None) -> None:
    """Sleep for *seconds*, waking early when cancellation is requested."""
    if seconds <= 0:
        if cancel_event:
            raise_if_cancelled(cancel_event)
        return
    if cancel_event and cancel_event.wait(seconds):
        raise_if_cancelled(cancel_event)
    if cancel_event is None:
        time.sleep(seconds)


def _select_subtitle_from_attempts(
    client: OpenSubtitlesClient,
    attempts: list[dict],
    config: CuratorConfig,
    source_filename: str,
    feature_type: str,
    lang: str,
    cancel_event: threading.Event | None,
) -> tuple[dict | None, dict]:
    """Run ordered search attempts and return the first selected subtitle."""
    selected_params = attempts[0]
    for index, attempt in enumerate(attempts):
        if cancel_event:
            raise_if_cancelled(cancel_event)
        desc = _search_desc(attempt)
        print(
            f"[SUBS] Searching OpenSubtitles: {desc}, "
            f"lang={lang}, strategy={config.subtitles.strategy}",
            flush=True,
        )
        results = client.search(
            query=attempt["query"],
            year=attempt.get("year"),
            languages=lang,
            season=attempt.get("season"),
            episode=attempt.get("episode"),
            type=feature_type,
        )
        print(
            f"[SUBS] Search returned {len(results)} results for: {desc}",
            flush=True,
        )

        best = _search_with_fallbacks(
            client, results, config.subtitles.strategy,
            config.subtitles.filters, source_filename, attempt,
        )
        if best:
            return best, attempt
        if index < len(attempts) - 1:
            print(
                "[SUBS] No suitable subtitle found, retrying with filename",
                flush=True,
            )
    return None, selected_params


def _source_matches_torrent(source: str, torrent_name: str) -> bool:
    """Check if a mapping source path belongs to a given torrent.

    Source paths look like 'movies/TorrentName/file.mkv' or
    'shows/TorrentName/Season 01/file.mkv'. The torrent name is the
    first directory component after the category.
    """
    parts = Path(source).parts
    # parts[0] is category (movies/shows/anime), parts[1] is torrent dir
    return len(parts) >= 2 and parts[1] == torrent_name


def _open_state_db(config: CuratorConfig):
    """Open the curator state database with migrations applied."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(config.state_dir / "buzz.sqlite")
    db.apply_migrations(conn)
    return conn


def _load_query_overrides(
    config: CuratorConfig,
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """Load per-file subtitle query overrides and a torrent name->hash map."""
    conn = _open_state_db(config)
    try:
        overrides = db.load_subtitle_query_overrides(conn)
        if not overrides:
            return {}, {}
        name_to_hash = {
            name: thash
            for thash, name in db.load_torrent_name_hints(conn).items()
        }
        return overrides, name_to_hash
    finally:
        conn.close()


def _read_subtitle_meta(
    config: CuratorConfig, overlay_path: Path
) -> dict | None:
    """Read subtitle metadata from the SQLite state store."""
    conn = _open_state_db(config)
    try:
        overlay_key = db.subtitle_overlay_key(
            config.subtitle_root, overlay_path
        )
        return db.get_subtitle_metadata(conn, overlay_key)
    finally:
        conn.close()


def _write_subtitle_meta(
    config: CuratorConfig, overlay_path: Path, meta: dict
) -> None:
    """Write subtitle metadata into the SQLite state store."""
    conn = _open_state_db(config)
    try:
        overlay_key = db.subtitle_overlay_key(
            config.subtitle_root, overlay_path
        )
        db.upsert_subtitle_metadata(conn, overlay_key, meta)
    finally:
        conn.close()


def _prepare_mapping(
    config: CuratorConfig,
    mapping: list[dict] | None,
    torrent_names: list[str] | None,
) -> list[dict]:
    if mapping is None:
        conn = _open_state_db(config)
        try:
            mapping = db.load_curator_mapping(conn)
        finally:
            conn.close()
    if not mapping:
        if torrent_names:
            joined = ", ".join(sorted(set(torrent_names)))
            message = (
                f"no library mapping found for torrents: {joined}. "
                "try RESYNC LIB first."
            )
        else:
            message = (
                "no video files found in library mapping. "
                "try RESYNC LIB first."
            )
        record_event(message, level="error")
        raise RuntimeError(message)
    if torrent_names:
        names = set(torrent_names)
        mapping = [
            e for e in mapping
            if any(
                _source_matches_torrent(e["source"], name)
                for name in names
            )
        ]
        joined = ", ".join(sorted(names))
        record_event(f"subtitle fetch triggered for torrents: {joined}")
    else:
        record_event("subtitle fetch triggered for full library")
    if not mapping:
        if torrent_names:
            joined = ", ".join(sorted(set(torrent_names)))
            message = (
                f"no library mapping found for torrents: {joined}. "
                "try RESYNC LIB first."
            )
        else:
            message = (
                "no video files found in library mapping. "
                "try RESYNC LIB first."
            )
        record_event(message, level="error")
        raise RuntimeError(message)
    return mapping


def _search_desc(params: dict) -> str:
    desc = f"query='{params['query']}'"
    if params.get("year"):
        desc += f", year={params['year']}"
    if params.get("season"):
        desc += f", S{params['season']:02d}E{params.get('episode', 0):02d}"
    return desc


def _search_with_fallbacks(
    client: OpenSubtitlesClient,
    results: list,
    strategy: str,
    filters: Any,
    source_filename: str,
    params: dict,
) -> Any:
    best = rank_subtitles(
        results, strategy, filters, source_filename,
        query=params["query"], year=params.get("year"),
    )
    if not best and strategy != "most-downloaded":
        print(
            f"[SUBS] No match with strategy '{strategy}', "
            "falling back to most-downloaded",
            flush=True,
        )
        best = rank_subtitles(
            results, "most-downloaded", filters, source_filename,
            query=params["query"], year=params.get("year"),
        )
    if not best and strategy != "best-match":
        print(
            "[SUBS] No match with fallback, trying best-match",
            flush=True,
        )
        best = rank_subtitles(
            results, "best-match", filters, source_filename,
            query=params["query"], year=params.get("year"),
        )
    return best


def _install_subtitle(
    config: CuratorConfig,
    client: OpenSubtitlesClient,
    overlay_path: Path,
    target_path: Path,
    best: dict,
    params: dict,
    lang: str,
) -> bool | None:
    """Download and install a subtitle. Returns True for new, False for replacement, None if already up-to-date."""
    attr = best.get("attributes", {})
    file_id = attr.get("files", [{}])[0].get("file_id")
    release = attr.get("release", "unknown")

    if not file_id:
        print(
            f"[SUBS] WARNING: No file_id in result for '{release}'",
            flush=True,
        )
        record_event(
            f"no file ID in subtitle result for: {params['query']} ({lang})",
            level="warning",
        )
        return False

    if overlay_path.exists():
        meta = _read_subtitle_meta(config, overlay_path)
        if meta and meta.get("file_id") == file_id:
            print(
                f"[SUBS] Subtitle already up-to-date: '{release}' ({lang})",
                flush=True,
            )
            return None

    downloads = attr.get("download_count", 0)
    ratings = attr.get("ratings", 0)
    hi = attr.get("hearing_impaired", False)
    print(
        f"[SUBS] Selected: '{release}' (lang={lang}, "
        f"downloads={downloads}, rating={ratings}, hearing_impaired={hi})",
        flush=True,
    )

    is_replacement = overlay_path.exists()
    action = "replacing" if is_replacement else "downloading"
    record_event(
        f"{action} subtitle '{release}' ({lang}) for: {params['query']}"
    )
    download_link = client.download(file_id)
    content = client.fetch_content(download_link)

    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_bytes(content)
    _write_subtitle_meta(
        config, overlay_path, {"file_id": file_id, "release": release}
    )

    curated_sub = config.target_root / target_path.parent / f"{target_path.stem}.{lang}.srt"
    curated_sub.parent.mkdir(parents=True, exist_ok=True)
    if curated_sub.exists() or curated_sub.is_symlink():
        curated_sub.unlink()
    os.symlink(overlay_path, curated_sub)
    return not is_replacement


def _entry_hash_and_path(
    entry: dict, name_to_hash: dict[str, str]
) -> tuple[str, str]:
    """Resolve (torrent hash, in-torrent file path) for a mapping entry.

    The mapping ``source`` is ``{category}/{torrent_name}/{file path}``, the
    same layout the cache UI uses to store per-file subtitle query overrides.
    """
    parts = Path(entry["source"]).parts
    if len(parts) < 3:
        return "", ""
    torrent_name = parts[1]
    file_path = "/".join(parts[2:])
    thash = name_to_hash.get(torrent_name, "")
    return thash, file_path


def _query_override_for_entry(
    entry: dict,
    name_to_hash: dict[str, str],
    overrides: dict[tuple[str, str], str],
) -> str:
    """Return the subtitle query override for a mapping entry (empty if none)."""
    if not overrides:
        return ""
    thash, file_path = _entry_hash_and_path(entry, name_to_hash)
    if not thash or not file_path:
        return ""
    return overrides.get((thash.strip().lower(), file_path), "")


def _fetch_entry_subtitles(
    config: CuratorConfig,
    client: OpenSubtitlesClient,
    entry: dict,
    counters: dict,
    fetched_targets: list[str],
    cancel_event: threading.Event | None = None,
    query_override: str = "",
) -> None:
    target_path = Path(entry["target"])
    if target_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return

    source_filename = Path(entry["source"]).name
    params = get_search_params(entry)
    auto_query = params["query"]
    attempts = _subtitle_search_attempts(entry, params, query_override)
    feature_type = "movie" if entry["type"] == "movie" else "episode"

    if query_override and query_override != auto_query:
        record_event(
            f"subtitle query for {source_filename}: "
            f"'{query_override}' (override)"
        )
    else:
        record_event(
            f"subtitle query for {source_filename}: "
            f"'{attempts[0]['query']}'"
        )

    for lang in config.subtitles.languages:
        if cancel_event:
            raise_if_cancelled(cancel_event)

        overlay_path = (
            config.subtitle_root
            / target_path.parent
            / f"{target_path.stem}.{lang}.srt"
        )
        state.set_current(f"{target_path.stem} ({lang})")
        try:
            best, selected_params = _select_subtitle_from_attempts(
                client,
                attempts,
                config,
                source_filename,
                feature_type,
                lang,
                cancel_event,
            )
            if not best:
                desc = _search_desc(attempts[-1])
                print(
                    f"[SUBS] No suitable subtitle found for: {desc} ({lang})",
                    flush=True,
                )
                counters["skipped"] += 1
                _sleep_or_cancel(
                    config.subtitles.search_delay_secs, cancel_event
                )
                continue

            is_new = _install_subtitle(
                config,
                client,
                overlay_path,
                target_path,
                best,
                selected_params,
                lang,
            )
            if is_new is None:
                counters["already_exists"] += 1
                _sleep_or_cancel(
                    config.subtitles.search_delay_secs, cancel_event
                )
                continue
            if is_new:
                counters["fetched"] += 1
            else:
                counters["replaced"] += 1
            fetched_targets.append(entry["target"])
            _sleep_or_cancel(
                config.subtitles.download_delay_secs, cancel_event
            )
            _sleep_or_cancel(config.subtitles.search_delay_secs, cancel_event)

        except Exception as e:
            if str(e) == "cancelled":
                raise
            error_query = attempts[0]["query"]
            print(f"[SUBS] ERROR: {error_query} ({lang}): {e}", flush=True)
            record_event(
                f"subtitle error for {error_query} ({lang}): {e}",
                level="error",
            )
            state.error_count += 1
            counters["errors"] += 1


def _subtitle_summary(counters: dict) -> str:
    parts = []
    if counters["fetched"] > 0:
        parts.append(f"{counters['fetched']} downloaded")
    if counters["replaced"] > 0:
        parts.append(f"{counters['replaced']} replaced")
    if counters["skipped"] > 0:
        parts.append(f"{counters['skipped']} no match")
    if counters["errors"] > 0:
        parts.append(f"{counters['errors']} errors")
    if counters["already_exists"] > 0:
        parts.append(f"{counters['already_exists']} already up-to-date")
    if not parts:
        return "subtitle fetch complete: nothing to do"
    return "subtitle fetch complete: " + ", ".join(parts)


def fetch_subtitles_for_library(
    config: CuratorConfig,
    mapping: list[dict] | None = None,
    torrent_name: str | None = None,
    torrent_names: list[str] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Fetch subtitles for the entire library or specific torrents.

    Pass ``torrent_name`` for a single torrent or ``torrent_names`` for a
    batch of changed torrents. When neither is given, the whole library is
    scanned.
    """
    if not config.subtitles.enabled:
        return

    names = list(torrent_names or [])
    if torrent_name:
        names.append(torrent_name)
    mapping = _prepare_mapping(config, mapping, names or None)
    if not mapping:
        return

    query_overrides, name_to_hash = _load_query_overrides(config)

    state.start()
    counters = {
        "fetched": 0,
        "replaced": 0,
        "skipped": 0,
        "errors": 0,
        "already_exists": 0,
    }
    fetched_targets: list[str] = []
    try:
        with OpenSubtitlesClient(config.subtitles) as client:
            for entry in mapping:
                if cancel_event:
                    raise_if_cancelled(cancel_event)
                _fetch_entry_subtitles(
                    config,
                    client,
                    entry,
                    counters,
                    fetched_targets,
                    cancel_event=cancel_event,
                    query_override=_query_override_for_entry(
                        entry, name_to_hash, query_overrides
                    ),
                )

        state.stop()
        summary = _subtitle_summary(counters)
        print(f"[SUBS] {summary}", flush=True)
        record_event(summary)
        if counters["errors"] > 0:
            raise RuntimeError(summary)

        if (
            fetched_targets
            and config.trigger_lib_scan
            and config.jellyfin_api_key
        ):
            trigger_jellyfin_selective_refresh(config, fetched_targets)
    except Exception as e:
        if str(e) == "cancelled":
            print("[SUBS] subtitle fetch cancelled", flush=True)
            record_event("subtitle fetch cancelled")
            state.stop()
            raise
        print(f"[SUBS] FATAL: Subtitle fetcher failed: {e}", flush=True)
        record_event(f"subtitle fetcher failed: {e}", level="error")
        state.stop(error=True)
        raise


def apply_subtitle_overlay(
    tmp_root: Path,
    subtitle_root: Path,
    mapping: list[dict] | None = None,
) -> None:
    """Symlink downloaded subtitles into the temporary root.

    When a current Curator mapping is provided, only subtitles matching
    currently mapped video targets are overlaid. This prevents stale subtitle
    sidecars from recreating old curated folders after an identity override
    changes a movie or show target path.
    """
    if not subtitle_root.exists():
        return

    allowed_targets: set[tuple[Path, str]] | None = None
    if mapping is not None:
        allowed_targets = set()
        for entry in mapping:
            target = Path(str(entry.get("target") or ""))
            if target.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            allowed_targets.add((target.parent, target.stem))

    for sub_path in subtitle_root.rglob("*.srt"):
        rel_path = sub_path.relative_to(subtitle_root)
        if allowed_targets is not None and (
            rel_path.parent,
            rel_path.stem.rsplit(".", 1)[0],
        ) not in allowed_targets:
            continue
        target_path = tmp_root / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() or target_path.is_symlink():
            target_path.unlink()
        os.symlink(sub_path, target_path)


def background_fetch_subtitles(
    config: CuratorConfig,
    torrent_name: str | None = None,
) -> None:
    """Start a background thread to fetch subtitles."""
    thread = threading.Thread(
        target=fetch_subtitles_for_library,
        args=(config,),
        kwargs={"torrent_name": torrent_name},
        daemon=True,
    )
    thread.start()
