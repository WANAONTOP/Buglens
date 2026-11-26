"""Investigation endpoints (all authenticated, read-only: no patches).

POST start validates the incident target (repo/ref/commit pin — never
invented) and the referenced run synchronously, then queues a bounded
background job. Only ``done`` / ``REPRODUCED`` runs with complete
artifacts and matching provenance are investigated; anything else is
reported BLOCKED rather than worked around.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException

from . import deploy
from . import store as incident_store
from .auth import require_auth
from .repro import store as repro_store
from .investigate import jobs as inv_jobs
from .investigate import store as inv_store
from .investigate.models import Investigation, InvestigationStart, InvestigationSummary
from .investigate.runner import run_investigation

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _check_id(value: str, label: str) -> str:
    if not _ID_PATTERN.match(value or ""):
        raise HTTPException(status_code=404, detail=f"Unknown {label}.")
    return value


@router.post("/incidents/{incident_id}/investigations", status_code=202)
def start_investigation(incident_id: str, payload: InvestigationStart) -> InvestigationSummary:
    incident = incident_store.get_incident(_check_id(incident_id, "incident"))
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident.")
    missing = [label for label, value in (("repo", incident.repo), ("ref", incident.ref), ("commit_sha", incident.commit_sha)) if not value]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Incident needs {', '.join(missing)} before investigation; nothing was invented.",
        )
    run = repro_store.get_run(_check_id(payload.run_id, "run"))
    if run is None or run.incident_id != incident.id:
        raise HTTPException(status_code=404, detail="Unknown run for this incident.")
    if not inv_jobs.has_capacity():
        raise HTTPException(
            status_code=429, detail="An investigation is already running; retry shortly."
        )
    now = inv_store.utcnow()
    inv = Investigation(
        id=uuid.uuid4().hex,
        incident_id=incident.id,
        run_id=run.id,
        repo=run.repo,
        commit_sha=run.commit_sha,
        contract_hash=run.contract_hash,
        state="queued",
        created_at=now,
        updated_at=now,
    )
    inv_store.create(inv)
    inv_store.add_event(inv.id, "lifecycle", f"queued for run {run.id[:12]}")
    try:
        started = inv_jobs.try_start(inv.id, lambda event: run_investigation(inv.id, event))
    except deploy.DeployError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not started:
        def mark_rejected(candidate: Investigation) -> None:
            candidate.state = "cancelled"
            candidate.detail = "rejected: another investigation holds the worker"

        inv_store.update(inv.id, mark_rejected)
        raise HTTPException(
            status_code=429, detail="An investigation is already running; retry shortly."
        )
    created = inv_store.get(inv.id)
    assert created is not None
    return InvestigationSummary.from_investigation(created)


@router.get("/incidents/{incident_id}/investigations", response_model=list[InvestigationSummary])
def list_investigations(incident_id: str) -> list[InvestigationSummary]:
    if incident_store.get_incident(_check_id(incident_id, "incident")) is None:
        raise HTTPException(status_code=404, detail="Unknown incident.")
    return [InvestigationSummary.from_investigation(i) for i in inv_store.list_for_incident(incident_id)]


@router.get("/investigations/{inv_id}", response_model=Investigation)
def get_investigation(inv_id: str) -> Investigation:
    inv = inv_store.get(_check_id(inv_id, "investigation"))
    if inv is None:
        raise HTTPException(status_code=404, detail="Unknown investigation.")
    return inv_jobs.reconcile(inv)


@router.post("/investigations/{inv_id}/cancel", response_model=InvestigationSummary)
def cancel_investigation(inv_id: str) -> InvestigationSummary:
    inv = inv_store.get(_check_id(inv_id, "investigation"))
    if inv is None:
        raise HTTPException(status_code=404, detail="Unknown investigation.")
    inv = inv_jobs.reconcile(inv)
    if inv.state not in ("queued", "running"):
        raise HTTPException(status_code=409, detail=f"Investigation already {inv.state}.")
    inv_jobs.cancel(inv.id)  # records the lifecycle event itself
    refreshed = inv_store.get(inv.id)
    assert refreshed is not None
    return InvestigationSummary.from_investigation(refreshed)
