"""Verified GitHub PR publication (all authenticated; never auto-merges).

POST preview assembles the exact publish preview — destination repo
and base, repair branch, changed files, diff, PR title/body and the
full server-side preflight — with GitHub reads only. POST publish
requires an explicit developer Publish: it re-runs preflight and then
performs the branch + PR writes. GET returns a persisted publication
record (real PR URL only after GitHub confirms it).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import config
from .auth import require_auth
from .publish.models import Publication, PublicationPreview
from .publish.service import (
    BASE_BRANCH,
    PublishBusy,
    PublishConflict,
    PublishError,
    build_preview,
    publish,
)
from .publish import store as publish_store

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _check_id(value: str, label: str) -> str:
    if not _ID_PATTERN.match(value or ""):
        raise HTTPException(status_code=404, detail=f"Unknown {label}.")
    return value


class PreviewRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64)
    verification_id: str = Field(min_length=1, max_length=64)
    pr_title: str = Field(default="", max_length=200)
    base_branch: str = Field(default=BASE_BRANCH, max_length=100)


class PublishRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64)
    verification_id: str = Field(min_length=1, max_length=64)
    pr_title: str = Field(default="", max_length=200)
    base_branch: str = Field(default=BASE_BRANCH, max_length=100)


@router.post("/publications/preview", response_model=PublicationPreview)
def preview_publication(payload: PreviewRequest) -> PublicationPreview:
    _check_id(payload.candidate_id, "candidate")
    _check_id(payload.verification_id, "verification")
    try:
        return build_preview(
            payload.candidate_id, payload.verification_id,
            pr_title=payload.pr_title, base_branch=payload.base_branch or BASE_BRANCH,
            token=config.get_github_token())
    except PublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/publications", response_model=Publication, status_code=201)
def publish_repair(payload: PublishRequest) -> Publication:
    _check_id(payload.candidate_id, "candidate")
    _check_id(payload.verification_id, "verification")
    try:
        return publish(
            payload.candidate_id, payload.verification_id, payload.pr_title,
            base_branch=payload.base_branch or BASE_BRANCH,
            token=config.get_github_token())
    except PublishBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except PublishConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/publications/{publication_id}", response_model=Publication)
def get_publication(publication_id: str) -> Publication:
    record = publish_store.get(_check_id(publication_id, "publication"))
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown publication.")
    return record


@router.get("/candidates/{candidate_id}/publications", response_model=list[Publication])
def list_publications(candidate_id: str) -> list[Publication]:
    return publish_store.list_for_candidate(_check_id(candidate_id, "candidate"))
