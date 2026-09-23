"""Typed provider errors and semantic ingest primitives."""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import requests

from buzz.core import db
from buzz.core.providers import (
    DEFAULT_RESOLUTION_REFRESH_SECS,
    FALLBACK_ELIGIBLE_ERROR_CODES,
    FileSelection,
    ProviderErrorCode,
    ProviderOperationError,
    ProviderRequestLimiter,
    ProviderRequestPolicy,
    httpx_exception_error,
    retry_after_value,
)
from buzz.providers import (
    LocalProviderClient,
    RealDebridProviderClient,
    TorBoxProviderClient,
)

MAGNET = "magnet:?xt=urn:btih:ABC123&dn=Movie"


class TestErrorCodeVocabulary(unittest.TestCase):
    def test_codes_are_unique_strings(self):
        codes = [
            ProviderErrorCode.INVALID_MAGNET,
            ProviderErrorCode.UNSUPPORTED_OPERATION,
            ProviderErrorCode.AUTHENTICATION_FAILED,
            ProviderErrorCode.PERMISSION_DENIED,
            ProviderErrorCode.ACCOUNT_LIMIT_REACHED,
            ProviderErrorCode.MAGNET_REJECTED,
            ProviderErrorCode.RATE_LIMITED,
            ProviderErrorCode.UPSTREAM_TIMEOUT,
            ProviderErrorCode.UPSTREAM_UNAVAILABLE,
            ProviderErrorCode.UPSTREAM_PROTOCOL_ERROR,
            ProviderErrorCode.TORRENT_NOT_FOUND,
            ProviderErrorCode.FILE_SELECTION_REJECTED,
            ProviderErrorCode.UNKNOWN,
        ]
        self.assertEqual(len(set(codes)), len(codes))
        self.assertTrue(all(isinstance(code, str) for code in codes))

    def test_fallback_eligibility_policy(self):
        eligible = {
            ProviderErrorCode.UNSUPPORTED_OPERATION,
            ProviderErrorCode.ACCOUNT_LIMIT_REACHED,
            ProviderErrorCode.MAGNET_REJECTED,
            ProviderErrorCode.RATE_LIMITED,
            ProviderErrorCode.UPSTREAM_TIMEOUT,
            ProviderErrorCode.UPSTREAM_UNAVAILABLE,
        }
        self.assertEqual(eligible, set(FALLBACK_ELIGIBLE_ERROR_CODES))

    def test_fallback_eligible_property(self):
        for code in (
            ProviderErrorCode.RATE_LIMITED,
            ProviderErrorCode.MAGNET_REJECTED,
        ):
            error = ProviderOperationError("p", "submit_magnet", code)
            self.assertTrue(error.fallback_eligible, code)
        for code in (
            ProviderErrorCode.INVALID_MAGNET,
            ProviderErrorCode.AUTHENTICATION_FAILED,
            ProviderErrorCode.PERMISSION_DENIED,
            ProviderErrorCode.UPSTREAM_PROTOCOL_ERROR,
            ProviderErrorCode.TORRENT_NOT_FOUND,
            ProviderErrorCode.FILE_SELECTION_REJECTED,
            ProviderErrorCode.UNKNOWN,
        ):
            error = ProviderOperationError("p", "submit_magnet", code)
            self.assertFalse(error.fallback_eligible, code)

    def test_message_and_attributes(self):
        error = ProviderOperationError(
            "real_debrid",
            "submit_magnet",
            ProviderErrorCode.MAGNET_REJECTED,
            detail="Magnet link not found",
            retryable=True,
            retry_after=12.0,
            diagnostic="raw payload",
        )
        self.assertEqual(
            "real_debrid submit_magnet failed: magnet_rejected "
            "(Magnet link not found)",
            str(error),
        )
        self.assertEqual("real_debrid", error.provider)
        self.assertEqual("submit_magnet", error.operation)
        self.assertEqual(ProviderErrorCode.MAGNET_REJECTED, error.code)
        self.assertEqual("Magnet link not found", error.detail)
        self.assertTrue(error.retryable)
        self.assertEqual(12.0, error.retry_after)
        self.assertEqual("raw payload", error.diagnostic)
        self.assertIsInstance(error, ValueError)


def _status_error(status: int, **headers: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example.invalid/resource")
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError("error", request=request, response=response)


class TestHttpxExceptionMapping(unittest.TestCase):
    def test_timeout_maps_to_upstream_timeout(self):
        error = httpx_exception_error(
            "p", "submit_magnet", httpx.ReadTimeout("timed out")
        )
        self.assertEqual(ProviderErrorCode.UPSTREAM_TIMEOUT, error.code)
        self.assertTrue(error.retryable)

    def test_connect_error_maps_to_unavailable(self):
        error = httpx_exception_error(
            "p", "submit_magnet", httpx.ConnectError("refused")
        )
        self.assertEqual(ProviderErrorCode.UPSTREAM_UNAVAILABLE, error.code)

    def test_http_status_mapping(self):
        cases = {
            401: ProviderErrorCode.AUTHENTICATION_FAILED,
            403: ProviderErrorCode.PERMISSION_DENIED,
            404: ProviderErrorCode.TORRENT_NOT_FOUND,
            408: ProviderErrorCode.UPSTREAM_TIMEOUT,
            429: ProviderErrorCode.RATE_LIMITED,
            500: ProviderErrorCode.UPSTREAM_UNAVAILABLE,
            422: ProviderErrorCode.UPSTREAM_PROTOCOL_ERROR,
        }
        for status, code in cases.items():
            error = httpx_exception_error("p", "submit_magnet", _status_error(status))
            self.assertEqual(code, error.code, f"HTTP {status}")

    def test_429_captures_retry_after(self):
        error = httpx_exception_error(
            "p", "submit_magnet", _status_error(429, **{"Retry-After": "1.5"})
        )
        self.assertEqual(ProviderErrorCode.RATE_LIMITED, error.code)
        self.assertTrue(error.retryable)
        self.assertEqual(1.5, error.retry_after)

    def test_non_httpx_exception_maps_to_unavailable(self):
        error = httpx_exception_error("p", "submit_magnet", ValueError("boom"))
        self.assertEqual(ProviderErrorCode.UPSTREAM_UNAVAILABLE, error.code)
        self.assertEqual("boom", error.detail)

    def test_retry_after_value(self):
        self.assertIsNone(retry_after_value(None))
        self.assertIsNone(retry_after_value(""))
        self.assertIsNone(retry_after_value("not-a-number"))
        self.assertEqual(3.0, retry_after_value("3"))
        self.assertEqual(0.0, retry_after_value("-5"))


class _FakeRDResponse:
    def __init__(self, body, status_code: int = 200):
        self.body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self):
        return self.body


class _FakeRD:
    def __init__(self, post_response=None, post_error: Exception | None = None):
        self.post_response = post_response
        self.post_error = post_error
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, **payload):
        self.calls.append((path, payload))
        if self.post_error is not None:
            raise self.post_error
        assert self.post_response is not None
        return self.post_response


class TestRealDebridIngestPrimitives(unittest.TestCase):
    def test_submit_magnet_sends_full_uri_and_returns_ref(self):
        rd = _FakeRD(post_response=_FakeRDResponse({"id": "T1"}))
        client = RealDebridProviderClient("token", raw_client=rd)
        submission = client.submit_magnet(MAGNET)
        self.assertEqual("T1", submission.torrent_id)
        self.assertFalse(submission.reused)
        self.assertEqual(
            ("/torrents/addMagnet", {"magnet": MAGNET}), rd.calls[0]
        )

    def test_submit_magnet_without_id_raises_typed_error(self):
        rd = _FakeRD(post_response=_FakeRDResponse({"error": "bad", "id": ""}))
        client = RealDebridProviderClient("token", raw_client=rd)
        with self.assertRaises(ProviderOperationError) as ctx:
            client.submit_magnet(MAGNET)
        self.assertEqual(
            ProviderErrorCode.UPSTREAM_PROTOCOL_ERROR, ctx.exception.code
        )
        self.assertFalse(ctx.exception.fallback_eligible)

    def test_submit_magnet_rejection_is_fallback_eligible(self):
        rd = _FakeRD(
            post_response=_FakeRDResponse(
                {"error": "Magnet link not found", "error_code": 18}, 403
            )
        )
        client = RealDebridProviderClient("token", raw_client=rd)
        with self.assertRaises(ProviderOperationError) as ctx:
            client.submit_magnet(MAGNET)
        self.assertEqual(ProviderErrorCode.MAGNET_REJECTED, ctx.exception.code)
        self.assertTrue(ctx.exception.fallback_eligible)

    def test_submit_magnet_infringing_file_is_fallback_eligible(self):
        rd = _FakeRD(
            post_response=_FakeRDResponse(
                {"error": "infringing_file", "error_code": 25}
            )
        )
        client = RealDebridProviderClient("token", raw_client=rd)
        with self.assertRaises(ProviderOperationError) as ctx:
            client.submit_magnet(MAGNET)
        self.assertEqual(ProviderErrorCode.MAGNET_REJECTED, ctx.exception.code)
        self.assertTrue(ctx.exception.fallback_eligible)

    def test_submit_magnet_auth_failure_is_not_fallback_eligible(self):
        rd = _FakeRD(
            post_response=_FakeRDResponse({"error": "invalid token"}, 401)
        )
        client = RealDebridProviderClient("token", raw_client=rd)
        with self.assertRaises(ProviderOperationError) as ctx:
            client.submit_magnet(MAGNET)
        self.assertEqual(
            ProviderErrorCode.AUTHENTICATION_FAILED, ctx.exception.code
        )
        self.assertFalse(ctx.exception.fallback_eligible)

    def test_submit_magnet_timeout_maps_to_upstream_timeout(self):
        rd = _FakeRD(post_error=requests.exceptions.Timeout("slow"))
        client = RealDebridProviderClient("token", raw_client=rd)
        with self.assertRaises(ProviderOperationError) as ctx:
            client.submit_magnet(MAGNET)
        self.assertEqual(ProviderErrorCode.UPSTREAM_TIMEOUT, ctx.exception.code)
        self.assertTrue(ctx.exception.retryable)

    def _rd_with_detail(self, detail: dict):
        class Torrents:
            @staticmethod
            def info(torrent_id):
                return _FakeRDResponse(detail)

        return RealDebridProviderClient(
            "token", raw_client=SimpleNamespace(torrents=Torrents())
        )

    def test_resolve_magnet_reports_files_ready(self):
        client = self._rd_with_detail(
            {
                "id": "T1",
                "hash": "abc",
                "filename": "Movie.mkv",
                "original_filename": "Movie.mkv",
                "bytes": 100,
                "progress": 100,
                "status": "downloaded",
                "links": ["https://cdn.invalid/1"],
                "files": [{"id": "1", "path": "Movie.mkv", "bytes": 100, "selected": True}],
            }
        )
        resolution = client.resolve_magnet("T1")
        self.assertEqual("files_ready", resolution.status)
        self.assertEqual("Movie.mkv", resolution.name)
        self.assertEqual(100, resolution.bytes)
        self.assertEqual(1, len(resolution.files))

    def test_resolve_magnet_reports_pending_without_files(self):
        client = self._rd_with_detail(
            {
                "id": "T1",
                "hash": "abc",
                "filename": "Movie",
                "original_filename": "Movie",
                "bytes": 0,
                "progress": 0,
                "status": "waiting_files",
                "links": [],
                "files": [],
            }
        )
        resolution = client.resolve_magnet("T1")
        self.assertEqual("metadata_pending", resolution.status)
        self.assertEqual(DEFAULT_RESOLUTION_REFRESH_SECS, resolution.next_refresh_secs)

    def test_resolve_magnet_error_status_raises_not_found(self):
        client = self._rd_with_detail(
            {
                "id": "T1",
                "hash": "abc",
                "filename": "Movie",
                "original_filename": "Movie",
                "bytes": 0,
                "progress": 0,
                "status": "error",
                "links": [],
                "files": [],
            }
        )
        with self.assertRaises(ProviderOperationError) as ctx:
            client.resolve_magnet("T1")
        self.assertEqual(ProviderErrorCode.TORRENT_NOT_FOUND, ctx.exception.code)

    def _rd_with_select_response(self, body, status_code: int = 200):
        captured: dict = {}

        class Torrents:
            @staticmethod
            def select_files(torrent_id, files):
                captured["args"] = (torrent_id, files)
                return _FakeRDResponse(body, status_code)

        return RealDebridProviderClient("token", raw_client=SimpleNamespace(torrents=Torrents())), captured

    def test_apply_file_selections_treats_action_already_done_as_ok(self):
        client, captured = self._rd_with_select_response(
            {"error": "action_already_done", "error_code": 31}
        )
        results = client.apply_file_selections(
            [FileSelection("T1", ("1", "2"))]
        )
        self.assertTrue(results[0].ok)
        self.assertIsNone(results[0].error)
        self.assertEqual(("T1", "1,2"), captured["args"])

    def test_apply_file_selections_reports_per_torrent_failure(self):
        def select_response(torrent_id, files):
            if torrent_id == "T2":
                return _FakeRDResponse({"error": "bad_request", "error_code": 9})
            return _FakeRDResponse(None)

        class Torrents:
            @staticmethod
            def select_files(torrent_id, files):
                return select_response(torrent_id, files)

        client = RealDebridProviderClient(
            "token", raw_client=SimpleNamespace(torrents=Torrents())
        )
        results = client.apply_file_selections(
            [FileSelection("T1", ("1",)), FileSelection("T2", ("2",))]
        )
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        error = results[1].error
        assert error is not None
        self.assertEqual(ProviderErrorCode.FILE_SELECTION_REJECTED, error.code)

    def test_apply_file_selections_non_2xx_is_typed(self):
        client, _ = self._rd_with_select_response(None, status_code=503)
        results = client.apply_file_selections([FileSelection("T1", ("1",))])
        self.assertFalse(results[0].ok)
        error = results[0].error
        assert error is not None
        self.assertEqual(ProviderErrorCode.UPSTREAM_UNAVAILABLE, error.code)

    def test_legacy_add_magnet_delegates_to_submit_magnet(self):
        rd = _FakeRD(post_response=_FakeRDResponse({"id": "T9"}))
        client = RealDebridProviderClient("token", raw_client=rd)
        self.assertEqual("T9", client.add_magnet(MAGNET))

    def test_list_torrents_stops_at_empty_204_page(self):
        page = [
            {"id": str(index), "filename": f"Movie {index}", "bytes": 1}
            for index in range(100)
        ]
        offsets: list[int | None] = []
        decoded = 0

        class _Empty:
            status_code = 204

            def json(self):
                nonlocal decoded
                decoded += 1
                raise requests.exceptions.JSONDecodeError(
                    "Expecting value", "<html>", 0
                )

        class Torrents:
            @staticmethod
            def get(offset=None, limit=100):
                offsets.append(offset)
                if offset is None:
                    return _FakeRDResponse(page)
                return _Empty()

        client = RealDebridProviderClient(
            "token", raw_client=SimpleNamespace(torrents=Torrents())
        )
        summaries = client.list_torrents()
        self.assertEqual(100, len(summaries))
        self.assertEqual("0", summaries[0].id)
        self.assertEqual("99", summaries[-1].id)
        self.assertEqual([None, 100], offsets)
        self.assertEqual(0, decoded)


class TestTorBoxIngestPrimitives(unittest.TestCase):
    @staticmethod
    def _ok(data):
        response = MagicMock()
        response.json.return_value = {"success": True, "data": data}
        return response

    @staticmethod
    def _fail(error: str, detail: str) -> MagicMock:
        response = MagicMock()
        response.json.return_value = {
            "success": False,
            "error": error,
            "detail": detail,
        }
        return response

    @staticmethod
    def _client(max_attempts: int = 1) -> TorBoxProviderClient:
        return TorBoxProviderClient(
            "fake_token",
            request_policy=ProviderRequestPolicy(
                min_interval_secs=0.0, max_attempts=max_attempts
            ),
        )

    @patch("buzz.providers.torbox.httpx.Client")
    def test_submit_magnet_returns_torrent_ref(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        mock_client.request.return_value = self._ok({"torrent_id": "42"})
        client = self._client()
        submission = client.submit_magnet(MAGNET)
        self.assertEqual("42", submission.torrent_id)

    @patch("buzz.providers.torbox.httpx.Client")
    def test_submit_magnet_payload_rejection_is_typed(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        mock_client.request.return_value = self._fail(
            "Invalid magnet link", "MAGNET_INVALID"
        )
        client = self._client()
        with self.assertRaises(ProviderOperationError) as ctx:
            client.submit_magnet(MAGNET)
        self.assertEqual(ProviderErrorCode.MAGNET_REJECTED, ctx.exception.code)
        self.assertTrue(ctx.exception.fallback_eligible)
        self.assertIn("MAGNET_INVALID", ctx.exception.detail)

    @patch("buzz.providers.torbox.httpx.Client")
    def test_submit_magnet_account_limit_is_fallback_eligible(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        mock_client.request.return_value = self._fail(
            "Torrent list limit reached", "LIMIT"
        )
        client = self._client()
        with self.assertRaises(ProviderOperationError) as ctx:
            client.submit_magnet(MAGNET)
        self.assertEqual(
            ProviderErrorCode.ACCOUNT_LIMIT_REACHED, ctx.exception.code
        )
        self.assertTrue(ctx.exception.fallback_eligible)

    @patch("buzz.providers.torbox.time.sleep")
    @patch("buzz.providers.torbox.httpx.Client")
    def test_submit_magnet_429_is_rate_limited(
        self, mock_client_class, mock_sleep
    ):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        request = httpx.Request("POST", "https://api.torbox.app/createtorrent")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError("too many", request=request, response=response)
        mock_client.request.side_effect = error
        client = self._client(max_attempts=2)
        with self.assertRaises(ProviderOperationError) as ctx:
            client.submit_magnet(MAGNET)
        self.assertEqual(ProviderErrorCode.RATE_LIMITED, ctx.exception.code)
        self.assertTrue(ctx.exception.retryable)

    @patch("buzz.providers.torbox.httpx.Client")
    def test_resolve_magnet_missing_torrent_is_pending(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        mock_client.request.side_effect = [self._ok([]), self._ok([])]
        client = self._client()
        resolution = client.resolve_magnet("42")
        self.assertEqual("metadata_pending", resolution.status)

    @patch("buzz.providers.torbox.httpx.Client")
    def test_resolve_magnet_reports_files_ready(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        item = {
            "torrent_id": "42",
            "name": "Movie.mkv",
            "size": 100,
            "download_state": "downloading",
            "files": [
                {
                    "id": "1",
                    "path": "Movie.mkv",
                    "size": 100,
                    "selected": True,
                    "download_present": True,
                }
            ],
        }
        mock_client.request.return_value = self._ok([item])
        client = self._client()
        resolution = client.resolve_magnet("42")
        self.assertEqual("files_ready", resolution.status)
        self.assertEqual("Movie.mkv", resolution.name)
        self.assertEqual(1, len(resolution.files))

    @patch("buzz.providers.torbox.httpx.Client")
    def test_apply_file_selections_is_local_noop(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        client = self._client()
        results = client.apply_file_selections(
            [FileSelection("42", ("1", "2"))]
        )
        self.assertTrue(results[0].ok)
        self.assertIsNone(results[0].error)
        mock_client.request.assert_not_called()

    @patch("buzz.providers.torbox.httpx.Client")
    def test_legacy_add_magnet_delegates_to_submit_magnet(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        mock_client.request.return_value = self._ok({"torrent_id": "42"})
        client = self._client()
        self.assertEqual("42", client.add_magnet(MAGNET))


class TestLocalIngestPrimitives(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = LocalProviderClient(
            store_path=f"{self.tmp.name}/store",
            db_path=f"{self.tmp.name}/buzz.sqlite",
        )

    def test_submit_magnet_is_unsupported(self):
        with self.assertRaises(ProviderOperationError) as ctx:
            self.client.submit_magnet(MAGNET)
        self.assertEqual(
            ProviderErrorCode.UNSUPPORTED_OPERATION, ctx.exception.code
        )
        self.assertTrue(ctx.exception.fallback_eligible)

    def test_resolve_magnet_missing_is_not_found(self):
        with self.assertRaises(ProviderOperationError) as ctx:
            self.client.resolve_magnet("abc")
        self.assertEqual(ProviderErrorCode.TORRENT_NOT_FOUND, ctx.exception.code)

    def test_resolve_magnet_present_is_files_ready(self):
        conn = self.client._connection()
        db.save_local_torrent(
            conn,
            "abc",
            "Movie",
            10,
            [{"path": "Movie.mkv", "bytes": 10, "stored_rel_path": "Movie.mkv"}],
        )
        resolution = self.client.resolve_magnet("abc")
        self.assertEqual("files_ready", resolution.status)
        self.assertEqual("Movie", resolution.name)

    def test_apply_file_selections_is_unsupported(self):
        results = self.client.apply_file_selections(
            [FileSelection("abc", ("1",))]
        )
        self.assertFalse(results[0].ok)
        error = results[0].error
        assert error is not None
        self.assertEqual(ProviderErrorCode.UNSUPPORTED_OPERATION, error.code)

    def test_legacy_select_files_still_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            self.client.select_files("abc", ["1"])
        with self.assertRaises(RuntimeError):
            self.client.add_magnet(MAGNET)


class TestLimiterStillReplaysTypedErrors(unittest.TestCase):
    """Typed errors raised inside limiter calls propagate unchanged."""

    @patch("buzz.core.providers.time.sleep")
    def test_typed_error_is_not_retried_or_swallowed(self, mock_sleep):
        limiter = ProviderRequestLimiter(
            ProviderRequestPolicy(min_interval_secs=0.0, max_attempts=3)
        )

        def fail():
            raise ProviderOperationError(
                "torbox",
                "submit_magnet",
                ProviderErrorCode.MAGNET_REJECTED,
            )

        with self.assertRaises(ProviderOperationError) as ctx:
            limiter.run("submit_magnet", fail)
        self.assertEqual(ProviderErrorCode.MAGNET_REJECTED, ctx.exception.code)
        self.assertTrue(ctx.exception.fallback_eligible)


if __name__ == "__main__":
    unittest.main()
