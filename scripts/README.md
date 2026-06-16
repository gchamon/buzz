# Buzz Scripts

This directory contains various utility scripts for maintaining and managing the Buzz media ecosystem.

## Provider Management

### `sync.py`
Synchronizes the local SQLite database with all configured upstream providers. This is recommended before running other management scripts to ensure you are working with the latest state.

**Usage:**
```bash
uv run python scripts/sync.py
```

### `check_hash.py`
Queries all configured providers for a specific torrent hash and prints full details, useful for diagnosing name-resolution issues.

**Usage:**
```bash
uv run python scripts/check_hash.py <hash>
```

### `remove_duplicates.py`
Cleans up duplicate torrents across different providers (e.g., Real-Debrid and TorBox).

- **Default Behavior**: Performs a dry-run, listing torrents that exist in both the source and target providers based on the local database.
- **Source/Target**: `--source` and `--target` are required, with no defaults — you must always specify which provider to keep and which to remove duplicates from.
- **Commit**: Use `--commit` to actually perform deletions. It will still ask for manual confirmation before proceeding.

**Usage:**
```bash
# Dry-run: keep real_debrid, list duplicates in torbox
uv run python scripts/remove_duplicates.py --source real_debrid --target torbox

# Commit deletions (run sync.py first if the local DB is stale)
uv run python scripts/sync.py
uv run python scripts/remove_duplicates.py --source real_debrid --target torbox --commit
```

## Configuration & Setup

### `migrate_config.py`
Converts configuration files between the legacy Zurg format and the modern Buzz format.

### `generate_self_signed_cert.py`
Generates self-signed TLS certificates for secure communication between Buzz components.

## Media & Server Integration

### `media_update.sh`
A shell script typically triggered by hooks to notify media servers (like Jellyfin) of library changes.

### `plex_update.sh`
Specialized script for triggering Plex library updates.
