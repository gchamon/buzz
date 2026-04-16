# Selective Sync

## Status

Planned

## Outcome

The library-scan trigger should respect what actually changed in the
Real-Debrid inventory. Today, a single new movie causes Jellyfin to scan every
library (Movies, Shows, Anime); operators pay for that full scan in CPU, I/O,
and thumbnail churn on the `cache/jellyfin` volume. The buzz seedling should
instead propagate the set of changed library roots from the DAV sync path all
the way to the media server, so that only the affected Jellyfin libraries are
refreshed. Plex parity is deferred to a follow-up work item but must not be
blocked by the contract chosen here.

## Decision Changes

- The inventory-sync layer already computes `changed_roots` (see
  `buzz/core/state.py::_trigger_curator_and_hooks` and `_run_hook`, which
  already extends the shell hook command with those roots). We extend that
  contract end-to-end: `changed_roots` becomes a first-class payload field on
  the curator `/rebuild` POST and on the eventual media-server refresh call.
- Jellyfin's global "Scan Media Library" scheduled task (currently invoked in
  `buzz/core/curator.py::trigger_jellyfin_scan`) is replaced for the selective
  path by a per-library refresh using `GET /Library/VirtualFolders` for
  discovery and `POST /Items/{libraryId}/Refresh?Recursive=true` for the
  actual refresh. The scheduled-task call stays as the fallback when discovery
  fails or when `changed_roots` is empty (cold start, manual rebuild).
- Library-root → Jellyfin-library mapping resolves by matching
  `VirtualFolders.Locations` against `PRESENTATION_TARGET_ROOT/<root>`; no
  user configuration is required when the curator's target layout matches the
  Jellyfin library paths. A `media_server.library_map` override in `buzz.yml`
  covers non-conforming setups.
- The curator report grows two fields — `jellyfin_scan_scope` ("full" or
  "selective") and `jellyfin_scan_targets` (list of refreshed roots) — so
  the new logs surface from `expose-logs-in-the-interface` can show exactly
  which libraries were refreshed.
- Plex is not wired in this work item but the `changed_roots` payload reaches
  the curator so a follow-up can add `/library/sections/<id>/refresh?path=...`
  without reshaping the contract.

## Main Quests

- Thread `changed_roots` through the rebuild pipeline:
  - `BuzzState._trigger_curator` (`buzz/core/state.py`) posts
    `{"changed_roots": [...]}` as the request body to
    `self.config.curator_url`.
  - `CuratorApp.rebuild` (`buzz/curator_app.py`) accepts the list from the
    request body and forwards it into `curator.handle_rebuild(changed_roots)`.
  - `Curator.handle_rebuild` and `rebuild_and_trigger`
    (`buzz/core/curator.py`) accept and carry `changed_roots` through to the
    Jellyfin trigger.
- Implement selective Jellyfin refresh:
  - Add `discover_library_map(config)` that calls
    `GET /Library/VirtualFolders` and returns `{root_name: library_id}`
    keyed on the last path segment of each `Locations` entry that lives under
    `PRESENTATION_TARGET_ROOT`.
  - Rewrite `trigger_jellyfin_scan(config, changed_roots)` so that:
    - an empty `changed_roots`, a discovery failure, or an empty map falls
      back to the existing "Scan Media Library" scheduled task (`jellyfin_
      scan_scope="full"`)
    - otherwise posts `/Items/{library_id}/Refresh?Recursive=true&
      ImageRefreshMode=Default&MetadataRefreshMode=Default` for each matched
      library and records the set in the report
      (`jellyfin_scan_scope="selective"`)
- Extend the `PresentationConfig` / `buzz.yml` model in `buzz/models.py` with
  an optional `media_server.library_map` that overrides discovery.
- Extend the curator rebuild report to include `jellyfin_scan_scope` and
  `jellyfin_scan_targets`.
- Update `docs/architecture.md` to describe the new propagation path and
  extend the root `README.md` Configuration Reference with
  `media_server.library_map`.
- Add tests under `tests/` covering:
  - `state.sync` producing `changed_roots` and posting it to the curator
  - discovery correctly mapping a `VirtualFolders` fixture to
    `{"movies": ..., "shows": ..., "anime": ...}`
  - selective refresh calling exactly one Jellyfin endpoint per changed root
  - empty `changed_roots` falls back to the scheduled task
  - user override (`library_map` in `buzz.yml`) winning over discovery

## Acceptance Criteria

- Adding a single movie to Real-Debrid triggers exactly one Jellyfin library
  refresh, targeting only the Movies library. Shows and Anime libraries are
  not refreshed.
- A cold-start rebuild (for example `/api/curator/rebuild` without change
  context) still triggers the full-library scan.
- The curator `/rebuild` response includes `jellyfin_scan_scope` and
  `jellyfin_scan_targets` populated accurately.
- `buzz.yml` may override the discovered map with an explicit
  `media_server.library_map`; tests assert this precedence.
- Existing behavior is preserved when `JELLYFIN_API_KEY` is empty (scan
  skipped) or when `changed_roots` is empty (full scan).
- New unit/integration tests pass under
  `uv run python -m unittest discover -s tests`.

## Metadata

### id

selective-sync

### type

Issue
