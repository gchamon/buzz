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
            self.assertEqual(stdout.getvalue(), "")

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
            mapping_log = json.loads(lines[-1])
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
            mapping_log = json.loads(lines[-1])
            self.assertEqual(mapping_log["event"], "curator_mapping_diff")
            self.assertEqual(mapping_log["added"], [])
            self.assertEqual(mapping_log["removed"], [])
            self.assertEqual(mapping_log["changed"], [])

    def test_rebuild_and_trigger_calls_jellyfin_scan_when_auth_is_configured(self):
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
            self.assertEqual(report["jellyfin_scan_status"], "triggered")
            self.assertIsNone(report["jellyfin_scan_error"])

    def test_rebuild_and_trigger_raises_structured_error_for_scan_failure(self):
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
                with self.assertRaises(RebuildError) as ctx:
                    rebuild_and_trigger(config)

            self.assertEqual(str(ctx.exception), "scan failed")
            self.assertEqual(ctx.exception.payload["jellyfin_scan_status"], "failed")
            self.assertEqual(ctx.exception.payload["jellyfin_scan_error"], "scan failed")
            self.assertFalse(ctx.exception.payload["jellyfin_scan_triggered"])

    def test_curator_rebuild_logs_unexpected_errors(self):
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
            self.assertIn("Traceback", logged)


if __name__ == "__main__":
    unittest.main()
