import unittest
from unittest.mock import MagicMock, patch

from buzz.core.state import BuzzState
from buzz.models import DavConfig


class VFSSyncTests(unittest.TestCase):
    def setUp(self):
        self.config = DavConfig(
            token="token",
            library_mount="/mnt/buzz/raw",
            vfs_wait_timeout_secs=10,
            rd_update_delay_secs=0,
            state_dir="/tmp/buzz-tests-vfs"
        )
        self.client = MagicMock()
        self.state = BuzzState(self.config, self.client)
        # Setup a basic snapshot
        self.state.snapshot = {
            "files": {
                "movies/MyMovie/Movie.mkv": {"type": "remote"},
                "shows/MyShow/S01E01.mkv": {"type": "remote"}
            }
        }

    def _trigger_combined(self, roots):
        self.state._run_hook(roots)
        self.state._wait_for_vfs_visibility(roots)
        self.state._trigger_curator(roots)

    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("time.sleep")
    @patch("time.time")
    def test_wait_for_vfs_visibility_success(self, mock_time, mock_sleep, mock_exists, mock_isdir):
        # We need to mock the methods on the instance after it's created
        # and track their call order.
        call_order = []
        def track_run_hook(*args, **kwargs):
            call_order.append("_run_hook")
        def track_trigger_curator(*args, **kwargs):
            call_order.append("_trigger_curator")

        self.state._run_hook = MagicMock(side_effect=track_run_hook)
        self.state._trigger_curator = MagicMock(side_effect=track_trigger_curator)
        
        mock_time.side_effect = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        mock_isdir.return_value = True
        mock_exists.return_value = True

        with patch("buzz.core.state.record_event") as mock_record:
            self._trigger_combined(["movies/MyMovie"])

        mock_isdir.assert_called_with("/mnt/buzz/raw")
        mock_exists.assert_called()
        self.state._trigger_curator.assert_called_once()
        self.state._run_hook.assert_called_once()
        
        # Verify order: hook then curator
        self.assertEqual(call_order, ["_run_hook", "_trigger_curator"])
        
        events = [call.kwargs.get("event") for call in mock_record.call_args_list]
        self.assertIn("hook_waiting_vfs", events)
        self.assertIn("hook_vfs_visible", events)

    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("time.sleep")
    @patch("time.time")
    @patch("buzz.core.state.BuzzState._trigger_curator")
    @patch("buzz.core.state.BuzzState._run_hook")
    def test_wait_for_vfs_visibility_delay(self, mock_run_hook, mock_trigger_curator, mock_time, mock_sleep, mock_exists, mock_isdir):
        # Mock time to advance each call
        mock_time.side_effect = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0]
        mock_isdir.return_value = True
        # Mock exists to return False first, then True
        mock_exists.side_effect = [False, True]

        with patch("buzz.core.state.record_event") as mock_record:
            self._trigger_combined(["movies/MyMovie"])

        self.assertTrue(mock_exists.called)
        mock_sleep.assert_called_with(2)
        mock_trigger_curator.assert_called_once()
        self.assertIn(
            "hook_vfs_visible",
            [call.kwargs.get("event") for call in mock_record.call_args_list],
        )

    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("time.sleep")
    @patch("time.time")
    @patch("buzz.core.state.BuzzState._trigger_curator")
    @patch("buzz.core.state.BuzzState._run_hook")
    def test_wait_for_vfs_visibility_timeout(self, mock_run_hook, mock_trigger_curator, mock_time, mock_sleep, mock_exists, mock_isdir):
        mock_time.side_effect = [
            100.0, # start_time
            101.0, # first loop check
            102.0, # first exists check
            103.0, # first sleep
            111.0, # second loop check -> exit (timeout is 10s)
            112.0  # final log
        ]
        mock_isdir.return_value = True
        mock_exists.return_value = False

        with patch("buzz.core.state.record_event") as mock_record:
            self._trigger_combined(["movies/MyMovie"])

        mock_exists.assert_called()
        mock_trigger_curator.assert_called_once()
        mock_run_hook.assert_called_once()
        timeout_events = [
            call for call in mock_record.call_args_list
            if call.kwargs.get("event") == "hook_vfs_timeout"
        ]
        self.assertEqual(len(timeout_events), 1)

    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("time.sleep")
    @patch("time.time")
    @patch("buzz.core.state.BuzzState._trigger_curator")
    @patch("buzz.core.state.BuzzState._run_hook")
    def test_wait_for_vfs_visibility_removed_root(self, mock_run_hook, mock_trigger_curator, mock_time, mock_sleep, mock_exists, mock_isdir):
        self.state.snapshot = {"files": {}}
        mock_time.side_effect = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
        mock_isdir.return_value = True
        mock_exists.side_effect = [True, False]

        with patch("buzz.core.state.record_event") as mock_record:
            self._trigger_combined(["movies/MyMovie"])

        mock_exists.assert_called()
        mock_trigger_curator.assert_called_once()
        self.assertIn(
            "hook_vfs_visible",
            [call.kwargs.get("event") for call in mock_record.call_args_list],
        )

if __name__ == "__main__":
    unittest.main()
