# `buzz.pyview_templates`

Jinja2 (`ibis`) templates that render the `buzz-dav` operator UI.

The UI is built with [PyView](https://github.com/ogrodnek/pyview), a
Phoenix-LiveView-style framework: stateful server-side views push diffs to the
browser over a WebSocket, so pages update live without reloads. These templates
are the markup half; the live-view classes that drive them live in
[`../ui_live.py`](../ui_live.py).

For where the UI sits in the system, see the operator-UI section of
[`../../docs/architecture/system.md`](../../docs/architecture/system.md).

## The views

| Template | Backing view (`ui_live.py`) | Purpose |
| --- | --- | --- |
| `shell_live.html` | `BuzzLiveView` | Master shell/router; embeds the active page body, nav, and meta bar. |
| `cache_live.html` | `CacheLiveView` | Torrent cache and per-file download selection. |
| `archive_live.html` | `ArchiveLiveView` | Archived / deleted torrents (restore, permanent delete). |
| `logs_live.html` | `LogsLiveView` | Logs UI for internal events — displays the [`buzz.events`](../events.py) ring buffer. |
| `threads_live.html` | `ThreadsLiveView` | Background task / thread monitor. |
| `config_live.html` | `ConfigLiveView` | Configuration editor (providers, intervals, subtitles, …). |
| `partials/nav.html` | — | Reusable navigation bar. |
| `partials/meta_bar.html` | — | Reusable status meta bar. |

## How they load and route

- **Template dir / loader.** `_TEMPLATE_DIR` points at this directory and
  `ibis.loader = FileReloader(str(_TEMPLATE_DIR))` is set at import time in
  [`../ui_live.py`](../ui_live.py). `_load_template(name)` resolves a template
  file by name; views render through `LiveRender(_load_template("<page>.html"),
  assigns, meta)`.
- **App construction.** `build_ui(owner)` creates the `PyView` app, sets
  `rootTemplate` from `_build_root_template()`, and registers a route per page
  (`/`, `/cache`, `/archive`, `/logs`, `/threads`, `/config`). Every route maps
  to a fresh `BuzzLiveView(owner)`.
- **Mounting.** `DavApp` mounts the UI by calling `build_ui(self)` — the `owner`
  is the `DavApp`, giving views access to `BuzzState` and its operations.
- **Routing inside the shell.** `BuzzLiveView` is the shell/router. It holds an
  instance of each page view and renders the active one into the shell body.
  Navigation is a `navigate` event carrying the target page; valid pages are the
  `PAGE_NAMES` tuple (`"cache"`, `"archive"`, `"logs"`, `"threads"`, `"config"`)
  and the `PageName` literal.

## View class hierarchy

```
_BaseBuzzLiveView            # shared mount/context plumbing, base context
├── CacheLiveView
├── ArchiveLiveView
├── LogsLiveView
├── ThreadsLiveView
├── ConfigLiveView
└── BuzzLiveView             # master shell/router; delegates to the above
```

Each page view defines async `mount` / `handle_event` / `render` plus a
`_context(...)` helper, and a `*Context` `TypedDict` (subclassing
`PageContext`) describing the template variables.

## Creating a new view

1. **Add the template.** Create `pyview_templates/<page>_live.html`. Include the
   shared partials (`{% include "partials/nav.html" %}`,
   `{% include "partials/meta_bar.html" %}`) and use `phx-*` bindings for
   interaction (`phx-click`, `phx-value-*`, `phx-submit`, `phx-change`,
   `phx-hook`).
2. **Define the context.** Add a `class <Page>Context(PageContext, TypedDict)`
   in [`../ui_live.py`](../ui_live.py) listing every template variable.
3. **Add the view class.** Create
   `class <Page>LiveView(_BaseBuzzLiveView)` implementing:
   - `mount` — call `await super().mount(...)`, set `socket.context`, and
     `await socket.subscribe(...)` to any topics you need when connected;
   - `handle_event` — handle the page's `phx-*` events;
   - `render` — `return LiveRender(_load_template("<page>_live.html"), assigns,
     meta)`;
   - `_context(...)` — build the context dict from `self._base_context()`.
4. **Wire it into the shell.** In `BuzzLiveView`, construct the view in
   `__init__` and add cases for it in `_page_context`, `_replace_page_context`,
   and `_render_page_body`.
5. **Register the route.** Add `"<page>"` to `PAGE_NAMES` and the `PageName`
   literal, and add `app.add_live_view("/<page>", lambda: BuzzLiveView(owner))`
   in `build_ui`.
6. **Add it to the nav.** Add the page to `partials/nav.html` so it is
   reachable via the `navigate` event.
