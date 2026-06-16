# Contributing

## HTTPS UI

Buzz serves the WebDAV endpoint on plain HTTP port `9999`. The browser UI runs
on HTTPS port `9443` by default, and HTTP UI paths redirect there.

The default self-signed certificate paths in `buzz.yml` are CWD-relative:

```yaml
tls:
  cert_path: data/tls/buzz.crt
  key_path: data/tls/buzz.key
```

Inside the container, the working directory is `/app`, so those paths land in
`/app/data/tls`. On startup, `buzz-dav` creates missing TLS material and renews
certs that expire within 30 days. It also checks weekly; when a runtime renewal
happens, `buzz-dav` exits so Docker restarts it with the new certificate
loaded.

To opt out of HTTPS for the UI, set both paths to empty strings.

To generate the files manually instead, run:

```bash
python3 scripts/generate_self_signed_cert.py
```

If you change `buzz.yml`, stop and start the affected services. `docker compose
restart` is not supported for this stack.

```bash
docker compose stop buzz-dav buzz-curator
docker compose start buzz-dav buzz-curator
```

To inspect machine-managed state:

```bash
sqlite3 data/buzz.sqlite ".tables"
```

For the database tables and ownership model, see [State
Model](./docs/architecture/system.md#state-model).

## Architecture

For a deep dive into how Buzz works, components, and data flow, see the
[Architecture Documentation](./docs/architecture/system.md), especially [DAV
Service Internals](./docs/architecture/system.md#dav-service-internals),
[Curator Service
Internals](./docs/architecture/system.md#curator-service-internals), and [Media
Server Refresh](./docs/architecture/system.md#media-server-refresh).

### Library Safety

Debrid mounts are flaky: Real-Debrid hosters go offline, rclone VFS listings
lag behind reality, and a torrent can briefly look empty before it reappears.
Naively forwarding every "something changed" signal to Jellyfin will cause it
to mark items as deleted, prune extracted metadata for every file, and rescan
the entire library. Buzz inserts a few safeguards between Real-Debrid and the
media server so the library survives those transients:

- **Scan probe** before triggering a Jellyfin scan. Buzz reads a sample of
  source files through the rclone mount; if the mount is empty or unreadable,
  the scan is *not* triggered. Prevents Jellyfin from "discovering" that a
  flaky Real-Debrid mount has zero files and removing items it shouldn't.
  Tunable under `media_server.scan_probe.*`.
- **Stable per-file mtimes**. Files exposed via WebDAV use the Real-Debrid
  torrent's `added` time, not "now-at-snapshot-rebuild". Prevents Jellyfin's
  `File changed, pruning extracted data` storm on libraries that haven't
  actually changed.
- **Symlink-preserving curator merge**. Curator rebuilds keep unchanged
  symlinks in place by target, so Jellyfin doesn't see ctime/inode churn for
  unmodified content.
- **Selective per-library refresh**. Only refreshes the Jellyfin library
  whose category actually changed (e.g. only `Movies` when a movie was added),
  falling back to a full scan only when categories can't be mapped via
  `media_server.library_map`.
- **VFS visibility wait**. Curator waits for rclone to surface new files at
  the mount before triggering a scan, so Jellyfin doesn't scan a path that's
  about to fill in and treat it as empty.
- **Jellyfin auth probe on startup**. Curator validates
  `media_server.jellyfin.api_key` against the live server before doing any
  scan-triggering work, distinguishing an invalid token from a transient
  unreachable Jellyfin.
- **Canonical snapshot diff**. Internal change detection strips volatile
  fields before comparing snapshots, so only genuine content deltas count as
  "changed roots".
- **Real-Debrid error caching**. Non-transient hoster errors are cached for a
  short TTL so retries don't hammer the Real-Debrid API.
- **Internal categories never trigger scans**. The virtual `__unplayable__`
  and `__all__` directories are filtered out of scan triggers — only real
  category changes (`movies`, `shows`, `anime`) reach the media server.

For the underlying flow, see [Media Server
Refresh](./docs/architecture/system.md#media-server-refresh).

## Development

The DAV service lives in [`buzz/dav_app.py`](./buzz/dav_app.py) and the curator
service in [`buzz/curator_app.py`](./buzz/curator_app.py); the container image
is built from [`buzz/Dockerfile`](./buzz/Dockerfile).

For everyday hacking, use the development override
[`docker-compose.dev.yml`](./docker-compose.dev.yml) to mount your local code
directly into the containers (`- ./:/app`):

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml up \
  --pull always \
  --detach \
  --build
```

Source changes take effect immediately after stopping and starting the service
(`docker compose stop buzz-dav && docker compose start buzz-dav`) without
rebuilding the image. If you prefer an isolated environment, you can spin up a
full development VM with [Incus](./docs/incus-dev-vm.md). In production,
`docker compose up -d` runs the stable, immutable code baked into the image;
rebuild it after changes with `docker compose up -d --build`.

For the GitLab registry image, CI components, and development override model,
see [Deployment And CI
Architecture](./docs/architecture/system.md#deployment-and-ci-architecture).

Repository maintenance tools are documented in
[`maint-scripts/README.md`](./maint-scripts/README.md). This includes the
container image digest updater and the README UI screenshot renderer.

To refresh the pinned images used by Compose, `buzz/Dockerfile`, and Buzz-owned
GitLab CI jobs, run:

```bash
uv run ./maint-scripts/update_dependency_refs.py
```

Python dependencies are declared in [`pyproject.toml`](./pyproject.toml) and
locked in [`uv.lock`](./uv.lock). To update them manually, edit the version
ranges in `pyproject.toml` when needed, run `uv lock --upgrade`, then run `uv
sync --group dev`, `uv run pytest`, and `uvx pyright buzz tests`.

To preview Renovate updates locally with Docker, run it against the checked-out
repository:

```sh
docker run --rm \
  -e RENOVATE_PLATFORM=local \
  -e RENOVATE_REPOSITORIES=/workspace \
  -e LOG_LEVEL=debug \
  -v "$PWD:/workspace" \
  renovate/renovate
```

Add registry tokens as extra `-e` flags if you need Renovate to resolve private
dependencies or avoid public registry rate limits.

Run the test suite locally with `uv run pytest`. We also keep templates clean
with `htmlhint` (configured via `.htmlhintrc` in the root):

```sh
npx htmlhint "buzz/pyview_templates/*.html"
```

To refresh the README UI screenshots after interface changes, install the
Playwright browser once and run the screenshot generator:

```sh
uv run playwright install firefox
uv run python maint-scripts/render_ui_props.py
```

If you are migrating from an older configuration format, the helper in
[`scripts/migrate_config.py`](./scripts/migrate_config.py) can assist with the
conversion.
