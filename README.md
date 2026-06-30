# 🐝buzz

<!--toc:start-->
- [🐝buzz](#🐝buzz)
  - [Layout](#layout)
  - [Quick Start](#quick-start)
    - [Host Preparation](#host-preparation)
    - [Deployment](#deployment)
    - [Configure Jellyfin](#configure-jellyfin)
  - [Configuration Reference](#configuration-reference)
    - [`buzz.yml`](#buzzyml)
    - [`.env` *(optional)*](#env-optional)
  - [Contributing](#contributing)
<!--toc:end-->

`buzz` is a small Debrid WebDAV service that turns
[Real-Debrid](https://real-debrid.com/) and [TorBox](https://torbox.app/)
torrent caches into a stable, [Jellyfin](https://jellyfin.org/)-ready
presentation library. It exposes provider files over WebDAV, lets you shape
category, identity, and file-selection overrides in the UI, integrates with
[OpenSubtitles](https://www.opensubtitles.com/), and rebuilds a curated folder
tree Jellyfin can scan directly.

The canonical repo for Buzz is <https://gitlab.com/gabriel.chamon/buzz> and the
mirror repo, <https://github.com/gchamon/buzz>. 

![cache UI: torrent cache management with unified interface for adding new entries for all debrid providers](/docs/assets/ui/cache.png)

**Cache:** Torrent cache management with unified interface for adding new
entries for all debrid providers. Per entry category, identity and file
selection override to prepare the presentation of the files for Jellyfin.

![archive UI: persistent history of all magnet entries](/docs/assets/ui/archive.png)

**Archive:** Persistent history of all magnet entries. Delete them from your
providers to save on limits, but have a restore copy ready.

![logs UI: general logs of the system](/docs/assets/ui/logs.png)

**Logs:** General logs of the system so that it's easier to report any problem
you see.

![threads UI: background tasks and their logs](/docs/assets/ui/threads.png)

**Threads:** Threads are background tasks and their logs.

![config UI: server configuration management](/docs/assets/ui/config.png)

**Config:** Manage server configuration directly in the UI, without having to
change YAML files and redeploy containers.

## Layout

- `buzz` serves read-only WebDAV on `http://localhost:9999/dav`
- `rclone` mounts that WebDAV tree at `/mnt/buzz/raw`
- Media content is exposed under:
  - `/mnt/buzz/raw`: Original Real-Debrid files (via `rclone`)
  - `/mnt/buzz/curated`: Symbolic link library (via `buzz-curator`)
- Media server libraries should point to subfolders of `/mnt/buzz/curated`
(e.g., `movies`, `shows`, `animes`).

For the full service topology and data flow, see [Runtime
Topology](./docs/architecture/system.md#runtime-topology).


## Quick Start

The following steps are used to deploy buzz with jellyfin.

### Host Preparation

Before starting the stack, ensure the required host directories exist and have
the correct permissions (User/Group ID `1000` is the default for most
containers):

```sh
[ -f ./.env ] && source ./.env
BUZZ_MOUNT_ROOT=${BUZZ_MOUNT_ROOT:-/mnt/buzz}

sudo mkdir -p "${BUZZ_MOUNT_ROOT}"/{raw,curated} "${BUZZ_MOUNT_ROOT}"/subs/{movies,shows,anime}
sudo chown -R $USER: "${BUZZ_MOUNT_ROOT}"
mkdir -p data cache/jellyfin config/plex config/jellyfin
```

Relative paths aren't supported so if you need to customize `BUZZ_MOUNT_ROOT`,
put in `.env`:

```sh
BUZZ_MOUNT_ROOT=$PWD/mount
```

### Deployment

1. Download the deployment files from the canonical repository:

```bash
curl -fsSLO https://gitlab.com/gabriel.chamon/buzz/-/raw/main/docker-compose.yml
curl -fsSL https://gitlab.com/gabriel.chamon/buzz/-/raw/main/buzz.min.yml -o buzz.yml

# Full annotated config, alternatively:
# curl -fsSL https://gitlab.com/gabriel.chamon/buzz/-/raw/main/buzz.dist.yml -o buzz.yml
```

2. Set your Real-Debrid token in `buzz.yml`. Every other setting in `buzz.yml`
(Jellyfin URL/API key, subtitles, library mapping, etc.) can be edited live
from the Buzz web UI after first boot — you don't need to hand-edit them
upfront.

3. *(Optional)* Download `.env.dist` as `.env` only if you need to override
`PUID`/`PGID`, tune the DAV memory cap, opt into the Plex profile, or tune Plex
container settings. The stack defaults to Jellyfin and runs without an `.env`
file.

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
time ls -1R "${BUZZ_MOUNT_ROOT:-/mnt/buzz}"
```

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
   (defaults: `Movies`, `TV Shows`, `Anime`). Custom categories are defined
   separately in `categories`. Finish the wizard with the remaining defaults.
4. Optional: to let Buzz trigger Jellyfin library scans after Curator rebuilds,
set `media_server.trigger_lib_scan: true`. Then, in the Jellyfin UI, open
**Dashboard → API Keys**, click the **+** button, give the key an app name
(e.g. `buzz`), and copy the generated key.
5. Paste the key into `buzz.yml` under `media_server.jellyfin.api_key`.
6. Stop and start the Buzz services so the curator picks up the new key.
`docker compose restart` is not supported for this stack; use stop/start or
down/up instead.

```bash
docker compose stop buzz-dav buzz-curator
docker compose start buzz-dav buzz-curator
```

For HTTPS UI details, certificate behavior, architecture notes, and development
workflows, see [Contributing](./CONTRIBUTING.md).

## Configuration Reference

### `buzz.yml`

This file handles the DAV server logic, media-server integration, and RD
polling. **Every key documented below is editable live from the Buzz web UI** —
you generally don't need to hand-edit `buzz.yml` beyond `provider.token` and
opensubtitles.com credentials.

For config merge, masking, and reload behavior, see [Configuration
Model](./docs/architecture/system.md#configuration-model).

| Key | Default | Description |
| :--- | :--- | :--- |
| `provider.token` | *(Required)* | Your Real-Debrid API token. |
| `provider.connection_concurrency` | `4` | Maximum concurrent Real-Debrid stream setup/opening operations. Active media streams are intentionally not concurrency- or rate-limited by this setting and must not share metadata/listing throttles used for `PROPFIND`, GET setup, directory lists, or scans. |
| `provider.poll_interval_secs` | `10` | How often Buzz polls Real-Debrid for changes (seconds). |
| `server.bind` | `0.0.0.0` | IP address the DAV server binds to. |
| `server.port` | `9999` | Port for the DAV server (TCP port). |
| `ui.poll_interval_secs` | `3` | How often the Buzz web UI polls for updates (seconds). |
| `tls.cert_path` | `data/tls/buzz.crt` | TLS certificate path for the HTTPS UI on port `9443` (container path); relative paths resolve from the process working directory. Set both TLS paths to empty strings to opt out. |
| `tls.key_path` | `data/tls/buzz.key` | TLS private key path (container path). Buzz creates and renews missing or expiring certs automatically. |
| `state_dir` | `/app/data` | Shared path used by both `buzz-dav` and `buzz-curator` for `buzz.sqlite` and related state (container path). |
| `hooks.on_library_change` | `sh /app/scripts/media_update.sh` | Shell command executed when a change in the library is detected. |
| `hooks.curator_url` | `http://buzz-curator:8400/rebuild` | Internal URL to trigger the Curator rebuild. |
| `hooks.rd_update_delay_secs` | `15` | Delay before triggering a hook on RD updates (seconds). |
| `compat.enable_all_dir` | `true` | Exposes an `__all__` directory via WebDAV containing all playable files (boolean). |
| `compat.enable_unplayable_dir` | `true` | Exposes an `__unplayable__` directory for files that aren't video files (boolean). |
| `directories.anime.patterns` | *(Default regex list)* | List of regex patterns used to categorize files as Anime. |
| `request_timeout_secs` | `30` | Timeout for API requests to Real-Debrid (seconds). |
| `logging.verbose` | `false` | Enable verbose request and debug logging (boolean). |
| `logging.max_entries` | `1000` | Maximum number of log entries to keep in the Logs UI for internal events (`/logs`) (count). See [`buzz/events.py`](buzz/events.py). |
| `media_server.kind` | `jellyfin` | Which media server Buzz drives. `jellyfin` or `plex` (Plex is currently **untested**). |
| `media_server.trigger_lib_scan` | `false` | Trigger media-server library scans after Curator rebuilds (boolean). When true for Jellyfin, `media_server.jellyfin.api_key` is required and validated on Curator startup. |
| `media_server.jellyfin.url` | `http://jellyfin:8096` | URL to the Jellyfin server (must be reachable from the Buzz container). |
| `media_server.jellyfin.api_key` | *(Empty)* | Jellyfin API Key used to trigger library scans. |
| `media_server.jellyfin.scan_task_id` | *(Empty)* | Optional. Used if automatic Jellyfin task discovery fails. |
| `media_server.plex.url` | *(Empty)* | URL to the Plex server, e.g. `http://127.0.0.1:32400`. *(untested)* |
| `media_server.plex.token` | *(Empty)* | Plex Access Token for library update API calls. *(untested)* |
| `media_server.library_map` | `Movies` / `TV Shows` / `Anime` | Maps debrid category directories to Jellyfin library names. |
| `categories` | `{}` | Custom category names and kinds. Built-ins `movies`, `shows`, and `anime` stay embedded in the code. |

These settings control the Jellyfin scan probe — buzz reads a sample of
source files through the rclone mount before each scan; if the read fails,
the scan is skipped to prevent Jellyfin from removing items because of a
flaky mount. See [Library
Safety](./CONTRIBUTING.md#library-safety).

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

| Key | Default | Description |
| :--- | :--- | :--- |
| `subtitles.enabled` | `false` | Whether to enable automatic subtitle fetching from OpenSubtitles (boolean). |
| `subtitles.opensubtitles.api_key` | *(Required)* | Your OpenSubtitles.com API Key. |
| `subtitles.opensubtitles.username` | *(Required)* | Your OpenSubtitles.com username. |
| `subtitles.opensubtitles.password` | *(Required)* | Your OpenSubtitles.com password. |
| `subtitles.languages` | `[en]` | List of language codes to download (e.g., `[en, pt-br]`). |
| `subtitles.strategy` | `most-downloaded` | Ranking strategy: `best-match`, `most-downloaded`, `best-rated`, `trusted`, `latest`. |
| `subtitles.filters.hearing_impaired` | `exclude` | Handling of HI tracks: `exclude`, `include`, `prefer`. |
| `subtitles.filters.exclude_ai` | `true` | Exclude AI-translated subtitles (boolean). |
| `subtitles.filters.exclude_machine` | `true` | Exclude machine-translated subtitles (boolean). |
| `subtitles.search_delay_secs` | `0.5` | Delay between API search calls (seconds). |
| `subtitles.download_delay_secs` | `1.0` | Delay between download calls (seconds). |
| `subtitles.root` | `$BUZZ_MOUNT_ROOT/subs` | Path inside the container where downloaded `.srt` files are stored (container path). Defaults to the `subs/` subdirectory of `BUZZ_MOUNT_ROOT`; set explicitly only to override. |

For the subtitle overlay, metadata, and fetch pipeline, see [Subtitle
Pipeline](./docs/architecture/system.md#subtitle-pipeline).

Complete example:

```yaml
provider:
  connection_concurrency: 4
  poll_interval_secs: 10

server:
  bind: "0.0.0.0"
  port: 9999

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
  trigger_lib_scan: true
  jellyfin:
    url: http://jellyfin:8096
    api_key: "YOUR_JELLYFIN_API_KEY"
    scan_task_id: ""
  # Built-in categories are embedded in code.
  library_map:
    movies: Movies
    shows: TV Shows
    anime: Anime

categories:
  documentaries: movie
  kids shows: show

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

logging:
  verbose: false
```

### `.env` *(optional)*

The stack runs without an `.env` file — Jellyfin is the default media server
profile, and `PUID`/`PGID` default to `1000`. Create an `.env` only if you need
to override one of these values:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PUID` / `PGID` | `1000` | User and Group ID used for creating host-owned files and for running services. Override only if your host UID/GID differs. |
| `BUZZ_MOUNT_ROOT` | `/mnt/buzz` | Base path on the host where `raw/`, `curated/`, and `subs/` are mounted. Change this if you want the mount tree somewhere other than `/mnt/buzz`. |
| `BUZZ_DAV_MEMORY_LIMIT` | `2g` | Docker memory cap for `buzz-dav`, so DAV scan/playback pressure is contained to that container. |
| `BUZZ_DAV_MEMORY_SWAP_LIMIT` | `BUZZ_DAV_MEMORY_LIMIT` | Docker memory plus swap cap for `buzz-dav`. Defaults to the memory cap, allowing no extra swap to avoid disk wear. |
| `COMPOSE_PROFILES` | *(unset → Jellyfin)* | Set to `plex` to swap the Jellyfin container for the Plex container. Plex support is currently **untested**. |
| `PLEX_VERSION` | `docker` | Plex container image version. Only consumed when the `plex` profile is active. |
| `PLEX_CLAIM` | *(Empty)* | Plex claim token for first-time setup. Only consumed when the `plex` profile is active. |

All Jellyfin/Plex/subtitle credentials and URLs now live in `buzz.yml` and are
editable from the web UI.

## Contributing

HTTPS UI details, architecture notes, and development workflows now live in
[CONTRIBUTING.md](./CONTRIBUTING.md).
