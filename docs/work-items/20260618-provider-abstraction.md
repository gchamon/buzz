# Provider Abstraction

## Status

planned

## Outcome

buzz should support multiple debrid providers without letting provider-specific
API shapes leak through the library state, WebDAV streaming path, or operator
UI. Real-Debrid remains the existing provider, and TorBox is added as a second
provider behind the same application contract.

The local buzz library becomes the durable source of truth. Cache and archive
views are projections of that local library against all configured providers.
If a local entry has a live link to any configured provider, it appears in
cache. If it has no live provider links, it appears in archive. When multiple
providers have the same local entry, `provider.priority` chooses the effective
source for WebDAV streaming while the UI still shows provider availability.

Operators can configure both providers side by side, order them by priority,
and edit the Real-Debrid and TorBox credentials from the UI as secret fields.

## Decision Changes

- **Provider interface boundary.** Add a `ProviderClient` protocol that exposes
  normalized methods for auth/status checks, torrent listing, torrent detail,
  magnet add, file selection, torrent deletion, and direct stream URL
  resolution. Provider clients return buzz domain models, not raw upstream
  payloads.
- **Provider implementations.** Keep current Real-Debrid behavior through a
  `RealDebridProviderClient` adapter, then add `TorBoxProviderClient` with the
  same normalized contract.
- **Local library ownership.** Replace the current Real-Debrid-shaped cache and
  archive ownership model with provider-neutral local entries plus provider
  links. Existing Real-Debrid torrent and archive rows migrate into local
  entries with Real-Debrid provider links when available.
- **Priority provider projection.** `provider.priority` controls effective
  source selection for duplicate provider links. Cache/archive classification
  uses the union of all configured provider links.
- **Fallback behavior.** Magnet add, restore, and WebDAV stream resolution try
  providers in priority order. If a lower-priority provider succeeds after a
  higher-priority provider fails, buzz logs a clear warning and returns a
  warning in operation responses where applicable.
- **Editable provider secrets.** Real-Debrid and TorBox tokens are editable
  secret fields in the config UI. They remain masked in read views and logs,
  but the UI can submit replacements.

## Main Quests

- **Define provider models and protocol.**
  - Add provider-neutral typed models for torrent summaries, torrent details,
    files, provider links, selected files, operation results, and resolved
    stream URLs.
  - Add a `ProviderClient` protocol with methods needed by `BuzzState` and the
    DAV stream path.
  - Move Real-Debrid-specific non-transient error handling behind the
    Real-Debrid provider adapter so `BuzzState` handles provider-neutral
    unavailable-stream errors.

- **Add Real-Debrid and TorBox clients.**
  - Wrap the current Real-Debrid client calls in `RealDebridProviderClient`
    without changing behavior.
  - Implement `TorBoxProviderClient` using TorBox API endpoints for torrent
    list/detail, add magnet, select or request downloadable files where
    supported, delete, cache/status inspection, and download-link resolution.
  - Normalize TorBox and Real-Debrid statuses into the same internal status
    vocabulary used by cache/archive and WebDAV.

- **Reshape state and persistence.**
  - Add SQLite migrations for local library entries and provider links.
  - Migrate current `torrents` rows into local entries with Real-Debrid links.
  - Migrate current `archive` rows into local entries without live
    Real-Debrid links, preserving hash, name, selected files, magnet, bytes, and
    deletion metadata.
  - Keep compatibility readers only as long as needed for migration; new
    business logic should use repository helpers in `buzz.core.db`.

- **Update library behavior.**
  - Sync every configured provider and reconcile each provider's links against
    the local library.
  - Build WebDAV snapshots from local entries that have at least one provider
    link, using the highest-priority available provider as the effective source.
  - Add magnet creates or reuses a local entry, trying providers in priority
    order and falling back with a warning.
  - Move to archive deletes or detaches all configured provider links and keeps
    the local entry.
  - Restore re-adds the local entry to selected provider targets, defaulting to
    priority order and falling back with a warning.
  - Permanent archive deletion removes the local entry and all provider links
    for that entry.

- **Update configuration and UI.**
  - Change config shape to include `provider.priority`,
    `provider.real_debrid.token`, and `provider.torbox.token`, while keeping
    shared provider settings under `provider`.
  - Preserve backward compatibility with existing `provider.token` by loading
    it as `provider.real_debrid.token` when the nested Real-Debrid token is not
    set.
  - Add a provider-priority editor to the config UI.
  - Add editable secret controls for both provider tokens, with masking in
    effective-config read views and logs.
  - Show provider availability on cache/archive pages and use provider-neutral
    button copy and disabled-state titles.

- **Update documentation and terminology.**
  - Replace user-facing Real-Debrid-only wording with provider-neutral wording
    where behavior now applies to both providers.
  - Keep provider-specific references only where they describe credentials,
    upstream limitations, or provider-specific errors.

## Acceptance Criteria

- Real-Debrid continues to work through the new provider abstraction with no
  user-visible regression in cache, archive, WebDAV, add, delete, restore,
  sync, or stream behavior.
- TorBox can be configured alongside Real-Debrid, synced, used to add magnets,
  and used by WebDAV streaming through the same state and DAV code paths.
- Changing `provider.priority` changes the effective source for duplicate
  provider links without deleting local entries.
- Cache/archive projection uses the union of configured provider links.
- Existing databases migrate Real-Debrid cache and archive data into the new
  local-entry/provider-link model without losing hashes, magnets, selected
  files, names, bytes, or deletion timestamps.
- The config UI can change provider priority and update both provider tokens
  while still masking secrets in read-only views and logs.
- Provider-specific API payloads are contained inside provider client modules;
  `BuzzState`, templates, and DAV protocol code use provider-neutral models.
- New tests cover provider adapters with fake upstream payloads, database
  migration, cache/archive union projection, config secret editing,
  priority-based WebDAV stream resolution with fallback, add fallback, archive
  across providers, and Real-Debrid backward compatibility.
- `uvx pyright buzz tests` passes.
- The Docker-based pytest suite passes.

## Metadata

### id

provider-abstraction

### type

Issue
