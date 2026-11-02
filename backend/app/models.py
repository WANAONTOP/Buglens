"""Incident and evidence schemas for M1 evidence intake.

An incident is the run record for one reported UI bug: the developer's
description plus uploaded visual evidence. Status vocabulary is deliberate:

- ``blocked``: intake is incomplete (no pinned commit SHA or no evidence).
- ``intake_complete``: SHA pinned and at least one evidence artifact stored.

A saved report is NOT a reproduced or verified bug. Reproduction and
verification belong to M3/M5; no status here may imply them.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field, field_validator

REPO_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
VIEWPORT_PATTERN = re.compile(r"^(\d{1,5})x(\d{1,5})$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ABS_PATH_PATTERN = re.compile(r"^(?:[A-Za-z]:|\\\\|/)")


class EvidenceKind(str, Enum):
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    VIDEO_FRAME = "video_frame"


def _strip(value):
    return value.strip() if isinstance(value, str) else value


class IncidentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    observed_behavior: str = Field(min_length=1, max_length=5000)
    expected_behavior: str = Field(min_length=1, max_length=5000)
    reproduction_steps: list[str] = Field(min_length=1, max_length=50)
    # Draft intake: repository details are optional at creation so a
    # screenshot-only report can be saved. Missing repo/ref/commit/paths
    # block investigation and patch execution instead (see guards.py).
    repo: Optional[str] = Field(default=None, max_length=200)
    ref: Optional[str] = Field(default=None, max_length=256)
    commit_sha: Optional[str] = Field(default=None, max_length=40)
    browser_viewport: Optional[str] = Field(default=None, max_length=20)
    permitted_source_paths: list[str] = Field(default_factory=list, max_length=100)

    _strip_strings = field_validator(
        "title",
        "observed_behavior",
        "expected_behavior",
        mode="before",
    )(_strip)

    _blank_to_none = field_validator(
        "repo",
        "ref",
        "browser_viewport",
        mode="before",
    )(lambda v: None if _strip(v) in (None, "") else _strip(v))

    @field_validator("reproduction_steps", "permitted_source_paths", mode="before")
    @classmethod
    def _strip_lists(cls, value):
        if isinstance(value, list):
            return [_strip(v) for v in value]
        return value

    @field_validator("reproduction_steps", mode="after")
    @classmethod
    def _check_steps(cls, steps: list[str]) -> list[str]:
        for step in steps:
            if not step:
                raise ValueError("reproduction steps must not contain blank entries")
            if len(step) > 1000:
                raise ValueError("each reproduction step is limited to 1000 characters")
        return steps

    @field_validator("repo", mode="after")
    @classmethod
    def _check_repo(cls, repo: Optional[str]) -> Optional[str]:
        if repo is not None and not REPO_PATTERN.match(repo):
            raise ValueError("repo must look like 'owner/name' with no spaces")
        return repo

    @field_validator("commit_sha", mode="before")
    @classmethod
    def _normalize_sha(cls, value):
        if value is None:
            return None
        value = _strip(value)
        if value == "":
            return None
        return value.lower() if isinstance(value, str) else value

    @field_validator("commit_sha", mode="after")
    @classmethod
    def _check_sha(cls, sha: Optional[str]) -> Optional[str]:
        if sha is not None and not SHA_PATTERN.match(sha):
            raise ValueError("commit_sha must be a full 40-character lowercase hex SHA")
        return sha

    @field_validator("browser_viewport", mode="after")
    @classmethod
    def _check_viewport(cls, viewport: Optional[str]) -> Optional[str]:
        if viewport is None:
            return None
        match = VIEWPORT_PATTERN.match(viewport)
        if not match:
            raise ValueError("browser_viewport must look like '1440x900'")
        width, height = int(match.group(1)), int(match.group(2))
        if not (200 <= width <= 10000 and 200 <= height <= 10000):
            raise ValueError("viewport dimensions must each be within 200..10000")
        return viewport

    @field_validator("permitted_source_paths", mode="after")
    @classmethod
    def _check_paths(cls, paths: list[str]) -> list[str]:
        for path in paths:
            if not path:
                raise ValueError("permitted source paths must not contain blank entries")
            if len(path) > 512:
                raise ValueError("each permitted source path is limited to 512 characters")
            if ABS_PATH_PATTERN.match(path) or ".." in path.split("/"):
                raise ValueError(
                    f"permitted source paths must be relative and free of '..': {path!r}"
                )
        return paths


class ClickedPoint(BaseModel):
    """Developer-marked click location in original screenshot pixels."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Evidence(BaseModel):
    id: str
    incident_id: str
    kind: EvidenceKind
    original_filename: str
    stored_filename: str
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str
    # Server-parsed pixel dimensions for images; client-reported for video.
    width: Optional[int] = None
    height: Optional[int] = None
    # Client-reported video duration.
    duration_ms: Optional[int] = None
    # Position inside the source video for video_frame evidence.
    timestamp_ms: Optional[int] = None
    source_video_evidence_id: Optional[str] = None
    clicked_point: Optional[ClickedPoint] = None
    created_at: datetime


def investigation_blockers_for(incident: "Incident") -> list[str]:
    """Prerequisites missing before investigation/patch execution may run.

    Draft intake deliberately does NOT require these: a screenshot-only
    report saves fine and shows this list instead. Investigation and patch
    execution (M3+) must refuse to run while the list is non-empty; see
    ``guards.require_investigation_ready``. Repository details are never
    invented to fill these in.
    """
    blockers = []
    if not incident.repo:
        blockers.append("missing repository: set the owner/name to investigate")
    if not incident.ref:
        blockers.append("missing ref: set the branch or ref to investigate")
    if not incident.commit_sha:
        blockers.append("missing commit_sha: pin the target commit")
    if not incident.permitted_source_paths:
        blockers.append("missing permitted_source_paths: approve which files may change")
    return blockers


class Incident(BaseModel):
    id: str
    title: str
    observed_behavior: str
    expected_behavior: str
    reproduction_steps: list[str]
    repo: Optional[str] = None
    ref: Optional[str] = None
    commit_sha: Optional[str] = None
    browser_viewport: Optional[str] = None
    permitted_source_paths: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    evidence: list[Evidence] = Field(default_factory=list)

    @computed_field
    @property
    def intake_status(self) -> str:
        return "intake_complete" if not self.blockers else "blocked"

    @computed_field
    @property
    def blockers(self) -> list[str]:
        blockers = []
        if not self.commit_sha:
            blockers.append("missing commit_sha: pin the target commit")
        if not self.evidence:
            blockers.append("missing evidence: upload a screenshot or recording")
        return blockers

    @computed_field
    @property
    def investigation_blockers(self) -> list[str]:
        return investigation_blockers_for(self)


class IncidentSummary(BaseModel):
    id: str
    title: str
    repo: Optional[str] = None
    ref: Optional[str] = None
    commit_sha: Optional[str] = None
    intake_status: str
    blockers: list[str]
    evidence_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_incident(cls, incident: Incident) -> "IncidentSummary":
        return cls(
            id=incident.id,
            title=incident.title,
            repo=incident.repo,
            ref=incident.ref,
            commit_sha=incident.commit_sha,
            intake_status=incident.intake_status,
            blockers=incident.blockers,
            evidence_count=len(incident.evidence),
            created_at=incident.created_at,
            updated_at=incident.updated_at,
        )


class CommitPin(BaseModel):
    commit_sha: str = Field(min_length=1, max_length=40)

    _strip_sha = field_validator("commit_sha", mode="before")(_strip)

    @field_validator("commit_sha", mode="after")
    @classmethod
    def _check_sha(cls, sha: str) -> str:
        sha = sha.lower()
        if not SHA_PATTERN.match(sha):
            raise ValueError("commit_sha must be a full 40-character lowercase hex SHA")
        return sha
