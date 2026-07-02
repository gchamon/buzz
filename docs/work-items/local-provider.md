# Local Provider

## Status

doing

## Outcome

buzz gains a third provider kind: `local`. Operators can copy an entry's files
from a debrid provider onto buzz-managed local disk, and those files are then
served through the same WebDAV streaming path as any other provider,
transparently. The local provider participates in `provider.priority`, so when
a local copy exists and `local` wins priority, streams read from disk instead
of proxying the debrid CDN.

An entry with only a local link is still live in the provider-link projection:
it appears in cache, not archive, because the local copy is a provider link
like any other. The copy action itself is controlled from the archive view,
alongside the existing restore, delete, and provider-transfer actions.

Local copies are bounded by a strict disk-usage limit. By default, buzz will
never fill the store filesystem past 80% of its capacity.

## Decision Changes

- **`local` becomes a `ProviderKind`.** Extend `ProviderKind` in
  `buzz/core/providers.py` with `"local"` and add `LocalProviderClient` in
  `buzz/providers/local.py` implementing the existing `ProviderClient`
  protocol. The client's inventory is the local store (backed by SQLite
  records), and `resolve_stream` returns a local-file stream reference rather
  than an upstream URL.
- **The DAV path learns to serve local bytes.** `resolve_download_url` in
  `buzz/core/state.py` and `open_remote_media` in `buzz/dav_protocol.py` gain a
  local-file branch: when the effective source is a local stream reference,
  bytes are read from disk with full range-request support instead of proxying
  a ranged httpx request upstream.
- **Local is never an add/restore fallback target.** The local provider is
  populated only by the explicit copy action. Magnet add, restore, and
  provider-transfer priority fallback skip `local` as a destination; it only
  competes in priority for stream-source selection.
- **Copy-to-local is a provider transfer.** From the archive view, the copy
  action behaves like the existing `submit_archive_provider_transfer` flow with
  destination `local`: buzz picks the highest-priority provider with a live
  link (restoring via magnet first when none is live), streams each selected
  file to a temporary file in the store, and atomically renames it into place.
- **Strict disk-usage limit.** Before starting a copy, buzz inspects the store
  filesystem via `os.statvfs` on `provider.local.path` and refuses the copy if
  projected usage (current usage plus the entry's bytes) would exceed
  `provider.local.max_fs_usage_percent` (default `80`). The limit is
  re-checked during the copy so a concurrent writer cannot push the filesystem
  past the cap; on breach the copy aborts cleanly, the temporary file is
  removed, and a warning event is recorded. The check is against actual
  filesystem usage, not per-feature bookkeeping, so it composes safely with
  any other feature writing to the same filesystem. The archive UI disables
  the copy action, with an explanatory title, for entries that cannot fit.
- **New configuration.** Add `provider.local.enabled`, `provider.local.path`
  (store directory), and `provider.local.max_fs_usage_percent` to `DavConfig`
  in `buzz/models.py`, whitelist them in `UI_MANAGED_CONFIG_PATHS`, and accept
  `local` as a member of `provider.priority`.
- **Local file records live in SQLite.** Add migrations and repository helpers
  in `buzz.core.db` for local file records and their provider links; business
  logic never issues ad-hoc SQL.

## Main Quests

- **Extend the provider model and add the local client.**
  - Add `"local"` to `ProviderKind` and adjust provider-id helpers such as
    `split_provider_torrent_id` to accept the new prefix.
  - Implement `LocalProviderClient` with inventory listing, detail lookup,
    deletion (removes files and records), health checks (store path exists and
    is writable), and local stream-reference resolution. `add_magnet` and
    `select_files` report unsupported-operation errors.
- **Define the local store, configuration, and persistence.**
  - Add the `provider.local.*` config fields with validation and UI-managed
    paths.
  - Define the on-disk layout under `provider.local.path` keyed by entry hash
    and torrent name, with a temp area for in-flight copies.
  - Add SQLite migrations and `buzz.core.db` repository helpers for local file
    records; wire them into the provider-link projection.
- **Build the copy pipeline.**
  - Add `BuzzState` submit/execute methods mirroring the existing archive
    provider transfer, running on `BackgroundTaskPool` with progress on
    `/threads`, cooperative cancellation, and `record_event` telemetry.
  - Enforce the disk-usage limit up front and mid-copy with clean abort and
    temp-file cleanup.
- **Serve local bytes through DAV.**
  - Add the local-file branch to stream resolution and `open_remote_media`
    with correct range semantics, so rclone and media servers see no
    behavioral difference from upstream streaming.
- **Update the archive view.**
  - Add an `[L]` copy-to-local action, a `local` tag in the "Available On"
    column, and a delete-local-copy action, following the existing inline
    action style and Dracula color vocabulary from DESIGN.md.
  - Permanent archive deletion also removes local files and records.
- **Update documentation.**
  - Extend the architecture docs and config reference with the local provider,
    its non-fallback semantics, and the disk-usage limit.

## Acceptance Criteria

- Copy-to-local works from the archive view for entries available on either
  Real-Debrid or TorBox, including the restore-first path for entries with no
  live link.
- With `local` first in `provider.priority`, streams for copied entries are
  served from disk; deleting the local copy reverts streaming to the upstream
  provider without touching debrid links.
- An entry whose local link is the only live link appears in cache, not
  archive.
- Magnet add and restore never target the local provider.
- A copy that would push the store filesystem past
  `provider.local.max_fs_usage_percent` (default 80%) is refused before it
  starts, and a copy aborts cleanly with temp-file cleanup if the limit is hit
  mid-copy; the UI disables the action for entries that cannot fit.
- Existing databases migrate without losing hashes, magnets, selected files,
  names, bytes, or deletion timestamps.
- New tests cover the local client, copy pipeline including limit enforcement
  and cancellation, local stream serving with range requests, projection of
  local links, and config validation.
- `uvx pyright buzz tests` passes.
- The Docker-based pytest suite passes.

## Metadata

### id

local-provider

### type

Issue
