import asyncio
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import yaml
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from buzz.core.state import (
    BuzzState,
    LibraryBuilder,
    Poller,
    canonical_snapshot,
    dav_rel_path,
    normalize_posix_path,
)
from buzz.core import db
from buzz.core.tls import ensure_tls_certificate
from buzz.dav_app import DavApp
from buzz.dav_protocol import open_remote_media, propfind_body
from buzz.ui_live import CacheLiveView
from buzz.models import (
    DavConfig as Config,
)
from buzz.models import (
    CuratorConfig,
    DEFAULT_TLS_CERT_PATH,
    DEFAULT_TLS_KEY_PATH,
    SubtitleConfig,
    deep_merge,
    mask_secrets,
    to_nested_dict,
)


class LibraryBuilderTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(
            token="token",
            provider_poll_interval_secs=10,
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
            rd_update_delay_secs=0,
            curator_url="",
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
        self.assertIn(
            "movies/Little Shop of Horrors 1986/Little.Shop.of.Horrors.1986.mkv",
            snapshot["files"],
        )
        self.assertIn(
            "__all__/Little Shop of Horrors 1986/Little.Shop.of.Horrors.1986.mkv",
            snapshot["files"],
        )
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
                        {
                            "id": 1,
                            "path": "/Ren.and.Stimpy.S01E01.mkv",
                            "bytes": 456,
                            "selected": 1,
                        }
                    ],
                }
            ]
        )
        self.assertIn(
            "shows/Ren and Stimpy/Ren.and.Stimpy.S01E01.mkv", snapshot["files"]
        )

    def test_unplayable_torrent_is_exposed_under_compat_directory(self):
        snapshot, changed = self.builder.build(
            [
                {
                    "id": "BROKEN1",
                    "status": "error",
                    "filename": "Broken Torrent",
                    "links": [],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Broken.Movie.mkv",
                            "bytes": 42,
                            "selected": 1,
                        }
                    ],
                }
            ]
        )
        self.assertIn("__unplayable__/Broken Torrent/__buzz__.json", snapshot["files"])
        self.assertIn(
            "__unplayable__/Broken Torrent/Broken.Movie.mkv", snapshot["files"]
        )
        self.assertEqual(changed, ["__unplayable__/Broken Torrent"])

    def test_remote_entries_store_source_url(self):
        snapshot, _ = self.builder.build(
            [
                {
                    "id": "ABC123",
                    "status": "downloaded",
                    "filename": "Movie.mkv",
                    "links": ["https://example.invalid/source-link"],
                    "files": [
                        {"id": 1, "path": "/Movie.mkv", "bytes": 123, "selected": 1}
                    ],
                }
            ]
        )

        self.assertEqual(
            snapshot["files"]["movies/Movie.mkv/Movie.mkv"]["source_url"],
            "https://example.invalid/source-link",
        )


class BuzzStateTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, data, status_code=200, text=""):
            self.data = data
            self.status_code = status_code
            self.text = text

        def json(self):
            return self.data

    class FakeRD:
        def __init__(self, torrents_list=None, torrent_infos=None, download_url=None):
            self.calls = []
            self.unrestrict = self.Unrestrict(self, download_url)
            self.torrents = self.Torrents(torrents_list or [], torrent_infos or {})

        class Unrestrict:
            def __init__(self, parent, download_url):
                self.parent = parent
                self.download_url = download_url or "https://cdn.example.invalid/file"

            def link(self, link):
                self.parent.calls.append(link)
                return BuzzStateTests.FakeResponse({"download": self.download_url})

        class Torrents:
            def __init__(self, torrents_list, torrent_infos):
                self.torrents_list = torrents_list
                self.torrent_infos = torrent_infos
                self.added_magnets = []
                self.info_calls = []
                self.selected_files_calls = []
                self.deleted_ids = []

            def get(self):
                return BuzzStateTests.FakeResponse(self.torrents_list)

            def info(self, torrent_id):
                self.info_calls.append(torrent_id)
                return BuzzStateTests.FakeResponse(self.torrent_infos.get(torrent_id))

            def add_magnet(self, magnet):
                self.added_magnets.append(magnet)
                return BuzzStateTests.FakeResponse({"id": "NEW_TORRENT"})

            def select_files(self, torrent_id, files_str):
                self.selected_files_calls.append((torrent_id, files_str))
                return BuzzStateTests.FakeResponse({}, status_code=204)

            def delete(self, torrent_id):
                self.deleted_ids.append(torrent_id)
                return BuzzStateTests.FakeResponse({}, status_code=204)

    def _create_fake_rd(self):
        torrents_list = [
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
        torrent_infos = {
            "TORRENT1": {
                "id": "TORRENT1",
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
        }
        return self.FakeRD(torrents_list, torrent_infos)

    def test_resolve_download_url_uses_unrestrict_and_caches_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = self.FakeRD()
            state = BuzzState(config, client=client)

            first = state.resolve_download_url("https://example.invalid/source")
            second = state.resolve_download_url("https://example.invalid/source")

            self.assertEqual(first, "https://cdn.example.invalid/file")
            self.assertEqual(second, "https://cdn.example.invalid/file")
            self.assertEqual(client.calls, ["https://example.invalid/source"])

    def test_resolve_download_url_negative_caches_hoster_unavailable(self):
        from buzz.core import state as state_mod
        from buzz.core.state import HosterUnavailableError

        class HosterDownRD:
            def __init__(self):
                self.calls = []
                self.unrestrict = self
                self.torrents = None

            def link(self, url):
                self.calls.append(url)
                return BuzzStateTests.FakeResponse(
                    {"error": "hoster_unavailable"}
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                rd_hoster_failure_cache_secs=60,
                user_agent="buzz-tests",
                version_label="buzz/test",
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = HosterDownRD()
            state = BuzzState(config, client=client)

            with self.assertRaises(HosterUnavailableError) as ctx:
                state.resolve_download_url("https://example.invalid/source")
            self.assertEqual(ctx.exception.code, "hoster_unavailable")
            self.assertFalse(ctx.exception.cached)

            # Second call within TTL must short-circuit without API hit.
            with self.assertRaises(HosterUnavailableError) as cached_ctx:
                state.resolve_download_url("https://example.invalid/source")
            self.assertTrue(cached_ctx.exception.cached)
            self.assertEqual(len(client.calls), 1)

            # Force-expire the negative cache and verify the API is hit again.
            with state.lock:
                state.resolved_urls["https://example.invalid/source"][
                    "expires_at"
                ] = 0.0
            with self.assertRaises(HosterUnavailableError):
                state.resolve_download_url("https://example.invalid/source")
            self.assertEqual(len(client.calls), 2)

            # invalidate_download_url must clear negative entries too.
            state.invalidate_download_url("https://example.invalid/source")
            self.assertNotIn(
                "https://example.invalid/source", state.resolved_urls
            )

            # Regression guard: module exposes the classifier set.
            self.assertIn(
                "hoster_unavailable", state_mod.RD_NON_TRANSIENT_ERRORS
            )

    def test_resolve_download_url_deduplicates_concurrent_hoster_failures(self):
        from buzz.core.state import HosterUnavailableError

        class SlowHosterDownRD:
            def __init__(self):
                self.calls = []
                self.unrestrict = self
                self.torrents = None
                self.lock = threading.Lock()

            def link(self, url):
                with self.lock:
                    self.calls.append(url)
                time.sleep(0.01)
                return BuzzStateTests.FakeResponse(
                    {"error": "hoster_unavailable"}
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="",
                anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
                enable_all_dir=True,
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                rd_hoster_failure_cache_secs=60,
                user_agent="buzz-tests",
                version_label="buzz/test",
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = SlowHosterDownRD()
            state = BuzzState(config, client=client)
            barrier = threading.Barrier(5)
            errors: list[HosterUnavailableError] = []

            def resolve() -> None:
                barrier.wait(timeout=1)
                try:
                    state.resolve_download_url(
                        "https://example.invalid/source"
                    )
                except HosterUnavailableError as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=resolve) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1)

            self.assertEqual(len(errors), 5)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(
                sum(1 for error in errors if not error.cached), 1
            )
            self.assertEqual(sum(1 for error in errors if error.cached), 4)

    def test_torrents_exposes_cached_realdebrid_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            cache = {
                "b": {
                    "signature": {"status": "downloading"},
                    "info": {
                        "id": "b",
                        "filename": "Beta Torrent",
                        "status": "downloading",
                        "progress": 42,
                        "bytes": 2048,
                        "links": ["https://example.invalid/two"],
                        "files": [{"selected": 1}, {"selected": 0}],
                    },
                },
                "a": {
                    "signature": {"status": "downloaded"},
                    "info": {
                        "id": "a",
                        "original_filename": "Alpha Torrent",
                        "status": "downloaded",
                        "progress": 100,
                        "bytes": 1024,
                        "links": ["https://example.invalid/one"],
                        "ended": "2026-01-01T00:00:00Z",
                        "files": [{"selected": 1}, {"selected": 1}],
                    },
                },
            }
            (state_dir / "torrent_cache.json").write_text(
                json.dumps(cache), encoding="utf-8"
            )
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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

            torrents = state.torrents()

            self.assertEqual(
                [item["name"] for item in torrents], ["Alpha Torrent", "Beta Torrent"]
            )
            self.assertEqual(torrents[0]["selected_files"], 2)
            self.assertEqual(torrents[1]["status"], "downloading")

    def test_add_magnet_persists_original_magnet_in_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = self.FakeRD(
                torrent_infos={
                    "NEW_TORRENT": {
                        "id": "NEW_TORRENT",
                        "hash": "ABC123HASH",
                        "filename": "Movie.2026.1080p.mkv",
                        "files": [],
                    }
                }
            )
            state = BuzzState(config, client=client)

            state.add_magnet("magnet:?xt=urn:btih:ABC123HASH&dn=Movie")

            self.assertEqual(
                state.cache["NEW_TORRENT"]["magnet"],
                "magnet:?xt=urn:btih:ABC123HASH&dn=Movie",
            )
            row = state.conn.execute(
                "SELECT magnet FROM torrents WHERE id = ?",
                ("NEW_TORRENT",),
            ).fetchone()
            self.assertEqual(row["magnet"], "magnet:?xt=urn:btih:ABC123HASH&dn=Movie")

    def test_restore_trash_prefers_stored_magnet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = self.FakeRD()
            state = BuzzState(config, client=client)
            state.trashcan = {
                "ABC123HASH": {
                    "hash": "ABC123HASH",
                    "name": "Movie.2026.1080p.mkv",
                    "bytes": 123,
                    "files": [{"id": 1, "path": "/Movie.2026.1080p.mkv"}],
                    "deleted_at": "2026-01-01T00:00:00Z",
                    "magnet": "magnet:?xt=urn:btih:ABC123HASH&dn=Movie",
                }
            }

            state.restore_trash("ABC123HASH")

            self.assertEqual(
                client.torrents.added_magnets,
                ["magnet:?xt=urn:btih:ABC123HASH&dn=Movie"],
            )

    def test_restore_trash_falls_back_to_hash_when_magnet_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = self.FakeRD()
            state = BuzzState(config, client=client)
            state.trashcan = {
                "ABC123HASH": {
                    "hash": "ABC123HASH",
                    "name": "Movie.2026.1080p.mkv",
                    "bytes": 123,
                    "files": [],
                    "deleted_at": "2026-01-01T00:00:00Z",
                    "magnet": None,
                }
            }

            state.restore_trash("ABC123HASH")

            self.assertEqual(
                client.torrents.added_magnets,
                ["magnet:?xt=urn:btih:ABC123HASH"],
            )

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
            (state_dir / "library_snapshot.json").write_text(
                json.dumps(snapshot), encoding="utf-8"
            )
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
            self.assertEqual(
                normalize_posix_path("/movies/Torrent/file.mkv"),
                "movies/Torrent/file.mkv",
            )
            self.assertIsNotNone(state.lookup("/movies/Torrent/file.mkv"))
            self.assertEqual(state.list_children("/movies"), ["Torrent"])
            self.assertTrue(state.is_ready())

    def test_no_snapshot_starts_unready_until_first_sync_completes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            state = BuzzState(config, client=None)
            self.assertFalse(state.snapshot_loaded)
            self.assertFalse(state.startup_sync_complete)
            self.assertFalse(state.is_ready())
            state.mark_startup_sync_complete()
            self.assertFalse(state.is_ready())

    def test_successful_sync_marks_state_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            state = BuzzState(config, client=self._create_fake_rd())
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
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            state = BuzzState(config, client=self._create_fake_rd())
            first = state.sync(trigger_hook=False)
            second = state.sync(trigger_hook=False)

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(second["changed_paths"], [])

    def test_sync_excludes_internal_roots_from_changed_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = self.FakeRD(
                torrents_list=[
                    {
                        "id": "BROKEN1",
                        "filename": "Broken Torrent",
                        "bytes": 42,
                        "progress": 0,
                        "status": "error",
                        "ended": "2026-01-01T00:00:00Z",
                        "links": [],
                    }
                ],
                torrent_infos={
                    "BROKEN1": {
                        "id": "BROKEN1",
                        "status": "error",
                        "filename": "Broken Torrent",
                        "links": [],
                        "files": [
                            {
                                "id": 1,
                                "path": "/Broken.Movie.mkv",
                                "bytes": 42,
                                "selected": 1,
                            }
                        ],
                    }
                },
            )
            state = BuzzState(config, client=client)

            report = state.sync(trigger_hook=False)

            self.assertTrue(report["changed"])
            self.assertEqual(report["changed_paths"], [])
            self.assertEqual(report["added_paths"], [])

    def test_poller_formats_change_log_across_multiple_lines(self):
        state = MagicMock()
        state._format_change_message = (
            lambda added, removed, updated, synced:
            BuzzState._format_change_message(
                added, removed, updated, synced
            )
        )
        poller = Poller(state)

        message = poller._format_change_message(
            [
                "movies/The.Lord.of.the.Rings.The.Fellowship.of.the.Ring.2001.EXTENDED.2160p.UHD.BluRay.x265-BOREDOR",
                "movies/The.Lord.of.the.Rings.The.Return.Of.The.King.2003.EXTENDED.2160p.UHD.BluRay.x265-BOREDOR",
            ],
            [],
            [],
            96,
        )

        self.assertEqual(
            message,
            "\n".join(
                [
                    "real-Debrid library changed (96 torrents):",
                    "  +2 added",
                    "    movies/The.Lord.of.the.Rings.The.Fellowship.of.the.Ring.2001.EXTENDED.2160p.UHD.BluRay.x265-BOREDOR",
                    "    movies/The.Lord.of.the.Rings.The.Return.Of.The.King.2003.EXTENDED.2160p.UHD.BluRay.x265-BOREDOR",
                ]
            ),
        )

    def test_poller_does_not_emit_duplicate_change_event(self):
        state = MagicMock()
        state.config.provider_poll_interval_secs = 0
        state.sync.return_value = {
            "changed": True,
            "added_paths": ["shows/Black Mirror S07"],
            "removed_paths": [],
            "updated_paths": [],
            "synced_torrents": 100,
        }
        poller = Poller(state)
        poller._stop_event.wait = MagicMock(side_effect=[False, True])

        with patch("buzz.core.state.record_event") as mock_record:
            poller.run()

        state.sync.assert_called_once_with()
        mock_record.assert_not_called()

    def test_identical_syncs_do_not_enqueue_duplicate_hooks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
            state = BuzzState(config, client=self._create_fake_rd())
            enqueued = []
            state._enqueue_hook = lambda changed_roots: enqueued.append(
                list(changed_roots)
            )

            first = state.sync()
            second = state.sync()

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(enqueued, [["movies/Movie 2026"]])

    def test_sync_enqueues_curator_rebuild_without_hook_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                curator_url="http://curator.invalid/rebuild",
            )
            state = BuzzState(config, client=self._create_fake_rd())
            enqueued = []
            state._enqueue_hook = lambda changed_roots: enqueued.append(
                list(changed_roots)
            )

            report = state.sync()

            self.assertTrue(report["changed"])
            self.assertEqual(enqueued, [["movies/Movie 2026"]])

    def test_direct_sync_logs_realdebrid_change_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                curator_url="",
            )
            state = BuzzState(config, client=self._create_fake_rd())

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                report = state.sync()

            self.assertTrue(report["changed"])
            logged = stdout.getvalue()
            self.assertIn("real-Debrid library changed (1 torrents):", logged)
            self.assertIn("+1 added", logged)
            self.assertIn("movies/Movie 2026", logged)
            self.assertIn('"event": "realdebrid_update"', logged)

    def test_show_only_addition_enqueues_show_curator_rebuild(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                curator_url="http://curator.invalid/rebuild",
            )
            movie_summary = {
                "id": "MOVIE1",
                "filename": "Movie.2026.1080p.mkv",
                "bytes": 123,
                "progress": 100,
                "status": "downloaded",
                "ended": "2026-01-01T00:00:00Z",
                "links": ["https://example.invalid/movie"],
            }
            show_summary = {
                "id": "SHOW1",
                "filename": "Black.Mirror.S07.2160p.mkv",
                "bytes": 456,
                "progress": 100,
                "status": "downloaded",
                "ended": "2026-01-02T00:00:00Z",
                "links": ["https://example.invalid/show"],
            }
            movie_info = {
                "id": "MOVIE1",
                "status": "downloaded",
                "filename": "Movie.2026.1080p.mkv",
                "original_filename": "Movie 2026",
                "links": ["https://example.invalid/movie"],
                "files": [
                    {
                        "id": 1,
                        "path": "/Movie.2026.1080p.mkv",
                        "bytes": 123,
                        "selected": 1,
                    }
                ],
            }
            show_info = {
                "id": "SHOW1",
                "status": "downloaded",
                "filename": "Black.Mirror.S07.2160p.mkv",
                "original_filename": "Black Mirror S07",
                "links": ["https://example.invalid/show"],
                "files": [
                    {
                        "id": 1,
                        "path": "/Black.Mirror.S07E01.mkv",
                        "bytes": 456,
                        "selected": 1,
                    }
                ],
            }
            client = self.FakeRD(
                torrents_list=[movie_summary],
                torrent_infos={"MOVIE1": movie_info, "SHOW1": show_info},
            )
            state = BuzzState(config, client=client)
            state.sync(trigger_hook=False)
            client.torrents.torrents_list = [movie_summary, show_summary]
            enqueued = []
            state._enqueue_hook = lambda changed_roots: enqueued.append(
                list(changed_roots)
            )

            report = state.sync()

            self.assertEqual(report["added_paths"], ["shows/Black Mirror S07"])
            self.assertEqual(report["removed_paths"], [])
            self.assertEqual(report["updated_paths"], [])
            self.assertEqual(enqueued, [["shows/Black Mirror S07"]])

    def test_sync_refetches_stale_cached_info_when_summary_has_ready_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                curator_url="",
            )
            summary = {
                "id": "SHOW1",
                "filename": "Black.Mirror.S07.2160p.mkv",
                "bytes": 456,
                "progress": 100,
                "status": "downloaded",
                "ended": "2026-01-02T00:00:00Z",
                "links": ["https://example.invalid/show"],
            }
            stale_info = {
                "id": "SHOW1",
                "status": "downloaded",
                "filename": "Black.Mirror.S07.2160p.mkv",
                "links": [],
                "files": [
                    {
                        "id": 1,
                        "path": "/Black.Mirror.S07E01.mkv",
                        "bytes": 456,
                        "selected": 1,
                    }
                ],
            }
            fresh_info = {
                **stale_info,
                "links": ["https://example.invalid/show"],
            }
            client = self.FakeRD(
                torrents_list=[summary],
                torrent_infos={"SHOW1": fresh_info},
            )
            state = BuzzState(config, client=client)
            signature = state._summary_signature(summary)
            state.cache["SHOW1"] = {
                "signature": signature,
                "info": stale_info,
                "magnet": None,
            }

            report = state.sync(trigger_hook=False)

            self.assertEqual(client.torrents.info_calls, ["SHOW1"])
            self.assertEqual(
                report["added_paths"],
                ["shows/Black.Mirror.S07.2160p.mkv"],
            )
            self.assertIn(
                "shows/Black.Mirror.S07.2160p.mkv/Black.Mirror.S07E01.mkv",
                state.snapshot["files"],
            )

    def test_sync_moves_upstream_removed_torrent_to_trashcan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeRD([], {}))
            state.cache = {
                "TORRENT1": {
                    "signature": {"status": "downloaded"},
                    "magnet": "magnet:?xt=urn:btih:ABC123HASH&dn=Movie",
                    "info": {
                        "id": "TORRENT1",
                        "hash": "ABC123HASH",
                        "filename": "Movie.2026.1080p.mkv",
                        "original_filename": "Movie 2026",
                        "bytes": 123,
                        "files": [
                            {
                                "id": 1,
                                "path": "/Movie.2026.1080p.mkv",
                                "bytes": 123,
                                "selected": 1,
                            }
                        ],
                    },
                }
            }

            report = state.sync(trigger_hook=False)

            self.assertTrue(report["changed"])
            self.assertEqual(state.cache, {})
            self.assertIn("ABC123HASH", state.trashcan)
            self.assertEqual(
                state.trashcan["ABC123HASH"]["name"],
                "Movie.2026.1080p.mkv",
            )
            self.assertEqual(
                state.trashcan["ABC123HASH"]["magnet"],
                "magnet:?xt=urn:btih:ABC123HASH&dn=Movie",
            )

    def test_delete_torrent_archives_when_upstream_item_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = self._create_fake_rd()
            state = BuzzState(config, client=client)
            state.sync(trigger_hook=False)
            state.cache["TORRENT1"]["info"]["hash"] = "ABC123HASH"

            def missing_delete(torrent_id):
                return self.FakeResponse({}, status_code=404, text="not found")

            client.torrents.delete = missing_delete

            result = state.delete_torrent("TORRENT1")

            self.assertEqual(result["status"], "success")
            self.assertIn("warning", result)
            self.assertNotIn("TORRENT1", state.cache)
            self.assertIn("ABC123HASH", state.trashcan)

    def test_delete_torrent_keeps_non_missing_upstream_errors_hard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            client = self._create_fake_rd()
            state = BuzzState(config, client=client)
            state.sync(trigger_hook=False)
            state.cache["TORRENT1"]["info"]["hash"] = "ABC123HASH"

            def failing_delete(torrent_id):
                return self.FakeResponse({}, status_code=500, text="server error")

            client.torrents.delete = failing_delete

            with self.assertRaisesRegex(ValueError, "Failed to delete torrent"):
                state.delete_torrent("TORRENT1")

    @patch("buzz.core.state.record_event")
    @patch("buzz.core.state.subprocess.run")
    def test_run_hook_logs_stdout_and_stderr_on_failure(
        self, mock_run, mock_record_event
    ):
        config = Config(
            token="token",
            provider_poll_interval_secs=10,
            bind="127.0.0.1",
            port=9999,
            state_dir="/tmp/buzz-tests",
            hook_command="sh /app/scripts/media_update.sh",
            anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
            enable_all_dir=True,
            enable_unplayable_dir=True,
            request_timeout_secs=30,
            user_agent="buzz-tests",
            version_label="buzz/test",
            curator_url="",
        )
        state = BuzzState(config, client=None)
        mock_run.side_effect = subprocess.CalledProcessError(
            2,
            ["sh", "/app/scripts/media_update.sh", "movies/Interstellar"],
            output="hook stdout",
            stderr="hook stderr",
        )

        state._run_hook(["movies/Interstellar"])

        mock_record_event.assert_any_call(
            "running library update hook: 1 roots",
            event="hook_running_command",
            changed_roots=1,
        )
        mock_record_event.assert_any_call(
            "\n".join(
                [
                    "library update hook failed with exit code 2: ['sh', '/app/scripts/media_update.sh', 'movies/Interstellar']",
                    "stdout:\nhook stdout",
                    "stderr:\nhook stderr",
                ]
            ),
            level="error",
        )

    def test_existing_snapshot_digest_stays_stable_across_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            first_state = BuzzState(config, client=self._create_fake_rd())
            first_state.sync(trigger_hook=False)

            second_state = BuzzState(config, client=self._create_fake_rd())
            second = second_state.sync(trigger_hook=False)

            self.assertFalse(second["changed"])
            self.assertEqual(second["changed_paths"], [])

    def test_sync_does_not_block_on_hook_execution(self):
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
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            state = HookState(config, client=self._create_fake_rd())
            report = state.sync()
            self.assertTrue(report["changed"])
            self.assertTrue(state.hook_started.wait(timeout=5))
            self.assertIsNotNone(state.lookup("movies/Movie 2026/Movie.2026.1080p.mkv"))
            self.assertTrue(state.status()["hook_in_progress"])
            state.release_hook.set()

    def test_hook_requests_are_coalesced_while_busy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
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
            self.assertTrue(first_started.wait(timeout=5))
            status = state.status()
            self.assertTrue(status["hook_in_progress"])
            self.assertEqual(status["hook_phase"], "queued")
            self.assertEqual(status["hook_active_paths"], ["movies/A"])
            self.assertEqual(status["hook_pending_count"], 0)
            self.assertIsNotNone(status["hook_wait_started_at"])
            state._enqueue_hook(["shows/B"])
            state._enqueue_hook(["movies/A", "movies/C"])
            status = state.status()
            self.assertTrue(state.status()["hook_pending"])
            self.assertEqual(status["hook_pending_count"], 3)
            release_first.set()
            self.assertTrue(second_started.wait(timeout=5))

            deadline = time.time() + 1
            while time.time() < deadline and state.status()["hook_in_progress"]:
                time.sleep(0.01)

            self.assertEqual(runs[0], ["movies/A"])
            self.assertEqual(runs[1], ["movies/A", "movies/C", "shows/B"])
            status = state.status()
            self.assertFalse(status["hook_pending"])
            self.assertEqual(status["hook_phase"], "complete")

    def test_hook_failure_is_reported_without_affecting_readiness(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_poll_interval_secs=10,
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
                rd_update_delay_secs=0,
                curator_url="",
            )
            state = BuzzState(config, client=None)
            state.snapshot_loaded = True
            done = threading.Event()

            def fake_run_hook(changed_roots):
                raise RuntimeError("hook failed")

            state._run_hook = fake_run_hook
            state._enqueue_hook(["movies/A"])
            deadline = time.time() + 5
            while time.time() < deadline:
                if state.status()["hook_last_error"] == "hook failed":
                    done.set()
                    break
                time.sleep(0.01)

            self.assertTrue(done.is_set())
            self.assertTrue(state.is_ready())
            self.assertEqual(state.status()["hook_last_error"], "hook failed")


class PollerTests(unittest.TestCase):
    def test_background_sync_failure_records_warning_without_last_error(self):
        class FakeStopEvent:
            def __init__(self):
                self.calls = 0

            def wait(self, _timeout):
                self.calls += 1
                return self.calls > 1

            def set(self):
                pass

        class FakeState:
            def __init__(self):
                self.config = Config(provider_poll_interval_secs=0)
                self.last_error = "previous"
                self.lock = threading.Lock()

            def sync(self):
                raise RuntimeError("rd unavailable")

        state = FakeState()
        poller = Poller(cast(Any, state))
        poller._stop_event = cast(Any, FakeStopEvent())

        with patch("buzz.core.state.record_event") as mock_record_event:
            poller.run()

        self.assertIsNone(state.last_error)
        mock_record_event.assert_called_once_with(
            "background sync failed: rd unavailable",
            level="warning",
        )


class DavAppTests(unittest.TestCase):
    class FakeRDResponse:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class FakeRD:
        def __init__(self, download_urls=None):
            self.calls = []
            self.download_urls = download_urls or []
            self.torrents = None
            self.unrestrict = self.Unrestrict(self)

        class Unrestrict:
            def __init__(self, parent):
                self.parent = parent

            def link(self, link):
                self.parent.calls.append(link)
                idx = (
                    len(self.parent.calls) - 1
                    if len(self.parent.calls) <= len(self.parent.download_urls)
                    else 0
                )
                url = (
                    self.parent.download_urls[idx]
                    if self.parent.download_urls
                    else "https://cdn.example.invalid/file"
                )
                return DavAppTests.FakeRDResponse({"download": url})

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
        (state_dir / "library_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8"
        )
        config = Config(
            token="token",
            provider_poll_interval_secs=10,
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
            rd_update_delay_secs=0,
        )
        rd_patcher = patch("buzz.dav_app.RD", return_value=self.FakeRD())
        languages_patcher = patch(
            "buzz.dav_app._fetch_opensubtitles_languages",
            return_value=[],
        )
        self.addCleanup(rd_patcher.stop)
        self.addCleanup(languages_patcher.stop)
        rd_patcher.start()
        languages_patcher.start()
        self.dav_app = DavApp(config)
        self.state = self.dav_app.state
        self.client_cm = TestClient(self.dav_app.app)
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmpdir.cleanup()

    def test_dav_rel_path_decodes_encoded_names(self):
        self.assertEqual(
            dav_rel_path("/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/"),
            "movies/Little Shop [1986] + Extras",
        )

    def test_hook_status_is_visible_in_ui_meta_items(self):
        self.dav_app.state.hook_phase = "waiting_vfs"
        self.dav_app.state.hook_active_paths = ["movies/A", "shows/B"]

        meta_items = CacheLiveView(self.dav_app)._meta_items()

        self.assertIn(
            {"label": "hook", "value": "waiting_vfs (2 roots)"},
            meta_items,
        )

    def test_propfind_child_round_trips_encoded_directory_name(self):
        root_body = propfind_body(
            self.state,
            ["movies", "movies/Little Shop [1986] + Extras"]
        )
        self.assertIn(
            "/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras", root_body
        )

        decoded = dav_rel_path("/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/")
        self.assertIsNotNone(self.state.lookup(decoded))

        child_body = propfind_body(
            self.state,
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
        if node is None:
            self.fail("Expected encoded DAV path to resolve")
        self.assertEqual(node["size"], 2)
        self.assertEqual(node["content"], "ok")

    def test_cache_page_renders_pyview_shell(self):
        self.dav_app.config.subtitles.enabled = True
        self.state.cache = {
            "torrent-1": {
                "signature": {},
                "info": {
                    "id": "torrent-1",
                    "original_filename": "Movie & Stuff",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 1572864,
                    "links": ["https://example.invalid/file"],
                    "ended": "2026-01-01T00:00:00Z",
                    "files": [{"selected": 1}, {"selected": 0}],
                },
            }
        }
        self.state.last_sync_at = "2026-01-02T00:00:00Z"

        response = self.client.get("/cache")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn("buzz: cache", body)
        self.assertIn('data-phx-main="true"', body)
        self.assertIn('src="/pyview/assets/app.js"', body)
        self.assertIn("Movie &amp; Stuff", body)
        self.assertIn("1.5 MiB", body)
        self.assertIn('href="/static/buzz.css"', body)
        self.assertIn('phx-click="prompt_delete"', body)
        self.assertIn('phx-click="fetch_subs"', body)
        self.assertIn('phx-disable-with="..."', body)
        self.assertIn('phx-hook="BuzzOverflowMarquee"', body)
        self.assertIn("data-marquee-clip", body)
        self.assertIn("data-marquee-label", body)
        self.assertIn('title="Movie &amp; Stuff"', body)

    def test_archive_page_renders_pyview_shell(self):
        self.state.trashcan = {
            "trash-1": {
                "hash": "trash-1",
                "name": "Old & Gone",
                "bytes": 4096,
                "file_count": 3,
                "deleted_at": "2026-01-03T00:00:00Z",
                "magnet": "magnet:?xt=urn:btih:trash-1",
            }
        }

        response = self.client.get("/archive")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn("buzz: archive", body)
        self.assertIn('data-phx-main="true"', body)
        self.assertIn('src="/pyview/assets/app.js"', body)
        self.assertIn("fa-box-archive", body)
        self.assertIn('id="nav-archive-count"', body)
        self.assertIn("archive(<span id=\"nav-archive-count\">1</span>)", body)
        self.assertIn('id="nav-log-count"', body)
        self.assertIn("Old &amp; Gone", body)
        self.assertIn('href="/static/buzz.css"', body)
        self.assertIn('phx-click="prompt_restore"', body)
        self.assertIn('phx-hook="BuzzOverflowMarquee"', body)
        self.assertIn("data-marquee-clip", body)
        self.assertIn("data-marquee-label", body)
        self.assertIn('title="Old &amp; Gone"', body)

    def test_confirmation_actions_render_in_progress_disable_text(self):
        cache_template = Path("buzz/pyview_templates/cache_live.html").read_text(
            encoding="utf-8"
        )
        archive_template = Path("buzz/pyview_templates/archive_live.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('phx-disable-with="..."', cache_template)
        self.assertIn('phx-disable-with="..."', archive_template)

    def test_cache_page_renders_empty_state_and_error_banner(self):
        self.state.last_error = "Boom & stuff"

        response = self.client.get("/cache")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-phx-main="true"', body)
        self.assertIn("No cached items yet.", body)
        self.assertIn("Boom &amp; stuff", body)

    def test_archive_page_renders_empty_state(self):
        response = self.client.get("/archive")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn("Archive is empty.", body)

    def test_logs_page_renders_pyview_content(self):
        response = self.client.get("/logs")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn("buzz: system logs", body)
        self.assertIn('src="/pyview/assets/app.js"', body)
        self.assertIn("System Logs", body)
        self.assertNotIn("RESTART STACK", body)
        self.assertIn("COPY", body)

    def test_config_page_renders_pyview_content(self):
        response = self.client.get("/config")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn("buzz: config", body)
        self.assertIn('src="/pyview/assets/app.js"', body)
        self.assertIn("Effective Configuration", body)
        self.assertIn("EDIT", body)

    def test_config_page_marks_ui_overrides_in_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            overrides_path = Path(tmpdir) / "buzz.overrides.yml"
            base_path.write_text(
                f"provider:\n  token: testtoken\nstate_dir: {tmpdir}\n",
                encoding="utf-8",
            )
            overrides_path.write_text(
                "logging:\n  verbose: true\n",
                encoding="utf-8",
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.RD", return_value=DavAppTests.FakeRD())
            languages_patcher = patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                return_value=[],
            )
            rd_patcher.start()
            languages_patcher.start()
            self.addCleanup(rd_patcher.stop)
            self.addCleanup(languages_patcher.stop)
            app = DavApp(config)
            client = TestClient(app.app)

            response = client.get("/config")

            self.assertEqual(response.status_code, 200)
            self.assertIn("# Overriden via UI. Default: false", response.text)
            from buzz.ui_live import ConfigLiveView, _load_template
            from pyview.meta import PyViewMeta

            view = ConfigLiveView(owner=app)
            context = view._context(is_editing=True)
            html = _load_template("config_live.html").render(context, PyViewMeta())
            self.assertIn("hot reload · default: false", html)

    def test_config_page_ignores_default_valued_override_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dist_path = root / "buzz.dist.yml"
            base_path = root / "buzz.yml"
            data_dir = root / "data"
            data_dir.mkdir()
            overrides_path = data_dir / "buzz.overrides.yml"
            dist_path.write_text(
                "provider:\n  token: yourtoken\n"
                "hooks:\n"
                "  curator_url: http://buzz-curator:8400/rebuild\n"
                "  rd_update_delay_secs: 15\n"
                "logging:\n  verbose: false\n"
                "subtitles:\n"
                "  enabled: false\n"
                "  fetch_on_resync: false\n"
                "  filters:\n"
                "    hearing_impaired: exclude\n"
                "    exclude_ai: true\n"
                "    exclude_machine: true\n"
                "  search_delay_secs: 0.5\n"
                "  download_delay_secs: 1.0\n",
                encoding="utf-8",
            )
            base_path.write_text(
                "provider:\n  token: testtoken\n"
                f"state_dir: {data_dir}\n"
                "subtitles:\n"
                "  enabled: true\n"
                "  languages:\n"
                "    - en\n"
                "    - pt-br\n"
                "  strategy: best-rated\n",
                encoding="utf-8",
            )
            overrides_path.write_text(
                "hooks:\n"
                "  curator_url: http://buzz-curator:8400/rebuild\n"
                "  rd_update_delay_secs: 15\n"
                "logging:\n  verbose: false\n"
                "subtitles:\n"
                "  enabled: true\n"
                "  fetch_on_resync: false\n"
                "  filters:\n"
                "    hearing_impaired: exclude\n"
                "    exclude_ai: true\n"
                "    exclude_machine: true\n"
                "  search_delay_secs: 0.5\n"
                "  download_delay_secs: 1.0\n"
                "  languages:\n"
                "    - en\n"
                "    - pt-br\n"
                "  strategy: best-rated\n",
                encoding="utf-8",
            )
            config = Config.load(str(base_path))
            rd_patcher = patch(
                "buzz.dav_app.RD",
                return_value=DavAppTests.FakeRD(),
            )
            languages_patcher = patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                return_value=[],
            )
            rd_patcher.start()
            languages_patcher.start()
            self.addCleanup(rd_patcher.stop)
            self.addCleanup(languages_patcher.stop)
            app = DavApp(config)
            client = TestClient(app.app)

            response = client.get("/config")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn(
                "# Overriden via UI\n  curator_url: http://buzz-curator:8400/rebuild",
                response.text,
            )
            self.assertNotIn(
                "# Overriden via UI\n  rd_update_delay_secs: 15",
                response.text,
            )
            self.assertNotIn(
                "# Overriden via UI\n  verbose: false",
                response.text,
            )
            self.assertNotIn(
                "# Overriden via UI\n  fetch_on_resync: false",
                response.text,
            )
            self.assertNotIn(
                "# Overriden via UI\n    hearing_impaired: exclude",
                response.text,
            )
            self.assertNotIn(
                "# Overriden via UI\n    exclude_ai: true",
                response.text,
            )
            self.assertNotIn(
                "# Overriden via UI\n    exclude_machine: true",
                response.text,
            )
            self.assertNotIn(
                "# Overriden via UI\n  search_delay_secs: 0.5",
                response.text,
            )
            self.assertNotIn(
                "# Overriden via UI\n  download_delay_secs: 1.0",
                response.text,
            )

    def test_dockerfile_copies_config_templates(self):
        dockerfile = Path("buzz/Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY pyproject.toml README.md buzz.dist.yml /app/", dockerfile)

    def test_minimal_config_exists_and_is_valid(self):
        min_config_path = Path("buzz.min.yml")
        self.assertTrue(min_config_path.exists())
        with open(min_config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self.assertIn("provider", data)
        self.assertIn("token", data["provider"])
        self.assertIn("media_server", data)
        self.assertIn("subtitles", data)

    def test_pyview_assets_are_served(self):
        response = self.client.get("/pyview/assets/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("LiveSocket", response.text)

    def test_healthz_and_readyz_use_asgi_routes(self):
        self.state.snapshot_loaded = False
        health = self.client.get("/healthz")
        ready = self.client.get("/readyz")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertEqual(health.json()["archive_count"], 0)
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json()["status"], "starting")

        self.state.snapshot_loaded = True
        ready = self.client.get("/readyz")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertEqual(ready.json()["ui_status"], "starting")

    def test_readyz_waits_for_curator_ready_signal(self):
        self.dav_app.config.curator_url = "http://buzz-curator:8400/rebuild"
        self.dav_app.curator_ready = False
        self.state.snapshot_loaded = True

        response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["ui_status"], "starting")
        self.assertFalse(response.json()["curator_ready"])

        notify = self.client.post(
            "/api/ui/notify",
            json={
                "topics": ["status"],
                "message": {
                    "source": "curator",
                    "event": "curator_ready",
                    "level": "info",
                    "message": "Curator startup complete",
                },
            },
        )
        self.assertEqual(notify.status_code, 200)

        response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["ui_status"], "ready")
        self.assertTrue(response.json()["curator_ready"])

    def test_cache_page_shows_starting_until_curator_ready(self):
        self.dav_app.config.curator_url = "http://buzz-curator:8400/rebuild"
        self.dav_app.curator_ready = False
        self.state.snapshot_loaded = True

        response = self.client.get("/cache")

        self.assertEqual(response.status_code, 200)
        self.assertIn('[starting]</b>', response.text)

    def test_dav_app_init_does_not_fetch_languages_synchronously(self):
        config = Config(
            token="token",
            provider_poll_interval_secs=10,
            bind="127.0.0.1",
            port=9999,
            state_dir=self.tmpdir.name,
            hook_command="",
            anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
            enable_all_dir=True,
            enable_unplayable_dir=True,
            request_timeout_secs=30,
            user_agent="buzz-tests",
            version_label="buzz/test",
            rd_update_delay_secs=0,
        )
        with (
            patch("buzz.dav_app.RD", return_value=self.FakeRD()),
            patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                side_effect=AssertionError("should not run in __init__"),
            ),
        ):
            app = DavApp(config)

        self.assertEqual(app.opensubtitles_languages, [])

    def _config_with_credentials(self, tmpdir: str) -> Config:
        return Config(
            token="token",
            provider_poll_interval_secs=10,
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
            rd_update_delay_secs=0,
            subtitles=SubtitleConfig(
                api_key="ak", username="u", password="p"
            ),
        )

    def test_startup_uses_cached_languages_without_fetching(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = db.connect(Path(tmpdir) / "buzz.sqlite")
            db.apply_migrations(conn)
            db.save_opensubtitles_languages(
                conn, [("en", "English"), ("pt", "Portuguese")]
            )
            conn.close()
            config = self._config_with_credentials(tmpdir)
            with (
                patch("buzz.dav_app.RD", return_value=self.FakeRD()),
                patch(
                    "buzz.dav_app._fetch_opensubtitles_languages",
                    side_effect=AssertionError("should not refetch when fresh"),
                ),
            ):
                app = DavApp(config)
                app._load_opensubtitles_languages()
            self.assertEqual(
                app.opensubtitles_languages,
                [("en", "English"), ("pt", "Portuguese")],
            )

    def test_stale_cache_triggers_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = db.connect(Path(tmpdir) / "buzz.sqlite")
            db.apply_migrations(conn)
            db.save_opensubtitles_languages(conn, [("old", "Old Lang")])
            conn.execute(
                "UPDATE opensubtitles_languages_meta"
                " SET fetched_at = '2000-01-01T00:00:00Z' WHERE singleton = 1"
            )
            conn.commit()
            conn.close()
            config = self._config_with_credentials(tmpdir)
            refreshed = threading.Event()

            def fake_fetch(api_key):
                refreshed.set()
                return [("fr", "French")]

            with (
                patch("buzz.dav_app.RD", return_value=self.FakeRD()),
                patch(
                    "buzz.dav_app._fetch_opensubtitles_languages",
                    side_effect=fake_fetch,
                ),
            ):
                app = DavApp(config)
                app._load_opensubtitles_languages()
                self.assertTrue(refreshed.wait(timeout=2.0))
                # give the background thread a moment to finish the save
                for _ in range(20):
                    if app.opensubtitles_languages == [("fr", "French")]:
                        break
                    time.sleep(0.05)

            self.assertEqual(app.opensubtitles_languages, [("fr", "French")])
            conn = db.connect(Path(tmpdir) / "buzz.sqlite")
            try:
                cached, fetched_at = db.load_opensubtitles_languages(conn)
            finally:
                conn.close()
            self.assertEqual(cached, [("fr", "French")])
            self.assertNotEqual(fetched_at, "2000-01-01T00:00:00Z")

    def test_refresh_skipped_without_credentials(self):
        config = Config(
            token="token",
            provider_poll_interval_secs=10,
            bind="127.0.0.1",
            port=9999,
            state_dir=self.tmpdir.name,
            hook_command="",
            anime_patterns=(r"\b[a-fA-F0-9]{8}\b",),
            enable_all_dir=True,
            enable_unplayable_dir=True,
            request_timeout_secs=30,
            user_agent="buzz-tests",
            version_label="buzz/test",
            rd_update_delay_secs=0,
        )
        with (
            patch("buzz.dav_app.RD", return_value=self.FakeRD()),
            patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                side_effect=AssertionError("should not fetch without creds"),
            ),
        ):
            app = DavApp(config)
            self.assertFalse(app.trigger_language_refresh())

    def test_manual_reload_while_refresh_running_is_noop(self):
        config = self._config_with_credentials(self.tmpdir.name)
        with (
            patch("buzz.dav_app.RD", return_value=self.FakeRD()),
            patch("buzz.dav_app._fetch_opensubtitles_languages", return_value=[]),
        ):
            app = DavApp(config)
        app._language_refresh_running = True
        self.assertFalse(app.trigger_language_refresh(force=True))

    def test_rendered_config_without_credentials_omits_subtitle_controls(self):
        from buzz.ui_live import ConfigLiveView, _load_template
        from pyview.meta import PyViewMeta

        view = ConfigLiveView(owner=self.dav_app)
        context = view._context(is_editing=True)
        html = _load_template("config_live.html").render(context, PyViewMeta())

        self.assertIn("subtitles.enabled", html)
        self.assertIn("config-credentials-hint", html)
        self.assertNotIn("subtitles.fetch_on_resync", html)
        self.assertNotIn("lang-list", html)
        self.assertNotIn("subtitles.strategy", html)
        self.assertNotIn("subtitles.filters.exclude_ai", html)
        self.assertNotIn("subtitles.search_delay_secs", html)
        self.assertNotIn("reload_languages", html)

    def test_rendered_config_with_credentials_includes_subtitle_controls(self):
        from buzz.ui_live import ConfigLiveView, _load_template
        from pyview.meta import PyViewMeta

        self.dav_app.saved_config.subtitles.api_key = "ak"
        self.dav_app.saved_config.subtitles.username = "u"
        self.dav_app.saved_config.subtitles.password = "p"
        view = ConfigLiveView(owner=self.dav_app)
        context = view._context(is_editing=True)
        html = _load_template("config_live.html").render(context, PyViewMeta())

        self.assertIn("subtitles.fetch_on_resync", html)
        self.assertIn("lang-list", html)
        self.assertIn("subtitles.strategy", html)
        self.assertIn("subtitles.filters.exclude_ai", html)
        self.assertIn("subtitles.search_delay_secs", html)
        self.assertIn("reload_languages", html)
        self.assertIn("fa-arrows-rotate", html)

    def test_options_and_propfind_use_asgi_routes(self):
        options = self.client.options("/dav/movies")
        propfind = self.client.request("PROPFIND", "/dav/movies", headers={"Depth": "1"})

        self.assertEqual(options.status_code, 204)
        self.assertEqual(options.headers["dav"], "1")
        self.assertEqual(propfind.status_code, 207)
        self.assertIn(
            "/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras",
            propfind.text,
        )

    def test_api_validation_errors_return_json_error_envelope(self):
        response = self.client.post("/api/cache/add", json={"magnet": "  "})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Value error, Missing magnet link"})

    def test_memory_file_head_and_range_get_use_asgi_routes(self):
        head = self.client.head(
            "/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/"
            "Little%20Shop%20of%20Horrors%20%281986%29.mkv"
        )
        get_range = self.client.get(
            "/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/"
            "Little%20Shop%20of%20Horrors%20%281986%29.mkv",
            headers={"Range": "bytes=0-0"},
        )

        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["content-length"], "2")
        self.assertEqual(get_range.status_code, 206)
        self.assertEqual(get_range.headers["content-range"], "bytes 0-0/2")
        self.assertEqual(get_range.content, b"o")

    def test_remote_media_refreshes_stale_html_response_once(self):
        self.state.client = self.FakeRD(
            ["https://example.invalid/stale", "https://example.invalid/fresh"]
        )

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._stream = memoryview(body)
                self.headers = {"Content-Type": content_type}

            def read(self, amount=-1):
                if amount is None or amount < 0:
                    amount = len(self._stream)
                chunk = self._stream[:amount].tobytes()
                self._stream = self._stream[amount:]
                return chunk

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()
                return False

        response_queue = [
            FakeResponse(
                b"<!DOCTYPE html><html>bad</html>", "text/html; charset=utf-8"
            ),
            FakeResponse(b"\x1a\x45\xdf\xa3media-bytes", "video/x-matroska"),
        ]

        self.state.snapshot["files"][
            "movies/Little Shop [1986] + Extras/Little Shop of Horrors (1986).mkv"
        ] = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-2",
        }

        with patch("buzz.dav_protocol._open_upstream_response", side_effect=response_queue):
            node = self.state.lookup(
                "movies/Little Shop [1986] + Extras/Little Shop of Horrors (1986).mkv"
            )
            if node is None:
                self.fail("Expected snapshot node for streaming test")
            response, first_chunk = open_remote_media(self.state, node, None)
            self.assertEqual(first_chunk, b"\x1a\x45\xdf\xa3media-bytes")
            response.close()

        self.assertEqual(
            self.state.client.calls,
            ["https://example.invalid/source", "https://example.invalid/source"],
        )

    def test_remote_media_returns_bad_gateway_after_failed_retry(self):
        self.state.client = self.FakeRD(
            ["https://example.invalid/1", "https://example.invalid/2"]
        )

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._body = body
                self.headers = {"Content-Type": content_type}

            def read(self, amount=-1):
                if amount < 0:
                    amount = len(self._body)
                chunk = self._body[:amount]
                self._body = self._body[amount:]
                return chunk

            def close(self):
                return None

        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-3",
        }

        with patch(
            "buzz.dav_protocol._open_upstream_response",
            side_effect=lambda *a, **kw: FakeResponse(
                b"<!DOCTYPE html>bad", "text/html"
            ),
        ), patch("buzz.dav_protocol.time.sleep"), self.assertRaisesRegex(
            ValueError, "non-media content type|markup"
        ):
            open_remote_media(self.state, node, None)

    def test_force_download_media_payload_is_accepted(self):
        self.state.client = self.FakeRD(["https://example.invalid/download"])

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._stream = memoryview(body)
                self.headers = {"Content-Type": content_type}

            def read(self, amount=-1):
                if amount is None or amount < 0:
                    amount = len(self._stream)
                chunk = self._stream[:amount].tobytes()
                self._stream = self._stream[amount:]
                return chunk

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()
                return False

        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-4",
        }

        with patch(
            "buzz.dav_protocol._open_upstream_response",
            return_value=FakeResponse(
                b"\x1a\x45\xdf\xa3media-bytes", "application/force-download"
            ),
        ):
            response, first_chunk = open_remote_media(self.state, node, None)
            self.assertEqual(first_chunk, b"\x1a\x45\xdf\xa3media-bytes")
            response.close()

    def test_force_download_html_payload_is_still_rejected(self):
        self.state.client = self.FakeRD(
            ["https://example.invalid/download/1", "https://example.invalid/download/2"]
        )

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._body = body
                self.headers = {"Content-Type": content_type}

            def read(self, amount=-1):
                if amount < 0:
                    amount = len(self._body)
                chunk = self._body[:amount]
                self._body = self._body[amount:]
                return chunk

            def close(self):
                return None

        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-5",
        }

        with patch(
            "buzz.dav_protocol._open_upstream_response",
            side_effect=lambda *a, **kw: FakeResponse(
                b"<!DOCTYPE html>bad", "application/force-download"
            ),
        ), patch("buzz.dav_protocol.time.sleep"):
            with self.assertRaisesRegex(ValueError, "markup instead of media bytes"):
                open_remote_media(self.state, node, None)

    def test_open_remote_media_does_not_invalidate_url_on_connection_error(self):
        self.state.client = self.FakeRD(["https://example.invalid/cdn"])

        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-conn",
        }

        with patch.object(
            self.state, "invalidate_download_url"
        ) as mock_invalidate, patch(
            "buzz.dav_protocol._open_upstream_response",
            side_effect=OSError("Connection reset by peer"),
        ), patch("buzz.dav_protocol.time.sleep"):
            with self.assertRaisesRegex(ValueError, "failed to connect to upstream"):
                open_remote_media(self.state, node, None)

        self.assertEqual(mock_invalidate.call_count, 0)
        self.assertEqual(len(self.state.client.calls), 1)

    def test_open_remote_media_invalidates_url_on_http_error(self):
        from email.message import Message
        from urllib.error import HTTPError

        self.state.client = self.FakeRD(
            [
                "https://example.invalid/cdn1",
                "https://example.invalid/cdn2",
                "https://example.invalid/cdn3",
            ]
        )

        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-http",
        }

        def http_error(*args, **kwargs):
            raise HTTPError(
                "https://example.invalid/cdn", 503, "boom", hdrs=Message(), fp=None
            )

        with patch.object(
            self.state, "invalidate_download_url"
        ) as mock_invalidate, patch(
            "buzz.dav_protocol._open_upstream_response", side_effect=http_error
        ), patch("buzz.dav_protocol.time.sleep"):
            with self.assertRaisesRegex(ValueError, "upstream returned HTTP 503"):
                open_remote_media(self.state, node, None)

        self.assertEqual(mock_invalidate.call_count, 6)

    def test_retry_sleep_jitter_bounds(self):
        from buzz import dav_protocol

        recorded = []
        with patch("buzz.dav_protocol.time.sleep", side_effect=recorded.append):
            with patch("buzz.dav_protocol.random.random", return_value=0.0):
                dav_protocol._retry_sleep(0)
            with patch("buzz.dav_protocol.random.random", return_value=1.0):
                dav_protocol._retry_sleep(0)
            with patch("buzz.dav_protocol.random.random", return_value=0.0):
                dav_protocol._retry_sleep(5)

        # attempt 0: base=0.5, range [0.375, 0.625]
        self.assertAlmostEqual(recorded[0], 0.5 * 0.75, places=6)
        self.assertAlmostEqual(recorded[1], 0.5 * 1.25, places=6)
        # attempt 5 capped at base=15.0 (0.5 * 2**5 = 16 -> capped)
        self.assertAlmostEqual(recorded[2], 15.0 * 0.75, places=6)

    def test_open_remote_media_caps_concurrent_upstream_connections(self):
        import threading as _threading

        self.state.config = self.state.config.model_copy(
            update={"connection_concurrency": 2}
        )
        self.state.client = self.FakeRD(
            [f"https://example.invalid/cdn/{i}" for i in range(10)]
        )

        node_template = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-cap",
        }

        in_flight = 0
        max_in_flight = 0
        flight_lock = _threading.Lock()

        class FakeResponse:
            def __init__(self):
                self.headers = {"Content-Type": "video/x-matroska"}
                self._body = b"\x1a\x45\xdf\xa3media"

            def read(self, amount=-1):
                if amount is None or amount < 0:
                    amount = len(self._body)
                chunk = self._body[:amount]
                self._body = self._body[amount:]
                return chunk

            def close(self):
                return None

        def fake_urlopen(*args, **kwargs):
            nonlocal in_flight, max_in_flight
            with flight_lock:
                in_flight += 1
                if in_flight > max_in_flight:
                    max_in_flight = in_flight
            try:
                # Hold long enough that concurrent threads pile up against
                # the semaphore cap.
                time.sleep(0.05)
                return FakeResponse()
            finally:
                with flight_lock:
                    in_flight -= 1

        results: list = []
        errors: list[Exception] = []

        def worker():
            try:
                response, _ = open_remote_media(
                    self.state, dict(node_template), None
                )
                # Close immediately so the semaphore is released for the
                # next waiter; we are testing the cap, not stream lifetime.
                response.close()
                results.append(response)
            except Exception as exc:
                errors.append(exc)

        with patch(
            "buzz.dav_protocol._open_upstream_response", side_effect=fake_urlopen
        ):
            threads = [
                _threading.Thread(target=worker) for _ in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertLessEqual(max_in_flight, 2)
        self.assertGreaterEqual(max_in_flight, 1)

    def test_open_remote_media_releases_setup_slot_before_stream_close(self):
        self.state.config = self.state.config.model_copy(
            update={"connection_concurrency": 1}
        )
        self.state.client = self.FakeRD(
            [
                "https://example.invalid/cdn/first",
                "https://example.invalid/cdn/second",
            ]
        )

        class FakeResponse:
            def __init__(self, label: bytes):
                self.headers = {"Content-Type": "video/x-matroska"}
                self._body = b"\x1a\x45\xdf\xa3" + label

            def read(self, amount=-1):
                if amount is None or amount < 0:
                    amount = len(self._body)
                chunk = self._body[:amount]
                self._body = self._body[amount:]
                return chunk

            def close(self):
                return None

        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-release",
        }

        with patch(
            "buzz.dav_protocol._open_upstream_response",
            side_effect=[FakeResponse(b"first"), FakeResponse(b"second")],
        ):
            first_response, first_chunk = open_remote_media(
                self.state, node, None
            )
            second_response, second_chunk = open_remote_media(
                self.state, node, None
            )

        self.assertEqual(first_chunk, b"\x1a\x45\xdf\xa3first")
        self.assertEqual(second_chunk, b"\x1a\x45\xdf\xa3second")
        first_response.close()
        second_response.close()

    def test_open_remote_media_releases_setup_slot_before_retry_sleep(self):
        from buzz import dav_protocol

        self.state.config = self.state.config.model_copy(
            update={"connection_concurrency": 1}
        )
        self.state.client = self.FakeRD(
            [
                "https://example.invalid/cdn/first",
                "https://example.invalid/cdn/second",
            ]
        )

        class FakeResponse:
            def __init__(self):
                self.headers = {"Content-Type": "video/x-matroska"}
                self._body = b"\x1a\x45\xdf\xa3media"

            def read(self, amount=-1):
                if amount is None or amount < 0:
                    amount = len(self._body)
                chunk = self._body[:amount]
                self._body = self._body[amount:]
                return chunk

            def close(self):
                return None

        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-retry-release",
        }
        real_semaphore = dav_protocol._get_upstream_semaphore(1)

        def sleep_asserts_slot_released(_delay):
            self.assertTrue(real_semaphore.acquire(blocking=False))
            real_semaphore.release()

        with patch(
            "buzz.dav_protocol._open_upstream_response",
            side_effect=[
                OSError("Connection reset by peer"),
                FakeResponse(),
            ],
        ), patch(
            "buzz.dav_protocol.time.sleep",
            side_effect=sleep_asserts_slot_released,
        ):
            response, first_chunk = open_remote_media(self.state, node, None)

        self.assertEqual(first_chunk, b"\x1a\x45\xdf\xa3media")
        response.close()

    def test_open_remote_media_short_circuits_on_hoster_unavailable(self):
        from buzz.core.state import HosterUnavailableError

        class HosterDownRD:
            def __init__(self):
                self.calls = []
                self.unrestrict = self
                self.torrents = None

            def link(self, url):
                self.calls.append(url)
                return DavAppTests.FakeRDResponse(
                    {"error": "hoster_unavailable"}
                )

        self.state.client = HosterDownRD()
        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-hoster",
        }
        with patch("buzz.dav_protocol.time.sleep") as mock_sleep:
            with self.assertRaises(HosterUnavailableError):
                open_remote_media(self.state, node, None)
        # No retry sleep, exactly one API hit.
        self.assertEqual(mock_sleep.call_count, 0)
        self.assertEqual(len(self.state.client.calls), 1)

    def test_dav_get_returns_503_with_retry_after_on_hoster_unavailable(self):
        class HosterDownRD:
            def __init__(self):
                self.calls = []
                self.unrestrict = self
                self.torrents = None

            def link(self, url):
                self.calls.append(url)
                return DavAppTests.FakeRDResponse(
                    {"error": "hoster_unavailable"}
                )

        self.state.client = HosterDownRD()
        self.state.snapshot["files"][
            "movies/Little Shop [1986] + Extras/Little Shop of Horrors (1986).mkv"
        ] = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-hoster-503",
        }
        response = self.client.get(
            "/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/"
            "Little%20Shop%20of%20Horrors%20%281986%29.mkv"
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.headers["retry-after"],
            str(self.state.config.rd_hoster_failure_cache_secs),
        )
        # Second request inside TTL must not hit RD again.
        response2 = self.client.get(
            "/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/"
            "Little%20Shop%20of%20Horrors%20%281986%29.mkv"
        )
        self.assertEqual(response2.status_code, 503)
        self.assertEqual(len(self.state.client.calls), 1)

    def test_dav_get_logs_hoster_unavailable_once_per_cache_ttl(self):
        class HosterDownRD:
            def __init__(self):
                self.calls = []
                self.unrestrict = self
                self.torrents = None

            def link(self, url):
                self.calls.append(url)
                return DavAppTests.FakeRDResponse(
                    {"error": "hoster_unavailable"}
                )

        self.state.client = HosterDownRD()
        self.state.snapshot["files"][
            "movies/Little Shop [1986] + Extras/Little Shop of Horrors (1986).mkv"
        ] = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-hoster-log-once",
        }

        with patch("buzz.dav_app.record_event") as mock_record_event:
            for _ in range(2):
                response = self.client.get(
                    "/dav/movies/Little%20Shop%20%5B1986%5D%20%2B%20Extras/"
                    "Little%20Shop%20of%20Horrors%20%281986%29.mkv"
                )
                self.assertEqual(response.status_code, 503)

        self.assertEqual(len(self.state.client.calls), 1)
        hoster_events = [
            call
            for call in mock_record_event.call_args_list
            if call.kwargs.get("event") == "rd_hoster_unavailable"
        ]
        self.assertEqual(len(hoster_events), 1)

    def test_open_remote_media_fails_when_setup_slot_times_out(self):
        class BusySemaphore:
            def acquire(self, timeout=None):
                return False

        node = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-busy",
        }

        self.state.client = self.FakeRD(["https://example.invalid/cdn"])
        with patch(
            "buzz.dav_protocol._get_upstream_semaphore",
            return_value=BusySemaphore(),
        ), patch("buzz.dav_protocol.time.sleep"):
            with self.assertRaisesRegex(ValueError, "connection limit reached"):
                open_remote_media(self.state, node, None)

    def test_languages_refreshing_flag_set_while_fetching(self):
        config = self._config_with_credentials(self.tmpdir.name)
        with (
            patch("buzz.dav_app.RD", return_value=self.FakeRD()),
            patch("buzz.dav_app._fetch_opensubtitles_languages", return_value=[("de", "German")]),
        ):
            app = DavApp(config)
            self.assertFalse(app.languages_refreshing)
            started = app.trigger_language_refresh(force=True)
            self.assertTrue(started)
            self.assertTrue(app.languages_refreshing)
            # wait for background thread to finish
            for _ in range(40):
                if not app.languages_refreshing:
                    break
                time.sleep(0.05)
            self.assertFalse(app.languages_refreshing)
            self.assertEqual(app.opensubtitles_languages, [("de", "German")])

    def test_languages_refreshing_flag_cleared_on_empty_result(self):
        config = self._config_with_credentials(self.tmpdir.name)

        def _slow_empty_fetch(*_args):
            time.sleep(0.05)
            return []

        with (
            patch("buzz.dav_app.RD", return_value=self.FakeRD()),
            patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                side_effect=_slow_empty_fetch,
            ),
        ):
            app = DavApp(config)
            started = app.trigger_language_refresh(force=True)
            self.assertTrue(started)
            self.assertTrue(app.languages_refreshing)
            for _ in range(40):
                if not app.languages_refreshing:
                    break
                time.sleep(0.05)
            self.assertFalse(app.languages_refreshing)

    def test_refresh_logs_start_and_finish_events(self):
        from buzz.core.events import registry

        config = self._config_with_credentials(self.tmpdir.name)
        with (
            patch("buzz.dav_app.RD", return_value=self.FakeRD()),
            patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                return_value=[("it", "Italian")],
            ),
        ):
            app = DavApp(config)
            before = len(registry.events)
            app.trigger_language_refresh(force=True)
            for _ in range(40):
                if not app.languages_refreshing:
                    break
                time.sleep(0.05)
            after = len(registry.events)
            self.assertGreater(after, before)
            messages = [e["message"] for e in registry.events]
            self.assertIn("openSubtitles language refresh started", messages)
            self.assertIn("openSubtitles language refresh finished", messages)


class FetchOpenSubtitlesLanguagesTests(unittest.TestCase):
    def test_fetch_sends_api_key_header(self):
        from buzz.dav_app import _fetch_opensubtitles_languages

        captured = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"language_code": "en", "language_name": "English"}]}

        def fake_get(url, timeout, headers):
            captured["url"] = url
            captured["headers"] = headers
            return _Resp()

        with patch("httpx.get", fake_get):
            result = _fetch_opensubtitles_languages("secret-key")

        self.assertEqual(result, [("en", "English")])
        self.assertEqual(captured["headers"]["Api-Key"], "secret-key")
        self.assertIn("User-Agent", captured["headers"])


class DavRemoteStreamingTests(unittest.TestCase):
    """Tests for direct remote media streaming."""

    BODY_SIZE = 512 * 1024

    class FakeResponse:
        """Streaming response backed by a memoryview; supports read() and close()."""

        def __init__(self, body: bytes, content_type: str = "video/x-matroska"):
            self._stream = memoryview(body)
            self.headers = {"Content-Type": content_type}
            self.closed = False

        def read(self, amount=-1):
            if amount is None or amount < 0:
                amount = len(self._stream)
            chunk = self._stream[:amount].tobytes()
            self._stream = self._stream[amount:]
            return chunk

        def close(self):
            self.closed = True

    def _make_dav_app(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpdir)
        state_dir = Path(tmpdir)
        snapshot = {
            "dirs": ["", "movies", "movies/Test Film"],
            "files": {
                "movies/Test Film/film.mkv": {
                    "type": "remote",
                    "size": str(self.BODY_SIZE),
                    "source_url": "https://example.invalid/source",
                    "mime_type": "video/x-matroska",
                    "modified": "2026-01-01T00:00:00Z",
                    "etag": "etag-buf-1",
                },
            },
        }
        (state_dir / "library_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8"
        )
        config = Config(
            token="token",
            provider_poll_interval_secs=10,
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
            rd_update_delay_secs=0,
        )
        rd_patcher = patch("buzz.dav_app.RD", return_value=DavAppTests.FakeRD())
        languages_patcher = patch(
            "buzz.dav_app._fetch_opensubtitles_languages",
            return_value=[],
        )
        self.addCleanup(rd_patcher.stop)
        self.addCleanup(languages_patcher.stop)
        rd_patcher.start()
        languages_patcher.start()
        return DavApp(config)

    def _get_serve_dav(self, dav_app):
        """Return the serve_dav route endpoint directly for generator-level testing."""
        for route in dav_app.app.routes:
            if (
                getattr(route, "path", None) == "/dav/{path:path}"
                and "GET" in getattr(route, "methods", set())
            ):
                return route.endpoint
        raise AssertionError("serve_dav GET route not found")

    def _mock_request(self, url_path: str):
        req = MagicMock()
        req.method = "GET"
        req.url.path = url_path
        req.headers.get.return_value = None
        return req

    def test_remote_streaming_all_bytes_received(self):
        dav_app = self._make_dav_app()
        payload = bytes(range(256)) * (self.BODY_SIZE // 256)
        fake_response = self.FakeResponse(payload)

        received = b"".join(dav_app._stream_remote(fake_response, b""))

        self.assertEqual(received, payload)
        self.assertTrue(fake_response.closed)

    def test_remote_streaming_yields_first_chunk_before_reading_response(self):
        dav_app = self._make_dav_app()
        first_chunk = b"first"
        rest = b"second"
        fake_response = self.FakeResponse(rest)

        received = b"".join(dav_app._stream_remote(fake_response, first_chunk))

        self.assertEqual(received, first_chunk + rest)
        self.assertTrue(fake_response.closed)

    def test_remote_streaming_closes_response_after_early_close(self):
        dav_app = self._make_dav_app()
        payload = bytes(range(256)) * (self.BODY_SIZE // 256)
        fake_response = self.FakeResponse(payload)

        gen = dav_app._stream_remote(fake_response, b"")
        next(gen)
        gen.close()

        self.assertTrue(fake_response.closed)

    def test_remote_get_head_and_range_keep_size_headers(self):
        dav_app = self._make_dav_app()
        node = dav_app.state.lookup("movies/Test Film/film.mkv")
        if node is None:
            self.fail("Expected snapshot node for streaming header test")
        captured_headers: list[dict] = []

        def fake_streaming_response(_content, **kwargs):
            captured_headers.append(kwargs["headers"])
            return MagicMock(status_code=kwargs["status_code"])

        with patch(
            "buzz.dav_app.open_remote_media",
            return_value=(self.FakeResponse(b""), b""),
        ), patch(
            "buzz.dav_app.StreamingResponse",
            side_effect=fake_streaming_response,
        ):
            dav_app._dav_remote_response(
                node, True, None, "movies/Test Film/film.mkv"
            )
            dav_app._dav_remote_response(
                node, True, "bytes=0-0", "movies/Test Film/film.mkv"
            )
        head_response = dav_app._dav_remote_response(
            node, False, None, "movies/Test Film/film.mkv"
        )
        size = int(node["size"])

        self.assertEqual(
            captured_headers[0]["Content-Length"],
            str(size),
        )
        self.assertEqual(
            head_response.headers["content-length"],
            str(size),
        )
        self.assertEqual(captured_headers[1]["Content-Length"], "1")
        self.assertEqual(
            captured_headers[1]["Content-Range"],
            f"bytes 0-0/{size}",
        )

    def _start_patches(self, fake_response):
        """Start patches and return the captured raw sync generator."""
        captured = {}

        def fake_streaming_response(content, **kwargs):
            captured["gen"] = content
            return MagicMock(status_code=200)

        orm_patch = patch("buzz.dav_app.open_remote_media", return_value=(fake_response, b""))
        sr_patch = patch("buzz.dav_app.StreamingResponse", side_effect=fake_streaming_response)
        orm_patch.start()
        sr_patch.start()
        self.addCleanup(orm_patch.stop)
        self.addCleanup(sr_patch.stop)
        return captured

    def test_remote_streaming_through_route_closes_after_completion(self):
        dav_app = self._make_dav_app()
        payload = bytes(range(256)) * (self.BODY_SIZE // 256)
        fake_response = self.FakeResponse(payload)
        mock_req = self._mock_request("/dav/movies/Test%20Film/film.mkv")

        captured = self._start_patches(fake_response)
        serve_dav = self._get_serve_dav(dav_app)
        serve_dav(path="movies/Test%20Film/film.mkv", request=mock_req)
        gen = captured["gen"]

        received = b"".join(gen)

        self.assertEqual(received, payload)
        self.assertTrue(fake_response.closed)


class ConfigUITests(unittest.TestCase):
    def test_deep_merge_nested_overrides(self):
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        overrides = {"b": {"c": 99}}
        result = deep_merge(base, overrides)
        self.assertEqual(result, {"a": 1, "b": {"c": 99, "d": 3}})

    def test_deep_merge_empty_overrides(self):
        base = {"a": 1, "b": {"c": 2}}
        result = deep_merge(base, {})
        self.assertEqual(result, base)

    def test_deep_merge_additive_keys(self):
        base = {"a": 1}
        overrides = {"b": 2}
        result = deep_merge(base, overrides)
        self.assertEqual(result, {"a": 1, "b": 2})

    def test_deep_merge_replaces_non_dict(self):
        base = {"a": {"b": 1}}
        overrides = {"a": 2}
        result = deep_merge(base, overrides)
        self.assertEqual(result, {"a": 2})

    def test_mask_secrets(self):
        d = {
            "provider": {"token": "secret123"},
            "subtitles": {
                "opensubtitles": {
                    "api_key": "ak",
                    "username": "user",
                    "password": "pass",
                    "other": "ok",
                }
            },
            "public": "visible",
        }
        result = mask_secrets(d)
        self.assertEqual(result["provider"]["token"], "***")
        self.assertEqual(result["subtitles"]["opensubtitles"]["api_key"], "***")
        self.assertEqual(result["subtitles"]["opensubtitles"]["username"], "***")
        self.assertEqual(result["subtitles"]["opensubtitles"]["password"], "***")
        self.assertEqual(result["subtitles"]["opensubtitles"]["other"], "ok")
        self.assertEqual(result["public"], "visible")

    def test_config_load_without_overrides(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("provider:\n  token: testtoken\n")
            base_path = f.name
        try:
            config = Config.load(base_path)
            self.assertEqual(config.token, "testtoken")
            self.assertEqual(config.provider_poll_interval_secs, 10)
            self.assertEqual(config.bind, "0.0.0.0")
        finally:
            os.unlink(base_path)

    def test_presentation_config_load_uses_buzz_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                (
                    "provider:\n  token: testtoken\n"
                    f"state_dir: {tmpdir}/shared-state\n"
                ),
                encoding="utf-8",
            )

            config = CuratorConfig.load(str(base_path))

            self.assertEqual(
                config.state_dir,
                Path(tmpdir) / "shared-state",
            )

    def test_curator_config_loads_library_map_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                (
                    "provider:\n  token: testtoken\n"
                    f"state_dir: {tmpdir}\n"
                    "media_server:\n"
                    "  library_map:\n"
                    "    movies: Movies\n"
                    "    shows: Shows\n"
                    "    anime: Anime\n"
                ),
                encoding="utf-8",
            )

            config = CuratorConfig.load(str(base_path))

            self.assertEqual(
                config.jellyfin_library_map,
                {"movies": "Movies", "shows": "Shows", "anime": "Anime"},
            )

    def test_curator_config_loads_media_server_settings_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                (
                    "provider:\n  token: testtoken\n"
                    f"state_dir: {tmpdir}\n"
                    "media_server:\n"
                    "  kind: plex\n"
                    "  trigger_lib_scan: true\n"
                    "  jellyfin:\n"
                    "    url: http://jellyfin.local:8096/\n"
                    "    api_key: jf-secret\n"
                    "    scan_task_id: scan-task\n"
                    "  plex:\n"
                    "    url: http://plex.local:32400/\n"
                    "    token: plex-secret\n"
                ),
                encoding="utf-8",
            )

            config = CuratorConfig.load(str(base_path))

            self.assertEqual(config.media_server_kind, "plex")
            self.assertTrue(config.trigger_lib_scan)
            self.assertEqual(config.jellyfin_url, "http://jellyfin.local:8096")
            self.assertEqual(config.jellyfin_api_key, "jf-secret")
            self.assertEqual(config.jellyfin_scan_task_id, "scan-task")
            self.assertEqual(config.plex_url, "http://plex.local:32400")
            self.assertEqual(config.plex_token, "plex-secret")

    def test_curator_config_library_map_default_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                f"provider:\n  token: testtoken\nstate_dir: {tmpdir}\n",
                encoding="utf-8",
            )

            config = CuratorConfig.load(str(base_path))

            self.assertEqual(config.jellyfin_library_map, {})

    def test_dav_config_round_trips_library_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                (
                    "provider:\n  token: testtoken\n"
                    f"state_dir: {tmpdir}\n"
                    "media_server:\n"
                    "  library_map:\n"
                    "    shows: Shows\n"
                ),
                encoding="utf-8",
            )

            config = Config.load(str(base_path))

            self.assertEqual(config.library_map, {"shows": "Shows"})
            nested = to_nested_dict(config)
            self.assertEqual(
                nested["media_server"]["library_map"], {"shows": "Shows"}
            )

    def test_dav_config_round_trips_media_server_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                (
                    "provider:\n  token: testtoken\n"
                    f"state_dir: {tmpdir}\n"
                    "media_server:\n"
                    "  kind: jellyfin\n"
                    "  trigger_lib_scan: true\n"
                    "  jellyfin:\n"
                    "    url: http://jellyfin.local:8096\n"
                    "    api_key: jf-secret\n"
                    "    scan_task_id: scan-task\n"
                    "  plex:\n"
                    "    url: http://plex.local:32400\n"
                    "    token: plex-secret\n"
                    "  library_map:\n"
                    "    shows: Shows\n"
                ),
                encoding="utf-8",
            )

            config = Config.load(str(base_path))
            nested = to_nested_dict(config)

            self.assertEqual(nested["media_server"]["kind"], "jellyfin")
            self.assertTrue(nested["media_server"]["trigger_lib_scan"])
            self.assertEqual(
                nested["media_server"]["jellyfin"],
                {
                    "url": "http://jellyfin.local:8096",
                    "api_key": "jf-secret",
                    "scan_task_id": "scan-task",
                },
            )
            self.assertEqual(
                nested["media_server"]["plex"],
                {
                    "url": "http://plex.local:32400",
                    "token": "plex-secret",
                },
            )

    def test_dav_config_renames_connection_concurrency_to_connection_concurrency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                "provider:\n  token: testtoken\n  connection_concurrency: 8\n",
                encoding="utf-8",
            )

            config = Config.load(str(base_path))
            self.assertEqual(config.connection_concurrency, 8)

            nested = to_nested_dict(config)
            self.assertEqual(nested["provider"]["connection_concurrency"], 8)
            self.assertNotIn("connection_concurrency", nested["server"])

    def test_dav_config_ignores_old_connection_concurrency_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                "provider:\n  token: testtoken\nserver:\n  connection_concurrency: 8\n",
                encoding="utf-8",
            )

            config = Config.load(str(base_path))
            # Should fall back to default 4 because the old key is ignored
            self.assertEqual(config.connection_concurrency, 4)

            nested = to_nested_dict(config)
            self.assertEqual(nested["provider"]["connection_concurrency"], 4)
            self.assertNotIn("connection_concurrency", nested["server"])

    def test_config_modal_renders_provider_and_media_server_settings(self):
        template = Path("buzz/pyview_templates/config_live.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('name="provider.connection_concurrency"', template)
        self.assertIn('name="media_server.kind"', template)
        self.assertIn('name="media_server.trigger_lib_scan"', template)
        self.assertIn('name="media_server.jellyfin.url"', template)
        self.assertIn('name="media_server.jellyfin.api_key"', template)
        self.assertIn('name="media_server.jellyfin.scan_task_id"', template)
        self.assertIn('name="media_server.plex.url"', template)
        self.assertIn('name="media_server.plex.token"', template)

    def test_config_load_with_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            overrides_path = Path(tmpdir) / "buzz.overrides.yml"
            base_path.write_text(
                f"provider:\n  token: testtoken\nserver:\n  port: 9999\nstate_dir: {tmpdir}\n", encoding="utf-8"
            )
            overrides_path.write_text(
                "server:\n  port: 8888\nprovider:\n  poll_interval_secs: 60\n", encoding="utf-8"
            )
            config = Config.load(str(base_path))
            self.assertEqual(config.token, "testtoken")
            self.assertEqual(config.port, 8888)
            self.assertEqual(config.provider_poll_interval_secs, 60)

    def test_get_api_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                (
                    "provider:\n  token: sekrit\n"
                    "server:\n  port: 9999\n"
                    f"state_dir: {tmpdir}\n"
                ),
                encoding="utf-8",
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.RD", return_value=DavAppTests.FakeRD())
            languages_patcher = patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                return_value=[],
            )
            rd_patcher.start()
            languages_patcher.start()
            self.addCleanup(rd_patcher.stop)
            self.addCleanup(languages_patcher.stop)
            app = DavApp(config)
            client = TestClient(app.app)
            resp = client.get("/api/config")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["effective"]["provider"]["token"], "***")
            self.assertEqual(data["effective"]["server"]["port"], 9999)

    def test_post_api_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            overrides_path = Path(tmpdir) / "buzz.overrides.yml"
            base_path.write_text(
                f"provider:\n  token: testtoken\nserver:\n  port: 9999\nstate_dir: {tmpdir}\n", encoding="utf-8"
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.RD", return_value=DavAppTests.FakeRD())
            languages_patcher = patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                return_value=[],
            )
            rd_patcher.start()
            languages_patcher.start()
            self.addCleanup(rd_patcher.stop)
            self.addCleanup(languages_patcher.stop)
            app = DavApp(config)
            client = TestClient(app.app)
            resp = client.post(
                "/api/config",
                json={"overrides": {"server": {"port": 7777}}},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "saved")
            self.assertTrue(resp.json()["restart_required"])
            self.assertEqual(resp.json()["restart_required_fields"], ["server.port"])
            written = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
            self.assertEqual(written["server"]["port"], 7777)

    def test_post_api_config_hot_reloads_verbose(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            overrides_path = Path(tmpdir) / "buzz.overrides.yml"
            base_path.write_text(
                f"provider:\n  token: testtoken\nstate_dir: {tmpdir}\n",
                encoding="utf-8",
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.RD", return_value=DavAppTests.FakeRD())
            languages_patcher = patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                return_value=[],
            )
            rd_patcher.start()
            languages_patcher.start()
            self.addCleanup(rd_patcher.stop)
            self.addCleanup(languages_patcher.stop)
            app = DavApp(config)
            client = TestClient(app.app)
            resp = client.post(
                "/api/config",
                json={"overrides": {"logging": {"verbose": True}}},
            )

            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.json()["restart_required"])
            self.assertEqual(resp.json()["hot_reloaded_fields"], ["logging.verbose"])
            self.assertTrue(app.config.verbose)
            written = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
            self.assertTrue(written["logging"]["verbose"])

    def test_restore_defaults_removes_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            overrides_path = Path(tmpdir) / "buzz.overrides.yml"
            base_path.write_text(
                f"provider:\n  token: testtoken\nstate_dir: {tmpdir}\n",
                encoding="utf-8",
            )
            overrides_path.write_text(
                "logging:\n  verbose: true\n",
                encoding="utf-8",
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.RD", return_value=DavAppTests.FakeRD())
            languages_patcher = patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                return_value=[],
            )
            rd_patcher.start()
            languages_patcher.start()
            self.addCleanup(rd_patcher.stop)
            self.addCleanup(languages_patcher.stop)
            app = DavApp(config)
            client = TestClient(app.app)

            resp = client.post("/api/config/restore-defaults")

            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "saved")
            self.assertFalse(overrides_path.exists())

    def test_post_api_config_strips_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            overrides_path = Path(tmpdir) / "buzz.overrides.yml"
            base_path.write_text(
                f"provider:\n  token: testtoken\nstate_dir: {tmpdir}\n", encoding="utf-8"
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.RD", return_value=DavAppTests.FakeRD())
            languages_patcher = patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                return_value=[],
            )
            rd_patcher.start()
            languages_patcher.start()
            self.addCleanup(rd_patcher.stop)
            self.addCleanup(languages_patcher.stop)
            app = DavApp(config)
            client = TestClient(app.app)
            resp = client.post(
                "/api/config",
                json={
                    "overrides": {
                        "provider": {"token": "hacked"},
                        "subtitles": {
                            "opensubtitles": {
                                "api_key": "hacked",
                                "username": "hacked",
                                "password": "hacked",
                            }
                        },
                        "server": {"port": 7777},
                    }
                },
            )
            self.assertEqual(resp.status_code, 200)
            written = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
            self.assertNotIn("provider", written)
            self.assertNotIn("subtitles", written)
            self.assertEqual(written["server"]["port"], 7777)

class UIRedirectAppTests(unittest.TestCase):
    """Tests for the HTTP-side UI redirect app used in TLS mode."""

    def _client(self, https_port: int = 9443):
        from buzz.dav_app import build_ui_https_redirect_app
        app = build_ui_https_redirect_app(https_port)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_root_redirects(self):
        client = self._client()
        resp = client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("https://testserver:9443/", resp.headers["location"])

    def test_ui_pages_redirect(self):
        client = self._client()
        for path in ("/cache", "/archive", "/logs", "/config"):
            resp = client.get(path, follow_redirects=False)
            self.assertEqual(resp.status_code, 302, path)
            self.assertIn(
                f"https://testserver:9443{path}",
                resp.headers["location"],
            )

    def test_static_assets_redirect(self):
        client = self._client()
        resp = client.get("/static/buzz.css", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(
            "https://testserver:9443/static/buzz.css",
            resp.headers["location"],
        )

    def test_pyview_assets_redirect(self):
        client = self._client()
        resp = client.get("/pyview/assets/app.js", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(
            "https://testserver:9443/pyview/assets/app.js",
            resp.headers["location"],
        )

    def test_query_preserved_on_redirect(self):
        client = self._client()
        resp = client.get("/cache?filter=x", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(
            "https://testserver:9443/cache?filter=x",
            resp.headers["location"],
        )

    def test_api_path_returns_404(self):
        client = self._client()
        resp = client.get("/api/config", follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_dav_path_returns_404(self):
        client = self._client()
        resp = client.request("PROPFIND", "/dav/movies", follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_dav_path_delegates_to_owner_when_present(self):
        from buzz.dav_app import build_ui_https_redirect_app

        owner_app = FastAPI()

        @owner_app.api_route("/dav/{path:path}", methods=["PROPFIND"])
        async def propfind(path: str):
            return Response(status_code=207)

        class Owner:
            app = owner_app

        client = TestClient(build_ui_https_redirect_app(9443, Owner()))
        self.addCleanup(client.close)
        resp = client.request("PROPFIND", "/dav/", follow_redirects=False)

        self.assertEqual(resp.status_code, 207)

    def test_curator_notify_delegates_to_owner_when_present(self):
        from buzz.dav_app import build_ui_https_redirect_app

        owner_app = FastAPI()

        @owner_app.post("/api/ui/notify")
        async def notify():
            return {"status": "ok"}

        class Owner:
            app = owner_app

        client = TestClient(build_ui_https_redirect_app(9443, Owner()))
        self.addCleanup(client.close)
        resp = client.post("/api/ui/notify", follow_redirects=False)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_rebuild_path_returns_404(self):
        client = self._client()
        resp = client.get("/rebuild", follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_healthz_returns_ok(self):
        client = self._client()
        resp = client.get("/healthz", follow_redirects=False)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_readyz_returns_ready(self):
        client = self._client()
        resp = client.get("/readyz", follow_redirects=False)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ready"})

    def test_readyz_uses_dav_owner_when_present(self):
        from buzz.dav_app import build_ui_https_redirect_app

        owner_app = FastAPI()

        @owner_app.get("/readyz")
        async def readyz():
            return JSONResponse(
                status_code=503,
                content={"status": "starting"},
            )

        class Owner:
            app = owner_app

        client = TestClient(build_ui_https_redirect_app(9443, Owner()))
        self.addCleanup(client.close)
        resp = client.get("/readyz", follow_redirects=False)

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"status": "starting"})

    def test_head_method_redirects(self):
        client = self._client()
        resp = client.head("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def test_post_to_ui_path_uses_307(self):
        client = self._client()
        resp = client.post("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)

    def test_custom_https_port(self):
        client = self._client(https_port=8443)
        resp = client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("https://testserver:8443/", resp.headers["location"])


class UIPathMatcherTests(unittest.TestCase):
    """Tests for the is_ui_redirect_path helper."""

    def test_exact_ui_paths(self):
        from buzz.dav_app import is_ui_redirect_path
        for path in ("/", "/cache", "/archive", "/logs", "/config"):
            self.assertTrue(is_ui_redirect_path(path), path)

    def test_prefix_ui_paths(self):
        from buzz.dav_app import is_ui_redirect_path
        self.assertTrue(is_ui_redirect_path("/static/buzz.css"))
        self.assertTrue(is_ui_redirect_path("/pyview/live_socket.js"))

    def test_non_ui_paths(self):
        from buzz.dav_app import is_ui_redirect_path
        for path in ("/api/config", "/dav/movies", "/rebuild", "/favicon.ico"):
            self.assertFalse(is_ui_redirect_path(path), path)


class HttpTlsPassthroughMatcherTests(unittest.TestCase):
    """Tests for HTTP-side TLS passthrough routing."""

    def test_exact_passthrough_paths(self):
        from buzz.dav_app import is_http_tls_passthrough_path

        for path in ("/api/ui/notify", "/healthz", "/readyz", "/dav"):
            self.assertTrue(is_http_tls_passthrough_path(path), path)

    def test_prefix_passthrough_paths(self):
        from buzz.dav_app import is_http_tls_passthrough_path

        self.assertTrue(is_http_tls_passthrough_path("/dav/"))
        self.assertTrue(is_http_tls_passthrough_path("/dav/movies"))

    def test_non_passthrough_paths(self):
        from buzz.dav_app import is_http_tls_passthrough_path

        for path in ("/", "/cache", "/api/config", "/rebuild", "/static/x"):
            self.assertFalse(is_http_tls_passthrough_path(path), path)


class TlsCertificateTests(unittest.TestCase):
    """Tests for self-signed TLS certificate generation."""

    def test_config_defaults_enable_tls_paths(self):
        config = Config._from_merged_dict({"provider": {"token": "token"}})

        self.assertEqual(config.tls.cert_path, DEFAULT_TLS_CERT_PATH)
        self.assertEqual(config.tls.key_path, DEFAULT_TLS_KEY_PATH)

    def test_empty_tls_paths_opt_out(self):
        config = Config._from_merged_dict(
            {
                "provider": {"token": "token"},
                "tls": {"cert_path": "", "key_path": ""},
            }
        )

        self.assertEqual(config.tls.cert_path, "")
        self.assertEqual(config.tls.key_path, "")

    def test_generates_default_paths_relative_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = ensure_tls_certificate(cwd=cwd)

            self.assertTrue(result.generated)
            self.assertEqual(result.cert_path, cwd / "data/tls/buzz.crt")
            self.assertEqual(result.key_path, cwd / "data/tls/buzz.key")
            self.assertTrue(result.cert_path.exists())
            self.assertTrue(result.key_path.exists())
            self.assertRegex(
                result.fingerprint,
                r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$",
            )

    def test_keeps_valid_existing_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            first = ensure_tls_certificate(cwd=cwd)
            second = ensure_tls_certificate(cwd=cwd)

            self.assertFalse(second.generated)
            self.assertEqual(second.fingerprint, first.fingerprint)

    def test_renews_expiring_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            first = ensure_tls_certificate(valid_days=1, cwd=cwd)
            second = ensure_tls_certificate(cwd=cwd)

            self.assertTrue(second.generated)
            self.assertNotEqual(second.fingerprint, first.fingerprint)

    def test_renews_invalid_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cert_path = cwd / "data/tls/buzz.crt"
            key_path = cwd / "data/tls/buzz.key"
            cert_path.parent.mkdir(parents=True)
            cert_path.write_text("not a cert", encoding="utf-8")
            key_path.write_text("not a key", encoding="utf-8")

            result = ensure_tls_certificate(cwd=cwd)

            self.assertTrue(result.generated)
            self.assertIn(
                "BEGIN CERTIFICATE",
                cert_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "BEGIN RSA PRIVATE KEY",
                key_path.read_text(encoding="utf-8"),
            )

    def test_writes_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = ensure_tls_certificate(cwd=cwd)

            self.assertEqual(result.cert_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result.key_path.stat().st_mode & 0o777, 0o600)


class TlsMaintenanceTests(unittest.TestCase):
    """Tests for buzz-dav TLS renewal maintenance."""

    def test_renewal_stops_servers(self):
        from buzz.dav_app import _maintain_tls_certificate

        class Server:
            should_exit = False

        https_server = Server()
        http_server = Server()

        async def run_check():
            with patch("buzz.dav_app.asyncio.sleep", return_value=None):
                with patch("buzz.dav_app.ensure_tls_certificate") as mock_ensure:
                    mock_ensure.return_value.generated = True
                    await _maintain_tls_certificate(
                        "cert.pem",
                        "key.pem",
                        (https_server, http_server),
                        check_interval_secs=1,
                    )

        asyncio.run(run_check())

        self.assertTrue(https_server.should_exit)
        self.assertTrue(http_server.should_exit)

if __name__ == "__main__":
    unittest.main()
