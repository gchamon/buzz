# Runtime State Subsystems

This document describes the internal subsystems that manage mutable runtime
state inside buzz. The system-level architecture is documented in
[system.md](system.md).

## State Ownership

`buzz-dav` owns provider-facing state through `buzz.core.state.BuzzState`.
`DavApp` exposes HTTP and UI entrypoints, while `BuzzState` performs state
transitions against provider clients, SQLite, the WebDAV snapshot, hooks, and
UI-visible task state.

The operator UI does not own persistence or provider behavior. PyView live
views call `DavApp`/`BuzzState` methods and render snapshots from
`BuzzState.status()`, cache summaries, archive summaries, and event logs.

`buzz-curator` owns curated-library rebuild state in its own process. Shared
machine state is persisted in `buzz.sqlite`; process-local runtime state such
as active tasks, hook batches, and event rings is not shared across processes.

## Provider Sync And External State

Provider inventory is external state. Buzz periodically samples it, normalizes
it, persists a local view, and reacts only to meaningful changes.

- `Poller` is the long-running DAV-side polling thread. It waits on the
  configured provider interval or an explicit wake signal, then calls
  `BuzzState.sync()`.
- `InitialSync` runs once at startup without triggering hooks, then marks
  startup readiness complete.
- `BuzzState.sync()` compares provider torrent signatures with the persisted
  cache, rebuilds the canonical WebDAV snapshot, stores the snapshot digest,
  and records sync events.
- When sync detects changed roots and hooks are configured, it hands those
  roots to the hook queue instead of running hooks inline.

This separation keeps external provider polling, local state persistence, and
operator-triggered actions on the same state surface while avoiding duplicate
Real-Debrid or provider logic in the UI layer.

## Background Task Subsystem

`BackgroundTaskPool` is the small thread-backed task runner used for
operator-visible asynchronous work. The concrete pool lives on `BuzzState` as
`state.background_tasks`, so `/threads` represents DAV process tasks for that
state instance.

Task lifecycle:

- `submit()` queues work and starts it immediately in a daemon thread.
- `submit_manual()` registers pending work for explicit user acceptance.
- `start()` starts a pending manual task.
- `cancel()` sets a cooperative cancellation event and updates task status.
- `snapshot()` returns the UI-safe task list exposed through
  `BuzzState.status()["background_tasks"]`.

Tasks receive a `threading.Event` and must check it at safe points. Buzz does
not forcibly stop Python threads. Cancellation is cooperative: workers raise
`RuntimeError("cancelled")` after seeing the event, and the task pool records
the task as cancelled.

The `/threads` page reads the task snapshot, sorts active and recent tasks,
and delegates start/cancel actions back to `BuzzState`. Events recorded while
a task is running are scoped to the task id and displayed with that task.

## Hook Queue

Hooks react to local snapshot changes caused by provider sync or explicit
operator actions. They are separate from background tasks because they maintain
their own coalescing and delay semantics.

`BuzzState._enqueue_hook()` merges changed roots into a pending set and starts
one hook runner thread if none is active. `_run_hook_task()` drains pending
batches, waits for configured settle time, triggers curator and shell hooks,
records hook state, and loops again if more paths arrived while it was
running.

Hook status fields such as `hook_phase`, `hook_pending_paths`,
`hook_active_paths`, and timestamps are exposed through `BuzzState.status()`
for the UI meta bar and readiness context.

## Cache And Archive State

The provider cache, archive, selected files, stream sources, and library
snapshot are the core DAV process state. They are loaded from `buzz.sqlite`
during `BuzzState` initialization and updated through repository helpers in
`buzz.core.db`.

Main mutation surfaces:

- `add_magnet()` adds provider torrents and persists cache entries.
- `select_files()` applies provider file selection and updates local file
  selection state.
- `delete_torrent()` queues provider deletion, archives local metadata, and
  removes cache entries.
- `restore_archive()` restores archived items through provider priority and
  removes the archive entry.
- `delete_archive_permanently()` removes archive metadata without restoring.

Public UI/API methods either mutate state synchronously when the operation is
short or submit a background task when provider work, archive work, or follow-up
sync may take longer.

## Stream Resolution State

WebDAV reads resolve virtual file paths into provider stream references, then
into direct upstream URLs. `BuzzState` owns the state used by this path:

- `stream_sources` maps WebDAV source URLs to provider-specific stream
  sources.
- `resolved_urls` caches successful direct URLs and short-lived negative
  provider errors.
- `_resolve_locks` provides per-source locks so concurrent range requests do
  not stampede provider APIs for the same stream.

`dav_protocol` calls `resolve_download_url()` and `invalidate_download_url()`
instead of owning provider fallback or cache policy itself.

## Event And UI State

`buzz.core.events.EventRegistry` is process-local and thread-safe. It stores
structured events for `/logs`, task logs, and stdout visibility. The task pool
uses thread-local task context so events emitted by a task can be attached to
that task id.

`BuzzState.status()` is the compact runtime snapshot consumed by `DavApp` and
PyView. It combines sync state, hook state, readiness state, and the background
task snapshot. Detailed cache/archive data is exposed through dedicated query
methods rather than embedded in the status payload.

## Boundaries

- `DavApp` owns HTTP routes, PyView setup, request validation, and cross-service
  calls that are not part of core state transitions.
- `BuzzState` owns DAV process runtime state and the state transitions that
  touch providers, SQLite, hooks, snapshots, streams, and UI-visible tasks.
- `BackgroundTaskPool` owns generic task lifecycle mechanics but not domain
  behavior.
- `dav_protocol` owns WebDAV request/response mechanics and delegates stateful
  lookups and provider resolution to `BuzzState`.
- `buzz-curator` runs in a separate process and communicates with `buzz-dav`
  over HTTP for rebuilds, config reloads, and UI notifications.
