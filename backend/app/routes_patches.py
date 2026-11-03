"""Candidate patch endpoints (all authenticated; review only, no verification).

POST start validates the incident/investigation chain synchronously and
queues one bounded generation job. Approve/Reject are explicit developer
decisions: approval binds the candidate ID to the exact diff hash and
re-verifies the binding first, so any change to the diff invalidates it.
Approval authorizes a later verification run; it is not verification.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException

from . import config, deploy
from . import store as incident_store
from .auth import require_auth
from .investigate import store as inv_store
from .patches import jobs as patch_jobs
from .patches import store as patch_store
from .patches.models import (
    Approval,
    Candidate,
    CandidateDecision,
    CandidateStart,
    CandidateSummary,
)
from .patches.runner import approval_intact, run_candidate

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_ACTIVE_OR_DECIDED = ("queued", "generating", "proposed", "approved", "rejected")


def _check_id(value: str, label: str) -> str:
    if not _ID_PATTERN.match(value or ""):
        raise HTTPException(status_code=404, detail=f"Unknown {label}.")
    return value


@router.post("/incidents/{incident_id}/candidates", status_code=202)
def start_candidate(incident_id: str, payload: CandidateStart) -> CandidateSummary:
    incident = incident_store.get_incident(_check_id(incident_id, "incident"))
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident.")
    missing = [label for label, value in (("repo", incident.repo), ("ref", incident.ref), ("commit_sha", incident.commit_sha)) if not value]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Incident needs {', '.join(missing)} before patch generation; nothing was invented.",
        )
    inv = inv_store.get(_check_id(payload.investigation_id, "investigation"))
    if inv is None or inv.incident_id != incident.id:
        raise HTTPException(status_code=404, detail="Unknown investigation for this incident.")
    if inv.state != "done" or not inv.wonder or not inv.wonder.parsed:
        raise HTTPException(
            status_code=422, detail="Investigation is not a completed, parsed result."
        )
    if not any(h.rank == payload.hypothesis_rank for h in inv.wonder.hypotheses):
        raise HTTPException(status_code=422, detail="Hypothesis rank is not in the investigation.")
    attempts = [c for c in patch_store.list_for_incident(incident.id) if c.state in _ACTIVE_OR_DECIDED]
    if len(attempts) >= config.PATCH_MAX_CANDIDATES_PER_INCIDENT:
        raise HTTPException(
            status_code=409,
            detail=f"Repair attempt budget spent ({config.PATCH_MAX_CANDIDATES_PER_INCIDENT} candidates).",
        )
    if not patch_jobs.has_capacity():
        raise HTTPException(
            status_code=429, detail="A candidate is already generating; retry shortly."
        )
    now = patch_store.utcnow()
    candidate = Candidate(
        id=uuid.uuid4().hex,
        incident_id=incident.id,
        run_id=inv.run_id,
        investigation_id=inv.id,
        hypothesis_rank=payload.hypothesis_rank,
        repo=inv.repo,
        baseline_sha=inv.commit_sha,
        contract_hash=inv.contract_hash,
        state="queued",
        created_at=now,
        updated_at=now,
    )
    patch_store.create(candidate)
    patch_store.add_event(candidate.id, "lifecycle", f"queued for hypothesis {payload.hypothesis_rank}")
    try:
        started = patch_jobs.try_start(
            candidate.id, lambda event: run_candidate(candidate.id, event)
        )
    except deploy.DeployError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not started:
        def mark_rejected(item: Candidate) -> None:
            item.state = "blocked"
            item.detail = "rejected: another candidate holds the worker"

        patch_store.update(candidate.id, mark_rejected)
        raise HTTPException(
            status_code=429, detail="A candidate is already generating; retry shortly."
        )
    created = patch_store.get(candidate.id)
    assert created is not None
    return CandidateSummary.from_candidate(created)


@router.get("/incidents/{incident_id}/candidates", response_model=list[CandidateSummary])
def list_candidates(incident_id: str) -> list[CandidateSummary]:
    if incident_store.get_incident(_check_id(incident_id, "incident")) is None:
        raise HTTPException(status_code=404, detail="Unknown incident.")
    return [CandidateSummary.from_candidate(c) for c in patch_store.list_for_incident(incident_id)]


@router.get("/candidates/{candidate_id}", response_model=Candidate)
def get_candidate(candidate_id: str) -> Candidate:
    candidate = patch_store.get(_check_id(candidate_id, "candidate"))
    if candidate is None:
        raise HTTPException(status_code=404, detail="Unknown candidate.")
    return patch_jobs.reconcile(candidate)


@router.post("/candidates/{candidate_id}/approve", response_model=CandidateSummary)
def approve_candidate(candidate_id: str, payload: CandidateDecision) -> CandidateSummary:
    candidate = patch_store.get(_check_id(candidate_id, "candidate"))
    if candidate is None:
        raise HTTPException(status_code=404, detail="Unknown candidate.")
    candidate = patch_jobs.reconcile(candidate)
    if candidate.state != "proposed":
        raise HTTPException(status_code=409, detail=f"Candidate is {candidate.state}, not proposed.")
    if candidate.validation is None or not candidate.validation.ok:
        raise HTTPException(status_code=409, detail="Candidate failed validation; cannot approve.")
    current = hashlib.sha256(candidate.diff.encode("utf-8")).hexdigest()
    if current != candidate.diff_sha256 or payload.diff_sha256 != candidate.diff_sha256:
        raise HTTPException(
            status_code=409,
            detail="Diff no longer matches its recorded hash; approval refused (stale or tampered).",
        )
    now = patch_store.utcnow()

    def mark_approved(item: Candidate) -> None:
        item.state = "approved"
        item.detail = "approved by developer review; authorizes a later verification run"
        item.approval = Approval(
            candidate_id=item.id, diff_sha256=item.diff_sha256,
            approved_at=now, note=payload.note,
        )

    updated = patch_store.update(candidate.id, mark_approved)
    assert updated is not None
    if not approval_intact(updated):
        raise HTTPException(status_code=500, detail="Approval binding failed to persist.")
    patch_store.add_event(candidate.id, "review", "approved by developer")
    refreshed = patch_store.get(candidate.id)
    assert refreshed is not None
    return CandidateSummary.from_candidate(refreshed)


@router.post("/candidates/{candidate_id}/reject", response_model=CandidateSummary)
def reject_candidate(candidate_id: str, payload: CandidateDecision) -> CandidateSummary:
    candidate = patch_store.get(_check_id(candidate_id, "candidate"))
    if candidate is None:
        raise HTTPException(status_code=404, detail="Unknown candidate.")
    candidate = patch_jobs.reconcile(candidate)
    if candidate.state != "proposed":
        raise HTTPException(status_code=409, detail=f"Candidate is {candidate.state}, not proposed.")
    reason = payload.note.strip() or "rejected by developer review"

    def mark_rejected(item: Candidate) -> None:
        item.state = "rejected"
        item.detail = reason

    patch_store.update(candidate.id, mark_rejected)
    patch_store.add_event(candidate.id, "review", reason)
    refreshed = patch_store.get(candidate.id)
    assert refreshed is not None
    return CandidateSummary.from_candidate(refreshed)


@router.post("/candidates/{candidate_id}/cancel", response_model=CandidateSummary)
def cancel_candidate(candidate_id: str) -> CandidateSummary:
    candidate = patch_store.get(_check_id(candidate_id, "candidate"))
    if candidate is None:
        raise HTTPException(status_code=404, detail="Unknown candidate.")
    candidate = patch_jobs.reconcile(candidate)
    if candidate.state not in ("queued", "generating"):
        raise HTTPException(status_code=409, detail=f"Candidate already {candidate.state}.")
    patch_jobs.cancel(candidate.id)  # records the lifecycle event itself
    refreshed = patch_store.get(candidate.id)
    assert refreshed is not None
    return CandidateSummary.from_candidate(refreshed)
