"""SQLite database setup, schema migrations, and repository helpers."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
]

def connect(path: Path | str) -> sqlite3.Connection:
    """Open the DB with WAL mode and return the connection."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
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
    version = _current_version(conn)
    pending = [(v, sql) for v, sql in _MIGRATIONS if v > version]
    if not pending:
        return
    with conn:
        for v, sql in pending:
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    conn.execute(statement)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at)"
                " VALUES (?, datetime('now'))",
                (v,),
            )
    logger.info("Applied %d migration(s), now at version %d", len(pending), pending[-1][0])


def migrate_legacy_files(conn: sqlite3.Connection, state_dir: Path) -> None:
    """Import legacy JSON files into DB tables if the tables are empty."""
    _migrate_torrent_cache(conn, state_dir)
    _migrate_archive(conn, state_dir)
    _migrate_library_snapshot(conn, state_dir)
    _migrate_curator_mapping(conn, state_dir)
    _migrate_curator_report(conn, state_dir)


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
                "INSERT OR REPLACE INTO torrents (id, signature_json, info_json, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    torrent_id,
                    json.dumps(entry.get("signature", {})),
                    json.dumps(entry.get("info", {})),
                    now,
                ),
            )
    _rename_migrated(path)
    logger.info("Imported %d torrent(s) from %s", len(data), path.name)


def _migrate_archive(conn: sqlite3.Connection, state_dir: Path) -> None:
    count = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
    if count:
        return
    path = state_dir / "trashcan.json"
    data = _load_json_file(path)
    if not isinstance(data, dict) or not data:
        return
    with conn:
        for thash, entry in data.items():
            if not isinstance(entry, dict):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO archive (hash, name, bytes, files_json, deleted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    thash,
                    entry.get("name"),
                    entry.get("bytes"),
                    json.dumps(entry.get("files", [])),
                    entry.get("deleted_at", _now_iso()),
                ),
            )
    _rename_migrated(path)
    logger.info("Imported %d archive entry/entries from %s", len(data), path.name)


def _migrate_library_snapshot(conn: sqlite3.Connection, state_dir: Path) -> None:
    count = conn.execute("SELECT COUNT(*) FROM library_snapshot").fetchone()[0]
    if count:
        return
    path = state_dir / "library_snapshot.json"
    data = _load_json_file(path)
    if not isinstance(data, dict) or not data:
        return
    from .utils import stable_json
    from ..core.state import canonical_snapshot
    digest = stable_json(canonical_snapshot(data))
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO library_snapshot"
            " (singleton, snapshot_json, digest, generated_at) VALUES (1, ?, ?, ?)",
            (json.dumps(data), digest, data.get("generated_at", _now_iso())),
        )
    _rename_migrated(path)
    logger.info("Imported library snapshot from %s", path.name)


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
    logger.info("Imported %d mapping entry/entries from %s", len(data), path.name)


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
    logger.info("Imported curator report from %s", path.name)


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
        logger.info("Imported %d subtitle sidecar(s) from %s", imported, subtitle_root)


# ---------------------------------------------------------------------------
# Repository helpers used by business-logic modules
# ---------------------------------------------------------------------------


def load_curator_mapping(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Return the current curator mapping rows ordered by target path."""
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
    """Replace all curator mapping rows in one transaction."""
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


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
