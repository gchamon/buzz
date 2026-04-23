# Migrate JSON Files to SQLite

## Status

done

## Outcome

All machine-managed persistent state — the torrent cache, the archived
(trashcan) entries, the library snapshot, the curator mapping and report, and
per-subtitle metadata — lives in a single SQLite database at
`{state_dir}/buzz.sqlite`. JSON files on disk are reserved for user-edited
configuration (`buzz.yml`, `buzz.overrides.yml`). Operators gain indexed
lookups, transactional multi-table updates, and a queryable store they can
inspect with the `sqlite3` CLI without parsing whole files. Existing installs
upgrade automatically on first run with no manual step and no data loss;
legacy JSON files are renamed to `<name>.migrated` as a safety trail.

## Decision Changes

- **One database, one file.** A single SQLite file at
  `{state_dir}/buzz.sqlite` holds all DAV and curator state. Subtitle metadata
  moves off the media tree into the same database, so `/mnt/buzz/subs` ends up
  containing only `.srt` files. This is a cleaner overlay and one fewer thing
  for operators to rsync or back up separately.
- **stdlib `sqlite3`, no ORM.** Hand-written SQL in a thin repository layer.
  This matches the codebase's "no-magic" style and keeps the dependency graph
  small. A new module `buzz/core/db.py` owns connection setup, the
  `schema_version` table, and the migration runner.
- **WAL mode, single writer.** The database is opened with
  `journal_mode=WAL`, `synchronous=NORMAL`, and `foreign_keys=ON`. Writes
  continue to go through the existing `BuzzState.lock` (`RLock`) — the app is
  already single-writer within a process, so we preserve that contract rather
  than sprinkle explicit transactions everywhere. WAL gives the `Poller` and
  the DAV request handlers concurrent reads without additional locking.
- **Schema versioning baked in from day one.** A `schema_version` table
  tracks applied migrations. Startup applies any pending migrations inside a
  single transaction before the app accepts requests. The initial migration
  is `001_initial`; future schema changes (e.g., shredding the snapshot blob
  into indexed tables) arrive as new numbered migrations, never as in-place
  edits to existing ones.
- **Import on first run, once.** On startup, if `buzz.sqlite` does not exist
  (or is present but has empty tables) and legacy JSON files are found in
  `state_dir`, the app reads them into the database in a single transaction,
  then renames each imported file to `<name>.migrated`. A subsequent startup
  finds the tables populated and skips the import; there is no separate
  migration command to run. A single log line per file reports the row count
  imported.
- **Atomic file writes retire.** `BuzzState._load_json` / `_write_json` and
  the `.tmp`-plus-`os.replace` pattern in `state.py` are deleted. All reads
  and writes of machine-managed state become SQL. WAL provides durability
  guarantees; no more `.tmp` files.
- **Tests use in-memory or tempfile databases.** Unit tests default to
  `sqlite3.connect(":memory:")` with the migrations applied against that
  connection, so they stay fast. Tests that need to prove durability or
  simulate startup migrations open a DB file inside `TemporaryDirectory()`.

## Main Quests

- **Database foundation** (`buzz/core/db.py`, new):
  - `connect(path: Path | str) -> sqlite3.Connection` — opens the DB, sets
    the pragmas (`journal_mode=WAL`, `synchronous=NORMAL`,
    `foreign_keys=ON`), installs `row_factory=sqlite3.Row`, and returns the
    connection.
  - `apply_migrations(conn)` — reads the current version from
    `schema_version`, applies any pending migrations from an in-module
    ordered list (each migration is a Python string of SQL), and records the
    new version in the same transaction.
  - `migrate_legacy_files(conn, state_dir)` — idempotent one-shot importer
    for `torrent_cache.json`, `trashcan.json`, `library_snapshot.json`,
    `mapping.json`, `report.json`. Each file is imported only if the
    corresponding table is empty and the file exists; on success the file is
    renamed to `<name>.migrated`.
  - `migrate_subtitle_sidecars(conn, subtitle_root)` — walks
    `subtitle_root` once for `*.buzz.json` files, imports each row into
    `subtitle_metadata`, and renames the sidecar to `<name>.migrated`. Runs
    lazily on the first curator rebuild after the upgrade rather than at
    startup, because `subtitle_root` may be large and network-mounted.

- **Schema** (migration `001_initial`):
  - `schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)`
    — migration tracking.
  - `torrents(id TEXT PRIMARY KEY, signature_json TEXT NOT NULL, info_json
    TEXT NOT NULL, updated_at TEXT NOT NULL)` — replaces
    `torrent_cache.json`. RD payloads stay opaque as JSON text for now; we
    do not shred RD fields into columns because the current code treats
    `info` as an opaque dict.
  - `archive(hash TEXT PRIMARY KEY, name TEXT, bytes INTEGER, files_json
    TEXT, deleted_at TEXT NOT NULL)` — replaces `trashcan.json`. Retains the
    same payload shape so the restore flow needs no additional reshaping.
  - `library_snapshot(singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    snapshot_json TEXT NOT NULL, digest TEXT NOT NULL, generated_at TEXT NOT
    NULL)` — replaces `library_snapshot.json`. Kept as a single-row table
    initially; a future migration can shred it into `snapshot_dirs` and
    `snapshot_files` if per-path queries become hot.
  - `curator_mapping(source TEXT NOT NULL, target TEXT PRIMARY KEY, type
    TEXT NOT NULL)` — replaces `mapping.json`. `target` is already unique in
    the existing JSON.
  - `curator_report(singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    report_json TEXT NOT NULL, generated_at TEXT NOT NULL)` — replaces
    `report.json`.
  - `subtitle_metadata(overlay_path TEXT PRIMARY KEY, file_id INTEGER NOT
    NULL, release TEXT, updated_at TEXT NOT NULL)` — replaces per-file
    `*.buzz.json` sidecars. The primary key is the path under
    `subtitle_root` (for example, `movies/Foo (2024)/Foo.en.srt`) so it is
    stable across mount-point changes.

- **Repository layer** (`buzz/core/state.py`, `buzz/core/subtitles.py`,
  `buzz/core/curator.py`):
  - In `state.py`, replace `BuzzState._load_json` / `_write_json` usage with
    private helper methods (`_load_cache`, `_save_cache_entry`,
    `_delete_cache_entry`, `_load_archive`, `_save_archive_entry`,
    `_load_snapshot`, `_save_snapshot`). Each helper wraps SQL in
    `with self.conn:` for implicit transactions. The in-memory
    `self.cache`, `self.trashcan`, and `self.snapshot` are retained as
    read-through caches hydrated from the DB at startup.
  - In `subtitles.py`, replace `_subtitle_meta_path` /
    `_read_subtitle_meta` / `_write_subtitle_meta` with
    `get_subtitle_metadata(conn, overlay_path)` and
    `upsert_subtitle_metadata(conn, overlay_path, meta)`. The
    `.{lang}.srt.buzz.json` sidecar files disappear entirely.
  - In `curator.py`, replace the `mapping.json` and `report.json` writes at
    `curator.py:281-286` with a single-transaction wipe-and-insert into
    `curator_mapping` / `curator_report`.
    `load_previous_mapping` (`curator.py:155`) becomes
    `SELECT source, target, type FROM curator_mapping`.

- **Connection lifetime**:
  - `BuzzState` and `Curator` each hold a single `sqlite3.Connection` opened
    in their `__init__`. Python's default `check_same_thread=True` is
    insufficient for the `Poller` / `InitialSync` threads, so a small
    `_get_conn()` helper lazily opens per-thread connections keyed by
    `threading.get_ident()`; all share the same file and benefit from WAL
    for concurrent reads.

- **Tests** (`tests/test_db.py` new; updates to `tests/test_buzz.py`,
  `tests/test_curator_app.py`, `tests/test_subtitles.py`):
  - `test_schema_migration_applies_and_is_idempotent` — running
    `apply_migrations` twice leaves the schema at the same version.
  - `test_legacy_json_import_renames_files_to_migrated` — seeds legacy files
    into a tempdir, runs `migrate_legacy_files`, asserts rows and the
    `.migrated` rename.
  - `test_legacy_import_skipped_when_tables_nonempty` — pre-populated tables
    are not overwritten.
  - `test_subtitle_metadata_upsert_roundtrip`.
  - `test_curator_mapping_replace_replaces_all_rows`.
  - Existing persistence tests in `test_buzz.py`,
    `test_curator_app.py`, and `test_subtitles.py` are updated to assert
    database rows instead of JSON file contents, still wrapped in
    `TemporaryDirectory()` for isolation.

- **Documentation**:
  - Add a short "Database access" section to `AGENTS.md`: open the DB via
    `db.connect()`, write through the repository helpers, never hand-write
    ad-hoc SQL in business-logic files.
  - Add a one-liner to the operations section of `README.md` showing how
    operators can inspect state:
    `sqlite3 $STATE_DIR/buzz.sqlite ".tables"`.

## Acceptance Criteria

- A fresh install starts with no JSON files and an empty `buzz.sqlite`. The
  first Real-Debrid sync populates `torrents` and `library_snapshot`; a
  curator rebuild populates `curator_mapping` and `curator_report`; a
  subtitle fetch writes rows into `subtitle_metadata`.
- An upgrade install starts with legacy JSON files present and no DB.
  Migrations run inside a single transaction, the tables are populated, and
  the legacy files are renamed to `<name>.migrated`. A second startup is a
  no-op on the migration (no re-import, no errors).
- Subtitle behaviour end-to-end matches the current
  smart-replace contract: when an overlay exists and its stored `file_id`
  matches the best OpenSubtitles result, the fetch skips; when the `file_id`
  differs, the fetch replaces the subtitle and updates the row. After the
  subtitle migration runs, zero `*.buzz.json` files remain under
  `subtitle_root`.
- `buzz.sqlite` is the only machine-managed persistence artifact in
  `state_dir`. No `.tmp` files linger after writes.
- `uv run pytest tests/` passes (including the new tests).
- `uv run pyright buzz/` reports zero errors.
- `uv run ruff check buzz/` reports zero violations.

## Metadata

### id

migrate-json-to-sqlite

### type

Issue
