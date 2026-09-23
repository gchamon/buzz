"""Durable magnet ingest: state machine, persistence, and orchestration."""

import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from buzz.core import db, ingest
from buzz.core.events import registry as event_registry
from buzz.core.ingest import (
    IngestEntry,
    IngestState,
    can_transition,
    new_batch_id,
    new_entry_id,
    parse_btih,
)
from buzz.core.providers import (
    FileSelectionResult,
    MagnetResolution,
    MagnetSubmission,
    ProviderErrorCode,
    ProviderFile,
    ProviderOperationError,
    ProviderTorrentDetail,
    ProviderTorrentSummary,
)
from buzz.core.state import BuzzState
from buzz.models import DavConfig as Config

HEX_HASH = "a" * 40
B32_HASH = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # base32 of 20 zero bytes
MAGNET = f"magnet:?xt=urn:btih:{HEX_HASH}&dn=Movie"
OTHER_MAGNET = f"magnet:?xt=urn:btih:{'b' * 40}"


class FakeIngestProvider:
    """Configurable fake provider implementing the semantic primitives.

    Mirrors the real provider contract: once a magnet is accepted, the
    torrent stays listed and detail-fetchable until
    ``remove_upstream_torrent`` models its deletion.
    """

    def __init__(
        self,
        submit_error: Exception | None = None,
        resolve_behavior: object = "ready",
        selection_errors: dict[str, ProviderOperationError] | None = None,
        submit_gate: threading.Event | None = None,
        submit_started: threading.Event | None = None,
    ):
        self.submitted: list[str] = []
        self.torrents: dict[str, str] = {}
        self.removed: list[str] = []
        self.fail_submit = True
        self.submit_error = submit_error
        self.resolve_calls: list[str] = []
        self.resolve_behavior = resolve_behavior
        self.selection_errors = selection_errors or {}
        self.selection_calls: list[tuple[str, list[str]]] = []
        self.submit_gate = submit_gate
        self.submit_started = submit_started

    def is_healthy(self) -> bool:
        return True

    def list_torrents(self) -> list:
        return [
            self._summary(torrent_id)
            for torrent_id in self.torrents
            if torrent_id not in self.removed
        ]

    def remove_upstream_torrent(self, torrent_id: str) -> None:
        if torrent_id not in self.removed:
            self.removed.append(torrent_id)

    def fetch_details(
        self, torrent_ids: list[str], on_progress=None
    ) -> dict:
        results = {}
        for torrent_id in torrent_ids:
            if on_progress is not None:
                on_progress(torrent_id, 0, len(torrent_ids))
            magnet = self.torrents.get(torrent_id)
            if magnet is None:
                continue
            results[torrent_id] = self._detail(torrent_id, magnet)
        return results

    def submit_magnet(self, magnet: str) -> MagnetSubmission:
        if self.submit_started is not None:
            self.submit_started.set()
        if self.submit_gate is not None:
            self.submit_gate.wait(timeout=5.0)
        self.submitted.append(magnet)
        if self.fail_submit and self.submit_error is not None:
            raise self.submit_error
        torrent_id = f"T{len(self.submitted)}"
        self.torrents[torrent_id] = magnet
        return MagnetSubmission(torrent_id=torrent_id)

    def resolve_magnet(self, torrent_id: str) -> MagnetResolution:
        self.resolve_calls.append(torrent_id)
        behavior = self.resolve_behavior
        if callable(behavior):
            behavior = behavior(torrent_id)
        if isinstance(behavior, Exception):
            raise behavior
        if behavior == "pending":
            return MagnetResolution(
                status="metadata_pending", next_refresh_secs=0.1
            )
        return MagnetResolution(
            status="files_ready",
            name="Movie",
            bytes=101,
            files=(
                ProviderFile(id="1", path="Movie.mkv", bytes=100, selected=True),
                ProviderFile(id="2", path="Movie.nfo", bytes=1, selected=False),
            ),
        )

    def apply_file_selections(
        self, selections
    ) -> list[FileSelectionResult]:
        results = []
        for selection in selections:
            self.selection_calls.append(
                (selection.torrent_id, list(selection.file_ids))
            )
            error = self.selection_errors.get(selection.torrent_id)
            if error is not None:
                results.append(
                    FileSelectionResult(
                        selection.torrent_id, ok=False, error=error
                    )
                )
            else:
                results.append(FileSelectionResult(selection.torrent_id, ok=True))
        return results

    def _summary(self, torrent_id: str) -> ProviderTorrentSummary:
        return ProviderTorrentSummary(
            id=torrent_id,
            name="Movie",
            bytes=101,
            progress=0.0,
            status="downloaded",
        )

    def _detail(self, torrent_id: str, magnet: str) -> ProviderTorrentDetail:
        thash = ingest.parse_btih(magnet) or ""
        return ProviderTorrentDetail(
            id=torrent_id,
            hash=thash,
            name="Movie",
            original_name="Movie",
            bytes=101,
            progress=0.0,
            status="downloaded",
            files=(
                ProviderFile(
                    id="1", path="Movie.mkv", bytes=100, selected=True
                ),
                ProviderFile(
                    id="2", path="Movie.nfo", bytes=1, selected=False
                ),
            ),
        )


def _config(tmpdir: str, priority=("real_debrid", "torbox")) -> Config:
    return Config(
        token="token",
        provider_priority=priority,
        state_dir=tmpdir,
        hook_command="",
        curator_url="",
    )


def _wait_for_ingest_task(state: BuzzState, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for task in state.background_tasks.snapshot():
            if task["kind"] == "ingest" and task["status"] in {
                "complete",
                "failed",
                "cancelled",
            }:
                return task
        time.sleep(0.02)
    raise TimeoutError("ingest task did not finish")


def _ingest_task(state: BuzzState) -> dict:
    for task in state.background_tasks.snapshot():
        if task["kind"] == "ingest":
            return task
    raise AssertionError("no ingest task found")


def _entry_by_thash(state: BuzzState, thash: str) -> IngestEntry:
    for entry in state.ingest.values():
        if entry.thash == thash:
            return entry
    raise AssertionError(f"no ingest entry for thash {thash}")


def _wait_task_by_id(state: BuzzState, task_id: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for task in state.background_tasks.snapshot():
            if task["id"] == task_id and task["status"] in {
                "complete",
                "failed",
                "cancelled",
            }:
                return task
        time.sleep(0.02)
    raise TimeoutError(f"task {task_id} did not finish")



class TestParseBtih(unittest.TestCase):
    def test_hex_hash(self):
        self.assertEqual(HEX_HASH, parse_btih(MAGNET))

    def test_base32_hash(self):
        magnet = f"magnet:?xt=urn:btih:{B32_HASH}"
        self.assertEqual("0" * 40, parse_btih(magnet))

    def test_mixed_case_and_whitespace(self):
        magnet = f"  magnet:?xt=urn:btih:{HEX_HASH.upper()}  "
        self.assertEqual(HEX_HASH, parse_btih(magnet))

    def test_missing_hash_is_none(self):
        self.assertIsNone(parse_btih("magnet:?dn=Movie"))
        self.assertIsNone(parse_btih("not-a-magnet:?xt=urn:btih:abc"))
        self.assertIsNone(parse_btih(""))


class TestStateMachine(unittest.TestCase):
    def test_valid_transition_paths(self):
        path = [
            IngestState.QUEUED,
            IngestState.SUBMITTING,
            IngestState.METADATA_PENDING,
            IngestState.FILES_READY,
            IngestState.SELECTING,
            IngestState.AWAITING_CONFIRMATION,
            IngestState.CONFIRMED,
        ]
        for current, target in zip(path, path[1:], strict=False):
            self.assertTrue(can_transition(current, target), f"{current} -> {target}")

    def test_invalid_transitions_rejected(self):
        self.assertFalse(
            can_transition(IngestState.FILES_READY, IngestState.CONFIRMED)
        )
        self.assertFalse(can_transition(IngestState.FAILED, IngestState.SUBMITTING))
        self.assertFalse(
            can_transition(IngestState.CONFIRMED, IngestState.FILES_READY)
        )
        self.assertTrue(can_transition(IngestState.QUEUED, IngestState.QUEUED))


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = self._tmp.name
        self.clients: dict[str, FakeIngestProvider] = {}

    def make_state(self, on_ui_change=None):
        clients = dict(self.clients)
        return BuzzState(
            _config(self.state_dir, tuple(clients)),
            client=clients,
            on_ui_change=on_ui_change,
        )

    def add_provider(self, name, **kwargs):
        provider = FakeIngestProvider(**kwargs)
        self.clients[name] = provider
        return provider


class TestSubmitMagnets(IngestTestCase):
    def test_valid_magnet_creates_queued_entry_and_archive(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        entry_ids = state.submit_magnets([MAGNET])
        self.assertEqual(1, len(entry_ids))
        entry = state.ingest[entry_ids[0]]
        # The background task may already have started submitting.
        self.assertIn(entry.state, {IngestState.QUEUED, IngestState.SUBMITTING})
        self.assertEqual(HEX_HASH, entry.thash)
        self.assertIn(HEX_HASH, state.archive)
        self.assertEqual(MAGNET, state.archive[HEX_HASH]["magnet"])
        _wait_for_ingest_task(state)

    def test_malformed_line_fails_before_provider_work(self):
        rd = self.add_provider("real_debrid")
        state = self.make_state()
        entry_ids = state.submit_magnets(["this is not a magnet"])
        entry = state.ingest[entry_ids[0]]
        self.assertEqual(IngestState.FAILED, entry.state)
        self.assertEqual(ProviderErrorCode.INVALID_MAGNET, entry.error_code)
        self.assertEqual("", entry.thash)
        self.assertEqual([], rd.submitted)
        self.assertEqual({}, state.archive)

    def test_duplicate_hash_within_batch_is_single_entry(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        entry_ids = state.submit_magnets([MAGNET, MAGNET])
        self.assertEqual(1, len(entry_ids))

    def test_existing_cache_hash_confirms_without_provider_work(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        state.cache["T9"] = {
            "signature": {},
            "info": {"id": "T9", "hash": HEX_HASH, "provider": "real_debrid"},
            "magnet": None,
        }
        entry_ids = state.submit_magnets([MAGNET])
        entry = state.ingest[entry_ids[0]]
        self.assertEqual(IngestState.CONFIRMED, entry.state)
        self.assertEqual("real_debrid", entry.accepted_provider)
        self.assertEqual("T9", entry.provider_torrent_id)
        self.assertEqual([], state.clients["real_debrid"].submitted)

    def test_unknown_explicit_provider_is_rejected(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        with self.assertRaises(ValueError):
            state.submit_magnets([MAGNET], provider="torbox")

    def test_entries_survive_restart(self):
        self.add_provider("real_debrid", resolve_behavior="pending")
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(IngestState.METADATA_PENDING, entry.state)
        state.close()

        state2 = self.make_state()
        loaded = _entry_by_thash(state2, HEX_HASH)
        self.assertIsNotNone(loaded)
        self.assertEqual(IngestState.METADATA_PENDING, loaded.state)
        self.assertEqual("real_debrid", loaded.accepted_provider)


class TestFallbackPolicy(IngestTestCase):
    def test_fallback_eligible_failure_moves_to_next_provider(self):
        rd = self.add_provider(
            "real_debrid",
            submit_error=ProviderOperationError(
                "real_debrid", "submit_magnet", ProviderErrorCode.RATE_LIMITED
            ),
        )
        tb = self.add_provider("torbox")
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual("torbox", entry.accepted_provider)
        self.assertEqual(1, len(rd.submitted))
        self.assertEqual(1, len(tb.submitted))
        attempts = state.ingest_attempts(entry.id)
        self.assertEqual(
            [
                ("real_debrid", "submit_magnet", "failed"),
                ("torbox", "submit_magnet", "accepted"),
            ],
            [(a["provider"], a["operation"], a["outcome"]) for a in attempts],
        )

    def test_non_fallback_failure_stops_without_further_attempts(self):
        rd = self.add_provider(
            "real_debrid",
            submit_error=ProviderOperationError(
                "real_debrid",
                "submit_magnet",
                ProviderErrorCode.AUTHENTICATION_FAILED,
            ),
        )
        tb = self.add_provider("torbox")
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(IngestState.FAILED, entry.state)
        self.assertEqual(
            ProviderErrorCode.AUTHENTICATION_FAILED, entry.error_code
        )
        self.assertEqual(1, len(rd.submitted))
        self.assertEqual(0, len(tb.submitted))

    def test_all_providers_failing_fails_entry_with_last_code(self):
        self.add_provider(
            "real_debrid",
            submit_error=ProviderOperationError(
                "real_debrid",
                "submit_magnet",
                ProviderErrorCode.UPSTREAM_UNAVAILABLE,
                detail="connection refused",
            ),
        )
        self.add_provider(
            "torbox",
            submit_error=ProviderOperationError(
                "torbox",
                "submit_magnet",
                ProviderErrorCode.MAGNET_REJECTED,
                detail="magnet link not found",
            ),
        )
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(IngestState.FAILED, entry.state)
        self.assertEqual(ProviderErrorCode.MAGNET_REJECTED, entry.error_code)
        attempts = state.ingest_attempts(entry.id)
        self.assertEqual(2, len(attempts))

    def test_explicit_provider_choice_limits_targets(self):
        self.add_provider(
            "real_debrid",
            submit_error=ProviderOperationError(
                "real_debrid",
                "submit_magnet",
                ProviderErrorCode.UPSTREAM_UNAVAILABLE,
            ),
        )
        self.add_provider("torbox")
        state = self.make_state()
        state.submit_magnets([MAGNET], provider="real_debrid")
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(IngestState.FAILED, entry.state)

    def test_infringing_file_fallback_to_torbox(self):
        rd = self.add_provider(
            "real_debrid",
            submit_error=ProviderOperationError(
                "real_debrid",
                "submit_magnet",
                ProviderErrorCode.MAGNET_REJECTED,
                detail="infringing_file",
            ),
        )
        tb = self.add_provider("torbox")
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual("torbox", entry.accepted_provider)
        self.assertEqual(1, len(rd.submitted))
        self.assertEqual(1, len(tb.submitted))
        attempts = state.ingest_attempts(entry.id)
        self.assertEqual(
            [
                ("real_debrid", "submit_magnet", "failed"),
                ("torbox", "submit_magnet", "accepted"),
            ],
            [(a["provider"], a["operation"], a["outcome"]) for a in attempts],
        )

    def test_files_ready_transition_notifies_entry_id_and_state(self):
        self.add_provider("real_debrid")
        notifications: list[tuple] = []
        state = self.make_state(
            on_ui_change=lambda topic, payload=None: notifications.append(
                (topic, payload)
            )
        )
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(IngestState.FILES_READY, entry.state)
        self.assertIn(
            ("ingest", {"entry_id": entry.id, "state": IngestState.FILES_READY}),
            notifications,
        )


class TestResolution(IngestTestCase):
    def test_files_ready_enriches_archive_and_seeds_selection(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(IngestState.FILES_READY, entry.state)
        self.assertEqual(2, len(entry.files))
        self.assertEqual("Movie", entry.name)
        self.assertEqual(101, entry.total_bytes)
        record = state.archive[HEX_HASH]
        self.assertEqual("Movie", record["name"])
        self.assertEqual(101, record["bytes"])
        self.assertEqual(1, len(record["files"]))
        self.assertIn(HEX_HASH, state.file_selections)
        self.assertEqual({"Movie.mkv"}, state.file_selections[HEX_HASH])

    def test_pending_metadata_schedules_refresh(self):
        self.add_provider("real_debrid", resolve_behavior="pending")
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(IngestState.METADATA_PENDING, entry.state)
        next_at = entry.next_resolution_at
        self.assertIsNotNone(next_at)
        if next_at is None:
            self.fail("next_resolution_at not set")
        self.assertGreater(next_at, time.time())

    def test_reconcile_due_resolves_pending_entry(self):
        provider = self.add_provider("real_debrid", resolve_behavior="pending")
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(1, len(provider.resolve_calls))
        entry.next_resolution_at = 0.0
        provider.resolve_behavior = "ready"
        ingest.reconcile_due(state)
        self.assertEqual(IngestState.FILES_READY, entry.state)
        self.assertEqual(2, len(provider.resolve_calls))

    def test_bounded_resolution_errors_fail_entry(self):
        self.add_provider(
            "real_debrid",
            resolve_behavior=ProviderOperationError(
                "real_debrid",
                "resolve_magnet",
                ProviderErrorCode.UPSTREAM_UNAVAILABLE,
                detail="timeout",
            ),
        )
        state = self.make_state()
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)
        self.assertEqual(IngestState.METADATA_PENDING, entry.state)
        self.assertEqual(1, entry.resolution_errors)
        for _ in range(ingest.RESOLUTION_ERROR_LIMIT):
            if entry.state == IngestState.FAILED:
                break
            entry.next_resolution_at = 0.0
            ingest.reconcile_due(state)
        self.assertEqual(IngestState.FAILED, entry.state)
        self.assertEqual(
            ProviderErrorCode.UPSTREAM_UNAVAILABLE, entry.error_code
        )


class TestSelectionConfirmation(IngestTestCase):
    def _awaiting_entry(self, state):
        """Create and persist a minimal awaiting-confirmation entry.

        Direct finalizer unit setup: bypasses the background confirmation
        flow so the finalizer's membership and transition behavior can be
        exercised without racing the sync it queues.
        """
        batch_id = new_batch_id()
        entry = IngestEntry(
            id=new_entry_id(),
            batch_id=batch_id,
            thash=HEX_HASH,
            magnet=MAGNET,
            state=IngestState.AWAITING_CONFIRMATION,
            created_at=time.time(),
            display_name="Movie",
            accepted_provider="real_debrid",
            provider_torrent_id="T1",
            files=[{"id": "1", "path": "Movie.mkv", "bytes": 100, "selected": 1}],
        )
        state.ingest[entry.id] = entry
        db.save_ingest_batch(state.conn, batch_id, "auto", time.time())
        db.save_ingest_entry(state.conn, entry.to_row())
        return entry

    def _ready_entry(self, state, provider_name="real_debrid"):
        state.submit_magnets([MAGNET])
        _wait_for_ingest_task(state)
        return _entry_by_thash(state, HEX_HASH)

    def _wait_cache_key(self, state, cache_key):
        """Block until the queued ingest sync commits ``state.cache``.

        ``state.cache`` is replaced only inside the sync commit under the
        lock, so a present provider/torrent key proves the ingest-triggered
        sync has settled. If it already finished before the first poll, the
        completed sync task is observed and the cache is checked one more
        time before the three-second deadline.
        """
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if cache_key in state.cache:
                return
            for task in state.background_tasks.snapshot():
                if task["kind"] == "sync" and task["status"] in {
                    "complete",
                    "failed",
                    "cancelled",
                }:
                    _wait_task_by_id(state, task["id"])
            time.sleep(0.02)
        self.fail(f"initial sync never populated cache key {cache_key!r}")

    def test_confirm_selections_through_synced_cache(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        entry = self._ready_entry(state)
        state.confirm_ingest_selections({entry.id: ["1"]})
        deadline = time.monotonic() + 3.0
        while entry.state != IngestState.CONFIRMED:
            if time.monotonic() > deadline:
                self.fail(f"entry stuck in {entry.state}")
            time.sleep(0.02)
        provider = state.clients["real_debrid"]
        self.assertEqual([("T1", ["1"])], provider.selection_calls)

    def test_confirmed_rd_selection_unions_provider_selections(self):
        provider = self.add_provider("real_debrid")
        state = self.make_state()
        entry = self._ready_entry(state)
        # Provider already has file 2 selected; request only file 1.
        entry.files = [
            {"id": "1", "path": "Movie.mkv", "bytes": 100, "selected": 0},
            {"id": "2", "path": "Extra.mkv", "bytes": 50, "selected": 1},
        ]
        state.confirm_ingest_selections({entry.id: ["1"]})
        deadline = time.monotonic() + 3.0
        while entry.state != IngestState.CONFIRMED:
            if time.monotonic() > deadline:
                self.fail(f"entry stuck in {entry.state}")
            time.sleep(0.02)
        self.assertEqual([("T1", ["1", "2"])], provider.selection_calls)
        # The portable draft keeps only what the operator requested.
        self.assertEqual({"Movie.mkv"}, state.file_selections[HEX_HASH])

    def test_partial_failure_reverts_entry_to_files_ready(self):
        self.add_provider(
            "real_debrid",
            selection_errors={
                "T1": ProviderOperationError(
                    "real_debrid",
                    "apply_file_selections",
                    ProviderErrorCode.FILE_SELECTION_REJECTED,
                    detail="selection rejected",
                )
            },
        )
        state = self.make_state()
        entry = self._ready_entry(state)
        state.confirm_ingest_selections({entry.id: ["1"]})
        deadline = time.monotonic() + 3.0
        while entry.error_code is None:
            if time.monotonic() > deadline:
                self.fail(f"failure never recorded; state={entry.state}")
            time.sleep(0.02)
        self.assertEqual(IngestState.FILES_READY, entry.state)
        self.assertEqual(
            ProviderErrorCode.FILE_SELECTION_REJECTED, entry.error_code
        )

    def test_rejected_selection_fails_task_loudly_with_logs(self):
        self.add_provider(
            "real_debrid",
            selection_errors={
                "T1": ProviderOperationError(
                    "real_debrid",
                    "apply_file_selections",
                    ProviderErrorCode.FILE_SELECTION_REJECTED,
                    detail="unknown_ressource",
                )
            },
        )
        state = self.make_state()
        entry = self._ready_entry(state)
        task_id = state.confirm_ingest_selections({entry.id: ["1"]})
        task = _wait_task_by_id(state, task_id)
        self.assertEqual("failed", task["status"])
        self.assertIn("file selection rejected", task["error"])
        self.assertIn(entry.display_name, task["error"])
        self.assertEqual(IngestState.FILES_READY, entry.state)
        messages = [log["message"] for log in task["logs"]]
        self.assertTrue(
            any(
                "confirming file selection for" in message
                and entry.display_name in message
                for message in messages
            ),
            messages,
        )
        self.assertTrue(
            any(
                "file selection rejected for" in message
                and entry.display_name in message
                and "unknown_ressource" in message
                for message in messages
            ),
            messages,
        )

    def test_confirm_label_uses_file_selection_confirmation(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        entry = self._ready_entry(state)
        task_id = state.confirm_ingest_selections({entry.id: ["1"]})
        task = _wait_task_by_id(state, task_id)
        self.assertEqual(
            "ingest_file_selection_confirmation: 1 entry(ies)", task["label"]
        )

    def test_confirm_unknown_entry_fails_task(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        task_id = state.confirm_ingest_selections({"no-such-entry": ["1"]})
        deadline = time.monotonic() + 3.0
        task = None
        while time.monotonic() < deadline:
            task = next(
                (
                    item
                    for item in state.background_tasks.snapshot()
                    if item["id"] == task_id
                ),
                None,
            )
            if task is not None and task["status"] == "failed":
                break
            time.sleep(0.02)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual("failed", task["status"])
        self.assertIn("no file selection to confirm", task["error"])

    def test_finalize_confirmations_confirms_and_prunes(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        entry = self._awaiting_entry(state)
        # The synced cache holds the accepted torrent's record.
        state.cache["T1"] = {
            "signature": {},
            "info": {"id": "T1", "hash": HEX_HASH, "provider": "real_debrid"},
            "magnet": None,
        }
        self.assertTrue(ingest.finalize_confirmations(state))
        self.assertEqual(IngestState.CONFIRMED, entry.state)
        self.assertEqual(MAGNET, state.cache["T1"]["magnet"])
        # A second pass prunes the confirmed entry.
        self.assertTrue(ingest.finalize_confirmations(state))
        self.assertNotIn(entry.id, state.ingest)

    def test_finalize_waits_for_missing_cache_entry(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        entry = self._awaiting_entry(state)
        self.assertFalse(ingest.finalize_confirmations(state))
        self.assertEqual(IngestState.AWAITING_CONFIRMATION, entry.state)

    def test_confirmed_selections_survive_state_restart(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        entry = self._ready_entry(state)
        state.confirm_ingest_selections({entry.id: ["1"]})
        deadline = time.monotonic() + 3.0
        while entry.state != IngestState.CONFIRMED:
            if time.monotonic() > deadline:
                self.fail(f"entry stuck in {entry.state}")
            time.sleep(0.02)
        state.close()
        reloaded = BuzzState(
            _config(self.state_dir, tuple(self.clients)),
            client=dict(self.clients),
        )
        self.addCleanup(reloaded.close)
        reloaded_entry = _entry_by_thash(reloaded, HEX_HASH)
        # Reloading does not sync; the confirmed row persists until the
        # next prune pass.
        self.assertEqual(IngestState.CONFIRMED, reloaded_entry.state)
        selected = {
            f.get("id") for f in reloaded_entry.files if f.get("selected")
        }
        self.assertEqual({"1"}, selected)

    def test_sync_removes_files_ready_entry_whose_upstream_disappeared(self):
        client = self.add_provider("real_debrid")
        state = self.make_state()
        entry = self._ready_entry(state)
        self.assertEqual(IngestState.FILES_READY, entry.state)
        accepted = entry.accepted_provider
        torrent_id = entry.provider_torrent_id
        self.assertEqual("T1", torrent_id)
        assert accepted is not None
        assert torrent_id is not None
        cache_key = state._cache_key(accepted, torrent_id)
        self._wait_cache_key(state, cache_key)
        state.cache[cache_key] = {
            "signature": {},
            "info": {
                "id": torrent_id,
                "hash": HEX_HASH,
                "provider": "real_debrid",
            },
            "magnet": MAGNET,
        }
        # Upstream deletes the torrent; the next sync's rebuilt cache no
        # longer contains the entry's exact provider/torrent key.
        client.remove_upstream_torrent(torrent_id)
        event_registry.clear()
        state.sync(trigger_hook=False)
        self.assertNotIn(entry.id, state.ingest)
        self.assertEqual([], state.ingest_attempts(entry.id))
        self.assertNotIn(cache_key, state.cache)
        # Archive metadata and the saved selection draft survive the removal.
        self.assertIn(HEX_HASH, state.archive)
        self.assertEqual("Movie", state.archive[HEX_HASH]["name"])
        self.assertIn(HEX_HASH, state.file_selections)
        # Automatic cleanup is distinguishable from manual removal in logs.
        removed = [
            event
            for event in event_registry.get_recent()
            if event.get("event") == "ingest_upstream_removed"
        ]
        self.assertEqual(1, len(removed))
        self.assertEqual(
            "ingest upstream torrent disappeared for Movie on real_debrid",
            removed[0]["message"],
        )
        self.assertEqual(entry.id, removed[0]["entry_id"])
        self.assertEqual(accepted, removed[0]["provider"])
        self.assertEqual(HEX_HASH, removed[0]["hash"])

    def test_finalizer_keeps_files_ready_entry_with_matching_upstream(self):
        self.add_provider("real_debrid")
        state = self.make_state()
        entry = self._ready_entry(state)
        self.assertEqual(IngestState.FILES_READY, entry.state)
        accepted = entry.accepted_provider
        torrent_id = entry.provider_torrent_id
        self.assertEqual("T1", torrent_id)
        assert accepted is not None
        assert torrent_id is not None
        cache_key = state._cache_key(accepted, torrent_id)
        self._wait_cache_key(state, cache_key)
        changed = ingest.finalize_confirmations(state)
        self.assertFalse(changed)
        self.assertIn(entry.id, state.ingest)
        self.assertEqual(IngestState.FILES_READY, entry.state)


class TestCancellation(IngestTestCase):
    def test_cancel_stops_future_entries_and_keeps_accepted(self):
        gate = threading.Event()
        started = threading.Event()
        self.add_provider(
            "real_debrid",
            resolve_behavior="pending",
            submit_gate=gate,
            submit_started=started,
        )
        state = self.make_state()
        entry_ids = state.submit_magnets([MAGNET, OTHER_MAGNET])
        self.assertTrue(started.wait(timeout=3.0))
        state.background_tasks.cancel(_ingest_task(state)["id"])
        gate.set()
        # The in-flight entry completes its acceptance; the next entry is
        # never submitted and stays queued as a durable row.
        deadline = time.monotonic() + 3.0
        while (
            state.ingest[entry_ids[0]].state != IngestState.METADATA_PENDING
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        self.assertEqual(IngestState.METADATA_PENDING, state.ingest[entry_ids[0]].state)
        self.assertEqual(IngestState.QUEUED, state.ingest[entry_ids[1]].state)
        self.assertEqual(1, len(state.clients["real_debrid"].submitted))
        self.assertEqual(2, len(state.ingest))
        self.assertIsNotNone(state.ingest[entry_ids[0]].provider_torrent_id)


class TestRetryAndRemoval(IngestTestCase):
    def _failed_entry(self, state):
        state.submit_magnets([MAGNET], provider="torbox")
        entry = _entry_by_thash(state, HEX_HASH)
        self._wait_state(state, entry, IngestState.FAILED)
        return entry

    def _wait_state(self, state, entry, target):
        deadline = time.monotonic() + 3.0
        while entry.state != target:
            if time.monotonic() > deadline:
                self.fail(f"entry stuck in {entry.state}")
            time.sleep(0.02)

    def test_retry_reuses_row_keeps_history_and_original_choice(self):
        provider = self.add_provider(
            "torbox",
            submit_error=ProviderOperationError(
                "torbox",
                "submit_magnet",
                ProviderErrorCode.MAGNET_REJECTED,
                detail="magnet link not found",
            ),
        )
        state = self.make_state()
        entry = self._failed_entry(state)
        self.assertEqual(1, len(state.ingest_attempts(entry.id)))
        # Hold the retry's provider call so the reset fields can be
        # inspected while the row is durably queued.
        gate = threading.Event()
        provider.submit_gate = gate

        state.retry_ingest(entry.id)

        self._wait_state(state, entry, IngestState.SUBMITTING)
        self.assertEqual(1, len(state.ingest))
        self.assertEqual(HEX_HASH, entry.thash)
        self.assertIsNone(entry.name)
        self.assertIsNone(entry.total_bytes)
        self.assertIsNone(entry.accepted_provider)
        self.assertIsNone(entry.provider_torrent_id)
        self.assertEqual(0, entry.resolution_attempts)
        self.assertEqual(0, entry.resolution_errors)
        self.assertIsNone(entry.next_resolution_at)
        self.assertIsNone(entry.error_code)
        self.assertIsNone(entry.error_detail)
        self.assertEqual([], entry.files)
        # Replacement attempt on the same row, original provider choice.
        provider.fail_submit = False
        gate.set()
        self._wait_state(state, entry, IngestState.FILES_READY)
        self.assertEqual(2, len(provider.submitted))
        self.assertEqual(2, len(state.ingest_attempts(entry.id)))
        self.assertEqual(
            ("torbox", "submit_magnet", "accepted"),
            (
                state.ingest_attempts(entry.id)[1]["provider"],
                state.ingest_attempts(entry.id)[1]["operation"],
                state.ingest_attempts(entry.id)[1]["outcome"],
            ),
        )
        state.close()

    def test_retry_with_missing_batch_row_uses_auto(self):
        provider = self.add_provider(
            "torbox",
            submit_error=ProviderOperationError(
                "torbox",
                "submit_magnet",
                ProviderErrorCode.MAGNET_REJECTED,
            ),
        )
        state = self.make_state()
        entry = self._failed_entry(state)
        provider.fail_submit = False

        with patch(
            "buzz.core.db.load_ingest_batch_provider_choice",
            return_value=None,
        ):
            task_id = state.retry_ingest(entry.id)

        self.assertTrue(task_id)
        _wait_for_ingest_task(state)
        self._wait_state(state, entry, IngestState.FILES_READY)
        state.close()

    def test_load_batch_provider_choice_helper(self):
        self.add_provider(
            "torbox",
            submit_error=ProviderOperationError(
                "torbox",
                "submit_magnet",
                ProviderErrorCode.MAGNET_REJECTED,
            ),
        )
        state = self.make_state()
        state.submit_magnets([MAGNET], provider="torbox")
        _wait_for_ingest_task(state)
        entry = _entry_by_thash(state, HEX_HASH)

        self.assertEqual(
            "torbox",
            db.load_ingest_batch_provider_choice(state.conn, entry.batch_id),
        )
        self.assertIsNone(
            db.load_ingest_batch_provider_choice(state.conn, "missing-batch")
        )
        state.close()

    def test_retry_rejects_non_failed_entry(self):
        self.add_provider("torbox")
        state = self.make_state()
        state.submit_magnets([MAGNET])
        entry = _entry_by_thash(state, HEX_HASH)
        self._wait_state(state, entry, IngestState.FILES_READY)

        with self.assertRaises(ValueError):
            state.retry_ingest(entry.id)
        with self.assertRaises(ValueError):
            state.retry_ingest("missing")

        self.assertEqual(IngestState.FILES_READY, entry.state)
        state.close()

    def test_remove_drops_entry_and_attempts_but_not_archive(self):
        self.add_provider(
            "torbox",
            submit_error=ProviderOperationError(
                "torbox",
                "submit_magnet",
                ProviderErrorCode.MAGNET_REJECTED,
            ),
        )
        state = self.make_state()
        entry = self._failed_entry(state)
        state.archive[entry.thash] = {
            "hash": entry.thash,
            "name": "Movie",
            "bytes": 99,
            "files": [],
            "deleted_at": "2026-01-01T00:00:00Z",
            "magnet": entry.magnet,
        }

        state.remove_ingest(entry.id)

        self.assertNotIn(entry.id, state.ingest)
        self.assertEqual([], state.ingest_attempts(entry.id))
        self.assertEqual(
            "Movie",
            state.archive[entry.thash]["name"],
            "removal must keep archive records",
        )
        with self.assertRaises(ValueError):
            state.remove_ingest(entry.id)
        state.close()


if __name__ == "__main__":
    unittest.main()
