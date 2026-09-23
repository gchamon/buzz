# Redesign Cache Entry Details

## Status

planned

## Outcome

Replace the overloaded cache-entry metadata expansion with a focused tabbed
interface. File selection, subtitle management, and metadata management should
be independently navigable without forcing every control into one dense
`cache-entry-metadata-row` panel.

Investigate and fix the deterministic renderer's durable-ingest expansion so
its screenshot can exercise the file-selection panel before the redesign.

## Main Quests

- **Investigate durable-ingest expansion capture.** Determine why the
  renderer's exact `ingest:<entry-id>` selector matches the files-ready row but
  does not leave the file-selection panel visible in `cache.png`. Preserve the
  direct-click capture approach unless the live-view contract requires a
  different deterministic hook.
- **Define the cache-entry tab model.** Separate file selection, subtitle
  management, and metadata management into explicit tabs with stable active-tab
  state, keyboard navigation, and deep-link-safe identifiers.
- **Refactor the metadata row.** Replace the current tortured box with a
  compact tab shell. Keep each tab's existing behavior and validation while
  removing unrelated controls from inactive panels.
- **Make file lists consistently usable.** Fix `folder-list` so an overflowing
  folder list is scrollable with the same maximum height as the file-selection
  list. Preserve visible scrollbar access, keyboard scrolling, and stable row
  selection.
- **Keep screenshots representative.** Update the deterministic cache fixture
  and renderer capture once the live view supports the new tabs. Keep
  `cache-add.png` as the add-form reference image.

## Acceptance Criteria

- The root cause of durable-ingest expansion capture is documented and fixed.
- A files-ready ingest row expands deterministically to a visible file-selection
  panel in the renderer without browser-side special cases for ingest state.
- Cache-entry details expose separate file-selection, subtitle, and metadata
  tabs with one clear active panel.
- Existing file selection, subtitle, and metadata actions retain their current
  behavior and error handling.
- `folder-list` scrolls when its contents exceed the file-selection list's
  maximum height and remains keyboard accessible.
- The cache-entry expansion is visibly less dense than the current combined
  panel at the supported viewport sizes.
- Focused live-view and renderer tests cover tab transitions, selection state,
  folder overflow, and deterministic ingest expansion.
- `uv run pytest`, `uv run pyright`, and `uv run ruff check .` pass.

## Metadata

### id

cache-entry-metadata-tabs

### type

Issue
