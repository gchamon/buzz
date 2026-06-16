import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from watchfiles import Change

from buzz.core import db
from buzz.core.curator import (
    MediaServerAuthError,
    RebuildError,
    build_library,
    rebuild_and_trigger,
    scan_probe_sample_size,
    validate_media_server_startup_auth,
)
from buzz.core.events import record_event
from buzz.core.events import registry as event_registry
from buzz.core.media_server import JellyfinAuthProbe
from buzz.curator_app import CuratorApp, SourceRootWatcher, changed_source_roots
from buzz.models import CuratorConfig, ScanProbeConfig


class CuratorAppTests(unittest.TestCase):
    def setUp(self) -> None:
        event_registry.clear(listeners=True)
        event_registry.stdout_enabled = True

    def tearDown(self) -> None:
        event_registry.clear(listeners=True)
        event_registry.stdout_enabled = True

    def _config(self, root: Path, **overrides) -> CuratorConfig:
        defaults = {
            "source_root": root / "raw",
            "target_root": root / "curated",
            "state_dir": root / "state",
            "trigger_lib_scan": False,
            "build_on_start": False,
        }
        defaults.update(overrides)
        return CuratorConfig(
            **defaults,
        )

    def _create_source_tree(self, source_root: Path):
        movies = source_root / "movies"
        shows = source_root / "shows"
        anime = source_root / "anime"
        movies.mkdir(parents=True, exist_ok=True)
        shows.mkdir(parents=True, exist_ok=True)
        anime.mkdir(parents=True, exist_ok=True)
        (movies / "Movie.2026.1080p.mkv").write_text("video", encoding="utf-8")

    def _create_torbox_source_tree(self, source_root: Path):
        movies = source_root / "movies"
        shows = source_root / "shows"
        anime = source_root / "anime"
        movies.mkdir(parents=True, exist_ok=True)
        shows.mkdir(parents=True, exist_ok=True)
        anime.mkdir(parents=True, exist_ok=True)
        source = movies / "Torbox.File.2026.1080p.mkv"
        source.write_text("video", encoding="utf-8")
        return source

    def test_changed_source_roots_collapses_paths_to_media_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "raw"

            roots = changed_source_roots(
                root,
                [
                    str(root / "movies" / "Movie.Root" / "movie.mkv"),
                    str(root / "movies" / "Movie.Root" / "movie.srt"),
                    str(root / "shows" / "Show.Root" / "S01E01.mkv"),
                    str(root / "version.txt"),
                    "/outside/movies/Other/file.mkv",
                ],
            )

            self.assertEqual(
                roots,
                ["movies/Movie.Root", "shows/Show.Root"],
            )

    def test_source_root_watcher_rebuilds_detected_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "raw"
            source_root.mkdir(parents=True)
            app = CuratorApp(
                self._config(
                    root,
                    source_root=source_root,
                    watch_source_root=True,
                )
            )
            watcher = SourceRootWatcher(app)

            with patch(
                "buzz.curator_app.watch",
                return_value=[
                    {
                        (
                            Change.added,
                            str(
                                source_root
                                / "movies"
                                / "Movie.Root"
                                / "movie.mkv"
                            ),
                        )
                    }
                ],
            ), patch.object(app, "_run_rebuild") as rebuild:
                watcher.run()

            rebuild.assert_called_once_with(["movies/Movie.Root"])

    def test_scan_probe_sample_size_uses_percent_with_minimum(self):
        self.assertEqual(scan_probe_sample_size(125, 10, 1), 13)
        self.assertEqual(scan_probe_sample_size(9, 10, 1), 1)
        self.assertEqual(scan_probe_sample_size(0, 10, 1), 0)
        self.assertEqual(scan_probe_sample_size(5, 0, 1), 1)

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_scan_probe_logs_start_and_success_counts(self, mock_validate):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
                scan_probe=ScanProbeConfig(sample_ratio_percent=100),
            )
            self._create_source_tree(config.source_root)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch(
                "buzz.core.curator.trigger_jellyfin_scan"
            ):
                rebuild_and_trigger(config)

            logged = stdout.getvalue()
            self.assertIn(
                "starting Jellyfin scan probe: 1 of 1 file(s)", logged
            )
            self.assertIn("jellyfin scan probe succeeded", logged)

    def test_build_library_accepts_canonical_curator_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            report = build_library(self._config(root))

            self.assertEqual(report["movies"], 0)
            self.assertEqual(report["show_files"], 0)
            self.assertEqual(report["anime_files"], 0)

    def test_build_library_resolves_hash_named_show_root_from_library_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            hash_name = "909fb7428de59f84d819a6bed64e7f37bbf10464"
            show_root = config.source_root / "shows" / hash_name
            show_root.mkdir(parents=True)
            (config.source_root / "movies").mkdir(parents=True)
            (config.source_root / "anime").mkdir(parents=True)
            (
                show_root
                / "Adventure Time (2008) - S00E01 - Pilot.mkv"
            ).write_text("video", encoding="utf-8")
            (
                show_root
                / "Adventure Time (2008) - S00E02 - The Wand.mkv"
            ).write_text("video", encoding="utf-8")

            config.state_dir.mkdir(parents=True, exist_ok=True)
            conn = db.connect(config.state_dir / "buzz.sqlite")
            try:
                db.apply_migrations(conn)
                conn.execute(
                    "INSERT INTO library_entries "
                    "(hash, name, files_json, updated_at) "
                    "VALUES (?, ?, '[]', '2026-01-01T00:00:00Z')",
                    (hash_name, "Adventure Time (2008)"),
                )
                conn.commit()
            finally:
                conn.close()

            report = build_library(config)

            self.assertEqual(report["show_files"], 2)
            self.assertTrue(
                (
                    config.target_root
                    / "shows"
                    / "Adventure Time (2008)"
                    / "Season 00"
                    / "Adventure Time (2008) S00E01.mkv"
                ).exists()
            )
            self.assertFalse(
                (config.target_root / "shows" / hash_name).exists()
            )

    def test_build_library_resolves_hash_show_root_from_provider_info(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            hash_name = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            show_root = config.source_root / "shows" / hash_name
            show_root.mkdir(parents=True)
            (config.source_root / "movies").mkdir(parents=True)
            (config.source_root / "anime").mkdir(parents=True)
            (
                show_root
                / "Example Show - S01E01 - Start.mkv"
            ).write_text("video", encoding="utf-8")

            config.state_dir.mkdir(parents=True, exist_ok=True)
            conn = db.connect(config.state_dir / "buzz.sqlite")
            try:
                db.apply_migrations(conn)
                conn.execute(
                    "INSERT INTO library_entries "
                    "(hash, name, files_json, updated_at) "
                    "VALUES (?, NULL, '[]', '2026-01-01T00:00:00Z')",
                    (hash_name,),
                )
                conn.execute(
                    "INSERT INTO provider_links "
                    "(provider, provider_torrent_id, hash, info_json, "
                    "signature_json, updated_at) "
                    "VALUES ('torbox', 'TB1', ?, ?, '{}', "
                    "'2026-01-01T00:00:00Z')",
                    (
                        hash_name,
                        json.dumps(
                            {
                                "original_filename": (
                                    "Example Show (2026) Complete"
                                )
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            report = build_library(config)

            self.assertEqual(report["show_files"], 1)
            self.assertTrue(
                (
                    config.target_root
                    / "shows"
                    / "Example Show (2026)"
                    / "Season 01"
                    / "Example Show (2026) S01E01.mkv"
                ).exists()
            )

    def test_build_library_keeps_valid_show_files_when_one_file_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            show_root = config.source_root / "shows" / "Example.Show"
            show_root.mkdir(parents=True)
            (config.source_root / "movies").mkdir(parents=True)
            (config.source_root / "anime").mkdir(parents=True)
            (show_root / "Example Show - S01E01.mkv").write_text(
                "video", encoding="utf-8"
            )
            (show_root / "Behind the Scenes.mkv").write_text(
                "video", encoding="utf-8"
            )

            report = build_library(config)

            self.assertEqual(report["show_files"], 1)
            self.assertEqual(len(report["skipped_shows"]), 1)
            self.assertTrue(
                (
                    config.target_root
                    / "shows"
                    / "Example Show"
                    / "Season 01"
                    / "Example Show S01E01.mkv"
                ).exists()
            )

    def test_curator_app_routes_use_fastapi(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            app = CuratorApp(self._config(root))
            event_registry.stdout_enabled = True
            client = TestClient(app.app)

            health = client.get("/healthz")
            rebuild = client.post("/rebuild")

            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json(), {"status": "ok"})
            self.assertEqual(rebuild.status_code, 200)
            self.assertEqual(rebuild.json(), {"status": "rebuilding"})

    def test_curator_lifespan_runs_startup_build(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            app = CuratorApp(self._config(root, build_on_start=True))
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with TestClient(app.app) as client:
                    response = client.get("/healthz")

            self.assertEqual(response.status_code, 200)
            conn = db.connect(root / "state" / "buzz.sqlite")
            db.apply_migrations(conn)
            try:
                report = db.load_curator_report(conn)
            finally:
                conn.close()
            self.assertIsNotNone(report)

    def test_curator_lifespan_notifies_dav_when_startup_is_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            response = MagicMock()
            response.status = 200
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            config = self._config(
                root,
                dav_ui_notify_url="http://buzz-dav:9999/api/ui/notify",
            )

            with patch(
                "urllib.request.urlopen", return_value=response
            ) as mock_urlopen:
                with TestClient(CuratorApp(config).app) as client:
                    self.assertEqual(client.get("/healthz").status_code, 200)

            payloads = []
            for call in mock_urlopen.call_args_list:
                request_obj = call.args[0]
                if getattr(request_obj, "method", "") != "POST":
                    continue
                payloads.append(json.loads(request_obj.data.decode("utf-8")))

            self.assertTrue(
                any(
                    payload["message"].get("event") == "curator_ready"
                    for payload in payloads
                )
            )

    def test_curator_lifespan_unregisters_dav_notify_listener(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            response = MagicMock()
            response.status = 200
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            config = self._config(
                root,
                dav_ui_notify_url="http://buzz-dav:9999/api/ui/notify",
            )

            with patch(
                "urllib.request.urlopen", return_value=response
            ) as mock_urlopen:
                with TestClient(CuratorApp(config).app) as client:
                    self.assertEqual(client.get("/healthz").status_code, 200)
                calls_during_lifespan = mock_urlopen.call_count
                record_event("post-shutdown event")

            self.assertGreater(calls_during_lifespan, 0)
            self.assertEqual(mock_urlopen.call_count, calls_during_lifespan)

    def test_curator_lifespan_logs_startup_auth_error_and_stays_up(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            app = CuratorApp(self._config(root))
            event_registry.stdout_enabled = True
            stdout = io.StringIO()
            with patch(
                "buzz.curator_app.validate_media_server_startup_auth",
                side_effect=MediaServerAuthError("bad auth"),
            ):
                with patch("sys.stdout", stdout):
                    with TestClient(app.app) as client:
                        response = client.get("/healthz")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Curator startup failed: bad auth", stdout.getvalue())
            self.assertIn("Curator startup complete", stdout.getvalue())

    def test_curator_lifespan_logs_startup_auth_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
            )
            app = CuratorApp(config)
            event_registry.stdout_enabled = True
            stdout = io.StringIO()
            with patch(
                "buzz.curator_app.validate_media_server_startup_auth"
            ) as validate:
                with patch("sys.stdout", stdout):
                    with TestClient(app.app) as client:
                        response = client.get("/healthz")

            self.assertEqual(response.status_code, 200)
            validate.assert_called_once_with(config)
            self.assertIn("Jellyfin API token validated", stdout.getvalue())

    def test_startup_auth_allows_missing_token_when_scan_trigger_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root, trigger_lib_scan=False, jellyfin_api_key=""
            )

            with patch("buzz.core.curator.probe_jellyfin_auth") as probe:
                validate_media_server_startup_auth(config)

            probe.assert_not_called()

    def test_startup_auth_requires_token_when_scan_trigger_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root, trigger_lib_scan=True, jellyfin_api_key=""
            )

            with self.assertRaisesRegex(
                MediaServerAuthError, "media_server.jellyfin.api_key"
            ):
                validate_media_server_startup_auth(config)

    def test_startup_auth_rejects_invalid_token_immediately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root, trigger_lib_scan=True, jellyfin_api_key="bad-token"
            )

            with patch(
                "buzz.core.curator.probe_jellyfin_auth",
                return_value=JellyfinAuthProbe(
                    valid=False,
                    invalid_token=True,
                    error="unauthorized",
                ),
            ) as probe:
                with self.assertRaisesRegex(
                    MediaServerAuthError, "invalid or unauthorized"
                ):
                    validate_media_server_startup_auth(config)

            probe.assert_called_once_with(config)

    def test_startup_auth_retries_until_jellyfin_is_reachable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root, trigger_lib_scan=True, jellyfin_api_key="token"
            )
            now = [0.0]

            def monotonic():
                return now[0]

            def sleep(seconds: float) -> None:
                now[0] += seconds

            with patch(
                "buzz.core.curator.probe_jellyfin_auth",
                side_effect=[
                    JellyfinAuthProbe(
                        valid=False,
                        unreachable=True,
                        error="connection refused",
                    ),
                    JellyfinAuthProbe(valid=True),
                ],
            ) as probe:
                validate_media_server_startup_auth(
                    config,
                    timeout_secs=300,
                    retry_interval_secs=5,
                    sleep=sleep,
                    monotonic=monotonic,
                )

            self.assertEqual(probe.call_count, 2)
            self.assertEqual(now[0], 5)

    def test_startup_auth_fails_when_jellyfin_stays_unreachable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root, trigger_lib_scan=True, jellyfin_api_key="token"
            )
            now = [0.0]

            def monotonic():
                return now[0]

            def sleep(seconds: float) -> None:
                now[0] += seconds

            with patch(
                "buzz.core.curator.probe_jellyfin_auth",
                return_value=JellyfinAuthProbe(
                    valid=False,
                    unreachable=True,
                    error="connection refused",
                ),
            ):
                with self.assertRaisesRegex(
                    MediaServerAuthError, "jellyfin is unreachable"
                ):
                    validate_media_server_startup_auth(
                        config,
                        timeout_secs=10,
                        retry_interval_secs=5,
                        sleep=sleep,
                        monotonic=monotonic,
                    )

    def test_curator_subtitle_fetch_uses_consistent_torrent_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_dir = root / "state"
            state_dir.mkdir(parents=True)

            # Torrent name logic: original_filename or filename
            # raw tree uses _torrent_name() which prefers original_filename
            # state.py torrents() now also uses _torrent_name()

            torrent_id = "abc123"
            original_filename = (
                "The Imaginarium of Doctor Parnassus 2009 BRrip"
            )
            filename = "The Imaginarium of Doctor Parnassus.mp4"

            # 1. Mock state and client to return our torrent
            from buzz.core.state import BuzzState
            from buzz.models import DavConfig

            mock_client = MagicMock()
            # Mock torrents.get_info
            mock_client.torrents.get_info.return_value.json.return_value = {
                "id": torrent_id,
                "filename": filename,
                "original_filename": original_filename,
                "status": "downloaded",
                "progress": 100,
                "bytes": 1000,
                "files": [{"selected": 1}],
                "links": ["link1"],
                "ended": "2024-04-21",
            }

            dav_config = DavConfig(
                state_dir=str(state_dir), token="test-token"
            )
            state = BuzzState(dav_config, mock_client)

            # Manually seed cache since update() doesn't exist (it's part of sync())
            state.cache[torrent_id] = {
                "info": {
                    "id": torrent_id,
                    "filename": filename,
                    "original_filename": original_filename,
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 1000,
                    "files": [{"selected": 1}],
                    "links": ["link1"],
                    "ended": "2024-04-21",
                }
            }

            # Verify state.torrents() returns the consistent name
            torrents = state.torrents()
            self.assertEqual(len(torrents), 1)
            self.assertEqual(torrents[0]["name"], original_filename)

            # 2. Verify curator app trigger uses this name
            from buzz.models import SubtitleConfig

            config = self._config(
                root, subtitles=SubtitleConfig(enabled=True, api_key="test")
            )
            app = CuratorApp(config)
            client = TestClient(app.app)

            # Mock _run_subtitle_fetch to capture what name it gets
            with patch.object(
                app, "_run_subtitle_fetch"
            ) as mock_fetch:
                response = client.post(
                    "/api/subtitles/fetch",
                    json={"torrent_name": torrents[0]["name"]},
                )
                self.assertEqual(response.status_code, 200)
                mock_fetch.assert_called_once()
                self.assertEqual(
                    mock_fetch.call_args[0][0], original_filename
                )

            # 3. Verify mapping match
            from buzz.core.subtitles import _source_matches_torrent

            # mapping source path looks like: category/TorrentName/file
            source_path = f"movies/{original_filename}/{filename}"
            self.assertTrue(
                _source_matches_torrent(source_path, original_filename)
            )

    @patch("buzz.curator_app.httpx.Client")
    def test_subtitle_fetch_failure_signals_dav_task_failed(
        self, mock_client_cls
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            from buzz.models import SubtitleConfig

            config = self._config(
                root,
                dav_url="http://buzz-dav:8080",
                dav_tls_cert="",
                subtitles=SubtitleConfig(enabled=True, api_key="test"),
            )
            app = CuratorApp(config)
            response = MagicMock()
            response.status_code = 200
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_client.post.return_value = response

            app._run_subtitle_fetch(
                task_id="task-1",
                torrent_names=["Missing.Movie.2026"],
            )

            completion_calls = [
                call
                for call in mock_client.post.call_args_list
                if call.args[0].endswith("/api/tasks/task-1/complete")
            ]
            self.assertEqual(len(completion_calls), 1)
            url = completion_calls[0].args[0]
            payload = completion_calls[0].kwargs["json"]
            self.assertEqual(
                url, "http://buzz-dav:8080/api/tasks/task-1/complete"
            )
            self.assertIn("error", payload)
            self.assertIn("no library mapping found", payload["error"])

    def test_curator_config_load_merges_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base_path = root / "buzz.yml"
            overrides_path = root / "state" / "buzz.overrides.yml"
            overrides_path.parent.mkdir(parents=True)
            base_path.write_text(
                "provider:\n  token: testtoken\n"
                f"state_dir: {overrides_path.parent}\n"
                "subtitles:\n  enabled: false\n  strategy: most-downloaded\n",
                encoding="utf-8",
            )
            overrides_path.write_text(
                "subtitles:\n  enabled: true\n  strategy: trusted\n",
                encoding="utf-8",
            )

            config = CuratorConfig.load(str(base_path))

            self.assertTrue(config.subtitles.enabled)
            self.assertEqual(config.subtitles.strategy, "trusted")

    def test_curator_reload_endpoint_refreshes_subtitle_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base_path = root / "buzz.yml"
            overrides_path = root / "state" / "buzz.overrides.yml"
            overrides_path.parent.mkdir(parents=True)
            base_path.write_text(
                "provider:\n  token: testtoken\n"
                f"state_dir: {overrides_path.parent}\n"
                "subtitles:\n  enabled: false\n",
                encoding="utf-8",
            )
            overrides_path.write_text(
                "subtitles:\n  enabled: true\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"BUZZ_CONFIG": str(base_path)}):
                app = CuratorApp(CuratorConfig.load(str(base_path)))
                client = TestClient(app.app)
                app.config.subtitles.enabled = False

                response = client.post("/api/config/reload")

            self.assertEqual(response.status_code, 200)
            self.assertTrue(app.config.subtitles.enabled)

    def test_curator_rebuild_error_is_logged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            app = CuratorApp(self._config(root))
            event_registry.stdout_enabled = True

            with patch.object(
                app.curator,
                "handle_rebuild",
                side_effect=RebuildError(
                    "scan failed",
                    {
                        "jellyfin_scan_status": "failed",
                        "jellyfin_scan_triggered": False,
                    },
                ),
            ):
                with patch("sys.stdout", io.StringIO()) as stdout:
                    app._run_rebuild([])

            logged = stdout.getvalue()
            self.assertIn("curator rebuild failed: scan failed", logged)

    def test_rebuild_and_trigger_skips_scan_when_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root, trigger_lib_scan=False)
            self._create_source_tree(config.source_root)

            report = rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            self.assertFalse(report["jellyfin_scan_triggered"])
            self.assertEqual(
                report["jellyfin_scan_status"], "skipped_configured"
            )
            self.assertIsNone(report["jellyfin_scan_error"])

    def test_build_library_applies_db_curator_title_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            movie_dir = config.source_root / "movies" / "Movie Pack"
            movie_dir.mkdir(parents=True)
            (config.source_root / "shows").mkdir(parents=True)
            (config.source_root / "anime").mkdir(parents=True)
            movie_file = movie_dir / "Movie.Pack.2020.mkv"
            movie_file.write_text("video", encoding="utf-8")

            config.state_dir.mkdir(parents=True)
            conn = db.connect(config.state_dir / "buzz.sqlite")
            try:
                db.apply_migrations(conn)
                db.replace_provider_library(
                    conn,
                    [
                        {
                            "hash": "abc123",
                            "name": "Movie Pack",
                            "bytes": 100,
                            "files": [
                                {
                                    "id": "1",
                                    "path": "Movie.Pack.2020.mkv",
                                    "bytes": 100,
                                }
                            ],
                            "magnet": None,
                            "provider": "real_debrid",
                            "provider_torrent_id": "RD1",
                            "status": "downloaded",
                            "progress": 100,
                            "info_json": "{}",
                            "signature_json": "{}",
                        }
                    ],
                )
                db.save_curator_title_override(
                    conn,
                    "abc123",
                    {
                        "kind": "movie",
                        "title": "Real Title",
                        "year": 2021,
                        "provider_ids": {"imdbid": "tt1234567"},
                    },
                )
            finally:
                conn.close()

            report = build_library(config)

            self.assertEqual(report["movies"], 1)
            self.assertTrue(
                (
                    config.target_root
                    / "movies"
                    / "Real Title (2021) [imdbid-tt1234567]"
                    / "Real Title (2021) [imdbid-tt1234567].mkv"
                ).is_symlink()
            )

    def test_db_show_override_renames_series_folder_entry_wide(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            (config.source_root / "movies").mkdir(parents=True)
            (config.source_root / "anime").mkdir(parents=True)
            show_dir = config.source_root / "shows" / "Example.Show.S01"
            show_dir.mkdir(parents=True)
            for episode in ("E01", "E02"):
                (show_dir / f"Example.Show.S01{episode}.mkv").write_text(
                    "video", encoding="utf-8"
                )

            config.state_dir.mkdir(parents=True)
            conn = db.connect(config.state_dir / "buzz.sqlite")
            try:
                db.apply_migrations(conn)
                db.replace_provider_library(
                    conn,
                    [
                        {
                            "hash": "show1",
                            "name": "Example.Show.S01",
                            "bytes": 200,
                            "files": [
                                {
                                    "id": "1",
                                    "path": "Example.Show.S01E01.mkv",
                                    "bytes": 100,
                                },
                                {
                                    "id": "2",
                                    "path": "Example.Show.S01E02.mkv",
                                    "bytes": 100,
                                },
                            ],
                            "magnet": None,
                            "provider": "real_debrid",
                            "provider_torrent_id": "RD2",
                            "status": "downloaded",
                            "progress": 100,
                            "info_json": "{}",
                            "signature_json": "{}",
                        }
                    ],
                )
                # season/episode are intentionally omitted: only the top-level
                # series folder is overridable; episode numbering is parsed.
                db.save_curator_title_override(
                    conn,
                    "show1",
                    {
                        "kind": "show",
                        "series": "Real Series",
                        "year": 2022,
                        "provider_ids": {"tvdbid": "12345"},
                    },
                )
            finally:
                conn.close()

            report = build_library(config)

            self.assertEqual(report["show_files"], 2)
            series_dir = (
                config.target_root / "shows" / "Real Series (2022) [tvdbid-12345]"
            )
            season_dir = series_dir / "Season 01"
            # Both episodes share the single overridden top-level folder, with
            # season/episode still derived from each filename.
            self.assertTrue(
                (
                    season_dir
                    / "Real Series (2022) [tvdbid-12345] S01E01.mkv"
                ).is_symlink()
            )
            self.assertTrue(
                (
                    season_dir
                    / "Real Series (2022) [tvdbid-12345] S01E02.mkv"
                ).is_symlink()
            )

    def test_db_anime_override_renames_top_level_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            (config.source_root / "movies").mkdir(parents=True)
            (config.source_root / "shows").mkdir(parents=True)
            anime_dir = config.source_root / "anime" / "Anime.Show.S01"
            (anime_dir / "Season 01").mkdir(parents=True)
            (
                anime_dir / "Season 01" / "Anime.Show.S01E01.mkv"
            ).write_text("video", encoding="utf-8")

            config.state_dir.mkdir(parents=True)
            conn = db.connect(config.state_dir / "buzz.sqlite")
            try:
                db.apply_migrations(conn)
                db.replace_provider_library(
                    conn,
                    [
                        {
                            "hash": "anime1",
                            "name": "Anime.Show.S01",
                            "bytes": 100,
                            "files": [
                                {
                                    "id": "1",
                                    "path": (
                                        "Season 01/Anime.Show.S01E01.mkv"
                                    ),
                                    "bytes": 100,
                                }
                            ],
                            "magnet": None,
                            "provider": "real_debrid",
                            "provider_torrent_id": "RD3",
                            "status": "downloaded",
                            "progress": 100,
                            "info_json": "{}",
                            "signature_json": "{}",
                        }
                    ],
                )
                db.save_curator_title_override(
                    conn,
                    "anime1",
                    {
                        "kind": "anime",
                        "series": "Real Anime",
                        "year": 2023,
                        "provider_ids": {"anidbid": "9876"},
                    },
                )
            finally:
                conn.close()

            report = build_library(config)

            self.assertEqual(report["anime_files"], 1)
            # Only the top-level folder is renamed; the inner structure
            # ("Season 01/<file>") is preserved verbatim.
            self.assertTrue(
                (
                    config.target_root
                    / "animes"
                    / "Real Anime (2023) [anidbid-9876]"
                    / "Season 01"
                    / "Anime.Show.S01E01.mkv"
                ).is_symlink()
            )

    def test_anime_without_override_preserves_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            (config.source_root / "movies").mkdir(parents=True)
            (config.source_root / "shows").mkdir(parents=True)
            anime_dir = config.source_root / "anime" / "Anime.Show.S01"
            anime_dir.mkdir(parents=True)
            (anime_dir / "Anime.Show.S01E01.mkv").write_text(
                "video", encoding="utf-8"
            )

            report = build_library(config)

            self.assertEqual(report["anime_files"], 1)
            self.assertTrue(
                (
                    config.target_root
                    / "animes"
                    / "Anime.Show.S01"
                    / "Anime.Show.S01E01.mkv"
                ).is_symlink()
            )

    def test_rebuild_and_trigger_skips_scan_when_api_key_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root, trigger_lib_scan=True, jellyfin_api_key=""
            )
            self._create_source_tree(config.source_root)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                report = rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            self.assertFalse(report["jellyfin_scan_triggered"])
            self.assertEqual(
                report["jellyfin_scan_status"], "skipped_missing_auth"
            )
            self.assertIsNone(report["jellyfin_scan_error"])
            self.assertIn(
                "media_server.jellyfin.api_key is empty",
                stdout.getvalue(),
            )
            # The curator now always logs mapping events, summarizing the
            # library size and diff in the message.
            self.assertIn(
                "Curator mapping updated: 1 entries (+1 -0 ~0)",
                stdout.getvalue(),
            )

    def test_rebuild_and_trigger_warns_when_plex_token_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                media_server_kind="plex",
                plex_token="",
                trigger_lib_scan=True,
            )
            self._create_source_tree(config.source_root)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                report = rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            self.assertFalse(report["jellyfin_scan_triggered"])
            self.assertEqual(
                report["jellyfin_scan_status"], "skipped_missing_auth"
            )
            self.assertIsNone(report["jellyfin_scan_error"])
            self.assertIn("media_server.plex.token is empty", stdout.getvalue())

    def test_rebuild_logs_mapping_when_verbose_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root, verbose=True)
            self._create_source_tree(config.source_root)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                report = rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            lines = [line for line in stdout.getvalue().splitlines() if line]
            last_line = lines[-1]
            json_start = last_line.find("{")
            mapping_log = json.loads(last_line[json_start:])
            self.assertEqual(mapping_log["event"], "curator_mapping_diff")
            self.assertEqual(mapping_log["mapping_entries"], 1)
            self.assertEqual(mapping_log["removed"], [])
            self.assertEqual(mapping_log["changed"], [])
            self.assertEqual(
                mapping_log["added"],
                [
                    {
                        "source": "movies/Movie.2026.1080p.mkv",
                        "target": "movies/Movie (2026)/Movie (2026).mkv",
                        "type": "movie",
                    }
                ],
            )

    def test_rebuild_logs_empty_diff_when_mapping_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root, verbose=True)
            self._create_source_tree(config.source_root)

            with patch("sys.stdout", io.StringIO()):
                rebuild_and_trigger(config)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                report = rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            lines = [line for line in stdout.getvalue().splitlines() if line]
            last_line = lines[-1]
            json_start = last_line.find("{")
            mapping_log = json.loads(last_line[json_start:])
            self.assertEqual(mapping_log["event"], "curator_mapping_diff")
            self.assertEqual(mapping_log["added"], [])
            self.assertEqual(mapping_log["removed"], [])
            self.assertEqual(mapping_log["changed"], [])
            self.assertTrue(
                last_line.startswith("Curator mapping unchanged: 1 entries"),
                last_line,
            )

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_rebuild_and_trigger_calls_jellyfin_scan_when_auth_is_configured(
        self, mock_validate
    ):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
                jellyfin_scan_task_id="scan-task",
            )
            self._create_source_tree(config.source_root)

            with patch(
                "buzz.core.curator.trigger_jellyfin_scan"
            ) as trigger_scan:
                report = rebuild_and_trigger(config)

            trigger_scan.assert_called_once_with(config)
            self.assertTrue(report["jellyfin_scan_triggered"])
            self.assertEqual(report["jellyfin_scan_status"], "full_triggered")
            self.assertIsNone(report["jellyfin_scan_error"])

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_rebuild_and_trigger_calls_selective_refresh_when_changed_roots_provided(
        self, mock_validate
    ):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root, trigger_lib_scan=True, jellyfin_api_key="token"
            )
            self._create_source_tree(config.source_root)

            with patch(
                "buzz.core.curator.trigger_jellyfin_selective_refresh"
            ) as trigger_selective:
                report = rebuild_and_trigger(
                    config, changed_roots=["movies/Movie.2026.1080p.mkv"]
                )

            trigger_selective.assert_called_once_with(
                config, ["movies/Movie.2026.1080p.mkv"]
            )
            self.assertTrue(report["jellyfin_scan_triggered"])
            self.assertEqual(
                report["jellyfin_scan_status"], "selective_triggered"
            )

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_scan_probe_matches_changed_root_against_mapping_target(
        self, mock_validate
    ):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
                scan_probe=ScanProbeConfig(sample_ratio_percent=100),
            )
            self._create_torbox_source_tree(config.source_root)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch(
                "buzz.core.curator.trigger_jellyfin_selective_refresh"
            ) as trigger_selective:
                report = rebuild_and_trigger(
                    config,
                    changed_roots=["movies/Torbox File (2026)"],
                )

            trigger_selective.assert_called_once_with(
                config, ["movies/Torbox File (2026)"]
            )
            self.assertTrue(report["jellyfin_scan_triggered"])
            self.assertIn("jellyfin scan probe succeeded", stdout.getvalue())
            self.assertNotIn(
                "probing full mapped library",
                stdout.getvalue(),
            )

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_scan_probe_falls_back_to_full_pool_for_unmatched_changed_roots(
        self, mock_validate
    ):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
                scan_probe=ScanProbeConfig(sample_ratio_percent=100),
            )
            self._create_torbox_source_tree(config.source_root)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch(
                "buzz.core.curator.trigger_jellyfin_selective_refresh"
            ) as trigger_selective:
                report = rebuild_and_trigger(
                    config,
                    changed_roots=["movies/Torbox Torrent"],
                )

            trigger_selective.assert_called_once_with(
                config, ["movies/Torbox Torrent"]
            )
            self.assertTrue(report["jellyfin_scan_triggered"])
            logged = stdout.getvalue()
            self.assertIn("probing full mapped library", logged)
            self.assertIn("jellyfin scan probe succeeded", logged)

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_rebuild_and_trigger_skips_scan_when_probe_fails(
        self, mock_validate
    ):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
                scan_probe=ScanProbeConfig(max_attempts=2, retry_delay_secs=0),
            )
            self._create_source_tree(config.source_root)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch(
                "buzz.core.curator._read_probe_file",
                side_effect=OSError("upstream returned HTTP 503"),
            ), patch("buzz.core.curator.trigger_jellyfin_scan") as trigger_scan:
                report = rebuild_and_trigger(config)

            trigger_scan.assert_not_called()
            self.assertFalse(report["jellyfin_scan_triggered"])
            self.assertEqual(
                report["jellyfin_scan_status"], "skipped_probe_failed"
            )
            self.assertIn("HTTP 503", report["jellyfin_scan_error"])
            self.assertIn("jellyfin scan skipped", stdout.getvalue())
            self.assertIn("media probe failed", stdout.getvalue())

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_rebuild_and_trigger_can_disable_scan_probe(self, mock_validate):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
                scan_probe=ScanProbeConfig(enabled=False),
            )
            self._create_source_tree(config.source_root)

            with patch(
                "buzz.core.curator._read_probe_file",
                side_effect=OSError("should not probe"),
            ), patch("buzz.core.curator.trigger_jellyfin_scan") as trigger_scan:
                report = rebuild_and_trigger(config)

            trigger_scan.assert_called_once_with(config)
            self.assertTrue(report["jellyfin_scan_triggered"])

    @patch("buzz.core.media_server.discover_jellyfin_libraries")
    @patch("urllib.request.urlopen")
    def test_trigger_jellyfin_selective_refresh_calls_correct_id(
        self, mock_urlopen, mock_discover
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                jellyfin_api_key="token",
                jellyfin_library_map={"movies": "Movies"},
            )
            mock_discover.return_value = {"Movies": "movie-id-123"}

            from buzz.core.curator import trigger_jellyfin_selective_refresh

            trigger_jellyfin_selective_refresh(config, ["movies/MyMovie"])

            # Verify that urlopen was called with the refresh URL for movie-id-123
            calls = [call.args[0] for call in mock_urlopen.call_args_list]
            refresh_url = f"{config.jellyfin_url}/Items/movie-id-123/Refresh"
            self.assertTrue(
                any(
                    refresh_url
                    in (url.full_url if hasattr(url, "full_url") else str(url))
                    for url in calls
                )
            )

    @patch("buzz.core.media_server.trigger_jellyfin_scan")
    @patch("buzz.core.media_server.discover_jellyfin_libraries")
    def test_selective_refresh_warns_with_available_jellyfin_libraries(
        self, mock_discover, mock_scan
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                jellyfin_api_key="token",
                jellyfin_library_map={"shows": "TV Shows"},
            )
            mock_discover.return_value = {
                "Movies": "movie-id-123",
                "Shows": "show-id-123",
            }

            from buzz.core.curator import trigger_jellyfin_selective_refresh

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                trigger_jellyfin_selective_refresh(
                    config, ["shows/Some Show"]
                )

            mock_scan.assert_called_once_with(config)
            logged = stdout.getvalue()
            self.assertIn(
                "could not find Jellyfin library 'TV Shows' "
                "for category 'shows'",
                logged,
            )
            self.assertIn(
                "available Jellyfin libraries: Movies, Shows",
                logged,
            )
            self.assertIn('"event": "jellyfin_library_not_found"', logged)
            self.assertIn('"category": "shows"', logged)
            self.assertIn('"library_name": "TV Shows"', logged)
            self.assertIn(
                '"available_libraries": ["Movies", "Shows"]',
                logged,
            )

    @patch("buzz.core.media_server.discover_jellyfin_libraries")
    @patch("urllib.request.urlopen")
    def test_selective_refresh_failure_marks_rebuild_scan_failed(
        self, mock_urlopen, mock_discover
    ):
        mock_discover.return_value = {"Movies": "movie-id-123"}
        mock_urlopen.side_effect = RuntimeError("refresh failed")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
                jellyfin_library_map={"movies": "Movies"},
            )
            self._create_source_tree(config.source_root)

            with patch(
                "buzz.core.curator.validate_jellyfin_auth",
                return_value=True,
            ):
                report = rebuild_and_trigger(
                    config, changed_roots=["movies/Movie.2026.1080p.mkv"]
                )

            self.assertFalse(report["jellyfin_scan_triggered"])
            self.assertEqual(report["jellyfin_scan_status"], "failed")
            self.assertIn("refresh failed", report["jellyfin_scan_error"])

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_rebuild_and_trigger_logs_error_and_returns_report_for_scan_failure(
        self, mock_validate
    ):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                trigger_lib_scan=True,
                jellyfin_api_key="token",
                jellyfin_scan_task_id="scan-task",
            )
            self._create_source_tree(config.source_root)

            with patch(
                "buzz.core.curator.trigger_jellyfin_scan",
                side_effect=RuntimeError("scan failed"),
            ):
                report = rebuild_and_trigger(config)

            self.assertEqual(report["jellyfin_scan_status"], "failed")
            self.assertEqual(report["jellyfin_scan_error"], "scan failed")
            self.assertFalse(report["jellyfin_scan_triggered"])

    def test_rebuild_and_trigger_logs_unexpected_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            app = CuratorApp(self._config(root))
            event_registry.stdout_enabled = True
            client = TestClient(app.app)

            stdout = io.StringIO()
            with patch.object(
                app.curator, "handle_rebuild", side_effect=RuntimeError("boom")
            ):
                with patch("sys.stdout", stdout):
                    response = client.post("/rebuild")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "rebuilding"})
            logged = stdout.getvalue()
            self.assertIn("curator rebuild failed: boom", logged)

    def test_build_library_handles_year_at_start_of_title(self):
        # Case: 2001 - A Space Odyssey (1968)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            movie_dir = (
                config.source_root
                / "movies"
                / "2001 - A Space Odyssey (1968) V2 (2160p BluRay x265 HEVC 10bit HDR AAC 5.1 Tigole)"
            )
            movie_dir.mkdir(parents=True)
            (movie_dir / "2001.mkv").write_text("video", encoding="utf-8")

            report = build_library(config)

            self.assertEqual(report["movies"], 1)
            self.assertEqual(len(report["skipped_movies"]), 0)

            # The folder name should be "2001 A Space Odyssey (1968)"
            curated_file = (
                config.target_root
                / "movies"
                / "2001 A Space Odyssey (1968)"
                / "2001 A Space Odyssey (1968).mkv"
            )
            self.assertTrue(curated_file.exists())

    def test_build_library_handles_year_only_in_folder_name(self):
        # Case: The Imaginarium of Doctor Parnassus (no year in file, year in folder)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            movie_dir = (
                config.source_root
                / "movies"
                / "The Imaginarium of Doctor Parnassus 2009 BRrip 1080P x264 MP4 - Ofek"
            )
            movie_dir.mkdir(parents=True)
            (movie_dir / "The Imaginarium of Doctor Parnassus.mp4").write_text(
                "video", encoding="utf-8"
            )

            report = build_library(config)

            self.assertEqual(report["movies"], 1)
            self.assertEqual(len(report["skipped_movies"]), 0)

            curated_file = (
                config.target_root
                / "movies"
                / "The Imaginarium Of Doctor Parnassus (2009)"
                / "The Imaginarium Of Doctor Parnassus (2009).mp4"
            )
            self.assertTrue(curated_file.exists())


    def test_rebuild_preserves_inode_for_unchanged_symlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._create_source_tree(root / "raw")
            config = self._config(root)

            build_library(config)

            curated_file = next((config.target_root / "movies").rglob("*.mkv"))
            inode_before = curated_file.lstat().st_ino

            build_library(config)

            self.assertTrue(curated_file.exists())
            inode_after = curated_file.lstat().st_ino
            self.assertEqual(inode_before, inode_after)

    def test_rebuild_removes_stale_symlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "raw"
            self._create_source_tree(source_root)
            config = self._config(root)

            build_library(config)

            curated_file = next((config.target_root / "movies").rglob("*.mkv"))

            (source_root / "movies" / "Movie.2026.1080p.mkv").unlink()

            build_library(config)

            self.assertFalse(curated_file.exists())

    def test_rebuild_does_not_restore_stale_identity_folder_from_subtitles(self):
        from buzz.models import SubtitleConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "raw"
            source_file = (
                source_root
                / "movies"
                / "Sample Feature 2004"
                / "Sample Feature 2004.mkv"
            )
            source_file.parent.mkdir(parents=True)
            source_file.write_text("video", encoding="utf-8")
            config = self._config(
                root,
                subtitles=SubtitleConfig(enabled=True, api_key="test"),
                subtitle_root=root / "subs",
            )

            build_library(config)
            old_subtitle = (
                config.subtitle_root
                / "movies"
                / "Sample Feature (2004)"
                / "Sample Feature (2004).en.srt"
            )
            old_subtitle.parent.mkdir(parents=True)
            old_subtitle.write_text("subtitle", encoding="utf-8")

            conn = db.connect(config.state_dir / "buzz.sqlite")
            try:
                db.apply_migrations(conn)
                db.replace_provider_library(
                    conn,
                    [
                        {
                            "hash": "hash-sample",
                            "name": "Sample Feature 2004",
                            "bytes": 100,
                            "files": [
                                {
                                    "id": "1",
                                    "path": "Sample Feature 2004.mkv",
                                    "bytes": 100,
                                }
                            ],
                            "magnet": "",
                            "provider": "torbox",
                            "provider_torrent_id": "37310789",
                            "status": "downloaded",
                            "progress": 100,
                            "info_json": "{}",
                            "signature_json": "{}",
                        }
                    ],
                )
                db.save_curator_title_override(
                    conn,
                    "hash-sample",
                    {
                        "kind": "movie",
                        "title": "Sample Feature Revised",
                        "year": 2004,
                    },
                )
            finally:
                conn.close()

            build_library(config)

            self.assertFalse(
                (config.target_root / "movies" / "Sample Feature (2004)").exists()
            )
            self.assertTrue(
                (
                    config.target_root
                    / "movies"
                    / "Sample Feature Revised (2004)"
                ).exists()
            )

    def test_rebuild_updates_symlink_when_target_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "raw"
            self._create_source_tree(source_root)
            config = self._config(root)

            build_library(config)

            curated_file = next((config.target_root / "movies").rglob("*.mkv"))
            old_target = os.readlink(curated_file)

            # Replace with a different source file (different name → new symlink target)
            (source_root / "movies" / "Movie.2026.1080p.mkv").unlink()
            (source_root / "movies" / "Movie.2027.1080p.mkv").write_text(
                "video", encoding="utf-8"
            )

            build_library(config)

            new_file = next((config.target_root / "movies").rglob("*.mkv"))
            self.assertNotEqual(os.readlink(new_file), old_target)



if __name__ == "__main__":
    unittest.main()
