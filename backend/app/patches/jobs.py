"""Background candidate jobs: one worker, capacity gate, cancellation.

At most ``config.PATCH_MAX_CONCURRENT`` generations execute at once;
further starts are rejected (HTTP 429) rather than queued silently.
Each job gets a cancellation event the runner polls around the model
call. If the backend restarts mid-run, ``reconcile`` marks the orphaned
record on the next status read instead of leaving it running forever.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional

from .. import config
from . import store as patch_store
from .models import Candidate

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="patches"
)
_capacity: Optional[threading.Semaphore] = None
_capacity_lock = threading.Lock()
_live: dict[str, dict] = {}
_live_lock = threading.Lock()


def _semaphore() -> threading.Semaphore:
    global _capacity
    with _capacity_lock:
        if _capacity is None:
            _capacity = threading.Semaphore(max(1, config.PATCH_MAX_CONCURRENT))
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


def try_start(candidate_id: str, fn: Callable[[threading.Event], None]) -> bool:
    """Reserve capacity and submit the job; False when at capacity."""
    if not _semaphore().acquire(blocking=False):
        return False
    cancel_event = threading.Event()
    with _live_lock:
        _live[candidate_id] = {"event": cancel_event, "future": None}

    def wrapper() -> None:
        try:
            fn(cancel_event)
        finally:
            with _live_lock:
                _live.pop(candidate_id, None)
            _semaphore().release()

    future = _executor.submit(wrapper)
    with _live_lock:
        if candidate_id in _live:
            _live[candidate_id]["future"] = future
    return True


def cancel(candidate_id: str) -> bool:
    """Request cancellation; True when a live job was signalled."""
    with _live_lock:
        job = _live.get(candidate_id)
        if job is None:
            return False
        job["event"].set()
        return True


def is_live(candidate_id: str) -> bool:
    with _live_lock:
        return candidate_id in _live


def reconcile(candidate: Candidate) -> Candidate:
    """Mark orphaned active candidates (e.g. after a backend restart)."""
    if candidate.state in ("queued", "generating") and not is_live(candidate.id):
        def mark_blocked(item: Candidate) -> None:
            item.state = "blocked"
            item.detail = "backend restarted or worker lost; run did not complete"

        updated = patch_store.update(candidate.id, mark_blocked)
        if updated is not None:
            patch_store.add_event(candidate.id, "lifecycle", updated.detail)
            refreshed = patch_store.get(candidate.id)
            return refreshed if refreshed is not None else updated
    return candidate
