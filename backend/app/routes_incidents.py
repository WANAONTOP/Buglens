"""Incident and evidence API routes (M1 evidence intake, all authenticated)."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import config, store
from .auth import require_auth
from .media import MediaValidationError, identify_media, sanitize_filename
from .models import (
    ClickedPoint,
    CommitPin,
    Evidence,
    EvidenceKind,
    Incident,
    IncidentCreate,
    IncidentSummary,
)

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CHUNK_BYTES = 1024 * 1024


def _check_id(value: str, label: str) -> str:
    """Accept only server-issued 32-hex ids; everything else is a 404.

    Strict allowlisting (rather than blocklisting ``..``) is what makes
    path traversal impossible: no user input ever reaches the filesystem.
    """
    if not _ID_PATTERN.match(value or ""):
        raise HTTPException(status_code=404, detail=f"Unknown {label}.")
    return value


def _get_incident_or_404(incident_id: str) -> Incident:
    incident = store.get_incident(_check_id(incident_id, "incident"))
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident.")
    return incident


@router.post("/incidents", status_code=201, response_model=Incident)
def create_incident(payload: IncidentCreate) -> Incident:
    now = store.utcnow()
    incident = Incident(
        id=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        **payload.model_dump(),
    )
    return store.create_incident(incident)


@router.get("/incidents", response_model=list[IncidentSummary])
def list_incidents() -> list[IncidentSummary]:
    return [IncidentSummary.from_incident(i) for i in store.list_incidents()]


@router.get("/incidents/{incident_id}", response_model=Incident)
def get_incident(incident_id: str) -> Incident:
    return _get_incident_or_404(incident_id)


@router.patch("/incidents/{incident_id}/commit", response_model=Incident)
def pin_commit(incident_id: str, payload: CommitPin) -> Incident:
    updated = store.update_commit_sha(_check_id(incident_id, "incident"), payload.commit_sha)
    if updated is None:
        raise HTTPException(status_code=404, detail="Unknown incident.")
    return updated


def _kind_or_400(kind: str) -> EvidenceKind:
    try:
        return EvidenceKind(kind)
    except ValueError:
        allowed = ", ".join(k.value for k in EvidenceKind)
        raise HTTPException(
            status_code=400, detail=f"Unknown evidence kind {kind!r} (expected: {allowed})."
        )


@router.post("/incidents/{incident_id}/evidence", status_code=201, response_model=Evidence)
async def upload_evidence(
    incident_id: str,
    file: UploadFile = File(...),
    kind: str = Form(...),
    timestamp_ms: Optional[int] = Form(default=None),
    source_video_evidence_id: Optional[str] = Form(default=None),
    clicked_x: Optional[int] = Form(default=None),
    clicked_y: Optional[int] = Form(default=None),
    client_width: Optional[int] = Form(default=None),
    client_height: Optional[int] = Form(default=None),
    client_duration_ms: Optional[int] = Form(default=None),
) -> Evidence:
    incident = _get_incident_or_404(incident_id)
    evidence_kind = _kind_or_400(kind)

    if len(incident.evidence) >= config.MAX_EVIDENCE_PER_INCIDENT:
        raise HTTPException(
            status_code=413,
            detail=f"Incident already holds the maximum of {config.MAX_EVIDENCE_PER_INCIDENT} evidence items.",
        )

    family = "video" if evidence_kind == EvidenceKind.VIDEO else "image"
    cap = config.MAX_VIDEO_BYTES if family == "video" else config.MAX_IMAGE_BYTES

    # Stream the body to a staging file so oversized uploads are rejected
    # without buffering the whole body in memory.
    staging_dir = config.get_data_dir() / "artifacts"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / f"upload-{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with open(staged, "wb") as handle:
            while True:
                chunk = await file.read(_CHUNK_BYTES)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > cap:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds the {cap // (1024 * 1024)} MiB {family} limit.",
                    )
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise
    finally:
        await file.close()

    if size_bytes == 0:
        os.unlink(staged)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        media = identify_media(staged, file.content_type, family)
    except MediaValidationError as exc:
        os.unlink(staged)
        raise HTTPException(status_code=415, detail=str(exc))

    if evidence_kind == EvidenceKind.VIDEO_FRAME:
        if media.content_type not in ("image/png", "image/jpeg"):
            os.unlink(staged)
            raise HTTPException(
                status_code=415,
                detail="Video frames must be PNG or JPEG images.",
            )
        if timestamp_ms is None or timestamp_ms < 0:
            os.unlink(staged)
            raise HTTPException(
                status_code=400,
                detail="Video frames require timestamp_ms (milliseconds into the source video).",
            )
        if source_video_evidence_id is not None:
            source = next(
                (e for e in incident.evidence if e.id == source_video_evidence_id), None
            )
            if source is None or source.kind != EvidenceKind.VIDEO:
                os.unlink(staged)
                raise HTTPException(
                    status_code=400,
                    detail="source_video_evidence_id must reference a video of this incident.",
                )
    elif timestamp_ms is not None or source_video_evidence_id is not None:
        os.unlink(staged)
        raise HTTPException(
            status_code=400,
            detail="timestamp_ms/source_video_evidence_id apply to video_frame evidence only.",
        )

    clicked_point: Optional[ClickedPoint] = None
    if clicked_x is not None or clicked_y is not None:
        if evidence_kind != EvidenceKind.SCREENSHOT:
            os.unlink(staged)
            raise HTTPException(
                status_code=400,
                detail="Clicked points apply to screenshot evidence only.",
            )
        if clicked_x is None or clicked_y is None or clicked_x < 0 or clicked_y < 0:
            os.unlink(staged)
            raise HTTPException(
                status_code=400,
                detail="clicked_x/clicked_y must both be non-negative pixel coordinates.",
            )
        clicked_point = ClickedPoint(x=clicked_x, y=clicked_y)

    incident_bytes = sum(e.size_bytes for e in incident.evidence) + size_bytes
    if incident_bytes > config.MAX_INCIDENT_BYTES:
        os.unlink(staged)
        raise HTTPException(
            status_code=413,
            detail=f"Incident would exceed the {config.MAX_INCIDENT_BYTES // (1024 * 1024)} MiB total evidence budget.",
        )

    width, height = media.width, media.height
    duration_ms: Optional[int] = None
    if family == "video":
        # Client-reported from the decoded stream; sanity-checked, never trusted blindly.
        if client_width and 1 <= client_width <= 10000 and client_height and 1 <= client_height <= 10000:
            width, height = client_width, client_height
        if client_duration_ms is not None and 0 <= client_duration_ms <= 24 * 3600 * 1000:
            duration_ms = client_duration_ms

    evidence = Evidence(
        id=uuid.uuid4().hex,
        incident_id=incident.id,
        kind=evidence_kind,
        original_filename=sanitize_filename(file.filename or ""),
        stored_filename="",  # filled below; server-generated, never client-derived
        content_type=media.content_type,
        size_bytes=size_bytes,
        sha256=digest.hexdigest(),
        width=width,
        height=height,
        duration_ms=duration_ms,
        timestamp_ms=timestamp_ms,
        source_video_evidence_id=source_video_evidence_id,
        clicked_point=clicked_point,
        created_at=store.utcnow(),
    )
    evidence.stored_filename = f"{evidence.id}.{media.extension}"
    return store.add_evidence(incident.id, evidence, staged)


@router.get("/incidents/{incident_id}/evidence/{evidence_id}/download")
def download_evidence(incident_id: str, evidence_id: str) -> FileResponse:
    incident = _get_incident_or_404(incident_id)
    _check_id(evidence_id, "evidence")
    evidence = next((e for e in incident.evidence if e.id == evidence_id), None)
    if evidence is None:
        raise HTTPException(status_code=404, detail="Unknown evidence.")
    try:
        path = store.safe_artifact_path(evidence.stored_filename)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown evidence.")
    if not path.exists():
        # Record exists but bytes are gone: storage corruption, not a bad id.
        raise HTTPException(status_code=500, detail="Evidence bytes are missing from disk.")
    return FileResponse(
        path, media_type=evidence.content_type, filename=evidence.original_filename
    )
