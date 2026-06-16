
import httpx
import unittest
from unittest.mock import MagicMock, patch

from buzz.core.events import registry as event_registry
from buzz.core.providers import ProviderRequestLimiter, ProviderRequestPolicy
from buzz.providers import TorBoxProviderClient


class RecordingLimiter(ProviderRequestLimiter):
    def __init__(self):
        super().__init__(ProviderRequestPolicy())
        self.operations = []

    def run(self, operation, call):
        self.operations.append(operation)
        return call()


class TestProviderRequestLimiter(unittest.TestCase):
    @patch("buzz.core.providers.time.sleep")
    def test_enforces_minimum_interval_between_calls(self, mock_sleep):
        limiter = ProviderRequestLimiter(
            ProviderRequestPolicy(min_interval_secs=0.1)
        )

        with patch(
            "buzz.core.providers.time.monotonic",
            side_effect=[10.0, 10.02, 10.1],
        ):
            self.assertEqual(limiter.run("list_torrents", lambda: "first"), "first")
            self.assertEqual(limiter.run("list_torrents", lambda: "second"), "second")

        mock_sleep.assert_called_once_with(0.08000000000000007)

    @patch("buzz.core.providers.time.sleep")
    def test_retries_retryable_status_and_honors_retry_after(self, mock_sleep):
        request = httpx.Request("GET", "https://api.example.invalid/resource")
        rate_limited = httpx.Response(
            429,
            headers={"Retry-After": "1.25"},
            request=request,
        )
        error = httpx.HTTPStatusError(
            "too many requests",
            request=request,
            response=rate_limited,
        )
        calls = [error, "ok"]

        def call():
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        limiter = ProviderRequestLimiter(
            ProviderRequestPolicy(max_attempts=2)
        )

        self.assertEqual(limiter.run("resolve_stream", call), "ok")
        mock_sleep.assert_called_once_with(1.25)

    @patch("buzz.core.providers.time.sleep")
    def test_stops_after_max_attempts(self, mock_sleep):
        request = httpx.Request("GET", "https://api.example.invalid/resource")
        response = httpx.Response(503, request=request)
        error = httpx.HTTPStatusError(
            "unavailable",
            request=request,
            response=response,
        )
        limiter = ProviderRequestLimiter(
            ProviderRequestPolicy(max_attempts=2, backoff_initial_secs=0.0)
        )
        calls = 0

        def fail():
            nonlocal calls
            calls += 1
            raise error

        with self.assertRaises(httpx.HTTPStatusError):
            limiter.run("list_torrents", fail)

        self.assertEqual(calls, 2)
        mock_sleep.assert_not_called()

class TestTorBoxRetry(unittest.TestCase):
    def setUp(self):
        event_registry.clear()

    @patch("buzz.providers.torbox.time.sleep")
    @patch("buzz.providers.torbox.httpx.Client")
    def test_delete_torrent_retries_on_database_error(self, mock_client_class, mock_sleep):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        
        # Mock response sequence: 2 failures then 1 success
        failure_response = MagicMock()
        failure_response.status_code = 200
        failure_response.json.return_value = {
            "success": False,
            "error": "error message",
            "detail": "DATABASE_ERROR"
        }
        
        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {
            "success": True,
            "data": {"ok": True}
        }
        
        mock_client.request.side_effect = [failure_response, failure_response, success_response]
        
        client = TorBoxProviderClient(
            "fake_token",
            request_policy=ProviderRequestPolicy(
                min_interval_secs=0.0,
                max_attempts=1,
            ),
        )
        client.delete_torrent("123")
        
        self.assertEqual(mock_client.request.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        
        events = event_registry.get_recent()
        retry_events = [e for e in events if "retrying TorBox delete" in e["message"]]
        self.assertEqual(len(retry_events), 2)

    @patch("buzz.providers.torbox.time.sleep")
    @patch("buzz.providers.torbox.httpx.Client")
    def test_delete_torrent_raises_after_max_retries(self, mock_client_class, mock_sleep):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        
        failure_response = MagicMock()
        failure_response.status_code = 200
        failure_response.json.return_value = {
            "success": False,
            "error": "error message",
            "detail": "DATABASE_ERROR"
        }
        
        mock_client.request.return_value = failure_response
        
        client = TorBoxProviderClient(
            "fake_token",
            request_policy=ProviderRequestPolicy(
                min_interval_secs=0.0,
                max_attempts=1,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "DATABASE_ERROR"):
            client.delete_torrent("123")
        
        self.assertEqual(mock_client.request.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("buzz.providers.torbox.time.sleep")
    @patch("buzz.providers.torbox.httpx.Client")
    def test_delete_torrent_retries_on_http_500_database_error(self, mock_client_class, mock_sleep):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        
        # Mock response: 500 error with DATABASE_ERROR
        response = MagicMock()
        response.status_code = 500
        response.text = '{"success":false,"detail":"DATABASE_ERROR"}'
        response.json.return_value = {
            "success": False,
            "error": "error message",
            "detail": "DATABASE_ERROR"
        }
        
        # Raise HTTPStatusError
        error = httpx.HTTPStatusError("500 error", request=MagicMock(), response=response)
        
        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {"success": True}
        
        mock_client.request.side_effect = [error, success_response]
        
        client = TorBoxProviderClient(
            "fake_token",
            request_policy=ProviderRequestPolicy(
                min_interval_secs=0.0,
                max_attempts=1,
            ),
        )
        client.delete_torrent("123")
        
        self.assertEqual(mock_client.request.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("buzz.providers.torbox.time.sleep")
    @patch("buzz.providers.torbox.httpx.Client")
    def test_delete_torrent_no_retry_on_other_error(self, mock_client_class, mock_sleep):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        
        failure_response = MagicMock()
        failure_response.status_code = 200
        failure_response.json.return_value = {
            "success": False,
            "error": "some other error",
            "detail": "SOME_OTHER_ERROR"
        }
        
        mock_client.request.return_value = failure_response
        
        client = TorBoxProviderClient(
            "fake_token",
            request_policy=ProviderRequestPolicy(
                min_interval_secs=0.0,
                max_attempts=1,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "SOME_OTHER_ERROR"):
            client.delete_torrent("123")
        
        self.assertEqual(mock_client.request.call_count, 1)
        self.assertEqual(mock_sleep.call_count, 0)

    @patch("buzz.providers.torbox.httpx.Client")
    def test_public_api_calls_use_limiter_operations(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        limiter = RecordingLimiter()
        client = TorBoxProviderClient("fake_token", limiter=limiter)

        list_response = MagicMock()
        list_response.json.return_value = {"success": True, "data": []}
        add_response = MagicMock()
        add_response.json.return_value = {
            "success": True,
            "data": {"torrent_id": "123"},
        }
        delete_response = MagicMock()
        delete_response.json.return_value = {"success": True, "data": {"ok": True}}
        stream_response = MagicMock()
        stream_response.json.return_value = {
            "success": True,
            "data": "https://cdn.example.invalid/file",
        }
        mock_client.request.side_effect = [
            list_response,
            add_response,
            delete_response,
            stream_response,
        ]

        self.assertEqual(client.list_torrents(), [])
        self.assertEqual(client.add_magnet("magnet:?xt=urn:btih:abc"), "123")
        client.delete_torrent("123")
        self.assertEqual(
            client.resolve_stream("123:1"), "https://cdn.example.invalid/file"
        )

        self.assertEqual(
            limiter.operations,
            ["list_torrents", "add_magnet", "delete_torrent", "resolve_stream"],
        )

    @patch("buzz.core.providers.time.sleep")
    @patch("buzz.providers.torbox.httpx.Client")
    def test_resolve_stream_retries_torbox_429(
        self, mock_client_class, mock_core_sleep
    ):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        request = httpx.Request(
            "GET", "https://api.torbox.app/v1/api/torrents/requestdl"
        )
        response_429 = httpx.Response(429, request=request)
        error_429 = httpx.HTTPStatusError(
            "too many requests",
            request=request,
            response=response_429,
        )
        success_response = MagicMock()
        success_response.json.return_value = {
            "success": True,
            "data": "https://cdn.example.invalid/file",
        }
        mock_client.request.side_effect = [error_429, success_response]

        client = TorBoxProviderClient(
            "fake_token",
            request_policy=ProviderRequestPolicy(
                min_interval_secs=0.0,
                max_attempts=2,
                backoff_initial_secs=0.0,
            ),
            operation_policies={
                "resolve_stream": ProviderRequestPolicy(
                    min_interval_secs=0.0,
                    max_attempts=2,
                    backoff_initial_secs=0.0,
                )
            },
        )

        self.assertEqual(
            client.resolve_stream("123:1"), "https://cdn.example.invalid/file"
        )
        self.assertEqual(mock_client.request.call_count, 2)
        mock_core_sleep.assert_not_called()

if __name__ == "__main__":
    unittest.main()
