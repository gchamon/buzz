import json
import tempfile
import unittest
from pathlib import Path

from buzz.app import BuzzState, Config, LibraryBuilder, normalize_posix_path


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


if __name__ == "__main__":
    unittest.main()
