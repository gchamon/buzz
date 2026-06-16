
import time
import unittest
import tempfile
from typing import cast, Any
from types import SimpleNamespace
from unittest.mock import MagicMock
from buzz.core.state import BuzzState
from buzz.core.providers import ProviderDeleteError
from buzz.models import DavConfig as Config
from buzz.core.events import registry as event_registry
from buzz.ui_live import CacheLiveView


def _make_config(tmpdir: str) -> Config:
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
        curator_url="",
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


class TestDeleteVisibility(unittest.TestCase):
    def setUp(self):
        event_registry.clear(listeners=True)

    def test_delete_torrent_records_error_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            class MockClient:
                kind = "real_debrid"
                def delete_torrent(self, torrent_id):
                    raise ProviderDeleteError(500, "DATABASE_ERROR")
                def get_torrents(self):
                    return []
                def is_healthy(self):
                    return True

            client = MockClient()
            state = BuzzState(_make_config(tmpdir), client=client)
            state.cache["TORRENT1"] = {
                "id": "TORRENT1",
                "info": {"hash": "HASH1", "id": "TORRENT1"},
                "magnet": "magnet:?xt=urn:btih:HASH1",
            }

            task_id = state.delete_torrent("TORRENT1")
            task = _wait_for_task(state, task_id)
            self.assertEqual(task["status"], "failed")

            events = event_registry.get_recent()
            error_events = [e for e in events if e.get("event") == "provider_delete_failed"]
            self.assertEqual(len(error_events), 1)
            self.assertEqual(error_events[0]["level"], "error")
            self.assertIn("gave up removing", error_events[0]["message"])
            self.assertIn("after 1 attempt(s)", error_events[0]["message"])
            self.assertEqual(error_events[0]["torrent_id"], "TORRENT1")

    def test_delete_torrent_keeps_cache_on_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            class MockClient:
                kind = "real_debrid"
                def delete_torrent(self, torrent_id):
                    raise ProviderDeleteError(500, "DATABASE_ERROR")
                def get_torrents(self):
                    return []
                def is_healthy(self):
                    return True

            state = BuzzState(_make_config(tmpdir), client=MockClient())
            state.cache["TORRENT1"] = {
                "id": "TORRENT1",
                "info": {"hash": "HASH1", "id": "TORRENT1"},
            }

            task_id = state.delete_torrent("TORRENT1")
            _wait_for_task(state, task_id)

            self.assertIn("TORRENT1", state.cache)

    def test_delete_torrent_removes_cache_on_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            class MockClient:
                kind = "real_debrid"
                def delete_torrent(self, torrent_id):
                    pass
                def get_torrents(self):
                    return []
                def is_healthy(self):
                    return True

            state = BuzzState(_make_config(tmpdir), client=MockClient())
            state.cache["TORRENT1"] = {
                "id": "TORRENT1",
                "info": {"hash": "HASH1", "id": "TORRENT1"},
            }

            task_id = state.delete_torrent("TORRENT1")
            task = _wait_for_task(state, task_id)

            self.assertEqual(task["status"], "complete")
            self.assertNotIn("TORRENT1", state.cache)

    def test_cache_live_view_handle_delete_queued(self):
        owner = MagicMock()
        owner.state.delete_torrent.return_value = "task_abc123"
        owner.config.provider_active = "real_debrid"
        owner.config.subtitles.enabled = False
        owner.state.torrents.return_value = []

        view = CacheLiveView(owner)
        context = view._context(
            analysis_results=[{"torrent_id": "T1", "files": [], "filename": "test", "provider": "rd", "provider_label": "RD"}],
            confirm_delete_id="HASH1",
            sort_col=1,
            sort_dir="desc",
        )
        socket = SimpleNamespace(context=context)

        view._handle_delete(cast(Any, socket), "HASH1")

        self.assertEqual(socket.context["console_msg"], "removing from cache...")
        self.assertEqual(socket.context["console_class"], "service-status-orange")
        self.assertIsNone(socket.context["confirm_delete_id"])
        self.assertEqual(socket.context["analysis_results"], [{"torrent_id": "T1", "files": [], "filename": "test", "provider": "rd", "provider_label": "RD"}])
        self.assertEqual(socket.context["sort_col"], 1)
        self.assertEqual(socket.context["sort_dir"], "desc")

    def test_cache_live_view_handle_delete_failure(self):
        owner = MagicMock()
        owner.state.delete_torrent.side_effect = ValueError("server error")
        owner.config.provider_active = "real_debrid"
        owner.config.subtitles.enabled = False
        owner.state.torrents.return_value = []

        view = CacheLiveView(owner)
        context = view._context(
            analysis_results=[{"torrent_id": "T1", "files": [], "filename": "test", "provider": "rd", "provider_label": "RD"}],
            confirm_delete_id="HASH1",
            sort_col=1,
            sort_dir="desc",
        )
        socket = SimpleNamespace(context=context)

        view._handle_delete(cast(Any, socket), "HASH1")

        self.assertEqual(socket.context["console_msg"], "delete failed: server error")
        self.assertEqual(socket.context["console_class"], "service-status-red")
        self.assertIsNone(socket.context["confirm_delete_id"])

    def test_api_cache_delete_queued(self):
        from fastapi.testclient import TestClient
        from buzz.dav_app import DavApp

        with tempfile.TemporaryDirectory() as tmpdir:
            class MockClient:
                kind = "real_debrid"
                def delete_torrent(self, torrent_id):
                    raise ProviderDeleteError(500, "DATABASE_ERROR")
                def get_torrents(self):
                    return []
                def is_healthy(self):
                    return True

            client = MockClient()
            app = DavApp(_make_config(tmpdir))
            app.client = cast(Any, client)
            app.clients = {"real_debrid": cast(Any, client)}
            app.state.client = cast(Any, client)
            app.state.clients = {"real_debrid": cast(Any, client)}

            test_client = TestClient(app.app)
            app.state.cache["TORRENT1"] = {
                "id": "TORRENT1",
                "info": {"hash": "HASH1", "id": "TORRENT1"},
                "magnet": "magnet:?xt=urn:btih:HASH1",
            }

            response = test_client.post("/api/cache/delete", json={"torrent_id": "TORRENT1"})

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["status"], "queued")
            self.assertIn("task_id", body)

            task_id = body["task_id"]
            _wait_for_task(app.state, task_id)

            events = event_registry.get_recent()
            error_events = [e for e in events if e.get("event") == "provider_delete_failed"]
            self.assertEqual(len(error_events), 1)
