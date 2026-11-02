"""Reproduction-contract manifest (``.buglens.json``) validation.

The manifest declares HOW to reproduce a bug: working directory, install /
start commands, port, routes, viewport, actions, expected confirmation,
permitted patch paths, and protected paths. This module validates the
manifest's SHAPE and repository containment only:

- Paths must be relative and free of ``..``; permitted patch paths must
  stay inside the working directory; protected paths must include the
  manifest itself; no protected path may sit inside a permitted path.
- Actions use a closed vocabulary (``goto`` / ``assertVisible`` / ``click`` /
  ``assertConfirmation``); commands are opaque strings with length limits.

Execution policy (AGENTS.md rules 4/6): the BugLens backend NEVER executes
manifest ``install``/``start`` strings. Repository commands run only inside
the Modal sandbox (M5). The fixture's ``reproduce.mjs`` runner is the
documented development-time exception: it is invoked explicitly by a human
or agent from a shell — never by backend code from manifest strings — and
it spawns only the fixture's own pinned ``node server.js``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

MANIFEST_FILENAME = ".buglens.json"
_ABS_PATTERN = re.compile(r"^(?:[A-Za-z]:|\\\\|/)")


def _is_safe_relative(value: str) -> bool:
    if not value or _ABS_PATTERN.match(value) or "\\" in value:
        return False
    parts = PurePosixPath(value).parts
    return bool(parts) and ".." not in parts


class ManifestViewport(BaseModel):
    width: int = Field(ge=200, le=10000)
    height: int = Field(ge=200, le=10000)


class ManifestAction(BaseModel):
    action: Literal["goto", "assertVisible", "click", "assertConfirmation"]
    selector: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_selector(self) -> "ManifestAction":
        needs_selector = self.action in ("assertVisible", "click")
        if needs_selector and not (self.selector or "").strip():
            raise ValueError(f"{self.action} requires a non-empty selector")
        if not needs_selector and self.selector is not None:
            raise ValueError(f"{self.action} takes no selector")
        return self


class ManifestConfirmation(BaseModel):
    selector: str = Field(min_length=1, max_length=500)
    textContains: str = Field(min_length=1, max_length=500)


class BugLensManifest(BaseModel):
    version: Literal[1]
    workingDirectory: str = Field(min_length=1, max_length=256)
    install: str = Field(min_length=1, max_length=500)
    start: str = Field(min_length=1, max_length=500)
    port: int = Field(ge=1, le=65535)
    route: str = Field(min_length=1, max_length=256)
    controlRoute: str = Field(min_length=1, max_length=256)
    viewport: ManifestViewport
    actions: list[ManifestAction] = Field(min_length=1, max_length=50)
    requiredElements: list[str] = Field(min_length=1, max_length=50)
    expectedConfirmation: ManifestConfirmation
    permittedPatchPaths: list[str] = Field(min_length=1, max_length=50)
    protectedPaths: list[str] = Field(min_length=1, max_length=100)

    @field_validator("workingDirectory", mode="after")
    @classmethod
    def _check_workdir(cls, value: str) -> str:
        if not _is_safe_relative(value.rstrip("/")):
            raise ValueError("workingDirectory must be a safe relative path")
        return value.rstrip("/") or value

    @field_validator("route", "controlRoute", mode="after")
    @classmethod
    def _check_route(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("routes must start with '/'")
        return value

    @field_validator("requiredElements", mode="after")
    @classmethod
    def _check_selectors(cls, selectors: list[str]) -> list[str]:
        for selector in selectors:
            if not selector.strip() or len(selector) > 500:
                raise ValueError("required elements must be non-empty selectors")
        return selectors

    @field_validator("permittedPatchPaths", "protectedPaths", mode="after")
    @classmethod
    def _check_path_lists(cls, paths: list[str]) -> list[str]:
        for path in paths:
            if not _is_safe_relative(path.rstrip("/")):
                raise ValueError(f"unsafe path entry: {path!r}")
        return paths

    @field_validator("protectedPaths", mode="after")
    @classmethod
    def _check_self_protection(cls, paths: list[str]) -> list[str]:
        if MANIFEST_FILENAME not in paths:
            raise ValueError(f"protectedPaths must include {MANIFEST_FILENAME}")
        return paths


def load_manifest(path: str | Path) -> BugLensManifest:
    """Parse and schema-validate a manifest file (never executes anything)."""
    with open(path, "r", encoding="utf-8") as handle:
        return BugLensManifest(**json.load(handle))


def _under(path: str, prefix: str) -> bool:
    path = path.rstrip("/")
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def check_repository(manifest: BugLensManifest, repo_root: str | Path) -> list[str]:
    """Check manifest paths against a repository checkout.

    Returns a list of problems (empty means consistent): working directory
    exists, permitted paths stay inside it, no protected path sits inside
    a permitted path, and every protected path exists.
    """
    root = Path(repo_root)
    problems: list[str] = []
    workdir = manifest.workingDirectory
    if not (root / workdir).is_dir():
        problems.append(f"working directory missing: {workdir}")
    for permitted in manifest.permittedPatchPaths:
        if not _under(permitted, workdir):
            problems.append(f"permitted path escapes working directory: {permitted}")
    for protected in manifest.protectedPaths:
        if any(_under(protected, permitted) for permitted in manifest.permittedPatchPaths):
            problems.append(f"protected path inside permitted path: {protected}")
        if not (root / protected).exists():
            problems.append(f"protected path missing: {protected}")
    return problems
