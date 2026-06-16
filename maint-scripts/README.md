# maint-scripts

This directory contains repository maintenance tools used while developing
buzz. These scripts are not runtime helpers for a deployed stack; user-facing
management scripts live in [`../scripts`](../scripts).

## Files

### `update_dependency_refs.py`

Refreshes third-party container image digest pins in repository-maintained
files:

- `buzz/Dockerfile`
- `docker-compose.yml`
- Buzz-owned GitLab CI component files under `.gitlab/ci`

It resolves current digests with `skopeo`, updates matching image references,
then validates Docker Compose and GitLab CI YAML syntax.

```sh
uv run ./maint-scripts/update_dependency_refs.py
```

Use `--check` when you only need to verify that third-party image references
are already pinned:

```sh
uv run ./maint-scripts/update_dependency_refs.py --check
```

### `render_ui_props.py`

Renders deterministic UI images used by the root README. The script starts a
seeded local Buzz app, captures configured routes with Playwright, composes
them over a generated background, and writes the results to `docs/assets/ui`.

Install the Playwright browser once before the first full render:

```sh
uv run playwright install firefox
```

Then refresh the screenshots:

```sh
uv run python maint-scripts/render_ui_props.py
```

The background can be sampled from a local image or image URL:

```sh
uv run python maint-scripts/render_ui_props.py --background-source ./reference.jpg
uv run python maint-scripts/render_ui_props.py --background-only --background-source ./reference.jpg
```

### `props.yml`

Configuration for `render_ui_props.py`. It controls the output directory,
browser viewport, captured routes, generated background, and window-frame
styling. Relative output paths resolve from the repository root.
