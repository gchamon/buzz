# buzz

`buzz` is a small Real-Debrid WebDAV service for this stack. It polls the RD torrent list, materializes a stable virtual library, serves it at `/dav`, and lets `rclone` mount that library under `/mnt/buzz` for Plex or Jellyfin.

This replaces Zurg in this repo. The goal is a smaller service we control, with explicit snapshot persistence and post-sync hooks that trigger media-server refresh behavior only when the exposed library actually changes.

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
sudo mkdir -p /mnt/buzz/raw /mnt/buzz/curated
mkdir -p data state/curator cache/jellyfin config/plex config/jellyfin

# 2. Set ownership to the container user (1000:1000)
sudo chown -R 1000:1000 /mnt/buzz data state/curator cache/jellyfin config/plex config/jellyfin
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

## Configuration Reference

### `buzz.yml`

This file handles the DAV server logic and RD polling.

| Key | Default | Description |
| :--- | :--- | :--- |
| `provider.token` | *(Required)* | Your Real-Debrid API token. |
| `poll_interval_secs` | `10` | How often Buzz polls Real-Debrid for changes. |
| `server.bind` | `0.0.0.0` | IP address the DAV server binds to. |
| `server.port` | `9999` | Port for the DAV server. |
| `state_dir` | `/app/data` | Path to store the SQLite DB and snapshots inside the container. |
| `hooks.on_library_change` | `sh /app/scripts/media_update.sh` | Shell command executed when a change in the library is detected. |
| `hooks.curator_url` | `http://buzz-curator:8400/rebuild` | Internal URL to trigger the Curator rebuild. |
| `hooks.rd_update_delay_secs` | `15` | Delay before triggering a hook on RD updates (allows inventory to settle). |
| `compat.enable_all_dir` | `true` | Exposes an `__all__` directory via WebDAV containing all playable files. |
| `compat.enable_unplayable_dir` | `true` | Exposes an `__unplayable__` directory for files that aren't video files. |
| `directories.anime.patterns` | *(Default regex list)* | List of regex patterns used to categorize files as Anime. |
| `request_timeout_secs` | `30` | Timeout in seconds for API requests to Real-Debrid. |
| `logging.verbose` | `false` | Enable verbose request and debug logging. |

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

## Architecture

For a deep dive into how Buzz works, components, and data flow, see the [Architecture Documentation](./docs/architecture.md).

## Development

- DAV service code lives in [buzz/dav_app.py](./buzz/dav_app.py).
- Curator service code lives in [buzz/curator_app.py](./buzz/curator_app.py).
- The container image is built from [buzz/Dockerfile](./buzz/Dockerfile).
- **Local Development:** The `docker-compose.override.yml` file is automatically used by `docker compose up -d`. It mounts your local code directly into the containers (`- ./:/app`). Source changes take effect immediately after a service restart (`docker compose restart buzz-dav`) without rebuilding the image.
- **Production Testing:** To test the immutable production image (bypassing the local mounts), run: `docker compose -f docker-compose.yml up -d --build`.
- Tests live in [tests/test_buzz.py](./tests/test_buzz.py).
- Config migration helper lives in [scripts/migrate_config.py](./scripts/migrate_config.py).

Convert an old Zurg config into Buzz format with:

```sh
python3 scripts/migrate_config.py --from zurg --to buzz config.yml -o buzz.yml
```

Convert a Buzz config back into a best-effort Zurg-style config with:

```sh
python3 scripts/migrate_config.py --from buzz --to zurg buzz.yml -o config.yml
```

Run tests locally with:

```sh
uv run python -m unittest tests.test_buzz tests.test_curator_app
```
