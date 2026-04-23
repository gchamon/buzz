# buzz

`buzz` is a small Debrid WebDAV service for [Jellyfin](https://jellyfin.org/) and [Plex](https://watch.plex.tv).

![screenshot](/docs/assets/screenshot.png) 

## Layout

- `buzz` serves read-only WebDAV on `http://localhost:9999/dav`
- `rclone` mounts that WebDAV tree at `/mnt/buzz/raw`
- Media content is exposed under:
  - `/mnt/buzz/raw`: Original Real-Debrid files (via `rclone`)
  - `/mnt/buzz/curated`: Symbolic link library (via `buzz-curator`)
- Media server libraries should point to subfolders of `/mnt/buzz/curated` (e.g., `movies`, `shows`, `animes`).

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

1. Copy [buzz.dist.yml](./buzz.dist.yml) to `buzz.yml` and set your Real-Debrid token.
2. Copy `.env.dist` to `.env` and adjust any mount or media-server settings.
3. Perform the **Host Preparation** steps above.
4. Start the stack:

```sh
docker compose up -d
```

5. Verify the WebDAV mount:

```sh
time ls -1R /mnt/buzz
```

If you change `buzz.yml`, restart the service:

```sh
docker compose restart buzz
```

To inspect machine-managed state:

```sh
sqlite3 /path/to/state_dir/buzz.sqlite ".tables"
```

## Configuration Reference

### `buzz.yml`

This file handles the DAV server logic and RD polling.

| Key | Default | Description |
| :--- | :--- | :--- |
| `provider.token` | *(Required)* | Your Real-Debrid API token. |
| `poll_interval_secs` | `10` | How often Buzz polls Real-Debrid for changes. |
| `server.bind` | `0.0.0.0` | IP address the DAV server binds to. |
| `server.port` | `9999` | Port for the DAV server. |
| `server.stream_buffer_size` | `0` | Read-ahead buffer size in bytes for streaming media (e.g., 50MB: `52428800`). Set to `0` to disable. |
| `state_dir` | `/app/data` | Shared path used by both `buzz-dav` and `buzz-curator` for `buzz.sqlite` and related state. |
| `hooks.on_library_change` | `sh /app/scripts/media_update.sh` | Shell command executed when a change in the library is detected. |
| `hooks.curator_url` | `http://buzz-curator:8400/rebuild` | Internal URL to trigger the Curator rebuild. |
| `hooks.rd_update_delay_secs` | `15` | Delay before triggering a hook on RD updates (allows inventory to settle). |
| `compat.enable_all_dir` | `true` | Exposes an `__all__` directory via WebDAV containing all playable files. |
| `compat.enable_unplayable_dir` | `true` | Exposes an `__unplayable__` directory for files that aren't video files. |
| `directories.anime.patterns` | *(Default regex list)* | List of regex patterns used to categorize files as Anime. |
| `request_timeout_secs` | `30` | Timeout in seconds for API requests to Real-Debrid. |
| `logging.verbose` | `false` | Enable verbose request and debug logging. |

These settings control the automatic subtitle fetcher.

| Key | Default | Description |
| :--- | :--- | :--- |
| `enabled` | `false` | Whether to enable automatic subtitle fetching from OpenSubtitles. |
| `opensubtitles.api_key` | *(Required)* | Your OpenSubtitles.com API Key (needed for search and download). |
| `opensubtitles.username` | *(Required)* | Your OpenSubtitles.com username (needed for download authentication). |
| `opensubtitles.password` | *(Required)* | Your OpenSubtitles.com password (needed for download authentication). |
| `languages` | `[en]` | List of language codes to download (e.g., `[en, pt-br]`). Supports regional codes like `pt-br` and `pt-pt`. |
| `strategy` | `most-downloaded` | Ranking strategy: `best-match`, `most-downloaded`, `best-rated`, `trusted`, `latest`. |
| `filters.hearing_impaired` | `exclude` | Handling of HI tracks: `exclude`, `include`, `prefer`. |
| `filters.exclude_ai` | `true` | Exclude AI-translated subtitles. |
| `filters.exclude_machine` | `true` | Exclude machine-translated subtitles. |
| `search_delay_secs` | `0.5` | Delay between API search calls to respect rate limits. |
| `download_delay_secs` | `1.0` | Delay between download calls. |

Complete example:

```yaml
provider:
  token: "YOUR_RD_TOKEN"

poll_interval_secs: 10

server:
  bind: "0.0.0.0"
  port: 9999
  stream_buffer_size: 52428800

state_dir: "/app/data"

hooks:
  on_library_change: "sh /app/scripts/media_update.sh"
  curator_url: "http://buzz-curator:8400/rebuild"
  rd_update_delay_secs: 15

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

### `.env`

This file handles the stack deployment and media-server integration.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MEDIA_SERVER` | `jellyfin` | Controls which media server services are started and which update script Buzz uses (matches `COMPOSE_PROFILES`). |
| `LIBRARY_MOUNT` | `/mnt/buzz` | Path where the library is mounted. |
| `PUID` / `PGID` | `1000` | User and Group ID used for creating host-owned files and for running services. |
| `PLEX_URL` | *(Empty)* | URL to the Plex server (e.g., `http://127.0.0.1:32400`). |
| `PLEX_TOKEN` | *(Empty)* | Plex Access Token for library update API calls. |
| `JELLYFIN_URL` | `http://jellyfin:8096` | URL to the Jellyfin server (must be reachable from the Buzz container). |
| `JELLYFIN_API_KEY` | *(Empty)* | Jellyfin API Key used to trigger library scans. |
| `JELLYFIN_SCAN_TASK_ID` | *(Empty)* | Optional. Used if automatic task discovery fails. |
| `SUBTITLE_ENABLED` | `false` | Enable or disable subtitle fetching. |
| `OPENSUBTITLES_API_KEY` | *(Empty)* | API Key for OpenSubtitles. |
| `OPENSUBTITLES_USERNAME` | *(Empty)* | Username for OpenSubtitles. |
| `OPENSUBTITLES_PASSWORD` | *(Empty)* | Password for OpenSubtitles. |
| `SUBTITLE_LANGUAGES` | `en` | Comma-separated list of language codes. |
| `SUBTITLE_STRATEGY` | `most-downloaded` | Subtitle ranking strategy. |
| `SUBTITLE_ROOT` | `/mnt/buzz/subs` | Path where subtitles are stored. |

## Architecture

For a deep dive into how Buzz works, components, and data flow, see the [Architecture Documentation](./docs/architecture.md).

## Development

- DAV service code lives in [buzz/dav_app.py](./buzz/dav_app.py).
- Curator service code lives in [buzz/curator_app.py](./buzz/curator_app.py).
- The container image is built from [buzz/Dockerfile](./buzz/Dockerfile).
- **Local Development:** Use the development override to mount your local code directly into the containers (`- ./:/app`):
  ```sh
  docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
  ```
  Source changes take effect immediately after a service restart (`docker compose restart buzz-dav`) without rebuilding the image.
- **Isolated Development VM:** You can also deploy an isolated development environment using [Incus](./docs/incus-dev-vm.md).
- **Production (Default):** Running `docker compose up -d` uses the stable, immutable code baked into the container image. To rebuild the production image after code changes, use `docker compose up -d --build`.
- **Tests:** Run tests locally with:
  ```sh
  uv run python -m unittest discover -s tests
  ```
- **Linting:** We use `htmlhint` to enforce clean HTML templates and forbid inline styles. A `.htmlhintrc` is provided in the root directory.
  ```sh
  # Run linting on all templates
  npx htmlhint "buzz/templates/*.html"
  ```
- **Config Migration:** Config migration helper lives in [scripts/migrate_config.py](./scripts/migrate_config.py).
