"""Background verification jobs: one worker, capacity gate, cancellation.

At most ``config.VERIFY_MAX_CONCURRENT`` verifications execute at once;
further starts are rejected (HTTP 429) rather than queued silently.
Each job gets a cancellation event the runner polls between sandbox
phases. If the backend restarts mid-run, ``reconcile`` marks the
orphaned record on the next status read — never verified.

Deploy mode (``BUGLENS_JOBS=modal``) spawns a Modal worker function per
verification instead of a thread; capacity is enforced by counting
active records, cancellation terminates the remote call, and stale
records (no persisted progress within budget) reconcile as lost
workers.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional

from .. import config, deploy
from . import store as verify_store
from .models import Verification, VerificationEvent

REMOTE_WORKER = "run_verify_worker"
RECORD_SUBDIR = "verifications"
ACTIVE_STATES = ("queued", "running")

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="verify"
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
            _capacity = threading.Semaphore(max(1, config.VERIFY_MAX_CONCURRENT))
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
            < max(1, config.VERIFY_MAX_CONCURRENT)
        )
    if _semaphore().acquire(blocking=False):
        _semaphore().release()
        return True
    return False


def _try_start_remote(verification_id: str) -> bool:
    """Spawn the deployed worker exactly once; False when at capacity.

    A repeat submission for an already-dispatched record returns True
    without spawning again. Raises ``DeployError`` when the spawn
    itself fails (routes map it to 502): a spawn failure is not a
    capacity signal.
    """
    with _spawn_lock:
        deploy.sync_before_read()
        verification = verify_store.get(verification_id)
        if verification is None:
            return False
        if verification.worker_call_id or verification.state != "queued":
            return True
        if (
            deploy.count_active(RECORD_SUBDIR, ACTIVE_STATES, exclude=verification_id)
            >= max(1, config.VERIFY_MAX_CONCURRENT)
        ):
            return False
        call_id = deploy.spawn_worker(REMOTE_WORKER, verification_id)

        def mark_spawned(item: Verification) -> None:
            item.worker_call_id = call_id

        marked = verify_store.update(
            verification_id, mark_spawned, expect_states=("queued",)
        )
        if marked is None:  # defensive: the worker waits for this mark,
            deploy.cancel_call(call_id)  # so it cannot have claimed first
            return False
        return True


def try_start(verification_id: str, fn: Callable[[threading.Event], None]) -> bool:
    """Reserve capacity and submit the job; False when at capacity.

    Deploy mode ignores ``fn`` and spawns the remote worker instead.
    """
    if deploy.use_remote_workers():
        return _try_start_remote(verification_id)
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


def _cancel_remote(verification_id: str) -> bool:
    """Kill the deployed worker, then mark the record blocked.

    The kill is confirmed (briefly) before the mark, and the mark
    expects an active record, so a completion that landed first is
    never overwritten. Residual risk is only a worker that survives
    container termination and writes after the mark.
    """
    verification = verify_store.get(verification_id)
    if verification is None or verification.state not in ACTIVE_STATES:
        return False
    if verification.worker_call_id:
        deploy.cancel_call(verification.worker_call_id)
        deploy.wait_for_call_end(verification.worker_call_id)
    deploy.sync_before_read()

    def mark_cancelled(item: Verification) -> None:
        item.state = "blocked"
        item.outcome = "BLOCKED"
        item.detail = "cancelled by user; remote worker terminated"
        item.events.append(
            VerificationEvent(
                at=verify_store.utcnow(),
                phase="lifecycle",
                message="cancellation requested",
            )
        )

    return (
        verify_store.update(verification_id, mark_cancelled, expect_states=ACTIVE_STATES)
        is not None
    )


def cancel(verification_id: str) -> bool:
    """Request cancellation; True when a live job was signalled."""
    if deploy.use_remote_workers():
        return _cancel_remote(verification_id)
    with _live_lock:
        job = _live.get(verification_id)
        if job is None:
            return False
        job["event"].set()
    verify_store.add_event(verification_id, "lifecycle", "cancellation requested")
    return True


def is_live(verification_id: str) -> bool:
    with _live_lock:
        return verification_id in _live


def reconcile(verification: Verification) -> Verification:
    """Mark orphaned active verifications (e.g. after a backend restart).

    Deploy mode marks a stale record lost only when its worker call is
    confirmed finished (or was never recorded): a quiet-but-alive
    worker, or one whose liveness cannot be determined, is left alone.
    Never verified.
    """
    if verification.state in ACTIVE_STATES:
        if deploy.is_modal():
            if not deploy.is_stale(verification.updated_at):
                return verification
            record = verify_store.get(verification.id)
            if record is None or record.state not in ACTIVE_STATES:
                return record if record is not None else verification
            if record.worker_call_id:
                try:
                    status = deploy.call_status(record.worker_call_id)
                except deploy.DeployError:
                    return record
                if status != "finished":
                    return record

            def mark_lost(item: Verification) -> None:
                item.state = "blocked"
                item.outcome = "BLOCKED"
                item.detail = (
                    "remote worker lost (no persisted progress within budget); "
                    "run did not complete"
                )
                item.events.append(
                    VerificationEvent(
                        at=verify_store.utcnow(),
                        phase="lifecycle",
                        message=item.detail,
                    )
                )

            updated = verify_store.update(
                verification.id, mark_lost, expect_states=ACTIVE_STATES
            )
            if updated is not None:
                return updated
            refreshed = verify_store.get(verification.id)
            return refreshed if refreshed is not None else record
        if not is_live(verification.id):
            def mark_blocked(item: Verification) -> None:
                item.state = "blocked"
                item.outcome = "BLOCKED"
                item.detail = "backend restarted or worker lost; run did not complete"
                item.events.append(
                    VerificationEvent(
                        at=verify_store.utcnow(),
                        phase="lifecycle",
                        message=item.detail,
                    )
                )

            updated = verify_store.update(
                verification.id, mark_blocked, expect_states=ACTIVE_STATES
            )
            if updated is not None:
                return updated
            refreshed = verify_store.get(verification.id)
            return refreshed if refreshed is not None else verification
    return verification
