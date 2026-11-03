"""Deploy-mode worker entry points (one Modal function per job area).

Each ``run_*_guarded`` wrapper reloads the shared Volume, waits for the
API's spawn commit (``worker_call_id`` — deploy mode only, so a worker
can never start before its input record is committed), claims its
record with a guarded ``queued``-only write, invokes the existing
runner unchanged, and commits on the way out. Store writes inside the
runners commit individually (see the ``sync_after_write`` hooks), so
polls and the reconcile observe progress as it happens.

Anything already claimed or resolved means a duplicate spawn or a
record the API already settled, so the worker exits without spending
anything. A spawn the API never committed (crash between spawn and
mark) exits as ``uncommitted`` without writing; the stale reconcile
later marks that record lost.

Unexpected runner crashes are marked ``error``/``BLOCKED`` with the
sanitized detail instead of leaving the record active forever; a
worker killed by its function timeout leaves no marker, and status
reads reconcile that via liveness-checked staleness. Nothing here
retries: Modal functions run with ``retries=0``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import deploy

# Bound on waiting for the API's spawn mark. The mark lands
# synchronously inside the spawn request, so this only ever waits out
# a crashed API; tests monkeypatch it down.
CALL_ID_TIMEOUT_S = 60.0
CALL_ID_POLL_S = 2.0


def _wait_for_spawn_commit(
    record_id: str, get: Callable[[str], Any]
) -> Any:
    """Poll until the API's spawn mark is visible (deploy mode only)."""
    deadline = time.monotonic() + CALL_ID_TIMEOUT_S
    while True:
        deploy.sync_before_read()
        record = get(record_id)
        if record is None or record.state != "queued" or record.worker_call_id:
            return record
        if time.monotonic() >= deadline:
            return record
        time.sleep(CALL_ID_POLL_S)


def _guarded(
    record_id: str,
    get: Callable[[str], Any],
    claim: Callable[[], bool],
    mark_crashed: Callable[[], None],
    invoke: Callable[[threading.Event], None],
) -> str:
    """Claim a queued record and run it; returns the outcome label."""
    deploy.sync_before_read()
    record = get(record_id)
    if record is None:
        return "missing"
    if record.state != "queued":
        return "duplicate"
    if deploy.is_modal():
        record = _wait_for_spawn_commit(record_id, get)
        if record is None:
            return "missing"
        if record.state != "queued":
            return "duplicate"
        if not record.worker_call_id:
            return "uncommitted"
    if not claim():
        return "duplicate"
    try:
        invoke(threading.Event())
    except Exception as exc:
        mark_crashed()
        try:
            deploy.sync_after_write()
        except deploy.DeployError:
            pass
        return f"error: {exc}"
    try:
        deploy.sync_after_write()
    except deploy.DeployError:
        pass
    return "done"


def run_repro_guarded(run_id: str) -> str:
    """Worker body for one reproduction (Modal ``run_repro_worker``)."""
    from .repro import runner as repro_runner
    from .repro import store as repro_store
    from .repro.models import ReproEvent

    def claim() -> bool:
        def mutate(run) -> None:
            run.state = "running"
            run.events.append(
                ReproEvent(
                    at=repro_store.utcnow(),
                    phase="lifecycle",
                    message="worker claimed queued run",
                )
            )

        return (
            repro_store.update_run(run_id, mutate, expect_states=("queued",))
            is not None
        )

    def mark_crashed() -> None:
        def mutate(run) -> None:
            run.state = "error"
            run.detail = "worker crashed before finishing; see lifecycle events"
            run.events.append(
                ReproEvent(
                    at=repro_store.utcnow(),
                    phase="lifecycle",
                    message="worker crashed; marked error",
                )
            )

        repro_store.update_run(run_id, mutate, expect_states=("queued", "running"))

    return _guarded(
        run_id,
        repro_store.get_run,
        claim,
        mark_crashed,
        lambda event: repro_runner.run_reproduction(run_id, event),
    )


def run_investigation_guarded(inv_id: str) -> str:
    """Worker body for one investigation (Modal ``run_investigate_worker``)."""
    from .investigate import runner as inv_runner
    from .investigate import store as inv_store
    from .investigate.models import InvestigationEvent

    def claim() -> bool:
        def mutate(inv) -> None:
            inv.state = "running"
            inv.events.append(
                InvestigationEvent(
                    at=inv_store.utcnow(),
                    phase="lifecycle",
                    message="worker claimed queued investigation",
                )
            )

        return (
            inv_store.update(inv_id, mutate, expect_states=("queued",)) is not None
        )

    def mark_crashed() -> None:
        def mutate(inv) -> None:
            inv.state = "error"
            inv.detail = "worker crashed before finishing; see lifecycle events"
            inv.events.append(
                InvestigationEvent(
                    at=inv_store.utcnow(),
                    phase="lifecycle",
                    message="worker crashed; marked error",
                )
            )

        inv_store.update(inv_id, mutate, expect_states=("queued", "running"))

    return _guarded(
        inv_id,
        inv_store.get,
        claim,
        mark_crashed,
        lambda event: inv_runner.run_investigation(inv_id, event),
    )


def run_candidate_guarded(candidate_id: str) -> str:
    """Worker body for one candidate (Modal ``run_candidate_worker``)."""
    from .patches import runner as patch_runner
    from .patches import store as patch_store
    from .patches.models import CandidateEvent

    def claim() -> bool:
        def mutate(candidate) -> None:
            candidate.state = "generating"
            candidate.events.append(
                CandidateEvent(
                    at=patch_store.utcnow(),
                    phase="lifecycle",
                    message="worker claimed queued candidate",
                )
            )

        return (
            patch_store.update(candidate_id, mutate, expect_states=("queued",))
            is not None
        )

    def mark_crashed() -> None:
        def mutate(candidate) -> None:
            candidate.state = "error"
            candidate.detail = "worker crashed before finishing; see lifecycle events"
            candidate.events.append(
                CandidateEvent(
                    at=patch_store.utcnow(),
                    phase="lifecycle",
                    message="worker crashed; marked error",
                )
            )

        patch_store.update(
            candidate_id, mutate, expect_states=("queued", "generating")
        )

    return _guarded(
        candidate_id,
        patch_store.get,
        claim,
        mark_crashed,
        lambda event: patch_runner.run_candidate(candidate_id, event),
    )


def run_verification_guarded(verification_id: str) -> str:
    """Worker body for one verification (Modal ``run_verify_worker``)."""
    from .verify import runner as verify_runner
    from .verify import store as verify_store
    from .verify.models import VerificationEvent

    def claim() -> bool:
        def mutate(verification) -> None:
            verification.state = "running"
            verification.events.append(
                VerificationEvent(
                    at=verify_store.utcnow(),
                    phase="lifecycle",
                    message="worker claimed queued verification",
                )
            )

        return (
            verify_store.update(verification_id, mutate, expect_states=("queued",))
            is not None
        )

    def mark_crashed() -> None:
        def mutate(verification) -> None:
            verification.state = "error"
            verification.outcome = "BLOCKED"
            verification.detail = "worker crashed before finishing; see lifecycle events"
            verification.events.append(
                VerificationEvent(
                    at=verify_store.utcnow(),
                    phase="lifecycle",
                    message="worker crashed; marked error",
                )
            )

        verify_store.update(
            verification_id, mutate, expect_states=("queued", "running")
        )

    return _guarded(
        verification_id,
        verify_store.get,
        claim,
        mark_crashed,
        lambda event: verify_runner.run_verification(verification_id, event),
    )


__all__ = [
    "run_repro_guarded",
    "run_investigation_guarded",
    "run_candidate_guarded",
    "run_verification_guarded",
]
