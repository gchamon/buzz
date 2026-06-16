#!/usr/bin/env python3
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Add the project root to sys.path to allow importing buzz
sys.path.insert(0, str(Path(__file__).parent.parent))

from buzz.core.state import BuzzState
from buzz.models import DavConfig
from buzz.providers import RealDebridProviderClient, TorBoxProviderClient

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("cleanup_duplicates")


def build_clients(config: DavConfig) -> dict[str, Any]:
    """Build configured provider clients."""
    clients = {}
    if config.real_debrid_enabled:
        token = config.real_debrid_token or config.token
        if token:
            clients["real_debrid"] = RealDebridProviderClient(token)
    if config.torbox_enabled and config.torbox_token:
        clients["torbox"] = TorBoxProviderClient(config.torbox_token)
    return clients


def _find_duplicates(
    state: BuzzState,
    source: str,
    target: str,
) -> list[tuple[str, str, str, str]]:
    """Return (cache_key, tid, hash, name) for target torrents that duplicate source hashes."""
    rows = state.conn.execute(
        "SELECT provider, provider_torrent_id, hash, info_json FROM provider_links"
    ).fetchall()

    source_hashes: dict[str, str] = {}
    target_items: list[tuple[str, str, str]] = []

    for row in rows:
        provider = row["provider"]
        tid = row["provider_torrent_id"]
        h = str(row["hash"] or "").strip().lower()
        if not h:
            continue
        if provider == source:
            source_hashes[h] = tid
        elif provider == target:
            info = json.loads(row["info_json"] or "{}")
            name = info.get("filename") or info.get("name") or tid
            target_items.append((tid, h, name))

    to_delete: list[tuple[str, str, str, str]] = []
    for tid, h, name in target_items:
        if h in source_hashes:
            cache_key = state._cache_key(target, tid)
            to_delete.append((cache_key, tid, h, name))
    return to_delete


def _perform_deletions(
    state: BuzzState,
    clients: dict[str, Any],
    to_delete: list[tuple[str, str, str, str]],
    target: str,
) -> None:
    """Delete duplicate torrents from the target provider and local DB."""
    target_client = clients[target]
    total = len(to_delete)
    deleted_count = 0
    for i, (cache_key, tid, _h, _name) in enumerate(to_delete, 1):
        try:
            target_client.delete_torrent(tid)
            state.conn.execute(
                "DELETE FROM provider_links WHERE provider = ? AND provider_torrent_id = ?",
                (target, tid),
            )
            state._delete_cache_entry(cache_key)
            with state.lock:
                if cache_key in state.cache:
                    del state.cache[cache_key]
            deleted_count += 1
            logger.info(f"Successfully deleted {tid} ({i}/{total})")
        except Exception as e:
            logger.error(f"Failed to delete {tid}: {e}")
    logger.info(f"Cleanup complete. Deleted {deleted_count} torrent(s).")


def main():
    """Identify and optionally remove duplicate torrents across providers."""
    parser = argparse.ArgumentParser(
        description="Cleanup duplicate torrents between providers using local database."
    )
    parser.add_argument(
        "--source", required=True, help="Provider to keep (e.g. real_debrid)"
    )
    parser.add_argument(
        "--target", required=True, help="Provider to remove duplicates from (e.g. torbox)"
    )
    parser.add_argument("--commit", action="store_true", help="Actually perform deletions (dry-run by default)")
    args = parser.parse_args()

    try:
        config = DavConfig.load()
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    clients = build_clients(config)

    if args.source not in clients:
        logger.error(f"Source provider '{args.source}' is not configured or available.")
        sys.exit(1)
    if args.target not in clients:
        logger.error(f"Target provider '{args.target}' is not configured or available.")
        sys.exit(1)

    state = BuzzState(config, clients)

    logger.info("Reading local database...")
    logger.info("NOTE: If the duplicates found are incorrect, run 'uv run python scripts/sync.py' first.")

    to_delete = _find_duplicates(state, args.source, args.target)
    source_count = state.conn.execute(
        "SELECT COUNT(*) FROM provider_links WHERE provider = ?", (args.source,)
    ).fetchone()[0]
    logger.info(f"Found {source_count} torrent(s) in source provider '{args.source}'.")

    if not to_delete:
        logger.info(f"No duplicate torrents found in target provider '{args.target}' within the local database.")
        return

    logger.info(f"Found {len(to_delete)} duplicate torrent(s) in '{args.target}' that exist in '{args.source}'.")

    if not args.commit:
        logger.info("DRY-RUN: Use --commit to perform deletions.")
        for _cache_key, tid, h, name in to_delete:
            logger.info(f"  - Would delete: {name} (id={tid}, hash={h})")
        return

    confirm = input(f"Are you sure you want to delete {len(to_delete)} torrents from '{args.target}'? [y/N]: ")
    if confirm.lower() not in ("y", "yes"):
        logger.info("Aborted by user.")
        return

    _perform_deletions(state, clients, to_delete, args.target)


if __name__ == "__main__":
    main()
