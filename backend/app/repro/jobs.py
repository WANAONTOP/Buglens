"""Background reproduction jobs: one worker pool, capacity gate, cancellation.

At most ``config.REPRO_MAX_CONCURRENT`` runs execute at once; further
starts are rejected (HTTP 429) rather than queued silently. Each job gets
a cancellation event the runner polls between phases. If the backend
restarts mid-run, ``reconcile`` marks the orphaned record cancelled on the
next status read instead of leaving it running forever.

Deploy mode (``BUGLENS_JOBS=modal``) spawns a Modal worker function per
run instead of a thread; capacity is enforced by counting active
records, cancellation terminates the remote call, and stale records
(no persisted progress within budget) reconcile as lost workers.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable, Optional

from .. import config, deploy
from . import store as repro_store
from .models import ReproEvent, ReproRun

REMOTE_WORKER = "run_repro_worker"
RECORD_SUBDIR = "repro_runs"
RECORD_FILENAME = "run.json"
ACTIVE_STATES = ("queued", "running")

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="repro"
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
            _capacity = threading.Semaphore(max(1, config.REPRO_MAX_CONCURRENT))
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
            deploy.count_active(RECORD_SUBDIR, ACTIVE_STATES, RECORD_FILENAME)
            < max(1, config.REPRO_MAX_CONCURRENT)
        )
    if _semaphore().acquire(blocking=False):
        _semaphore().release()
        return True
    return False


def _try_start_remote(run_id: str) -> bool:
    """Spawn the deployed worker exactly once; False when at capacity.

    A repeat submission for an already-dispatched record returns True
    without spawning again. Raises ``DeployError`` when the spawn
    itself fails (routes map it to 502): a spawn failure is not a
    capacity signal.
    """
    with _spawn_lock:
        deploy.sync_before_read()
        run = repro_store.get_run(run_id)
        if run is None:
            return False
        if run.worker_call_id or run.state != "queued":
            return True
        if (
            deploy.count_active(
                RECORD_SUBDIR, ACTIVE_STATES, RECORD_FILENAME, exclude=run_id
            )
            >= max(1, config.REPRO_MAX_CONCURRENT)
        ):
            return False
        call_id = deploy.spawn_worker(REMOTE_WORKER, run_id)

        def mark_spawned(candidate: ReproRun) -> None:
            candidate.worker_call_id = call_id

        marked = repro_store.update_run(
            run_id, mark_spawned, expect_states=("queued",)
        )
        if marked is None:  # defensive: the worker waits for this mark,
            deploy.cancel_call(call_id)  # so it cannot have claimed first
            return False
        return True


def try_start(run_id: str, fn: Callable[[threading.Event], None]) -> bool:
    """Reserve capacity and submit the job; False when at capacity.

    Deploy mode ignores ``fn`` and spawns the remote worker instead.
    """
    if deploy.use_remote_workers():
        return _try_start_remote(run_id)
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


def _cancel_remote(run_id: str) -> bool:
    """Kill the deployed worker, then mark the record cancelled.

    The kill is confirmed (briefly) before the mark, and the mark
    expects an active record, so a completion that landed first is
    never overwritten. Residual risk is only a worker that survives
    container termination and writes after the mark.
    """
    run = repro_store.get_run(run_id)
    if run is None or run.state not in ACTIVE_STATES:
        return False
    if run.worker_call_id:
        deploy.cancel_call(run.worker_call_id)
        deploy.wait_for_call_end(run.worker_call_id)
    deploy.sync_before_read()

    def mark_cancelled(candidate: ReproRun) -> None:
        candidate.state = "cancelled"
        candidate.detail = "cancellation requested; remote worker terminated"
        candidate.events.append(
            ReproEvent(
                at=repro_store.utcnow(),
                phase="lifecycle",
                message="cancellation requested",
            )
        )

    return (
        repro_store.update_run(run_id, mark_cancelled, expect_states=ACTIVE_STATES)
        is not None
    )


def cancel(run_id: str) -> bool:
    """Request cancellation; True when a live job was signalled."""
    if deploy.use_remote_workers():
        return _cancel_remote(run_id)
    with _live_lock:
        job = _live.get(run_id)
        if job is None:
            return False
        job["event"].set()
    repro_store.add_event(run_id, "lifecycle", "cancellation requested")
    return True


def is_live(run_id: str) -> bool:
    with _live_lock:
        return run_id in _live


def reconcile(run: ReproRun) -> ReproRun:
    """Mark orphaned active runs cancelled (e.g. after a backend restart).

    Deploy mode marks a stale record lost only when its worker call is
    confirmed finished (or was never recorded): a quiet-but-alive
    worker, or one whose liveness cannot be determined, is left alone.
    """
    if run.state in ACTIVE_STATES:
        if deploy.is_modal():
            if not deploy.is_stale(run.updated_at):
                return run
            record = repro_store.get_run(run.id)
            if record is None or record.state not in ACTIVE_STATES:
                return record if record is not None else run
            if record.worker_call_id:
                try:
                    status = deploy.call_status(record.worker_call_id)
                except deploy.DeployError:
                    return record
                if status != "finished":
                    return record

            def mark_lost(candidate: ReproRun) -> None:
                candidate.state = "cancelled"
                candidate.detail = (
                    "remote worker lost (no persisted progress within budget); "
                    "run did not complete"
                )
                candidate.events.append(
                    ReproEvent(
                        at=repro_store.utcnow(),
                        phase="lifecycle",
                        message=candidate.detail,
                    )
                )

            updated = repro_store.update_run(
                run.id, mark_lost, expect_states=ACTIVE_STATES
            )
            if updated is not None:
                return updated
            refreshed = repro_store.get_run(run.id)
            return refreshed if refreshed is not None else record
        if not is_live(run.id):
            def mark_cancelled(candidate: ReproRun) -> None:
                candidate.state = "cancelled"
                candidate.detail = "backend restarted or worker lost; run did not complete"
                candidate.events.append(
                    ReproEvent(
                        at=repro_store.utcnow(),
                        phase="lifecycle",
                        message=candidate.detail,
                    )
                )

            updated = repro_store.update_run(
                run.id, mark_cancelled, expect_states=ACTIVE_STATES
            )
            if updated is not None:
                return updated
            refreshed = repro_store.get_run(run.id)
            return refreshed if refreshed is not None else run
    return run
