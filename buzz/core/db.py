"""SQLite database setup, schema migrations, and repository helpers."""

import json
import logging
import re
import sqlite3
from datetime import UTC
from pathlib import Path
from typing import Any

from .providers import split_provider_torrent_id
from .utils import magnet_display_name, stable_json

logger = logging.getLogger(__name__)
_HASH_RE = re.compile(r"^[a-fA-F0-9]{32,64}$")

_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS torrents (
            id TEXT PRIMARY KEY,
            signature_json TEXT NOT NULL,
            info_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive (
            hash TEXT PRIMARY KEY,
            name TEXT,
            bytes INTEGER,
            files_json TEXT,
            deleted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS library_snapshot (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            snapshot_json TEXT NOT NULL,
            digest TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS curator_mapping (
            source TEXT NOT NULL,
            target TEXT PRIMARY KEY,
            type TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS curator_report (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            report_json TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subtitle_metadata (
            overlay_path TEXT PRIMARY KEY,
            file_id INTEGER NOT NULL,
            release TEXT,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE torrents ADD COLUMN magnet TEXT;
        ALTER TABLE archive ADD COLUMN magnet TEXT;
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS opensubtitles_languages (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS opensubtitles_languages_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            fetched_at TEXT NOT NULL
        );
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS library_entries (
            hash TEXT PRIMARY KEY,
            name TEXT,
            bytes INTEGER,
            files_json TEXT NOT NULL DEFAULT '[]',
            magnet TEXT,
            deleted_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_links (
            provider TEXT NOT NULL,
            provider_torrent_id TEXT NOT NULL,
            hash TEXT NOT NULL,
            status TEXT,
            progress REAL,
            info_json TEXT NOT NULL DEFAULT '{}',
            signature_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, provider_torrent_id),
            FOREIGN KEY (hash) REFERENCES library_entries(hash)
                ON DELETE CASCADE
        );
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS file_selections (
            hash TEXT NOT NULL,
            path TEXT NOT NULL,
            selected INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (hash, path)
        );
        """,
    ),
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS category_overrides (
            hash TEXT PRIMARY KEY,
            category TEXT NOT NULL CHECK (category IN ('movies', 'shows', 'anime')),
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS subtitle_query_overrides (
            hash TEXT NOT NULL,
            path TEXT NOT NULL,
            query TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (hash, path)
        );
        """,
    ),
    (
        8,
        """
        CREATE TABLE IF NOT EXISTS config_favorites (
            section TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL
        );

        INSERT OR IGNORE INTO config_favorites (section, updated_at)
        VALUES ('subtitles', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
        """,
    ),
    (
        9,
        """
        CREATE TABLE IF NOT EXISTS curator_title_overrides (
            hash TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('movie', 'show')),
            title TEXT,
            series TEXT,
            year INTEGER,
            external_id TEXT,
            provider_ids_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        10,
        # Relax the curator_title_overrides.kind CHECK to allow 'anime'.
        # SQLite cannot ALTER a CHECK constraint, so rebuild the table and
        # copy existing rows across.
        """
        CREATE TABLE curator_title_overrides_v10 (
            hash TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('movie', 'show', 'anime')),
            title TEXT,
            series TEXT,
            year INTEGER,
            external_id TEXT,
            provider_ids_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );

        INSERT INTO curator_title_overrides_v10
            (hash, kind, title, series, year, external_id,
             provider_ids_json, updated_at)
        SELECT hash, kind, title, series, year, external_id,
               provider_ids_json, updated_at
        FROM curator_title_overrides;

        DROP TABLE curator_title_overrides;

        ALTER TABLE curator_title_overrides_v10
            RENAME TO curator_title_overrides;
        """,
    ),
]

def connect(path: Path | str, timeout: float = 30.0) -> sqlite3.Connection:
    """Open the DB with WAL mode and return the connection."""
    conn = sqlite3.connect(
        str(path),
        timeout=timeout,
        check_same_thread=False,
        isolation_level="IMMEDIATE",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        return row[0] or 0
    except sqlite3.OperationalError:
        return 0


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations inside a single transaction."""
    # Use an IMMEDIATE transaction to prevent deadlocks when multiple processes
    # (e.g. dav and curator) start simultaneously and check/apply migrations.
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = _current_version(conn)
        pending = [(v, sql) for v, sql in _MIGRATIONS if v > version]
        if pending:
            for v, sql in pending:
                for statement in sql.split(";"):
                    statement = statement.strip()
                    if statement:
                        conn.execute(statement)
                if v == 4:
                    _backfill_provider_library(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version (version, applied_at)"
                    " VALUES (?, datetime('now'))",
                    (v,),
                )
            logger.info(
                "applied %d migration(s), now at version %d",
                len(pending),
                pending[-1][0],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _backfill_provider_library(conn: sqlite3.Connection) -> None:
    """Populate provider-neutral tables from legacy torrent/archive tables."""
    if conn.execute("SELECT COUNT(*) FROM provider_links").fetchone()[0]:
        return
    now = _now_iso()
    with conn:
        torrent_rows = conn.execute(
            "SELECT id, signature_json, info_json, magnet FROM torrents"
        ).fetchall()
        for row in torrent_rows:
            info = json.loads(row["info_json"] or "{}")
            thash = str(info.get("hash") or row["id"]).lower()
            files = [
                {
                    "id": item.get("id"),
                    "path": item.get("path"),
                    "bytes": item.get("bytes"),
                }
                for item in info.get("files", [])
                if isinstance(item, dict) and item.get("selected")
            ]
            conn.execute(
                "INSERT OR IGNORE INTO library_entries "
                "(hash, name, bytes, files_json, magnet, deleted_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    thash,
                    info.get("filename") or info.get("original_filename"),
                    info.get("bytes"),
                    json.dumps(files),
                    row["magnet"],
                    now,
                ),
            )
            provider, provider_torrent_id = split_provider_torrent_id(str(row["id"]))
            conn.execute(
                "INSERT OR REPLACE INTO provider_links "
                "(provider, provider_torrent_id, hash, status, progress, "
                "info_json, signature_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    provider_torrent_id,
                    thash,
                    info.get("status"),
                    info.get("progress"),
                    row["info_json"],
                    row["signature_json"],
                    now,
                ),
            )

        archive_rows = conn.execute(
            "SELECT hash, name, bytes, files_json, deleted_at, magnet FROM archive"
        ).fetchall()
        for row in archive_rows:
            thash = str(row["hash"]).lower()
            conn.execute(
                "INSERT OR IGNORE INTO library_entries "
                "(hash, name, bytes, files_json, magnet, deleted_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    thash,
                    row["name"],
                    row["bytes"],
                    row["files_json"] or "[]",
                    row["magnet"],
                    row["deleted_at"],
                    now,
                ),
            )


def migrate_legacy_files(conn: sqlite3.Connection, state_dir: Path) -> None:
    """Import legacy JSON files into DB tables if the tables are empty."""
    _migrate_torrent_cache(conn, state_dir)
    _migrate_archive(conn, state_dir)
    _migrate_library_snapshot(conn, state_dir)
    _migrate_curator_mapping(conn, state_dir)
    _migrate_curator_report(conn, state_dir)
    _backfill_provider_library(conn)


def _load_json_file(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _rename_migrated(path: Path) -> None:
    path.rename(path.with_suffix(".migrated"))


def _migrate_torrent_cache(conn: sqlite3.Connection, state_dir: Path) -> None:
    count = conn.execute("SELECT COUNT(*) FROM torrents").fetchone()[0]
    if count:
        return
    path = state_dir / "torrent_cache.json"
    data = _load_json_file(path)
    if not isinstance(data, dict) or not data:
        return
    now = _now_iso()
    with conn:
        for torrent_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO torrents "
                "(id, signature_json, info_json, updated_at, magnet) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    torrent_id,
                    json.dumps(entry.get("signature", {})),
                    json.dumps(entry.get("info", {})),
                    now,
                    entry.get("magnet"),
                ),
            )
    _rename_migrated(path)
    logger.info("imported %d torrent(s) from %s", len(data), path.name)


def _migrate_archive(conn: sqlite3.Connection, state_dir: Path) -> None:
    count = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
    if count:
        return
    path = state_dir / "archive.json"
    data = _load_json_file(path)
    if not isinstance(data, dict) or not data:
        return
    with conn:
        for thash, entry in data.items():
            if not isinstance(entry, dict):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO archive "
                "(hash, name, bytes, files_json, deleted_at, magnet) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    thash,
                    entry.get("name"),
                    entry.get("bytes"),
                    json.dumps(entry.get("files", [])),
                    entry.get("deleted_at", _now_iso()),
                    entry.get("magnet"),
                ),
            )
    _rename_migrated(path)
    logger.info("imported %d archive entry/entries from %s", len(data), path.name)


def _migrate_library_snapshot(conn: sqlite3.Connection, state_dir: Path) -> None:
    count = conn.execute("SELECT COUNT(*) FROM library_snapshot").fetchone()[0]
    if count:
        return
    path = state_dir / "library_snapshot.json"
    data = _load_json_file(path)
    if not isinstance(data, dict) or not data:
        return
    from ..core.state import canonical_snapshot
    from .utils import stable_json
    digest = stable_json(canonical_snapshot(data))
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO library_snapshot"
            " (singleton, snapshot_json, digest, generated_at) VALUES (1, ?, ?, ?)",
            (json.dumps(data), digest, data.get("generated_at", _now_iso())),
        )
    _rename_migrated(path)
    logger.info("imported library snapshot from %s", path.name)


def _migrate_curator_mapping(conn: sqlite3.Connection, state_dir: Path) -> None:
    count = conn.execute("SELECT COUNT(*) FROM curator_mapping").fetchone()[0]
    if count:
        return
    path = state_dir / "mapping.json"
    data = _load_json_file(path)
    if not isinstance(data, list) or not data:
        return
    with conn:
        for entry in data:
            if not isinstance(entry, dict):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO curator_mapping (source, target, type) VALUES (?, ?, ?)",
                (entry.get("source", ""), entry.get("target", ""), entry.get("type", "")),
            )
    _rename_migrated(path)
    logger.info("imported %d mapping entry/entries from %s", len(data), path.name)


def _migrate_curator_report(conn: sqlite3.Connection, state_dir: Path) -> None:
    count = conn.execute("SELECT COUNT(*) FROM curator_report").fetchone()[0]
    if count:
        return
    path = state_dir / "report.json"
    data = _load_json_file(path)
    if not isinstance(data, dict) or not data:
        return
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO curator_report"
            " (singleton, report_json, generated_at) VALUES (1, ?, ?)",
            (json.dumps(data), _now_iso()),
        )
    _rename_migrated(path)
    logger.info("imported curator report from %s", path.name)


def migrate_subtitle_sidecars(
    conn: sqlite3.Connection, subtitle_root: Path
) -> None:
    """Import *.buzz.json subtitle sidecars into subtitle_metadata."""
    if not subtitle_root.exists():
        return
    imported = 0
    now = _now_iso()
    with conn:
        for sidecar in sorted(subtitle_root.rglob("*.buzz.json")):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            file_id = data.get("file_id")
            if not file_id:
                continue
            # The sidecar sits next to the .srt; strip the .buzz.json suffix
            srt_path = sidecar.with_suffix("").with_suffix("")
            if not srt_path.exists():
                continue
            overlay_path = srt_path.relative_to(subtitle_root).as_posix()
            conn.execute(
                "INSERT OR REPLACE INTO subtitle_metadata"
                " (overlay_path, file_id, release, updated_at) VALUES (?, ?, ?, ?)",
                (overlay_path, int(file_id), data.get("release"), now),
            )
            sidecar.rename(sidecar.with_suffix(".migrated"))
            imported += 1
    if imported:
        logger.info("imported %d subtitle sidecar(s) from %s", imported, subtitle_root)


# ---------------------------------------------------------------------------
# Repository helpers used by business-logic modules
# ---------------------------------------------------------------------------


def load_curator_mapping(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Return the current Curator mapping rows ordered by target path."""
    rows = conn.execute(
        "SELECT source, target, type FROM curator_mapping ORDER BY target"
    ).fetchall()
    return [
        {"source": row["source"], "target": row["target"], "type": row["type"]}
        for row in rows
    ]


def replace_curator_mapping(
    conn: sqlite3.Connection, mapping: list[dict[str, str]]
) -> None:
    """Replace all Curator mapping rows in one transaction."""
    with conn:
        conn.execute("DELETE FROM curator_mapping")
        for entry in mapping:
            conn.execute(
                "INSERT INTO curator_mapping (source, target, type)"
                " VALUES (?, ?, ?)",
                (entry["source"], entry["target"], entry["type"]),
            )


def load_curator_report(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the latest curator report payload, if present."""
    row = conn.execute(
        "SELECT report_json FROM curator_report WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["report_json"])


def save_curator_report(conn: sqlite3.Connection, report: dict[str, Any]) -> None:
    """Persist the current curator report payload."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO curator_report"
            " (singleton, report_json, generated_at) VALUES (1, ?, ?)",
            (json.dumps(report), _now_iso()),
        )


def subtitle_overlay_key(subtitle_root: Path, overlay_path: Path) -> str:
    """Return the DB key for an overlay path relative to *subtitle_root*."""
    try:
        return overlay_path.relative_to(subtitle_root).as_posix()
    except ValueError:
        return overlay_path.as_posix()


def get_subtitle_metadata(
    conn: sqlite3.Connection, overlay_path: str
) -> dict | None:
    """Return subtitle metadata for *overlay_path*, or None if not found."""
    row = conn.execute(
        "SELECT file_id, release FROM subtitle_metadata WHERE overlay_path = ?",
        (overlay_path,),
    ).fetchone()
    if row is None:
        return None
    return {"file_id": row["file_id"], "release": row["release"]}


def upsert_subtitle_metadata(
    conn: sqlite3.Connection, overlay_path: str, meta: dict
) -> None:
    """Insert or replace subtitle metadata for *overlay_path*."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO subtitle_metadata"
            " (overlay_path, file_id, release, updated_at) VALUES (?, ?, ?, ?)",
            (overlay_path, int(meta["file_id"]), meta.get("release"), _now_iso()),
        )


def load_opensubtitles_languages(
    conn: sqlite3.Connection,
) -> tuple[list[tuple[str, str]], str | None]:
    """Return cached (code, name) language pairs and the fetched_at timestamp."""
    rows = conn.execute(
        "SELECT code, name FROM opensubtitles_languages ORDER BY name COLLATE NOCASE"
    ).fetchall()
    languages = [(row["code"], row["name"]) for row in rows]
    meta = conn.execute(
        "SELECT fetched_at FROM opensubtitles_languages_meta WHERE singleton = 1"
    ).fetchone()
    fetched_at = meta["fetched_at"] if meta else None
    return languages, fetched_at


def save_opensubtitles_languages(
    conn: sqlite3.Connection, languages: list[tuple[str, str]]
) -> None:
    """Replace the cached language list and stamp fetched_at to now."""
    with conn:
        conn.execute("DELETE FROM opensubtitles_languages")
        conn.executemany(
            "INSERT INTO opensubtitles_languages (code, name) VALUES (?, ?)",
            [(code, name) for code, name in languages],
        )
        conn.execute(
            "INSERT OR REPLACE INTO opensubtitles_languages_meta"
            " (singleton, fetched_at) VALUES (1, ?)",
            (_now_iso(),),
        )


def replace_provider_library(
    conn: sqlite3.Connection,
    entries: list[dict[str, Any]],
) -> None:
    """Replace live (non-archived) provider library rows in one transaction.

    Each entry must have: hash, name, bytes, files (list of selected-file dicts),
    magnet, provider, provider_torrent_id, status, progress, info_json, signature_json.
    Archived rows (deleted_at IS NOT NULL) in library_entries are preserved.
    """
    now = _now_iso()
    by_hash: dict[str, dict[str, Any]] = {}
    provider_entries: list[tuple[str, dict[str, Any]]] = []
    for entry in entries:
        thash = str(entry.get("hash") or "").strip().lower()
        if not thash:
            continue
        provider_entries.append((thash, entry))
        aggregate = by_hash.get(thash)
        if aggregate is None:
            by_hash[thash] = dict(entry)
            continue
        if not _readable_name(aggregate.get("name")):
            name = _readable_name(entry.get("name"))
            if name:
                aggregate["name"] = name
        if aggregate.get("bytes") is None and entry.get("bytes") is not None:
            aggregate["bytes"] = entry.get("bytes")
        if not aggregate.get("files") and entry.get("files"):
            aggregate["files"] = entry.get("files")
        if not aggregate.get("magnet") and entry.get("magnet"):
            aggregate["magnet"] = entry.get("magnet")

    with conn:
        conn.execute("DELETE FROM provider_links")
        for thash, entry in by_hash.items():
            conn.execute(
                "INSERT INTO library_entries "
                "(hash, name, bytes, files_json, magnet, deleted_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(hash) DO UPDATE SET "
                "  name       = COALESCE(excluded.name, CASE "
                "    WHEN length(library_entries.name) BETWEEN 32 AND 64 "
                "     AND library_entries.name NOT GLOB '*[^0-9a-fA-F]*' "
                "    THEN NULL ELSE library_entries.name END), "
                "  bytes      = COALESCE(excluded.bytes, library_entries.bytes), "
                "  files_json = CASE "
                "    WHEN excluded.files_json != '[]' THEN excluded.files_json "
                "    ELSE library_entries.files_json END, "
                "  magnet     = COALESCE(excluded.magnet, library_entries.magnet), "
                "  updated_at = excluded.updated_at "
                "WHERE library_entries.deleted_at IS NULL",
                (
                    thash,
                    _readable_name(entry.get("name")) or None,
                    entry.get("bytes"),
                    json.dumps(entry.get("files", [])),
                    entry.get("magnet"),
                    now,
                ),
            )
        for thash, entry in provider_entries:
            provider = str(entry.get("provider") or "").strip()
            provider_torrent_id = str(entry.get("provider_torrent_id") or "").strip()
            if not provider or not provider_torrent_id:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO provider_links "
                "(provider, provider_torrent_id, hash, status, progress, "
                "info_json, signature_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    provider_torrent_id,
                    thash,
                    entry.get("status"),
                    entry.get("progress"),
                    entry.get("info_json", "{}"),
                    entry.get("signature_json", "{}"),
                    now,
                ),
            )
        # Remove live entries no longer tracked by any provider.
        conn.execute(
            "DELETE FROM library_entries "
            "WHERE deleted_at IS NULL "
            "  AND hash NOT IN (SELECT DISTINCT hash FROM provider_links)"
        )


def load_provider_links_by_hash(
    conn: sqlite3.Connection, thash: str
) -> list[tuple[str, str]]:
    """Return [(provider, provider_torrent_id)] for a normalized hash."""
    rows = conn.execute(
        "SELECT provider, provider_torrent_id FROM provider_links WHERE hash = ?",
        (thash.strip().lower(),),
    ).fetchall()
    return [(row["provider"], row["provider_torrent_id"]) for row in rows]


def load_torrent_name_hints(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {hash: name} for library_entries, falling back to magnet dn=."""
    rows = conn.execute(
        "SELECT hash, name, magnet FROM library_entries"
    ).fetchall()
    hints: dict[str, str] = {}
    for row in rows:
        thash = row["hash"]
        if not thash:
            continue
        name = _readable_name(row["name"])
        if not name:
            name = magnet_display_name(row["magnet"] or "")
        if name:
            hints[thash] = name
    return hints


def load_library_entry_files(
    conn: sqlite3.Connection,
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Return {hash: (name, selected files)} for live library entries."""
    rows = conn.execute(
        "SELECT hash, name, magnet, files_json FROM library_entries "
        "WHERE deleted_at IS NULL"
    ).fetchall()
    entries: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for row in rows:
        thash = str(row["hash"] or "").strip().lower()
        if not thash:
            continue
        name = _readable_name(row["name"])
        if not name:
            name = magnet_display_name(row["magnet"] or "")
        try:
            files = json.loads(row["files_json"] or "[]")
        except json.JSONDecodeError:
            files = []
        if name and isinstance(files, list):
            entries[thash] = (
                name,
                [file for file in files if isinstance(file, dict)],
            )
    return entries


def delete_library_entry(conn: sqlite3.Connection, thash: str) -> None:
    """Remove a library entry by hash; cascades to provider_links."""
    with conn:
        conn.execute(
            "DELETE FROM library_entries WHERE hash = ?",
            (thash.strip().lower(),),
        )


def load_file_selections(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Return {hash: {selected_path, ...}} for stored per-torrent selections."""
    rows = conn.execute(
        "SELECT hash, path FROM file_selections WHERE selected = 1"
    ).fetchall()
    selections: dict[str, set[str]] = {}
    for row in rows:
        thash = str(row["hash"] or "").strip().lower()
        path = str(row["path"] or "")
        if not thash or not path:
            continue
        selections.setdefault(thash, set()).add(path)
    return selections


def save_file_selection(
    conn: sqlite3.Connection,
    thash: str,
    selected_paths: set[str],
    all_paths: set[str],
) -> None:
    """Persist the selection for a torrent: one row per known path, 0/1 flag."""
    thash = thash.strip().lower()
    if not thash:
        return
    now = _now_iso()
    with conn:
        conn.execute(
            "DELETE FROM file_selections WHERE hash = ?",
            (thash,),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO file_selections "
            "(hash, path, selected, updated_at) VALUES (?, ?, ?, ?)",
            [
                (thash, path, 1 if path in selected_paths else 0, now)
                for path in all_paths
            ],
        )


def delete_file_selection(conn: sqlite3.Connection, thash: str) -> None:
    """Remove a stored selection by hash."""
    thash = thash.strip().lower()
    if not thash:
        return
    with conn:
        conn.execute(
            "DELETE FROM file_selections WHERE hash = ?",
            (thash,),
        )


def load_category_overrides(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {hash: category} for per-torrent category overrides."""
    rows = conn.execute(
        "SELECT hash, category FROM category_overrides"
    ).fetchall()
    overrides: dict[str, str] = {}
    for row in rows:
        thash = str(row["hash"] or "").strip().lower()
        category = str(row["category"] or "").strip()
        if thash and category in {"movies", "shows", "anime"}:
            overrides[thash] = category
    return overrides


def save_category_override(
    conn: sqlite3.Connection, thash: str, category: str | None
) -> None:
    """Persist or clear a per-torrent category override."""
    thash = thash.strip().lower()
    if not thash:
        return
    normalized = str(category or "").strip()
    with conn:
        if not normalized:
            conn.execute(
                "DELETE FROM category_overrides WHERE hash = ?",
                (thash,),
            )
            return
        if normalized not in {"movies", "shows", "anime"}:
            raise ValueError(f"invalid category override: {category}")
        conn.execute(
            "INSERT OR REPLACE INTO category_overrides "
            "(hash, category, updated_at) VALUES (?, ?, ?)",
            (thash, normalized, _now_iso()),
        )


def load_config_favorites(conn: sqlite3.Connection) -> set[str]:
    """Return the set of favorited config section keys."""
    rows = conn.execute("SELECT section FROM config_favorites").fetchall()
    favorites: set[str] = set()
    for row in rows:
        section = str(row["section"] or "").strip()
        if section:
            favorites.add(section)
    return favorites


def save_config_favorite(
    conn: sqlite3.Connection, section: str, favorited: bool
) -> None:
    """Persist or clear a favorited config section."""
    section = section.strip()
    if not section:
        return
    with conn:
        if favorited:
            conn.execute(
                "INSERT OR REPLACE INTO config_favorites "
                "(section, updated_at) VALUES (?, ?)",
                (section, _now_iso()),
            )
        else:
            conn.execute(
                "DELETE FROM config_favorites WHERE section = ?",
                (section,),
            )


def load_subtitle_query_overrides(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    """Return {(hash, path): query} for per-file subtitle query overrides."""
    rows = conn.execute(
        "SELECT hash, path, query FROM subtitle_query_overrides"
    ).fetchall()
    overrides: dict[tuple[str, str], str] = {}
    for row in rows:
        thash = str(row["hash"] or "").strip().lower()
        path = str(row["path"] or "").strip()
        query = str(row["query"] or "").strip()
        if thash and path and query:
            overrides[(thash, path)] = query
    return overrides


def get_subtitle_query_override(
    conn: sqlite3.Connection, thash: str, path: str
) -> str | None:
    """Return the subtitle query override for a single file, or None."""
    thash = thash.strip().lower()
    path = path.strip()
    if not thash or not path:
        return None
    row = conn.execute(
        "SELECT query FROM subtitle_query_overrides WHERE hash = ? AND path = ?",
        (thash, path),
    ).fetchone()
    if not row:
        return None
    query = str(row["query"] or "").strip()
    return query or None


def save_subtitle_query_override(
    conn: sqlite3.Connection, thash: str, path: str, query: str | None
) -> None:
    """Persist or clear a per-file subtitle query override."""
    thash = thash.strip().lower()
    path = path.strip()
    if not thash or not path:
        return
    normalized = str(query or "").strip()
    with conn:
        if not normalized:
            conn.execute(
                "DELETE FROM subtitle_query_overrides "
                "WHERE hash = ? AND path = ?",
                (thash, path),
            )
            return
        conn.execute(
            "INSERT OR REPLACE INTO subtitle_query_overrides "
            "(hash, path, query, updated_at) VALUES (?, ?, ?, ?)",
            (thash, path, normalized, _now_iso()),
        )


def _curator_title_override_from_row(
    row: sqlite3.Row,
) -> dict[str, Any]:
    override: dict[str, Any] = {
        "kind": row["kind"],
    }
    for key in ("title", "series", "year"):
        value = row[key]
        if value is not None and value != "":
            override[key] = value
    external_id = str(row["external_id"] or "").strip()
    if external_id:
        override["id"] = external_id
    try:
        provider_ids = json.loads(row["provider_ids_json"] or "{}")
    except (KeyError, json.JSONDecodeError):
        provider_ids = {}
    if isinstance(provider_ids, dict):
        cleaned = {
            str(key).strip().lower(): str(value).strip()
            for key, value in provider_ids.items()
            if str(key).strip() and str(value).strip()
        }
        if cleaned:
            override["provider_ids"] = cleaned
    return override


def load_curator_title_overrides(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Return {hash: title override} for Curator naming rules."""
    rows = conn.execute(
        "SELECT hash, kind, title, series, year, "
        "external_id, provider_ids_json FROM curator_title_overrides"
    ).fetchall()
    overrides: dict[str, dict[str, Any]] = {}
    for row in rows:
        thash = str(row["hash"] or "").strip().lower()
        if thash:
            overrides[thash] = _curator_title_override_from_row(row)
    return overrides


def get_curator_title_override(
    conn: sqlite3.Connection, thash: str
) -> dict[str, Any] | None:
    """Return one Curator naming override, if present."""
    thash = thash.strip().lower()
    if not thash:
        return None
    row = conn.execute(
        "SELECT hash, kind, title, series, year, "
        "external_id, provider_ids_json FROM curator_title_overrides "
        "WHERE hash = ?",
        (thash,),
    ).fetchone()
    if row is None:
        return None
    return _curator_title_override_from_row(row)


def save_curator_title_override(
    conn: sqlite3.Connection,
    thash: str,
    override: dict[str, Any] | None,
) -> None:
    """Persist or clear an entry-level Curator naming override."""
    thash = thash.strip().lower()
    if not thash:
        return
    if not override:
        with conn:
            conn.execute(
                "DELETE FROM curator_title_overrides WHERE hash = ?",
                (thash,),
            )
        return

    kind = str(override.get("kind") or "").strip()
    if kind not in {"movie", "show", "anime"}:
        raise ValueError(f"invalid curator title override kind: {kind}")
    provider_ids = {
        str(key).strip().lower(): str(value).strip()
        for key, value in (override.get("provider_ids") or {}).items()
        if str(key).strip() and str(value).strip()
    }
    for provider in ("imdbid", "tmdbid", "tvdbid", "anidbid"):
        value = str(override.get(provider) or "").strip()
        if value:
            provider_ids[provider] = value

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO curator_title_overrides "
            "(hash, kind, title, series, year, "
            "external_id, provider_ids_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thash,
                kind,
                str(override.get("title") or "").strip() or None,
                str(override.get("series") or "").strip() or None,
                override.get("year"),
                str(override.get("id") or "").strip() or None,
                stable_json(provider_ids),
                _now_iso(),
            ),
        )


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _readable_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or _HASH_RE.fullmatch(name):
        return ""
    return name
