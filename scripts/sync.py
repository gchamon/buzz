#!/usr/bin/env python3
import argparse
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
logger = logging.getLogger("sync")

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

def main():
    """Sync all configured providers to the local database."""
    parser = argparse.ArgumentParser(description="Sync providers to the local database.")
    parser.add_argument(
        "--resync",
        action="store_true",
        help="Force re-fetch of all torrent details, ignoring local cache",
    )
    args = parser.parse_args()

    try:
        config = DavConfig.load()
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    clients = build_clients(config)
    if not clients:
        logger.error("No providers are configured. Check your buzz.yml.")
        sys.exit(1)

    state = BuzzState(config, clients)

    logger.info("Starting library sync...")
    try:
        report = state.sync(resync=args.resync)
        logger.info("Sync complete.")

        # Display a brief summary
        added = len(report.get("added_paths", []))
        removed = len(report.get("removed_paths", []))
        if added or removed:
            logger.info(f"Summary: {added} path(s) added, {removed} path(s) removed.")
        else:
            logger.info("No library changes detected.")

    except Exception:
        logger.exception("Sync failed")
        sys.exit(1)

if __name__ == "__main__":
    main()
