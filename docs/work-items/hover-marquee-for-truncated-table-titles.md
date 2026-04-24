# Hover Marquee For Truncated Table Titles

## Status

todo

## Outcome

An operator scanning the cache and archive tables should be able to read long
torrent titles without opening dev tools, resizing the browser, or leaving the
table context. When a title is truncated with ellipsis, hovering the row or
moving keyboard focus into the row should reveal the hidden suffix through a
lightweight marquee motion. Short titles should remain static.

The idle state should stay visually identical to the current UI: single-line
cells with ellipsis. The marquee is a progressive enhancement for genuinely
truncated titles in the cache and archive name columns only.

## Decision Changes

- **Use a shared PyView hook, not CSS-only overflow heuristics.** Pure CSS
  cannot reliably distinguish truncated titles from titles that already fit,
  and these tables are updated by PyView after initial page load. A shared
  front-end hook can remeasure overflow on mount, update, and resize without
  adding backend complexity.
- **Trigger on row hover and row focus-within.** Pointer users should see the
  effect on hover, and keyboard users should get the same reveal behavior when
  they tab to row actions such as `[X]`, `[S]`, `[R]`, or `[D]`.
- **Animate only overflowing titles.** Cells that fit in the available width
  must keep the current static rendering and never enter the marquee state.
- **One-way loop with an initial pause.** On activation, the title should hold
  briefly at the start, then scroll left to reveal the clipped suffix, then
  restart from the beginning while the hover/focus trigger remains active.
- **Respect reduced-motion preferences.** When the browser reports
  `prefers-reduced-motion: reduce`, the UI should keep plain ellipsis and skip
  marquee animation entirely.
- **Keep a non-animated fallback.** The rendered title element should expose
  the full title through a standard `title` attribute so the full value is
  still inspectable when animation is disabled or unsupported.

## Main Quests

- **Template structure** (`buzz/pyview_templates/cache_live.html`,
  `buzz/pyview_templates/archive_live.html`):
  - Wrap the existing title text in a dedicated marquee structure with an
    outer clipping element and an inner label element.
  - Add stable DOM markers for marquee measurement, and attach a shared
    `phx-hook="BuzzOverflowMarquee"` at the table-container level.
  - Preserve the current table semantics and visible copy; only the inner cell
    structure changes.
- **Hook logic** (`buzz/static/pyview_helpers.js`):
  - Add a `BuzzOverflowMarquee` hook that scans marked title cells within the
    hooked container.
  - On mount and update, measure `scrollWidth` against the visible clip width
    to determine whether each title is truly overflowing.
  - Mark overflowing cells with a data attribute and write CSS custom
    properties for scroll distance and duration.
  - Recompute measurements on container resize using `ResizeObserver`, and
    clean up observers when the hook is destroyed.
- **Styling and motion** (`buzz/static/buzz.css`):
  - Keep the default state as the current single-line ellipsis presentation.
  - Add marquee-specific selectors that activate only when a cell is marked as
    overflowing and its row is hovered or `:focus-within`.
  - Define a keyframe animation that pauses at `translateX(0)` for the start
    of the cycle, then scrolls left by the measured overflow distance.
  - Add reduced-motion handling that disables the marquee and preserves the
    static ellipsis state.
- **Tests** (`tests/test_buzz.py`):
  - Extend the cache and archive page rendering tests to assert the new
    marquee wrapper structure is present.
  - Assert the shared hook is rendered on the relevant table containers.
  - Assert title cells expose the fallback `title` attribute with the full
    torrent name.

## Acceptance Criteria

- Long cache and archive titles remain ellipsized when idle.
- Hovering a row with a truncated title starts a one-way marquee after a short
  initial pause.
- Moving keyboard focus into the row actions triggers the same reveal behavior
  through `:focus-within`.
- Titles that fit within the available width never animate.
- Resizing the table container recomputes which titles overflow.
- Reduced-motion users see static ellipsis with no marquee animation.
- Updated rendering tests pass under `uv run python -m unittest tests.test_buzz`.
- Type checking passes under `uvx pyright buzz tests`.

## Metadata

### id

hover-marquee-for-truncated-table-titles

### type

Issue
