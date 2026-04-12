# zurg

A self-hosted Real-Debrid webdav server written from scratch. Together with [rclone](https://rclone.org/) it can mount your Real-Debrid torrent library into your file system like Dropbox. It's meant to be used with Infuse (webdav server) and a media server such as Plex or Jellyfin.

## Changes from upstream

- `config.yml` is now `config.dist.yml`. This is to avoid potentially commiting changes to `config.yml` and exposing your token
- `rclone` mounts the volume with `1000` gid and uid, so that Plex or Jellyfin can read it
- `media-server` is now a selectable service that integrates with the other services by waiting for rclone to finish before mounting. This way it avoids a race condition to the mount endpoint where if the media server is started before rclone finishes, the mountpoint will fail with `Transport endpoint missing`.
- `healthcheck` is a service that continuously try to access `/mnt/zurg/movies`. If it identifies that it can't access the mountpoint that means `rclone` failed for some reason and it restart both the selected media server and itself, so they can pick up the mount point again.

## Download

[Release Cycle](https://github.com/debridmediamanager/zurg-testing/wiki/Release-cycle)

### Latest version: v0.10.0-rc.4-1 (Sponsors only)

[Download the binary](https://github.com/debridmediamanager/zurg/releases) or use docker

Instructions on [HOW TO PULL THE PRIVATE DOCKER IMAGE](https://www.patreon.com/posts/guide-to-pulling-105779285)

Also the [CONFIG guide for v0.10](https://github.com/debridmediamanager/zurg-testing/wiki/Config-v0.10)

```sh
docker pull ghcr.io/debridmediamanager/zurg:latest
# or
docker pull ghcr.io/debridmediamanager/zurg:v0.10.0-rc.4-1
```

### Stable version: v0.9.3-final (Public)

[Download the binary](https://github.com/debridmediamanager/zurg-testing/releases) or use docker

```sh
docker pull ghcr.io/debridmediamanager/zurg-testing:latest
# or
docker pull ghcr.io/debridmediamanager/zurg-testing:v0.9.3-final
```

## How to run zurg in 6 steps for Plex or Jellyfin with Docker

1. Clone the repo `git clone https://github.com/debridmediamanager/zurg-testing.git` or `git clone https://github.com/debridmediamanager/zurg.git`
2. Add your token in `config.yml`
3. `sudo mkdir -p /mnt/zurg`
4. Copy `.env.dist` to `.env` and set the variables you need. `COMPOSE_PROFILES=plex` and `MEDIA_SERVER=plex` are the defaults; set both to `jellyfin` to switch the stack to Jellyfin.
5. Put media-server state under `./config/plex` or `./config/jellyfin` depending on what you deploy.
6. Run `docker compose up -d`

`time ls -1R /mnt/zurg` confirms the mount is working. If you do edits on your `config.yml` just run `docker compose restart zurg`.

A web server is now running at `localhost:9999`.

### Media server notes

- The Zurg library layout is unchanged for both Plex and Jellyfin: `/mnt/zurg/movies`, `/mnt/zurg/shows`, `/mnt/zurg/anime`
- Plex keeps the existing partial refresh script in [`scripts/plex_update.sh`](./scripts/plex_update.sh)
- Jellyfin uses [`scripts/jellyfin_update.sh`](./scripts/jellyfin_update.sh) to call the internal `presentation-builder` sidecar, which rebuilds a Jellyfin-facing library under `/mnt/jellyfin-library` and then triggers `Scan Media Library`
- `COMPOSE_PROFILES` selects which media-server services Docker starts
- `MEDIA_SERVER` controls which update-hook script Zurg calls and should match `COMPOSE_PROFILES`
- In Jellyfin mode, set `JELLYFIN_URL=http://jellyfin:8096` so the `zurg` container can reach Jellyfin over the Compose network
- In Jellyfin mode, you can still open the UI from the host at `http://localhost:8096`
- Point Jellyfin libraries at `/mnt/jellyfin-library/movies`, `/mnt/jellyfin-library/shows`, and `/mnt/jellyfin-library/animes`
- Manual naming overrides live in [`presentation/overrides.yml`](./presentation/overrides.yml) and currently use JSON-compatible YAML
- Generator state and reports are written under `./state/jellyfin-library/`
- `presentation-builder` only exists in the `jellyfin` profile and performs an initial library build when it starts, before serving rebuild requests on port `8400`
- `.env.dist` documents the supported Compose and update-hook variables

### Note: when using zurg in a server outside of your home network, ensure that "Use my Remote Traffic automatically when needed" is unchecked on your [Account page](https://real-debrid.com/account)

## Command-line utility

```
Usage:
  zurg [flags]
  zurg [command]

Available Commands:
  clear-downloads Clear all downloads (unrestricted links) in your account
  clear-torrents  Clear all torrents in your account
  completion      Generate the autocompletion script for the specified shell
  help            Help about any command
  network-test    Run a network test
  version         Prints zurg's current version

Flags:
  -c, --config string   config file path (default "./config.yml")
  -h, --help            help for zurg

Use "zurg [command] --help" for more information about a command.
```

## Why zurg? Why not X?

- Better performance than anything out there; changes in your library appear instantly ([assuming your media server picks it up fast enough](./scripts/plex_update.sh))
- You can configure a flexible directory structure in `config.yml`; you can select individual torrents that should appear on a directory by the ID you see in [DMM](https://debridmediamanager.com/). [Need help?](https://github.com/debridmediamanager/zurg-testing/wiki/Config)
- If you've ever experienced Plex scanner being stuck on a file and thereby freezing Plex completely, it should not happen anymore because zurg does a comprehensive check if a torrent is dead or not. You can run `ps aux --sort=-time | grep "Plex Media Scanner"` to check for stuck scanner processes.
- zurg guarantees that your library is **always available** because of its repair abilities!

## Guides

- [@I-am-PUID-0](https://github.com/I-am-PUID-0) - [pd_zurg](https://github.com/I-am-PUID-0/pd_zurg)
- [@Pukabyte](https://github.com/Pukabyte) - [Guide: Zurg + RDT + Prowlarr + Arrs + Petio + Autoscan + Plex + Scannarr](https://puksthepirate.notion.site/Guide-Zurg-RDT-Prowlarr-Arrs-Petio-Autoscan-Plex-Scannarr-eebe27d130fa400c8a0536cab9d46eb3)
- [u/pg988](https://www.reddit.com/user/pg988/) - [Windows + zurg + Plex guide](https://www.reddit.com/r/RealDebrid/comments/18so926/windows_zurg_plex_guide/)
- [@ignamiranda](https://github.com/ignamiranda) - [Plex Debrid Zurg Windows Guide](https://github.com/ignamiranda/plex_debrid_zurg_scripts/)
- [@funkypenguin](https://github.com/funkypenguin) - ["Infinite streaming" from Real Debrid with Plex](https://elfhosted.com/guides/media/stream-from-real-debrid-with-plex/) (ElfHosted)
- [u/TimeyWimeyInsaan](https://www.reddit.com/user/TimeyWimeyInsaan/) - [A Newbie guide for Plex+Real-Debrid using Zurg & Rclone](https://docs.google.com/document/d/114URAz5h5jarpo1xz4GyFUzRzoBnOKVQPxH0-2R5KC8/view)

## Service Providers

- [ElfHosted](https://elfhosted.com) - Easy, [open source](https://elfhosted.com/open/), Kubernetes / GitOps driven hosting of popular self-hosted apps - tested, tightly integrated, and secured. Apps start at $0.05/day, and new accounts get $10 credit, no commitment.

## Please read our [wiki](https://github.com/debridmediamanager/zurg-testing/wiki) for more information

## [zurg's version history](https://github.com/debridmediamanager/zurg-testing/wiki/History)
