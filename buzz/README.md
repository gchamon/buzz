# `buzz` package

Code-level guide to the `buzz` Python package. This documents the
implementation — modules, database schema, application entry points, and how
the pieces fit together. For runtime topology, service boundaries, and the
*why* behind the design, see the architecture docs:

- [`../docs/architecture/system.md`](../docs/architecture/system.md) — system
  architecture, runtime topology, deployment.
- [`../docs/architecture/subsystems.md`](../docs/architecture/subsystems.md) —
  internal state subsystems coordinated by `BuzzState`.

Buzz exposes a Real-Debrid / TorBox account as a read-only WebDAV tree (mounted
through rclone) and curates that tree into a media-server-friendly symlink
library. It runs as two cooperating processes that share one SQLite database:

- **`buzz-dav`** — talks to the providers, serves WebDAV, hosts the operator UI.
- **`buzz-curator`** — builds the curated symlink library and refreshes the
  media server.

## Code layout

| Path | Role |
| --- | --- |
| [`dav_app.py`](dav_app.py) | `DavApp` — FastAPI app for `buzz-dav`: WebDAV serving, operator UI, provider wiring, polling, TLS. |
| [`dav_protocol.py`](dav_protocol.py) | WebDAV XML generation, upstream stream resolution, transient-error classification. |
| [`curator_app.py`](curator_app.py) | `CuratorApp` — FastAPI sidecar for `buzz-curator`: symlink rebuilds, media-server refresh, subtitles. |
| [`models.py`](models.py) | Pydantic config models (`DavConfig`, `CuratorConfig`, nested config) and shared typed structures. |
| [`ui_live.py`](ui_live.py) | PyView live-view classes that back the operator UI. The console bar is written via `buzz.console`; the Logs UI is fed by `buzz.events`. |
| [`console.py`](console.py) | Write API for the per-page console status bar (`buzz.console.log`). |
| [`events.py`](events.py) | Write API for structured event records shown in the Logs UI (`buzz.events.log`). Facade over `buzz.core.events`. |
| [`core/`](core/README.md) | Core subsystems: state, DB, providers contract, curator logic, media parsing, events, subtitles, TLS. See [`core/README.md`](core/README.md). |
| [`providers/`](providers/README.md) | Concrete provider clients (Real-Debrid, TorBox). See [`providers/README.md`](providers/README.md). |
| [`pyview_templates/`](pyview_templates/README.md) | Jinja2 templates for the operator UI. See [`pyview_templates/README.md`](pyview_templates/README.md). |

## Database

State lives in a single SQLite database (`buzz.sqlite`) shared by both
processes. The connection ([`core/db.py:connect`](core/db.py)) uses **WAL**
journaling, `synchronous=NORMAL`, foreign keys on, and `IMMEDIATE` isolation so
that `buzz-dav` and `buzz-curator` starting at the same time don't deadlock when
checking/applying migrations.

Conceptually, `buzz.sqlite` is *machine state* (regenerable from the providers);
the `.yml` files are *user config*.

### Tables

| Table | Owner | Purpose | Key columns |
| --- | --- | --- | --- |
| `schema_version` | shared | Tracks applied migrations. | `version` (PK), `applied_at` |
| `torrents` | `buzz-dav` | Legacy per-provider torrent cache: API signature + info payloads. | `id` (PK), `signature_json`, `info_json`, `magnet`, `updated_at` |
| `archive` | `buzz-dav` | Legacy archived/deleted torrent metadata for restore. | `hash` (PK), `name`, `bytes`, `files_json`, `magnet`, `deleted_at` |
| `library_snapshot` | `buzz-dav` | Canonical WebDAV tree snapshot + digest for change detection (single row). | `singleton`=1 (PK), `snapshot_json`, `digest`, `generated_at` |
| `curator_mapping` | `buzz-curator` | Source → target symlink mapping for the curated tree. | `target` (PK), `source`, `type` |
| `curator_report` | `buzz-curator` | Last rebuild report (counts, scan scope) (single row). | `singleton`=1 (PK), `report_json`, `generated_at` |
| `subtitle_metadata` | `buzz-curator` | Downloaded subtitle file id + release keyed by overlay path. | `overlay_path` (PK), `file_id`, `release`, `updated_at` |
| `opensubtitles_languages` | `buzz-dav` | Cached OpenSubtitles language list for the config UI. | `code` (PK), `name` |
| `opensubtitles_languages_meta` | `buzz-dav` | Fetch timestamp for the cached language list (single row). | `singleton`=1 (PK), `fetched_at` |
| `library_entries` | shared | Provider-neutral torrent metadata keyed by content hash (v4+). | `hash` (PK), `name`, `bytes`, `files_json`, `magnet`, `deleted_at`, `updated_at` |
| `provider_links` | shared | Per-provider tracking rows that point at a `library_entries.hash` (v4+). | (`provider`, `provider_torrent_id`) PK, `hash` (FK → `library_entries`, cascade), `status`, `progress`, `info_json`, `signature_json`, `updated_at` |
| `file_selections` | shared | User per-file selection per torrent (v5+). | (`hash`, `path`) PK, `selected`, `updated_at` |
| `category_overrides` | shared | User category override per torrent (v6+). | `hash` (PK), `category` ∈ {`movies`,`shows`,`anime`}, `updated_at` |

The provider-neutral pair (`library_entries` + `provider_links`) is the current
model: one library entry per torrent hash, with one or more provider links
tracking where that content lives. The older `torrents`/`archive` tables remain
for backward-compatible reads and were the source for the v4 backfill.

Repository helpers (the typed read/write API over these tables) live in
[`core/db.py`](core/db.py) — e.g. `replace_provider_library`,
`load_file_selections`, `save_category_override`, `load_curator_mapping`.

### Migrations

Migrations are a small **custom** system (not Alembic): an ordered
`_MIGRATIONS: list[tuple[int, str]]` of `(version, sql)` in
[`core/db.py`](core/db.py). `apply_migrations(conn)` opens an `IMMEDIATE`
transaction, reads the current version from `schema_version`, runs each pending
migration's statements, and records the new version with `applied_at`.

| Version | Change |
| --- | --- |
| 1 | Initial schema: `schema_version`, `torrents`, `archive`, `library_snapshot`, `curator_mapping`, `curator_report`, `subtitle_metadata`. |
| 2 | Add `magnet` column to `torrents` and `archive`. |
| 3 | Add `opensubtitles_languages` and `opensubtitles_languages_meta`. |
| 4 | Add provider-neutral `library_entries` + `provider_links`; backfill from legacy tables via `_backfill_provider_library`. |
| 5 | Add `file_selections`. |
| 6 | Add `category_overrides`. |

**Legacy JSON import.** `migrate_legacy_files(conn, state_dir)` imports
pre-SQLite JSON state (`torrent_cache.json`, `archive.json`,
`library_snapshot.json`, `mapping.json`, `report.json`) — but only when the
target table is empty — then renames each consumed file to `*.migrated`.
`migrate_subtitle_sidecars` does the same for `*.buzz.json` subtitle sidecars.

## `dav_app`

[`dav_app.py`](dav_app.py) defines `DavApp`, the FastAPI application for the
`buzz-dav` service. It is the only component that talks to the providers.
Responsibilities:

- **WebDAV** — serves a read-only tree under `/dav/{path}` (PROPFIND/GET/HEAD)
  for rclone to mount.
- **State ownership** — constructs and owns the `BuzzState` instance
  ([`core/state.py`](core/state.py)) that holds the cache, snapshot, archive,
  selections, and stream resolution.
- **Polling** — runs a `Poller` thread that calls `BuzzState.sync()` on the
  configured interval.
- **Operator UI** — mounts the PyView UI (`/`, `/cache`, `/archive`, `/logs`,
  `/threads`, `/config`) via `build_ui(self)`.
- **Provider wiring** — `_build_provider_clients` / `_build_provider_client`
  instantiate the configured clients in `provider_priority` order.
- **APIs** — config read/write/restore (`/api/config`), curator rebuild trigger
  (`/api/curator/rebuild`), subtitle fetch, logs, and `/healthz` / `/readyz`.
- **TLS** — a companion app serves the HTTPS UI with an auto-renewing
  self-signed certificate.

A FastAPI `lifespan` context manager orchestrates startup (TLS, DB migrations,
state init, polling threads) and graceful shutdown.

### Background task kinds

Long-running operations are tracked as background tasks visible in the `/threads`
UI. Each task has a `kind` that describes what it does:

| Kind | Color | Description |
| --- | --- | --- |
| `cache` | cyan | Provider cache sync — fetches torrent list from a provider. |
| `curator` | purple | Curator rebuild — triggers the curator sidecar to rescan and relink the media library. |
| `delete` | red | Permanent delete — removes a torrent from a provider or the archive. |
| `hook` | green | Media update hook — runs the configured external command after library changes. |
| `maintenance` | yellow | Provider maintenance — migrations, scans, or transfers between providers. |
| `restore` | cyan | Archive restore — re-adds an archived torrent to a provider. |
| `subtitles` | cyan | Subtitle fetch — downloads and overlays subtitle files via OpenSubtitles. |
| `sync` | yellow | Full library sync — reconciles provider state with the local cache. |

## `dav_protocol`

[`dav_protocol.py`](dav_protocol.py) holds the WebDAV protocol mechanics, kept
separate from `DavApp` and stateless where possible (it delegates stateful
operations back to `BuzzState`):

- `propfind_body(snapshot)` — builds the PROPFIND XML directory listing from a
  library snapshot.
- `open_remote_media(...)` — opens an upstream provider stream with a defensive
  SSL context, content-type validation, and HTTP Range support
  (`_HttpxStreamAdapter` bridges httpx streaming to a urllib-style read/close).
- `is_transient_stream_error(exc)` — classifies upstream failures so transient
  transport errors (TLS, connection timeouts) can be retried while permanent
  errors surface.

## `curator_app`

[`curator_app.py`](curator_app.py) defines `CuratorApp`, the FastAPI sidecar for
`buzz-curator`. It owns the curated symlink library and the media server
integration; it reads shared SQLite state and coordinates with `buzz-dav` over
HTTP.

**Rebuild flow** (`POST /rebuild`, optionally scoped to changed roots):

1. Scan the rclone-mounted source root (`/mnt/buzz/raw`).
2. Classify entries into `movies` / `shows` / `anime` using the media parsers
   (`parse_movie`, `parse_show`, anime patterns) from
   [`core/media.py`](core/media.py).
3. Build a temporary symlink tree.
4. Apply the persistent subtitle overlay from `/mnt/buzz/subs`.
5. Atomically swap in the new tree, **preserving unchanged symlinks** to avoid
   inode churn.
6. Persist `curator_mapping` and `curator_report` in SQLite.
7. Trigger a Jellyfin/Plex library scan (selective when the changed roots map to
   specific libraries).
8. Optionally kick off background subtitle fetching.

Supporting pieces:

- `SourceRootWatcher` — optional thread that watches the source root and
  triggers rebuilds on change.
- **Scan-probe safety** — validates that files are readable and the rclone VFS
  is visible before triggering a media-server scan, preventing false deletions.
- **Config reload** — `POST /api/config/reload` lets `buzz-dav` hot-reload the
  curator's config after UI-driven changes.
