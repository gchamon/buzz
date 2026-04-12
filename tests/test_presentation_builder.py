import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import presentation_builder as pb


class PresentationBuilderTests(unittest.TestCase):
    def make_config(self, root: Path, **overrides):
        base = {
            "bind": "127.0.0.1",
            "port": 8400,
            "source_root": root / "source",
            "target_root": root / "target" / "jellyfin-library",
            "state_root": root / "state",
            "overrides_path": root / "overrides.yml",
            "jellyfin_url": "http://jellyfin:8096",
            "jellyfin_api_key": "",
            "jellyfin_scan_task_id": "",
            "skip_jellyfin_scan": False,
            "build_on_start": False,
            "verbose": False,
        }
        base.update(overrides)
        return pb.Config(**base)

    def create_source_tree(self, source_root: Path):
        movies = source_root / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        (movies / "Movie.2026.1080p.mkv").write_text("video", encoding="utf-8")

    def test_rebuild_skips_scan_when_api_key_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self.make_config(root)
            self.create_source_tree(config.source_root)

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                report = pb.rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            self.assertFalse(report["jellyfin_scan_triggered"])
            self.assertEqual(report["jellyfin_scan_status"], "skipped_missing_auth")
            self.assertIsNone(report["jellyfin_scan_error"])
            self.assertEqual(stdout.getvalue(), "")

    def test_rebuild_logs_mapping_when_verbose_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self.make_config(root, verbose=True)
            self.create_source_tree(config.source_root)

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                report = pb.rebuild_and_trigger(config)

            self.assertEqual(report["movies"], 1)
            lines = [line for line in stdout.getvalue().splitlines() if line]
            mapping_log = json.loads(lines[-1])
            self.assertEqual(mapping_log["event"], "presentation_builder_mapping")
            self.assertEqual(mapping_log["mapping_entries"], 1)
            self.assertEqual(
                mapping_log["entries"],
                [{"source": "movies/Movie.2026.1080p.mkv", "target": "movies/Movie (2026)/Movie (2026).mkv", "type": "movie"}],
            )

    def test_rebuild_triggers_scan_when_auth_is_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self.make_config(root, jellyfin_api_key="token", jellyfin_scan_task_id="scan-task")
            self.create_source_tree(config.source_root)

            with mock.patch.object(pb, "trigger_jellyfin_scan") as trigger_scan:
                report = pb.rebuild_and_trigger(config)

            trigger_scan.assert_called_once_with(config)
            self.assertTrue(report["jellyfin_scan_triggered"])
            self.assertEqual(report["jellyfin_scan_status"], "triggered")
            self.assertIsNone(report["jellyfin_scan_error"])

    def test_rebuild_raises_structured_error_for_real_scan_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self.make_config(root, jellyfin_api_key="token", jellyfin_scan_task_id="scan-task")
            self.create_source_tree(config.source_root)

            with mock.patch.object(pb, "trigger_jellyfin_scan", side_effect=RuntimeError("scan failed")):
                with self.assertRaises(pb.RebuildError) as ctx:
                    pb.rebuild_and_trigger(config)

            self.assertEqual(str(ctx.exception), "scan failed")
            self.assertEqual(ctx.exception.payload["jellyfin_scan_status"], "failed")
            self.assertEqual(ctx.exception.payload["jellyfin_scan_error"], "scan failed")
            self.assertFalse(ctx.exception.payload["jellyfin_scan_triggered"])

    def test_rebuild_handler_returns_structured_payload_for_rebuild_errors(self):
        payload = {
            "movies": 1,
            "jellyfin_scan_triggered": False,
            "jellyfin_scan_status": "failed",
            "jellyfin_scan_error": "scan failed",
        }

        class FakeApp:
            def handle_rebuild(self):
                raise pb.RebuildError("scan failed", payload)

        handler = pb.Handler.__new__(pb.Handler)
        handler.path = "/rebuild"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.app = FakeApp()
        recorded = {"status": None, "headers": []}

        def send_response(status):
            recorded["status"] = status

        def send_header(name, value):
            recorded["headers"].append((name, value))

        def end_headers():
            return None

        handler.send_response = send_response
        handler.send_header = send_header
        handler.end_headers = end_headers

        handler.do_POST()

        self.assertEqual(recorded["status"], 500)
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(body["error"], "scan failed")
        self.assertEqual(body["jellyfin_scan_status"], "failed")
        self.assertEqual(body["jellyfin_scan_error"], "scan failed")

    def test_rebuild_handler_logs_rebuild_errors(self):
        class FakeApp:
            def handle_rebuild(self):
                raise RuntimeError("boom")

        handler = pb.Handler.__new__(pb.Handler)
        handler.path = "/rebuild"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.app = FakeApp()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            handler.do_POST()

        logged = stderr.getvalue()
        self.assertIn("presentation-builder rebuild failed: boom", logged)
        self.assertIn("Traceback", logged)


if __name__ == "__main__":
    unittest.main()
