"""Background reproduction jobs: one worker pool, capacity gate, cancellation.

At most ``config.REPRO_MAX_CONCURRENT`` runs execute at once; further
starts are rejected (HTTP 429) rather than queued silently. Each job gets
a cancellation event the runner polls between phases. If the backend
restarts mid-run, ``reconcile`` marks the orphaned record cancelled on the
next status read instead of leaving it running forever.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional

from .. import config
from . import store as repro_store
from .models import ReproRun

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="repro"
)
_capacity: Optional[threading.Semaphore] = None
_capacity_lock = threading.Lock()
_live: dict[str, dict] = {}
_live_lock = threading.Lock()


def _semaphore() -> threading.Semaphore:
    global _capacity
    with _capacity_lock:
        if _capacity is None:
            _capacity = threading.Semaphore(max(1, config.REPRO_MAX_CONCURRENT))
        return _capacity


def reset_for_tests() -> None:
    """Drop the cached semaphore (tests that monkeypatch the limit)."""
    global _capacity
    with _capacity_lock:
        _capacity = None


def has_capacity() -> bool:
    """Advisory capacity probe (the try_start gate stays authoritative)."""
    if _semaphore().acquire(blocking=False):
        _semaphore().release()
        return True
    return False


def try_start(run_id: str, fn: Callable[[threading.Event], None]) -> bool:
    """Reserve capacity and submit the job; False when at capacity."""
    if not _semaphore().acquire(blocking=False):
        return False
    cancel_event = threading.Event()
    with _live_lock:
        _live[run_id] = {"event": cancel_event, "future": None}

    def wrapper() -> None:
        try:
            fn(cancel_event)
        finally:
            with _live_lock:
                _live.pop(run_id, None)
            _semaphore().release()

    future = _executor.submit(wrapper)
    with _live_lock:
        if run_id in _live:
            _live[run_id]["future"] = future
    return True


def cancel(run_id: str) -> bool:
    """Request cancellation; True when a live job was signalled."""
    with _live_lock:
        job = _live.get(run_id)
        if job is None:
            return False
        job["event"].set()
        return True


def is_live(run_id: str) -> bool:
    with _live_lock:
        return run_id in _live


def reconcile(run: ReproRun) -> ReproRun:
    """Mark orphaned active runs cancelled (e.g. after a backend restart)."""
    if run.state in ("queued", "running") and not is_live(run.id):
        def mark_cancelled(candidate: ReproRun) -> None:
            candidate.state = "cancelled"
            candidate.detail = "backend restarted or worker lost; run did not complete"

        updated = repro_store.update_run(run.id, mark_cancelled)
        if updated is not None:
            repro_store.add_event(run.id, "lifecycle", updated.detail)
            refreshed = repro_store.get_run(run.id)
            return refreshed if refreshed is not None else updated
    return run
