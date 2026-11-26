"""Seven-gate verification records (Step 8: no PR publication)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

VerificationState = Literal["queued", "running", "done", "blocked", "error"]
GateVerdict = Literal["PASS", "FAIL", "BLOCKED"]
VerificationOutcome = Literal["VERIFIED", "NOT_VERIFIED", "BLOCKED"]


class GateResult(BaseModel):
    gate: int = Field(ge=1, le=7)
    name: str
    verdict: GateVerdict
    detail: str = ""
    measurements: dict = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)


class ApprovalSnapshot(BaseModel):
    candidate_id: str
    diff_sha256: str
    approved_at: datetime


class TestedIdentity(BaseModel):
    diff_sha256: str
    file_hashes: dict[str, str] = Field(default_factory=dict)  # tested bytes
    baseline_sha: str = ""


class VerifyArtifact(BaseModel):
    name: str  # logical name, e.g. cd-broken-before.png
    stored_filename: str  # server-generated
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str


class VerificationEvent(BaseModel):
    at: datetime
    phase: str
    message: str


class Verification(BaseModel):
    id: str
    incident_id: str
    run_id: str
    investigation_id: str
    candidate_id: str
    repo: str
    contract_hash: str
    policy_hash: str = ""
    approval: Optional[ApprovalSnapshot] = None
    state: VerificationState
    outcome: Optional[VerificationOutcome] = None
    detail: str = ""
    gates: list[GateResult] = Field(default_factory=list)
    tested: Optional[TestedIdentity] = None
    artifacts: list[VerifyArtifact] = Field(default_factory=list)
    events: list[VerificationEvent] = Field(default_factory=list)
    worker_call_id: str = ""
    created_at: datetime
    updated_at: datetime


class VerificationSummary(BaseModel):
    id: str
    incident_id: str
    candidate_id: str
    state: VerificationState
    outcome: Optional[VerificationOutcome] = None
    detail: str = ""
    gates_passed: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_verification(cls, verification: Verification) -> "VerificationSummary":
        return cls(
            id=verification.id,
            incident_id=verification.incident_id,
            candidate_id=verification.candidate_id,
            state=verification.state,
            outcome=verification.outcome,
            detail=verification.detail,
            gates_passed=sum(1 for g in verification.gates if g.verdict == "PASS"),
            created_at=verification.created_at,
            updated_at=verification.updated_at,
        )


class VerificationStart(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64)
