import json
import tempfile
import unittest
from pathlib import Path

from buzz.app import BuzzState, Config, LibraryBuilder, normalize_posix_path
from scripts.migrate_config import buzz_to_zurg, convert, parse_zurg_config, zurg_to_buzz


class LibraryBuilderTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(
            token="token",
            poll_interval_secs=10,
            bind="127.0.0.1",
            port=9999,
            state_dir="/tmp/buzz-tests",
            hook_command="",
            anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
            enable_all_dir=True,
            enable_unplayable_dir=True,
            request_timeout_secs=30,
            user_agent="buzz-tests",
            version_label="buzz/test",
        )
        self.builder = LibraryBuilder(self.config)

    def test_movie_torrent_exposed_under_movies_and_all(self):
        snapshot, changed = self.builder.build(
            [
                {
                    "id": "ABC123",
                    "status": "downloaded",
                    "filename": "Little.Shop.of.Horrors.1986.mkv",
                    "original_filename": "Little Shop of Horrors 1986",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Little.Shop.of.Horrors.1986.mkv",
                            "bytes": 123,
                            "selected": 1,
                        }
                    ],
                }
            ]
        )
        self.assertIn("movies/Little Shop of Horrors 1986/Little.Shop.of.Horrors.1986.mkv", snapshot["files"])
        self.assertIn("__all__/Little Shop of Horrors 1986/Little.Shop.of.Horrors.1986.mkv", snapshot["files"])
        self.assertEqual(changed, ["movies/Little Shop of Horrors 1986"])

    def test_show_torrent_routed_to_shows(self):
        snapshot, _ = self.builder.build(
            [
                {
                    "id": "SHOW1",
                    "status": "downloaded",
                    "filename": "Ren and Stimpy",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {"id": 1, "path": "/Ren.and.Stimpy.S01E01.mkv", "bytes": 456, "selected": 1}
                    ],
                }
            ]
        )
        self.assertIn("shows/Ren and Stimpy/Ren.and.Stimpy.S01E01.mkv", snapshot["files"])

    def test_unplayable_torrent_is_exposed_under_compat_directory(self):
        snapshot, changed = self.builder.build(
            [
                {
                    "id": "BROKEN1",
                    "status": "error",
                    "filename": "Broken Torrent",
                    "links": [],
                    "files": [
                        {"id": 1, "path": "/Broken.Movie.mkv", "bytes": 42, "selected": 1}
                    ],
                }
            ]
        )
        self.assertIn("__unplayable__/Broken Torrent/__buzz__.json", snapshot["files"])
        self.assertIn("__unplayable__/Broken Torrent/Broken.Movie.mkv", snapshot["files"])
        self.assertEqual(changed, ["__unplayable__/Broken Torrent"])


class BuzzStateTests(unittest.TestCase):
    def test_lookup_and_children_use_normalized_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            snapshot = {
                "dirs": ["", "movies", "movies/Torrent"],
                "files": {
                    "movies/Torrent/file.mkv": {
                        "type": "memory",
                        "content": "",
                        "size": 0,
                        "mime_type": "application/octet-stream",
                        "modified": "2026-01-01T00:00:00Z",
                        "etag": "abc",
                    }
                },
            }
            (state_dir / "library_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=str(state_dir),
                hook_command="",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            state = BuzzState(config, client=None)
            self.assertEqual(normalize_posix_path("/movies/Torrent/file.mkv"), "movies/Torrent/file.mkv")
            self.assertIsNotNone(state.lookup("/movies/Torrent/file.mkv"))
            self.assertEqual(state.list_children("/movies"), ["Torrent"])


class ConfigMigrationTests(unittest.TestCase):
    def test_zurg_to_buzz_maps_supported_fields(self):
        raw = """
zurg: v1
token: test-token
check_for_changes_every_secs: 15
api_timeout_secs: 12
on_library_update: sh /app/media_update.sh "$@"

directories:
  anime:
    group_order: 10
    group: media
    filters:
      - regex: /\\b[a-fA-F0-9]{8}\\b/
      - any_file_inside_regex: /custom/

  shows:
    group_order: 20
    group: media
    filters:
      - has_episodes: true
"""
        buzz = zurg_to_buzz(parse_zurg_config(raw))
        self.assertEqual(buzz["provider"]["token"], "test-token")
        self.assertEqual(buzz["poll_interval_secs"], 15)
        self.assertEqual(buzz["request_timeout_secs"], 12)
        self.assertEqual(buzz["hooks"]["on_library_change"], "sh /app/media_update.sh")
        self.assertEqual(buzz["directories"]["anime"]["patterns"], [r"\b[a-fA-F0-9]{8}\b", "custom"])

    def test_buzz_to_zurg_emits_usable_defaults(self):
        buzz = {
            "provider": {"token": "buzz-token"},
            "poll_interval_secs": 20,
            "server": {"port": 9999},
            "hooks": {"on_library_change": "sh /app/media_update.sh"},
            "directories": {"anime": {"patterns": ["abc", "def"]}},
            "request_timeout_secs": 25,
        }
        rendered = buzz_to_zurg(buzz)
        self.assertIn("token: buzz-token", rendered)
        self.assertIn("check_for_changes_every_secs: 20", rendered)
        self.assertIn('on_library_update: sh /app/media_update.sh "$@"', rendered)
        self.assertIn("      - regex: /abc/", rendered)
        self.assertIn("      - regex: /def/", rendered)

    def test_cli_convert_outputs_json_for_buzz(self):
        raw = """
zurg: v1
token: tok
check_for_changes_every_secs: 10
"""
        converted = convert("zurg", "buzz", raw)
        payload = json.loads(converted)
        self.assertEqual(payload["provider"]["token"], "tok")


if __name__ == "__main__":
    unittest.main()
