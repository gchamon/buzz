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
from .media import VIDEO_EXTENSIONS
from .media_server import trigger_jellyfin_selective_refresh


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

        record_event("Logging in to OpenSubtitles...", level="info")
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

    elif entry["type"] == "show":
        # shows/Series Name/Season 01/Series Name S01E01.mkv
        series_name = target_path.parts[1]
        stem = target_path.stem
        match = re.search(r"(?i)S(\d+)E(\d+)", stem)
        if match:
            return {
                "query": series_name,
                "season": int(match.group(1)),
                "episode": int(match.group(2)),
            }
        return {"query": series_name}

    return {"query": target_path.stem}


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
    torrent_name: str | None,
) -> list[dict]:
    if mapping is None:
        conn = _open_state_db(config)
        try:
            mapping = db.load_curator_mapping(conn)
        finally:
            conn.close()
    if not mapping:
        return []
    if torrent_name:
        mapping = [
            e for e in mapping
            if _source_matches_torrent(e["source"], torrent_name)
        ]
        record_event(f"Subtitle fetch triggered for torrent: {torrent_name}")
    else:
        record_event("Subtitle fetch triggered for full library")
    if not mapping:
        if torrent_name:
            record_event(
                f"No library mapping found for torrent: {torrent_name}. "
                "Try RESYNC LIB first.",
                level="error",
            )
        else:
            record_event(
                "No video files found in library mapping. "
                "Try RESYNC LIB first.",
                level="error",
            )
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
            f"No file ID in subtitle result for: {params['query']} ({lang})",
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
    action = "Replacing" if is_replacement else "Downloading"
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


def _fetch_entry_subtitles(
    config: CuratorConfig,
    client: OpenSubtitlesClient,
    entry: dict,
    counters: dict,
    fetched_targets: list[str],
) -> None:
    target_path = Path(entry["target"])
    if target_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return

    source_filename = Path(entry["source"]).name
    params = get_search_params(entry)
    desc = _search_desc(params)
    feature_type = "movie" if entry["type"] == "movie" else "episode"

    for lang in config.subtitles.languages:
        overlay_path = (
            config.subtitle_root
            / target_path.parent
            / f"{target_path.stem}.{lang}.srt"
        )
        state.set_current(f"{target_path.stem} ({lang})")
        print(
            f"[SUBS] Searching OpenSubtitles: {desc}, "
            f"lang={lang}, strategy={config.subtitles.strategy}",
            flush=True,
        )

        try:
            results = client.search(
                query=params["query"],
                year=params.get("year"),
                languages=lang,
                season=params.get("season"),
                episode=params.get("episode"),
                type=feature_type,
            )
            print(
                f"[SUBS] Search returned {len(results)} results for: {desc}",
                flush=True,
            )

            best = _search_with_fallbacks(
                client, results, config.subtitles.strategy,
                config.subtitles.filters, source_filename, params,
            )
            if not best:
                print(
                    f"[SUBS] No suitable subtitle found for: {desc} ({lang})",
                    flush=True,
                )
                counters["skipped"] += 1
                time.sleep(config.subtitles.search_delay_secs)
                continue

            is_new = _install_subtitle(
                config, client, overlay_path, target_path, best, params, lang
            )
            if is_new is None:
                counters["already_exists"] += 1
                time.sleep(config.subtitles.search_delay_secs)
                continue
            if is_new:
                counters["fetched"] += 1
            else:
                counters["replaced"] += 1
            fetched_targets.append(entry["target"])
            time.sleep(config.subtitles.download_delay_secs)
            time.sleep(config.subtitles.search_delay_secs)

        except Exception as e:
            print(f"[SUBS] ERROR: {params['query']} ({lang}): {e}", flush=True)
            record_event(
                f"Subtitle error for {params['query']} ({lang}): {e}",
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
        return "Subtitle fetch complete: nothing to do"
    return "Subtitle fetch complete: " + ", ".join(parts)


def fetch_subtitles_for_library(
    config: CuratorConfig,
    mapping: list[dict] | None = None,
    torrent_name: str | None = None,
) -> None:
    """Fetch subtitles for the entire library or a single torrent."""
    if not config.subtitles.enabled:
        return

    mapping = _prepare_mapping(config, mapping, torrent_name)
    if not mapping:
        return

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
                _fetch_entry_subtitles(config, client, entry, counters, fetched_targets)

        state.stop()
        summary = _subtitle_summary(counters)
        print(f"[SUBS] {summary}", flush=True)
        record_event(summary)

        if (
            fetched_targets
            and config.trigger_lib_scan
            and config.jellyfin_api_key
        ):
            trigger_jellyfin_selective_refresh(config, fetched_targets)
    except Exception as e:
        print(f"[SUBS] FATAL: Subtitle fetcher failed: {e}", flush=True)
        record_event(f"Subtitle fetcher failed: {e}", level="error")
        state.stop(error=True)


def apply_subtitle_overlay(
    tmp_root: Path, subtitle_root: Path
) -> None:
    """Symlink downloaded subtitles into the temporary root."""
    if not subtitle_root.exists():
        return

    for sub_path in subtitle_root.rglob("*.srt"):
        rel_path = sub_path.relative_to(subtitle_root)
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
