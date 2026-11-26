"""Prerequisites and pinned source context for one candidate.

The chain incident -> run -> investigation must exist, agree on the
repository SHA and contract hash, and carry a completed REPRODUCED run
with a completed investigation holding the selected hypothesis. Missing
or inconsistent prerequisites mean BLOCKED — no smoke reruns, no
invented replacement evidence.

Source context is fetched from GitHub at the exact pinned SHA: the
overlay definition, applicable styles, and interaction logic, drawn
from the investigation's validated mappings plus the hypothesis's own
validated locations. The cited element location is NOT assumed to be
the faulty-CSS location — surrounding style/script context ships with
it so the model sees the actual mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .. import config
from .. import store as incident_store
from ..investigate import store as inv_store
from ..investigate.models import Investigation
from ..models import Incident
from ..repro import store as repro_store
from ..repro.github_source import GitHubSourceError, fetch_blob, fetch_tree
from ..repro.models import ReproRun


class CandidateBlocked(Exception):
    """Prerequisites are missing or inconsistent (honestly reported)."""


@dataclass
class Prereqs:
    incident: Incident
    run: ReproRun
    investigation: Investigation
    hypothesis_rank: int
    contract: dict


@dataclass
class SourceContext:
    files: dict[str, list[str]]  # path -> pinned lines
    tree_paths: list[str]
    entry_types: dict[str, str]  # path -> blob | symlink | ...
    permitted: list[str]
    protected: list[str]
    combined_excerpt: str
    truncated: bool
    notes: list[str] = field(default_factory=list)


def resolve_prereqs(incident_id: str, run_id: str, inv_id: str, rank: int) -> Prereqs:
    """Load and cross-check the incident/run/investigation chain."""
    incident = incident_store.get_incident(incident_id)
    run = repro_store.get_run(run_id)
    inv = inv_store.get(inv_id)
    if incident is None or run is None or inv is None:
        raise CandidateBlocked("incident, run, or investigation record is missing")
    if run.incident_id != incident.id or inv.incident_id != incident.id:
        raise CandidateBlocked("records do not belong to the same incident")
    if inv.run_id != run.id:
        raise CandidateBlocked("investigation was built on a different run")
    if not incident.commit_sha or run.commit_sha != incident.commit_sha:
        raise CandidateBlocked("run SHA does not match the incident pin")
    if inv.commit_sha != incident.commit_sha or run.contract_hash != inv.contract_hash:
        raise CandidateBlocked("contract or SHA drift between run and investigation")
    if run.state != "done" or run.verdict != "REPRODUCED":
        raise CandidateBlocked(f"run is {run.state}/{run.verdict}; need done/REPRODUCED")
    if inv.state != "done" or not inv.wonder or not inv.wonder.parsed:
        raise CandidateBlocked("investigation is not a completed, parsed result")
    if not any(h.rank == rank for h in inv.wonder.hypotheses):
        raise CandidateBlocked(f"hypothesis rank {rank} is not in the investigation")
    contract = repro_store.load_contract(run.id)
    if contract is None or contract.get("contract_hash") != run.contract_hash:
        raise CandidateBlocked("frozen contract record is missing or inconsistent")
    return Prereqs(incident, run, inv, rank, contract)


def _wanted_paths(prereqs: Prereqs) -> list[str]:
    """Candidate source files: mappings + hypothesis locations (deduped)."""
    wanted: list[str] = []
    for mapping in prereqs.investigation.mappings:
        if mapping.file and mapping.file not in wanted:
            wanted.append(mapping.file)
    for hypothesis in prereqs.investigation.wonder.hypotheses:  # type: ignore[union-attr]
        if hypothesis.rank != prereqs.hypothesis_rank:
            continue
        for location in hypothesis.locations:
            if location.file and location.file not in wanted:
                wanted.append(location.file)
    return wanted


def collect_context(
    prereqs: Prereqs,
    token: str,
    transport: "httpx.BaseTransport | None" = None,
) -> SourceContext:
    """Fetch wanted sources at the pinned SHA (bounded, validated)."""
    assert prereqs.incident.repo is not None and prereqs.incident.commit_sha is not None
    try:
        tree = fetch_tree(prereqs.incident.repo, prereqs.incident.commit_sha, token,
                          transport=transport)
    except GitHubSourceError as exc:
        raise CandidateBlocked(f"pinned tree unreadable: {exc}")
    by_path = {entry.path: entry for entry in tree}
    manifest = prereqs.contract.get("manifest", {})
    permitted = [str(p) for p in manifest.get("permittedPatchPaths", [])]
    protected = [str(p) for p in manifest.get("protectedPaths", [])]
    if not permitted:
        raise CandidateBlocked("frozen contract names no permitted patch paths")

    files: dict[str, list[str]] = {}
    notes: list[str] = []
    for path in _wanted_paths(prereqs):
        entry = by_path.get(path)
        if entry is None or entry.type != "blob":
            notes.append(f"{path}: not a readable blob at the pinned SHA; skipped")
            continue
        try:
            data = fetch_blob(prereqs.incident.repo, entry.sha, token, transport=transport)
        except GitHubSourceError:
            notes.append(f"{path}: blob fetch failed; skipped")
            continue
        if b"\x00" in data[:8192]:
            notes.append(f"{path}: binary at the pinned SHA; skipped")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            notes.append(f"{path}: not UTF-8; skipped")
            continue
        files[path] = text.splitlines()
    if not files:
        raise CandidateBlocked("no wanted source files were readable at the pinned SHA")

    chunks: list[str] = []
    for path, lines in files.items():
        numbered = "\n".join(f"{i:4d}: {line}" for i, line in enumerate(lines, start=1))
        chunks.append(f"--- {path} ({len(lines)} lines) ---\n{numbered}")
    combined = "\n\n".join(chunks)
    truncated = False
    if len(combined) > config.PATCH_MAX_SOURCE_CHARS:
        combined = combined[: config.PATCH_MAX_SOURCE_CHARS] + "\n…[truncated to the source budget]"
        truncated = True
    entry_types = {
        path: ("symlink" if entry.is_symlink else entry.type) for path, entry in by_path.items()
    }
    return SourceContext(
        files=files,
        tree_paths=sorted(by_path),
        entry_types=entry_types,
        permitted=permitted,
        protected=protected,
        combined_excerpt=combined,
        truncated=truncated,
        notes=notes,
    )
