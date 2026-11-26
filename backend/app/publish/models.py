"""Publication records (Step 9: trusted GitHub publisher, no auto-merge)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

PublicationState = Literal["publishing", "published", "failed"]


class PreflightCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class PublicationEvent(BaseModel):
    at: datetime
    phase: str
    message: str


class Publication(BaseModel):
    id: str
    incident_id: str
    candidate_id: str
    verification_id: str
    repo: str
    base_branch: str
    base_sha: str
    branch: str
    pr_title: str = ""
    state: PublicationState
    detail: str = ""
    commit_sha: str = ""
    pr_number: int = 0
    pr_url: str = ""
    preflight: list[PreflightCheck] = Field(default_factory=list)
    events: list[PublicationEvent] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PublicationPreview(BaseModel):
    repo: str
    base_branch: str
    base_sha: str
    branch: str
    changed_files: list[str]
    diff: str
    diff_sha256: str
    pr_title: str
    pr_body: str
    verification_id: str
    candidate_id: str
    preflight: list[PreflightCheck]
    preflight_ok: bool
