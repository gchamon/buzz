# `buzz.core`

Core subsystems of buzz and how they relate to the central **buzz state**.

This README is the module map. For the narrative description of how these
subsystems coordinate at runtime — provider polling, task execution, hooks,
stream resolution, and operator-visible state — see
[`../../docs/architecture/subsystems.md`](../../docs/architecture/subsystems.md).
For the package overview and database schema, see
[`../README.md`](../README.md).

## Subsystem map

| Module | Responsibility |
| --- | --- |
| [`state.py`](state.py) | `BuzzState`: the central thread-safe state container. Torrent cache, archive, library snapshot, file selections, stream resolution, background tasks, provider sync, hook/curator triggers. |
| [`db.py`](db.py) | SQLite setup, schema migrations, legacy-file import, and the repository helpers that read/write every table. |
| [`providers.py`](providers.py) | Provider-neutral contract: the `ProviderClient` protocol, normalized dataclasses, and error types. See [`../providers/README.md`](../providers/README.md). |
| [`curator.py`](curator.py) | Curated symlink-library construction, change detection, metadata overrides, and media-server orchestration used by `buzz-curator`. |
| [`media.py`](media.py) | Media-file detection and filename parsing: `is_video_file`, `is_sidecar_file`, `parse_movie`, `parse_show`. |
| [`media_server.py`](media_server.py) | Jellyfin integration: auth validation, library discovery, scan triggering, selective refresh. |
| [`subtitles.py`](subtitles.py) | OpenSubtitles.com client: search, rank, download, overlay management, and background fetch. |
| [`events.py`](events.py) | In-memory ring-buffer event registry; thread-safe structured logging with UI listeners. Surfaced in the **Logs UI** (`/logs`). Public write API: [`buzz.events.log()`](../events.py). |
| [`constants.py`](constants.py) | Shared regexes/extension sets (video extensions, show patterns, sidecar extensions, noise/year regexes). |
| [`utils.py`](utils.py) | Helpers: UTC timestamps, byte formatting, path normalization, stable JSON, magnet parsing. |
| [`tls.py`](tls.py) | Self-signed TLS certificate generation and renewal for the DAV HTTPS UI. |

## Buzz state (`BuzzState`)

`BuzzState` in [`state.py`](state.py) is the single, thread-safe (`threading.
RLock`) container that everything in `buzz-dav` reads from and writes through.

Held state:

| Attribute | Meaning |
| --- | --- |
| `config` | The effective `DavConfig` (tokens, intervals, curator URL, hooks). |
| `clients` | Dict of `ProviderClient` instances keyed by provider name. |
| `cache` | In-memory torrent cache, mirroring the persisted torrent rows. |
| `archive` | Archived/deleted torrents keyed by hash, for restore. |
| `snapshot` / `snapshot_digest` | The current virtual WebDAV tree and its stable fingerprint. |
| `file_selections` | Per-torrent selected file paths (survives restarts). |
| `category_overrides` | User category overrides (`movies`/`shows`/`anime`). |
| `resolved_urls` | Cache of resolved provider stream URLs. |
| `stream_sources` | Index of provider stream refs by source URL. |
| `background_tasks` | A `BackgroundTaskPool` for async operations. |

### The snapshot

The **snapshot** is buzz's virtual WebDAV filesystem — the thing `dav_protocol`
serves and the thing curator rebuilds against. It is a plain dict with:

- `generated_at` — ISO timestamp.
- `dirs` — list of directory paths (e.g. `movies`, `shows/Breaking Bad`).
- `files` — map of path → node metadata (`type`, `size`, `mime_type`,
  `modified`, `etag`; streamed files carry a source ref, synthetic files like
  `version.txt` carry inline `content`).
- `report` — counts (movies, show files, anime files, unplayable, torrents).

`LibraryBuilder` ([`state.py`](state.py)) builds a snapshot from provider
torrent details, classifying files via [`media.py`](media.py) and honoring
`category_overrides`. The snapshot is persisted to the `library_snapshot` table;
its `digest` drives change detection.

## How the subsystems relate to state

- **`db` ↔ state.** On startup `BuzzState` opens the connection, applies
  migrations, and loads cache, archive, snapshot, file selections, and category
  overrides. Each mutation is written back through `db.py` helpers
  (`replace_provider_library`, `save_file_selection`, `save_category_override`,
  snapshot save, …).
- **`providers` ↔ state.** `clients` is built in provider-priority order
  (`_ordered_clients()`). `sync()` fetches summaries then details across the
  ordered clients, and `_build_torrent_cache()` does hit/miss detection so only
  changed torrents are refetched, merging results (first provider wins).
- **`events` ↔ state.** State emits structured events via `record_event()` into
  the ring buffer for sync progress, hook execution, task lifecycle, and errors;
  UI listeners subscribe through the registry.
- **`curator` / `media_server` ↔ state.** After a sync, state diffs the new
  snapshot against the old, derives the changed top-level roots, and triggers a
  rebuild — `_trigger_curator()` (HTTP POST to the curator) and/or `_run_hook()`
  (subprocess). The curator then drives the Jellyfin scan via
  [`media_server.py`](media_server.py).
- **`media` ↔ state.** `LibraryBuilder` uses `is_video_file`, `parse_movie`, and
  `parse_show` to categorize and name files while building the snapshot.
- **`subtitles` ↔ state.** State tracks background subtitle-fetch status;
  fetches run asynchronously against OpenSubtitles.
- **state → `dav_protocol`.** WebDAV serving calls back into state: `lookup()`,
  `list_children()`, `resolve_download_url()`, `status()`, `torrents()`.

### Background operations

`BackgroundTaskPool` runs state-mutating work off the request thread, keeping a
bounded history of recent `BackgroundTask`s with cooperative cancellation.
Typical tasks: delete torrent, infringing-file scan, cache (file) selection,
and archive restore. Each submits, updates cache/archive on success, and
triggers a curator rebuild where relevant.

## Lifecycle

**Init** (`BuzzState.__init__`): open SQLite → apply migrations → load cache,
archive, snapshot, selections, overrides → build snapshot indexes
(`_children_by_dir`, dirs set) → create the task pool → ready to serve.

**Sync cycle** (`Poller` on an interval, or one-shot `InitialSync` at startup):
fetch summaries from ordered providers → classify hits/misses → fetch details
for misses → merge caches → apply file selections → build a new snapshot via
`LibraryBuilder` → diff against the previous snapshot to find changed roots →
persist cache + snapshot → trigger hooks/curator for changed roots → notify UI
listeners.
