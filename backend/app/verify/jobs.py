"""Background verification jobs: one worker, capacity gate, cancellation.

At most ``config.VERIFY_MAX_CONCURRENT`` verifications execute at once;
further starts are rejected (HTTP 429) rather than queued silently.
Each job gets a cancellation event the runner polls between sandbox
phases. If the backend restarts mid-run, ``reconcile`` marks the
orphaned record on the next status read — never verified.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional

from .. import config
from . import store as verify_store
from .models import Verification

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="verify"
)
_capacity: Optional[threading.Semaphore] = None
_capacity_lock = threading.Lock()
_live: dict[str, dict] = {}
_live_lock = threading.Lock()


def _semaphore() -> threading.Semaphore:
    global _capacity
    with _capacity_lock:
        if _capacity is None:
            _capacity = threading.Semaphore(max(1, config.VERIFY_MAX_CONCURRENT))
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


def try_start(verification_id: str, fn: Callable[[threading.Event], None]) -> bool:
    """Reserve capacity and submit the job; False when at capacity."""
    if not _semaphore().acquire(blocking=False):
        return False
    cancel_event = threading.Event()
    with _live_lock:
        _live[verification_id] = {"event": cancel_event, "future": None}

    def wrapper() -> None:
        try:
            fn(cancel_event)
        finally:
            with _live_lock:
                _live.pop(verification_id, None)
            _semaphore().release()

    future = _executor.submit(wrapper)
    with _live_lock:
        if verification_id in _live:
            _live[verification_id]["future"] = future
    return True


def cancel(verification_id: str) -> bool:
    """Request cancellation; True when a live job was signalled."""
    with _live_lock:
        job = _live.get(verification_id)
        if job is None:
            return False
        job["event"].set()
        return True


def is_live(verification_id: str) -> bool:
    with _live_lock:
        return verification_id in _live


def reconcile(verification: Verification) -> Verification:
    """Mark orphaned active verifications (e.g. after a backend restart)."""
    if verification.state in ("queued", "running") and not is_live(verification.id):
        def mark_blocked(item: Verification) -> None:
            item.state = "blocked"
            item.outcome = "BLOCKED"
            item.detail = "backend restarted or worker lost; run did not complete"

        updated = verify_store.update(verification.id, mark_blocked)
        if updated is not None:
            verify_store.add_event(verification.id, "lifecycle", updated.detail)
            refreshed = verify_store.get(verification.id)
            return refreshed if refreshed is not None else updated
    return verification
