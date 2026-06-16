import asyncio
import io
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import yaml
import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from buzz.core.state import (
    BackgroundTask,
    BackgroundTaskPool,
    BuzzState,
    LibraryBuilder,
    Poller,
    canonical_snapshot,
    dav_rel_path,
    normalize_posix_path,
)
from buzz.core import db
from buzz.core.events import registry
from buzz.core.providers import (
    ProviderDeleteError,
    ProviderFile,
    ProviderStreamError,
    ProviderTorrentDetail,
    ProviderTorrentSummary,
)
from buzz.providers import RealDebridProviderClient, TorBoxProviderClient
from buzz.core.tls import ensure_tls_certificate
from buzz.dav_app import DavApp
from buzz.dav_app import UvicornReadyzAccessFilter
from buzz.dav_app import install_uvicorn_readyz_access_filter
from buzz.dav_protocol import open_remote_media, propfind_body
from buzz.ui_live import ArchiveLiveView, CacheLiveView, ThreadsLiveView
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


class UvicornReadyzAccessFilterTests(unittest.TestCase):
    def _record(self, method: str, path: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='%s - "%s %s HTTP/%s" %s',
            args=("127.0.0.1:12345", method, path, "1.1", 200),
            exc_info=None,
        )

    def test_filter_suppresses_readyz_get_access_logs(self):
        filter_ = UvicornReadyzAccessFilter()

        self.assertFalse(filter_.filter(self._record("GET", "/readyz")))

    def test_filter_keeps_other_access_logs(self):
        filter_ = UvicornReadyzAccessFilter()

        self.assertTrue(filter_.filter(self._record("GET", "/healthz")))
        self.assertTrue(filter_.filter(self._record("POST", "/readyz")))

    def test_install_filter_is_idempotent(self):
        access_logger = logging.getLogger("uvicorn.access")
        original_filters = list(access_logger.filters)
        self.addCleanup(setattr, access_logger, "filters", original_filters)

        access_logger.filters = [
            filter_
            for filter_ in access_logger.filters
            if not isinstance(filter_, UvicornReadyzAccessFilter)
        ]

        install_uvicorn_readyz_access_filter()
        install_uvicorn_readyz_access_filter()

        installed = [
            filter_
            for filter_ in access_logger.filters
            if isinstance(filter_, UvicornReadyzAccessFilter)
        ]
        self.assertEqual(len(installed), 1)


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
                    "filename": "Spaceship.Adventure.1999.mkv",
                    "original_filename": "Spaceship Adventure 1999",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Spaceship.Adventure.1999.mkv",
                            "bytes": 123,
                            "selected": 1,
                        }
                    ],
                }
            ]
        )
        self.assertIn(
            "movies/Spaceship Adventure 1999/Spaceship.Adventure.1999.mkv",
            snapshot["files"],
        )
        self.assertIn(
            "__all__/Spaceship Adventure 1999/Spaceship.Adventure.1999.mkv",
            snapshot["files"],
        )
        self.assertEqual(changed, ["movies/Spaceship Adventure 1999"])

    def test_show_torrent_routed_to_shows(self):
        snapshot, _ = self.builder.build(
            [
                {
                    "id": "SHOW1",
                    "status": "downloaded",
                    "filename": "Wacky Critters",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {
                            "id": 1,
                            "path": "/Wacky.Critters.S01E01.mkv",
                            "bytes": 456,
                            "selected": 1,
                        }
                    ],
                }
            ]
        )
        self.assertIn(
            "shows/Wacky Critters/Wacky.Critters.S01E01.mkv", snapshot["files"]
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

    def test_file_modified_uses_torrent_added_and_is_stable_across_rebuilds(self):
        # Regression: previously every rebuild stamped each file with
        # utc_now_iso(), making rclone surface a fresh mtime to Jellyfin and
        # causing the "File changed, pruning extracted data" storm.
        infos = [
            {
                "id": "ABC123",
                "status": "downloaded",
                "filename": "Movie.mkv",
                "added": "2024-01-15T12:34:56.000Z",
                "links": ["https://example.invalid/file"],
                "files": [
                    {"id": 1, "path": "/Movie.mkv", "bytes": 123, "selected": 1}
                ],
            },
            {
                "id": "BROKEN1",
                "status": "error",
                "filename": "Broken Torrent",
                "added": "2024-02-20T08:00:00.000Z",
                "links": [],
                "files": [
                    {"id": 1, "path": "/Broken.Movie.mkv", "bytes": 42, "selected": 1}
                ],
            },
        ]

        first, _ = self.builder.build(infos)
        second, _ = self.builder.build(infos)

        movie_path = "movies/Movie.mkv/Movie.mkv"
        # Canonicalized: fractional seconds dropped, trailing Z preserved.
        self.assertEqual(
            first["files"][movie_path]["modified"], "2024-01-15T12:34:56Z"
        )
        self.assertEqual(
            first["files"][movie_path]["modified"],
            second["files"][movie_path]["modified"],
        )

        unplayable_path = "__unplayable__/Broken Torrent/Broken.Movie.mkv"
        self.assertEqual(
            first["files"][unplayable_path]["modified"], "2024-02-20T08:00:00Z"
        )
        self.assertEqual(
            first["files"][unplayable_path]["modified"],
            second["files"][unplayable_path]["modified"],
        )

    def test_file_modified_canonicalizes_various_added_formats(self):
        cases = [
            ("2024-03-04T05:06:07.000Z", "2024-03-04T05:06:07Z"),
            ("2024-03-04T05:06:07Z", "2024-03-04T05:06:07Z"),
            ("2024-03-04T05:06:07+00:00", "2024-03-04T05:06:07Z"),
            ("2024-03-04T07:06:07+02:00", "2024-03-04T05:06:07Z"),
            ("2024-03-04T05:06:07", "2024-03-04T05:06:07Z"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                snapshot, _ = self.builder.build(
                    [
                        {
                            "id": "ABC",
                            "status": "downloaded",
                            "filename": "Movie.mkv",
                            "added": raw,
                            "links": ["https://example.invalid/file"],
                            "files": [
                                {
                                    "id": 1,
                                    "path": "/Movie.mkv",
                                    "bytes": 1,
                                    "selected": 1,
                                }
                            ],
                        }
                    ]
                )
                self.assertEqual(
                    snapshot["files"]["movies/Movie.mkv/Movie.mkv"]["modified"],
                    expected,
                )

    def test_file_modified_falls_back_to_stable_epoch_for_unparseable_added(self):
        snapshot, _ = self.builder.build(
            [
                {
                    "id": "ABC",
                    "status": "downloaded",
                    "filename": "Movie.mkv",
                    "added": "not-a-date",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}
                    ],
                }
            ]
        )
        self.assertEqual(
            snapshot["files"]["movies/Movie.mkv/Movie.mkv"]["modified"],
            "1970-01-01T00:00:00Z",
        )

    def test_file_modified_falls_back_to_stable_epoch_when_added_missing(self):
        snapshot, _ = self.builder.build(
            [
                {
                    "id": "ABC123",
                    "status": "downloaded",
                    "filename": "Movie.mkv",
                    "links": ["https://example.invalid/file"],
                    "files": [
                        {"id": 1, "path": "/Movie.mkv", "bytes": 123, "selected": 1}
                    ],
                }
            ]
        )
        self.assertEqual(
            snapshot["files"]["movies/Movie.mkv/Movie.mkv"]["modified"],
            "1970-01-01T00:00:00Z",
        )

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


def _wait_for_task(state: BuzzState, task_id: str, timeout: float = 2.0) -> dict:
    """Poll until the background task reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for task in state.background_tasks.snapshot():
            if task["id"] == task_id and task["status"] in {"complete", "failed", "cancelled"}:
                return task
        time.sleep(0.05)
    raise TimeoutError(f"task {task_id} did not finish within {timeout}s")


class BuzzStateTests(unittest.TestCase):
    class FakeProvider:
        def __init__(
            self,
            torrents_list=None,
            torrent_infos=None,
            download_url=None,
            stream_error=None,
            delete_error=None,
            add_error=None,
        ):
            self.calls = []
            self.torrents_list = torrents_list or []
            self.torrent_infos = torrent_infos or {}
            self.download_url = download_url or "https://cdn.example.invalid/file"
            self.stream_error = stream_error
            self.delete_error = delete_error
            self.add_error = add_error
            self.added_magnets = []
            self.info_calls = []
            self.selected_files_calls = []
            self.deleted_ids = []

        def list_torrents(self):
            return [self._summary(item) for item in self.torrents_list]

        def get_torrent(self, torrent_id):
            self.info_calls.append(torrent_id)
            return self._detail(self.torrent_infos.get(torrent_id) or {})

        def fetch_details(self, torrent_ids, on_progress=None):
            total = len(torrent_ids)
            results = {}
            for i, torrent_id in enumerate(torrent_ids, 1):
                if on_progress is not None:
                    on_progress(torrent_id, i, total)
                results[torrent_id] = self.get_torrent(torrent_id)
            return results

        def add_magnet(self, magnet):
            self.added_magnets.append(magnet)
            if self.add_error is not None:
                raise self.add_error
            return "NEW_TORRENT"

        def select_files(self, torrent_id, file_ids):
            self.selected_files_calls.append((torrent_id, ",".join(file_ids)))

        def delete_torrent(self, torrent_id):
            self.deleted_ids.append(torrent_id)
            if self.delete_error is not None:
                raise self.delete_error

        def is_healthy(self):
            return True

        def resolve_stream(self, stream_ref):
            self.calls.append(stream_ref)
            if self.stream_error is not None:
                raise self.stream_error
            return self.download_url

        def _summary(self, item):
            return ProviderTorrentSummary(
                id=str(item.get("id") or ""),
                name=str(item.get("filename") or item.get("id") or "torrent"),
                bytes=int(item.get("bytes") or 0),
                progress=float(item.get("progress") or 0),
                status=str(item.get("status") or "unknown"),
                ended=item.get("ended"),
                stream_refs=tuple(str(link) for link in item.get("links") or []),
            )

        def _detail(self, item):
            links = iter(str(link) for link in item.get("links") or [])
            files = []
            for file_item in item.get("files") or []:
                selected = bool(file_item.get("selected"))
                files.append(
                    ProviderFile(
                        id=str(file_item.get("id") or ""),
                        path=str(file_item.get("path") or ""),
                        bytes=int(file_item.get("bytes") or 0),
                        selected=selected,
                        stream_ref=next(links, "") if selected else "",
                    )
                )
            name = str(
                item.get("filename")
                or item.get("original_filename")
                or item.get("id")
                or "torrent"
            )
            return ProviderTorrentDetail(
                id=str(item.get("id") or ""),
                hash=str(item.get("hash") or "").lower(),
                name=name,
                original_name=str(item.get("original_filename") or name),
                bytes=int(item.get("bytes") or 0),
                progress=float(item.get("progress") or 0),
                status=str(item.get("status") or "unknown"),
                added=item.get("added"),
                ended=item.get("ended"),
                files=tuple(files),
                stream_refs=tuple(str(link) for link in item.get("links") or []),
            )

    def test_manual_background_task_waits_for_start(self):
        ran = threading.Event()
        pool = BackgroundTaskPool()
        task_id = pool.submit_manual(
            "maintenance",
            "manual cleanup",
            lambda _tid, _cancel_event: ran.set(),
        )

        task = pool.snapshot()[0]
        self.assertEqual(task["status"], "pending")
        self.assertTrue(task["startable"])
        self.assertFalse(ran.is_set())

        self.assertTrue(pool.start(task_id))
        deadline = time.time() + 2
        while time.time() < deadline and not ran.is_set():
            time.sleep(0.01)

        self.assertTrue(ran.is_set())
        final = pool.snapshot()[0]
        self.assertEqual(final["status"], "complete")

    def test_pending_background_task_can_be_cancelled(self):
        ran = threading.Event()
        pool = BackgroundTaskPool()
        task_id = pool.submit_manual(
            "maintenance",
            "manual cleanup",
            lambda _tid, _cancel_event: ran.set(),
        )

        self.assertTrue(pool.cancel(task_id))

        task = pool.snapshot()[0]
        self.assertEqual(task["status"], "cancelled")
        self.assertFalse(task["startable"])
        self.assertFalse(ran.is_set())

    def _create_fake_provider(self):
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
        return self.FakeProvider(torrents_list, torrent_infos)

    def _hash_provider(
        self,
        torrent_id: str,
        thash: str,
        name: str,
        *,
        link: str = "",
        file_path: str | None = None,
    ):
        links = [link] if link else []
        files = []
        if file_path is not None:
            files.append({
                "id": 1,
                "path": file_path,
                "bytes": 1,
                "selected": 1,
            })
        return self.FakeProvider(
            torrents_list=[
                {
                    "id": torrent_id,
                    "filename": name,
                    "status": "downloaded",
                    "progress": 100,
                    "links": links,
                }
            ],
            torrent_infos={
                torrent_id: {
                    "id": torrent_id,
                    "hash": thash,
                    "filename": name,
                    "original_filename": name,
                    "status": "downloaded",
                    "progress": 100,
                    "links": links,
                    "files": files,
                }
            },
        )

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
            client = self.FakeProvider()
            state = BuzzState(config, client=client)

            first = state.resolve_download_url("https://example.invalid/source")
            second = state.resolve_download_url("https://example.invalid/source")

            self.assertEqual(first, "https://cdn.example.invalid/file")
            self.assertEqual(second, "https://cdn.example.invalid/file")
            self.assertEqual(client.calls, ["https://example.invalid/source"])
            self.assertEqual(
                state.resolved_urls["https://example.invalid/source"]["provider"],
                "real_debrid",
            )

    def test_resolve_download_url_negative_caches_hoster_unavailable(self):
        from buzz.core import state as state_mod
        from buzz.core.state import HosterUnavailableError

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
            client = self.FakeProvider(
                stream_error=ProviderStreamError(
                    "https://example.invalid/source",
                    "hoster_unavailable",
                )
            )
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

        class SlowHosterDownProvider(self.FakeProvider):
            def __init__(self):
                super().__init__(
                    stream_error=ProviderStreamError(
                        "https://example.invalid/source",
                        "hoster_unavailable",
                    )
                )
                self.call_lock = threading.Lock()

            def resolve_stream(self, stream_ref):
                with self.call_lock:
                    self.calls.append(stream_ref)
                time.sleep(0.01)
                raise ProviderStreamError(stream_ref, "hoster_unavailable")

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
            client = SlowHosterDownProvider()
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

    def test_multi_provider_sync_uses_highest_priority_duplicate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                [{"id": "RD1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["rd-link"]}],
                {
                    "RD1": {
                        "id": "RD1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["rd-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            torbox = self.FakeProvider(
                [{"id": "TB1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["tb-link"]}],
                {
                    "TB1": {
                        "id": "TB1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["tb-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )

            state.sync(trigger_hook=False)

            self.assertEqual(len(state.cache), 1)
            info = next(iter(state.cache.values()))["info"]
            self.assertEqual(info["provider"], "real_debrid")
            node = state.snapshot["files"]["movies/Movie.mkv/Movie.mkv"]
            self.assertEqual(node["source_url"], "rd-link")

    def test_add_magnet_falls_back_to_lower_priority_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(add_error=RuntimeError("rd down"))
            torbox = self.FakeProvider(
                torrent_infos={
                    "NEW_TORRENT": {
                        "id": "NEW_TORRENT",
                        "hash": "fallbackhash",
                        "filename": "Fallback.mkv",
                        "status": "downloaded",
                        "files": [],
                    }
                }
            )
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )

            result = state.add_magnet("magnet:?xt=urn:btih:fallbackhash")

            self.assertEqual(result["provider"], "torbox")
            self.assertIn("warning", result)
            self.assertEqual(real_debrid.added_magnets, ["magnet:?xt=urn:btih:fallbackhash"])
            self.assertEqual(torbox.added_magnets, ["magnet:?xt=urn:btih:fallbackhash"])

    def test_resolve_download_url_falls_back_to_lower_priority_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                stream_error=ProviderStreamError("rd-link", "file_unavailable")
            )
            torbox = self.FakeProvider(download_url="https://cdn.example.invalid/torbox")
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )
            state.stream_sources = {
                "rd-link": [
                    {"provider": "real_debrid", "source_url": "rd-link"},
                    {"provider": "torbox", "source_url": "tb-link"},
                ]
            }

            resolved = state.resolve_download_url("rd-link")

            self.assertEqual(resolved, "https://cdn.example.invalid/torbox")
            self.assertEqual(real_debrid.calls, ["rd-link"])
            self.assertEqual(torbox.calls, ["tb-link"])

    def test_startup_rebuilds_stream_sources_from_persisted_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                [
                    {
                        "id": "RD1",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "progress": 100,
                        "links": ["https://real-debrid.com/d/RD1"],
                    }
                ],
                {
                    "RD1": {
                        "id": "RD1",
                        "hash": "hash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["https://real-debrid.com/d/RD1"],
                        "files": [
                            {
                                "id": 1,
                                "path": "/Movie.mkv",
                                "bytes": 1,
                                "selected": 1,
                            }
                        ],
                    }
                },
            )
            BuzzState(config, client={"real_debrid": real_debrid}).sync(
                trigger_hook=False
            )

            torbox_first = Config(
                token="token",
                torbox_token="tbtoken",
                provider_priority=("torbox", "real_debrid"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            restarted_rd = self.FakeProvider()
            restarted_tb = self.FakeProvider()
            restarted = BuzzState(
                torbox_first,
                client={"torbox": restarted_tb, "real_debrid": restarted_rd},
            )

            resolved = restarted.resolve_download_url(
                "https://real-debrid.com/d/RD1"
            )

            self.assertEqual(resolved, "https://cdn.example.invalid/file")
            self.assertEqual(
                restarted_rd.calls, ["https://real-debrid.com/d/RD1"]
            )
            self.assertEqual(restarted_tb.calls, [])

    def test_http_source_without_mapping_never_uses_torbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="tbtoken",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            torbox = self.FakeProvider()
            state = BuzzState(config, client={"torbox": torbox})

            with self.assertRaises(ValueError):
                state.resolve_download_url("https://real-debrid.com/d/RD1")

            self.assertEqual(torbox.calls, [])

    def test_stream_source_fallbacks_match_selected_files_by_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                torbox_token="tbtoken",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                [
                    {
                        "id": "RD1",
                        "filename": "Show",
                        "status": "downloaded",
                        "progress": 100,
                        "links": ["rd-a", "rd-b"],
                    }
                ],
                {
                    "RD1": {
                        "id": "RD1",
                        "hash": "samehash",
                        "filename": "Show",
                        "status": "downloaded",
                        "links": ["rd-a", "rd-b"],
                        "files": [
                            {
                                "id": 1,
                                "path": "/Show.S01E01.mkv",
                                "bytes": 1,
                                "selected": 1,
                            },
                            {
                                "id": 2,
                                "path": "/Show.S01E02.mkv",
                                "bytes": 1,
                                "selected": 1,
                            },
                        ],
                    }
                },
            )
            torbox = self.FakeProvider(
                [
                    {
                        "id": "TB1",
                        "filename": "Show",
                        "status": "downloaded",
                        "progress": 100,
                        "links": ["tb-b", "tb-a"],
                    }
                ],
                {
                    "TB1": {
                        "id": "TB1",
                        "hash": "samehash",
                        "filename": "Show",
                        "status": "downloaded",
                        "links": ["tb-b", "tb-a"],
                        "files": [
                            {
                                "id": 2,
                                "path": "/Show.S01E02.mkv",
                                "bytes": 1,
                                "selected": 1,
                            },
                            {
                                "id": 1,
                                "path": "/Show.S01E01.mkv",
                                "bytes": 1,
                                "selected": 1,
                            },
                        ],
                    }
                },
            )
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )

            state.sync(trigger_hook=False)

            self.assertEqual(
                state.stream_sources["rd-a"],
                [
                    {"provider": "real_debrid", "source_url": "rd-a"},
                    {"provider": "torbox", "source_url": "tb-a"},
                ],
            )
            self.assertEqual(
                state.stream_sources["rd-b"],
                [
                    {"provider": "real_debrid", "source_url": "rd-b"},
                    {"provider": "torbox", "source_url": "tb-b"},
                ],
            )

    def test_torbox_detail_uses_bypass_only_after_empty_file_detail(self):
        class FakeTorBox(TorBoxProviderClient):
            def __init__(self):
                super().__init__("token")
                self.calls = []

            def _request(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs.get("params")))
                params = kwargs.get("params") or {}
                if params.get("bypass_cache") == "true":
                    return [
                        {
                            "id": 37257646,
                            "name": "Movie.mkv",
                            "download_state": "cached",
                            "size": 10,
                            "files": [
                                {
                                    "id": 1,
                                    "name": "Movie.mkv",
                                    "size": 10,
                                    "selected": True,
                                }
                            ],
                        }
                    ]
                return [
                    {
                        "id": 37257646,
                        "name": "Movie.mkv",
                        "download_state": "cached",
                        "size": 10,
                        "files": [],
                    }
                ]

        client = FakeTorBox()

        detail = client.get_torrent("37257646")

        self.assertEqual(detail.id, "37257646")
        self.assertEqual(len(detail.files), 1)
        self.assertEqual(client.calls[0][2], {"id": "37257646"})
        self.assertEqual(
            client.calls[1][2],
            {"id": "37257646", "bypass_cache": "true"},
        )

    def test_torbox_summary_prefers_torrent_id_over_id(self):
        client = TorBoxProviderClient(token="token")
        summary = client._summary(
            {
                "id": 3,
                "torrent_id": 37257646,
                "name": "Movie.mkv",
                "download_state": "completed",
            }
        )

        self.assertEqual(summary.id, "37257646")
        self.assertEqual(summary.stream_refs, ("37257646",))

    def test_torbox_single_file_without_numeric_id_resolves_without_file_id(self):
        class FakeTorBox(TorBoxProviderClient):
            def __init__(self):
                super().__init__("token")
                self.calls = []

            def _request(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs.get("params")))
                return "https://cdn.example.invalid/torbox"

        client = FakeTorBox()
        detail = client._detail(
            {
                "torrent_id": 12345678,
                "name": "Synthetic Feature",
                "download_state": "completed",
                "files": [
                    {
                        "name": "Synthetic Feature (2026).mkv",
                        "size": 100,
                    }
                ],
            }
        )

        self.assertEqual(detail.files[0].id, "")
        self.assertEqual(detail.files[0].path, "Synthetic Feature (2026).mkv")
        self.assertEqual(detail.stream_refs, ("12345678",))

        resolved = client.resolve_stream(detail.stream_refs[0])

        self.assertEqual(resolved, "https://cdn.example.invalid/torbox")
        self.assertEqual(
            client.calls[0][2],
            {"token": "token", "torrent_id": "12345678"},
        )

    def test_torbox_file_id_preserves_zero_and_ignores_names(self):
        client = TorBoxProviderClient(token="token")

        zero = client._file(
            "TB42",
            {"file_id": 0, "name": "movie.mkv", "size": 100},
        )
        named = client._file(
            "TB42",
            {
                "short_name": "short.mkv",
                "name": "movie.mkv",
                "size": 100,
            },
        )

        self.assertEqual(zero.id, "0")
        self.assertEqual(zero.stream_ref, "TB42:0")
        self.assertEqual(named.id, "")
        self.assertEqual(named.path, "short.mkv")
        self.assertEqual(named.stream_ref, "")

    def test_torbox_resolve_stream_sends_token_and_omits_nonnumeric_file_id(self):
        class FakeTorBox(TorBoxProviderClient):
            def __init__(self):
                super().__init__("secret-token")
                self.calls = []

            def _request(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs.get("params")))
                return "https://cdn.example.invalid/torbox"

        client = FakeTorBox()

        resolved = client.resolve_stream("12345678:Synthetic Feature.mkv")

        self.assertEqual(resolved, "https://cdn.example.invalid/torbox")
        self.assertEqual(
            client.calls[0][2],
            {"token": "secret-token", "torrent_id": "12345678"},
        )

    def test_torbox_resolve_stream_sends_token_and_numeric_file_id(self):
        class FakeTorBox(TorBoxProviderClient):
            def __init__(self):
                super().__init__("secret-token")
                self.calls = []

            def _request(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs.get("params")))
                return "https://cdn.example.invalid/torbox"

        client = FakeTorBox()

        resolved = client.resolve_stream("12345678:42")

        self.assertEqual(resolved, "https://cdn.example.invalid/torbox")
        self.assertEqual(
            client.calls[0][2],
            {"token": "secret-token", "torrent_id": "12345678", "file_id": "42"},
        )

    def test_torbox_stream_http_errors_are_sanitized(self):
        class FakeTorBox(TorBoxProviderClient):
            def _request(self, method, path, **kwargs):
                request = httpx.Request(
                    "GET",
                    "https://api.torbox.app/v1/api/torrents/requestdl"
                    "?token=secret-token&torrent_id=12345678",
                )
                response = httpx.Response(
                    429,
                    request=request,
                    json={"detail": "Too Many Requests"},
                )
                raise httpx.HTTPStatusError(
                    "Client error '429 Too Many Requests' for url "
                    "'https://api.torbox.app/v1/api/torrents/requestdl"
                    "?token=secret-token&torrent_id=12345678'",
                    request=request,
                    response=response,
                )

        client = FakeTorBox("secret-token")

        with self.assertRaises(ProviderStreamError) as raised:
            client.resolve_stream("12345678")

        self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual(raised.exception.code, "http_429 Too Many Requests")

    def test_torbox_multi_file_without_numeric_ids_does_not_use_filenames(self):
        client = TorBoxProviderClient(token="token")
        detail = client._detail(
            {
                "torrent_id": 12345678,
                "name": "Synthetic Feature",
                "download_state": "completed",
                "files": [
                    {"name": "Movie.mkv", "size": 100},
                    {"name": "Sample.mkv", "size": 10},
                ],
            }
        )

        self.assertEqual(detail.stream_refs, ())
        self.assertTrue(all(file_item.id == "" for file_item in detail.files))

    def test_torbox_cached_selection_does_not_synthesize_filename_file_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            info = {
                "id": "12345678",
                "hash": "hash",
                "status": "downloaded",
                "filename": "Synthetic Feature",
                "links": [],
                "files": [
                    {
                        "id": "Synthetic Feature.mkv",
                        "path": "/Synthetic Feature.mkv",
                        "bytes": 100,
                        "selected": 1,
                    },
                    {
                        "id": "Sample.mkv",
                        "path": "/Sample.mkv",
                        "bytes": 10,
                        "selected": 1,
                    },
                ],
            }

            state._rebuild_torbox_links(info, "12345678")

            self.assertEqual(info["links"], [])

    def test_startup_repairs_persisted_torbox_filename_stream_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            stale_entry = {
                "signature": {},
                "info": {
                    "id": "12345678",
                    "hash": "hash",
                    "status": "downloaded",
                    "filename": "Synthetic Feature",
                    "links": ["12345678:Synthetic Feature.mkv"],
                    "files": [
                        {
                            "id": "Synthetic Feature.mkv",
                            "path": "/Synthetic Feature.mkv",
                            "bytes": 100,
                            "selected": 1,
                            "stream_ref": "12345678:Synthetic Feature.mkv",
                        }
                    ],
                },
                "magnet": None,
            }
            state.cache["torbox:12345678"] = stale_entry
            state._save_cache_entry("torbox:12345678", stale_entry)

            restarted = BuzzState(
                config,
                client={"torbox": self.FakeProvider()},
            )

            info = restarted.cache["torbox:12345678"]["info"]
            self.assertEqual(info["links"], ["12345678"])
            self.assertEqual(info["files"][0]["id"], "")
            self.assertEqual(info["files"][0]["stream_ref"], "")
            node = restarted.lookup(
                "movies/Synthetic Feature/Synthetic Feature.mkv"
            )
            if node is None:
                self.fail("Expected repaired TorBox file in DAV snapshot")
            self.assertEqual(node["source_url"], "12345678")

    def test_torbox_delete_uses_json_controltorrent_payload(self):
        class FakeTorBox(TorBoxProviderClient):
            def __init__(self):
                super().__init__("token")
                self.calls = []

            def _request(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs))
                return {"ok": True}

        client = FakeTorBox()

        client.delete_torrent("37257646")

        self.assertEqual(client.calls[0][0], "POST")
        self.assertEqual(
            client.calls[0][1],
            "/v1/api/torrents/controltorrent",
        )
        self.assertEqual(
            client.calls[0][2]["json"],
            {"torrent_id": 37257646, "operation": "delete"},
        )

    def test_rd_fetch_details_calls_on_progress_per_torrent(self):
        class FakeRD:
            def __init__(self):
                self.calls = []

            class torrents:
                @staticmethod
                def info(torrent_id):
                    resp = SimpleNamespace()
                    resp.json = lambda: {
                        "id": torrent_id,
                        "hash": "abc123",
                        "filename": f"Movie-{torrent_id}.mkv",
                        "original_filename": f"Movie-{torrent_id}.mkv",
                        "bytes": 100,
                        "progress": 100,
                        "status": "downloaded",
                        "links": [],
                        "files": [],
                    }
                    resp.status_code = 200
                    return resp

        client = RealDebridProviderClient("token", raw_client=FakeRD())
        progress_calls = []
        details = client.fetch_details(
            ["AAA", "BBB", "CCC"],
            on_progress=lambda tid, i, n: progress_calls.append((tid, i, n)),
        )
        self.assertEqual(len(details), 3)
        self.assertEqual(progress_calls, [("AAA", 1, 3), ("BBB", 2, 3), ("CCC", 3, 3)])

    def test_rd_get_torrent_retries_on_error_response(self):
        class FakeRD:
            def __init__(self, responses):
                self.responses = iter(responses)

            class _TorrentsProxy:
                def __init__(self, rd):
                    self._rd = rd

                def info(self, torrent_id):
                    return next(self._rd.responses)

            @property
            def torrents(self):
                return self._TorrentsProxy(self)

        def _ok_response(torrent_id):
            resp = SimpleNamespace()
            resp.json = lambda: {
                "id": torrent_id,
                "hash": "abc123",
                "filename": "Movie.mkv",
                "original_filename": "Movie.mkv",
                "bytes": 100,
                "progress": 100,
                "status": "downloaded",
                "links": [],
                "files": [],
            }
            resp.status_code = 200
            return resp

        def _error_response():
            resp = SimpleNamespace()
            resp.json = lambda: {"error": "SERVICE_UNAVAILABLE", "error_code": 503}
            resp.status_code = 503
            return resp

        with patch("buzz.providers.real_debrid.time.sleep"), patch("buzz.providers.real_debrid.record_event"):
            # First call returns error, second returns valid data.
            rd = FakeRD([_error_response(), _ok_response("T1")])
            client = RealDebridProviderClient("token", raw_client=rd)
            detail = client.get_torrent("T1")
        self.assertEqual(detail.id, "T1")

    def test_rd_get_torrent_raises_after_exhausting_retries(self):
        class FakeRD:
            class torrents:
                @staticmethod
                def info(torrent_id):
                    resp = SimpleNamespace()
                    resp.json = lambda: {"error": "SERVICE_UNAVAILABLE", "error_code": 503}
                    resp.status_code = 503
                    return resp

        with patch("buzz.providers.real_debrid.time.sleep"), patch("buzz.providers.real_debrid.record_event"):
            client = RealDebridProviderClient("token", raw_client=FakeRD())
            with self.assertRaises(RuntimeError) as ctx:
                client.get_torrent("T1")
        self.assertIn("Real-Debrid transient error", str(ctx.exception))
        self.assertIn("T1", str(ctx.exception))

    def test_rd_get_torrent_error_body_does_not_leak_none_strip(self):
        """Regression: a transient RD error body must never produce NoneType.strip() downstream."""
        class FakeRD:
            class torrents:
                @staticmethod
                def info(torrent_id):
                    resp = SimpleNamespace()
                    resp.json = lambda: {"error": "hoster_unavailable", "error_code": 8}
                    resp.status_code = 200
                    return resp

        with patch("buzz.providers.real_debrid.time.sleep"), patch("buzz.providers.real_debrid.record_event"):
            client = RealDebridProviderClient("token", raw_client=FakeRD())
            with self.assertRaises(RuntimeError):
                client.get_torrent("FAIL")

    def _rd_select_files_client(self, body, status_code=200):
        captured = {}

        class FakeRD:
            class torrents:
                @staticmethod
                def select_files(torrent_id, files):
                    captured["args"] = (torrent_id, files)
                    resp = SimpleNamespace()
                    resp.status_code = status_code
                    resp.text = json.dumps(body) if body is not None else ""
                    resp.json = lambda: body
                    return resp

        client = RealDebridProviderClient("token", raw_client=FakeRD())
        return client, captured

    def test_rd_select_files_treats_action_already_done_as_success(self):
        client, captured = self._rd_select_files_client(
            {"error": "action_already_done", "error_code": 31}
        )
        # Must not raise: RD already has this selection applied.
        client.select_files("T1", ["1", "2"])
        self.assertEqual(captured["args"], ("T1", "1,2"))

    def test_rd_select_files_raises_real_error_body(self):
        client, _ = self._rd_select_files_client(
            {"error": "bad_token", "error_code": 8}
        )
        with self.assertRaises(ValueError) as ctx:
            client.select_files("T1", ["1"])
        self.assertIn("bad_token", str(ctx.exception))

    def test_rd_select_files_raises_on_non_2xx_status(self):
        client, _ = self._rd_select_files_client(None, status_code=503)
        with self.assertRaises(ValueError):
            client.select_files("T1", ["1"])

    def test_torbox_fetch_details_silent_for_list_cache_hits(self):
        class FakeTorBox(TorBoxProviderClient):
            def __init__(self):
                super().__init__("token")
                self.get_torrent_calls = []

            def _request(self, method, path, **kwargs):
                return []

            def get_torrent(self, torrent_id):
                self.get_torrent_calls.append(torrent_id)
                return super().get_torrent(torrent_id)

        client = FakeTorBox()
        client._list_cache = [
            {
                "torrent_id": "TB1",
                "name": "Movie.mkv",
                "size": 100,
                "download_state": "downloaded",
                "progress": 1.0,
                "files": [{"id": 1, "path": "/Movie.mkv", "size": 100, "selected": True}],
            }
        ]
        progress_calls = []
        details = client.fetch_details(
            ["TB1"],
            on_progress=lambda tid, i, n: progress_calls.append((tid, i, n)),
        )
        self.assertIn("TB1", details)
        self.assertEqual(progress_calls, [], "no progress for list-cache hits")
        self.assertEqual(client.get_torrent_calls, [], "get_torrent not called for cache hits")

    def test_build_torrent_cache_prints_progress_only_for_misses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                real_debrid_token="token",
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            progress_calls = []

            class TrackingProvider(self.FakeProvider):
                def is_healthy(self):
                    return True
                def fetch_details(self, torrent_ids, on_progress=None):
                    total = len(torrent_ids)
                    results = {}
                    for i, tid in enumerate(torrent_ids, 1):
                        if on_progress is not None:
                            on_progress(tid, i, total)
                        results[tid] = self.get_torrent(tid)
                    return results

            provider = TrackingProvider(
                torrents_list=[
                    {"id": "T1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["http://link1"]},
                    {"id": "T2", "filename": "Show.mkv", "status": "downloaded", "progress": 100, "links": ["http://link2"]},
                ],
                torrent_infos={
                    "T1": {"id": "T1", "hash": "h1", "filename": "Movie.mkv", "original_filename": "Movie.mkv", "bytes": 10, "progress": 100, "status": "downloaded", "links": ["http://link1"], "files": [{"id": "1", "path": "/Movie.mkv", "bytes": 10, "selected": 1}]},
                    "T2": {"id": "T2", "hash": "h2", "filename": "Show.mkv", "original_filename": "Show.mkv", "bytes": 10, "progress": 100, "status": "downloaded", "links": ["http://link2"], "files": [{"id": "1", "path": "/Show.mkv", "bytes": 10, "selected": 1}]},
                },
            )
            state = BuzzState(config, client={"real_debrid": provider})
            import io as _io
            buf = _io.StringIO()
            with patch("sys.stdout", buf):
                state.sync(trigger_hook=False)
            output = buf.getvalue()
            # Both T1 and T2 need a refetch on first sync (no cache).
            self.assertIn("Fetching entry: T1 (1/2)", output)
            self.assertIn("Fetching entry: T2 (2/2)", output)

            # Second sync: all are terminal hits, no progress output.
            buf2 = _io.StringIO()
            with patch("sys.stdout", buf2):
                state.sync(trigger_hook=False)
            output2 = buf2.getvalue()
            self.assertNotIn("Fetching entry:", output2)

    def test_sync_uses_cached_torbox_detail_on_transient_detail_error(self):
        class FailingDetailProvider(self.FakeProvider):
            def is_healthy(self):
                return True
            def get_torrent(self, torrent_id):
                request = httpx.Request(
                    "GET",
                    "https://api.torbox.app/v1/api/torrents/mylist",
                )
                response = httpx.Response(500, request=request)
                raise httpx.HTTPStatusError("boom", request=request, response=response)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            torbox = FailingDetailProvider(
                torrents_list=[
                    {
                        "id": "TB1",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "progress": 100,
                        "links": ["TB1:1"],
                    }
                ]
            )
            state = BuzzState(config, client={"torbox": torbox})
            # Status is "downloading" (non-terminal) so terminal_hit won't fire.
            # The refresh is attempted, hits the 500, and falls back to cached info.
            cached_entry = {
                "signature": {},
                "info": {
                    "id": "TB1",
                    "hash": "hash",
                    "filename": "Movie.mkv",
                    "original_filename": "Movie.mkv",
                    "bytes": 10,
                    "status": "downloading",
                    "progress": 50,
                    "links": [],
                    "files": [
                        {
                            "id": "1",
                            "path": "/Movie.mkv",
                            "bytes": 10,
                            "selected": 1,
                        }
                    ],
                },
                "magnet": "magnet:?xt=urn:btih:hash",
            }
            state.cache["torbox:TB1"] = cached_entry
            # Cache-hit detection reads from the full per-provider mirror; keep
            # it in sync as a real prior sync would.
            state._full_cache["torbox:TB1"] = cached_entry

            with patch("buzz.core.state.record_event") as mock_record:
                report = state.sync(trigger_hook=False)

            self.assertEqual(report["synced_torrents"], 1)
            self.assertEqual(
                state.cache["torbox:TB1"]["info"]["filename"], "Movie.mkv"
            )
            self.assertTrue(
                any(
                    call.kwargs.get("level") == "error"
                    and call.kwargs.get("event")
                    == "provider_detail_refresh_failed"
                    for call in mock_record.call_args_list
                )
            )

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
            client = self.FakeProvider(
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

    def test_restore_archive_prefers_stored_magnet(self):
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
            client = self.FakeProvider()
            state = BuzzState(config, client=client)
            state.archive = {
                "ABC123HASH": {
                    "hash": "ABC123HASH",
                    "name": "Movie.2026.1080p.mkv",
                    "bytes": 123,
                    "files": [{"id": 1, "path": "/Movie.2026.1080p.mkv"}],
                    "deleted_at": "2026-01-01T00:00:00Z",
                    "magnet": "magnet:?xt=urn:btih:ABC123HASH&dn=Movie",
                }
            }

            state.restore_archive("ABC123HASH")

            self.assertEqual(
                client.added_magnets,
                ["magnet:?xt=urn:btih:ABC123HASH&dn=Movie"],
            )

    def test_restore_archive_falls_back_to_hash_when_magnet_missing(self):
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
            client = self.FakeProvider()
            state = BuzzState(config, client=client)
            state.archive = {
                "ABC123HASH": {
                    "hash": "ABC123HASH",
                    "name": "Movie.2026.1080p.mkv",
                    "bytes": 123,
                    "files": [],
                    "deleted_at": "2026-01-01T00:00:00Z",
                    "magnet": None,
                }
            }

            state.restore_archive("ABC123HASH")

            self.assertEqual(
                client.added_magnets,
                ["magnet:?xt=urn:btih:ABC123HASH"],
            )

    def test_restore_archive_selects_files_with_provider_cache_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            state.select_files = MagicMock()
            state.archive = {
                "ABC123HASH": {
                    "hash": "ABC123HASH",
                    "name": "Movie.2026.1080p.mkv",
                    "bytes": 123,
                    "files": [{"id": 1, "path": "/Movie.2026.1080p.mkv"}],
                    "deleted_at": "2026-01-01T00:00:00Z",
                    "magnet": None,
                }
            }

            state.restore_archive("ABC123HASH")

            state.select_files.assert_called_once_with(
                "torbox:NEW_TORRENT",
                ["1"],
            )

    def test_select_files_persists_requested_selection_for_real_debrid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            # RD may report action_already_done for a repeated selection. The
            # requested ids still represent the user's chosen selection.
            client = self.FakeProvider(
                torrent_infos={
                    "T1": {
                        "id": "T1",
                        "hash": "abc123hash",
                        "status": "downloaded",
                        "filename": "Show.mkv",
                        "links": ["https://rd.invalid/link1"],
                        "files": [
                            {"id": "1", "path": "/Show.mkv", "bytes": 10, "selected": True},
                            {"id": "2", "path": "/extra.nfo", "bytes": 1, "selected": False},
                        ],
                    }
                }
            )
            state = BuzzState(config, client={"real_debrid": client})
            state.cache["T1"] = {
                "signature": {},
                "info": {
                    "id": "T1",
                    "hash": "abc123hash",
                    "status": "downloaded",
                    "links": [],
                    "files": [
                        {"id": "1", "path": "/Show.mkv", "bytes": 10, "selected": 0},
                        {"id": "2", "path": "/extra.nfo", "bytes": 1, "selected": 0},
                    ],
                },
                "magnet": None,
            }

            state.select_files("T1", ["1", "2"])

            cached_info = state.cache["T1"]["info"]
            selected = {
                f["id"]: bool(f["selected"]) for f in cached_info["files"]
            }
            self.assertEqual(selected, {"1": True, "2": True})
            self.assertEqual(cached_info["links"], ["https://rd.invalid/link1"])
            # Persisted by-hash selection reflects the request.
            # Paths are stored normalized (leading slash stripped).
            self.assertEqual(
                state.file_selections["abc123hash"],
                {"Show.mkv", "extra.nfo"},
            )
            self.assertEqual(
                db.load_file_selections(state.conn).get("abc123hash"),
                {"Show.mkv", "extra.nfo"},
            )

    def test_select_files_populates_links_so_resync_does_not_refetch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            client = self.FakeProvider(
                torrent_infos={
                    "T1": {
                        "id": "T1",
                        "hash": "abc123hash",
                        "status": "downloaded",
                        "filename": "Show.mkv",
                        "links": ["https://rd.invalid/link1"],
                        "files": [
                            {"id": "1", "path": "/Show.mkv", "bytes": 10, "selected": True},
                        ],
                    }
                }
            )
            state = BuzzState(config, client={"real_debrid": client})
            state.cache["T1"] = {
                "signature": {},
                "info": {
                    "id": "T1",
                    "hash": "abc123hash",
                    "status": "downloaded",
                    "links": [],
                    "files": [
                        {"id": "1", "path": "/Show.mkv", "bytes": 10, "selected": 0},
                    ],
                },
                "magnet": None,
            }
            state.select_files("T1", ["1"])
            # After a successful select the cached entry has links, so the
            # terminal-hit path keeps it out of refetch on the next sync.
            self.assertTrue(state.cache["T1"]["info"]["links"])

    def test_should_refresh_skips_refetch_when_signature_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"real_debrid": self.FakeProvider()})
            # Downloaded torrent, summary advertises a link, but the cached
            # detail has no playable selected media (e.g. no selected video).
            summary = {
                "id": "T1",
                "status": "downloaded",
                "progress": 100,
                "links": ["https://rd.invalid/link1"],
            }
            info = {
                "id": "T1",
                "status": "downloaded",
                "links": [],
                "files": [
                    {"id": "1", "path": "/notes.txt", "bytes": 1, "selected": 0},
                ],
            }
            # Without a matching signature the old behaviour forces a refresh.
            self.assertTrue(
                state._should_refresh_cached_info(
                    summary, info, signature_matches=False
                )
            )
            # Once the cached signature matches the current summary, the detail
            # is current for this upstream state and must not be refetched.
            self.assertFalse(
                state._should_refresh_cached_info(
                    summary, info, signature_matches=True
                )
            )
            # But a detail with a selected video that still lacks a resolved
            # link is genuinely stale and must refetch even if the signature
            # matches (the per-file links still need fetching).
            stale_video = {
                "id": "T1",
                "status": "downloaded",
                "links": [],
                "files": [
                    {"id": "1", "path": "/Movie.1080p.mkv", "bytes": 9, "selected": 1},
                ],
            }
            self.assertTrue(
                state._should_refresh_cached_info(
                    summary, stale_video, signature_matches=True
                )
            )

    def test_submit_archive_restore_registers_task_without_running_inline(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return "task-restore"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            state.background_tasks = cast(Any, FakeTaskPool())
            state.restore_archive = MagicMock()
            state.archive = {
                "ABC123HASH": {
                    "hash": "ABC123HASH",
                    "name": "Movie.2026.1080p.mkv",
                }
            }

            task_id = state.submit_archive_restore("ABC123HASH")

            self.assertEqual(task_id, "task-restore")
            self.assertEqual(
                state.background_tasks.submitted[0][:2],
                ("restore", "restore_archive: Movie.2026.1080p.mkv"),
            )
            state.restore_archive.assert_not_called()

    def test_submit_archive_restore_work_wakes_poller(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return "task-restore"

        class FakePoller:
            def __init__(self):
                self.wake_count = 0

            def wake(self):
                self.wake_count += 1

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            state.background_tasks = cast(Any, FakeTaskPool())
            poller = FakePoller()
            state.attach_poller(cast(Any, poller))
            state.restore_archive = MagicMock(
                return_value={
                    "status": "success",
                    "id": "NEW_TORRENT",
                    "provider": "real_debrid",
                    "provider_torrent_id": "NEW_TORRENT",
                    "selected_files": 1,
                    "total_files": 3,
                }
            )
            state.archive = {
                "ABC123HASH": {
                    "hash": "ABC123HASH",
                    "name": "Movie.2026.1080p.mkv",
                }
            }

            state.submit_archive_restore("ABC123HASH")
            _kind, _label, work = state.background_tasks.submitted[0]
            with patch("buzz.core.state.record_event") as mock_record_event:
                work("test-task-id", threading.Event())

            state.restore_archive.assert_called_once_with("ABC123HASH")
            mock_record_event.assert_any_call(
                "restored archive: provider=Real-Debrid, "
                "files_marked=1/3, torrent_id=NEW_TORRENT",
                event="archive_restore_complete",
                provider="real_debrid",
                provider_torrent_id="NEW_TORRENT",
                selected_files=1,
                total_files=3,
            )
            self.assertEqual(poller.wake_count, 1)
            self.assertEqual(len(state.background_tasks.submitted), 1)

    def test_submit_archive_restore_work_submits_sync_task_without_poller(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return f"task-{len(self.submitted)}"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            state.background_tasks = cast(Any, FakeTaskPool())
            state.restore_archive = MagicMock(
                return_value={
                    "status": "success",
                    "id": "NEW_TORRENT",
                    "provider": "real_debrid",
                    "provider_torrent_id": "NEW_TORRENT",
                    "selected_files": 1,
                    "total_files": 3,
                }
            )
            state.sync = MagicMock()
            state.archive = {
                "ABC123HASH": {
                    "hash": "ABC123HASH",
                    "name": "Movie.2026.1080p.mkv",
                }
            }

            state.submit_archive_restore("ABC123HASH")
            _kind, _label, work = state.background_tasks.submitted[0]
            work("test-task-id", threading.Event())

            self.assertEqual(
                [item[0] for item in state.background_tasks.submitted],
                ["restore", "sync"],
            )
            state.background_tasks.submitted[1][2]("test-task-id", threading.Event())
            state.sync.assert_called_once_with()

    def test_infringing_scan_registers_manual_cleanup(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []
                self.manual = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return f"task-{len(self.submitted)}"

            def submit_manual(self, kind, label, work, **_kwargs):
                self.manual.append((kind, label, work))
                return "cleanup-task"

        class InfringingProvider(self.FakeProvider):
            def resolve_stream(self, stream_ref):
                self.calls.append(stream_ref)
                if stream_ref == "rd-bad":
                    raise ProviderStreamError(stream_ref, "infringing_file (9)")
                return "https://cdn.example.invalid/file"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                torbox_token="tbtoken",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            provider = InfringingProvider()
            state = BuzzState(config, client={"real_debrid": provider})
            state.background_tasks = cast(Any, FakeTaskPool())
            state.cache = {
                "RD1": {
                    "signature": {},
                    "info": {
                        "id": "RD1",
                        "hash": "hash1",
                        "filename": "Movie",
                        "status": "downloaded",
                        "links": ["rd-ok", "rd-bad"],
                        "files": [
                            {
                                "id": "1",
                                "path": "/Movie.ok.mkv",
                                "bytes": 1,
                                "selected": 1,
                            },
                            {
                                "id": "2",
                                "path": "/Movie.bad.mkv",
                                "bytes": 1,
                                "selected": 1,
                            },
                        ],
                    },
                    "magnet": "magnet:?xt=urn:btih:hash1",
                },
                "torbox:TB1": {
                    "signature": {},
                    "info": {
                        "id": "TB1",
                        "hash": "hash2",
                        "filename": "Torbox",
                        "status": "downloaded",
                        "links": ["TB1:1"],
                        "files": [
                            {
                                "id": "1",
                                "path": "/Torbox.mkv",
                                "bytes": 1,
                                "selected": 1,
                            }
                        ],
                    },
                    "magnet": "magnet:?xt=urn:btih:hash2",
                },
            }

            task_id = state.submit_infringing_scan()
            _kind, _label, work = state.background_tasks.submitted[0]
            work("test-task-id", threading.Event())

            self.assertEqual(task_id, "task-1")
            self.assertEqual(provider.calls, ["rd-ok", "rd-bad"])
            self.assertEqual(
                state.background_tasks.manual[0][:2],
                (
                    "maintenance",
                    "cleanup_rd_infringing: 1 torrent(s)",
                ),
            )

    def test_infringing_cleanup_archives_deletes_and_queues_sync(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []
                self.manual = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return f"task-{len(self.submitted)}"

            def submit_manual(self, kind, label, work, **_kwargs):
                self.manual.append((kind, label, work))
                return "cleanup-task"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            provider = self.FakeProvider()
            state = BuzzState(config, client={"real_debrid": provider})
            state.background_tasks = cast(Any, FakeTaskPool())
            state.cache = {
                "RD1": {
                    "signature": {},
                    "info": {
                        "id": "RD1",
                        "hash": "hash1",
                        "filename": "Movie",
                        "status": "downloaded",
                        "links": ["rd-bad"],
                        "files": [
                            {
                                "id": "1",
                                "path": "/Movie.bad.mkv",
                                "bytes": 1,
                                "selected": 1,
                            }
                        ],
                    },
                    "magnet": "magnet:?xt=urn:btih:hash1",
                }
            }
            state._save_cache(state.cache)
            candidates = [
                {
                    "cache_key": "RD1",
                    "torrent_id": "RD1",
                    "source_url": "rd-bad",
                    "path": "Movie.bad.mkv",
                    "name": "Movie",
                },
                {
                    "cache_key": "RD1",
                    "torrent_id": "RD1",
                    "source_url": "rd-bad",
                    "path": "Movie.bad.mkv",
                    "name": "Movie",
                },
            ]

            task_id = state._register_infringing_cleanup(candidates)
            _kind, _label, cleanup = state.background_tasks.manual[0]
            cleanup("test-task-id", threading.Event())

            self.assertEqual(task_id, "cleanup-task")
            self.assertEqual(provider.deleted_ids, ["RD1"])
            self.assertNotIn("RD1", state.cache)
            self.assertIn("hash1", state.archive)
            self.assertEqual(
                state.archive["hash1"]["magnet"],
                "magnet:?xt=urn:btih:hash1",
            )
            self.assertEqual(
                [item[0] for item in state.background_tasks.submitted],
                ["sync"],
            )

    def test_provider_migration_scan_registers_manual_commit(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []
                self.manual = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return f"task-{len(self.submitted)}"

            def submit_manual(self, kind, label, work, **_kwargs):
                self.manual.append((kind, label, work))
                return "commit-task"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                torbox_token="tbtoken",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(
                config,
                client={
                    "real_debrid": self.FakeProvider(),
                    "torbox": self.FakeProvider(),
                },
            )
            state.background_tasks = cast(Any, FakeTaskPool())
            state.cache = {
                "RD1": {
                    "signature": {},
                    "info": {
                        "id": "RD1",
                        "hash": "hash1",
                        "filename": "Movie",
                        "status": "downloaded",
                        "links": ["rd-a"],
                        "files": [
                            {
                                "id": "1",
                                "path": "/Movie.mkv",
                                "bytes": 1,
                                "selected": 1,
                            }
                        ],
                    },
                    "magnet": "magnet:?xt=urn:btih:hash1&dn=Movie",
                },
                "RD2": {
                    "signature": {},
                    "info": {
                        "id": "RD2",
                        "hash": "hash2",
                        "filename": "Existing",
                        "status": "downloaded",
                        "links": ["rd-b"],
                        "files": [],
                    },
                    "magnet": None,
                },
                "torbox:TB2": {
                    "signature": {},
                    "info": {
                        "id": "TB2",
                        "hash": "hash2",
                        "filename": "Existing",
                        "status": "downloaded",
                        "links": ["TB2:1"],
                        "files": [],
                    },
                    "magnet": None,
                },
            }

            task_id = state.submit_provider_migration_scan(
                "real_debrid", "torbox"
            )
            _kind, _label, work = state.background_tasks.submitted[0]
            work("test-task-id", threading.Event())

            self.assertEqual(task_id, "task-1")
            self.assertEqual(
                state.background_tasks.manual[0][:2],
                (
                    "maintenance",
                    "commit_provider_migration: 1 torrent(s) "
                    "Real-Debrid -> TorBox",
                ),
            )

    def test_provider_migration_commit_copies_to_destination_only(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []
                self.manual = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return f"task-{len(self.submitted)}"

            def submit_manual(self, kind, label, work, **_kwargs):
                self.manual.append((kind, label, work))
                return "commit-task"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                torbox_token="tbtoken",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            rd_provider = self.FakeProvider()
            tb_provider = self.FakeProvider(
                torrent_infos={
                    "NEW_TORRENT": {
                        "id": "NEW_TORRENT",
                        "hash": "hash1",
                        "filename": "Movie",
                        "status": "downloaded",
                        "links": ["NEW_TORRENT:7"],
                        "files": [
                            {
                                "id": "7",
                                "path": "/Movie.mkv",
                                "bytes": 1,
                                "selected": 1,
                            }
                        ],
                    }
                }
            )
            state = BuzzState(
                config,
                client={"real_debrid": rd_provider, "torbox": tb_provider},
            )
            state.background_tasks = cast(Any, FakeTaskPool())
            state.cache = {
                "RD1": {
                    "signature": {},
                    "info": {
                        "id": "RD1",
                        "hash": "hash1",
                        "filename": "Movie",
                        "status": "downloaded",
                        "links": ["rd-a"],
                        "files": [
                            {
                                "id": "1",
                                "path": "/Movie.mkv",
                                "bytes": 1,
                                "selected": 1,
                            }
                        ],
                    },
                    "magnet": None,
                }
            }

            task_id = state.submit_provider_migration_scan(
                "real_debrid", "torbox"
            )
            state.background_tasks.submitted[0][2]("test-task-id", threading.Event())
            _kind, _label, commit = state.background_tasks.manual[0]
            commit("test-task-id", threading.Event())

            self.assertEqual(task_id, "task-1")
            self.assertEqual(
                tb_provider.added_magnets,
                ["magnet:?xt=urn:btih:hash1"],
            )
            self.assertEqual(
                tb_provider.selected_files_calls,
                [("NEW_TORRENT", "7")],
            )
            self.assertEqual(rd_provider.deleted_ids, [])
            self.assertIn("RD1", state.cache)
            self.assertIn("torbox:NEW_TORRENT", state.cache)
            self.assertEqual(
                [item[0] for item in state.background_tasks.submitted],
                ["maintenance", "sync"],
            )

    def test_provider_migration_commit_skips_late_destination_match(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []
                self.manual = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return f"task-{len(self.submitted)}"

            def submit_manual(self, kind, label, work, **_kwargs):
                self.manual.append((kind, label, work))
                return "commit-task"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                torbox_token="tbtoken",
                provider_priority=("torbox", "real_debrid"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            rd_provider = self.FakeProvider()
            tb_provider = self.FakeProvider()
            state = BuzzState(
                config,
                client={"real_debrid": rd_provider, "torbox": tb_provider},
            )
            state.background_tasks = cast(Any, FakeTaskPool())
            state.cache = {
                "torbox:TB1": {
                    "signature": {},
                    "info": {
                        "id": "TB1",
                        "hash": "hash1",
                        "filename": "Movie",
                        "status": "downloaded",
                        "links": ["TB1:1"],
                        "files": [],
                    },
                    "magnet": "magnet:?xt=urn:btih:hash1",
                }
            }

            state.submit_provider_migration_scan("torbox", "real_debrid")
            state.background_tasks.submitted[0][2]("test-task-id", threading.Event())
            state.cache["RD1"] = {
                "signature": {},
                "info": {
                    "id": "RD1",
                    "hash": "hash1",
                    "filename": "Movie",
                    "status": "downloaded",
                    "links": [],
                    "files": [],
                },
                "magnet": None,
            }
            state.background_tasks.manual[0][2]("test-task-id", threading.Event())

            self.assertEqual(rd_provider.added_magnets, [])
            self.assertEqual(
                [item[0] for item in state.background_tasks.submitted],
                ["maintenance", "sync"],
            )

    def test_provider_migration_commit_continues_past_item_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                torbox_token="tbtoken",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            call_count = [0]

            class FlakyProvider(self.FakeProvider):
                def add_magnet(self, magnet):
                    call_count[0] += 1
                    if call_count[0] == 2:
                        raise ValueError(
                            "Failed to add TorBox magnet: {'hash': 'hash2', 'queued_id': 999}"
                        )
                    return super().add_magnet(magnet)

            rd_provider = self.FakeProvider(
                torrent_infos={
                    "RD1": {"id": "RD1", "hash": "hash1", "filename": "Movie1.mkv", "status": "downloaded", "files": []},
                    "RD2": {"id": "RD2", "hash": "hash2", "filename": "Movie2.mkv", "status": "downloaded", "files": []},
                    "RD3": {"id": "RD3", "hash": "hash3", "filename": "Movie3.mkv", "status": "downloaded", "files": []},
                }
            )
            tb_provider = FlakyProvider(
                torrent_infos={
                    "NEW_TORRENT": {"id": "NEW_TORRENT", "hash": "newhash", "filename": "New.mkv", "status": "downloaded", "files": []},
                }
            )
            state = BuzzState(
                config,
                client={"real_debrid": rd_provider, "torbox": tb_provider},
            )
            state.cache = {
                "RD1": {"signature": {}, "info": {"id": "RD1", "hash": "hash1", "filename": "Movie1.mkv", "status": "downloaded", "links": [], "files": []}, "magnet": "magnet:?xt=urn:btih:hash1"},
                "RD2": {"signature": {}, "info": {"id": "RD2", "hash": "hash2", "filename": "Movie2.mkv", "status": "downloaded", "links": [], "files": []}, "magnet": "magnet:?xt=urn:btih:hash2"},
                "RD3": {"signature": {}, "info": {"id": "RD3", "hash": "hash3", "filename": "Movie3.mkv", "status": "downloaded", "links": [], "files": []}, "magnet": "magnet:?xt=urn:btih:hash3"},
            }

            task_id = state.submit_provider_migration_scan("real_debrid", "torbox")
            scan_task = _wait_for_task(state, task_id)
            self.assertEqual(scan_task["status"], "complete", scan_task.get("error"))

            pending = [t for t in state.background_tasks.snapshot() if t["kind"] == "maintenance" and t["status"] == "pending"]
            self.assertEqual(len(pending), 1)
            state.background_tasks.start(pending[0]["id"])
            commit_task = _wait_for_task(state, pending[0]["id"])

            self.assertEqual(commit_task["status"], "complete", commit_task.get("error"))
            # add_magnet called 3 times: succeeds for item 1, fails for item 2, succeeds for item 3
            self.assertEqual(call_count[0], 3)
            # Two successful add_magnet calls despite the middle item error
            self.assertEqual(len(tb_provider.added_magnets), 2)

    def test_torrents_resolves_hash_name_from_magnet_dn(self):
        """torrents() returns the magnet dn= title when provider gives only a hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="",
                torbox_token="tbtoken",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            thash = "a7b063a88ef3f87704f071f24a615062b97ff60a"
            state.cache["torbox:3"] = {
                "signature": {},
                "info": {
                    "id": thash,
                    "hash": thash,
                    "filename": thash,
                    "original_filename": thash,
                    "status": "downloaded",
                    "links": [],
                    "files": [],
                },
                "magnet": f"magnet:?xt=urn:btih:{thash}&dn=Some.Great.Show.S01",
            }
            names = [t["name"] for t in state.torrents()]
            self.assertIn("Some.Great.Show.S01", names)

    def test_torrents_provider_filename_takes_priority_over_magnet_dn(self):
        """Provider filename wins over magnet dn= when both are present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="",
                torbox_token="tbtoken",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            thash = "a7b063a88ef3f87704f071f24a615062b97ff60a"
            state.cache["torbox:3"] = {
                "signature": {},
                "info": {
                    "id": thash,
                    "hash": thash,
                    "filename": "Provider.Title.mkv",
                    "status": "downloaded",
                    "links": [],
                    "files": [],
                },
                "magnet": f"magnet:?xt=urn:btih:{thash}&dn=Magnet.Title",
            }
            names = [t["name"] for t in state.torrents()]
            self.assertIn("Provider.Title.mkv", names)
            self.assertNotIn("Magnet.Title", names)

    def test_archive_torrents_resolves_hash_name_from_library_entries_magnet(self):
        """archive_torrents() resolves hash names via magnet dn= stored in library_entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="",
                torbox_token="tbtoken",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            thash = "b8c174b99fe4f98815f182f35b726173c08ee71b"
            # Insert a library_entries row with the magnet but no name
            with state.conn:
                state.conn.execute(
                    "INSERT INTO library_entries (hash, name, magnet, updated_at) "
                    "VALUES (?, NULL, ?, '2024-01-01T00:00:00Z')",
                    (thash, f"magnet:?xt=urn:btih:{thash}&dn=Archived.Movie.2024"),
                )
            state.archive[thash] = {
                "hash": thash,
                "name": thash,  # hash-only name that should be resolved
                "bytes": 0,
                "files": [],
                "deleted_at": "2024-06-01T00:00:00Z",
                "magnet": None,
            }
            names = [t["name"] for t in state.archive_torrents()]
            self.assertIn("Archived.Movie.2024", names)

    def test_hash_valued_filename_not_stored_in_library_entries(self):
        """A provider returning the info-hash as filename must not store that hash as name."""
        thash = "a7b063a88ef3f87704f071f24a615062b97ff60a"
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="",
                torbox_token="tbtoken",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            tb = self._hash_provider("TB1", thash, thash)
            state = BuzzState(config, client={"torbox": tb})
            state.sync(trigger_hook=False)
            row = state.conn.execute(
                "SELECT name FROM library_entries WHERE hash = ?", (thash,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row["name"])

    def test_sync_preserves_name_when_provider_loses_it(self):
        """Name written by one provider must survive after that provider is disabled."""
        thash = "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0"
        with tempfile.TemporaryDirectory() as tmpdir:
            config_both = Config(
                token="rdtoken",
                torbox_token="tbtoken",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            rd = self._hash_provider("RD1", thash, "Great.Show.S01.mkv")
            tb = self._hash_provider("TB1", thash, thash)
            state = BuzzState(config_both, client={"real_debrid": rd, "torbox": tb})
            state.sync(trigger_hook=False)
            # Confirm name was stored from RD
            row = state.conn.execute(
                "SELECT name FROM library_entries WHERE hash = ?", (thash,)
            ).fetchone()
            self.assertEqual(row["name"], "Great.Show.S01.mkv")

            # Now disable RD — only TorBox active, returning hash as filename
            config_tb_only = Config(
                token="",
                torbox_token="tbtoken",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state.apply_config(config_tb_only)
            state.clients = {"torbox": tb}
            state.sync(trigger_hook=False)

            row = state.conn.execute(
                "SELECT name FROM library_entries WHERE hash = ?", (thash,)
            ).fetchone()
            self.assertEqual(row["name"], "Great.Show.S01.mkv")
            names = [t["name"] for t in state.torrents()]
            self.assertIn("Great.Show.S01.mkv", names)

    def test_sync_uses_readable_name_from_non_winning_provider(self):
        """The effective provider can be hash-named while another provider supplies the title."""
        thash = "909fb7428de59f84d819a6bed64e7f37bbf10464"
        readable_name = (
            "Adventure Time (2010) Season 1-10 S01-S10 + Extras "
            "(1080p BluRay x265 HEVC 10bit AAC 2.0 ImE)"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="rdtoken",
                torbox_token="tbtoken",
                provider_priority=("torbox", "real_debrid"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            rd = self._hash_provider(
                "RD1",
                thash,
                readable_name,
                link="rd-link",
                file_path=f"/{readable_name}/Season 01/S01E01.mkv",
            )
            tb = self._hash_provider(
                "TB1",
                thash,
                thash,
                link="tb-link",
                file_path=f"/{readable_name}/Season 01/S01E01.mkv",
            )
            state = BuzzState(
                config,
                client={"real_debrid": rd, "torbox": tb},
            )
            state.sync(trigger_hook=False)

            row = state.conn.execute(
                "SELECT name FROM library_entries WHERE hash = ?", (thash,)
            ).fetchone()
            self.assertEqual(row["name"], readable_name)
            self.assertEqual(state.torrents()[0]["name"], readable_name)
            self.assertIn("torbox:TB1", state.cache)
            self.assertEqual(
                state.cache["torbox:TB1"]["info"]["display_name"],
                readable_name,
            )
            self.assertTrue(
                any(
                    path.startswith(f"shows/{readable_name}/")
                    for path in state.snapshot["files"]
                )
            )

    def test_sync_updates_name_when_better_value_available(self):
        """A null/hash name in library_entries is overwritten when a provider returns a real name."""
        thash = "c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1"
        with tempfile.TemporaryDirectory() as tmpdir:
            config_tb = Config(
                token="",
                torbox_token="tbtoken",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            tb = self._hash_provider("TB1", thash, thash)
            state = BuzzState(config_tb, client={"torbox": tb})
            state.sync(trigger_hook=False)
            row = state.conn.execute(
                "SELECT name FROM library_entries WHERE hash = ?", (thash,)
            ).fetchone()
            self.assertIsNone(row["name"])

            # Now enable RD which returns a readable name for the same hash
            config_both = Config(
                token="rdtoken",
                torbox_token="tbtoken",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            rd = self._hash_provider("RD1", thash, "Great.Movie.2024.mkv")
            state.apply_config(config_both)
            state.clients = {"real_debrid": rd, "torbox": tb}
            state.sync(trigger_hook=False)

            row = state.conn.execute(
                "SELECT name FROM library_entries WHERE hash = ?", (thash,)
            ).fetchone()
            self.assertEqual(row["name"], "Great.Movie.2024.mkv")

    def test_sync_overwrites_hash_valued_name_with_real_name(self):
        """A hash string already stored as name must be replaced by a real name on sync."""
        thash = "d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2"
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="rdtoken",
                torbox_token="",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            rd = self._hash_provider("RD1", thash, "Some.Show.S01.mkv")
            state = BuzzState(config, client={"real_debrid": rd})
            # Pre-populate library_entries with the hash as the name (simulating old data)
            with state.conn:
                state.conn.execute(
                    "INSERT INTO library_entries (hash, name, bytes, files_json, magnet, updated_at) "
                    "VALUES (?, ?, 0, '[]', NULL, '2024-01-01T00:00:00Z')",
                    (thash, thash),
                )
            state.sync(trigger_hook=False)
            row = state.conn.execute(
                "SELECT name FROM library_entries WHERE hash = ?", (thash,)
            ).fetchone()
            self.assertEqual(row["name"], "Some.Show.S01.mkv")

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
            state = BuzzState(config, client=self._create_fake_provider())
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
            state = BuzzState(config, client=self._create_fake_provider())
            first = state.sync(trigger_hook=False)
            second = state.sync(trigger_hook=False)

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(second["changed_paths"], [])

    def test_identical_sync_skips_rewriting_cache(self):
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
            state = BuzzState(config, client=self._create_fake_provider())

            with patch.object(
                state, "_save_cache", wraps=state._save_cache
            ) as save_cache:
                state.sync(trigger_hook=False)
                state.sync(trigger_hook=False)

            self.assertEqual(save_cache.call_count, 1)

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
            client = self.FakeProvider(
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
            lambda added, removed, updated, synced, providers=None, root_providers=None:
            BuzzState._format_change_message(
                added, removed, updated, synced, providers, root_providers
            )
        )
        poller = Poller(state)

        message = poller._format_change_message(
            [
                "movies/Galaxy.Quest.1999.2160p.UHD.BluRay.x265-TESTGROUP",
                "movies/Starfall.2001.EXTENDED.2160p.UHD.BluRay.x265-TESTGROUP",
            ],
            [],
            [],
            96,
            ["real_debrid"],
            {
                "movies/Galaxy.Quest.1999.2160p.UHD.BluRay.x265-TESTGROUP": "real_debrid",
                "movies/Starfall.2001.EXTENDED.2160p.UHD.BluRay.x265-TESTGROUP": "real_debrid",
            },
        )

        self.assertEqual(
            message,
            "\n".join(
                [
                    "detected changes (2 torrents):",
                    "  +2 added",
                    "    movies/Galaxy.Quest.1999.2160p.UHD.BluRay.x265-TESTGROUP [real-debrid]",
                    "    movies/Starfall.2001.EXTENDED.2160p.UHD.BluRay.x265-TESTGROUP [real-debrid]",
                ]
            ),
        )

    def test_poller_does_not_emit_duplicate_change_event(self):
        state = MagicMock()
        state.config.provider_poll_interval_secs = 0
        state.sync.return_value = {
            "changed": True,
            "added_paths": ["shows/Static Dreams S07"],
            "removed_paths": [],
            "updated_paths": [],
            "synced_torrents": 100,
        }
        poller = Poller(state)
        poller._wake_event.wait = MagicMock(return_value=None)
        poller._stop_event.is_set = MagicMock(side_effect=[False, True])

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
            state = BuzzState(config, client=self._create_fake_provider())
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
            state = BuzzState(config, client=self._create_fake_provider())
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
            state = BuzzState(config, client=self._create_fake_provider())

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                report = state.sync()

            self.assertTrue(report["changed"])
            logged = stdout.getvalue()
            self.assertIn("detected changes (1 torrents):", logged)
            self.assertIn("+1 added", logged)
            self.assertIn("movies/Movie 2026 [real-debrid]", logged)
            self.assertIn('"event": "library_update"', logged)

    def test_sync_change_event_labels_only_provider_with_changes(self):
        """When only one provider has new content the log names that provider, not all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="tbtoken",
                token="rdtoken",
                provider_priority=("real_debrid", "torbox"),
                provider_poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="",
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
                curator_url="",
            )
            rd_torrent = {
                "id": "RD1",
                "filename": "Movie.2026.1080p.mkv",
                "bytes": 123,
                "progress": 100,
                "status": "downloaded",
                "ended": "2026-01-01T00:00:00Z",
                "links": ["https://example.invalid/rd"],
            }
            rd_info = {
                "id": "RD1",
                "hash": "aaaa",
                "status": "downloaded",
                "filename": "Movie.2026.1080p.mkv",
                "original_filename": "Movie 2026",
                "links": ["https://example.invalid/rd"],
                "files": [{"id": 1, "path": "/Movie.2026.1080p.mkv", "bytes": 123, "selected": 1}],
            }
            tb_torrent = {
                "id": "TB1",
                "filename": "Wacky.Critters.S01.mkv",
                "bytes": 456,
                "progress": 100,
                "status": "downloaded",
                "ended": "2026-01-02T00:00:00Z",
                "links": ["https://example.invalid/tb"],
            }
            tb_info = {
                "id": "TB1",
                "hash": "bbbb",
                "status": "downloaded",
                "filename": "Wacky.Critters.S01.mkv",
                "original_filename": "Wacky Critters S01",
                "links": ["https://example.invalid/tb"],
                "files": [{"id": 1, "path": "/Wacky.Critters.S01E01.mkv", "bytes": 456, "selected": 1}],
            }
            rd_provider = self.FakeProvider([rd_torrent], {"RD1": rd_info})
            tb_provider = self.FakeProvider([], {})
            state = BuzzState(config, client={"real_debrid": rd_provider, "torbox": tb_provider})
            state.sync(trigger_hook=False)

            # now only torbox gets a new torrent
            tb_provider.torrents_list = [tb_torrent]
            tb_provider.torrent_infos = {"TB1": tb_info}

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                state.sync(trigger_hook=False)

            logged = stdout.getvalue()
            self.assertIn("detected changes (1 torrents):", logged)
            self.assertIn("shows/Wacky Critters S01 [torbox]", logged)
            self.assertNotIn("[real-debrid]", logged)

    def test_sync_change_event_labels_added_and_removed_path_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="tbtoken",
                token="rdtoken",
                provider_priority=("real_debrid", "torbox"),
                provider_poll_interval_secs=10,
                bind="127.0.0.1",
                port=9999,
                state_dir=tmpdir,
                hook_command="",
                enable_unplayable_dir=True,
                request_timeout_secs=30,
                user_agent="buzz-tests",
                version_label="buzz/test",
                curator_url="",
            )
            rd_torrent = {
                "id": "RD1",
                "filename": "Synthetic.Feature.2026.mkv",
                "bytes": 123,
                "progress": 100,
                "status": "downloaded",
                "ended": "2026-01-01T00:00:00Z",
                "links": ["https://example.invalid/rd"],
            }
            rd_info = {
                "id": "RD1",
                "hash": "aaaa",
                "status": "downloaded",
                "filename": "Synthetic.Feature.2026.mkv",
                "original_filename": "Synthetic.Feature (Variant).2026.mkv",
                "links": ["https://example.invalid/rd"],
                "files": [
                    {
                        "id": 1,
                        "path": "/Synthetic.Feature.2026.mkv",
                        "bytes": 123,
                        "selected": 1,
                    }
                ],
            }
            tb_torrent = {
                "id": "TB1",
                "filename": "Synthetic.Feature.2026.PROPER.HDR.2160p.WEB.h265-FIXTURE",
                "bytes": 456,
                "progress": 100,
                "status": "downloaded",
                "ended": "2026-01-02T00:00:00Z",
                "links": ["TB1:1"],
            }
            tb_info = {
                "id": "TB1",
                "hash": "bbbb",
                "status": "downloaded",
                "filename": "Synthetic.Feature.2026.PROPER.HDR.2160p.WEB.h265-FIXTURE",
                "original_filename": "Synthetic.Feature.2026.PROPER.HDR.2160p.WEB.h265-FIXTURE",
                "links": ["TB1:1"],
                "files": [
                    {
                        "id": 1,
                        "path": "/Synthetic.Feature.2026.PROPER.HDR.2160p.WEB.h265-FIXTURE.mkv",
                        "bytes": 456,
                        "selected": 1,
                    }
                ],
            }
            rd_provider = self.FakeProvider([rd_torrent], {"RD1": rd_info})
            tb_provider = self.FakeProvider([], {})
            state = BuzzState(
                config,
                client={"real_debrid": rd_provider, "torbox": tb_provider},
            )
            state.sync(trigger_hook=False)
            rd_provider.torrents_list = []
            tb_provider.torrents_list = [tb_torrent]
            tb_provider.torrent_infos = {"TB1": tb_info}

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                state.sync(trigger_hook=False)

            logged = stdout.getvalue()
            self.assertIn("detected changes (2 torrents):", logged)
            self.assertIn("+1 added", logged)
            self.assertIn(
                "movies/Synthetic.Feature.2026.PROPER.HDR.2160p.WEB.h265-FIXTURE [torbox]",
                logged,
            )
            self.assertIn("-1 removed", logged)
            self.assertIn(
                "movies/Synthetic.Feature (Variant).2026.mkv [real-debrid]",
                logged,
            )

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
                "filename": "Static.Dreams.S07.2160p.mkv",
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
                "filename": "Static.Dreams.S07.2160p.mkv",
                "original_filename": "Static Dreams S07",
                "links": ["https://example.invalid/show"],
                "files": [
                    {
                        "id": 1,
                        "path": "/Static.Dreams.S07E01.mkv",
                        "bytes": 456,
                        "selected": 1,
                    }
                ],
            }
            client = self.FakeProvider(
                torrents_list=[movie_summary],
                torrent_infos={"MOVIE1": movie_info, "SHOW1": show_info},
            )
            state = BuzzState(config, client=client)
            state.sync(trigger_hook=False)
            client.torrents_list = [movie_summary, show_summary]
            enqueued = []
            state._enqueue_hook = lambda changed_roots: enqueued.append(
                list(changed_roots)
            )

            report = state.sync()

            self.assertEqual(report["added_paths"], ["shows/Static Dreams S07"])
            self.assertEqual(report["removed_paths"], [])
            self.assertEqual(report["updated_paths"], [])
            self.assertEqual(enqueued, [["shows/Static Dreams S07"]])

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
                "filename": "Static.Dreams.S07.2160p.mkv",
                "bytes": 456,
                "progress": 100,
                "status": "downloaded",
                "ended": "2026-01-02T00:00:00Z",
                "links": ["https://example.invalid/show"],
            }
            stale_info = {
                "id": "SHOW1",
                "status": "downloaded",
                "filename": "Static.Dreams.S07.2160p.mkv",
                "links": [],
                "files": [
                    {
                        "id": 1,
                        "path": "/Static.Dreams.S07E01.mkv",
                        "bytes": 456,
                        "selected": 1,
                    }
                ],
            }
            fresh_info = {
                **stale_info,
                "links": ["https://example.invalid/show"],
            }
            client = self.FakeProvider(
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

            self.assertEqual(client.info_calls, ["SHOW1"])
            self.assertEqual(
                report["added_paths"],
                ["shows/Static.Dreams.S07.2160p.mkv"],
            )
            self.assertIn(
                "shows/Static.Dreams.S07.2160p.mkv/Static.Dreams.S07E01.mkv",
                state.snapshot["files"],
            )

    def test_sync_moves_upstream_removed_torrent_to_archive(self):
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
            state = BuzzState(config, client=self.FakeProvider([], {}))
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
            self.assertIn("ABC123HASH", state.archive)
            self.assertEqual(
                state.archive["ABC123HASH"]["name"],
                "Movie.2026.1080p.mkv",
            )
            self.assertEqual(
                state.archive["ABC123HASH"]["magnet"],
                "magnet:?xt=urn:btih:ABC123HASH&dn=Movie",
            )

    def test_sync_archives_new_torrents_on_first_sync(self):
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
            torrents_list = [{"id": "T1", "filename": "Movie.mkv", "status": "downloaded", "bytes": 500}]
            torrent_infos = {
                "T1": {
                    "id": "T1",
                    "hash": "aabbccdd1122",
                    "filename": "Movie.mkv",
                    "bytes": 500,
                    "status": "downloaded",
                    "files": [{"id": "1", "path": "/Movie.mkv", "bytes": 500, "selected": True}],
                    "links": ["https://cdn.example.invalid/movie.mkv"],
                }
            }
            state = BuzzState(config, client=self.FakeProvider(torrents_list, torrent_infos))

            self.assertEqual(state.archive, {})
            state.sync(trigger_hook=False)

            self.assertIn("aabbccdd1122", state.archive)
            self.assertEqual(state.archive["aabbccdd1122"]["name"], "Movie.mkv")

    def test_sync_does_not_overwrite_existing_archive_entry(self):
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
            torrents_list = [{"id": "T1", "filename": "Movie.mkv", "status": "downloaded", "bytes": 500}]
            torrent_infos = {
                "T1": {
                    "id": "T1",
                    "hash": "aabbccdd1122",
                    "filename": "Movie.mkv",
                    "bytes": 500,
                    "status": "downloaded",
                    "files": [{"id": "1", "path": "/Movie.mkv", "bytes": 500, "selected": True}],
                    "links": ["https://cdn.example.invalid/movie.mkv"],
                }
            }
            state = BuzzState(config, client=self.FakeProvider(torrents_list, torrent_infos))
            original_entry = {
                "hash": "aabbccdd1122",
                "name": "Old Name",
                "bytes": 100,
                "files": [],
                "deleted_at": "2020-01-01T00:00:00Z",
                "magnet": None,
            }
            state.archive["aabbccdd1122"] = original_entry

            state.sync(trigger_hook=False)

            self.assertEqual(state.archive["aabbccdd1122"]["deleted_at"], "2020-01-01T00:00:00Z")
            self.assertEqual(state.archive["aabbccdd1122"]["name"], "Old Name")

    def test_sync_archives_newly_seen_torrent_on_long_running_instance(self):
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
            torrents_list = [
                {"id": "T1", "filename": "OldMovie.mkv", "status": "downloaded", "bytes": 100},
                {"id": "T2", "filename": "NewMovie.mkv", "status": "downloaded", "bytes": 200},
            ]
            torrent_infos = {
                "T1": {
                    "id": "T1",
                    "hash": "hash0000aaaa",
                    "filename": "OldMovie.mkv",
                    "bytes": 100,
                    "status": "downloaded",
                    "files": [{"id": "1", "path": "/OldMovie.mkv", "bytes": 100, "selected": True}],
                    "links": ["https://cdn.example.invalid/old.mkv"],
                },
                "T2": {
                    "id": "T2",
                    "hash": "hash1111bbbb",
                    "filename": "NewMovie.mkv",
                    "bytes": 200,
                    "status": "downloaded",
                    "files": [{"id": "2", "path": "/NewMovie.mkv", "bytes": 200, "selected": True}],
                    "links": ["https://cdn.example.invalid/new.mkv"],
                },
            }
            state = BuzzState(config, client=self.FakeProvider(torrents_list, torrent_infos))
            # Simulate T1 already known (in archive from previous sync)
            state.archive["hash0000aaaa"] = {
                "hash": "hash0000aaaa",
                "name": "OldMovie.mkv",
                "bytes": 100,
                "files": [],
                "deleted_at": "2024-01-01T00:00:00Z",
                "magnet": None,
            }

            state.sync(trigger_hook=False)

            # T2 is new — must be archived
            self.assertIn("hash1111bbbb", state.archive)
            self.assertEqual(state.archive["hash1111bbbb"]["name"], "NewMovie.mkv")
            # T1's existing archive entry is untouched
            self.assertEqual(state.archive["hash0000aaaa"]["deleted_at"], "2024-01-01T00:00:00Z")

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
            client = self._create_fake_provider()
            client.delete_error = ProviderDeleteError(404, "not found")
            state = BuzzState(config, client=client)
            state.sync(trigger_hook=False)
            state.cache["TORRENT1"]["info"]["hash"] = "ABC123HASH"

            task_id = state.delete_torrent("TORRENT1")
            task = _wait_for_task(state, task_id)

            self.assertEqual(task["status"], "complete")
            self.assertNotIn("TORRENT1", state.cache)
            self.assertIn("ABC123HASH", state.archive)

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
            client = self._create_fake_provider()
            client.delete_error = ProviderDeleteError(500, "server error")
            state = BuzzState(config, client=client)
            state.sync(trigger_hook=False)
            state.cache["TORRENT1"]["info"]["hash"] = "ABC123HASH"

            task_id = state.delete_torrent("TORRENT1")
            task = _wait_for_task(state, task_id)
            self.assertEqual(task["status"], "failed")
            self.assertIn("Failed to delete torrent", task["error"])

    def test_delete_torbox_torrent_uses_raw_id_and_keeps_generic_422_hard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            torbox = self.FakeProvider(
                delete_error=ProviderDeleteError(422, "bad request")
            )
            state = BuzzState(config, client={"torbox": torbox})
            state.cache["torbox:TB1"] = {
                "signature": {},
                "info": {
                    "id": "TB1",
                    "hash": "hash",
                    "filename": "Movie.mkv",
                    "files": [],
                },
            }

            task_id = state.delete_torrent("torbox:TB1")
            task = _wait_for_task(state, task_id)

            self.assertEqual(task["status"], "failed")
            self.assertIn("Failed to delete torrent", task["error"])
            self.assertEqual(torbox.deleted_ids, ["TB1"])
            self.assertIn("torbox:TB1", state.cache)

    def test_delete_torrent_worker_respects_cancellation_before_delete(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []

            def submit(self, kind, label, work):
                self.submitted.append((kind, label, work))
                return "task-delete"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            torbox = self.FakeProvider()
            state = BuzzState(config, client={"torbox": torbox})
            state.background_tasks = cast(Any, FakeTaskPool())
            state.cache["torbox:TB1"] = {
                "signature": {},
                "info": {
                    "id": "TB1",
                    "hash": "hash",
                    "filename": "Movie.mkv",
                    "files": [],
                },
            }

            task_id = state.delete_torrent("torbox:TB1")
            _kind, _label, run_delete = state.background_tasks.submitted[0]
            cancel_event = threading.Event()
            cancel_event.set()

            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                run_delete("test-task-id", cancel_event)

            self.assertEqual(task_id, "task-delete")
            self.assertEqual(torbox.deleted_ids, [])
            self.assertIn("torbox:TB1", state.cache)

    def test_delete_torrent_removes_all_provider_copies_by_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                [{"id": "RD1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["rd-link"]}],
                {
                    "RD1": {
                        "id": "RD1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["rd-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            torbox = self.FakeProvider(
                [{"id": "TB1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["tb-link"]}],
                {
                    "TB1": {
                        "id": "TB1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["tb-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )
            state.sync(trigger_hook=False)

            # RD wins; cache has one entry
            self.assertEqual(len(state.cache), 1)
            winning_key = next(iter(state.cache))

            task_id = state.delete_torrent(winning_key)
            task = _wait_for_task(state, task_id)

            self.assertEqual(task["status"], "complete")
            # Both providers must have been asked to delete
            self.assertEqual(real_debrid.deleted_ids, ["RD1"])
            self.assertEqual(torbox.deleted_ids, ["TB1"])
            self.assertNotIn(winning_key, state.cache)
            self.assertIn("samehash", state.archive)

    def test_delete_torrent_multi_provider_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                [{"id": "RD1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["rd-link"]}],
                {
                    "RD1": {
                        "id": "RD1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["rd-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            torbox = self.FakeProvider(
                [{"id": "TB1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["tb-link"]}],
                {
                    "TB1": {
                        "id": "TB1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["tb-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )
            state.sync(trigger_hook=False)
            state.close()

            # Fresh state on same state_dir — no re-sync
            real_debrid2 = self.FakeProvider()
            torbox2 = self.FakeProvider()
            state2 = BuzzState(
                config,
                client={"real_debrid": real_debrid2, "torbox": torbox2},
            )
            winning_key = next(iter(state2.cache))
            task_id = state2.delete_torrent(winning_key)
            task = _wait_for_task(state2, task_id)

            self.assertEqual(task["status"], "complete")
            self.assertEqual(real_debrid2.deleted_ids, ["RD1"])
            self.assertEqual(torbox2.deleted_ids, ["TB1"])

    def test_delete_torrent_secondary_provider_hard_failure_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                [{"id": "RD1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["rd-link"]}],
                {
                    "RD1": {
                        "id": "RD1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["rd-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            torbox = self.FakeProvider(
                [{"id": "TB1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["tb-link"]}],
                {
                    "TB1": {
                        "id": "TB1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["tb-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
                delete_error=ProviderDeleteError(500, "torbox server error"),
            )
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )
            state.sync(trigger_hook=False)
            winning_key = next(iter(state.cache))

            task_id = state.delete_torrent(winning_key)
            task = _wait_for_task(state, task_id)

            # Primary (RD) succeeded; secondary (torbox) 500 is non-fatal
            self.assertEqual(task["status"], "complete")
            self.assertEqual(real_debrid.deleted_ids, ["RD1"])
            self.assertEqual(torbox.deleted_ids, ["TB1"])
            self.assertNotIn(winning_key, state.cache)
            self.assertIn("samehash", state.archive)

    def test_delete_torrent_primary_hard_failure_aborts_even_with_secondary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                [{"id": "RD1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["rd-link"]}],
                {
                    "RD1": {
                        "id": "RD1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["rd-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
                delete_error=ProviderDeleteError(500, "rd server error"),
            )
            torbox = self.FakeProvider(
                [{"id": "TB1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["tb-link"]}],
                {
                    "TB1": {
                        "id": "TB1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["tb-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )
            state.sync(trigger_hook=False)
            winning_key = next(iter(state.cache))

            task_id = state.delete_torrent(winning_key)
            task = _wait_for_task(state, task_id)

            self.assertEqual(task["status"], "failed")
            self.assertIn("Failed to delete torrent", task["error"])
            self.assertIn(winning_key, state.cache)

    def test_provider_links_populated_after_sync_and_cleared_after_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            real_debrid = self.FakeProvider(
                [{"id": "RD1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["rd-link"]}],
                {
                    "RD1": {
                        "id": "RD1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["rd-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            torbox = self.FakeProvider(
                [{"id": "TB1", "filename": "Movie.mkv", "status": "downloaded", "progress": 100, "links": ["tb-link"]}],
                {
                    "TB1": {
                        "id": "TB1",
                        "hash": "samehash",
                        "filename": "Movie.mkv",
                        "status": "downloaded",
                        "links": ["tb-link"],
                        "files": [{"id": 1, "path": "/Movie.mkv", "bytes": 1, "selected": 1}],
                    }
                },
            )
            state = BuzzState(
                config,
                client={"real_debrid": real_debrid, "torbox": torbox},
            )
            state.sync(trigger_hook=False)

            from buzz.core import db as buzz_db
            links_after_sync = buzz_db.load_provider_links_by_hash(state.conn, "samehash")
            self.assertEqual(
                sorted(links_after_sync),
                [("real_debrid", "RD1"), ("torbox", "TB1")],
            )

            winning_key = next(iter(state.cache))
            task_id = state.delete_torrent(winning_key)
            _wait_for_task(state, task_id)

            links_after_delete = buzz_db.load_provider_links_by_hash(state.conn, "samehash")
            self.assertEqual(links_after_delete, [])

    def test_torbox_detail_info_preserves_file_stream_refs(self):
        detail = ProviderTorrentDetail(
            id="TB1",
            hash="hash",
            name="Sitcom",
            original_name="Sitcom",
            bytes=100,
            progress=100,
            status="downloaded",
            files=(
                ProviderFile(
                    id="1",
                    path="/Sitcom.2000.S01E01.mkv",
                    bytes=100,
                    selected=False,
                    stream_ref="TB1:1",
                ),
            ),
        )

        info = BuzzState._detail_to_info(detail)

        self.assertEqual(info["files"][0]["stream_ref"], "TB1:1")

    def test_torbox_select_files_rebuilds_playable_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            torbox = self.FakeProvider()
            state = BuzzState(config, client={"torbox": torbox})
            state.cache["torbox:TB1"] = {
                "signature": {},
                "info": {
                    "id": "TB1",
                    "hash": "hash",
                    "status": "downloaded",
                    "filename": "Sitcom",
                    "links": [],
                    "files": [
                        {
                            "id": "1",
                            "path": "/Sitcom.2000.S01E01.mkv",
                            "bytes": 100,
                            "selected": 0,
                            "stream_ref": "TB1:1",
                        },
                        {
                            "id": "2",
                            "path": "/sample.txt",
                            "bytes": 10,
                            "selected": 0,
                            "stream_ref": "TB1:2",
                        },
                    ],
                },
            }

            state.select_files("torbox:TB1", ["1"])

            info = state.cache["torbox:TB1"]["info"]
            self.assertEqual(info["links"], ["TB1:1"])
            live_node = state.lookup("shows/Sitcom/Sitcom.2000.S01E01.mkv")
            if live_node is None:
                self.fail("Expected selected TorBox file in live snapshot")
            self.assertEqual(live_node["source_url"], "TB1:1")
            self.assertEqual(
                state.stream_sources["TB1:1"],
                [{"provider": "torbox", "source_url": "TB1:1"}],
            )
            snapshot, _changed = state.builder.build([info])
            episode_path = "shows/Sitcom/Sitcom.2000.S01E01.mkv"
            self.assertEqual(
                snapshot["files"][episode_path]["source_url"],
                "TB1:1",
            )
            self.assertNotIn(
                "__unplayable__/Sitcom/Sitcom.2000.S01E01.mkv",
                snapshot["files"],
            )

    def test_torbox_cached_selection_replay_rebuilds_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            info = {
                "id": "TB1",
                "hash": "hash",
                "status": "downloaded",
                "filename": "Sitcom",
                "links": [],
                "files": [
                    {
                        "id": "1",
                        "path": "/Sitcom.2000.S01E01.mkv",
                        "bytes": 100,
                        "selected": 0,
                        "stream_ref": "TB1:1",
                    }
                ],
            }
            state.file_selections["hash"] = {"Sitcom.2000.S01E01.mkv"}

            state._apply_cached_file_selection("torbox", "torbox:TB1", info)

            self.assertEqual(info["files"][0]["selected"], 1)
            self.assertEqual(info["links"], ["TB1:1"])

    def test_matching_destination_file_ids_exact_path(self):
        info = {
            "files": [
                {"id": "1", "path": "/Season 01/Ep1.mkv"},
                {"id": "2", "path": "/Season 01/Ep2.mkv"},
            ]
        }

        selected = BuzzState._matching_destination_file_ids(
            info, {"Season 01/Ep1.mkv"}
        )

        self.assertEqual(selected, ["1"])

    def test_matching_destination_file_ids_suffix_reconciles_root(self):
        info = {
            "files": [
                {"id": "1", "path": "/Series (2010)/Season 01/Ep1.mkv"},
                {"id": "2", "path": "/Series (2010)/Season 01/Ep2.mkv"},
            ]
        }

        selected = BuzzState._matching_destination_file_ids(
            info, {"Season 01/Ep1.mkv"}
        )

        self.assertEqual(selected, ["1"])

    def test_matching_destination_file_ids_uses_full_suffix_segments(self):
        info = {
            "files": [
                {"id": "1", "path": "/Series/A/ep.mkv"},
                {"id": "2", "path": "/Series/B/ep.mkv"},
            ]
        }

        selected = BuzzState._matching_destination_file_ids(
            info, {"A/ep.mkv"}
        )

        self.assertEqual(selected, ["1"])

    def test_matching_destination_file_ids_selects_ambiguous_suffix(self):
        info = {
            "files": [
                {"id": "1", "path": "/Series A/Season 01/Ep1.mkv"},
                {"id": "2", "path": "/Series B/Season 01/Ep1.mkv"},
            ]
        }

        selected = BuzzState._matching_destination_file_ids(
            info, {"Season 01/Ep1.mkv"}
        )

        self.assertEqual(selected, ["1", "2"])

    def test_matching_destination_file_ids_exact_match_gates_suffix(self):
        info = {
            "files": [
                {"id": "1", "path": "/Ep1.mkv"},
                {"id": "2", "path": "/Season 01/Ep1.mkv"},
            ]
        }

        selected = BuzzState._matching_destination_file_ids(
            info, {"Ep1.mkv"}
        )

        self.assertEqual(selected, ["1"])

    def test_matching_destination_file_ids_alias_reconciles_renamed_path(self):
        info = {
            "files": [
                {
                    "id": "258",
                    "path": (
                        "/Season 06/Adventure Time (2008) - S06E16 - "
                        "Joshua & Margaret Investigations "
                        "(1080p BluRay x265 ImE).mkv"
                    ),
                }
            ]
        }

        selected = BuzzState._matching_destination_file_ids(
            info,
            {
                "Adventure Time (2008) - S06E16 - "
                "Joshua and Margaret Investigations "
                "(1080p BluRay x265 ImE).mkv"
            },
        )

        self.assertEqual(selected, ["258"])

    def test_matching_destination_file_ids_selects_ambiguous_alias(self):
        info = {
            "files": [
                {"id": "1", "path": "/A/Joshua & Margaret.mkv"},
                {"id": "2", "path": "/B/Joshua & Margaret.mkv"},
            ]
        }

        selected = BuzzState._matching_destination_file_ids(
            info, {"Joshua and Margaret.mkv"}
        )

        self.assertEqual(selected, ["1", "2"])

    def test_matching_destination_file_ids_warns_unresolved_once(self):
        info = {"files": [{"id": "1", "path": "/A/Ep1.mkv"}]}
        warned: set[tuple[str, str]] = set()

        with patch("buzz.core.state.record_event") as mock_record:
            BuzzState._matching_destination_file_ids(
                info,
                {"missing.mkv"},
                thash="abc123",
                warned_paths=warned,
            )
            BuzzState._matching_destination_file_ids(
                info,
                {"missing.mkv"},
                thash="abc123",
                warned_paths=warned,
            )

        self.assertEqual(mock_record.call_count, 1)

    def test_cached_selection_suffix_replay_rebuilds_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            info = {
                "id": "RD1",
                "hash": "hash",
                "status": "downloaded",
                "filename": "Series",
                "links": [],
                "files": [
                    {
                        "id": "1",
                        "path": "/Series (2010)/Season 01/Ep1.mkv",
                        "bytes": 100,
                        "selected": 0,
                        "stream_ref": "rd-link-1",
                    },
                    {
                        "id": "2",
                        "path": "/Series (2010)/Season 01/Ep2.mkv",
                        "bytes": 100,
                        "selected": 0,
                        "stream_ref": "rd-link-2",
                    },
                ],
            }
            state.file_selections["hash"] = {"Season 01/Ep1.mkv"}

            state._apply_cached_file_selection("real_debrid", "RD1", info)

            self.assertEqual(info["files"][0]["selected"], 1)
            self.assertEqual(info["files"][1]["selected"], 0)
            self.assertEqual(info["links"], ["rd-link-1"])
            state.close()

    def test_category_override_moves_snapshot_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            cache_key = "RD1"
            info = {
                "id": "RD1",
                "hash": "abc123",
                "status": "downloaded",
                "filename": "Movie Pack",
                "links": ["rd-link-1"],
                "files": [
                    {
                        "id": "1",
                        "path": "/Movie.Pack.2020.mkv",
                        "bytes": 100,
                        "selected": 1,
                        "stream_ref": "rd-link-1",
                    }
                ],
            }
            state.cache[cache_key] = {
                "signature": {},
                "info": info,
                "magnet": None,
            }
            state._refresh_snapshot_from_cache()
            self.assertIn(
                "movies/Movie Pack/Movie.Pack.2020.mkv",
                state.snapshot["files"],
            )

            state.set_torrent_category(cache_key, "anime")

            self.assertEqual(state.category_overrides["abc123"], "anime")
            self.assertIn(
                "anime/Movie Pack/Movie.Pack.2020.mkv",
                state.snapshot["files"],
            )
            self.assertNotIn(
                "movies/Movie Pack/Movie.Pack.2020.mkv",
                state.snapshot["files"],
            )
            state.close()

    def test_config_favorites_default_seed_and_toggle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = db.connect(Path(tmpdir) / "buzz.sqlite")
            db.apply_migrations(conn)
            # subtitles is favorited by default (seeded by migration).
            self.assertEqual(db.load_config_favorites(conn), {"subtitles"})

            db.save_config_favorite(conn, "provider", True)
            self.assertEqual(
                db.load_config_favorites(conn), {"subtitles", "provider"}
            )

            db.save_config_favorite(conn, "subtitles", False)
            self.assertEqual(db.load_config_favorites(conn), {"provider"})

            # re-applying migrations must not re-seed subtitles.
            db.apply_migrations(conn)
            self.assertEqual(db.load_config_favorites(conn), {"provider"})
            conn.close()

    def test_config_favorites_persist_across_state_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            self.assertEqual(state.config_favorites, {"subtitles"})

            self.assertTrue(state.toggle_config_favorite("provider"))
            self.assertIn("provider", state.config_favorites)
            self.assertFalse(state.toggle_config_favorite("subtitles"))
            self.assertNotIn("subtitles", state.config_favorites)
            state.close()

            reopened = BuzzState(config, client=self.FakeProvider())
            self.assertEqual(reopened.config_favorites, {"provider"})
            reopened.close()

    def test_subtitle_query_override_set_get_and_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            cache_key = "RD1"
            info = {
                "id": "RD1",
                "hash": "abc123",
                "status": "downloaded",
                "filename": "Movie Pack",
                "links": ["rd-link-1"],
                "files": [
                    {
                        "id": "1",
                        "path": "/Movie.Pack.2020.mkv",
                        "bytes": 100,
                        "selected": 1,
                        "stream_ref": "rd-link-1",
                    }
                ],
            }
            state.cache[cache_key] = {
                "signature": {},
                "info": info,
                "magnet": None,
            }

            # Unset by default.
            self.assertEqual(
                state.subtitle_query_override(cache_key, "/Movie.Pack.2020.mkv"),
                "",
            )

            state.set_subtitle_query_override(
                cache_key, "/Movie.Pack.2020.mkv", "Real Title"
            )
            self.assertEqual(
                state.subtitle_query_overrides[("abc123", "Movie.Pack.2020.mkv")],
                "Real Title",
            )
            # Path is normalized; lookup works with the leading-slash form too.
            self.assertEqual(
                state.subtitle_query_override(cache_key, "/Movie.Pack.2020.mkv"),
                "Real Title",
            )

            # Persisted to the DB and reloaded on a fresh state instance.
            reloaded = db.load_subtitle_query_overrides(state.conn)
            self.assertEqual(
                reloaded[("abc123", "Movie.Pack.2020.mkv")], "Real Title"
            )

            # Empty value clears the override.
            state.set_subtitle_query_override(
                cache_key, "/Movie.Pack.2020.mkv", ""
            )
            self.assertEqual(
                state.subtitle_query_override(cache_key, "/Movie.Pack.2020.mkv"),
                "",
            )
            self.assertEqual(db.load_subtitle_query_overrides(state.conn), {})
            state.close()

    def test_curator_title_override_set_get_clear_and_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            cache_key = "RD1"
            state.cache[cache_key] = {
                "signature": {},
                "info": {
                    "id": "RD1",
                    "hash": "abc123",
                    "status": "downloaded",
                    "filename": "Movie Pack",
                    "links": ["rd-link-1"],
                    "files": [
                        {
                            "id": "1",
                            "path": "/Movie.Pack.2020.mkv",
                            "bytes": 100,
                            "selected": 1,
                            "stream_ref": "rd-link-1",
                        }
                    ],
                },
                "magnet": None,
            }

            with patch.object(state, "_enqueue_hook") as enqueue:
                state.set_curator_title_override(
                    cache_key,
                    {
                        "kind": "movie",
                        "title": "Real Title",
                        "year": "2020",
                        "imdbid": "tt1234567",
                    },
                )

            self.assertEqual(
                state.curator_title_override(cache_key),
                {
                    "kind": "movie",
                    "title": "Real Title",
                    "year": 2020,
                    "provider_ids": {"imdbid": "tt1234567"},
                },
            )
            self.assertEqual(
                db.load_curator_title_overrides(state.conn),
                {
                    "abc123": {
                        "kind": "movie",
                        "title": "Real Title",
                        "year": 2020,
                        "provider_ids": {"imdbid": "tt1234567"},
                    }
                },
            )
            enqueue.assert_called_once_with(["movies/Movie Pack"])

            with patch.object(state, "_enqueue_hook"):
                state.set_curator_title_override(cache_key, None)
            self.assertEqual(
                state.curator_title_override(cache_key),
                {},
            )
            self.assertEqual(db.load_curator_title_overrides(state.conn), {})
            state.close()

    def test_curator_title_override_accepts_anime_series_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            cache_key = "RD1"
            state.cache[cache_key] = {
                "signature": {},
                "info": {
                    "id": "RD1",
                    "hash": "anime123",
                    "status": "downloaded",
                    "filename": "Anime Pack",
                    "links": ["rd-link-1"],
                    "files": [
                        {
                            "id": "1",
                            "path": "/Anime.Show.S01E01.mkv",
                            "bytes": 100,
                            "selected": 1,
                            "stream_ref": "rd-link-1",
                        }
                    ],
                },
                "magnet": None,
            }

            with patch.object(state, "_enqueue_hook"):
                state.set_curator_title_override(
                    cache_key,
                    {
                        "kind": "anime",
                        "series": "Real Anime",
                        "year": "2023",
                        "anidbid": "9876",
                    },
                )

            self.assertEqual(
                state.curator_title_override(cache_key),
                {
                    "kind": "anime",
                    "series": "Real Anime",
                    "year": 2023,
                    "provider_ids": {"anidbid": "9876"},
                },
            )
            # Unknown kinds are still rejected.
            with self.assertRaises(ValueError):
                state.set_curator_title_override(
                    cache_key, {"kind": "bogus", "title": "X"}
                )
            state.close()

    def test_curator_title_suggestion_extracts_jellyfin_provider_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                provider_priority=("real_debrid",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
                jellyfin_api_key="",
            )
            state = BuzzState(config, client=self.FakeProvider())
            cache_key = "RD1"
            state.cache[cache_key] = {
                "signature": {},
                "info": {
                    "id": "RD1",
                    "hash": "abc123",
                    "status": "downloaded",
                    "filename": "Movie Pack (2020) [imdbid-tt1234567]",
                    "links": ["rd-link-1"],
                    "files": [
                        {
                            "id": "1",
                            "path": "Movie.Pack.2020.mkv",
                            "bytes": 100,
                            "selected": 1,
                            "stream_ref": "rd-link-1",
                        }
                    ],
                },
                "magnet": None,
            }

            self.assertEqual(
                state.suggest_curator_title_override(cache_key, "movie"),
                {
                    "kind": "movie",
                    "title": "Movie Pack",
                    "year": 2020,
                    "provider_ids": {"imdbid": "tt1234567"},
                },
            )
            state.close()

    def _torbox_two_file_info(self):
        return {
            "id": "TB1",
            "hash": "abc123",
            "status": "downloaded",
            "filename": "Snatch",
            "links": [],
            "files": [
                {
                    "id": "1",
                    "path": "/Snitch.199.1080p.mkv",
                    "bytes": 100,
                    "selected": 1,
                    "stream_ref": "TB1:1",
                },
                {
                    "id": "2",
                    "path": "/Sample/sample.mkv",
                    "bytes": 5,
                    "selected": 1,
                    "stream_ref": "TB1:2",
                },
            ],
        }

    def test_file_selection_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            info = self._torbox_two_file_info()
            cache_key = "torbox:TB1"
            with state.lock:
                state.cache[cache_key] = {
                    "signature": {},
                    "info": info,
                    "magnet": None,
                }
                state._save_cache_entry(cache_key, state.cache[cache_key])

            # Select only the feature file (id "1"), drop the sample.
            state.select_files(cache_key, ["1"])
            state.close()

            # A fresh BuzzState on the same DB must reload the selection.
            restarted = BuzzState(
                config, client={"torbox": self.FakeProvider()}
            )
            self.assertEqual(
                restarted.file_selections.get("abc123"),
                {"Snitch.199.1080p.mkv"},
            )

            # Re-applying against a fresh all-selected detail keeps it narrow.
            fresh = self._torbox_two_file_info()
            restarted._apply_cached_file_selection("torbox", cache_key, fresh)
            selected = {
                f["path"] for f in fresh["files"] if f.get("selected")
            }
            self.assertEqual(selected, {"/Snitch.199.1080p.mkv"})
            restarted.close()

    def test_torbox_refetch_does_not_clobber_stored_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            # Stored selection narrows to the feature file only.
            state.file_selections["abc123"] = {"Snitch.199.1080p.mkv"}

            # Provider refetch returns everything selected (TorBox default).
            fresh = self._torbox_two_file_info()
            state._apply_cached_file_selection("torbox", "torbox:TB1", fresh)

            selected = {
                f["path"] for f in fresh["files"] if f.get("selected")
            }
            self.assertEqual(selected, {"/Snitch.199.1080p.mkv"})
            self.assertEqual(fresh["links"], ["TB1:1"])
            state.close()

    def test_stored_selection_applies_by_path_across_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                torbox_token="token",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            state = BuzzState(
                config,
                client={
                    "real_debrid": self.FakeProvider(),
                    "torbox": self.FakeProvider(),
                },
            )
            # Selection stored by path; same hash on a provider whose file ids
            # differ from the originating provider.
            state.file_selections["abc123"] = {"Snitch.199.1080p.mkv"}
            other_provider_info = {
                "id": "RD1",
                "hash": "abc123",
                "status": "downloaded",
                "filename": "Snatch",
                "links": [],
                "files": [
                    {
                        "id": "77",
                        "path": "/Snitch.199.1080p.mkv",
                        "bytes": 100,
                        "selected": 0,
                        "stream_ref": "rd-link-77",
                    },
                    {
                        "id": "88",
                        "path": "/Sample/sample.mkv",
                        "bytes": 5,
                        "selected": 1,
                        "stream_ref": "rd-link-88",
                    },
                ],
            }
            state._apply_cached_file_selection(
                "real_debrid", "RD1", other_provider_info
            )
            selected = {
                f["path"]
                for f in other_provider_info["files"]
                if f.get("selected")
            }
            self.assertEqual(selected, {"/Snitch.199.1080p.mkv"})
            self.assertEqual(other_provider_info["links"], ["rd-link-77"])
            state.close()

    def test_torbox_files_selected_by_default_when_no_selection_flag(self):
        """TorBox API doesn't send per-file selection flags; files default to selected."""
        client = TorBoxProviderClient(token="token")
        raw_item = {
            "id": "42",
            "name": "Sitcom.2000.COMPLETE.S01.mkv",
            "size": 1000,
        }
        file = client._file("TB42", raw_item)
        self.assertTrue(file.selected)
        self.assertEqual(file.stream_ref, "TB42:42")

    def test_torbox_explicit_false_selection_flag_respected(self):
        """An explicit selected=False should override the default."""
        client = TorBoxProviderClient(token="token")
        raw_item = {
            "id": "99",
            "name": "sample.txt",
            "size": 10,
            "selected": False,
        }
        file = client._file("TB42", raw_item)
        self.assertFalse(file.selected)

    def test_torbox_downloaded_torrent_routes_to_shows_without_manual_selection(self):
        """A fully downloaded TorBox torrent with no selection flags ends up in shows/, not __unplayable__."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            client = TorBoxProviderClient(token="token")
            detail = client._detail({
                "id": "42",
                "name": "Sitcom.2000.COMPLETE.MULTi.1080p.WEB.DDP.x264-TESTGROUP",
                "original_filename": "Sitcom.2000.COMPLETE.MULTi.1080p.WEB.DDP.x264-TESTGROUP",
                "size": 50000,
                "progress": 100,
                "download_state": "completed",
                "files": [
                    {"id": "1", "name": "Sitcom.S01E01.mkv", "size": 500},
                    {"id": "2", "name": "Sitcom.S01E02.mkv", "size": 500},
                    {"id": "3", "name": "sample.nfo", "size": 1},
                ],
            })
            self.assertTrue(all(f.selected for f in detail.files))
            self.assertEqual(len(detail.stream_refs), 3)

            state = BuzzState(config, client={"torbox": self.FakeProvider()})
            info = BuzzState._detail_to_info(detail)
            info["provider"] = "torbox"
            snapshot, _ = state.builder.build([info])

            self.assertIn("shows/Sitcom.2000.COMPLETE.MULTi.1080p.WEB.DDP.x264-TESTGROUP/Sitcom.S01E01.mkv", snapshot["files"])
            self.assertNotIn("__unplayable__/Sitcom.2000.COMPLETE.MULTi.1080p.WEB.DDP.x264-TESTGROUP", snapshot.get("dirs", set()))

    def test_cache_selection_task_wakes_poller_instead_of_syncing_inline(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []

            def submit(self, kind, label, work):
                self.submitted.append((kind, label, work))
                return "task-1"

        class FakePoller:
            def __init__(self):
                self.wake_count = 0

            def wake(self):
                self.wake_count += 1

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            torbox = self.FakeProvider()
            state = BuzzState(config, client={"torbox": torbox})
            state.background_tasks = cast(Any, FakeTaskPool())
            poller = FakePoller()
            state.attach_poller(cast(Any, poller))
            state.sync = MagicMock()

            task_id = state.submit_cache_selection({"torbox:TB1": ["1"]})
            _kind, _label, work = state.background_tasks.submitted[0]
            work("test-task-id", threading.Event())

            self.assertEqual(task_id, "task-1")
            self.assertEqual(torbox.selected_files_calls, [("TB1", "1")])
            state.sync.assert_not_called()
            self.assertEqual(poller.wake_count, 1)
            self.assertEqual(len(state.background_tasks.submitted), 1)

    def test_cache_selection_task_submits_sync_task_without_poller(self):
        class FakeTaskPool:
            def __init__(self):
                self.submitted = []

            def submit(self, kind, label, work, **_kwargs):
                self.submitted.append((kind, label, work))
                return f"task-{len(self.submitted)}"

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                torbox_token="token",
                provider_priority=("torbox",),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            torbox = self.FakeProvider()
            state = BuzzState(config, client={"torbox": torbox})
            state.background_tasks = cast(Any, FakeTaskPool())
            state.sync = MagicMock()

            state.submit_cache_selection({"torbox:TB1": ["1"]})
            _kind, _label, work = state.background_tasks.submitted[0]
            work("test-task-id", threading.Event())

            self.assertEqual(
                [item[0] for item in state.background_tasks.submitted],
                ["cache", "sync"],
            )
            state.background_tasks.submitted[1][2]("test-task-id", threading.Event())
            state.sync.assert_called_once_with()

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
            ["sh", "/app/scripts/media_update.sh", "movies/Starfall"],
            output="hook stdout",
            stderr="hook stderr",
        )

        state._run_hook(["movies/Starfall"])

        mock_record_event.assert_any_call(
            "running configured media update command: 1 library root(s)",
            event="hook_running_command",
            changed_roots=1,
            command=(
                "sh /app/scripts/media_update.sh \\\n"
                "    movies/Starfall"
            ),
        )
        mock_record_event.assert_any_call(
            "\n".join(
                [
                    "configured media update command failed with exit code 2: ['sh', '/app/scripts/media_update.sh', 'movies/Starfall']",
                    "    command: |",
                    "        sh /app/scripts/media_update.sh \\",
                    "            movies/Starfall",
                    "    stdout: |",
                    "        hook stdout",
                    "    stderr: |",
                    "        hook stderr",
                ]
            ),
            level="error",
        )

    @patch("buzz.core.state.record_event")
    @patch("buzz.core.state.subprocess.run")
    def test_run_hook_logs_stdout_and_stderr_on_success(
        self, mock_run, mock_record_event
    ):
        config = Config(
            token="token",
            state_dir="/tmp/buzz-tests",
            hook_command="bash /app/scripts/media_update.sh",
            curator_url="",
        )
        state = BuzzState(config, client=None)
        mock_run.return_value = subprocess.CompletedProcess(
            ["bash", "/app/scripts/media_update.sh", "movies/Starfall"],
            0,
            stdout="hook stdout\nline 2\n",
            stderr="",
        )

        state._run_hook(["movies/Starfall"])

        mock_record_event.assert_any_call(
            "\n".join(
                [
                    "configured media update command completed: 1 library root(s)",
                    "    command: |",
                    "        bash /app/scripts/media_update.sh \\",
                    "            movies/Starfall",
                    "    stdout: |",
                    "        hook stdout",
                    "        line 2",
                    "    stderr: \"\"",
                ]
            ),
            event="hook_command_finished",
            changed_roots=1,
            command=(
                "bash /app/scripts/media_update.sh \\\n"
                "    movies/Starfall"
            ),
        )

    def test_curator_rebuild_cancels_during_vfs_wait(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mount = Path(tmpdir) / "raw"
            mount.mkdir()
            config = Config(
                token="token",
                state_dir=tmpdir,
                hook_command="",
                curator_url="http://curator.invalid/rebuild",
                library_mount=str(mount),
                vfs_wait_timeout_secs=30,
            )
            state = BuzzState(config, client=None)
            state.snapshot = {
                "files": {
                    "movies/Movie 2026/Movie.2026.1080p.mkv": {
                        "type": "file"
                    }
                }
            }
            task_id = state.background_tasks.submit(
                kind="curator",
                label="resync_lib: 1 roots",
                work=lambda tid, cancel_event: state._work_trigger_curator_task(
                    tid,
                    ["movies/Movie 2026"],
                    cancel_event,
                ),
                auto_complete=False,
            )

            deadline = time.time() + 2
            while time.time() < deadline:
                task = next(
                    item for item in state.background_tasks.snapshot()
                    if item["id"] == task_id
                )
                if task["status"] == "running":
                    break
                time.sleep(0.01)

            self.assertTrue(state.background_tasks.cancel(task_id))
            task = _wait_for_task(state, task_id)

            self.assertEqual(task["status"], "cancelled")

    def test_curator_rebuild_failure_completes_local_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="token",
                state_dir=tmpdir,
                hook_command="",
                curator_url="http://curator.invalid/rebuild",
                library_mount="",
            )
            state = BuzzState(config, client=None)
            state._trigger_curator = MagicMock(side_effect=RuntimeError("boom"))
            task_id = state.background_tasks.submit(
                kind="curator",
                label="resync_lib: 1 roots",
                work=lambda tid, cancel_event: state._work_trigger_curator_task(
                    tid,
                    ["movies/Movie 2026"],
                    cancel_event,
                ),
                auto_complete=False,
            )

            task = _wait_for_task(state, task_id)

            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["error"], "boom")

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
            first_state = BuzzState(config, client=self._create_fake_provider())
            first_state.sync(trigger_hook=False)

            second_state = BuzzState(config, client=self._create_fake_provider())
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
            state = HookState(config, client=self._create_fake_provider())
            report = state.sync()
            self.assertTrue(report["changed"])
            self.assertTrue(state.hook_started.wait(timeout=5))
            self.assertIsNotNone(state.lookup("movies/Movie 2026/Movie.2026.1080p.mkv"))
            self.assertTrue(state.status()["hook_in_progress"])
            state.release_hook.set()
            deadline = time.time() + 5
            while time.time() < deadline and state.status()["hook_in_progress"]:
                time.sleep(0.01)
            self.assertFalse(state.status()["hook_in_progress"])

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
    def test_background_sync_failure_records_event_and_stores_last_error(self):
        class FakeStopEvent:
            def __init__(self):
                self.calls = 0

            def wait(self, _timeout):
                self.calls += 1
                return self.calls > 1

            def is_set(self):
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
        poller._wake_event.wait = MagicMock(
            side_effect=lambda _timeout: poller._stop_event.wait(_timeout)
        )

        with patch("buzz.core.state.record_event") as mock_record_event:
            poller.run()

        self.assertEqual(state.last_error, "rd unavailable")
        mock_record_event.assert_called_once_with(
            "background sync failed: rd unavailable",
            level="error",
        )


class DavAppTests(unittest.TestCase):
    class FakeProvider:
        def __init__(self, download_urls=None, stream_error=None):
            self.calls = []
            self.download_urls = download_urls or []
            self.stream_error = stream_error

        def list_torrents(self):
            return []

        def get_torrent(self, torrent_id):
            return ProviderTorrentDetail(
                id=torrent_id,
                hash="",
                name=torrent_id,
                original_name=torrent_id,
                bytes=0,
                progress=0,
                status="unknown",
            )

        def fetch_details(self, torrent_ids, on_progress=None):
            total = len(torrent_ids)
            results = {}
            for i, torrent_id in enumerate(torrent_ids, 1):
                if on_progress is not None:
                    on_progress(torrent_id, i, total)
                results[torrent_id] = self.get_torrent(torrent_id)
            return results

        def add_magnet(self, magnet):
            return "NEW_TORRENT"

        def select_files(self, torrent_id, file_ids):
            return None

        def delete_torrent(self, torrent_id):
            return None

        def resolve_stream(self, stream_ref):
            self.calls.append(stream_ref)
            if self.stream_error is not None:
                raise self.stream_error
            idx = (
                len(self.calls) - 1
                if len(self.calls) <= len(self.download_urls)
                else 0
            )
            if self.download_urls:
                return self.download_urls[idx]
            return "https://cdn.example.invalid/file"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        state_dir = Path(self.tmpdir.name)
        snapshot = {
            "dirs": ["", "movies", "movies/Rocket Voyage [1986] + Extras"],
            "files": {
                "movies/Rocket Voyage [1986] + Extras/Rocket Voyage (1986).mkv": {
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
        rd_patcher = patch(
            "buzz.dav_app.DavApp._build_provider_client",
            return_value=None,
        )
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
            dav_rel_path("/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras/"),
            "movies/Rocket Voyage [1986] + Extras",
        )

    def test_hook_status_is_merged_into_state_meta_item(self):
        self.dav_app.state.hook_phase = "waiting_vfs"
        self.dav_app.state.hook_active_paths = ["movies/A", "shows/B"]

        meta_items = CacheLiveView(self.dav_app)._meta_items()

        self.assertIn(
            {"label": "state", "value": "waiting_vfs"},
            meta_items,
        )
        self.assertNotIn("hook", {item["label"] for item in meta_items})

    def test_state_meta_item_cycles_sync_and_hook_states(self):
        self.dav_app.state.sync_in_progress = True
        self.dav_app.state.hook_phase = "waiting_vfs"
        self.dav_app.state.hook_active_paths = ["movies/A", "shows/B"]

        meta_items = CacheLiveView(self.dav_app)._meta_items()
        state_item = next(item for item in meta_items if item["label"] == "state")

        self.assertEqual(state_item["value"], "syncing")
        self.assertIn(
            "waiting_vfs",
            state_item.get("cycle_values_json", ""),
        )

    def test_state_meta_item_marks_pending_file_selection_red(self):
        self.dav_app.state.cache = {
            "torrent-1": {
                "signature": {},
                "info": {
                    "id": "torrent-1",
                    "filename": "Pending Movie",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [{"path": "/Movie.mkv", "selected": 0}],
                },
            }
        }

        status = self.dav_app.state.status()
        meta_items = CacheLiveView(self.dav_app)._meta_items()
        state_item = next(item for item in meta_items if item["label"] == "state")

        self.assertTrue(status["file_selection_pending"])
        self.assertEqual(status["file_selection_pending_count"], 1)
        self.assertEqual(state_item["value"], "file_selection_pending")
        self.assertEqual(state_item.get("css_class"), "service-status-red")

    def test_state_meta_item_cycles_pending_file_selection(self):
        self.dav_app.state.sync_in_progress = True
        self.dav_app.state.hook_phase = "waiting_vfs"
        self.dav_app.state.hook_active_paths = ["movies/A", "shows/B"]
        self.dav_app.state.cache = {
            "torrent-1": {
                "signature": {},
                "info": {
                    "id": "torrent-1",
                    "filename": "Pending Movie",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [{"path": "/Movie.mkv", "selected": 0}],
                },
            }
        }

        meta_items = CacheLiveView(self.dav_app)._meta_items()
        state_item = next(item for item in meta_items if item["label"] == "state")
        cycle_values = json.loads(state_item.get("cycle_values_json", "[]"))
        cycle_classes = json.loads(state_item.get("cycle_classes_json", "{}"))

        self.assertEqual(state_item["value"], "syncing")
        self.assertEqual(
            cycle_values,
            ["syncing", "file_selection_pending", "waiting_vfs"],
        )
        self.assertEqual(
            cycle_classes,
            {"file_selection_pending": "service-status-red"},
        )

    def test_cache_view_derives_selectable_folders(self):
        files = [
            {
                "id": "1",
                "path": "/Extras/Season 01/one.mkv",
                "bytes": 1,
                "size": "1 B",
                "is_video": True,
                "selected": True,
            },
            {
                "id": "2",
                "path": "/Extras/Season 01/two.mkv",
                "bytes": 1,
                "size": "1 B",
                "is_video": True,
                "selected": False,
            },
            {
                "id": "3",
                "path": "/Extras/Season 02/three.mkv",
                "bytes": 1,
                "size": "1 B",
                "is_video": True,
                "selected": True,
            },
            {
                "id": "4",
                "path": "/flat.mkv",
                "bytes": 1,
                "size": "1 B",
                "is_video": True,
                "selected": True,
            },
        ]

        folders = CacheLiveView._expanded_folders(files)

        self.assertEqual(
            folders,
            [
                {
                    "path": "Extras",
                    "selected": False,
                    "selected_files": 2,
                    "total_files": 3,
                },
                {
                    "path": "Extras/Season 01",
                    "selected": False,
                    "selected_files": 1,
                    "total_files": 2,
                },
                {
                    "path": "Extras/Season 02",
                    "selected": True,
                    "selected_files": 1,
                    "total_files": 1,
                },
            ],
        )

    def test_cache_view_toggles_folder_selection(self):
        view = CacheLiveView(self.dav_app)
        files = [
            {
                "id": "1",
                "path": "/Extras/Season 01/one.mkv",
                "bytes": 1,
                "size": "1 B",
                "is_video": True,
                "selected": False,
            },
            {
                "id": "2",
                "path": "/Extras/Season 02/two.mkv",
                "bytes": 1,
                "size": "1 B",
                "is_video": True,
                "selected": False,
            },
            {
                "id": "3",
                "path": "/Movie.mkv",
                "bytes": 1,
                "size": "1 B",
                "is_video": True,
                "selected": False,
            },
        ]
        socket = SimpleNamespace(
            context={
                "expanded_files": files,
                "expanded_folders": CacheLiveView._expanded_folders(files),
            }
        )

        asyncio.run(
            view.handle_event(
                "toggle_folder_selection",
                cast(Any, socket),
                id="Extras",
            )
        )

        self.assertEqual(
            [file["selected"] for file in socket.context["expanded_files"]],
            [True, True, False],
        )
        self.assertEqual(
            socket.context["expanded_folders"][0],
            {
                "path": "Extras",
                "selected": True,
                "selected_files": 2,
                "total_files": 2,
            },
        )

        asyncio.run(
            view.handle_event(
                "toggle_folder_selection",
                cast(Any, socket),
                id="Extras/Season 01",
            )
        )

        self.assertEqual(
            [file["selected"] for file in socket.context["expanded_files"]],
            [False, True, False],
        )

    def test_analyze_splits_multiline_magnet_textarea(self):
        view = CacheLiveView(self.dav_app)
        socket = SimpleNamespace(context=view._context())

        self.state.add_magnet = MagicMock(
            side_effect=[
                {
                    "id": "TORRENT1",
                    "filename": "Movie One",
                    "files": [],
                },
                {
                    "id": "TORRENT2",
                    "filename": "Movie Two",
                    "files": [],
                },
            ]
        )

        asyncio.run(
            view.handle_event(
                "analyze",
                cast(Any, socket),
                payload={"magnet": " magnet-a \n\nmagnet-b\n "},
            )
        )

        self.assertEqual(
            self.state.add_magnet.call_args_list,
            [call("magnet-a", None), call("magnet-b", None)],
        )
        self.assertEqual(len(socket.context["analysis_results"]), 2)

    def test_cache_template_uses_local_textarea_for_bulk_magnets(self):
        cache_template = Path("buzz/pyview_templates/cache_live.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('name="magnet"', cache_template)
        self.assertIn("<textarea", cache_template)
        self.assertIn('phx-hook="BuzzBulkMagnetDraft"', cache_template)
        self.assertNotIn("line-numbers", cache_template)
        self.assertNotIn('phx-change="update_magnets"', cache_template)
        self.assertNotIn('phx-click="add_magnet_input"', cache_template)
        self.assertNotIn('phx-click="remove_magnet_input"', cache_template)

    def test_propfind_child_round_trips_encoded_directory_name(self):
        root_body = propfind_body(
            self.state,
            ["movies", "movies/Rocket Voyage [1986] + Extras"]
        )
        self.assertIn(
            "/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras", root_body
        )

        decoded = dav_rel_path("/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras/")
        self.assertIsNotNone(self.state.lookup(decoded))

        child_body = propfind_body(
            self.state,
            [
                decoded,
                f"{decoded}/Rocket Voyage (1986).mkv",
            ]
        )
        self.assertIn("Rocket%20Voyage%20%281986%29.mkv", child_body)

    def test_get_and_head_resolve_encoded_file_paths(self):
        encoded_path = dav_rel_path(
            "/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras/"
            "Rocket%20Voyage%20%281986%29.mkv"
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

    def test_cache_page_marks_torrents_with_pending_file_selection(self):
        self.state.cache = {
            "torrent-1": {
                "signature": {},
                "info": {
                    "id": "torrent-1",
                    "filename": "Pending Movie",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [{"path": "/Movie.mkv", "selected": 0}],
                },
            },
            "torrent-2": {
                "signature": {},
                "info": {
                    "id": "torrent-2",
                    "filename": "Selected Movie",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [{"path": "/Selected.mkv", "selected": 1}],
                },
            },
        }

        response = self.client.get("/cache")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn("file_selection_pending", body)
        self.assertIn("file-selection-pending", body)
        pending_row_start = body.index("file-selection-pending")
        selected_name_start = body.index("Selected Movie")
        selected_row_start = body.rfind("<tr", 0, selected_name_start)
        selected_row = body[selected_row_start:selected_name_start]
        self.assertLess(pending_row_start, selected_name_start)
        self.assertNotIn("file-selection-pending", selected_row)

    def _render_expanded_cache_panel(self, cache_key: str) -> str:
        from buzz.ui_live import _load_template

        view = CacheLiveView(self.dav_app)
        context = view._context(expanded_id=cache_key)
        rendered = _load_template("cache_live.html").render(context, None)
        return str(rendered)

    def test_expanded_detail_panel_has_labeled_sections(self):
        self.state.cache = {
            "torrent-1": {
                "signature": {},
                "info": {
                    "id": "torrent-1",
                    "filename": "Example Feature 1999",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [
                        {"id": "1", "path": "/Example.Feature.1999.mkv",
                         "bytes": 100, "selected": 1},
                        {"id": "2", "path": "/readme.txt",
                         "bytes": 10, "selected": 0},
                    ],
                },
            }
        }

        body = self._render_expanded_cache_panel("torrent-1")

        # Three labeled sections present.
        self.assertIn(">Category<", body)
        self.assertIn(">Identity<", body)
        self.assertIn(">File selection<", body)
        # Subtitle box includes the default query it will use when empty.
        self.assertIn('placeholder="subs query: Example Feature"', body)
        self.assertNotIn('placeholder="auto subtitle name"', body)
        # Year is a plain text box, not a number spinner.
        self.assertNotIn('type="number"', body)
        # Editable inputs are isolated from server re-renders.
        self.assertIn('phx-update="ignore"', body)
        # Identity revert is client-side only (no server event).
        self.assertIn("BuzzIdentityRevert", body)
        self.assertIn("data-revert", body)
        # Suggest/Clear server round-trips are gone from the panel.
        self.assertNotIn("suggest_curator_title", body)
        self.assertNotIn("clear_curator_title", body)
        # Non-video rows reserve the subtitle-box height.
        self.assertIn("subtitle-row-placeholder", body)

    def test_expanded_identity_prefills_derived_defaults(self):
        self.state.cache = {
            "torrent-1": {
                "signature": {},
                "info": {
                    "id": "torrent-1",
                    "filename": "Example Feature 1999",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [
                        {"id": "1", "path": "/Example.Feature.1999.mkv",
                         "bytes": 100, "selected": 1},
                    ],
                },
            }
        }

        body = self._render_expanded_cache_panel("torrent-1")

        # Derived title/year are stamped as both value and data-default so the
        # form is prefilled and Revert can restore them.
        self.assertIn('data-default="Example Feature"', body)
        self.assertIn('data-default="1999"', body)
        self.assertIn('value="Example Feature"', body)
        self.assertIn('value="1999"', body)

    def test_expanded_identity_dom_ids_are_sanitized(self):
        self.state.cache = {
            "torbox:38618447": {
                "signature": {},
                "info": {
                    "id": "38618447",
                    "filename": "Example Feature 1999",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [
                        {"id": "1", "path": "/Example.Feature.1999.mkv",
                         "bytes": 100, "selected": 1},
                    ],
                },
            }
        }

        body = self._render_expanded_cache_panel("torbox:38618447")

        self.assertNotIn('id="identity-form-torbox:38618447"', body)
        self.assertNotIn('id="identity-inputs-torbox:38618447"', body)
        self.assertNotIn('id="identity-form-torbox-38618447"', body)
        self.assertIn('id="identity-section-torbox-38618447-movie"', body)
        self.assertIn('id="identity-inputs-torbox-38618447-movie"', body)
        self.assertEqual(body.count('phx-submit="set_curator_title"'), 1)
        self.assertEqual(body.count('class="subtitle-query-form curator-title-form"'), 1)
        identity_start = body.index('phx-submit="set_curator_title"')
        identity_end = body.index("</form>", identity_start)
        identity_form = body[identity_start:identity_end]
        self.assertIn('name="cache_id" value="torbox:38618447"', identity_form)
        self.assertNotIn('name="id" value="torbox:38618447"', identity_form)

    def test_expanded_identity_override_marks_inputs_overridden(self):
        cache_key = "torbox:38618447"
        self.state.cache = {
            cache_key: {
                "signature": {},
                "info": {
                    "id": "38618447",
                    "hash": "abc123",
                    "filename": "Example Feature 1999",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [
                        {"id": "1", "path": "/Example.Feature.1999.mkv",
                         "bytes": 100, "selected": 1},
                    ],
                },
            }
        }
        self.state.curator_title_overrides["abc123"] = {
            "kind": "movie",
            "title": "Example Feature Revised",
            "year": 1999,
        }

        body = self._render_expanded_cache_panel(cache_key)

        self.assertIn("identity-inputs-overridden", body)
        self.assertIn(">override<", body)

    def test_expanded_show_subtitle_query_placeholder_uses_series(self):
        self.state.cache = {
            "torbox:37310789": {
                "signature": {},
                "info": {
                    "id": "37310789",
                    "filename": "Example Series 2016 Season 01",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [
                        {
                            "id": "1",
                            "path": (
                                "/Example Series (2016) - S01E01 - "
                                "Pilot.mkv"
                            ),
                            "bytes": 100,
                            "selected": 1,
                        },
                    ],
                },
            }
        }

        body = self._render_expanded_cache_panel("torbox:37310789")

        self.assertIn('id="identity-section-torbox-37310789-show"', body)
        self.assertIn('placeholder="subs query: Example Series"', body)

    def test_expanded_anime_renders_series_style_identity_form(self):
        cache_key = "torbox:37310790"
        self.state.cache = {
            cache_key: {
                "signature": {},
                "info": {
                    "id": "37310790",
                    "hash": "anime123",
                    "filename": "Example Anime 2023 Season 01",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [
                        {
                            "id": "1",
                            "path": (
                                "/Example Anime (2023) - S01E01 - "
                                "Pilot.mkv"
                            ),
                            "bytes": 100,
                            "selected": 1,
                        },
                    ],
                },
            }
        }
        self.state.set_torrent_category(cache_key, "anime")

        body = self._render_expanded_cache_panel(cache_key)

        self.assertIn(
            'id="identity-section-torbox-37310790-anime"', body
        )
        # Series-style fields, including the anime-specific anidbid input.
        self.assertIn('name="series"', body)
        self.assertIn('name="anidbid"', body)
        # The hidden kind input records the override as ``anime``.
        self.assertIn(
            '<input type="hidden" name="kind" value="anime">', body
        )

    def test_expanded_auto_category_glows_without_active_background(self):
        self.state.cache = {
            "torrent-1": {
                "signature": {},
                "info": {
                    "id": "torrent-1",
                    "filename": "Example Feature 1999",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [
                        {"id": "1", "path": "/Example.Feature.1999.mkv",
                         "bytes": 100, "selected": 1},
                    ],
                },
            }
        }

        body = self._render_expanded_cache_panel("torrent-1")

        auto_start = body.index('phx-value-mode="auto"')
        auto_button = body[body.rfind("<button", 0, auto_start):auto_start]
        # Auto button carries the colored glow class but NOT the solid
        # active-background highlight.
        self.assertIn("auto-category", auto_button)
        self.assertNotIn("active", auto_button)

    def test_expanded_context_makes_no_jellyfin_lookup(self):
        # Regression: identity prefill must derive locally only. A remote
        # Jellyfin RemoteSearch on every render floods Jellyfin once an entry
        # is expanded (it re-renders on each provider-status push).
        self.dav_app.config.jellyfin_url = "http://jellyfin.local:8096"
        self.dav_app.config.jellyfin_api_key = "jf-secret"
        self.state.cache = {
            "torrent-1": {
                "signature": {},
                "info": {
                    "id": "torrent-1",
                    "filename": "Example Feature 1999",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 100,
                    "files": [
                        {"id": "1", "path": "/Example.Feature.1999.mkv",
                         "bytes": 100, "selected": 1},
                    ],
                },
            }
        }

        def _boom(*_args, **_kwargs):
            raise AssertionError(
                "Jellyfin RemoteSearch must not run during render"
            )

        with patch.object(
            self.state, "_jellyfin_curator_title_suggestion", _boom
        ):
            body = self._render_expanded_cache_panel("torrent-1")

        # Local-only prefill still populated the identity form.
        self.assertIn('value="Example Feature"', body)
        self.assertIn('data-default="Example Feature"', body)

    def test_ui_documents_are_not_cached(self):
        for path in ("/", "/cache", "/archive", "/logs", "/threads", "/config"):
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers["cache-control"],
                    "no-store, no-cache, must-revalidate, max-age=0",
                )
                self.assertEqual(response.headers["pragma"], "no-cache")
                self.assertEqual(response.headers["expires"], "0")

    def test_threads_page_renders_tasks_newest_first(self):
        self.state.background_tasks._tasks = {
            "complete-task": BackgroundTask(
                id="complete-task",
                kind="cache",
                label="finished cache",
                cancel_event=threading.Event(),
                status="complete",
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:01:00Z",
            ),
            "queued-task": BackgroundTask(
                id="queued-task",
                kind="cache",
                label="queued cache",
                cancel_event=threading.Event(),
                status="queued",
            ),
            "running-task": BackgroundTask(
                id="running-task",
                kind="cache",
                label="running cache",
                cancel_event=threading.Event(),
                status="running",
                started_at="2026-01-01T00:02:00Z",
            ),
            "pending-task": BackgroundTask(
                id="pending-task",
                kind="maintenance",
                label="manual maintenance",
                cancel_event=threading.Event(),
                status="pending",
            ),
        }

        response = self.client.get("/threads")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn("buzz: threads", body)
        self.assertIn("🧵", body)
        # Manual approval tasks sort first; other tasks stay newest first by
        # timestamp, with timestamp-less queued tasks last.
        self.assertLess(
            body.index("manual maintenance"),
            body.index("running cache"),
        )
        self.assertLess(
            body.index("running cache"),
            body.index("finished cache"),
        )
        self.assertLess(
            body.index("finished cache"),
            body.index("queued cache"),
        )
        self.assertIn('phx-click="cancel_thread"', body)
        self.assertIn('phx-click="scan_rd"', body)
        self.assertIn("SCAN RD", body)
        # migrate buttons absent when torbox is not configured
        self.assertNotIn('phx-click="migrate_rd_tb"', body)
        self.assertNotIn('phx-click="migrate_tb_rd"', body)
        self.assertIn('phx-hook="BuzzOverflowMarquee"', body)
        self.assertIn('class="thread-error trunc-cell"', body)
        self.assertIn("thread-row-pending", body)
        self.assertIn("data-marquee-clip", body)

    def test_threads_page_shows_migrate_buttons_only_when_both_providers_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                token="rdtoken",
                torbox_token="tbtoken",
                provider_priority=("real_debrid", "torbox"),
                state_dir=tmpdir,
                hook_command="",
                curator_url="",
            )
            with patch("buzz.dav_app.DavApp._build_provider_client", return_value=None), \
                 patch("buzz.dav_app._fetch_opensubtitles_languages", return_value=[]):
                app = DavApp(config)
                with TestClient(app.app) as client:
                    response = client.get("/threads")
                body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn('phx-click="scan_rd"', body)
        self.assertIn('phx-click="migrate_rd_tb"', body)
        self.assertIn("MIGRATE RD-&gt;TB", body)
        self.assertIn('phx-click="migrate_tb_rd"', body)
        self.assertIn("MIGRATE TB-&gt;RD", body)

    def test_threads_page_renders_accept_for_pending_manual_thread(self):
        self.state.background_tasks._tasks = {
            "task-pending": BackgroundTask(
                id="task-pending",
                kind="maintenance",
                label="cleanup 1 Real-Debrid infringing torrent(s)",
                cancel_event=threading.Event(),
                status="pending",
            )
        }

        response = self.client.get("/threads")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn("cleanup 1 Real-Debrid infringing torrent(s)", body)
        self.assertIn('phx-click="start_thread"', body)
        self.assertIn("Accept", body)

    def test_threads_view_cancel_thread_delegates_to_state(self):
        view = ThreadsLiveView(self.dav_app)
        socket = SimpleNamespace(context=view._context())
        self.state.cancel_background_task = MagicMock()

        asyncio.run(
            view.handle_event(
                "cancel_thread",
                cast(Any, socket),
                task_id="task-1",
            )
        )

        self.state.cancel_background_task.assert_called_once_with("task-1")
        self.assertEqual(socket.context["console_class"], "service-status-yellow")

    def test_threads_view_scan_rd_queues_maintenance_scan(self):
        view = ThreadsLiveView(self.dav_app)
        socket = SimpleNamespace(context=view._context())
        self.state.submit_infringing_scan = MagicMock(return_value="scan-task")

        asyncio.run(
            view.handle_event(
                "scan_rd",
                cast(Any, socket),
            )
        )

        self.state.submit_infringing_scan.assert_called_once_with()
        self.assertEqual(socket.context["selected_thread_id"], "scan-task")
        self.assertEqual(socket.context["console_class"], "service-status-yellow")

    def test_threads_view_migration_buttons_queue_scans(self):
        view = ThreadsLiveView(self.dav_app)
        socket = SimpleNamespace(context=view._context())
        self.state.submit_provider_migration_scan = MagicMock(
            side_effect=["rd-tb-task", "tb-rd-task"]
        )

        asyncio.run(
            view.handle_event(
                "migrate_rd_tb",
                cast(Any, socket),
            )
        )
        asyncio.run(
            view.handle_event(
                "migrate_tb_rd",
                cast(Any, socket),
            )
        )

        self.assertEqual(
            self.state.submit_provider_migration_scan.call_args_list,
            [
                call("real_debrid", "torbox"),
                call("torbox", "real_debrid"),
            ],
        )
        self.assertEqual(socket.context["selected_thread_id"], "tb-rd-task")
        self.assertEqual(socket.context["console_class"], "service-status-yellow")

    def test_threads_view_start_thread_delegates_to_state(self):
        view = ThreadsLiveView(self.dav_app)
        socket = SimpleNamespace(context=view._context())
        self.state.start_background_task = MagicMock()

        asyncio.run(
            view.handle_event(
                "start_thread",
                cast(Any, socket),
                task_id="task-1",
            )
        )

        self.state.start_background_task.assert_called_once_with("task-1")
        self.assertEqual(socket.context["selected_thread_id"], "task-1")
        self.assertEqual(socket.context["console_class"], "service-status-yellow")

    def test_threads_view_expands_selected_thread_logs(self):
        registry.clear()
        self.addCleanup(registry.clear)
        task = BackgroundTask(
            id="task-logs",
            kind="cache",
            label="cache with logs",
            cancel_event=threading.Event(),
            status="complete",
            started_at="2026-01-01T00:00:00Z",
            error="provider detail refresh failed with a long message",
        )
        self.state.background_tasks._tasks = {"task-logs": task}
        with registry.task_context("task-logs"):
            registry.record("older thread scoped log", level="info", source="dav")
            registry.record("newer thread scoped log", level="error", source="dav")
        registry.record("global log", level="error", source="dav")
        view = ThreadsLiveView(self.dav_app)
        socket = SimpleNamespace(context=view._context())

        asyncio.run(
            view.handle_event(
                "toggle_thread",
                cast(Any, socket),
                task_id="task-logs",
            )
        )

        self.assertEqual(socket.context["selected_thread_id"], "task-logs")
        thread = socket.context["thread_items"][0]
        self.assertTrue(thread["expanded"])
        self.assertEqual(
            [log["message"] for log in thread["logs"]],
            ["newer thread scoped log", "older thread scoped log"],
        )
        self.assertTrue(socket.context["logs_newest_first"])
        self.assertEqual(thread["log_order_label"], "NEWEST")
        self.assertTrue(thread["show_log_severity"])
        self.assertEqual(thread["log_severity_class"], "thread-log-severity-error")
        self.assertEqual(
            thread["log_severity_title"],
            "worst task log level: error",
        )

        asyncio.run(
            view.handle_event(
                "invert_thread_log_order",
                cast(Any, socket),
            )
        )

        thread = socket.context["thread_items"][0]
        self.assertEqual(
            [log["message"] for log in thread["logs"]],
            ["older thread scoped log", "newer thread scoped log"],
        )
        self.assertFalse(socket.context["logs_newest_first"])
        self.assertEqual(thread["log_order_label"], "OLDEST")

    def test_archive_page_renders_pyview_shell(self):
        self.state.archive = {
            "archive-1": {
                "hash": "archive-1",
                "name": "Old & Gone",
                "bytes": 4096,
                "file_count": 3,
                "deleted_at": "2026-01-03T00:00:00Z",
                "magnet": "magnet:?xt=urn:btih:archive-1",
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
        self.assertIn("fa-magnet", body)
        self.assertIn('class="archive-magnet-copy-btn"', body)
        self.assertIn('data-copy-text="magnet:?xt=urn:btih:archive-1"', body)
        self.assertIn("magnet copied to clipboard!", body)
        self.assertIn("Copy magnet link for Old &amp; Gone", body)

    def _archive_provider_link(self, thash: str, provider: str, torrent_id: str) -> None:
        with self.state.conn:
            self.state.conn.execute(
                "INSERT INTO library_entries "
                "(hash, name, bytes, files_json, magnet, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(hash) DO UPDATE SET "
                "name = excluded.name, "
                "bytes = excluded.bytes, "
                "files_json = excluded.files_json, "
                "magnet = excluded.magnet, "
                "updated_at = excluded.updated_at",
                (
                    thash,
                    "Old & Gone",
                    4096,
                    "[]",
                    f"magnet:?xt=urn:btih:{thash}",
                    "2026-01-03T00:00:00Z",
                ),
            )
            self.state.conn.execute(
                "INSERT OR REPLACE INTO provider_links "
                "(provider, provider_torrent_id, hash, status, progress, "
                "info_json, signature_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    torrent_id,
                    thash,
                    "downloaded",
                    100,
                    "{}",
                    "{}",
                    "2026-01-03T00:00:00Z",
                ),
            )

    def test_archive_transfer_renders_copy_move_and_disabled_states(self):
        self.state.config.provider_priority = ("real_debrid", "torbox")
        self.state.clients = {
            "real_debrid": self.FakeProvider(),
            "torbox": self.FakeProvider(),
        }
        self.dav_app.clients = self.state.clients
        self.state.archive = {
            "rd-only": {
                "hash": "rd-only",
                "name": "RD Only",
                "bytes": 1,
                "files": [{"path": "/Movie.mkv"}],
                "deleted_at": "2026-01-03T00:00:00Z",
                "magnet": "magnet:?xt=urn:btih:rd-only",
            },
            "tb-only": {
                "hash": "tb-only",
                "name": "TB Only",
                "bytes": 1,
                "files": [{"path": "/Movie.mkv"}],
                "deleted_at": "2026-01-03T00:00:00Z",
                "magnet": "magnet:?xt=urn:btih:tb-only",
            },
            "both": {
                "hash": "both",
                "name": "Both",
                "bytes": 1,
                "files": [{"path": "/Movie.mkv"}],
                "deleted_at": "2026-01-03T00:00:00Z",
                "magnet": "magnet:?xt=urn:btih:both",
            },
        }
        self._archive_provider_link("rd-only", "real_debrid", "RD1")
        self._archive_provider_link("tb-only", "torbox", "TB1")
        self._archive_provider_link("both", "real_debrid", "RD2")
        self._archive_provider_link("both", "torbox", "TB2")

        context = ArchiveLiveView(self.dav_app)._context()
        by_name = {item["name"]: item for item in context["archive_items"]}
        self.assertEqual(by_name["RD Only"]["transfer_label"], "C")
        self.assertFalse(by_name["RD Only"]["transfer_disabled"])
        self.assertEqual(by_name["TB Only"]["transfer_label"], "M")
        self.assertFalse(by_name["TB Only"]["transfer_disabled"])
        self.assertTrue(by_name["Both"]["transfer_disabled"])

        response = self.client.get("/archive")
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn(">RD Only<", body)
        self.assertIn(">TB Only<", body)
        self.assertIn(">Both<", body)
        self.assertIn("Copy to TorBox", body)
        self.assertIn(">[C]</button>", body)
        self.assertIn("Move to Real-Debrid", body)
        self.assertIn(">[M]</button>", body)
        self.assertIn("not-allowed", Path("buzz/static/buzz.css").read_text())

    def test_archive_transfer_registers_manual_task(self):
        self.state.config.provider_priority = ("real_debrid", "torbox")
        self.state.clients = {
            "real_debrid": self.FakeProvider(),
            "torbox": self.FakeProvider(),
        }
        self.dav_app.clients = self.state.clients
        self.state.archive = {
            "rd-only": {
                "hash": "rd-only",
                "name": "RD Only",
                "bytes": 1,
                "files": [{"path": "/Movie.mkv"}],
                "deleted_at": "2026-01-03T00:00:00Z",
                "magnet": "magnet:?xt=urn:btih:rd-only",
            }
        }
        self._archive_provider_link("rd-only", "real_debrid", "RD1")
        view = ArchiveLiveView(self.dav_app)
        socket = SimpleNamespace(context=view._context())

        asyncio.run(
            view.handle_event(
                "transfer_provider",
                cast(Any, socket),
                hash="rd-only",
            )
        )

        pending = [
            task
            for task in self.state.background_tasks.snapshot()
            if task["status"] == "pending"
        ]
        self.assertEqual(len(pending), 1)
        self.assertIn("provider transfer pending:", socket.context["console_msg"])

    def test_archive_view_restore_queues_background_task(self):
        view = ArchiveLiveView(self.dav_app)
        socket = SimpleNamespace(context=view._context())
        self.state.submit_archive_restore = MagicMock(return_value="task-1")
        self.state.restore_archive = MagicMock()
        self.state.sync = MagicMock()

        asyncio.run(
            view.handle_event(
                "restore",
                cast(Any, socket),
                hash="archive-1",
            )
        )

        self.state.submit_archive_restore.assert_called_once_with("archive-1")
        self.state.restore_archive.assert_not_called()
        self.state.sync.assert_not_called()
        self.assertEqual(socket.context["console_msg"], "restore queued: task-1")
        self.assertEqual(socket.context["console_class"], "service-status-yellow")

    def test_restore_api_queues_background_task(self):
        self.state.submit_archive_restore = MagicMock(return_value="task-1")

        response = self.client.post(
            "/api/cache/restore",
            json={"hash": "archive-1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "queued", "task_id": "task-1"},
        )
        self.state.submit_archive_restore.assert_called_once_with("archive-1")

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
        self.assertIn("CLEAR", body)
        self.assertIn("RESYNC LIB", body)
        self.assertIn('phx-click="resync"', body)
        self.assertNotIn("REFRESH", body)
        self.assertNotIn("AUTO ON", body)
        self.assertNotIn("AUTO OFF", body)

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
                f"version: 1\nprovider:\n  token: testtoken\nstate_dir: {tmpdir}\n",
                encoding="utf-8",
            )
            overrides_path.write_text(
                "logging:\n  verbose: true\n",
                encoding="utf-8",
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.DavApp._build_provider_client", return_value=DavAppTests.FakeProvider())
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
                f"version: 1\nprovider:\n  token: testtoken\n"

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
                "version: 1\nprovider:\n  token: testtoken\n"
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
            rd_patcher = patch("buzz.dav_app.DavApp._build_provider_client", return_value=DavAppTests.FakeProvider())
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

        self.assertEqual(data.get("version"), 2)
        self.assertIn("provider", data)
        self.assertIn("real_debrid", data["provider"])
        self.assertIn("media_server", data)
        self.assertIn("subtitles", data)

    def test_provider_enabled_flags_round_trip_config(self):
        config = Config._from_merged_dict(
            {
                "version": 2,
                "provider": {
                    "priority": ["real_debrid", "torbox"],
                    "real_debrid": {
                        "enabled": False,
                        "token": "rdtoken",
                    },
                    "torbox": {
                        "enabled": True,
                        "token": "tbtoken",
                    },
                },
            }
        )

        nested = to_nested_dict(config)

        self.assertFalse(config.real_debrid_enabled)
        self.assertTrue(config.torbox_enabled)
        self.assertFalse(nested["provider"]["real_debrid"]["enabled"])
        self.assertTrue(nested["provider"]["torbox"]["enabled"])

    def test_disabled_provider_is_not_built(self):
        app = self.dav_app
        app._build_provider_client = MagicMock(return_value=self.FakeProvider())
        config = Config(
            token="rdtoken",
            real_debrid_enabled=False,
            torbox_token="tbtoken",
            torbox_enabled=True,
            provider_priority=("real_debrid", "torbox"),
            state_dir=self.tmpdir.name,
            hook_command="",
            curator_url="",
        )

        clients = app._build_provider_clients(config)

        self.assertEqual(list(clients), ["torbox"])
        app._build_provider_client.assert_called_once_with(config, "torbox")

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
            patch("buzz.dav_app.DavApp._build_provider_client", return_value=self.FakeProvider()),
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
                patch("buzz.dav_app.DavApp._build_provider_client", return_value=self.FakeProvider()),
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
                patch("buzz.dav_app.DavApp._build_provider_client", return_value=self.FakeProvider()),
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
            patch("buzz.dav_app.DavApp._build_provider_client", return_value=self.FakeProvider()),
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
            patch("buzz.dav_app.DavApp._build_provider_client", return_value=self.FakeProvider()),
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

    def test_rendered_config_favorite_stars(self):
        from buzz.ui_live import ConfigLiveView, _load_template
        from pyview.meta import PyViewMeta

        view = ConfigLiveView(owner=self.dav_app)
        context = view._context(is_editing=True)
        html = _load_template("config_live.html").render(context, PyViewMeta())

        # subtitles is favorited by default: filled star, pinned to the top.
        self.assertIn(
            'id="config-section-subtitles" style="order: 0"', html
        )
        self.assertIn(
            'phx-value-section="subtitles"', html
        )
        self.assertIn("fa-solid fa-star", html)
        self.assertIn("config-fav-star is-favorite", html)
        # provider is not favorited: hollow star, natural order.
        self.assertIn(
            'id="config-section-provider" style="order: 1"', html
        )
        self.assertIn("fa-regular fa-star", html)

    def test_options_and_propfind_use_asgi_routes(self):
        options = self.client.options("/dav/movies")
        propfind = self.client.request("PROPFIND", "/dav/movies", headers={"Depth": "1"})

        self.assertEqual(options.status_code, 204)
        self.assertEqual(options.headers["dav"], "1")
        self.assertEqual(propfind.status_code, 207)
        self.assertIn(
            "/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras",
            propfind.text,
        )

    def test_api_validation_errors_return_json_error_envelope(self):
        response = self.client.post("/api/cache/add", json={"magnet": "  "})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Value error, Missing magnet link"})

    def test_memory_file_head_and_range_get_use_asgi_routes(self):
        head = self.client.head(
            "/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras/"
            "Rocket%20Voyage%20%281986%29.mkv"
        )
        get_range = self.client.get(
            "/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras/"
            "Rocket%20Voyage%20%281986%29.mkv",
            headers={"Range": "bytes=0-0"},
        )

        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["content-length"], "2")
        self.assertEqual(get_range.status_code, 206)
        self.assertEqual(get_range.headers["content-range"], "bytes 0-0/2")
        self.assertEqual(get_range.content, b"o")

    def test_remote_media_refreshes_stale_html_response_once(self):
        self.state.client = self.FakeProvider(
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
            "movies/Rocket Voyage [1986] + Extras/Rocket Voyage (1986).mkv"
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
                "movies/Rocket Voyage [1986] + Extras/Rocket Voyage (1986).mkv"
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
        self.state.client = self.FakeProvider(
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
        self.state.client = self.FakeProvider(["https://example.invalid/download"])

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
        self.state.client = self.FakeProvider(
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
        self.state.client = self.FakeProvider(["https://example.invalid/cdn"])

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
        ), patch("buzz.dav_protocol.time.sleep"), patch(
            "buzz.dav_protocol.record_event"
        ) as mock_record_event:
            with self.assertRaisesRegex(ValueError, "failed to connect to upstream"):
                open_remote_media(self.state, node, None)

        self.assertEqual(mock_invalidate.call_count, 0)
        self.assertEqual(len(self.state.client.calls), 1)
        self.assertTrue(mock_record_event.call_args_list)
        self.assertTrue(
            all(
                call.kwargs.get("level") == "debug"
                for call in mock_record_event.call_args_list
            )
        )

    def test_open_remote_media_invalidates_url_on_http_error(self):
        from email.message import Message
        from urllib.error import HTTPError

        self.state.client = self.FakeProvider(
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
        self.state.client = self.FakeProvider(
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
        self.state.client = self.FakeProvider(
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
        self.state.client = self.FakeProvider(
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

        self.state.client = self.FakeProvider(
            stream_error=ProviderStreamError(
                "https://example.invalid/source",
                "hoster_unavailable",
            )
        )
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
        self.state.client = self.FakeProvider(
            stream_error=ProviderStreamError(
                "https://example.invalid/source",
                "hoster_unavailable",
            )
        )
        self.state.snapshot["files"][
            "movies/Rocket Voyage [1986] + Extras/Rocket Voyage (1986).mkv"
        ] = {
            "type": "remote",
            "size": 14,
            "source_url": "https://example.invalid/source",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-hoster-503",
        }
        response = self.client.get(
            "/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras/"
            "Rocket%20Voyage%20%281986%29.mkv"
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.headers["retry-after"],
            str(self.state.config.rd_hoster_failure_cache_secs),
        )
        # Second request inside TTL must not hit RD again.
        response2 = self.client.get(
            "/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras/"
            "Rocket%20Voyage%20%281986%29.mkv"
        )
        self.assertEqual(response2.status_code, 503)
        self.assertEqual(len(self.state.client.calls), 1)

    def test_dav_get_logs_hoster_unavailable_once_per_cache_ttl(self):
        self.state.client = self.FakeProvider(
            stream_error=ProviderStreamError(
                "https://example.invalid/source",
                "hoster_unavailable",
            )
        )
        self.state.snapshot["files"][
            "movies/Rocket Voyage [1986] + Extras/Rocket Voyage (1986).mkv"
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
                    "/dav/movies/Rocket%20Voyage%20%5B1986%5D%20%2B%20Extras/"
                    "Rocket%20Voyage%20%281986%29.mkv"
                )
                self.assertEqual(response.status_code, 503)

        self.assertEqual(len(self.state.client.calls), 1)
        hoster_events = [
            call
            for call in mock_record_event.call_args_list
            if call.kwargs.get("event") == "rd_hoster_unavailable"
        ]
        self.assertEqual(len(hoster_events), 1)

    def test_dav_get_caches_torbox_stream_resolution_rate_limit(self):
        torbox = self.FakeProvider(
            stream_error=ProviderStreamError(
                "12345678",
                "http_429 Too Many Requests",
            )
        )
        self.state.clients = {"torbox": torbox}
        self.state.client = None
        self.state.snapshot["dirs"].extend(
            ["movies", "movies/Synthetic Feature"]
        )
        self.state.snapshot["files"][
            "movies/Synthetic Feature/Synthetic Feature.mkv"
        ] = {
            "type": "remote",
            "size": 14,
            "source_url": "12345678",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-torbox-429",
        }
        self.state._rebuild_snapshot_indexes()

        for _ in range(2):
            response = self.client.get(
                "/dav/movies/Synthetic%20Feature/"
                "Synthetic%20Feature.mkv"
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                response.headers["retry-after"],
                str(self.state.config.rd_hoster_failure_cache_secs),
            )

        self.assertEqual(torbox.calls, ["12345678"])

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

        self.state.client = self.FakeProvider(["https://example.invalid/cdn"])
        with patch(
            "buzz.dav_protocol._get_upstream_semaphore",
            return_value=BusySemaphore(),
        ), patch("buzz.dav_protocol.time.sleep"):
            with self.assertRaisesRegex(ValueError, "connection limit reached"):
                open_remote_media(self.state, node, None)

    def test_dav_get_labels_torbox_setup_limit_failures(self):
        class BusySemaphore:
            def acquire(self, timeout=None):
                return False

        torbox = self.FakeProvider(["https://cdn.example.invalid/torbox"])
        self.state.clients = {"torbox": torbox}
        self.state.client = None
        self.state.snapshot["dirs"].extend(["movies", "movies/Synthetic Feature"])
        self.state.snapshot["files"][
            "movies/Synthetic Feature/Synthetic Feature.mkv"
        ] = {
            "type": "remote",
            "size": 14,
            "source_url": "12345678",
            "mime_type": "video/x-matroska",
            "modified": "2026-01-01T00:00:00Z",
            "etag": "etag-torbox-busy",
        }
        self.state._rebuild_snapshot_indexes()

        with patch(
            "buzz.dav_protocol._get_upstream_semaphore",
            return_value=BusySemaphore(),
        ), patch("buzz.dav_protocol.time.sleep"), patch(
            "buzz.dav_app.record_event"
        ) as mock_record_event:
            response = self.client.get(
                "/dav/movies/Synthetic%20Feature/"
                "Synthetic%20Feature.mkv"
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("torbox media", response.text)
        stream_events = [
            call
            for call in mock_record_event.call_args_list
            if call.kwargs.get("event") == "provider_stream_failed"
        ]
        self.assertEqual(len(stream_events), 1)
        self.assertEqual(stream_events[0].kwargs.get("provider"), "torbox")

    def test_languages_refreshing_flag_set_while_fetching(self):
        config = self._config_with_credentials(self.tmpdir.name)

        def _slow_language_fetch(*_args):
            time.sleep(0.05)
            return [("de", "German")]

        with (
            patch("buzz.dav_app.DavApp._build_provider_client", return_value=self.FakeProvider()),
            patch(
                "buzz.dav_app._fetch_opensubtitles_languages",
                side_effect=_slow_language_fetch,
            ),
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
            patch("buzz.dav_app.DavApp._build_provider_client", return_value=self.FakeProvider()),
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
            patch("buzz.dav_app.DavApp._build_provider_client", return_value=self.FakeProvider()),
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
        rd_patcher = patch(
            "buzz.dav_app.DavApp._build_provider_client",
            return_value=None,
        )
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

    def test_transient_remote_stream_failure_records_debug(self):
        dav_app = self._make_dav_app()
        node = dav_app.state.lookup("movies/Test Film/film.mkv")
        if node is None:
            self.fail("Expected snapshot node for streaming failure test")

        def raise_transient_failure(*_args):
            try:
                raise ConnectionResetError(104, "Connection reset by peer")
            except ConnectionResetError as exc:
                raise ValueError(
                    "failed to connect to upstream: "
                    "[Errno 104] Connection reset by peer"
                ) from exc

        with patch(
            "buzz.dav_app.open_remote_media",
            side_effect=raise_transient_failure,
        ), patch("buzz.dav_app.record_event") as mock_record_event:
            response = dav_app._dav_remote_response(
                node,
                True,
                None,
                "movies/Test Film/film.mkv",
            )

        self.assertEqual(response.status_code, 502)
        mock_record_event.assert_called_once()
        self.assertEqual(mock_record_event.call_args.kwargs["level"], "debug")

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
            with patch.dict(os.environ, {"BUZZ_OVERRIDES": "/nonexistent/buzz.overrides.yml"}):
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
                    "version: 1\nprovider:\n  token: testtoken\n"
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
                    "version: 1\nprovider:\n  token: testtoken\n"
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
                    "version: 1\nprovider:\n  token: testtoken\n"
                    f"state_dir: {tmpdir}\n"
                    "media_server:\n"
                    "  kind: plex\n"
                    "  trigger_lib_scan: true\n"
                    "  scan_probe:\n"
                    "    enabled: true\n"
                    "    sample_ratio_percent: 25\n"
                    "    min_files: 2\n"
                    "    max_attempts: 4\n"
                    "    read_bytes: 1024\n"
                    "    retry_delay_secs: 1.5\n"
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
            self.assertTrue(config.scan_probe.enabled)
            self.assertEqual(config.scan_probe.sample_ratio_percent, 25)
            self.assertEqual(config.scan_probe.min_files, 2)
            self.assertEqual(config.scan_probe.max_attempts, 4)
            self.assertEqual(config.scan_probe.read_bytes, 1024)
            self.assertEqual(config.scan_probe.retry_delay_secs, 1.5)

    def test_curator_config_library_map_default_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                f"version: 1\nprovider:\n  token: testtoken\nstate_dir: {tmpdir}\n",
                encoding="utf-8",
            )

            config = CuratorConfig.load(str(base_path))

            self.assertEqual(config.jellyfin_library_map, {})

    def test_dav_config_round_trips_library_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            base_path.write_text(
                (
                    "version: 1\nprovider:\n  token: testtoken\n"
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
                    "version: 1\nprovider:\n  token: testtoken\n"
                    f"state_dir: {tmpdir}\n"
                    "media_server:\n"
                    "  kind: jellyfin\n"
                    "  trigger_lib_scan: true\n"
                    "  scan_probe:\n"
                    "    enabled: false\n"
                    "    sample_ratio_percent: 25\n"
                    "    min_files: 2\n"
                    "    max_attempts: 4\n"
                    "    read_bytes: 1024\n"
                    "    retry_delay_secs: 1.5\n"
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
                nested["media_server"]["scan_probe"],
                {
                    "enabled": False,
                    "sample_ratio_percent": 25,
                    "min_files": 2,
                    "max_attempts": 4,
                    "read_bytes": 1024,
                    "retry_delay_secs": 1.5,
                    "concurrency": 4,
                },
            )
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
                f"provider:\n  token: testtoken\n  connection_concurrency: 8\nstate_dir: {tmpdir}\n",
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
                f"provider:\n  token: testtoken\nserver:\n  connection_concurrency: 8\nstate_dir: {tmpdir}\n",
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
        self.assertIn('name="media_server.scan_probe.enabled"', template)
        self.assertIn(
            'name="media_server.scan_probe.sample_ratio_percent"', template
        )
        self.assertIn('name="media_server.scan_probe.min_files"', template)
        self.assertIn('name="media_server.scan_probe.max_attempts"', template)
        self.assertIn('name="media_server.scan_probe.read_bytes"', template)
        self.assertIn(
            'name="media_server.scan_probe.retry_delay_secs"', template
        )
        self.assertIn(
            'name="media_server.scan_probe.concurrency"', template
        )
        self.assertIn('name="media_server.jellyfin.url"', template)
        self.assertIn('name="media_server.jellyfin.api_key"', template)
        self.assertIn('name="media_server.jellyfin.scan_task_id"', template)
        self.assertIn('name="media_server.plex.url"', template)
        self.assertIn('name="media_server.plex.token"', template)

    def test_config_form_parses_scan_probe_fields(self):
        from buzz.ui_live import _config_overrides_from_payload

        overrides = _config_overrides_from_payload(
            {
                "media_server.scan_probe.enabled": "on",
                "media_server.scan_probe.sample_ratio_percent": "25",
                "media_server.scan_probe.min_files": "2",
                "media_server.scan_probe.max_attempts": "4",
                "media_server.scan_probe.read_bytes": "1024",
                "media_server.scan_probe.retry_delay_secs": "1.5",
            }
        )

        self.assertEqual(
            overrides["media_server"]["scan_probe"],
            {
                "enabled": True,
                "sample_ratio_percent": 25,
                "min_files": 2,
                "max_attempts": 4,
                "read_bytes": 1024,
                "retry_delay_secs": 1.5,
            },
        )

    def test_config_load_with_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "buzz.yml"
            overrides_path = Path(tmpdir) / "buzz.overrides.yml"
            base_path.write_text(
                f"version: 1\nprovider:\n  token: testtoken\nserver:\n  port: 9999\nstate_dir: {tmpdir}\n", encoding="utf-8"
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
            rd_patcher = patch("buzz.dav_app.DavApp._build_provider_client", return_value=DavAppTests.FakeProvider())
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
                f"version: 1\nprovider:\n  token: testtoken\nserver:\n  port: 9999\nstate_dir: {tmpdir}\n", encoding="utf-8"
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.DavApp._build_provider_client", return_value=DavAppTests.FakeProvider())
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
                f"version: 1\nprovider:\n  token: testtoken\nstate_dir: {tmpdir}\n",
                encoding="utf-8",
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.DavApp._build_provider_client", return_value=DavAppTests.FakeProvider())
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
                f"version: 1\nprovider:\n  token: testtoken\nstate_dir: {tmpdir}\n",
                encoding="utf-8",
            )
            overrides_path.write_text(
                "logging:\n  verbose: true\n",
                encoding="utf-8",
            )
            config = Config.load(str(base_path))
            rd_patcher = patch("buzz.dav_app.DavApp._build_provider_client", return_value=DavAppTests.FakeProvider())
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
            rd_patcher = patch("buzz.dav_app.DavApp._build_provider_client", return_value=DavAppTests.FakeProvider())
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

    def test_regenerates_certificate_missing_expected_san(self):
        from cryptography import x509

        from buzz.core import tls

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cert_path = cwd / "data/tls/buzz.crt"
            key_path = cwd / "data/tls/buzz.key"
            cert_path.parent.mkdir(parents=True)

            # Generate a stale cert covering only localhost.
            original = tls.EXPECTED_DNS_NAMES
            tls.EXPECTED_DNS_NAMES = ("localhost",)
            try:
                tls._generate_cert_pair(
                    cert_path, key_path, valid_days=tls.DEFAULT_VALID_DAYS
                )
            finally:
                tls.EXPECTED_DNS_NAMES = original

            result = ensure_tls_certificate(cwd=cwd)

            self.assertTrue(result.generated)
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            self.assertIn(
                "buzz-dav", san.get_values_for_type(x509.DNSName)
            )


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

class SubtitleQueryOverrideTests(unittest.TestCase):
    def test_db_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = db.connect(Path(tmpdir) / "buzz.sqlite")
            db.apply_migrations(conn)

            self.assertEqual(db.load_subtitle_query_overrides(conn), {})
            self.assertIsNone(
                db.get_subtitle_query_override(conn, "ABC123", "file.mkv")
            )

            # Hash is normalized to lowercase on write.
            db.save_subtitle_query_override(conn, "ABC123", "file.mkv", "Title")
            self.assertEqual(
                db.load_subtitle_query_overrides(conn),
                {("abc123", "file.mkv"): "Title"},
            )
            self.assertEqual(
                db.get_subtitle_query_override(conn, "abc123", "file.mkv"),
                "Title",
            )

            # Empty query deletes the row.
            db.save_subtitle_query_override(conn, "abc123", "file.mkv", "")
            self.assertEqual(db.load_subtitle_query_overrides(conn), {})
            self.assertIsNone(
                db.get_subtitle_query_override(conn, "abc123", "file.mkv")
            )
            conn.close()

    def test_query_override_for_entry_matches_hash_and_path(self):
        from buzz.core import subtitles

        name_to_hash = {"Movie Pack": "abc123"}
        overrides = {("abc123", "Movie.Pack.2020.mkv"): "Real Title"}
        entry = {
            "source": "movies/Movie Pack/Movie.Pack.2020.mkv",
            "target": "movies/Movie Pack (2020)/Movie Pack (2020).mkv",
            "type": "movie",
        }
        self.assertEqual(
            subtitles._query_override_for_entry(
                entry, name_to_hash, overrides
            ),
            "Real Title",
        )

        # No override stored for this file -> empty.
        other = {
            "source": "movies/Movie Pack/Other.mkv",
            "target": "movies/Other (2020)/Other (2020).mkv",
            "type": "movie",
        }
        self.assertEqual(
            subtitles._query_override_for_entry(other, name_to_hash, overrides),
            "",
        )

    def test_fetch_entry_subtitles_applies_override_to_query(self):
        from buzz.core import subtitles

        entry = {
            "source": "movies/Movie Pack/Movie.Pack.2020.mkv",
            "target": "movies/Movie Pack (2020)/Movie Pack (2020).mkv",
            "type": "movie",
        }
        # Auto-derived query comes from the folder name "Movie Pack (2020)".
        self.assertEqual(
            subtitles.get_search_params(entry)["query"], "Movie Pack"
        )

        captured = {}

        class FakeClient:
            def search(self, **kwargs):
                captured.update(kwargs)
                return []

        config = SimpleNamespace(
            subtitle_root=Path("/tmp/subs"),
            subtitles=SimpleNamespace(
                languages=["en"],
                strategy="best-match",
                filters=None,
                search_delay_secs=0,
                download_delay_secs=0,
            ),
        )
        counters = {
            "fetched": 0,
            "replaced": 0,
            "skipped": 0,
            "errors": 0,
            "already_exists": 0,
        }
        with patch("buzz.core.subtitles.state"):
            subtitles._fetch_entry_subtitles(
                cast(Any, config),
                cast(Any, FakeClient()),
                entry,
                counters,
                [],
                query_override="Custom Search Name",
            )
        self.assertEqual(captured.get("query"), "Custom Search Name")


class CuratorTitleOverrideTests(unittest.TestCase):
    def test_db_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = db.connect(Path(tmpdir) / "buzz.sqlite")
            db.apply_migrations(conn)

            self.assertEqual(db.load_curator_title_overrides(conn), {})
            self.assertIsNone(
                db.get_curator_title_override(conn, "ABC123")
            )

            # season/episode are not part of the entry-level override; they
            # only affect sub-paths, not the top-level folder, so they are
            # silently ignored on save.
            db.save_curator_title_override(
                conn,
                "ABC123",
                {
                    "kind": "show",
                    "series": "Example",
                    "year": 2024,
                    "season": 1,
                    "episode": 2,
                    "tvdbid": "tvdb-1",
                },
            )

            expected = {
                "abc123": {
                    "kind": "show",
                    "series": "Example",
                    "year": 2024,
                    "provider_ids": {"tvdbid": "tvdb-1"},
                }
            }
            self.assertEqual(db.load_curator_title_overrides(conn), expected)
            self.assertEqual(
                db.get_curator_title_override(conn, "abc123"),
                expected["abc123"],
            )

            db.save_curator_title_override(conn, "abc123", None)
            self.assertEqual(db.load_curator_title_overrides(conn), {})
            conn.close()


if __name__ == "__main__":
    unittest.main()
