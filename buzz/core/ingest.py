"""Durable magnet ingest: domain model, validation, and orchestration.

Submitted magnets become independent, persistent ingest entries that are
processed sequentially in the background. Provider I/O never runs while
holding the state lock; state transitions are persisted to SQLite and pushed
to connected cache views so ingest survives restarts and reconnects.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import record_event
from .providers import (
    DEFAULT_RESOLUTION_REFRESH_SECS,
    FileSelection,
    MagnetResolution,
    ProviderErrorCode,
    ProviderOperationError,
)
from .utils import normalize_posix_path

# Consecutive provider errors during resolution before an entry reaches a
# visible terminal failure.
RESOLUTION_ERROR_LIMIT = 10
# Consecutive pending resolutions before an entry reaches a visible terminal
# failure (roughly an hour at the default 30 s refresh).
RESOLUTION_PENDING_LIMIT = 120

_HEX_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_B32_RE = re.compile(r"^[a-z2-7]{32}$", re.IGNORECASE)


class IngestState:
    """Durable ingest entry states and their labels."""

    QUEUED = "queued"
    SUBMITTING = "submitting"
    METADATA_PENDING = "metadata_pending"
    FILES_READY = "files_ready"
    SELECTING = "selecting"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    FAILED = "failed"

    @staticmethod
    def label(state: str) -> str:
        return {
            IngestState.QUEUED: "queued",
            IngestState.SUBMITTING: "submitting",
            IngestState.METADATA_PENDING: "metadata pending",
            IngestState.FILES_READY: "files ready",
            IngestState.SELECTING: "selecting files",
            IngestState.AWAITING_CONFIRMATION: "awaiting confirmation",
            IngestState.CONFIRMED: "confirmed",
            IngestState.FAILED: "failed",
        }.get(state, state)


VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    IngestState.QUEUED: frozenset({IngestState.SUBMITTING, IngestState.FAILED}),
    IngestState.SUBMITTING: frozenset(
        {IngestState.METADATA_PENDING, IngestState.FILES_READY, IngestState.FAILED}
    ),
    IngestState.METADATA_PENDING: frozenset(
        {IngestState.FILES_READY, IngestState.FAILED}
    ),
    IngestState.FILES_READY: frozenset({IngestState.SELECTING}),
    IngestState.SELECTING: frozenset(
        {IngestState.AWAITING_CONFIRMATION, IngestState.FILES_READY}
    ),
    IngestState.AWAITING_CONFIRMATION: frozenset({IngestState.CONFIRMED}),
    IngestState.CONFIRMED: frozenset(),
    IngestState.FAILED: frozenset({IngestState.QUEUED}),
}


def can_transition(current: str, target: str) -> bool:
    """Return True when the state machine allows moving to ``target``."""
    return target == current or target in VALID_TRANSITIONS.get(current, frozenset())


def parse_btih(magnet: str) -> str | None:
    """Extract and normalize the BTIH info hash from a magnet URI.

    Accepts hex (40 chars) or base32 (32 chars) encodings and returns the
    lowercase hex hash, or None when the magnet carries no usable BTIH.
    """
    text = (magnet or "").strip()
    if not text.lower().startswith("magnet:"):
        return None
    query = text.partition("?")[2]
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key.strip().lower() != "xt":
            continue
        candidate = value.strip()
        if candidate.lower().startswith("urn:btih:"):
            candidate = candidate.rpartition(":")[2].strip()
        candidate = candidate.replace("-", "").replace("_", "")
        if _HEX_RE.fullmatch(candidate):
            return candidate.lower()
        if _B32_RE.fullmatch(candidate):
            try:
                decoded = base64.b32decode(candidate.upper())
            except (binascii.Error, ValueError):
                continue
            if len(decoded) == 20:
                return decoded.hex()
    return None


def new_entry_id() -> str:
    """Return a new unique ingest entry id."""
    return uuid.uuid4().hex


def new_batch_id() -> str:
    """Return a new unique ingest batch id."""
    return uuid.uuid4().hex


@dataclass
class IngestEntry:
    """One durable, persistent magnet ingest entry."""

    id: str
    batch_id: str
    thash: str
    magnet: str
    state: str
    created_at: float
    display_name: str | None = None
    name: str | None = None
    total_bytes: int | None = None
    accepted_provider: str | None = None
    provider_torrent_id: str | None = None
    resolution_attempts: int = 0
    resolution_errors: int = 0
    next_resolution_at: float | None = None
    error_code: str | None = None
    error_detail: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0

    def to_row(self) -> dict[str, Any]:
        """Return the entry as a persistence-ready row dict."""
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "thash": self.thash,
            "magnet": self.magnet,
            "display_name": self.display_name,
            "name": self.name,
            "total_bytes": self.total_bytes,
            "state": self.state,
            "accepted_provider": self.accepted_provider,
            "provider_torrent_id": self.provider_torrent_id,
            "resolution_attempts": self.resolution_attempts,
            "resolution_errors": self.resolution_errors,
            "next_resolution_at": self.next_resolution_at,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "files_json": json.dumps(self.files, separators=(",", ":")),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> IngestEntry:
        """Build an entry from a persisted row."""
        files = row.get("files_json")
        try:
            parsed = json.loads(files) if files else []
        except ValueError:
            parsed = []
        return cls(
            id=str(row["id"]),
            batch_id=str(row["batch_id"]),
            thash=str(row["thash"]),
            magnet=str(row["magnet"]),
            display_name=row.get("display_name"),
            name=row.get("name"),
            total_bytes=row.get("total_bytes"),
            state=str(row["state"]),
            accepted_provider=row.get("accepted_provider"),
            provider_torrent_id=row.get("provider_torrent_id"),
            resolution_attempts=int(row.get("resolution_attempts") or 0),
            resolution_errors=int(row.get("resolution_errors") or 0),
            next_resolution_at=row.get("next_resolution_at"),
            error_code=row.get("error_code"),
            error_detail=row.get("error_detail"),
            files=parsed if isinstance(parsed, list) else [],
            created_at=float(row.get("created_at") or 0.0),
            updated_at=float(row.get("updated_at") or 0.0),
        )


# ---------------------------------------------------------------------------
# Orchestration
#
# These functions drive entries through the state machine. They receive the
# owning BuzzState (typed as Any to avoid an import cycle) and rely on its
# ingest support methods, which centralize locking and persistence.
# ---------------------------------------------------------------------------


def run_batch(
    state: Any,
    entry_ids: list[str],
    provider_choice: str,
    cancel_event: threading.Event,
) -> None:
    """Process a submitted batch sequentially in input order."""
    accepted_any = False
    for entry_id in entry_ids:
        if cancel_event.is_set():
            return
        entry = state.ingest.get(entry_id)
        if entry is None or entry.state != IngestState.QUEUED:
            continue
        accepted_any = _run_entry(state, entry, provider_choice, cancel_event) or accepted_any
    state._notify_ui_change("ingest")
    if accepted_any:
        state._queue_sync_after_task("sync_ingest")


def _target_providers(
    state: Any, provider_choice: str
) -> list[tuple[str, Any]]:
    """Return the providers to try, honoring an explicit provider choice."""
    clients = state._fallback_clients()
    if provider_choice != "auto":
        return [(name, client) for name, client in clients if name == provider_choice]
    return clients


def _run_entry(
    state: Any,
    entry: IngestEntry,
    provider_choice: str,
    cancel_event: threading.Event,
) -> bool:
    """Submit one entry to providers in priority order.

    Returns True when a provider accepted the magnet.
    """
    state._update_ingest_entry(entry, state=IngestState.SUBMITTING)
    last_code: str | None = None
    last_detail: str | None = None
    for provider, client in _target_providers(state, provider_choice):
        if cancel_event.is_set():
            return False
        try:
            submission = client.submit_magnet(entry.magnet)
        except ProviderOperationError as exc:
            _record_submit_failure(state, entry, provider, exc)
            last_code = exc.code
            last_detail = exc.detail or exc.code
            if not exc.fallback_eligible:
                break
            continue
        except Exception as exc:  # defensive: untyped adapter failure
            untyped = ProviderOperationError(
                provider, "submit_magnet", ProviderErrorCode.UNKNOWN, detail=str(exc)
            )
            _record_submit_failure(state, entry, provider, untyped)
            last_code = ProviderErrorCode.UNKNOWN
            last_detail = str(exc)
            continue
        return _accept(state, entry, provider, client, submission.torrent_id, cancel_event)
    state._update_ingest_entry(
        entry,
        state=IngestState.FAILED,
        error_code=last_code or ProviderErrorCode.UNKNOWN,
        error_detail=last_detail or "no provider accepted the magnet",
    )
    record_event(
        f"ingest failed for {entry.display_name or entry.thash}: "
        f"{last_code or ProviderErrorCode.UNKNOWN}",
        level="error",
        event="ingest_failed",
        entry_id=entry.id,
        hash=entry.thash,
        code=last_code or ProviderErrorCode.UNKNOWN,
    )
    return False


def _accept(
    state: Any,
    entry: IngestEntry,
    provider: str,
    client: Any,
    torrent_id: str,
    cancel_event: threading.Event,
) -> bool:
    """Record provider acceptance and start resolution."""
    state._update_ingest_entry(
        entry,
        state=IngestState.METADATA_PENDING,
        accepted_provider=provider,
        provider_torrent_id=torrent_id,
    )
    state._record_ingest_attempt(entry.id, provider, "submit_magnet", "accepted", None, None)
    record_event(
        f"ingest accepted by {provider}: {entry.display_name or entry.thash}",
        event="ingest_accepted",
        entry_id=entry.id,
        provider=provider,
        hash=entry.thash,
    )
    if cancel_event.is_set():
        return True
    resolve_pending(state, entry, client)
    return True


def _record_submit_failure(
    state: Any, entry: IngestEntry, provider: str, exc: ProviderOperationError
) -> None:
    state._record_ingest_attempt(
        entry.id, provider, "submit_magnet", "failed", exc.code, exc.detail
    )
    record_event(
        f"ingest submission failed at {provider}: {exc.detail or exc.code}",
        level="warning",
        event="ingest_submit_failed",
        entry_id=entry.id,
        provider=provider,
        hash=entry.thash,
        code=exc.code,
    )


def _resolution_to_files(resolution: MagnetResolution) -> list[dict[str, Any]]:
    """Convert normalized provider files into ingest draft file dicts."""
    return [
        {
            "id": file.id,
            "path": normalize_posix_path(file.path),
            "bytes": file.bytes,
            "selected": 1 if file.selected else 0,
            "stream_ref": file.stream_ref,
        }
        for file in resolution.files
        if file.path
    ]


def resolve_pending(state: Any, entry: IngestEntry, client: Any) -> None:
    """Run one resolution pass for an accepted entry.

    Files ready moves the entry to files_ready and enriches its archive
    record; pending metadata schedules the next refresh; bounded consecutive
    provider errors fail the entry visibly.
    """
    if entry.provider_torrent_id is None:
        return
    provider = entry.accepted_provider or ""
    try:
        resolution = client.resolve_magnet(entry.provider_torrent_id)
    except ProviderOperationError as exc:
        entry.resolution_errors += 1
        state._record_ingest_attempt(
            entry.id, provider, "resolve_magnet", "failed", exc.code, exc.detail
        )
        record_event(
            f"ingest resolution failed at {provider}: {exc.detail or exc.code}",
            level="warning",
            event="ingest_resolve_failed",
            entry_id=entry.id,
            provider=provider,
            hash=entry.thash,
            code=exc.code,
        )
        if entry.resolution_errors >= RESOLUTION_ERROR_LIMIT:
            state._update_ingest_entry(
                entry,
                state=IngestState.FAILED,
                error_code=exc.code,
                error_detail=exc.detail or exc.code,
            )
            record_event(
                f"ingest resolution gave up for "
                f"{entry.display_name or entry.thash}",
                level="error",
                event="ingest_resolution_gave_up",
                entry_id=entry.id,
                hash=entry.thash,
                code=exc.code,
            )
        else:
            state._update_ingest_entry(
                entry,
                state=IngestState.METADATA_PENDING,
                resolution_errors=entry.resolution_errors,
                next_resolution_at=time.time()
                + (exc.retry_after or DEFAULT_RESOLUTION_REFRESH_SECS),
            )
        return
    entry.resolution_errors = 0
    if resolution.status == "files_ready":
        entry.files = _resolution_to_files(resolution)
        entry.name = resolution.name or entry.name
        entry.total_bytes = resolution.bytes or entry.total_bytes
        state._update_ingest_entry(
            entry,
            state=IngestState.FILES_READY,
            files=entry.files,
            name=entry.name,
            total_bytes=entry.total_bytes,
            resolution_errors=0,
            next_resolution_at=None,
            error_code=None,
            error_detail=None,
        )
        state._enrich_archive_from_entry(entry)
        state._seed_ingest_file_selection(entry)
        record_event(
            f"ingest files ready for {entry.display_name or entry.thash} "
            f"({len(entry.files)} file(s))",
            event="ingest_files_ready",
            entry_id=entry.id,
            provider=provider,
            hash=entry.thash,
        )
        return
    entry.resolution_attempts += 1
    state._record_ingest_attempt(entry.id, provider, "resolve_magnet", "pending", None, None)
    if entry.resolution_attempts >= RESOLUTION_PENDING_LIMIT:
        state._update_ingest_entry(
            entry,
            state=IngestState.FAILED,
            error_code=ProviderErrorCode.UPSTREAM_TIMEOUT,
            error_detail=(
                f"metadata did not resolve after {entry.resolution_attempts} "
                f"refreshes at {provider}"
            ),
        )
        record_event(
            f"ingest metadata never resolved for "
            f"{entry.display_name or entry.thash}",
            level="error",
            event="ingest_resolution_gave_up",
            entry_id=entry.id,
            provider=provider,
            hash=entry.thash,
            code=ProviderErrorCode.UPSTREAM_TIMEOUT,
        )
        return
    state._update_ingest_entry(
        entry,
        state=IngestState.METADATA_PENDING,
        resolution_attempts=entry.resolution_attempts,
        resolution_errors=0,
        next_resolution_at=time.time()
        + (resolution.next_refresh_secs or DEFAULT_RESOLUTION_REFRESH_SECS),
    )


def reconcile_due(state: Any) -> None:
    """Resolve accepted entries whose refresh is due.

    Called from provider sync so pending metadata advances without browser
    polling. Provider I/O happens here, outside the sync commit lock.
    """
    now = time.time()
    due: list[tuple[str, IngestEntry]] = []
    with state.lock:
        for entry in state.ingest.values():
            if entry.state != IngestState.METADATA_PENDING:
                continue
            if entry.accepted_provider is None:
                continue
            if entry.next_resolution_at is None or entry.next_resolution_at <= now:
                due.append((entry.accepted_provider, entry))
    for provider, entry in due:
        client = state.clients.get(provider)
        if client is None:
            continue
        resolve_pending(state, entry, client)


def _entry_label(entry: IngestEntry) -> str:
    """Return a short operator-facing label for one ingest entry."""
    return entry.display_name or entry.name or entry.thash


def confirm_selections(
    state: Any, selections: dict[str, list[str]], cancel_event: threading.Event
) -> None:
    """Apply ready selections grouped by provider via the batch primitive.

    Adapters return one result per input selection, in input order; partial
    failures revert only the affected entries to files_ready with a visible
    error. Every outcome is written to the task log with the entry's label,
    and a rejected (or unconfirmable) selection raises so the job fails
    loudly instead of completing without side effects.
    """
    skipped: list[str] = []
    groups: dict[str, tuple[list[FileSelection], list[IngestEntry]]] = {}
    for entry_id, file_ids in selections.items():
        if cancel_event.is_set():
            raise RuntimeError("cancelled")
        reason = _prepare_selection(state, entry_id, file_ids, groups)
        if reason is not None:
            skipped.append(reason)
    for reason in skipped:
        record_event(
            f"skipping confirmation for {reason}",
            level="warning",
            event="ingest_selection_skipped",
        )
    if skipped and not groups:
        state._notify_ui_change("ingest")
        raise RuntimeError(f"no file selection to confirm: {', '.join(skipped)}")
    failures: list[str] = []
    for provider, (selection_list, entries) in groups.items():
        if cancel_event.is_set():
            raise RuntimeError("cancelled")
        record_event(
            f"applying selection to {len(entries)} entr(ies) on {provider}",
            event="ingest_selection_applying",
            provider=provider,
        )
        _apply_selection_group(state, provider, selection_list, entries, failures)
    state._notify_ui_change("ingest")
    if failures:
        raise RuntimeError("file selection rejected: " + "; ".join(failures))
    if groups:
        state._queue_sync_after_task("sync_ingest_selections")


def _prepare_selection(
    state: Any,
    entry_id: str,
    file_ids: list[str],
    groups: dict[str, tuple[list[FileSelection], list[IngestEntry]]],
) -> str | None:
    """Stage one files_ready entry into its provider group.

    Returns a human-readable skip reason when the entry cannot be confirmed;
    otherwise updates the entry's draft and state and returns None.
    """
    entry = state.ingest.get(entry_id)
    if entry is None:
        return f"{entry_id} (no such entry)"
    if entry.state != IngestState.FILES_READY:
        return f"{_entry_label(entry)} (state {entry.state})"
    provider = entry.accepted_provider
    if not provider or entry.provider_torrent_id is None:
        return f"{_entry_label(entry)} (no provider link)"
    record_event(
        f"confirming file selection for {_entry_label(entry)}: "
        f"{len(file_ids)} file(s) on {provider} torrent "
        f"{entry.provider_torrent_id}",
        event="ingest_selection_confirming",
        entry_id=entry.id,
        provider=provider,
        hash=entry.thash,
    )
    requested = {str(file_id) for file_id in file_ids}
    ids = requested
    if provider == "real_debrid":
        # RD selection is additive-only: request the union with what the
        # provider already has selected so its state never loses files.
        already = {
            str(f.get("id"))
            for f in entry.files
            if f.get("selected") and f.get("id")
        }
        ids = requested | already
    state._persist_ingest_selection_draft(entry, requested)
    state._update_ingest_entry(entry, state=IngestState.SELECTING)
    selection_list, entries = groups.setdefault(provider, ([], []))
    selection_list.append(
        FileSelection(entry.provider_torrent_id, tuple(sorted(ids)))
    )
    entries.append(entry)
    return None


def _apply_selection_group(
    state: Any,
    provider: str,
    selection_list: list[FileSelection],
    entries: list[IngestEntry],
    failures: list[str],
) -> None:
    """Apply one provider group's selections and record per-entry outcomes.

    Adapters return one result per input selection, in input order; partial
    failures revert only the affected entries to files_ready with a visible
    error, append a task-facing failure reason, and log the outcome.
    """
    client = state.clients.get(provider)
    if client is None:
        for entry in entries:
            failures.append(
                f"{_entry_label(entry)} ({provider}): provider not configured"
            )
        return
    try:
        results = client.apply_file_selections(selection_list)
    except ProviderOperationError as exc:
        for entry in entries:
            state._record_ingest_attempt(
                entry.id,
                provider,
                "apply_file_selections",
                "failed",
                exc.code,
                exc.detail,
            )
            state._update_ingest_entry(
                entry,
                state=IngestState.FILES_READY,
                error_code=exc.code,
                error_detail=exc.detail or exc.code,
            )
            failures.append(
                f"{_entry_label(entry)} ({provider}): {exc.code} "
                f"{exc.detail or ''}".strip()
            )
            record_event(
                f"file selection failed for {_entry_label(entry)} "
                f"({provider}): {exc.code} {exc.detail or ''}".strip(),
                level="error",
                event="ingest_selection_failed",
                entry_id=entry.id,
                provider=provider,
                hash=entry.thash,
                code=exc.code,
            )
        return
    for entry, result in zip(entries, results, strict=False):
        if result.ok:
            state._record_ingest_attempt(
                entry.id, provider, "apply_file_selections", "accepted", None, None
            )
            state._update_ingest_entry(entry, state=IngestState.AWAITING_CONFIRMATION)
            record_event(
                f"selection accepted for {_entry_label(entry)} ({provider}); "
                "awaiting cache confirmation",
                event="ingest_selection_accepted",
                entry_id=entry.id,
                provider=provider,
                hash=entry.thash,
            )
            continue
        error = result.error
        code = error.code if error else ProviderErrorCode.UNKNOWN
        detail = error.detail if error else "provider rejected the selection"
        state._record_ingest_attempt(
            entry.id, provider, "apply_file_selections", "failed", code, detail
        )
        state._update_ingest_entry(
            entry,
            state=IngestState.FILES_READY,
            error_code=code,
            error_detail=detail,
        )
        failures.append(f"{_entry_label(entry)} ({provider}): {code} {detail}".strip())
        record_event(
            f"file selection rejected for {_entry_label(entry)} ({provider}): "
            f"{code} {detail}".strip(),
            level="error",
            event="ingest_selection_rejected",
            entry_id=entry.id,
            provider=provider,
            hash=entry.thash,
            code=code,
        )


def finalize_confirmations(state: Any) -> bool:
    """Confirm awaiting entries and remove files-ready orphans at sync.

    Called from the sync commit while the state lock is held. Awaiting
    entries become confirmed once their torrent appears in the synced cache
    and are pruned on the following pass; their history now lives in the
    cache, the archive, and the event log. A files-ready entry whose exact
    upstream provider/torrent key is absent from the synced cache is removed
    here so the durable ingest row does not outlive the deleted torrent.
    Archive and selection data are left untouched. Returns True when any
    entry changed.
    """
    changed = False
    with state.lock:
        for entry in list(state.ingest.values()):
            if entry.state == IngestState.CONFIRMED:
                state._remove_ingest_entry_locked(entry.id)
                changed = True
                continue
            if entry.state == IngestState.FILES_READY:
                if (
                    entry.accepted_provider is None
                    or entry.provider_torrent_id is None
                ):
                    continue
                cache_key = state._cache_key(
                    entry.accepted_provider, entry.provider_torrent_id
                )
                if cache_key in state.cache:
                    continue
                record_event(
                    f"ingest upstream torrent disappeared for "
                    f"{entry.display_name or entry.thash} "
                    f"on {entry.accepted_provider}",
                    event="ingest_upstream_removed",
                    entry_id=entry.id,
                    provider=entry.accepted_provider,
                    hash=entry.thash,
                )
                state._remove_ingest_entry_locked(entry.id)
                changed = True
                continue
            if entry.state != IngestState.AWAITING_CONFIRMATION:
                continue
            if entry.accepted_provider is None or entry.provider_torrent_id is None:
                continue
            cache_key = state._cache_key(
                entry.accepted_provider, entry.provider_torrent_id
            )
            cached = state.cache.get(cache_key)
            if not (isinstance(cached, dict) and isinstance(cached.get("info"), dict)):
                continue
            cached["magnet"] = entry.magnet
            state._save_cache_entry(cache_key, cached)
            entry.updated_at = time.time()
            state._persist_ingest_entry_locked(entry, state=IngestState.CONFIRMED)
            state._enrich_archive_from_entry(entry)
            record_event(
                f"ingest confirmed for {entry.display_name or entry.thash} "
                f"on {entry.accepted_provider}",
                event="ingest_confirmed",
                entry_id=entry.id,
                provider=entry.accepted_provider,
                hash=entry.thash,
            )
            changed = True
    if changed:
        state._notify_ui_change("ingest")
    return changed
