import unittest
from unittest.mock import patch, MagicMock
import os
import time
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

    @patch("os.path.exists")
    @patch("time.sleep")
    @patch("time.time")
    @patch("buzz.core.state.BuzzState._trigger_curator")
    @patch("buzz.core.state.BuzzState._run_hook")
    def test_wait_for_vfs_visibility_success(self, mock_run_hook, mock_trigger_curator, mock_time, mock_sleep, mock_exists):
        # Mock time to not advance much
        mock_time.side_effect = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        # Mock exists to return True for the requested root
        mock_exists.return_value = True
        
        self.state._trigger_curator_and_hooks(["movies/MyMovie"])
        
        # Verify os.path.exists was called with the correct path
        mock_exists.assert_called_with("/mnt/buzz/raw/movies/MyMovie")
        # Verify curator and hooks were triggered
        mock_trigger_curator.assert_called_once()
        mock_run_hook.assert_called_once()
        # Verify no sleep was needed (it was visible immediately)
        mock_sleep.assert_not_called()

    @patch("os.path.exists")
    @patch("time.sleep")
    @patch("time.time")
    @patch("buzz.core.state.BuzzState._trigger_curator")
    @patch("buzz.core.state.BuzzState._run_hook")
    def test_wait_for_vfs_visibility_delay(self, mock_run_hook, mock_trigger_curator, mock_time, mock_sleep, mock_exists):
        # Mock time to advance each call
        mock_time.side_effect = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
        # Mock exists to return False first, then True
        mock_exists.side_effect = [False, True]
        
        self.state._trigger_curator_and_hooks(["movies/MyMovie"])
        
        # Verify it slept once
        mock_sleep.assert_called_once_with(2)
        # Verify curator and hooks were triggered
        mock_trigger_curator.assert_called_once()

    @patch("os.path.exists")
    @patch("time.sleep")
    @patch("time.time")
    @patch("buzz.core.state.BuzzState._trigger_curator")
    @patch("buzz.core.state.BuzzState._run_hook")
    def test_wait_for_vfs_visibility_timeout(self, mock_run_hook, mock_trigger_curator, mock_time, mock_sleep, mock_exists):
        # Mock time to hit timeout (10s)
        # start_time = 100.0
        # loop 1: 101.0 (elapsed 1.0 < 10.0) -> False -> sleep
        # loop 2: 104.0 (elapsed 4.0 < 10.0) -> False -> sleep
        # loop 3: 107.0 (elapsed 7.0 < 10.0) -> False -> sleep
        # loop 4: 110.0 (elapsed 10.0 < 10.0 is False, so loop terminates)
        mock_time.side_effect = [
            100.0, # start_time
            101.0, # first loop check
            102.0, # first exists check path join
            103.0, # first sleep check time
            104.0, # second loop check
            105.0, # second exists check
            106.0, # second sleep check
            107.0, # third loop check
            108.0, # third exists check
            109.0, # third sleep check
            110.0, # fourth loop check -> exit
            111.0  # final log
        ]
        # Mock exists to always return False
        mock_exists.return_value = False
        
        self.state._trigger_curator_and_hooks(["movies/MyMovie"])
        
        # Verify it timed out but still proceeded
        mock_trigger_curator.assert_called_once()
        mock_run_hook.assert_called_once()

    @patch("os.path.exists")
    @patch("time.sleep")
    @patch("time.time")
    @patch("buzz.core.state.BuzzState._trigger_curator")
    @patch("buzz.core.state.BuzzState._run_hook")
    def test_wait_for_vfs_visibility_removed_root(self, mock_run_hook, mock_trigger_curator, mock_time, mock_sleep, mock_exists):
        # If a root is NOT in snapshot, we wait for it to be GONE (exists=False)
        self.state.snapshot = {"files": {}} # Empty snapshot, so MyMovie is "removed"
        
        # Mock time
        mock_time.side_effect = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        # Mock exists to return True (stale) then False (gone)
        mock_exists.side_effect = [True, False]
        
        self.state._trigger_curator_and_hooks(["movies/MyMovie"])
        
        # Should have called exists twice
        self.assertEqual(mock_exists.call_count, 2)
        # Should have slept once
        mock_sleep.assert_called_once_with(2)
        mock_trigger_curator.assert_called_once()

if __name__ == "__main__":
    unittest.main()
