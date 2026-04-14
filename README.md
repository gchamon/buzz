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

## Config

`buzz.yml` is YAML and currently supports:

- `provider.token`
- `poll_interval_secs`
- `hooks.on_library_change`
- `logging.verbose`
- `compat.enable_all_dir`
- `compat.enable_unplayable_dir`
- `directories.anime.patterns`

The default hook is:

```json
{
  "hooks": {
    "on_library_change": "sh /app/media_update.sh"
  }
}
```

That script dispatches to Plex or Jellyfin based on `MEDIA_SERVER`.

## Media Server Notes

- `COMPOSE_PROFILES` controls which media-server services Docker starts.
- `MEDIA_SERVER` controls which update script Buzz invokes and should match `COMPOSE_PROFILES`.
- Plex refreshes changed library roots via [`scripts/plex_update.sh`](./scripts/plex_update.sh).
- Jellyfin uses [`scripts/jellyfin_update.sh`](./scripts/jellyfin_update.sh) to trigger the existing `presentation-builder` sidecar.
- In Jellyfin mode, set `JELLYFIN_URL=http://jellyfin:8096` so the Buzz container can reach Jellyfin over the Compose network.
- Point Jellyfin libraries at `/mnt/buzz/curated/movies`, `/mnt/buzz/curated/shows`, and `/mnt/buzz/curated/animes`.
- `logging.verbose: true` re-enables Buzz request/access logs and client-disconnect diagnostics when you need to debug stream behavior.
- `HEALTHCHECK_VERBOSE=true` re-enables the sidecar “mountpoint seems to be working” message; the default is quiet success logs.

## Development

- DAV service code lives in [buzz/dav_app.py](./buzz/dav_app.py).
- Curator service code lives in [buzz/curator_app.py](./buzz/curator_app.py).
- The container image is built from [buzz/Dockerfile](./buzz/Dockerfile).
- The `buzz/` package is bind-mounted into the Buzz container from the repo, so source changes take effect after `docker compose up -d --force-recreate buzz` without an image rebuild.
- Persisted RD cache and committed library snapshots live under the `buzzdata` volume at `/app/data` in the Buzz container.
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
