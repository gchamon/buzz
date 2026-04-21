import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from buzz.core.curator import RebuildError, build_library, rebuild_and_trigger
from buzz.curator_app import CuratorApp
from buzz.models import PresentationConfig


class CuratorAppTests(unittest.TestCase):
    def _config(self, root: Path, **overrides) -> PresentationConfig:
        defaults = {
            "source_root": root / "raw",
            "target_root": root / "curated",
            "state_root": root / "state",
            "overrides_path": root / "overrides.yml",
            "skip_jellyfin_scan": True,
            "build_on_start": False,
        }
        defaults.update(overrides)
        return PresentationConfig(
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

    def test_build_library_accepts_canonical_presentation_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            report = build_library(self._config(root))

            self.assertEqual(report["movies"], 0)
            self.assertEqual(report["show_files"], 0)
            self.assertEqual(report["anime_files"], 0)

    def test_curator_app_routes_use_fastapi(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            app = CuratorApp(self._config(root))
            client = TestClient(app.app)

            health = client.get("/healthz")
            rebuild = client.post("/rebuild")

            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json(), {"status": "ok"})
            self.assertEqual(rebuild.status_code, 200)
            self.assertEqual(rebuild.json()["movies"], 0)

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
            self.assertTrue((root / "state" / "report.json").exists())

    def test_curator_rebuild_error_payload_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "raw" / "movies").mkdir(parents=True)
            (root / "raw" / "shows").mkdir(parents=True)
            (root / "raw" / "anime").mkdir(parents=True)

            app = CuratorApp(self._config(root))
            client = TestClient(app.app)

            with patch.object(
                app.curator,
                "handle_rebuild",
                side_effect=RebuildError(
                    "scan failed",
                    {"jellyfin_scan_status": "failed", "jellyfin_scan_triggered": False},
                ),
            ):
                with patch("sys.stdout", io.StringIO()):
                    response = client.post("/rebuild")

            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json()["error"], "scan failed")
            self.assertEqual(response.json()["jellyfin_scan_status"], "failed")

    def test_rebuild_and_trigger_skips_scan_when_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root, skip_jellyfin_scan=True)
            self._create_source_tree(config.source_root)

            report = rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            self.assertFalse(report["jellyfin_scan_triggered"])
            self.assertEqual(report["jellyfin_scan_status"], "skipped_configured")
            self.assertIsNone(report["jellyfin_scan_error"])

    def test_rebuild_and_trigger_skips_scan_when_api_key_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root, skip_jellyfin_scan=False, jellyfin_api_key="")
            self._create_source_tree(config.source_root)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                report = rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            self.assertFalse(report["jellyfin_scan_triggered"])
            self.assertEqual(report["jellyfin_scan_status"], "skipped_missing_auth")
            self.assertIsNone(report["jellyfin_scan_error"])
            # The curator now always logs mapping events
            self.assertIn("Curator mapping updated", stdout.getvalue())

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

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_rebuild_and_trigger_calls_jellyfin_scan_when_auth_is_configured(self, mock_validate):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                skip_jellyfin_scan=False,
                jellyfin_api_key="token",
                jellyfin_scan_task_id="scan-task",
            )
            self._create_source_tree(config.source_root)

            with patch("buzz.core.curator.trigger_jellyfin_scan") as trigger_scan:
                report = rebuild_and_trigger(config)

            trigger_scan.assert_called_once_with(config)
            self.assertTrue(report["jellyfin_scan_triggered"])
            self.assertEqual(report["jellyfin_scan_status"], "full_triggered")
            self.assertIsNone(report["jellyfin_scan_error"])

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_rebuild_and_trigger_calls_selective_refresh_when_changed_roots_provided(self, mock_validate):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root, skip_jellyfin_scan=False, jellyfin_api_key="token")
            self._create_source_tree(config.source_root)

            with patch(
                "buzz.core.curator.trigger_jellyfin_selective_refresh"
            ) as trigger_selective:
                report = rebuild_and_trigger(config, changed_roots=["movies/MyMovie"])

            trigger_selective.assert_called_once_with(config, ["movies/MyMovie"])
            self.assertTrue(report["jellyfin_scan_triggered"])
            self.assertEqual(report["jellyfin_scan_status"], "selective_triggered")

    @patch("buzz.core.curator.discover_jellyfin_libraries")
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
                    refresh_url in (url.full_url if hasattr(url, "full_url") else str(url))
                    for url in calls
                )
            )

    @patch("buzz.core.curator.validate_jellyfin_auth")
    def test_rebuild_and_trigger_logs_error_and_returns_report_for_scan_failure(self, mock_validate):
        mock_validate.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(
                root,
                skip_jellyfin_scan=False,
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
            client = TestClient(app.app)

            stdout = io.StringIO()
            with patch.object(app.curator, "handle_rebuild", side_effect=RuntimeError("boom")):
                with patch("sys.stdout", stdout):
                    response = client.post("/rebuild")

            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json()["error"], "boom")
            logged = stdout.getvalue()
            self.assertIn("curator rebuild failed: boom", logged)

    def test_build_library_handles_year_at_start_of_title(self):
        # Case: 2001 - A Space Odyssey (1968)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            movie_dir = config.source_root / "movies" / "2001 - A Space Odyssey (1968) V2 (2160p BluRay x265 HEVC 10bit HDR AAC 5.1 Tigole)"
            movie_dir.mkdir(parents=True)
            (movie_dir / "2001.mkv").write_text("video", encoding="utf-8")

            report = build_library(config)

            self.assertEqual(report["movies"], 1)
            self.assertEqual(len(report["skipped_movies"]), 0)
            
            # The folder name should be "2001 A Space Odyssey (1968)"
            curated_file = config.target_root / "movies" / "2001 A Space Odyssey (1968)" / "2001 A Space Odyssey (1968).mkv"
            self.assertTrue(curated_file.exists())

    def test_build_library_handles_year_only_in_folder_name(self):
        # Case: The Imaginarium of Doctor Parnassus (no year in file, year in folder)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            movie_dir = config.source_root / "movies" / "The Imaginarium of Doctor Parnassus 2009 BRrip 1080P x264 MP4 - Ofek"
            movie_dir.mkdir(parents=True)
            (movie_dir / "The Imaginarium of Doctor Parnassus.mp4").write_text("video", encoding="utf-8")

            report = build_library(config)

            self.assertEqual(report["movies"], 1)
            self.assertEqual(len(report["skipped_movies"]), 0)
            
            curated_file = config.target_root / "movies" / "The Imaginarium Of Doctor Parnassus (2009)" / "The Imaginarium Of Doctor Parnassus (2009).mp4"
            self.assertTrue(curated_file.exists())


if __name__ == "__main__":
    unittest.main()
