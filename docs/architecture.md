# Architecture

This document describes the internal architecture of Buzz, its components, and the data flow between them.

## High-Level Overview

Buzz is designed to provide a stable, curated media library from a Real-Debrid account. It consists of two main services that work together to expose a virtual filesystem to your media server (Plex or Jellyfin).

### Core Components

1.  **DAV Server (`buzz-dav`)**:
    - The entry point for the stack.
    - Responsible for polling the Real-Debrid API for changes in the torrent list.
    - Maintains a local SQLite database and generates "snapshots" of the virtual library.
    - Serves the virtual library over a read-only WebDAV interface at `/dav`.
    - Provides a lightweight Web UI for management (adding/deleting torrents, triggering syncs).

2.  **Curator Service (`buzz-curator`)**:
    - A sidecar service that watches the "raw" WebDAV mount (via `rclone`).
    - It parses the raw torrent folders and organizes them into a clean, curated structure (e.g., `/mnt/buzz/curated/movies`, `/mnt/buzz/curated/shows`).
    - It handles naming convention logic and ensures that your media server can easily identify content.
    - It triggers library updates on the media server after a successful curation pass.

3.  **Rclone**:
    - Acts as the bridge between the DAV server and the filesystem.
    - It mounts the `buzz-dav` WebDAV share as a local FUSE mount at `/mnt/buzz/raw`.

---

## Service Interaction Diagram

This diagram shows how the Docker services interact and how data flows from Real-Debrid to your media server.

```mermaid
flowchart TD
    RDAPI["Real-Debrid API"] <--> |Polling/Sync| BuzzDAV["buzz-dav"]
    BuzzDAV --> |WebDAV Share| Rclone["rclone"]
    Rclone --> |FUSE Mount| HostRaw["/mnt/buzz/raw"]
    BuzzCurator["buzz-curator"] --> |Reads| HostRaw
    BuzzCurator --> |Creates Symlinks| HostCurated["/mnt/buzz/curated"]
    Plex["Plex"] --> |Reads| HostCurated
    Jellyfin["Jellyfin"] --> |Reads| HostCurated
    
    subgraph "Host OS Filesystem"
    HostRaw
    HostCurated
    end
```

---

## Functional Block: WebDAV and Rclone Interaction

When a media server requests a file, `rclone` translates that into a WebDAV request to `buzz-dav`. `buzz-dav` then fetches the actual stream link from Real-Debrid and proxies the stream.

```mermaid
sequenceDiagram
    participant MediaServer
    participant Rclone
    participant BuzzDAV
    participant RealDebrid
    participant RDCDN as Real-Debrid CDN
    MediaServer->>Rclone: Request Video Stream
    Rclone->>BuzzDAV: HTTP GET /dav/movies/...
    BuzzDAV->>RealDebrid: Request unrestricted stream link
    RealDebrid-->>BuzzDAV: 302 Redirect to CDN
    BuzzDAV->>RDCDN: HTTP GET with Range Headers
    RDCDN-->>BuzzDAV: Video Chunks
    BuzzDAV-->>Rclone: Video Chunks
    Rclone-->>MediaServer: Playback
```

---

## Functional Block: Curator and Media Server Refresh Flow

When the `buzz-dav` service detects a change in the Real-Debrid library, it triggers a post-sync hook. This hook informs the Curator to rebuild the curated library and then notifies the media server.

```mermaid
sequenceDiagram
    participant BuzzDAV
    participant HookScript
    participant Curator
    participant RcloneMount
    participant Jellyfin
    BuzzDAV->>HookScript: Detects change, executes hook
    HookScript->>Curator: POST /rebuild
    Curator->>RcloneMount: Read raw torrent folders
    Curator->>Curator: Parse titles, seasons, episodes
    Curator->>Jellyfin: Create symlinks in /mnt/buzz/curated
    Curator->>Jellyfin: POST /ScheduledTasks/Running/{task_id}
    Jellyfin-->>Curator: 204 No Content
    Curator-->>HookScript: 200 OK
```

---

## Common Workflows

### 1. Adding a Torrent via the UI
1.  The user visits the `buzz-dav` Web UI (default port 9999).
2.  The user enters a magnet link and clicks "Add".
3.  `buzz-dav` sends the magnet link to Real-Debrid via the API.
4.  Real-Debrid starts caching/downloading the torrent.
5.  The background `Poller` in `buzz-dav` detects the new torrent and waits for it to be fully cached.
6.  Once cached, `buzz-dav` generates a new VFS snapshot and triggers the `on_library_change` hook.

### 2. Perceiving Upstream Changes
1.  The `Poller` runs every `poll_interval_secs` (default 10s).
2.  It checks the RD torrent list and compares it with its internal database.
3.  If a change is detected (new torrent, deleted torrent, or status change), it waits for `rd_update_delay_secs` (default 15s) to allow the RD inventory to settle.
4.  A new snapshot is generated, updating the WebDAV view.
5.  The refresh hook is triggered.

### 3. Manual Sync
1.  User clicks "Sync" in the Web UI.
2.  `buzz-dav` immediately polls the RD API, bypassing the interval.
3.  If changes are found, it proceeds with snapshot generation and hook execution immediately.
