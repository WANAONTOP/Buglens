"""Frozen execution contracts.

A contract binds ONE published commit to ONE validated manifest plus the
path resolutions the runner is allowed to touch. Freezing:

1. Parses the manifest bytes fetched at the exact commit (never local files).
2. Resolves workdir-relative paths against the published tree listing.
3. Rejects traversal (``..``, absolute paths) and symlink escapes: every
   symlink under the working directory must resolve inside it.
4. Requires every protected path to exist and every permitted path to cover
   at least one blob; no protected path may sit inside a permitted path.
5. Hashes the canonical contract (sha256 over sorted-key JSON).

A frozen contract is immutable: any correction requires a new published
commit, which freezes to a different hash. The runner executes only frozen
contracts.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError

from ..manifest import MANIFEST_FILENAME, BugLensManifest
from .github_source import TreeEntry


class ContractError(Exception):
    """The published contract is missing, invalid, or unsafe to execute."""


@dataclass(frozen=True)
class FrozenContract:
    repo: str
    sha: str
    manifest: BugLensManifest
    manifest_bytes_sha256: str
    contract_hash: str
    blob_count: int


def _under(path: str, prefix: str) -> bool:
    path = path.rstrip("/")
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def freeze_contract(
    repo: str,
    sha: str,
    manifest_bytes: bytes,
    tree: list[TreeEntry],
    symlink_targets: Optional[dict[str, str]] = None,
) -> FrozenContract:
    try:
        manifest = BugLensManifest(**json.loads(manifest_bytes.decode("utf-8")))
    except (ValueError, ValidationError) as exc:
        raise ContractError(f"published manifest failed validation: {exc}")

    blobs = {entry.path: entry for entry in tree if entry.type == "blob"}
    workdir = manifest.workingDirectory
    if not any(_under(path, workdir) for path in blobs):
        raise ContractError(f"working directory has no files at {sha[:7]}: {workdir}")

    for permitted in manifest.permittedPatchPaths:
        if not _under(permitted, workdir):
            raise ContractError(f"permitted path escapes working directory: {permitted}")
        if not any(_under(path, permitted) for path in blobs):
            raise ContractError(f"permitted path covers no published files: {permitted}")

    for protected in manifest.protectedPaths:
        if any(_under(protected, permitted) for permitted in manifest.permittedPatchPaths):
            raise ContractError(f"protected path inside permitted path: {protected}")
        if protected not in blobs:
            raise ContractError(f"protected path missing at {sha[:7]}: {protected}")

    # Symlink escapes: a link inside the workdir must not point outside it.
    targets = symlink_targets or {}
    for entry in tree:
        if not (entry.is_symlink and _under(entry.path, workdir)):
            continue
        if entry.path not in targets:
            raise ContractError(f"symlink target unverified: {entry.path}")
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(entry.path), targets[entry.path])
        )
        if resolved == ".." or resolved.startswith("../") or not _under(resolved, workdir):
            raise ContractError(
                f"symlink escapes working directory: {entry.path} -> {targets[entry.path]}"
            )

    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    canonical = json.dumps(
        {
            "repo": repo,
            "sha": sha,
            "manifest": manifest.model_dump(mode="json"),
            "manifest_bytes_sha256": manifest_sha,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return FrozenContract(
        repo=repo,
        sha=sha,
        manifest=manifest,
        manifest_bytes_sha256=manifest_sha,
        contract_hash=hashlib.sha256(canonical).hexdigest(),
        blob_count=len(blobs),
    )


def manifest_blob_sha(tree: list[TreeEntry]) -> str:
    """Locate the committed manifest blob or raise ContractError."""
    for entry in tree:
        if entry.path == MANIFEST_FILENAME and entry.type == "blob":
            return entry.sha
    raise ContractError(f"{MANIFEST_FILENAME} missing from the published tree")


def freeze_to_dict(frozen: FrozenContract) -> dict:
    return {
        "repo": frozen.repo,
        "sha": frozen.sha,
        "manifest": frozen.manifest.model_dump(mode="json"),
        "manifest_bytes_sha256": frozen.manifest_bytes_sha256,
        "contract_hash": frozen.contract_hash,
        "blob_count": frozen.blob_count,
    }


def freeze_from_dict(payload: dict) -> FrozenContract:
    return FrozenContract(
        repo=payload["repo"],
        sha=payload["sha"],
        manifest=BugLensManifest(**payload["manifest"]),
        manifest_bytes_sha256=payload["manifest_bytes_sha256"],
        contract_hash=payload["contract_hash"],
        blob_count=payload["blob_count"],
    )
