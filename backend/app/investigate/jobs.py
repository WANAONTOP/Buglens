"""Background investigation jobs: one worker, capacity gate, cancellation.

At most ``config.INVESTIGATE_MAX_CONCURRENT`` investigations execute at
once; further starts are rejected (HTTP 429) rather than queued silently.
Each job gets a cancellation event the runner polls between model calls.
If the backend restarts mid-run, ``reconcile`` marks the orphaned record
on the next status read instead of leaving it running forever.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional

from .. import config
from . import store as inv_store
from .models import Investigation

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="investigate"
)
_capacity: Optional[threading.Semaphore] = None
_capacity_lock = threading.Lock()
_live: dict[str, dict] = {}
_live_lock = threading.Lock()


def _semaphore() -> threading.Semaphore:
    global _capacity
    with _capacity_lock:
        if _capacity is None:
            _capacity = threading.Semaphore(max(1, config.INVESTIGATE_MAX_CONCURRENT))
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


def try_start(inv_id: str, fn: Callable[[threading.Event], None]) -> bool:
    """Reserve capacity and submit the job; False when at capacity."""
    if not _semaphore().acquire(blocking=False):
        return False
    cancel_event = threading.Event()
    with _live_lock:
        _live[inv_id] = {"event": cancel_event, "future": None}

    def wrapper() -> None:
        try:
            fn(cancel_event)
        finally:
            with _live_lock:
                _live.pop(inv_id, None)
            _semaphore().release()

    future = _executor.submit(wrapper)
    with _live_lock:
        if inv_id in _live:
            _live[inv_id]["future"] = future
    return True


def cancel(inv_id: str) -> bool:
    """Request cancellation; True when a live job was signalled."""
    with _live_lock:
        job = _live.get(inv_id)
        if job is None:
            return False
        job["event"].set()
        return True


def is_live(inv_id: str) -> bool:
    with _live_lock:
        return inv_id in _live


def reconcile(inv: Investigation) -> Investigation:
    """Mark orphaned active investigations (e.g. after a backend restart)."""
    if inv.state in ("queued", "running") and not is_live(inv.id):
        def mark_blocked(candidate: Investigation) -> None:
            candidate.state = "blocked"
            candidate.detail = "backend restarted or worker lost; run did not complete"

        updated = inv_store.update(inv.id, mark_blocked)
        if updated is not None:
            inv_store.add_event(inv.id, "lifecycle", updated.detail)
            refreshed = inv_store.get(inv.id)
            return refreshed if refreshed is not None else updated
    return inv
