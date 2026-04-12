import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from buzz.app import BuzzState, Config, Handler, LibraryBuilder, canonical_snapshot, dav_rel_path, normalize_posix_path
from scripts.migrate_config import buzz_to_zurg, convert, parse_buzz_config, parse_zurg_config, zurg_to_buzz


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
            self.assertTrue(state.is_ready())

    def test_no_snapshot_starts_unready_until_first_sync_completes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            state = BuzzState(config, client=None)
            self.assertFalse(state.snapshot_loaded)
            self.assertFalse(state.startup_sync_complete)
            self.assertFalse(state.is_ready())
            state.mark_startup_sync_complete()
            self.assertFalse(state.is_ready())

    def test_successful_sync_marks_state_ready(self):
        class FakeClient:
            def list_torrents(self):
                return [
                    {
                        "id": "TORRENT1",
                        "filename": "Movie.2026.1080p.mkv",
                        "bytes": 123,
                        "progress": 100,
                        "status": "downloaded",
                        "ended": "2026-01-01T00:00:00Z",
                        "links": ["https://example.invalid/file"],
                    }
                ]

            def torrent_info(self, torrent_id):
                return {
                    "id": torrent_id,
                    "status": "downloaded",
                    "filename": "Movie.2026.1080p.mkv",
                    "original_filename": "Movie 2026",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Movie.2026.1080p.mkv",
                            "bytes": 123,
                            "selected": 1,
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            state = BuzzState(config, client=FakeClient())
            report = state.sync(trigger_hook=False)
            state.mark_startup_sync_complete()
            self.assertTrue(report["changed"])
            self.assertTrue(state.snapshot_loaded)
            self.assertTrue(state.is_ready())

    def test_canonical_snapshot_ignores_generated_timestamps(self):
        first = {
            "generated_at": "2026-01-01T00:00:00Z",
            "dirs": ["", "movies"],
            "files": {
                "movies/Movie/file.mkv": {
                    "type": "remote",
                    "size": 123,
                    "url": "https://example.invalid/file",
                    "mime_type": "video/x-matroska",
                    "modified": "2026-01-01T00:00:00Z",
                    "etag": "abc",
                }
            },
            "report": {"movies": 1, "generated_at": "2026-01-01T00:00:00Z"},
        }
        second = {
            "generated_at": "2026-01-02T00:00:00Z",
            "dirs": ["", "movies"],
            "files": {
                "movies/Movie/file.mkv": {
                    "type": "remote",
                    "size": 123,
                    "url": "https://example.invalid/file",
                    "mime_type": "video/x-matroska",
                    "modified": "2026-01-02T00:00:00Z",
                    "etag": "abc",
                }
            },
            "report": {"movies": 1, "generated_at": "2026-01-02T00:00:00Z"},
        }

        self.assertEqual(canonical_snapshot(first), canonical_snapshot(second))

    def test_identical_syncs_after_first_change_are_stable(self):
        class FakeClient:
            def list_torrents(self):
                return [
                    {
                        "id": "TORRENT1",
                        "filename": "Movie.2026.1080p.mkv",
                        "bytes": 123,
                        "progress": 100,
                        "status": "downloaded",
                        "ended": "2026-01-01T00:00:00Z",
                        "links": ["https://example.invalid/file"],
                    }
                ]

            def torrent_info(self, torrent_id):
                return {
                    "id": torrent_id,
                    "status": "downloaded",
                    "filename": "Movie.2026.1080p.mkv",
                    "original_filename": "Movie 2026",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Movie.2026.1080p.mkv",
                            "bytes": 123,
                            "selected": 1,
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            state = BuzzState(config, client=FakeClient())
            first = state.sync(trigger_hook=False)
            second = state.sync(trigger_hook=False)

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(second["changed_paths"], [])

    def test_identical_syncs_do_not_enqueue_duplicate_hooks(self):
        class FakeClient:
            def list_torrents(self):
                return [
                    {
                        "id": "TORRENT1",
                        "filename": "Movie.2026.1080p.mkv",
                        "bytes": 123,
                        "progress": 100,
                        "status": "downloaded",
                        "ended": "2026-01-01T00:00:00Z",
                        "links": ["https://example.invalid/file"],
                    }
                ]

            def torrent_info(self, torrent_id):
                return {
                    "id": torrent_id,
                    "status": "downloaded",
                    "filename": "Movie.2026.1080p.mkv",
                    "original_filename": "Movie 2026",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Movie.2026.1080p.mkv",
                            "bytes": 123,
                            "selected": 1,
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="test-hook",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            state = BuzzState(config, client=FakeClient())
            enqueued = []
            state._enqueue_hook = lambda changed_roots: enqueued.append(list(changed_roots))

            first = state.sync()
            second = state.sync()

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(enqueued, [["movies/Movie 2026"]])

    def test_existing_snapshot_digest_stays_stable_across_restart(self):
        class FakeClient:
            def list_torrents(self):
                return [
                    {
                        "id": "TORRENT1",
                        "filename": "Movie.2026.1080p.mkv",
                        "bytes": 123,
                        "progress": 100,
                        "status": "downloaded",
                        "ended": "2026-01-01T00:00:00Z",
                        "links": ["https://example.invalid/file"],
                    }
                ]

            def torrent_info(self, torrent_id):
                return {
                    "id": torrent_id,
                    "status": "downloaded",
                    "filename": "Movie.2026.1080p.mkv",
                    "original_filename": "Movie 2026",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Movie.2026.1080p.mkv",
                            "bytes": 123,
                            "selected": 1,
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            first_state = BuzzState(config, client=FakeClient())
            first_state.sync(trigger_hook=False)

            second_state = BuzzState(config, client=FakeClient())
            second = second_state.sync(trigger_hook=False)

            self.assertFalse(second["changed"])
            self.assertEqual(second["changed_paths"], [])

    def test_sync_does_not_block_on_hook_execution(self):
        class FakeClient:
            def list_torrents(self):
                return [
                    {
                        "id": "TORRENT1",
                        "filename": "Movie.2026.1080p.mkv",
                        "bytes": 123,
                        "progress": 100,
                        "status": "downloaded",
                        "ended": "2026-01-01T00:00:00Z",
                        "links": ["https://example.invalid/file"],
                    }
                ]

            def torrent_info(self, torrent_id):
                return {
                    "id": torrent_id,
                    "status": "downloaded",
                    "filename": "Movie.2026.1080p.mkv",
                    "original_filename": "Movie 2026",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Movie.2026.1080p.mkv",
                            "bytes": 123,
                            "selected": 1,
                        }
                    ],
                }

        class HookState(BuzzState):
            def __init__(self, *args, **kwargs):
                self.hook_started = threading.Event()
                self.release_hook = threading.Event()
                super().__init__(*args, **kwargs)

            def _run_hook(self, changed_roots: list[str]) -> None:
                self.hook_started.set()
                self.release_hook.wait(timeout=2)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="test-hook",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            state = HookState(config, client=FakeClient())
            report = state.sync()
            self.assertTrue(report["changed"])
            self.assertTrue(state.hook_started.wait(timeout=1))
            self.assertIsNotNone(state.lookup("movies/Movie 2026/Movie.2026.1080p.mkv"))
            self.assertTrue(state.status()["hook_in_progress"])
            state.release_hook.set()

    def test_hook_requests_are_coalesced_while_busy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="test-hook",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            state = BuzzState(config, client=None)
            runs = []
            first_started = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()

            def fake_run_hook(changed_roots):
                runs.append(list(changed_roots))
                if len(runs) == 1:
                    first_started.set()
                    release_first.wait(timeout=2)
                elif len(runs) == 2:
                    second_started.set()

            state._run_hook = fake_run_hook
            state._enqueue_hook(["movies/A"])
            self.assertTrue(first_started.wait(timeout=1))
            state._enqueue_hook(["shows/B"])
            state._enqueue_hook(["movies/A", "movies/C"])
            self.assertTrue(state.status()["hook_pending"])
            release_first.set()
            self.assertTrue(second_started.wait(timeout=1))

            deadline = time.time() + 1
            while time.time() < deadline and state.status()["hook_in_progress"]:
                time.sleep(0.01)

            self.assertEqual(runs[0], ["movies/A"])
            self.assertEqual(runs[1], ["movies/A", "movies/C", "shows/B"])
            self.assertFalse(state.status()["hook_pending"])

    def test_hook_failure_is_reported_without_affecting_readiness(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="test-hook",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
            )
            state = BuzzState(config, client=None)
            state.snapshot_loaded = True
            done = threading.Event()

            def fake_run_hook(changed_roots):
                raise RuntimeError("hook failed")

            state._run_hook = fake_run_hook
            state._enqueue_hook(["movies/A"])
            deadline = time.time() + 1
            while time.time() < deadline:
                if state.status()["hook_last_error"] == "hook failed":
                    done.set()
                    break
                time.sleep(0.01)

            self.assertTrue(done.is_set())
            self.assertTrue(state.is_ready())
            self.assertEqual(state.status()["hook_last_error"], "hook failed")


class DavHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        state_dir = Path(self.tmpdir.name)
        snapshot = {
            "dirs": ["", "movies", "movies/Little Shop [1986] + Extras"],
            "files": {
                "movies/Little Shop [1986] + Extras/Little Shop of Horrors (1986).mkv": {
                    "type": "memory",
                    "content": "ok",
                    "size": 2,
                    "mime_type": "video/x-matroska",
                    "modified": "2026-01-01T00:00:00Z",
                    "etag": "etag-1",
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
        self.state = BuzzState(config, client=None)
        self.handler = Handler.__new__(Handler)
        self.handler.state = self.state

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_dav_rel_path_decodes_encoded_names(self):
        self.assertEqual(
            dav_rel_path("/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/"),
            "movies/Little Shop [1986] + Extras",
        )

    def test_propfind_child_round_trips_encoded_directory_name(self):
        root_body = self.handler._propfind_body(["movies", "movies/Little Shop [1986] + Extras"])
        self.assertIn("/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras", root_body)

        decoded = dav_rel_path("/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/")
        self.assertIsNotNone(self.state.lookup(decoded))

        child_body = self.handler._propfind_body(
            [
                decoded,
                f"{decoded}/Little Shop of Horrors (1986).mkv",
            ]
        )
        self.assertIn("Little%20Shop%20of%20Horrors%20%281986%29.mkv", child_body)

    def test_get_and_head_resolve_encoded_file_paths(self):
        encoded_path = dav_rel_path(
            "/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/"
            "Little%20Shop%20of%20Horrors%20%281986%29.mkv"
        )
        node = self.state.lookup(encoded_path)
        self.assertIsNotNone(node)
        self.assertEqual(node["size"], 2)
        self.assertEqual(node["content"], "ok")


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

    def test_cli_convert_outputs_yaml_for_buzz(self):
        raw = """
zurg: v1
token: tok
check_for_changes_every_secs: 10
"""
        converted = convert("zurg", "buzz", raw)
        self.assertIn("provider:", converted)
        self.assertIn("  token: tok", converted)
        payload = parse_buzz_config(converted)
        self.assertEqual(payload["provider"]["token"], "tok")


if __name__ == "__main__":
    unittest.main()
