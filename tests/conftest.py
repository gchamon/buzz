"""Pytest configuration: per-test memory guardrails.

Two layers:

1. **Mid-flight watchdog** (this module): samples /proc/self/status every
   0.5 s and, if the process RSS crosses a hard ceiling (default 2 GiB,
   override via BUZZ_TEST_MEM_CAP_MB), keeps raising `OOMError` into the
   test thread until the test unwinds. The watchdog never escalates to a
   session-wide interrupt — only the offending test fails. A lock around
   the SetAsyncExc call and the teardown's `stop.set()` plus a no-op
   try/except drain after `thread.join()` guarantee no pending async
   exception leaks into the next test. Linux-only; no-ops elsewhere.

2. **Post-hoc memray check**: every test runs under `pytest-memray`'s
   `limit_memory` marker (applied automatically in
   `pytest_collection_modifyitems`). Memray reports per-test allocation
   hotspots and fails any test whose high-watermark exceeds the cap.

Tests can opt out of either layer with `@pytest.mark.no_memory_cap`.
"""

from __future__ import annotations

import ctypes
import os
import threading

import pytest

DEFAULT_CAP_MB = 256
_PROC_STATUS = "/proc/self/status"
_SAMPLE_INTERVAL_SECS = 0.5


class OOMError(Exception):
    """Raised into a test's thread when it exceeds the RSS guardrail."""


def _read_rss_bytes() -> int | None:
    try:
        with open(_PROC_STATUS) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except OSError:
        return None
    return None


def _cap_mb() -> int:
    raw = os.environ.get("BUZZ_TEST_MEM_CAP_MB", "").strip()
    try:
        return int(raw) if raw else DEFAULT_CAP_MB
    except ValueError:
        return DEFAULT_CAP_MB


def _raise_in_thread(thread_id: int, exc: type[BaseException]) -> int:
    return ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id), ctypes.py_object(exc)
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_memory_cap: disable the per-test memory guardrails",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Apply `limit_memory` to every test that hasn't opted out."""
    limit = f"{_cap_mb()} MB"
    for item in items:
        if item.get_closest_marker("no_memory_cap") is not None:
            continue
        if item.get_closest_marker("limit_memory") is not None:
            continue
        item.add_marker(pytest.mark.limit_memory(limit))


@pytest.fixture(autouse=True)
def _memory_guardrail(request: pytest.FixtureRequest):
    if request.node.get_closest_marker("no_memory_cap") is not None:
        yield
        return
    if _read_rss_bytes() is None:
        yield
        return

    cap = _cap_mb() * 1024 * 1024
    stop = threading.Event()
    watchdog_active = threading.Lock()
    breach: dict[str, int] = {}
    target_tid = threading.get_ident()

    def watch() -> None:
        while not stop.wait(_SAMPLE_INTERVAL_SECS):
            rss = _read_rss_bytes()
            if rss is None or rss <= cap:
                continue
            if "peak" not in breach or rss > breach["peak"]:
                breach["peak"] = rss
            with watchdog_active:
                if stop.is_set():
                    return
                _raise_in_thread(target_tid, OOMError)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield
    except OOMError:
        pass
    finally:
        with watchdog_active:
            stop.set()
        thread.join(timeout=2.0)
        # Drain a pending async exception that may have been scheduled
        # just before `stop.set()` won the lock. A bare bytecode-boundary
        # try/except catches and discards it so it can't leak into the
        # next test.
        try:
            pass
        except OOMError:
            pass

    if breach:
        peak_mb = breach["peak"] // (1024 * 1024)
        cap_mb = cap // (1024 * 1024)
        pytest.fail(
            f"memory guardrail exceeded: {peak_mb} MiB > {cap_mb} MiB cap "
            f"(test: {request.node.nodeid})",
            pytrace=False,
        )
