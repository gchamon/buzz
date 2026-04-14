import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from buzz.core.curator import RebuildError, build_library
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
                response = client.post("/rebuild")

            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json()["error"], "scan failed")
            self.assertEqual(response.json()["jellyfin_scan_status"], "failed")


if __name__ == "__main__":
    unittest.main()
