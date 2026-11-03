"""Deployment-mode support: Modal volumes, remote workers, cross-container sync.

Local mode (the default) is unchanged: in-process executors, local disk,
and every helper here is a no-op. Deploy mode (``BUGLENS_MODAL=1`` with
``BUGLENS_JOBS=modal``) runs the API and the workers as separate Modal
functions sharing one Volume.

A shared Volume is NOT a transactional database, so conflicting writes
are prevented by ownership and lifecycle ordering, not by locks no
other container honors:

- The API is deployed with ``max_containers=1`` (see ``modal_app.py``),
  so its in-process locks serialize every API-side write.
- ``incidents.json`` is API-write-only: workers and runners only read
  it (a test trips if a runner ever calls a registry mutator). The
  optimistic-concurrency guard (``ConflictError``) stays as a tripwire.
- Each job record is phase-owned. The API owns it while ``queued``
  (create, spawn, call-id mark); the one spawned worker owns it while
  active (claim, progress, terminal write); terminal states are
  immutable. Handoffs are ordered, not raced: the worker writes
  nothing until the API's spawn commit (``worker_call_id``) is visible
  (``workers._guarded``), and the API marks a spawned record only
  after killing its worker (cancel) or confirming its call finished
  (reconcile via ``call_status``).
- Every deploy-mode record update reloads first, honors an
  ``expect_states`` precondition, and refuses writes to terminal
  records, so a stale container aborts instead of resurrecting or
  overwriting newer state.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

MODAL_APP_NAME = "buglens-ai"
MODAL_VOLUME_NAME = "buglens-data"
MODAL_SECRET_NAME = "buglens-secrets"
DATA_MOUNT = "/data"

# A deployed job that persisted no progress for this long is presumed
# dead (worker killed or lost); status reads reconcile it instead of
# waiting forever. All four runners persist events far more often.
DEPLOY_STALE_AFTER_S = 3600.0


class DeployError(Exception):
    """Deploy-mode plumbing failed (spawn, cancel, or volume sync)."""


class ConflictError(Exception):
    """Optimistic-concurrency guard: the stored version moved under us."""


def is_modal() -> bool:
    """True inside a deployed Modal container (volume sync enabled)."""
    return os.environ.get("BUGLENS_MODAL", "") == "1"


def jobs_mode() -> str:
    """``"modal"`` for remote workers, anything else for local threads."""
    return "modal" if os.environ.get("BUGLENS_JOBS", "") == "modal" else "local"


def use_remote_workers() -> bool:
    """True only when remote dispatch is fully configured.

    ``BUGLENS_JOBS=modal`` without ``BUGLENS_MODAL=1`` raises instead
    of silently running threads: remote workers read the shared
    Volume, so a local-disk API must never dispatch to them.
    """
    wants_remote = jobs_mode() == "modal"
    if wants_remote and not is_modal():
        raise DeployError("BUGLENS_JOBS=modal requires BUGLENS_MODAL=1 (shared Volume)")
    return wants_remote and is_modal()


_volume: Any = None


def volume() -> Any:
    """The shared data Volume (resolved lazily; deploy mode only)."""
    global _volume
    if _volume is not None:
        return _volume
    try:
        import modal
    except ImportError as exc:
        raise DeployError(
            "modal package required for volume access; pip install -r backend/requirements.txt"
        ) from exc
    _volume = modal.Volume.from_name(MODAL_VOLUME_NAME)
    return _volume


def reset_for_tests() -> None:
    """Drop the cached Volume handle (tests that fake the volume)."""
    global _volume
    _volume = None


def sync_after_write() -> None:
    """Commit store writes so other containers can see them.

    Raises ``DeployError`` when the commit fails: an uncommitted write
    would silently diverge, so it must surface, never pass quietly.
    """
    if not is_modal():
        return
    try:
        volume().commit()
    except DeployError:
        raise
    except Exception as exc:
        raise DeployError(f"volume commit failed: {exc}") from exc


def sync_before_read() -> None:
    """Reload the Volume so this container sees others' commits."""
    if not is_modal():
        return
    try:
        volume().reload()
    except DeployError:
        raise
    except Exception as exc:
        raise DeployError(f"volume reload failed: {exc}") from exc


def spawn_worker(worker_name: str, record_id: str) -> str:
    """Start a deployed worker for one record; returns the Modal call id."""
    try:
        import modal
    except ImportError as exc:
        raise DeployError(
            "modal package required to spawn workers; pip install -r backend/requirements.txt"
        ) from exc
    try:
        call = modal.Function.from_name(MODAL_APP_NAME, worker_name).spawn(record_id)
    except Exception as exc:
        raise DeployError(f"worker {worker_name} spawn failed: {exc}") from exc
    call_id = getattr(call, "object_id", "") or ""
    if not call_id:
        raise DeployError(f"worker {worker_name} spawn returned no call id")
    return str(call_id)


def cancel_call(call_id: str, terminate_containers: bool = True) -> bool:
    """Terminate a spawned worker call; False when already finished/gone.

    Container termination is the kill half of the cancel ordering: the
    API only marks a record after this, so a killed worker cannot later
    resurrect the record with a stale progress write.
    """
    try:
        import modal
    except ImportError as exc:
        raise DeployError("modal package required to cancel workers") from exc
    try:
        modal.FunctionCall.from_id(call_id).cancel(
            terminate_containers=terminate_containers
        )
    except Exception:
        return False
    return True


def _is_modal_client_error(exc: Exception) -> bool:
    """True for Modal client/infra failures (liveness genuinely unknown)."""
    try:
        from modal import exception as modal_exception
    except ImportError:  # pragma: no cover - modal present in deploy mode
        return True
    return isinstance(
        exc,
        (
            modal_exception.AuthError,
            modal_exception.ConnectionError,
            modal_exception.ServiceError,
            modal_exception.InternalError,
            modal_exception.InternalFailure,
            modal_exception.ClientClosed,
            modal_exception.VersionError,
        ),
    )


def call_status(call_id: str) -> str:
    """Non-blocking worker-call liveness: "running", "finished", or "unknown".

    ``"finished"`` covers every terminal outcome (returned, raised,
    cancelled, timed out, expired, or never existed): in all of them the
    worker will write nothing more. ``"unknown"`` means the Modal API
    itself failed, and callers must fail safe (leave the record alone).
    """
    try:
        import modal
    except ImportError as exc:
        raise DeployError("modal package required to poll workers") from exc
    try:
        modal.FunctionCall.from_id(call_id).get(timeout=0)
    except TimeoutError:  # builtin: polled, no output yet
        return "running"
    except Exception as exc:
        if _is_modal_client_error(exc):
            return "unknown"
        return "finished"
    return "finished"


def wait_for_call_end(call_id: str, timeout_s: float = 5.0) -> bool:
    """Poll until a worker call finishes; False on timeout/unknown."""
    import time

    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            status = call_status(call_id)
        except DeployError:
            return False
        if status == "finished":
            return True
        if status == "unknown" or time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_stale(updated_at: datetime) -> bool:
    """True when a record saw no persisted progress within the budget."""
    stamp = updated_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (utcnow() - stamp).total_seconds() > DEPLOY_STALE_AFTER_S


def count_active(
    subdir: str,
    active_states: tuple[str, ...],
    filename: str = "record.json",
    exclude: Optional[str] = None,
) -> int:
    """Count records in an active state (deploy-mode capacity check).

    Reads raw JSON, not models: a corrupt or mid-write file is skipped,
    never fatal to a capacity probe. ``exclude`` skips one record id
    (its directory name): the run being started sits in ``queued``, an
    active state, and must not consume its own capacity slot.
    """
    from . import config

    base = Path(config.get_data_dir()) / subdir
    if not base.is_dir():
        return 0
    found = 0
    for record_path in base.glob(f"*/{filename}"):
        if exclude is not None and record_path.parent.name == exclude:
            continue
        try:
            state = json.loads(record_path.read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError):
            continue
        if state in active_states:
            found += 1
    return found


def resolve_store_version(path: Path) -> str:
    """Content hash of a store file ("" when missing); OCC versions."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def store_version_for_write() -> Optional[str]:
    """Expected OCC version for incident-registry writes (deploy mode)."""
    if not is_modal():
        return None
    from . import config

    return resolve_store_version(Path(config.get_data_dir()) / "incidents.json")
