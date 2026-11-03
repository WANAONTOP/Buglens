"""Investigation records (Step 6, read-only: no patches)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

InvestigationState = Literal["queued", "running", "done", "blocked", "cancelled", "error"]
EvidenceOrigin = Literal["uploaded", "baseline", "broken", "control"]
MappingCertainty = Literal["exact", "inferred", "unknown"]


class EvidenceItem(BaseModel):
    ref: str  # stable E-number, e.g. "E3"
    origin: EvidenceOrigin
    kind: str  # screenshot | geometry | style | hittest | console | annotation | trace | log
    route: Optional[str] = None  # broken | control
    artifact: Optional[str] = None  # repro artifact name when applicable
    summary: str
    data: dict = Field(default_factory=dict)  # small inline facts (rects, styles)


class ModelCall(BaseModel):
    model: str
    status: str
    latency_ms: int = 0
    attempts: int = 1
    usage: Optional[dict] = None


class SparkResult(BaseModel):
    call: ModelCall
    frames: list[str] = Field(default_factory=list)  # evidence refs sent
    observations: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    raw_text: str = ""
    parsed: bool = True
    parse_note: str = ""


class SamBox(BaseModel):
    object_id: int
    x1: int
    y1: int
    x2: int
    y2: int
    frame_w: int
    frame_h: int
    mask_h: int
    mask_w: int


class SamResult(BaseModel):
    call: ModelCall
    concept: str = ""
    frame_ref: str = ""  # evidence ref of the segmented frame
    frame_artifact: str = ""
    frame_w: int = 0
    frame_h: int = 0
    matches: list[SamBox] = Field(default_factory=list)
    note: str = ""


class SourceMapping(BaseModel):
    target: str  # DOM identity, e.g. div#promo-overlay
    file: Optional[str] = None  # repo-relative path at the pinned SHA
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    certainty: MappingCertainty = "unknown"
    evidence_refs: list[str] = Field(default_factory=list)
    note: str = ""


class WonderHypothesis(BaseModel):
    rank: int
    cause: str
    mechanism: str
    supporting_evidence: list[str] = Field(default_factory=list)
    conflicting_or_missing: list[str] = Field(default_factory=list)
    locations: list[SourceMapping] = Field(default_factory=list)
    repair_direction: str = ""
    unvalidated_citations: list[str] = Field(default_factory=list)


class WonderResult(BaseModel):
    call: ModelCall
    hypotheses: list[WonderHypothesis] = Field(default_factory=list)
    raw_text: str = ""
    parsed: bool = True
    parse_note: str = ""


class InvestigationEvent(BaseModel):
    at: datetime
    phase: str
    message: str


class Investigation(BaseModel):
    id: str
    incident_id: str
    run_id: str
    repo: str
    commit_sha: str
    contract_hash: str
    state: InvestigationState
    detail: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    absent_evidence: list[str] = Field(default_factory=list)
    spark: Optional[SparkResult] = None
    sam: Optional[SamResult] = None
    mappings: list[SourceMapping] = Field(default_factory=list)
    wonder: Optional[WonderResult] = None
    events: list[InvestigationEvent] = Field(default_factory=list)
    worker_call_id: str = ""
    created_at: datetime
    updated_at: datetime


class InvestigationSummary(BaseModel):
    id: str
    incident_id: str
    run_id: str
    commit_sha: str
    state: InvestigationState
    detail: str = ""
    hypothesis_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_investigation(cls, inv: Investigation) -> "InvestigationSummary":
        return cls(
            id=inv.id,
            incident_id=inv.incident_id,
            run_id=inv.run_id,
            commit_sha=inv.commit_sha,
            state=inv.state,
            detail=inv.detail,
            hypothesis_count=len(inv.wonder.hypotheses) if inv.wonder else 0,
            created_at=inv.created_at,
            updated_at=inv.updated_at,
        )


class InvestigationStart(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
