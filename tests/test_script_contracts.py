"""Behavioral coverage for maintenance-script record contracts."""

import importlib.util
import sqlite3
import sys
from pathlib import Path
from threading import RLock

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migrate_config = _load_script("migrate_config")
parse_zurg_config = migrate_config.parse_zurg_config
zurg_to_buzz = migrate_config.zurg_to_buzz


class FakeState:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE provider_links (provider, provider_torrent_id, hash, info_json)"
        )
        self.conn.executemany(
            "INSERT INTO provider_links VALUES (?, ?, ?, ?)",
            [
                ("real_debrid", "source-id", "ABC", "{}"),
                ("torbox", "target-id", "abc", '{"filename":"item.mkv"}'),
            ],
        )
        self.cache = {"torbox:target-id": object()}
        self.lock = RLock()
        self.deleted_cache_keys: list[str] = []

    def _cache_key(self, provider: str, torrent_id: str) -> str:
        return f"{provider}:{torrent_id}"

    def _delete_cache_entry(self, cache_key: str) -> None:
        self.deleted_cache_keys.append(cache_key)


class FakeClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_torrent(self, torrent_id: str) -> None:
        self.deleted.append(torrent_id)


def test_zurg_parser_preserves_directory_filter_transitions() -> None:
    """Ensure directory and filter state survives parser transitions."""
    raw = """directories:
  anime:
    filters:
      - regex: /anime/
        any_file_inside_regex: /episode/
  movies:
    filters:
      - regex: /.*/
"""

    parsed = parse_zurg_config(raw)
    assert parsed["directories"]["anime"]["filters"][0] == {
        "regex": "/anime/",
        "any_file_inside_regex": "/episode/",
    }
    assert zurg_to_buzz(parsed)["directories"]["anime"]["patterns"] == [
        "anime",
        "episode",
    ]


def test_duplicate_discovery_and_deletion_preserve_effects() -> None:
    """Ensure duplicate deletion preserves provider and cache effects."""
    module = _load_script("remove_duplicates")
    state = FakeState()
    duplicates = module._find_duplicates(state, "real_debrid", "torbox")
    client = FakeClient()

    module._perform_deletions(state, {"torbox": client}, duplicates, "torbox")

    assert client.deleted == ["target-id"]
    assert state.deleted_cache_keys == ["torbox:target-id"]
    assert "torbox:target-id" not in state.cache
    assert state.conn.execute(
        "SELECT COUNT(*) FROM provider_links WHERE provider = 'torbox'"
    ).fetchone()[0] == 0
