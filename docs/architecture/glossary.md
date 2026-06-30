# Glossary

This document defines project-specific terminology used across the buzz codebase
and documentation. Terms that appear in the operator UI, source code identifiers,
event logs, or configuration keys are listed here with their precise meaning in
this project's context.

For runtime architecture, see [system.md](system.md). For internal subsystem
design, see [subsystems.md](subsystems.md).

---

## UI Pages

The operator UI exposes five pages, identified by the `PageName` literal in
`buzz.ui_live`:

| Page | Description |
| --- | --- |
| `cache` | Live view of the provider torrent cache: status, file selection, stream links, identity overrides, and subtitle controls. |
| `archive` | Soft-deleted torrent records with options to restore or permanently delete. |
| `logs` | Scrollable event log with level filtering and task-scoped grouping. |
| `threads` | Background task monitor: status, kind, label, detail, timing, logs, and accept/cancel actions. |
| `config` | Live configuration editor for `buzz.yml` fields, with per-field dirty and override indicators. |

---

## Background Tasks

A **task** (called a **thread** in the UI) is an async unit of work managed by
`BackgroundTaskPool` and visible on the `/threads` page. Tasks are submitted by
`BuzzState` methods for work that involves provider API calls, file operations,
or multi-step sequences too slow to run inline.

A **manual task** (`submit_manual()`) starts in `pending` state and waits for
explicit operator acceptance before running. Non-manual tasks start immediately
in a daemon thread.

**Cooperative cancellation**: tasks receive a `threading.Event` and must check
it at safe points. The pool does not forcibly stop threads. When cancelled, a
worker raises `RuntimeError("cancelled")` after seeing the event, and the pool
records the task as `cancelled`.

See [subsystems.md — Background Task Subsystem](subsystems.md#background-task-subsystem).

### Task Statuses

The `TaskStatus` literal enumerates all valid task states:

| Status | Meaning |
| --- | --- |
| `pending` | Manual task registered, waiting for operator acceptance. |
| `queued` | Task accepted and waiting to start (currently unused; tasks start immediately on submit). |
| `running` | Task thread is active. |
| `cancelling` | Cancellation has been requested; thread has not yet acknowledged it. |
| `cancelled` | Task was cancelled before or during execution. |
| `complete` | Task finished successfully. |
| `failed` | Task raised an unhandled exception or was explicitly failed. |
| `aborted` | Task was terminated abnormally (e.g. process shutdown). |

### Task Kinds

The `kind` field classifies what a task does:

| Kind | Description |
| --- | --- |
| `hook` | Runs the configured `on_library_change` shell hook and/or triggers a curator rebuild after a library change. |
| `curator` | Requests a full or partial curator rebuild (`resync_lib`). |
| `subtitles` | Fetches subtitles for selected torrents via the OpenSubtitles API. |
| `maintenance` | Administrative operations: provider migration scans/commits, Real-Debrid infringing file scans and cleanup. |
| `cache` | Applies a bulk file selection across one or more torrents. |
| `sync` | Runs a provider sync pass (`BuzzState.sync()`). |
| `restore` | Restores an archived torrent from the archive back to the active provider. |
| `delete` | Removes a torrent from the provider and archives its local metadata. |

### Task Label Convention

Task labels use the format `snake_case_verb` optionally followed by `: detail`:

```
resync_lib: 3 roots
fetch_subtitles: 5 torrents
remove_from_cache: My.Movie.2023
```

The prefix identifies the operation; the detail after `: ` carries the variable
part (count, name, or scope). The `/threads` UI splits these into a **Label**
column and a **Detail** column.

All current labels:

| Label | Kind | Detail |
| --- | --- | --- |
| `cache_selections` | `cache` | `{n} torrent(s)` |
| `cleanup_rd_infringing` | `maintenance` | `{n} torrent(s)` |
| `commit_provider_migration` | `maintenance` | `{n} torrent(s) {src} -> {dst}` |
| `fetch_subtitles` | `subtitles` | torrent name, `{n} torrents`, or `full_library` |
| `remove_from_cache` | `delete` | torrent name |
| `resync_lib` | `curator` | `{n} roots` or empty |
| `restore_archive` | `restore` | torrent name |
| `scan_provider_migration` | `maintenance` | `{src} -> {dst}` |
| `scan_rd_infringing` | `maintenance` | — |
| `sync_cache_files_selected` | `sync` | — |
| `update_hook` | `hook` | `{n} roots` |

---

## Hook Queue

A **hook** is the reaction to a detected library change. When `BuzzState.sync()`
finds modified roots, it hands them to the hook queue rather than running hooks
inline. This separates external provider polling from hook execution timing.

A **hook batch** is the set of changed roots coalesced into a single hook run.
If more paths arrive while a batch is executing, they are merged into the next
batch rather than starting a parallel run.

**Hook phases** describe the current state of the hook runner:

| Phase | Meaning |
| --- | --- |
| `idle` | No hook activity. |
| `queued` | A batch has been queued; the runner thread has not started yet. |
| `waiting_delay` | Waiting for the configured `rd_update_delay_secs` settle time before proceeding. |
| `waiting_vfs` | Waiting for changed paths to become visible through the rclone VFS mount. |
| `triggering_curator` | Sending a rebuild request to `buzz-curator`. |
| `running_hook` | Executing the `on_library_change` shell command. |
| `complete` | Most recent batch finished successfully. |
| `failed` | Most recent batch ended with an error. |

See [subsystems.md — Hook Queue](subsystems.md#hook-queue).

---

## Providers

A **provider** is an external debrid or torrent service that hosts and streams
content. Buzz communicates with providers through a uniform `ProviderClient`
protocol that abstracts listing torrents, fetching detail, resolving streams,
adding magnets, selecting files, and deleting torrents.

The `ProviderKind` literal enumerates supported providers:

| Kind | Service |
| --- | --- |
| `real_debrid` | Real-Debrid (`real-debrid.com`) |
| `torbox` | TorBox (`torbox.app`) |

**Provider torrent ID**: the provider-assigned identifier for a torrent within
that service. Not portable across providers.

**Cache key**: the local storage key for a torrent record. For TorBox entries
the format is `torbox:<provider_torrent_id>`; for Real-Debrid entries it is the
bare provider torrent ID with no prefix.

**Hash**: the content hash of a torrent (from the info hash). Portable across
providers and used as the stable key for file selections, category overrides, and
multi-provider deduplication. When a torrent exists on multiple providers, buzz
uses the hash to link them.

**Provider priority**: when both providers are enabled, the configured
`provider_priority` list controls which provider is preferred for stream
resolution and restore operations.

### Real-Debrid Error Codes

Non-transient errors are cached so buzz does not re-resolve a known-bad stream:

| Code | Meaning |
| --- | --- |
| `hoster_unavailable` | The file's hoster is not available on Real-Debrid. |
| `hoster_unsupported` | The hoster is not supported by Real-Debrid. |
| `hoster_too_many_active_downloads` | Download concurrency limit reached. |
| `hoster_not_free` | Hoster requires a premium Real-Debrid account feature. |
| `file_unavailable` | The file is no longer available at the hoster. |
| `infringing_file` | Real-Debrid has flagged this file as infringing; stream resolution is blocked. |

### Provider Stream Error Codes

| Code | Behaviour |
| --- | --- |
| `http_429` | Transient rate-limit; buzz retries with backoff. |
| `http_422` | Non-transient unprocessable response; cached as a stream failure. |

---

## Cache and Archive

### Cache

The **cache** is the in-memory torrent detail map maintained by `BuzzState` and
persisted in the `torrents` SQLite table. Each entry stores the provider's torrent
info payload, a signature for change detection, and the last-updated timestamp.
The cache is keyed by **cache key**.

A **cache entry** is one record in the cache: the full torrent detail (name,
hash, file list with paths and download links, status, progress) for a single
provider torrent. Cache entries are loaded at startup and updated incrementally
as provider sync detects changes.

### Archive

The **archive** is the `archive` SQLite table that holds metadata for torrents
that have been deleted from the provider.
Archived entries can be **restored** (re-added to the provider and removed from
the archive) or **permanently deleted** (metadata dropped, no provider action).

### File Selection

**File selection** is the per-torrent choice of which provider files to include
in the WebDAV tree. Selections are stored in SQLite by hash so they persist
across provider changes and survive torrent re-adds. Unselected files are not
exposed through WebDAV and are not downloaded.

### Category Override

A **category override** is a manually assigned category name for a torrent
hash. Overrides take precedence over the automatic detection logic that
classifies by filename pattern.

### Subtitle Query Override

A **subtitle query override** is a per-file custom search term used when
fetching subtitles for that file. Overrides replace the default filename-derived
query.

---

## Library and Snapshot

### Snapshot

A **snapshot** is the complete WebDAV filesystem tree built by `BuzzState.sync()`
from the current provider cache. It encodes all directories and virtual file
entries that `buzz-dav` will serve under `/dav`.

A **canonical snapshot** is the snapshot stripped of volatile or runtime-only
fields (such as resolved URLs) so it can be hashed for change detection. Buzz
stores the canonical snapshot digest in `library_snapshot`; a sync pass only
writes the new snapshot and notifies hooks when the digest changes.

### Library Categories

The WebDAV tree groups playable content under named category directories:

| Category | Contents |
| --- | --- |
| `movies` | Standalone movie files, classified by filename heuristics. |
| `shows` | TV series files, detected by `S##E##` or `##x##` episode patterns. |
| `anime` | Anime series, detected by patterns in the `directories.anime` config list. |
| `__all__` | Virtual directory containing all playable files regardless of category. |
| `__unplayable__` | Files that cannot be played, grouped for diagnostics. Only present when `compat.enable_unplayable_dir` is enabled. |

### Unplayable Reasons

A file is placed in `__unplayable__` for one of these reasons:

| Reason | Meaning |
| --- | --- |
| `no_selected_files` | No files have been selected for the torrent. |
| `status={status}` | The torrent's provider status is not `downloaded` (e.g. `status=unknown`). |
| `no_playable_video_files` | The selected files contain no recognised video formats. |
| `missing_download_link` | A selected file has no download URL from the provider. |

See [system.md — Runtime Topology](system.md#runtime-topology) and
[system.md — State Model](system.md#state-model).

---

## Curator and Identity

**buzz-curator** is the sidecar service that builds the clean symlink library.
It scans `/mnt/buzz/raw` (the rclone-mounted WebDAV tree), creates symlinks under
`/mnt/buzz/curated`, overlays subtitle files from `/mnt/buzz/subs`, and triggers
Jellyfin or Plex library refreshes. It runs in a separate process and communicates
with `buzz-dav` over HTTP.

A **curator rebuild** (also called a **resync**) is the full pass that recreates
all symlinks under `/mnt/buzz/curated` from the current WebDAV source. Partial
rebuilds scope to a subset of changed roots. The `curator_mapping` SQLite table
records every source-to-target symlink path so the previous mapping can be
diffed against the new one.

**Curator title override** (shown as **Identity override** in the UI) is a
per-entry metadata record stored in `curator_title_overrides`. It can override
the title, year, and provider IDs (IMDb, TVDB, TMDb, AniDB) used to name the
curated directory and identify the entry to Jellyfin or Plex. When an override
is active, the identity section in the cache entry UI highlights the
overridden fields.

See [system.md — Curator Service Internals](system.md#curator-service-internals).

---

## Event Log

An **event** is a structured log entry emitted by `buzz.core.events.record_event()`
and stored in the process-local `EventRegistry`. Events have:

- **level**: `debug`, `info`, `warning`, or `error`.
- **message**: human-readable description.
- **timestamp**: emission time.
- **task scope**: optional task id; events emitted inside a task thread are
  attached to that task and shown in the task's log on `/threads`.
- **event code**: the `event=` keyword argument — a `snake_case` string used
  for machine-readable classification and filtering.

### Event Code Groups

Event codes are grouped by the subsystem that emits them:

| Group | Representative codes |
| --- | --- |
| Provider | `provider_add_fallback`, `provider_delete_failed`, `provider_detail_refresh_failed`, `provider_stream_fallback`, `provider_stream_unavailable` |
| Provider migration | `provider_migration_scan_complete`, `provider_migration_added`, `provider_migration_skipped`, `provider_migration_item_failed` |
| Real-Debrid | `rd_detail_retry`, `rd_hoster_unavailable`, `rd_infringing_scan_started`, `rd_infringing_scan_complete`, `rd_infringing_file_detected`, `rd_stream_exhausted`, `rd_stream_failed`, `rd_stream_retry` |
| Hook | `hook_queued`, `hook_batch_started`, `hook_waiting_delay`, `hook_waiting_vfs`, `hook_vfs_visible`, `hook_vfs_timeout`, `hook_triggering_curator`, `hook_running_command`, `hook_command_finished`, `hook_curator_queued`, `hook_curator_accepted` |
| Curator | `curator_ready`, `curator_rebuild_complete`, `curator_mapping_diff`, `curator_source_changes_detected`, `curator_config_reloaded`, `curator_config_reload_failed` |
| Thread (task) | `thread_started`, `thread_complete`, `thread_failed`, `thread_cancelled` |
| Sync / library | `startup_sync`, `library_update`, `torrent_detail_sync` |
| Jellyfin | `jellyfin_auth_validated`, `jellyfin_library_not_found`, `jellyfin_scan_probe_started`, `jellyfin_scan_probe_succeeded`, `jellyfin_scan_probe_failed`, `jellyfin_metadata_suggestion_failed` |

See [system.md — Event and Log Flow](system.md#event-and-log-flow).

---

## Configuration

Buzz configuration lives in `buzz.yml` and is loaded into Pydantic models in
`buzz.models`.

**Saved config** is `buzz.yml` as written on disk — the authoritative source of
truth for the operator's intent.

**Effective config** is the parsed and validated Pydantic model held in memory
by the running process. It may differ from the saved config if the file was
edited after startup and a reload has not yet occurred.

**UI override config** is the in-session draft held by the config page while
the operator is editing fields. It is not persisted until explicitly saved.
The config page shows per-field **dirty** (changed from effective config) and
**overridden** (different from the saved default) indicators.

**Hot-reloadable** fields apply immediately when the config file is rewritten
and buzz detects the change. Most behaviour fields are hot-reloadable.

**Restart-required** fields need a process restart to take effect:
`server.bind`, `server.port`, and TLS certificate/key paths.

See [system.md — Configuration Model](system.md#configuration-model).

---

## Subtitles

Buzz integrates with OpenSubtitles at two points: `buzz-dav` caches the
language list for the config UI, and `buzz-curator` fetches and places subtitle
files under `/mnt/buzz/subs` during or after a rebuild.

### Subtitle Strategies

The `subtitles.strategy` config key controls which subtitle is selected when
multiple candidates are available:

| Strategy | Selection logic |
| --- | --- |
| `best-match` | Closest language and format match to the video file. |
| `most-downloaded` | Highest download count on OpenSubtitles. |
| `best-rated` | Highest community rating. |
| `trusted` | From uploaders with trusted status. |
| `latest` | Most recently uploaded. |

### Subtitle Filters

| Filter | Effect |
| --- | --- |
| `hearing_impaired` | `exclude` removes HI subtitles; `include` allows them; `prefer` prioritises them. |
| `exclude_ai` | Filters out subtitles flagged as AI-generated. |
| `exclude_machine` | Filters out machine-translated subtitles. |
