#!/usr/bin/env python3
"""Resolve a torrent hash from its display name across configured providers.

The inverse of ``check_hash.py``: given a torrent entry name (as shown in the
UI / library, e.g. the provider ``filename``), look it up in each provider and
print its hash.

Usage:
    uv run python scripts/get_hash.py 'Adventure Time (2010) Season 1-10 ...'
"""

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


def _match_by_name(items: list, name_fields: tuple[str, ...], target: str) -> list:
    """Return items whose name matches target (exact, then ci, then substring)."""

    def name_of(item: dict) -> str:
        for field in name_fields:
            value = str(item.get(field) or "").strip()
            if value:
                return value
        return ""

    target_norm = target.strip().lower()
    exact = [t for t in items if name_of(t) == target.strip()]
    if exact:
        return exact
    ci = [t for t in items if name_of(t).lower() == target_norm]
    if ci:
        return ci
    return [t for t in items if target_norm in name_of(t).lower()]


def _check_real_debrid(target: str, config: DavConfig, verbose: bool) -> None:
    """Query Real-Debrid for the given name and print matching hashes."""
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
        matches = _match_by_name(torrents, ("filename",), target)
        if matches:
            for m in matches:
                print(f"  {m.get('hash')}  id={m.get('id')!r}  {m.get('filename')!r}")
        else:
            print("Name NOT found in RD.")
            if verbose:
                print("names present:")
                for t in torrents:
                    print(f"  {t.get('hash')}  {t.get('filename')!r}")
    except Exception:
        traceback.print_exc()


def _check_torbox(target: str, config: DavConfig, verbose: bool) -> None:
    """Query TorBox for the given name and print matching hashes."""
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

        matches = _match_by_name(items, ("name", "filename"), target)
        if matches:
            for m in matches:
                tid = str(m.get("torrent_id") or m.get("id") or "")
                name = m.get("name") or m.get("filename")
                print(f"  {m.get('hash')}  id={tid!r}  {name!r}")
        else:
            print("Name NOT found in TorBox.")
            if verbose:
                print("names present:")
                for t in items:
                    print(f"  {t.get('hash')}  {t.get('name')!r}")
    except Exception:
        traceback.print_exc()


def main() -> None:
    """Look up a torrent hash by name in all configured providers."""
    parser = argparse.ArgumentParser(
        description="Look up a torrent hash by its display name in all configured providers."
    )
    parser.add_argument("name", help="Torrent entry name to look up")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all names when the name is not found",
    )
    args = parser.parse_args()

    target = args.name.strip()
    if not target:
        print("ERROR: empty name", file=sys.stderr)
        sys.exit(1)

    try:
        config = DavConfig.load()
    except Exception as e:
        print(f"ERROR: Failed to load configuration: {e}", file=sys.stderr)
        sys.exit(1)

    section("REAL-DEBRID")
    _check_real_debrid(target, config, args.verbose)

    section("TORBOX")
    _check_torbox(target, config, args.verbose)


if __name__ == "__main__":
    main()
