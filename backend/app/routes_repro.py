"""Modal reproduction endpoints (all authenticated).

POST start resolves the incident's target to an immutable commit SHA
(existing pin wins, otherwise ``main`` is resolved and stored on the
incident), verifies the manifest and fixture at that SHA, freezes the
contract, and queues a bounded background job. Only frozen contracts ever
execute, and only inside Modal.
"""

from __future__ import annotations

import json
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import ValidationError

from . import config
from . import store as incident_store
from .auth import require_auth
from .manifest import BugLensManifest
from .providers.modal import ModalAdapter
from .repro import jobs as repro_jobs
from .repro import store as repro_store
from .repro.contract import (
    ContractError,
    freeze_contract,
    freeze_to_dict,
    manifest_blob_sha,
)
from .repro.github_source import (
    GitHubSourceError,
    fetch_blob,
    fetch_tree,
    resolve_ref_sha,
)
from .repro.models import ReproArtifact, ReproRun, ReproSummary
from .repro.runner import run_reproduction

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]*$")


def _check_id(value: str, label: str) -> str:
    if not _ID_PATTERN.match(value or ""):
        raise HTTPException(status_code=404, detail=f"Unknown {label}.")
    return value


def _check_sha(value: str) -> str:
    if not _SHA_PATTERN.match(value or ""):
        raise HTTPException(status_code=422, detail="Stored commit SHA is malformed.")
    return value


@router.post("/incidents/{incident_id}/reproductions", status_code=202)
def start_reproduction(incident_id: str) -> ReproSummary:
    incident = incident_store.get_incident(_check_id(incident_id, "incident"))
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident.")
    missing = [label for label, value in (("repo", incident.repo), ("ref", incident.ref)) if not value]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Incident needs {', '.join(missing)} before reproduction; nothing was invented.",
        )
    assert incident.repo is not None and incident.ref is not None
    modal_status = ModalAdapter(
        token_id=config.get_modal_token_id(),
        token_secret=config.get_modal_token_secret(),
    ).status()
    if modal_status.state == "not_configured":
        raise HTTPException(status_code=409, detail=modal_status.detail)
    if not repro_jobs.has_capacity():
        raise HTTPException(
            status_code=429, detail="A reproduction is already running; retry shortly."
        )

    token = config.get_github_token()
    if not token:
        raise HTTPException(
            status_code=409, detail="Set GITHUB_TOKEN in .env to resolve the published baseline."
        )
    try:
        if incident.commit_sha:
            sha = _check_sha(incident.commit_sha)
        else:
            if ".." in incident.ref or not _REF_PATTERN.match(incident.ref):
                raise HTTPException(status_code=422, detail="Stored ref is malformed.")
            sha = resolve_ref_sha(incident.repo, incident.ref, token)
            incident_store.update_commit_sha(incident.id, sha)
        tree = fetch_tree(incident.repo, sha, token)
        manifest_bytes = fetch_blob(incident.repo, manifest_blob_sha(tree), token)
        symlink_targets: dict[str, str] = {}
        try:
            workdir_prefix = BugLensManifest(
                **json.loads(manifest_bytes.decode("utf-8"))
            ).workingDirectory
        except (ValueError, ValidationError) as exc:
            raise ContractError(f"published manifest failed validation: {exc}")
        for entry in tree:
            if entry.is_symlink and (
                entry.path == workdir_prefix or entry.path.startswith(workdir_prefix + "/")
            ):
                symlink_targets[entry.path] = fetch_blob(
                    incident.repo, entry.sha, token
                ).decode("utf-8", errors="replace")
        frozen = freeze_contract(incident.repo, sha, manifest_bytes, tree, symlink_targets)
    except GitHubSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    now = repro_store.utcnow()
    run = ReproRun(
        id=uuid.uuid4().hex,
        incident_id=incident.id,
        repo=frozen.repo,
        commit_sha=frozen.sha,
        contract_hash=frozen.contract_hash,
        state="queued",
        created_at=now,
        updated_at=now,
    )
    repro_store.create_run(run)
    repro_store.save_contract(run.id, freeze_to_dict(frozen))
    repro_store.add_event(
        run.id, "freeze", f"contract {frozen.contract_hash[:12]} frozen at {frozen.sha[:7]}"
    )
    if not repro_jobs.try_start(run.id, lambda event: run_reproduction(run.id, event)):
        def mark_rejected(candidate: ReproRun) -> None:
            candidate.state = "cancelled"
            candidate.detail = "rejected: another reproduction holds the worker"

        repro_store.update_run(run.id, mark_rejected)
        raise HTTPException(
            status_code=429, detail="A reproduction is already running; retry shortly."
        )
    created = repro_store.get_run(run.id)
    assert created is not None
    return ReproSummary.from_run(created)


@router.get("/incidents/{incident_id}/reproductions", response_model=list[ReproSummary])
def list_reproductions(incident_id: str) -> list[ReproSummary]:
    if incident_store.get_incident(_check_id(incident_id, "incident")) is None:
        raise HTTPException(status_code=404, detail="Unknown incident.")
    return [ReproSummary.from_run(run) for run in repro_store.list_runs_for_incident(incident_id)]


@router.get("/reproductions/{run_id}", response_model=ReproRun)
def get_reproduction(run_id: str) -> ReproRun:
    run = repro_store.get_run(_check_id(run_id, "reproduction"))
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown reproduction.")
    return repro_jobs.reconcile(run)


@router.post("/reproductions/{run_id}/cancel", response_model=ReproSummary)
def cancel_reproduction(run_id: str) -> ReproSummary:
    run = repro_store.get_run(_check_id(run_id, "reproduction"))
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown reproduction.")
    run = repro_jobs.reconcile(run)
    if run.state not in ("queued", "running"):
        raise HTTPException(status_code=409, detail=f"Run already {run.state}.")
    repro_jobs.cancel(run.id)
    repro_store.add_event(run.id, "lifecycle", "cancellation requested")
    refreshed = repro_store.get_run(run.id)
    assert refreshed is not None
    return ReproSummary.from_run(refreshed)


@router.get("/reproductions/{run_id}/artifacts", response_model=list[ReproArtifact])
def list_repro_artifacts(run_id: str) -> list[ReproArtifact]:
    run = repro_store.get_run(_check_id(run_id, "reproduction"))
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown reproduction.")
    return run.artifacts


@router.get("/reproductions/{run_id}/artifacts/{name}/download")
def download_repro_artifact(run_id: str, name: str) -> FileResponse:
    run = repro_store.get_run(_check_id(run_id, "reproduction"))
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown reproduction.")
    artifact = next((a for a in run.artifacts if a.name == name), None)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Unknown artifact.")
    try:
        path = repro_store.safe_run_artifact_path(run.id, artifact.stored_filename)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown artifact.")
    if not path.exists():
        raise HTTPException(status_code=500, detail="Artifact bytes are missing from disk.")
    return FileResponse(path, media_type=artifact.content_type, filename=artifact.name)
