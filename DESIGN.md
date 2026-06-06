---
name: buzz
description: Compact Dracula-inspired admin panel for Real-Debrid, WebDAV, and Jellyfin operations.
colors:
  terminal-bg: "#282a36"
  terminal-fg: "#f8f8f2"
  selection-surface: "#44475a"
  comment-muted: "#6272a4"
  cyan-action: "#8be9fd"
  green-ready: "#50fa7b"
  orange-working: "#ffb86c"
  pink-heading: "#ff79c6"
  purple-primary: "#bd93f9"
  red-error: "#ff5555"
  yellow-override: "#f1fa8c"
typography:
  display:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, Courier New, monospace"
    fontSize: "1.3rem"
    fontWeight: 700
    lineHeight: 1.5
  title:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, Courier New, monospace"
    fontSize: "0.8rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "1px"
  body:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, Courier New, monospace"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  data:
    fontFamily: "JetBrains Mono, Fira Code, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, Courier New, monospace"
    fontSize: "0.8rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, Courier New, monospace"
    fontSize: "0.75rem"
    fontWeight: 700
    lineHeight: 1.5
rounded:
  xs: "2px"
  sm: "3px"
  md: "4px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "10px"
  lg: "15px"
  xl: "20px"
  section: "24px"
components:
  button-primary:
    backgroundColor: "{colors.purple-primary}"
    textColor: "{colors.terminal-bg}"
    rounded: "{rounded.md}"
    padding: "8px"
  button-secondary:
    backgroundColor: "{colors.comment-muted}"
    textColor: "{colors.terminal-fg}"
    rounded: "{rounded.md}"
    padding: "8px"
  input-default:
    backgroundColor: "{colors.terminal-bg}"
    textColor: "{colors.terminal-fg}"
    rounded: "{rounded.md}"
    padding: "6px 8px"
  panel-header:
    backgroundColor: "{colors.selection-surface}"
    textColor: "{colors.pink-heading}"
    rounded: "{rounded.md}"
    padding: "10px 15px"
  config-pill:
    textColor: "{colors.comment-muted}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
---

# Design System: buzz

## 1. Overview

**Creative North Star: "One-Stop Operator Console"**

buzz should feel like a local operator panel for Real-Debrid, WebDAV, and Jellyfin, not a glossy media app. The interface is intentionally compact, monospace, and Dracula-inspired because users are managing live system behavior, configuration, logs, and library safety. It should read as a terminal UI made practical for the browser.

The system values stable surfaces over ceremony. It uses predictable navigation, dense tables, direct form fields, visible raw configuration, and short console-like labels. The UI should help a capable user understand what is happening without layout shifts, pop-ups, heavy JavaScript, or decorative animation.

**Key Characteristics:**

- Dark Dracula terminal surface with a compact information rhythm.
- Monospace-first typography across navigation, data, controls, and logs.
- Flat panels with structural borders instead of layered cards.
- Color-coded state vocabulary for ready, working, warning, error, active, and changed states.
- Mobile-friendly reductions that hide lower-priority columns rather than reinventing the interface.

## 2. Colors

The palette is a direct Dracula terminal palette: saturated enough to classify state quickly, restrained by a dark violet-neutral background and muted blue-gray secondary text.

### Primary

- **Operator Purple** (`#bd93f9`): Primary button background, active focus border, brand prompt accent, and selected control emphasis. Use sparingly for actions or focus, not decoration.

### Secondary

- **Archive Cyan** (`#8be9fd`): Hover links, upload or in-progress status, file size data, metadata values, and secondary action hints.
- **Ready Green** (`#50fa7b`): Active navigation, successful or downloaded state, ready status, and positive confirmation actions.
- **Working Orange** (`#ffb86c`): Labels, warnings, dirty state, in-progress status, reload-mode markers, and operational attention.
- **Terminal Pink** (`#ff79c6`): Section headers, table headers, and high-level page titles.
- **Error Red** (`#ff5555`): Errors, destructive hover states, offline status, and failed cache or log states.
- **Override Yellow** (`#f1fa8c`): Explicit override state and rare caution markers.

### Neutral

- **Terminal Background** (`#282a36`): Body background, panel body background, code blocks, inputs, and scroll tracks.
- **Terminal Foreground** (`#f8f8f2`): Primary text and high-priority data.
- **Selection Surface** (`#44475a`): Hover rows, panel headers, add-torrent surfaces, selected table background, and subtle grouping.
- **Comment Muted** (`#6272a4`): Secondary text, inactive navigation, borders, scroll thumbs, baselines, sources, timestamps, and disabled-adjacent metadata.

### Named Rules

**The State-First Color Rule.** Color exists to classify state, syntax, metadata, or action. If a color does not help the operator read the system faster, remove it.

**The Dracula Literal Rule.** Preserve the established palette unless a replacement is deliberate and complete. Do not introduce unrelated blues, grays, or brand gradients.

## 3. Typography

**Display Font:** `ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace`

**Body Font:** `ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace`

**Label/Mono Font:** `JetBrains Mono, Fira Code, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace` for logs and YAML displays where available.

**Character:** The typography is terminal-native and intentionally plain. It should feel fast, inspectable, and local, with hierarchy coming from color, weight, uppercase labels, compact sizing, and panel structure rather than from display type.

### Hierarchy

- **Display** (700, `1.3rem`, 1.5): Prompt brand and the largest console-style labels. Use rarely.
- **Headline** (700, `1rem`, 1.5): Important inline markers, status labels, and emphasized metadata.
- **Title** (400, `0.8rem`, 1.5, `1px` letter spacing, uppercase): Section headers, table headers, logs/config headers, and panel labels.
- **Body** (400, `1rem`, 1.5): Default interface text. Long prose should stay under 65 to 75 characters where possible.
- **Data** (400, `0.8rem`, 1.5): Tables, logs, YAML blocks, configuration content, and dense operational rows.
- **Label** (700, `0.75rem`, 1.5): Chips, baselines, reload-mode tags, timestamps, and compact metadata.

### Named Rules

**The One Console Voice Rule.** Do not add display fonts, marketing type, or mixed typographic personalities. buzz speaks in one compact terminal voice.

## 4. Elevation

buzz is flat by default. Depth is conveyed through borders, tonal surfaces, sticky headers, row hover states, and rare glows tied to active system state. Shadows are not used as generic card elevation. When glow appears, it means attention, focus, dirty configuration, override state, new warning logs, or error logs.

### Shadow Vocabulary

- **Dirty Field Glow** (`box-shadow: 0 0 12px rgba(255, 184, 108, 0.18)`): Unsaved configuration changes only.
- **Override Field Glow** (`box-shadow: 0 0 12px rgba(241, 250, 140, 0.14)`): Values overridden through the UI only.
- **Navigation Warning Glow** (`text-shadow: 0 0 10px rgba(255, 184, 108, 0.8)`): New warning logs only.
- **Navigation Error Glow** (`text-shadow: 0 0 10px rgba(255, 85, 85, 0.8)`): New error logs only.
- **Active Navigation Glow** (`text-shadow: 0 0 5px rgba(80, 250, 123, 0.5)`): Current top-level route only.

### Named Rules

**The Flat-Until-State Rule.** Surfaces stay flat at rest. Glow appears only when a field, route, or log state is asking for attention.

## 5. Components

Components should feel like compact command-console controls. They are dense, predictable, border-led, and familiar. Avoid app-like card stacks when a panel, table, fieldset, or inline command row is more honest.

### Buttons

- **Shape:** Small rectangular controls with a `4px` radius by default; `3px` for icon buttons.
- **Primary:** Operator Purple background (`#bd93f9`) with Terminal Background text (`#282a36`), `8px` padding, bold uppercase text at `0.8rem`.
- **Hover / Focus:** Hover uses brightness for primary buttons. Focus for related inputs uses a purple border. Avoid animated layout changes.
- **Secondary / Ghost / Tertiary:** Secondary buttons use Comment Muted (`#6272a4`) with Terminal Foreground text. Inline row actions stay transparent until hover and shift text/background by state: green for restore/confirm, cyan for secondary, red for destructive.

### Chips

- **Style:** Pills use `999px` radius, `4px 10px` padding, `0.75rem` type, and a one-pixel border.
- **State:** Dirty chips use Working Orange. Override chips use Override Yellow. Generic chips use Comment Muted. Chips annotate state; they are not decoration.

### Cards / Containers

- **Corner Style:** `4px` radius on panels, add-torrent sections, fieldsets, log containers, and overlay messages.
- **Background:** Panel headers and grouped entry sections use Selection Surface. Panel bodies use Terminal Background.
- **Shadow Strategy:** No generic shadows. Use state glow only as documented in Elevation.
- **Border:** One-pixel Comment Muted borders define panels, fieldsets, inputs, lists, and log containers.
- **Internal Padding:** Use `10px 15px` for panel headers and scrollable panel interiors, `12px 16px` for fieldsets, and `8px` for compact fields.

### Inputs / Fields

- **Style:** Inputs and textareas use Terminal Background, Terminal Foreground text, Comment Muted one-pixel border, `4px` radius, monospace text, and `6px 8px` padding in config forms.
- **Focus:** Purple border (`#bd93f9`) only. No large focus animation or layout movement.
- **Error / Disabled:** Disabled fields reduce opacity to `0.5` and remove pointer events. Invalid or failed states should use Error Red plus text, not color alone.

### Navigation

- **Style:** The top prompt uses `buzz:` in purple followed by compact route links separated by `◈`. Inactive links are Comment Muted, hover is Cyan, active is Green.
- **Typography:** Monospace, compact, and inherited from body. Counts live inline with route labels such as `archive(12)` and `logs(4)`.
- **States:** Warning and error log states may glow. Active route may glow. Avoid navigation layout shifts during polling.
- **Mobile Treatment:** At `700px` and below, labels hide, icons remain, separators disappear, and status moves to a compact dot beside the brand prompt.

### Tables and Logs

Tables are first-class components. Use sticky uppercase pink headers, compact `0.8rem` type, `35px` row height, selection hover, and horizontal overflow for dense data. On mobile, hide lower-priority columns instead of turning tables into unrelated card views.

Logs use a bordered terminal pane with timestamp, source, level, and message in a grid. Copy actions remain quiet until hover. Log levels use muted, green, orange, and red state colors.

## 6. Do's and Don'ts

### Do:

- **Do** keep the interface compact, dense, and self-guiding with minimal visual elements.
- **Do** use the existing Dracula colors for state, syntax, categories, and affordances.
- **Do** keep panels flat, bordered, and stable during polling or refreshes.
- **Do** preserve raw configuration visibility and direct form controls.
- **Do** make mobile views compact by prioritizing information and hiding lower-priority columns at `700px`.
- **Do** respect reduced motion and avoid movement that interrupts reading or selection.

### Don't:

- **Don't** make buzz look like flashy SaaS, a gamified media app, a mobile-first consumer product, or a cloud-account dashboard.
- **Don't** hide raw configuration details behind opaque wizards.
- **Don't** use pop-ups as the primary interaction model.
- **Don't** add heavy JavaScript requirements for basic navigation, configuration, or inspection.
- **Don't** introduce repaint-heavy updates that shift the UI around while users read or interact.
- **Don't** add decorative animations, page-load choreography, gradient text, glassmorphism, or card-grid marketing patterns.
- **Don't** use thick colored side-stripe borders on cards, list items, callouts, or alerts. Use full borders, background tints, icons, labels, or inline text instead.
