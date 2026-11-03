"""Candidate patch records (Step 7: generation + review, no verification)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

CandidateState = Literal[
    "queued", "generating", "proposed", "approved", "rejected", "blocked", "error"
]


class ModelCall(BaseModel):
    model: str
    status: str
    latency_ms: int = 0
    attempts: int = 1
    usage: Optional[dict] = None


class ValidationCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class ValidationResult(BaseModel):
    ok: bool
    checks: list[ValidationCheck] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ChangeExplanation(BaseModel):
    path: str
    explanation: str


class Approval(BaseModel):
    candidate_id: str
    diff_sha256: str
    approved_at: datetime
    note: str = ""


class CandidateEvent(BaseModel):
    at: datetime
    phase: str
    message: str


class Candidate(BaseModel):
    id: str
    incident_id: str
    run_id: str
    investigation_id: str
    hypothesis_rank: int = 1
    repo: str
    baseline_sha: str
    contract_hash: str
    state: CandidateState
    detail: str = ""
    diff: str = ""
    diff_sha256: str = ""
    changed_paths: list[str] = Field(default_factory=list)
    explanations: list[ChangeExplanation] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    expected_effect: str = ""
    regression_risks: list[str] = Field(default_factory=list)
    provider: Optional[ModelCall] = None
    raw_text: str = ""
    parsed: bool = True
    parse_note: str = ""
    validation: Optional[ValidationResult] = None
    approval: Optional[Approval] = None
    events: list[CandidateEvent] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class CandidateSummary(BaseModel):
    id: str
    incident_id: str
    investigation_id: str
    baseline_sha: str
    state: CandidateState
    detail: str = ""
    changed_paths: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> "CandidateSummary":
        return cls(
            id=candidate.id,
            incident_id=candidate.incident_id,
            investigation_id=candidate.investigation_id,
            baseline_sha=candidate.baseline_sha,
            state=candidate.state,
            detail=candidate.detail,
            changed_paths=candidate.changed_paths,
            created_at=candidate.created_at,
            updated_at=candidate.updated_at,
        )


class CandidateStart(BaseModel):
    investigation_id: str = Field(min_length=1, max_length=64)
    hypothesis_rank: int = Field(default=1, ge=1, le=10)


class CandidateDecision(BaseModel):
    note: str = Field(default="", max_length=500)
    diff_sha256: str = Field(default="", max_length=64)
