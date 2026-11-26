"""Seven-gate verification endpoints (all authenticated; no PR creation).

POST start re-verifies the approval binding synchronously (candidate
state, approval record, diff hash, baseline pin, contract agreement)
and refuses stale or unapproved candidates with 409 — approval is
never fabricated or assumed. Execution runs as a bounded background
job across four fresh Modal sandboxes; gate decisions are made
server-side by the trusted controller.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from . import deploy
from . import store as incident_store
from .auth import require_auth
from .investigate import store as inv_store
from .patches import store as patch_store
from .repro import store as repro_store
from .verify import jobs as verify_jobs
from .verify import store as verify_store
from .verify.models import Verification, VerificationSummary
from .verify.runner import run_verification

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _check_id(value: str, label: str) -> str:
    if not _ID_PATTERN.match(value or ""):
        raise HTTPException(status_code=404, detail=f"Unknown {label}.")
    return value


def _approval_error(candidate_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"Candidate {candidate_id[:12]} is not validly approved; verification refused.",
    )


@router.post("/candidates/{candidate_id}/verifications", status_code=202)
def start_verification(candidate_id: str) -> VerificationSummary:
    candidate = patch_store.get(_check_id(candidate_id, "candidate"))
    if candidate is None:
        raise HTTPException(status_code=404, detail="Unknown candidate.")
    # Synchronous approval + provenance gate (the worker re-checks too).
    if candidate.state != "approved" or candidate.approval is None:
        raise _approval_error(candidate.id)
    current = hashlib.sha256(candidate.diff.encode("utf-8")).hexdigest()
    if current != candidate.diff_sha256 or candidate.approval.diff_sha256 != candidate.diff_sha256:
        raise _approval_error(candidate.id)
    if candidate.validation is None or not candidate.validation.ok:
        raise _approval_error(candidate.id)
    incident = incident_store.get_incident(candidate.incident_id)
    run = repro_store.get_run(candidate.run_id)
    inv = inv_store.get(candidate.investigation_id)
    if incident is None or run is None or inv is None:
        raise HTTPException(status_code=409, detail="Linked records are missing; verification refused.")
    if candidate.baseline_sha != incident.commit_sha or run.commit_sha != incident.commit_sha:
        raise HTTPException(status_code=409, detail="Baseline drift; verification refused.")
    if run.contract_hash != candidate.contract_hash:
        raise HTTPException(status_code=409, detail="Contract drift; verification refused.")
    if not verify_jobs.has_capacity():
        raise HTTPException(
            status_code=429, detail="A verification is already running; retry shortly."
        )
    now = verify_store.utcnow()
    verification = Verification(
        id=uuid.uuid4().hex,
        incident_id=incident.id,
        run_id=run.id,
        investigation_id=inv.id,
        candidate_id=candidate.id,
        repo=candidate.repo,
        baseline_sha=candidate.baseline_sha,
        contract_hash=candidate.contract_hash,
        state="queued",
        created_at=now,
        updated_at=now,
    )
    verify_store.create(verification)
    verify_store.add_event(verification.id, "lifecycle", f"queued for candidate {candidate.id[:12]}")
    try:
        started = verify_jobs.try_start(
            verification.id, lambda event: run_verification(verification.id, event)
        )
    except deploy.DeployError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not started:
        def mark_rejected(item: Verification) -> None:
            item.state = "blocked"
            item.outcome = "BLOCKED"
            item.detail = "rejected: another verification holds the worker"

        verify_store.update(verification.id, mark_rejected)
        raise HTTPException(
            status_code=429, detail="A verification is already running; retry shortly."
        )
    created = verify_store.get(verification.id)
    assert created is not None
    return VerificationSummary.from_verification(created)


@router.get("/candidates/{candidate_id}/verifications", response_model=list[VerificationSummary])
def list_verifications(candidate_id: str) -> list[VerificationSummary]:
    if patch_store.get(_check_id(candidate_id, "candidate")) is None:
        raise HTTPException(status_code=404, detail="Unknown candidate.")
    return [VerificationSummary.from_verification(v) for v in verify_store.list_for_candidate(candidate_id)]


@router.get("/verifications/{verification_id}", response_model=Verification)
def get_verification(verification_id: str) -> Verification:
    verification = verify_store.get(_check_id(verification_id, "verification"))
    if verification is None:
        raise HTTPException(status_code=404, detail="Unknown verification.")
    return verify_jobs.reconcile(verification)


@router.post("/verifications/{verification_id}/cancel", response_model=VerificationSummary)
def cancel_verification(verification_id: str) -> VerificationSummary:
    verification = verify_store.get(_check_id(verification_id, "verification"))
    if verification is None:
        raise HTTPException(status_code=404, detail="Unknown verification.")
    verification = verify_jobs.reconcile(verification)
    if verification.state not in ("queued", "running"):
        raise HTTPException(status_code=409, detail=f"Verification already {verification.state}.")
    verify_jobs.cancel(verification.id)  # records the lifecycle event itself
    refreshed = verify_store.get(verification.id)
    assert refreshed is not None
    return VerificationSummary.from_verification(refreshed)


@router.get("/verifications/{verification_id}/artifacts/{name}/download")
def download_verification_artifact(verification_id: str, name: str) -> FileResponse:
    verification = verify_store.get(_check_id(verification_id, "verification"))
    if verification is None:
        raise HTTPException(status_code=404, detail="Unknown verification.")
    artifact = next((a for a in verification.artifacts if a.name == name), None)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Unknown artifact.")
    try:
        path = verify_store.safe_artifact_path(verification.id, artifact.stored_filename)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown artifact.")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Artifact bytes are missing.")
    return FileResponse(path, media_type=artifact.content_type, filename=artifact.name)
