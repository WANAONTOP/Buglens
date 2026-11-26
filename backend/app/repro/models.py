"""Reproduction run records."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

RunState = Literal["queued", "running", "done", "cancelled", "error"]
Verdict = Literal["REPRODUCED", "NEEDS_REPRODUCTION", "BLOCKED"]


class ReproEvent(BaseModel):
    at: datetime
    phase: str
    message: str


class ReproArtifact(BaseModel):
    name: str  # logical name, e.g. broken-before.png
    stored_filename: str  # server-generated
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str


class ReproRun(BaseModel):
    id: str
    incident_id: str
    repo: str
    commit_sha: str
    contract_hash: str
    state: RunState
    verdict: Optional[Verdict] = None
    detail: str = ""
    events: list[ReproEvent] = Field(default_factory=list)
    artifacts: list[ReproArtifact] = Field(default_factory=list)
    worker_call_id: str = ""
    created_at: datetime
    updated_at: datetime


class ReproSummary(BaseModel):
    id: str
    incident_id: str
    repo: str
    commit_sha: str
    contract_hash: str
    state: RunState
    verdict: Optional[Verdict] = None
    detail: str = ""
    artifact_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_run(cls, run: ReproRun) -> "ReproSummary":
        return cls(
            id=run.id,
            incident_id=run.incident_id,
            repo=run.repo,
            commit_sha=run.commit_sha,
            contract_hash=run.contract_hash,
            state=run.state,
            verdict=run.verdict,
            detail=run.detail,
            artifact_count=len(run.artifacts),
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
