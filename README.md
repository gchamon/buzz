# buzz

`buzz` is a small Debrid WebDAV service for [Jellyfin](https://jellyfin.org/).
[Plex](https://watch.plex.tv) integration also exists in the codebase but is
currently **untested**; use it at your own risk.

The canonical repository for Buzz is https://gitlab.com/gabriel.chamon/buzz.

![screenshot](/docs/assets/screenshot.png) 

## Layout

- `buzz` serves read-only WebDAV on `http://localhost:9999/dav`
- `rclone` mounts that WebDAV tree at `/mnt/buzz/raw`
- Media content is exposed under:
  - `/mnt/buzz/raw`: Original Real-Debrid files (via `rclone`)
  - `/mnt/buzz/curated`: Symbolic link library (via `buzz-curator`)
- Media server libraries should point to subfolders of `/mnt/buzz/curated`
(e.g., `movies`, `shows`, `animes`).

For the full service topology and data flow, see [Runtime
Topology](./docs/architecture.md#runtime-topology).

## Library Safety

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
Refresh](./docs/architecture.md#media-server-refresh).

## Host Preparation

Before starting the stack, ensure the required host directories exist and have
the correct permissions (User/Group ID `1000` is the default for most
containers):

```sh # 1. Create mountpoints and state directories sudo mkdir -p /mnt/buzz/raw
/mnt/buzz/curated /mnt/buzz/subs/{movies,shows,anime} mkdir -p data
cache/jellyfin config/plex config/jellyfin

# 2. Set ownership to the container user (1000:1000) sudo chown -R 1000:1000
/mnt/buzz data cache/jellyfin config/plex config/jellyfin ```

## Quick Start

The following steps are used to deploy buzz with jellyfin.

1. Download the deployment files from the canonical repository:

```bash curl -fsSLO
https://gitlab.com/gabriel.chamon/buzz/-/raw/main/docker-compose.yml curl -fsSL
https://gitlab.com/gabriel.chamon/buzz/-/raw/main/buzz.min.yml -o buzz.yml

# Full annotated config, alternatively: # curl -fsSL
https://gitlab.com/gabriel.chamon/buzz/-/raw/main/buzz.dist.yml -o buzz.yml ```

2. Set your Real-Debrid token in `buzz.yml`. Every other setting in `buzz.yml`
(Jellyfin URL/API key, subtitles, library mapping, etc.) can be edited live
from the Buzz web UI after first boot — you don't need to hand-edit them
upfront.

3. *(Optional)* Download `.env.dist` as `.env` only if you need to override
`PUID`/`PGID`, tune the DAV memory cap, opt into the Plex profile, or tune Plex
container settings. The stack defaults to Jellyfin and runs without an `.env`
file.

   ```bash curl -fsSL
   https://gitlab.com/gabriel.chamon/buzz/-/raw/main/.env.dist -o .env ```

4. Perform the **Host Preparation** steps above.

5. Start the stack:

```bash docker compose up --pull always --detach ```

6. Verify the WebDAV mount:

```bash time ls -1R /mnt/buzz ```

### Configure Jellyfin

The Jellyfin container starts alongside the rest of the stack but needs a
one-time setup pass through its web UI before Buzz can drive library scans.

1. Wait for Jellyfin to finish loading at
[http://localhost:8096](http://localhost:8096). The first boot may take a
minute while it initializes its config volume.
2. Step through the setup wizard: pick a display language, then create the
admin user with a username and password of your choosing.
3. On the **Set up media libraries** step, add one library per category you
want Buzz to manage, pointing each at the matching folder under
`/mnt/buzz/curated`:
   - **Movies** → content type `Movies`, folder `/mnt/buzz/curated/movies`
   - **TV Shows** → content type `Shows`, folder `/mnt/buzz/curated/shows`
   - **Anime** → content type `Shows`, folder `/mnt/buzz/curated/anime`

   The library names must match `media_server.library_map` in `buzz.yml`
   (defaults: `Movies`, `TV Shows`, `Anime`). Finish the wizard with the
   remaining defaults.
4. Optional: to let Buzz trigger Jellyfin library scans after Curator rebuilds,
set `media_server.trigger_lib_scan: true`. Then, in the Jellyfin UI, open
**Dashboard → API Keys**, click the **+** button, give the key an app name
(e.g. `buzz`), and copy the generated key.
5. Paste the key into `buzz.yml` under `media_server.jellyfin.api_key`.
6. Stop and start the Buzz services so the curator picks up the new key.
`docker compose restart` is not supported for this stack; use stop/start or
down/up instead.

   ```bash docker compose stop buzz-dav buzz-curator docker compose start
   buzz-dav buzz-curator ```

### HTTPS UI

Buzz serves the WebDAV endpoint on plain HTTP port `9999`. The browser UI runs
on HTTPS port `9443` by default, and HTTP UI paths redirect there.

The default self-signed certificate paths in `buzz.yml` are CWD-relative:

```yaml tls: cert_path: data/tls/buzz.crt key_path: data/tls/buzz.key ```

Inside the container, the working directory is `/app`, so those paths land in
`/app/data/tls`. On startup, `buzz-dav` creates missing TLS material and renews
certs that expire within 30 days. It also checks weekly; when a runtime renewal
happens, `buzz-dav` exits so Docker restarts it with the new certificate
loaded.

To opt out of HTTPS for the UI, set both paths to empty strings.

To generate the files manually instead, run:

```bash python3 scripts/generate_self_signed_cert.py ```

If you change `buzz.yml`, stop and start the affected services. `docker compose
restart` is not supported for this stack.

```bash docker compose stop buzz-dav buzz-curator docker compose start buzz-dav
buzz-curator ```

To inspect machine-managed state:

```bash sqlite3 data/buzz.sqlite ".tables" ```

For the database tables and ownership model, see [State
Model](./docs/architecture.md#state-model).

## Configuration Reference

### `buzz.yml`

This file handles the DAV server logic, media-server integration, and RD
polling. **Every key documented below is editable live from the Buzz web UI** —
you generally don't need to hand-edit `buzz.yml` beyond `provider.token` and
opensubtitles.com credentials.

For config merge, masking, and reload behavior, see [Configuration
Model](./docs/architecture.md#configuration-model).

| Key | Default | Description | | :--- | :--- | :--- | | `provider.token` |
*(Required)* | Your Real-Debrid API token. | |
`provider.connection_concurrency` | `4` | Maximum concurrent Real-Debrid stream
setup/opening operations. Active media streams are intentionally not
concurrency- or rate-limited by this setting and must not share
metadata/listing throttles used for `PROPFIND`, GET setup, directory lists, or
scans. | | `provider.poll_interval_secs` | `10` | How often Buzz polls
Real-Debrid for changes (seconds). | | `server.bind` | `0.0.0.0` | IP address
the DAV server binds to. | | `server.port` | `9999` | Port for the DAV server
(TCP port). | | `ui.poll_interval_secs` | `3` | How often the Buzz web UI polls
for updates (seconds). | | `tls.cert_path` | `data/tls/buzz.crt` | TLS
certificate path for the HTTPS UI on port `9443` (container path); relative
paths resolve from the process working directory. Set both TLS paths to empty
strings to opt out. | | `tls.key_path` | `data/tls/buzz.key` | TLS private key
path (container path). Buzz creates and renews missing or expiring certs
automatically. | | `state_dir` | `/app/data` | Shared path used by both
`buzz-dav` and `buzz-curator` for `buzz.sqlite` and related state (container
path). | | `hooks.on_library_change` | `sh /app/scripts/media_update.sh` |
Shell command executed when a change in the library is detected. | |
`hooks.curator_url` | `http://buzz-curator:8400/rebuild` | Internal URL to
trigger the Curator rebuild. | | `hooks.rd_update_delay_secs` | `15` | Delay
before triggering a hook on RD updates (seconds). | | `compat.enable_all_dir` |
`true` | Exposes an `__all__` directory via WebDAV containing all playable
files (boolean). | | `compat.enable_unplayable_dir` | `true` | Exposes an
`__unplayable__` directory for files that aren't video files (boolean). | |
`directories.anime.patterns` | *(Default regex list)* | List of regex patterns
used to categorize files as Anime. | | `request_timeout_secs` | `30` | Timeout
for API requests to Real-Debrid (seconds). | | `logging.verbose` | `false` |
Enable verbose request and debug logging (boolean). | | `logging.max_entries` |
`1000` | Maximum number of log entries to keep in the UI log view (count). | |
`media_server.kind` | `jellyfin` | Which media server Buzz drives. `jellyfin`
or `plex` (Plex is currently **untested**). | | `media_server.trigger_lib_scan`
| `false` | Trigger media-server library scans after Curator rebuilds
(boolean). When true for Jellyfin, `media_server.jellyfin.api_key` is required
and validated on Curator startup. | | `media_server.jellyfin.url` |
`http://jellyfin:8096` | URL to the Jellyfin server (must be reachable from the
Buzz container). | | `media_server.jellyfin.api_key` | *(Empty)* | Jellyfin API
Key used to trigger library scans. | | `media_server.jellyfin.scan_task_id` |
*(Empty)* | Optional. Used if automatic Jellyfin task discovery fails. | |
`media_server.plex.url` | *(Empty)* | URL to the Plex server, e.g.
`http://127.0.0.1:32400`. *(untested)* | | `media_server.plex.token` |
*(Empty)* | Plex Access Token for library update API calls. *(untested)* | |
`media_server.library_map.{movies,shows,anime}` | `Movies` / `TV Shows` /
`Anime` | Maps debrid category directories to Jellyfin library names. |

These settings control the Jellyfin scan probe — buzz reads a sample of
source files through the rclone mount before each scan; if the read fails,
the scan is skipped to prevent Jellyfin from removing items because of a
flaky mount. See [Library Safety](#library-safety).

| Key | Default | Description |
| :--- | :--- | :--- |
| `media_server.scan_probe.enabled` | `true` | Enable the pre-scan probe (boolean). |
| `media_server.scan_probe.sample_ratio_percent` | `10` | Percentage of source files to read in each probe attempt (integer). |
| `media_server.scan_probe.min_files` | `1` | Minimum number of files to sample, regardless of `sample_ratio_percent` (integer). |
| `media_server.scan_probe.max_attempts` | `3` | How many times the probe re-rolls a fresh sample before giving up (integer). |
| `media_server.scan_probe.read_bytes` | `524288` | Bytes to read from each sampled file (integer, default 512 KiB). |
| `media_server.scan_probe.retry_delay_secs` | `10` | Delay between probe attempts (seconds). |
| `media_server.scan_probe.concurrency` | `4` | Maximum parallel file reads within a single probe attempt (integer). |

These settings control the automatic subtitle fetcher.

| Key | Default | Description | | :--- | :--- | :--- | | `subtitles.enabled` |
`false` | Whether to enable automatic subtitle fetching from OpenSubtitles
(boolean). | | `subtitles.opensubtitles.api_key` | *(Required)* | Your
OpenSubtitles.com API Key. | | `subtitles.opensubtitles.username` |
*(Required)* | Your OpenSubtitles.com username. | |
`subtitles.opensubtitles.password` | *(Required)* | Your OpenSubtitles.com
password. | | `subtitles.languages` | `[en]` | List of language codes to
download (e.g., `[en, pt-br]`). | | `subtitles.strategy` | `most-downloaded` |
Ranking strategy: `best-match`, `most-downloaded`, `best-rated`, `trusted`,
`latest`. | | `subtitles.filters.hearing_impaired` | `exclude` | Handling of HI
tracks: `exclude`, `include`, `prefer`. | | `subtitles.filters.exclude_ai` |
`true` | Exclude AI-translated subtitles (boolean). | |
`subtitles.filters.exclude_machine` | `true` | Exclude machine-translated
subtitles (boolean). | | `subtitles.search_delay_secs` | `0.5` | Delay between
API search calls (seconds). | | `subtitles.download_delay_secs` | `1.0` | Delay
between download calls (seconds). | | `subtitles.root` | `/mnt/buzz/subs` |
Path inside the container where downloaded `.srt` files are stored (container
path). |

For the subtitle overlay, metadata, and fetch pipeline, see [Subtitle
Pipeline](./docs/architecture.md#subtitle-pipeline).

Complete example:

```yaml provider: token: "YOUR_RD_TOKEN" connection_concurrency: 4
poll_interval_secs: 10

server: bind: "0.0.0.0" port: 9999

tls: cert_path: data/tls/buzz.crt key_path: data/tls/buzz.key

state_dir: "/app/data"

hooks: on_library_change: "bash /app/scripts/media_update.sh" curator_url:
"http://buzz-curator:8400/rebuild" rd_update_delay_secs: 15

media_server: kind: jellyfin trigger_lib_scan: true jellyfin: url:
http://jellyfin:8096 api_key: "YOUR_JELLYFIN_API_KEY" scan_task_id: ""
library_map: movies: Movies shows: TV Shows anime: Anime

subtitles: enabled: true opensubtitles: api_key: "YOUR_API_KEY" username:
"YOUR_USERNAME" password: "YOUR_PASSWORD" languages:
- en
- pt-br strategy: most-downloaded filters: hearing_impaired: exclude
exclude_ai: true exclude_machine: true root: /mnt/buzz/subs

logging: verbose: false ```

### `.env` *(optional)*

The stack runs without an `.env` file — Jellyfin is the default media server
profile, and `PUID`/`PGID` default to `1000`. Create an `.env` only if you need
to override one of these values:

| Variable | Default | Description | | :--- | :--- | :--- | | `PUID` / `PGID` |
`1000` | User and Group ID used for creating host-owned files and for running
services. Override only if your host UID/GID differs. | |
`BUZZ_DAV_MEMORY_LIMIT` | `2g` | Docker memory cap for `buzz-dav`, so DAV
scan/playback pressure is contained to that container. | |
`BUZZ_DAV_MEMORY_SWAP_LIMIT` | `BUZZ_DAV_MEMORY_LIMIT` | Docker memory plus
swap cap for `buzz-dav`. Defaults to the memory cap, allowing no extra swap to
avoid disk wear. | | `COMPOSE_PROFILES` | *(unset → Jellyfin)* | Set to `plex`
to swap the Jellyfin container for the Plex container. Plex support is
currently **untested**. | | `PLEX_VERSION` | `docker` | Plex container image
version. Only consumed when the `plex` profile is active. | | `PLEX_CLAIM` |
*(Empty)* | Plex claim token for first-time setup. Only consumed when the
`plex` profile is active. |

All Jellyfin/Plex/subtitle credentials and URLs now live in `buzz.yml` and are
editable from the web UI.

## Architecture

For a deep dive into how Buzz works, components, and data flow, see the
[Architecture Documentation](./docs/architecture.md), especially [DAV Service
Internals](./docs/architecture.md#dav-service-internals), [Curator Service
Internals](./docs/architecture.md#curator-service-internals), and [Media Server
Refresh](./docs/architecture.md#media-server-refresh).

## Development

The DAV service lives in [`buzz/dav_app.py`](./buzz/dav_app.py) and the curator
service in [`buzz/curator_app.py`](./buzz/curator_app.py); the container image
is built from [`buzz/Dockerfile`](./buzz/Dockerfile).

For everyday hacking, use the development override
[`docker-compose.dev.yml`](./docker-compose.dev.yml) to mount your local code
directly into the containers (`- ./:/app`):

```sh docker compose \ -f docker-compose.yml \ -f docker-compose.dev.yml up \
--pull always \ --detach \ --build ```

Source changes take effect immediately after stopping and starting the service
(`docker compose stop buzz-dav && docker compose start buzz-dav`) without
rebuilding the image. If you prefer an isolated environment, you can spin up a
full development VM with [Incus](./docs/incus-dev-vm.md). In production,
`docker compose up -d` runs the stable, immutable code baked into the image;
rebuild it after changes with `docker compose up -d --build`.

For the GitLab registry image, CI components, and development override model,
see [Deployment And CI
Architecture](./docs/architecture.md#deployment-and-ci-architecture).

To refresh the pinned images used by Compose, `buzz/Dockerfile`, and Buzz-owned
GitLab CI jobs, run:

```bash uv run ./maint-scripts/update_dependency_refs.py ```

Python dependencies are declared in [`pyproject.toml`](./pyproject.toml) and
locked in [`uv.lock`](./uv.lock). To update them manually, edit the version
ranges in `pyproject.toml` when needed, run `uv lock --upgrade`, then run `uv
sync --group dev`, `uv run pytest`, and `uvx pyright buzz tests`.

To preview Renovate updates locally with Docker, run it against the checked-out
repository:

```sh docker run --rm \ -e RENOVATE_PLATFORM=local \ -e
RENOVATE_REPOSITORIES=/workspace \ -e LOG_LEVEL=debug \ -v "$PWD:/workspace" \
renovate/renovate ```

Add registry tokens as extra `-e` flags if you need Renovate to resolve private
dependencies or avoid public registry rate limits.

Run the test suite locally with `uv run pytest`. We also keep templates clean
with `htmlhint` (configured via `.htmlhintrc` in the root):

```sh npx htmlhint "buzz/pyview_templates/*.html" ```

If you are migrating from an older configuration format, the helper in
[`scripts/migrate_config.py`](./scripts/migrate_config.py) can assist with the
conversion.
