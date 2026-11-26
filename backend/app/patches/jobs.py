"""Background candidate jobs: one worker, capacity gate, cancellation.

At most ``config.PATCH_MAX_CONCURRENT`` generations execute at once;
further starts are rejected (HTTP 429) rather than queued silently.
Each job gets a cancellation event the runner polls around the model
call. If the backend restarts mid-run, ``reconcile`` marks the orphaned
record on the next status read instead of leaving it running forever.

Deploy mode (``BUGLENS_JOBS=modal``) spawns a Modal worker function per
candidate instead of a thread; capacity is enforced by counting active
records, cancellation terminates the remote call, and stale records
(no persisted progress within budget) reconcile as lost workers.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional

from .. import config, deploy
from . import store as patch_store
from .models import Candidate, CandidateEvent

REMOTE_WORKER = "run_candidate_worker"
RECORD_SUBDIR = "candidates"
ACTIVE_STATES = ("queued", "generating")

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="patches"
)
_capacity: Optional[threading.Semaphore] = None
_capacity_lock = threading.Lock()
_live: dict[str, dict] = {}
_live_lock = threading.Lock()
# Serializes remote spawn check-and-act (the API is one container, so
# this lock makes duplicate submissions deterministic: exactly one
# worker per record, never two).
_spawn_lock = threading.Lock()


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
    if deploy.use_remote_workers():
        return (
            deploy.count_active(RECORD_SUBDIR, ACTIVE_STATES)
            < max(1, config.PATCH_MAX_CONCURRENT)
        )
    if _semaphore().acquire(blocking=False):
        _semaphore().release()
        return True
    return False


def _try_start_remote(candidate_id: str) -> bool:
    """Spawn the deployed worker exactly once; False when at capacity.

    A repeat submission for an already-dispatched record returns True
    without spawning again. Raises ``DeployError`` when the spawn
    itself fails (routes map it to 502): a spawn failure is not a
    capacity signal.
    """
    with _spawn_lock:
        deploy.sync_before_read()
        candidate = patch_store.get(candidate_id)
        if candidate is None:
            return False
        if candidate.worker_call_id or candidate.state != "queued":
            return True
        if (
            deploy.count_active(RECORD_SUBDIR, ACTIVE_STATES, exclude=candidate_id)
            >= max(1, config.PATCH_MAX_CONCURRENT)
        ):
            return False
        call_id = deploy.spawn_worker(REMOTE_WORKER, candidate_id)

        def mark_spawned(item: Candidate) -> None:
            item.worker_call_id = call_id

        marked = patch_store.update(candidate_id, mark_spawned, expect_states=("queued",))
        if marked is None:  # defensive: the worker waits for this mark,
            deploy.cancel_call(call_id)  # so it cannot have claimed first
            return False
        return True


def try_start(candidate_id: str, fn: Callable[[threading.Event], None]) -> bool:
    """Reserve capacity and submit the job; False when at capacity.

    Deploy mode ignores ``fn`` and spawns the remote worker instead.
    """
    if deploy.use_remote_workers():
        return _try_start_remote(candidate_id)
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


def _cancel_remote(candidate_id: str) -> bool:
    """Kill the deployed worker, then mark the record blocked.

    The kill is confirmed (briefly) before the mark, and the mark
    expects an active record, so a completion that landed first is
    never overwritten. Residual risk is only a worker that survives
    container termination and writes after the mark.
    """
    candidate = patch_store.get(candidate_id)
    if candidate is None or candidate.state not in ACTIVE_STATES:
        return False
    if candidate.worker_call_id:
        deploy.cancel_call(candidate.worker_call_id)
        deploy.wait_for_call_end(candidate.worker_call_id)
    deploy.sync_before_read()

    def mark_cancelled(item: Candidate) -> None:
        item.state = "blocked"
        item.detail = "cancelled by user; remote worker terminated"
        item.events.append(
            CandidateEvent(
                at=patch_store.utcnow(),
                phase="lifecycle",
                message="cancellation requested",
            )
        )

    return (
        patch_store.update(candidate_id, mark_cancelled, expect_states=ACTIVE_STATES)
        is not None
    )


def cancel(candidate_id: str) -> bool:
    """Request cancellation; True when a live job was signalled."""
    if deploy.use_remote_workers():
        return _cancel_remote(candidate_id)
    with _live_lock:
        job = _live.get(candidate_id)
        if job is None:
            return False
        job["event"].set()
    patch_store.add_event(candidate_id, "lifecycle", "cancellation requested")
    return True


def is_live(candidate_id: str) -> bool:
    with _live_lock:
        return candidate_id in _live


def reconcile(candidate: Candidate) -> Candidate:
    """Mark orphaned active candidates (e.g. after a backend restart).

    Deploy mode marks a stale record lost only when its worker call is
    confirmed finished (or was never recorded): a quiet-but-alive
    worker, or one whose liveness cannot be determined, is left alone.
    """
    if candidate.state in ACTIVE_STATES:
        if deploy.is_modal():
            if not deploy.is_stale(candidate.updated_at):
                return candidate
            record = patch_store.get(candidate.id)
            if record is None or record.state not in ACTIVE_STATES:
                return record if record is not None else candidate
            if record.worker_call_id:
                try:
                    status = deploy.call_status(record.worker_call_id)
                except deploy.DeployError:
                    return record
                if status != "finished":
                    return record

            def mark_lost(item: Candidate) -> None:
                item.state = "blocked"
                item.detail = (
                    "remote worker lost (no persisted progress within budget); "
                    "run did not complete"
                )
                item.events.append(
                    CandidateEvent(
                        at=patch_store.utcnow(),
                        phase="lifecycle",
                        message=item.detail,
                    )
                )

            updated = patch_store.update(candidate.id, mark_lost, expect_states=ACTIVE_STATES)
            if updated is not None:
                return updated
            refreshed = patch_store.get(candidate.id)
            return refreshed if refreshed is not None else record
        if not is_live(candidate.id):
            def mark_blocked(item: Candidate) -> None:
                item.state = "blocked"
                item.detail = "backend restarted or worker lost; run did not complete"
                item.events.append(
                    CandidateEvent(
                        at=patch_store.utcnow(),
                        phase="lifecycle",
                        message=item.detail,
                    )
                )

            updated = patch_store.update(candidate.id, mark_blocked, expect_states=ACTIVE_STATES)
            if updated is not None:
                return updated
            refreshed = patch_store.get(candidate.id)
            return refreshed if refreshed is not None else candidate
    return candidate
