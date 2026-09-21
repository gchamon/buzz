# Redesign Magnet Intake

## Status

backlog

## Outcome

Replace the synchronous magnet analysis dialog with a durable, reactive magnet
intake flow. Slow, incomplete, or failing upstream responses must not block the
cache page, silently discard a magnet, or require the operator to resubmit it.

The cache table starts with an expandable synthetic row named **ADD NEW MAGNET
LINKS TO THE CACHE**. Expanding it shows the multi-magnet input. Submitted
magnets become independent, persistent intake entries, while the add row
collapses and resets immediately for the next submission.

Each magnet is submitted and analyzed sequentially through enabled debrid
providers in priority order. Buzz falls back only while no provider has accepted
the magnet. Once a provider accepts it, that provider remains authoritative for
the entry: delayed metadata or file lists are shown as pending and reconciled
from that provider rather than creating duplicate torrents at fallback
providers.

Every valid magnet enters the archive when intake begins, before the provider
accepts it and before file selection. Its archive metadata is enriched as the
provider resolves the entry. A valid submission must contain a locally parseable
BTIH info hash; malformed or hashless input fails before provider work starts.

## Decision Changes

- **Replace transient dialog state with durable intake state.** Persist an
  intake record per submitted valid magnet, including its normalized hash,
  original URI, provider attempts, accepted provider link, resolution state,
  metadata, file-selection draft, timestamps, and terminal error. Intake state
  survives browser reconnects and process restarts.

- **Use a table-native intake control.** Render the add form in the first,
  expandable cache-table row instead of above the table. Once a batch is
  submitted, collapse and clear the synthetic row. Render each submitted magnet
  as an independent intake row until it becomes a confirmed cache entry or a
  terminal failure.

- **Run analysis asynchronously and sequentially.** Queue magnets in input
  order as background work. Provider I/O must have explicit timeouts, bounded
  retries, and cooperative cancellation, and must not run while holding the
  global state lock. Push state transitions to connected cache views.

- **Separate provider acceptance from metadata readiness.** A provider's
  successful submission result only establishes its torrent reference. A later
  resolution result explicitly says either that files are ready or that metadata
  is pending. Pending metadata is not a failure and does not trigger provider
  fallback.

- **Make fallback a typed policy.** Before acceptance, Buzz tries providers in
  priority order only for typed, fallback-eligible failures: provider
  unavailability, timeout, rate-limit exhaustion, account limitation,
  unsupported operation, or explicit magnet rejection. Invalid local input and
  authentication/configuration failures stop the entry without fallback.

- **Persist archive history at intake start.** Normalize the BTIH hash locally,
  then immediately create or update its archive record with the original magnet
  and available display name. Merge provider name, size, and selected-file
  metadata into this entry as it arrives. Archive persistence must be idempotent
  and independent of provider inventory sync.

- **Represent every analysis outcome in the UI.** An intake row explicitly
  displays files ready for selection, metadata pending at its accepted provider,
  or a terminal error after no provider accepts it or after metadata resolution
  reaches its bounded failure policy. Errors show a stable buzz code and safe
  provider detail.

- **Confirm selections through a provider-neutral batch primitive.** Group
  ready selections by provider and submit each group through the provider's
  multi-selection primitive. An adapter uses a native batch API where available
  or performs its own sequential requests otherwise, returning a per-torrent
  result. Entries remain visible while awaiting upstream confirmation.

- **Replace provider mechanics with semantic intake primitives.** The provider
  contract must expose submission, resolution, and multi-selection operations
  that correspond to the intake workflow rather than forcing `BuzzState` to
  infer meaning from `add_magnet()` followed by an immediate detail request.
  Inventory, deletion, and stream-resolution primitives remain for sync and
  WebDAV and share the same error contract.

- **Use normalized typed provider failures.** Introduce
  `ProviderOperationError` with provider, semantic operation, stable code,
  retryability, fallback eligibility, optional retry-after value, safe UI
  detail, and provider-native diagnostic code. Adapters map upstream payloads,
  HTTP status, and transport failures to this type; orchestration must not
  branch on exception text or provider-specific response shapes.

## Main Quests

- **Define intake persistence and state transitions.**
  - Add typed domain models and SQLite migrations for intake batches, intake
    entries, provider attempts, resolution state, and file-selection drafts.
  - Define queued, submitting, metadata-pending, files-ready, selecting,
    awaiting-confirmation, confirmed, and failed states with valid transitions.
  - Require and normalize BTIH hashes before persistence or provider calls.
  - Make archive creation and enrichment idempotent.

- **Define semantic provider operations.**
  - Replace `add_magnet()` with `submit_magnet(magnet) -> MagnetSubmission`.
    Its result identifies the accepted or reused provider torrent but does not
    imply metadata readiness.
  - Add `resolve_magnet(torrent_ref) -> MagnetResolution`, returning either
    `files_ready` with normalized metadata/files or `metadata_pending` with
    status and next-refresh guidance.
  - Add `apply_file_selections(selections) -> list[FileSelectionResult]`.
    Each adapter handles native batch selection where supported and sequential
    selection where it is not, reporting outcomes per torrent.
  - Retain normalized inventory, detail inspection, deletion, and stream
    resolution operations for the rest of buzz.

- **Define the provider failure vocabulary.**
  - Add `ProviderOperationError` and stable codes:
    `invalid_magnet`, `unsupported_operation`, `authentication_failed`,
    `permission_denied`, `account_limit_reached`, `magnet_rejected`,
    `rate_limited`, `upstream_timeout`, `upstream_unavailable`,
    `upstream_protocol_error`, `torrent_not_found`,
    `file_selection_rejected`, and `unknown`.
  - Treat `metadata_pending` as a successful resolution state, not an error.
  - Keep raw provider payloads and diagnostics out of the UI; record them only
    in structured logs, alongside the normalized error and safe detail.
  - Make retry and fallback decisions exclusively from typed error attributes.

- **Implement intake orchestration and reconciliation.**
  - Process entries sequentially without blocking the live-view event handler.
  - Record every provider submission attempt and its outcome.
  - Refresh accepted entries until metadata becomes ready or reaches a defined,
    visible terminal resolution failure.
  - Reconcile pending resolution and selection-confirmation entries during
    provider sync, enriching cache/archive metadata as upstream state changes.
  - Ensure cancellation stops future work cooperatively without silently
    removing accepted provider torrents or durable intake history.

- **Redesign the cache page.**
  - Move the multi-magnet form into the first expandable table row.
  - Render independent intake rows with provider, state, progress, safe error
    information, pending-metadata messaging, and file-selection controls.
  - Preserve current cache-entry expansion and selection editing behavior.
  - Remove the global `analysis_results` dialog state and its cancellation path
    that deletes already-added provider torrents.

- **Confirm selections and project provider state.**
  - Submit ready selections grouped by provider through the new batch primitive.
  - Retain partial success and failure results per intake entry.
  - Keep entries pending until sync confirms upstream selection and cache state.
  - Update cache, archive metadata, the WebDAV snapshot, hooks, and live views
    only when their corresponding upstream transition is confirmed.

- **Test and observe the flow.**
  - Cover mixed batches containing ready files, delayed metadata, add fallback,
    total failure, timeout, retry, cancellation, and restart recovery.
  - Cover immediate archive creation and later archive enrichment.
  - Cover native batch selection and adapter-level sequential fallback.
  - Verify raw provider failures map to the stable error vocabulary, safe UI
    detail, retryability, and fallback eligibility.
  - Verify live cache views remain responsive and receive intake transitions.

## Acceptance Criteria

- The first cache-table row is **ADD NEW MAGNET LINKS TO THE CACHE** and
  expands to accept multiple magnet links.
- Submission clears and collapses that row immediately, while every valid
  magnet appears as an independent persistent intake entry.
- A slow or incomplete provider response does not block the cache UI, cache
  mutations, or later intake entries.
- Providers are tried in priority order until one accepts the magnet, and every
  failed submission attempt is visible in entry history and structured logs.
- A provider that accepted a magnet remains authoritative while metadata is
  pending; buzz does not create fallback duplicates merely because files are
  delayed.
- A ready intake entry renders selectable files. A pending entry clearly states
  that its provider must resolve metadata before file selection is available.
- A magnet that no provider accepts retains a visible terminal error with a
  normalized code and safe detail.
- Invalid or hashless magnets are rejected locally before provider work. Valid
  BTIH magnets enter the archive as soon as intake begins, before acceptance or
  file confirmation.
- Archive name, size, and selected-file metadata are enriched as provider
  resolution and confirmation complete.
- All providers implement semantic magnet submission, resolution, and
  multi-selection primitives, and no intake flow branches on exception text,
  HTTP status, or provider-native payload shape.
- Selection uses provider-native batching when available and sequential adapter
  work otherwise, preserving a per-entry outcome for partial failures.
- Intake entries survive restart and live-view reconnection, and transition in
  response to provider sync without browser polling.
- New tests cover the state machine, persistence migration, adapter error
  mapping, fallback policy, partial selection outcomes, and live-view behavior.
- `uv run pytest`, `uv run pyright`, and `uv run ruff` pass.

## Metadata

### id

magnet-intake-redesign

### type

Issue
