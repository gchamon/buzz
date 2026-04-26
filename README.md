# buzz

`buzz` is a small Debrid WebDAV service for [Jellyfin](https://jellyfin.org/). [Plex](https://watch.plex.tv) integration also exists in the codebase but is currently **untested**; use it at your own risk.

The canonical repository for Buzz is
https://gitlab.com/gabriel.chamon/buzz.

![screenshot](/docs/assets/screenshot.png) 

## Layout

- `buzz` serves read-only WebDAV on `http://localhost:9999/dav`
- `rclone` mounts that WebDAV tree at `/mnt/buzz/raw`
- Media content is exposed under:
  - `/mnt/buzz/raw`: Original Real-Debrid files (via `rclone`)
  - `/mnt/buzz/curated`: Symbolic link library (via `buzz-curator`)
- Media server libraries should point to subfolders of `/mnt/buzz/curated` (e.g., `movies`, `shows`, `animes`).

For the full service topology and data flow, see
[Runtime Topology](./docs/architecture.md#runtime-topology).

## Host Preparation

Before starting the stack, ensure the required host directories exist and have the correct permissions (User/Group ID `1000` is the default for most containers):

```sh
# 1. Create mountpoints and state directories
sudo mkdir -p /mnt/buzz/raw /mnt/buzz/curated /mnt/buzz/subs/{movies,shows,anime}
mkdir -p data cache/jellyfin config/plex config/jellyfin

# 2. Set ownership to the container user (1000:1000)
sudo chown -R 1000:1000 /mnt/buzz data cache/jellyfin config/plex config/jellyfin
```

## Quick Start

The following steps are used to deploy buzz with jellyfin.

1. Download the deployment files from the canonical repository:

```bash
curl -fsSLO https://gitlab.com/gabriel.chamon/buzz/-/raw/main/docker-compose.yml
curl -fsSL https://gitlab.com/gabriel.chamon/buzz/-/raw/main/buzz.min.yml -o buzz.yml
```

2. Set your Real-Debrid token in `buzz.yml`. Every other setting in
   `buzz.yml` (Jellyfin URL/API key, subtitles, library mapping, etc.) can
   be edited live from the Buzz web UI after first boot — you don't need to
   hand-edit them upfront.

3. *(Optional)* Download `.env.dist` as `.env` only if you need to override
   `PUID`/`PGID`, opt into the Plex profile, or tune Plex container
   settings. The stack defaults to Jellyfin and runs without an `.env` file.

   ```bash
   curl -fsSL https://gitlab.com/gabriel.chamon/buzz/-/raw/main/.env.dist -o .env
   ```

4. Perform the **Host Preparation** steps above.

5. Start the stack:

```bash
docker compose up --pull always --detach
```

6. Verify the WebDAV mount:

```bash
time ls -1R /mnt/buzz
```

### Configure Jellyfin

The Jellyfin container starts alongside the rest of the stack but needs a
one-time setup pass through its web UI before Buzz can drive library
scans.

1. Wait for Jellyfin to finish loading at
   [http://localhost:8096](http://localhost:8096). The first boot may take
   a minute while it initializes its config volume.
2. Step through the setup wizard: pick a display language, then create the
   admin user with a username and password of your choosing.
3. On the **Set up media libraries** step, add one library per category
   you want Buzz to manage, pointing each at the matching folder under
   `/mnt/buzz/curated`:
   - **Movies** → content type `Movies`, folder `/mnt/buzz/curated/movies`
   - **TV Shows** → content type `Shows`, folder `/mnt/buzz/curated/shows`
   - **Anime** → content type `Shows`, folder `/mnt/buzz/curated/anime`

   The library names must match `media_server.library_map` in `buzz.yml`
   (defaults: `Movies`, `TV Shows`, `Anime`). Finish the wizard with the
   remaining defaults.
4. In the Jellyfin UI, open **Dashboard → API Keys**, click the **+**
   button, give the key an app name (e.g. `buzz`), and copy the generated
   key.
5. Paste the key into `buzz.yml` under
   `media_server.jellyfin.api_key`. Secret fields aren't editable from the
   web UI yet — they live in `buzz.yml` until HTTPS and basic auth are in
   place (tracked in
   [`implement-basic-security`](./docs/work-items/implement-basic-security.md)).
6. Restart the Buzz services so the curator picks up the new key:

   ```bash
   docker compose restart buzz-dav buzz-curator
   ```

### HTTPS UI

Buzz serves the WebDAV endpoint on plain HTTP port `9999`. The browser UI
runs on HTTPS port `9443` by default, and HTTP UI paths redirect there.

The default self-signed certificate paths in `buzz.yml` are CWD-relative:

```yaml
tls:
  cert_path: data/tls/buzz.crt
  key_path: data/tls/buzz.key
```

Inside the container, the working directory is `/app`, so those paths land in
`/app/data/tls`. On startup, `buzz-dav` creates missing TLS material and
renews certs that expire within 30 days. It also checks weekly; when a runtime
renewal happens, `buzz-dav` exits so Docker restarts it with the new
certificate loaded.

To opt out of HTTPS for the UI, set both paths to empty strings.

To generate the files manually instead, run:

```bash
python3 scripts/generate_self_signed_cert.py
```

If you change `buzz.yml`, restart the service:

```bash
docker compose restart buzz
```

To inspect machine-managed state:

```bash
sqlite3 data/buzz.sqlite ".tables"
```

For the database tables and ownership model, see
[State Model](./docs/architecture.md#state-model).

## Configuration Reference

### `buzz.yml`

This file handles the DAV server logic, media-server integration, and
RD polling. **Every key documented below is editable live from the Buzz
web UI** — you generally don't need to hand-edit `buzz.yml` beyond
`provider.token` and opensubtitles.com credentials.

For config merge, masking, and reload behavior, see
[Configuration Model](./docs/architecture.md#configuration-model).

| Key | Default | Description |
| :--- | :--- | :--- |
| `provider.token` | *(Required)* | Your Real-Debrid API token. |
| `poll_interval_secs` | `10` | How often Buzz polls Real-Debrid for changes. |
| `server.bind` | `0.0.0.0` | IP address the DAV server binds to. |
| `server.port` | `9999` | Port for the DAV server. |
| `server.stream_buffer_size` | `0` | Read-ahead buffer size in bytes for streaming media (e.g., 50MB: `52428800`). Set to `0` to disable. |
| `tls.cert_path` | `data/tls/buzz.crt` | TLS certificate path for the HTTPS UI on port `9443`; relative paths resolve from the process working directory. Set both TLS paths to empty strings to opt out. |
| `tls.key_path` | `data/tls/buzz.key` | TLS private key path. Buzz creates and renews missing or expiring certs automatically. |
| `state_dir` | `/app/data` | Shared path used by both `buzz-dav` and `buzz-curator` for `buzz.sqlite` and related state. |
| `hooks.on_library_change` | `sh /app/scripts/media_update.sh` | Shell command executed when a change in the library is detected. |
| `hooks.curator_url` | `http://buzz-curator:8400/rebuild` | Internal URL to trigger the Curator rebuild. |
| `hooks.rd_update_delay_secs` | `15` | Delay before triggering a hook on RD updates (allows inventory to settle). |
| `compat.enable_all_dir` | `true` | Exposes an `__all__` directory via WebDAV containing all playable files. |
| `compat.enable_unplayable_dir` | `true` | Exposes an `__unplayable__` directory for files that aren't video files. |
| `directories.anime.patterns` | *(Default regex list)* | List of regex patterns used to categorize files as Anime. |
| `request_timeout_secs` | `30` | Timeout in seconds for API requests to Real-Debrid. |
| `logging.verbose` | `false` | Enable verbose request and debug logging. |
| `media_server.kind` | `jellyfin` | Which media server Buzz drives. `jellyfin` or `plex` (Plex is currently **untested**). |
| `media_server.jellyfin.url` | `http://jellyfin:8096` | URL to the Jellyfin server (must be reachable from the Buzz container). |
| `media_server.jellyfin.api_key` | *(Empty)* | Jellyfin API Key used to trigger library scans. |
| `media_server.jellyfin.scan_task_id` | *(Empty)* | Optional. Used if automatic Jellyfin task discovery fails. |
| `media_server.plex.url` | *(Empty)* | URL to the Plex server, e.g. `http://127.0.0.1:32400`. *(untested)* |
| `media_server.plex.token` | *(Empty)* | Plex Access Token for library update API calls. *(untested)* |
| `media_server.library_map.{movies,shows,anime}` | `Movies` / `TV Shows` / `Anime` | Maps debrid category directories to Jellyfin library names. |

These settings control the automatic subtitle fetcher.

| Key | Default | Description |
| :--- | :--- | :--- |
| `subtitles.enabled` | `false` | Whether to enable automatic subtitle fetching from OpenSubtitles. |
| `subtitles.opensubtitles.api_key` | *(Required)* | Your OpenSubtitles.com API Key (needed for search and download). |
| `subtitles.opensubtitles.username` | *(Required)* | Your OpenSubtitles.com username (needed for download authentication). |
| `subtitles.opensubtitles.password` | *(Required)* | Your OpenSubtitles.com password (needed for download authentication). |
| `subtitles.languages` | `[en]` | List of language codes to download (e.g., `[en, pt-br]`). Supports regional codes like `pt-br` and `pt-pt`. |
| `subtitles.strategy` | `most-downloaded` | Ranking strategy: `best-match`, `most-downloaded`, `best-rated`, `trusted`, `latest`. |
| `subtitles.filters.hearing_impaired` | `exclude` | Handling of HI tracks: `exclude`, `include`, `prefer`. |
| `subtitles.filters.exclude_ai` | `true` | Exclude AI-translated subtitles. |
| `subtitles.filters.exclude_machine` | `true` | Exclude machine-translated subtitles. |
| `subtitles.search_delay_secs` | `0.5` | Delay between API search calls to respect rate limits. |
| `subtitles.download_delay_secs` | `1.0` | Delay between download calls. |
| `subtitles.root` | `/mnt/buzz/subs` | Path inside the container where downloaded `.srt` files are stored. |

For the subtitle overlay, metadata, and fetch pipeline, see
[Subtitle Pipeline](./docs/architecture.md#subtitle-pipeline).

Complete example:

```yaml
provider:
  token: "YOUR_RD_TOKEN"

poll_interval_secs: 10

server:
  bind: "0.0.0.0"
  port: 9999
  stream_buffer_size: 52428800

tls:
  cert_path: data/tls/buzz.crt
  key_path: data/tls/buzz.key

state_dir: "/app/data"

hooks:
  on_library_change: "bash /app/scripts/media_update.sh"
  curator_url: "http://buzz-curator:8400/rebuild"
  rd_update_delay_secs: 15

media_server:
  kind: jellyfin
  jellyfin:
    url: http://jellyfin:8096
    api_key: "YOUR_JELLYFIN_API_KEY"
    scan_task_id: ""
  library_map:
    movies: Movies
    shows: TV Shows
    anime: Anime

subtitles:
  enabled: true
  opensubtitles:
    api_key: "YOUR_API_KEY"
    username: "YOUR_USERNAME"
    password: "YOUR_PASSWORD"
  languages:
    - en
    - pt-br
  strategy: most-downloaded
  filters:
    hearing_impaired: exclude
    exclude_ai: true
    exclude_machine: true
  root: /mnt/buzz/subs

logging:
  verbose: false
```

### `.env` *(optional)*

The stack runs without an `.env` file — Jellyfin is the default media
server profile, and `PUID`/`PGID` default to `1000`. Create an `.env`
only if you need to override one of these values:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PUID` / `PGID` | `1000` | User and Group ID used for creating host-owned files and for running services. Override only if your host UID/GID differs. |
| `COMPOSE_PROFILES` | *(unset → Jellyfin)* | Set to `plex` to swap the Jellyfin container for the Plex container. Plex support is currently **untested**. |
| `PLEX_VERSION` | `docker` | Plex container image version. Only consumed when the `plex` profile is active. |
| `PLEX_CLAIM` | *(Empty)* | Plex claim token for first-time setup. Only consumed when the `plex` profile is active. |

All Jellyfin/Plex/subtitle credentials and URLs now live in `buzz.yml`
and are editable from the web UI.

## Architecture

For a deep dive into how Buzz works, components, and data flow, see the
[Architecture Documentation](./docs/architecture.md), especially
[DAV Service Internals](./docs/architecture.md#dav-service-internals),
[Curator Service Internals](./docs/architecture.md#curator-service-internals),
and [Media Server Refresh](./docs/architecture.md#media-server-refresh).

## Development

The DAV service lives in [`buzz/dav_app.py`](./buzz/dav_app.py) and the curator service in [`buzz/curator_app.py`](./buzz/curator_app.py); the container image is built from [`buzz/Dockerfile`](./buzz/Dockerfile).

For everyday hacking, use the development override [`docker-compose.dev.yml`](./docker-compose.dev.yml) to mount your local code directly into the containers (`- ./:/app`):

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml up \
  --pull always \
  --detach \
  --build
```

Source changes take effect immediately after restarting the service (`docker compose restart buzz-dav`) without rebuilding the image. If you prefer an isolated environment, you can spin up a full development VM with [Incus](./docs/incus-dev-vm.md). In production, `docker compose up -d` runs the stable, immutable code baked into the image; rebuild it after changes with `docker compose up -d --build`.

For the GitLab registry image, CI components, and development override model,
see [Deployment And CI Architecture](./docs/architecture.md#deployment-and-ci-architecture).

Docker references are pinned as `tag@sha256:digest` so Renovate can propose
reviewable image updates. To refresh the pinned images used by Compose,
`buzz/Dockerfile`, and Buzz-owned GitLab CI jobs, run:

```bash
uv run ./maint-scripts/update_dependency_refs.py
```

Python dependencies are declared in [`pyproject.toml`](./pyproject.toml) and
locked in [`uv.lock`](./uv.lock). To update them manually, edit the version
ranges in `pyproject.toml` when needed, run `uv lock --upgrade`, then run
`uv sync --group dev`, `uv run python -m unittest discover -s tests`, and
`uvx pyright buzz tests`.

You can also use Renovate locally for a dry run:

```sh
npm_config_cache=/tmp/npm-cache npx --yes --package renovate renovate \
  --platform=local \
  --repository-cache=enabled \
  --dry-run=full
```

Local Renovate is useful for checking what it would propose, but the normal
update path should still be Renovate merge requests so the seven-day release
age gate and CI checks remain visible.

Run the test suite locally with `uv run python -m unittest discover -s tests`. We also keep templates clean with `htmlhint` (configured via `.htmlhintrc` in the root):

```sh
npx htmlhint "buzz/pyview_templates/*.html"
```

If you are migrating from an older configuration format, the helper in [`scripts/migrate_config.py`](./scripts/migrate_config.py) can assist with the conversion.
