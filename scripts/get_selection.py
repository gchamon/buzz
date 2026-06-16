#!/usr/bin/env python3
"""Show the per-file selection for a given torrent hash in every provider.

For each configured provider, finds the torrent matching the hash and prints
each file with its selected/unselected state, so you can compare what each
provider currently has selected.

Usage:
    uv run python scripts/get_selection.py <hash>
"""

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from buzz.models import DavConfig
from buzz.providers import RealDebridProviderClient, TorBoxProviderClient


def section(title: str) -> None:
    """Print a section header."""
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def _format_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def _rd_id_for_hash(token: str, target_hash: str) -> str | None:
    """Find the RD torrent id whose hash matches, via the raw paginated list."""
    headers = {"Authorization": f"Bearer {token}"}
    page_size = 100
    offset: int | None = None
    with httpx.Client(timeout=30) as c:
        while True:
            params: dict = {"limit": page_size}
            if offset is not None:
                params["offset"] = offset
            page = c.get(
                "https://api.real-debrid.com/rest/1.0/torrents",
                headers=headers,
                params=params,
            ).json()
            if not isinstance(page, list):
                print(f"Unexpected RD response: {page}")
                return None
            for item in page:
                if str(item.get("hash") or "").lower() == target_hash:
                    return str(item.get("id") or "")
            if len(page) < page_size:
                return None
            offset = (offset or 0) + page_size


def _torbox_id_for_hash(token: str, target_hash: str) -> str | None:
    """Find the TorBox torrent id whose hash matches, via the raw list."""
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "buzz"}
    with httpx.Client(timeout=30) as c:
        resp = c.get(
            "https://api.torbox.app/v1/api/torrents/mylist",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    items = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(items, list):
        return None
    for item in items:
        if str(item.get("hash") or "").lower() == target_hash:
            return str(item.get("torrent_id") or item.get("id") or "")
    return None


def _print_selection(client: Any, torrent_id: str) -> None:
    """Fetch detail via the provider client and print per-file selection."""
    detail = client.get_torrent(torrent_id)
    selected = [f for f in detail.files if f.selected]
    print(f"id: {detail.id!r}")
    print(f"name: {detail.name!r}")
    print(f"hash: {detail.hash}")
    print(f"status: {detail.status}  progress: {detail.progress}")
    print(f"files: {len(detail.files)} total, {len(selected)} selected")
    print()
    for f in sorted(detail.files, key=lambda x: x.path):
        mark = "[x]" if f.selected else "[ ]"
        ref = f"  ref={f.stream_ref!r}" if f.stream_ref else ""
        print(f"  {mark} {f.path}  ({_format_bytes(f.bytes)}){ref}")


def _check_real_debrid(target_hash: str, config: DavConfig) -> None:
    """Show RD's file selection for the hash."""
    if not config.real_debrid_enabled:
        print("Real-Debrid is disabled in config, skipping.")
        return
    token = config.real_debrid_token or config.token
    if not token:
        print("No Real-Debrid token in config, skipping.")
        return
    try:
        torrent_id = _rd_id_for_hash(token, target_hash)
        if not torrent_id:
            print("Hash NOT found in Real-Debrid.")
            return
        _print_selection(RealDebridProviderClient(token), torrent_id)
    except Exception:
        traceback.print_exc()


def _check_torbox(target_hash: str, config: DavConfig) -> None:
    """Show TorBox's file selection for the hash."""
    if not config.torbox_enabled:
        print("TorBox is disabled in config, skipping.")
        return
    token = config.torbox_token
    if not token:
        print("No TorBox token in config, skipping.")
        return
    try:
        torrent_id = _torbox_id_for_hash(token, target_hash)
        if not torrent_id:
            print("Hash NOT found in TorBox.")
            return
        _print_selection(TorBoxProviderClient(token), torrent_id)
    except Exception:
        traceback.print_exc()


def main() -> None:
    """Show file selection for a hash across all configured providers."""
    parser = argparse.ArgumentParser(
        description="Show the per-file selection for a torrent hash in every provider."
    )
    parser.add_argument("hash", help="Torrent hash to look up")
    args = parser.parse_args()

    target_hash = args.hash.strip().lower()
    if not target_hash:
        print("ERROR: empty hash", file=sys.stderr)
        sys.exit(1)

    try:
        config = DavConfig.load()
    except Exception as e:
        print(f"ERROR: Failed to load configuration: {e}", file=sys.stderr)
        sys.exit(1)

    section("REAL-DEBRID")
    _check_real_debrid(target_hash, config)

    section("TORBOX")
    _check_torbox(target_hash, config)


if __name__ == "__main__":
    main()
