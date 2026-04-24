# Adopt PyView For The Management UI

## Status

in progress

## Progress

- **Integration spike** — complete; pyview is mounted inside the FastAPI app
- **Archive page migration** — complete
- **Logs page migration** — complete
- **Config page migration** — complete
- **Cache page migration** — complete; add-magnet, file-selection, delete-confirmation,
  subtitle-fetch, and server-side sorting are all server-managed
- **Cleanup** — superseded Jinja templates (`buzz/templates/*.html`) and the
  `_cache_page()` helper have been removed
- **Tests** — rendering tests updated for all pyview pages; HTTP endpoint tests
  preserved

## Outcome

Buzz should replace the current management UI's split between Jinja templates,
inline JavaScript, and imperative DOM updates with a single server-driven UI
model built on `pyview`. The operator-facing pages — cache, archive, logs, and
config — should keep the same capabilities, but their interactive behavior
should be expressed as stateful Python live views instead of hand-maintained
REST-plus-template glue.

The WebDAV surface and the existing JSON API endpoints are not the target of
this work item. Buzz still needs FastAPI request handlers for `/dav`, health,
and machine-facing POST endpoints. The adoption boundary is the operator UI.

## Decision Changes

- **Adopt `pyview-web` for operator-facing pages only.** The current
  `buzz-dav` service already runs on FastAPI, and `pyview` is built on top of
  Starlette. That makes it a plausible fit for the HTML management surface
  without forcing a rewrite of the WebDAV or API layers.
- **Keep FastAPI as the outer application shell.** `buzz-dav` and
  `buzz-curator` continue to expose their existing HTTP APIs through FastAPI.
  `pyview` is mounted or integrated as the UI layer for `/cache`, `/archive`,
  `/logs`, and `/config`, rather than becoming a replacement web stack.
- **Migrate page-by-page, not via a flag day.** The cache page is the last
  migration target because it has the highest concentration of fragile inline
  UI state (`[X]`, `[S]`, add/select flows, polling feedback). Archive, logs,
  and config can be usedto test the integration pattern before cache.
- **Prefer server-owned UI state over ad-hoc browser state.** View state such
  as confirmation prompts, pending mutations, selected torrent rows, filter
  values, and transient feedback should live in the live-view context instead
  of being reconstructed from manual DOM mutations and `localStorage`.
- **Require backend-driven live updates over websocket pushes.** Operator
  pages should not rely on client polling, heartbeat timers, or browser-owned
  refresh loops to discover state changes. When Real-Debrid syncs, archive
  contents change, logs append, or config status changes, the server should
  push the resulting live-view updates over the `pyview` websocket connection.
- **Preserve the current visual language during the migration.** This work
  item is an interaction-model change, not a redesign. Existing copy, tables,
  CSS classes, and keyboard-sized actions (`[X]`, `[S]`, `[R]`, `[D]`) should
  stay recognizable unless `pyview` integration forces a small structural
  adjustment.
- **Treat `pyview` as an adoption risk that must be contained.** The upstream
  project describes itself as early-stage and API-unstable. Buzz should keep
  the integration shallow enough that reverting the UI layer remains possible
  if `pyview` proves too limiting or too costly to maintain.

## Main Quests

- **Integration spike** (`buzz/dav_app.py`, new UI module[s]):
  - add `pyview-web` to project dependencies
  - prove that a `pyview` live view can run inside the existing `buzz-dav`
    service alongside the current FastAPI routes
  - document the mounting pattern and request lifecycle in
    `docs/architecture.md`
- **Cache page migration**:
  - replace the current Jinja/inline-JS cache page with a `pyview` live view
  - move add-magnet, file-selection, delete-confirmation, and subtitle-fetch
    affordances into server-managed view state
  - keep existing POST endpoints as the mutation boundary for the first pass;
    the live view may call the same state methods directly only if that
    meaningfully simplifies the code
- **Archive page migration**:
  - port restore/delete confirmation flows to `pyview`
  - keep support for legacy archive rows with `NULL` magnet values
  - preserve current restore semantics: prefer stored magnet when present,
    fall back to `magnet:?xt=urn:btih:<hash>`
  - react to archive changes through server-pushed live-view updates, not
    client-side polling
- **Logs and config migration**:
  - replace polling-heavy DOM code with live updates driven through `pyview`
    websocket pushes from the backend
  - keep the same operator-visible controls and status surfaces
  - ensure config save, restart-required notices, and log filtering still work
- **Live update plumbing**:
  - connect Buzz's backend change sources (sync completion, archive mutations,
    new log events, config save status) to `pyview` so connected sessions are
    updated proactively
  - avoid page-local timers, periodic `pushEvent("refresh")` hooks, and
    equivalent browser polling loops for operator state refresh
- **Cleanup and simplification**:
  - remove superseded Jinja templates and page-specific inline JavaScript once
    each page is migrated
  - keep shared static assets only where they still earn their weight
  - delete dead helper code introduced solely to support imperative DOM
    bookkeeping
- **Tests**:
  - add view-level tests that render the migrated pages and exercise the main
    event flows
  - keep existing HTTP endpoint tests that cover the underlying mutation APIs
  - run `uvx pyright buzz tests` after the migration settles

## Acceptance Criteria

- Buzz serves the cache, archive, logs, and config operator pages through
  `pyview`, while `/dav` and machine-facing API routes continue to work.
- The cache page no longer depends on page-specific inline JavaScript for its
  core interaction flow; confirmation and selection state are owned by the
  live view.
- Archive restore/delete, logs inspection, and config editing continue to work
  with the same operator-facing behavior as before the migration.
- Archive counts, log surfaces, and other operator-visible status views update
  in response to backend state changes without requiring browser polling.
- The resulting UI code is materially smaller or simpler than the combined
  Jinja-plus-inline-JS implementation it replaces.
- New or updated tests cover the migrated UI behavior, and the Python type
  check passes.

## Metadata

### id

adopt-pyview

### type

Issue
