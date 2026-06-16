#!/usr/bin/env python3
"""Check provider API responses for a specific hash to diagnose name resolution."""

import argparse
import sys
import traceback
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from buzz.models import DavConfig


def section(title: str) -> None:
    """Print a section header."""
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def _fetch_rd_torrents(headers: dict) -> list:
    """Fetch all RD torrents via paginated API calls."""
    page_size = 100
    offset: int | None = None
    torrents: list = []
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
                return page  # type: ignore[return-value]
            torrents.extend(page)
            if len(page) < page_size:
                break
            offset = (offset or 0) + page_size
    return torrents


def _print_rd_match(match: dict, headers: dict) -> None:
    """Print full details for a matched RD torrent."""
    print(f"\nFound in list — id: {match.get('id')}")
    for k, v in match.items():
        if k != "links":
            print(f"  {k}: {v!r}")
    with httpx.Client(timeout=30) as c:
        detail = c.get(
            f"https://api.real-debrid.com/rest/1.0/torrents/info/{match['id']}",
            headers=headers,
        ).json()
    print("\nDetail:")
    for k, v in detail.items():
        if k not in ("links", "files"):
            print(f"  {k}: {v!r}")
    files = detail.get("files") or []
    print(f"  files ({len(files)} total):")
    for f in files[:5]:
        print(f"    {f}")
    if len(files) > 5:
        print(f"    ... and {len(files) - 5} more")


def _check_real_debrid(target_hash: str, config: DavConfig, verbose: bool) -> None:
    """Query Real-Debrid for the given hash and print results."""
    if not config.real_debrid_enabled:
        print("Real-Debrid is disabled in config, skipping.")
        return
    rd_token = config.real_debrid_token or config.token
    if not rd_token:
        print("No Real-Debrid token in config, skipping.")
        return
    try:
        headers = {"Authorization": f"Bearer {rd_token}"}
        torrents = _fetch_rd_torrents(headers)
        if not isinstance(torrents, list):
            print(f"Unexpected response: {torrents}")
            return
        print(f"Total torrents in RD: {len(torrents)}")
        match = next(
            (t for t in torrents if str(t.get("hash") or "").lower() == target_hash),
            None,
        )
        if match:
            _print_rd_match(match, headers)
        else:
            print("Hash NOT found in RD.")
            if verbose:
                print("hashes present:")
                for t in torrents:
                    print(f"  {t.get('hash')}  {t.get('filename')!r}")
    except Exception:
        traceback.print_exc()


def _check_torbox(target_hash: str, config: DavConfig, verbose: bool) -> None:
    """Query TorBox for the given hash and print results."""
    if not config.torbox_enabled:
        print("TorBox is disabled in config, skipping.")
        return
    tb_token = config.torbox_token
    if not tb_token:
        print("No TorBox token in config, skipping.")
        return
    try:
        headers = {"Authorization": f"Bearer {tb_token}", "User-Agent": "buzz"}
        with httpx.Client(timeout=30) as c:
            resp = c.get(
                "https://api.torbox.app/v1/api/torrents/mylist",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        items = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(items, list):
            items = []
        print(f"Total torrents in TorBox: {len(items)}")

        match = next(
            (t for t in items if str(t.get("hash") or "").lower() == target_hash),
            None,
        )
        if match:
            tid = str(match.get("torrent_id") or match.get("id") or "")
            print(f"\nFound — id: {tid}")
            print("\nAll fields (except files):")
            for k, v in match.items():
                if k != "files":
                    print(f"  {k}: {v!r}")
            files = match.get("files") or []
            print(f"\nfiles ({len(files)} total):")
            for f in files[:5]:
                print(f"  {f.get('name') or f.get('path') or f.get('short_name')!r}")
            if len(files) > 5:
                print(f"  ... and {len(files) - 5} more")
        else:
            print("Hash NOT found in TorBox.")
            if verbose:
                print("Sample hashes present:")
                for t in items[:5]:
                    print(f"  {t.get('hash')}  name={t.get('name')!r}")
    except Exception:
        traceback.print_exc()


def main() -> None:
    """Look up a torrent hash in all configured providers."""
    parser = argparse.ArgumentParser(
        description="Look up a torrent hash in all configured providers."
    )
    parser.add_argument("hash", help="Torrent hash to look up")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all hashes when hash is not found")
    args = parser.parse_args()

    target_hash = args.hash.strip().lower()

    try:
        config = DavConfig.load()
    except Exception as e:
        print(f"ERROR: Failed to load configuration: {e}", file=sys.stderr)
        sys.exit(1)

    section("REAL-DEBRID")
    _check_real_debrid(target_hash, config, args.verbose)

    section("TORBOX")
    _check_torbox(target_hash, config, args.verbose)


if __name__ == "__main__":
    main()
