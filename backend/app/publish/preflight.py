"""Server-side preflight: recompute every claim before a GitHub write.

Preflight independently validates the verification (completed LIVE
run, 7/7 PASS from gates over persisted step artifacts — never a
client-supplied flag), the approval binding, the identity agreement
(candidate, baseline, contract, policy, tested file bytes, diff hash),
the approved repository, and the not-cancelled/not-superseded state.
Local record checks run first; GitHub reads (pinned bytes, remote
base HEAD, repair-branch state) run last so local failures never
touch the network.

The evaluated change (new file bytes per changed path) is recomputed
with the same trusted applier Step 8 used (``patches.validate``) on
pinned blob bytes fetched from GitHub — never from the working tree.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

import httpx

from .. import config
from .. import store as incident_store
from ..investigate import store as inv_store
from ..patches import store as patch_store
from ..patches.validate import DiffError, apply_in_memory, parse_diff
from ..repro import store as repro_store
from ..repro.github_source import GitHubSourceError, fetch_blob, fetch_tree
from ..verify import store as verify_store
from ..verify.models import Verification
from . import github
from .models import PreflightCheck

# Evidence a completed live Modal run must have recorded: the trusted
# controller's policy, both checkout results, before-click screenshots,
# both pinned-suite outputs, and the command log. A hand-built record
# with outcome=VERIFIED but no artifacts fails here.
REQUIRED_ARTIFACTS = (
    "policy.json",
    "bl-result.json",
    "cd-result.json",
    "bl-broken-before.png",
    "cd-broken-before.png",
    "gate4-bl-tests.txt",
    "gate4-cd-tests.txt",
    "run.log",
)

# All four Modal sandboxes must show creation AND termination: baseline
# and candidate browser checkouts plus both pinned-suite runs.
SANDBOX_PHASES = ("bl", "cd", "gate4-bl", "gate4-cd")


class PreflightError(Exception):
    """A remote read or local evaluation failed unexpectedly (sanitized)."""


def _check(preflight: list[PreflightCheck], name: str, ok: bool, detail: str = "") -> bool:
    preflight.append(PreflightCheck(name=name, ok=ok, detail=detail))
    return ok


def check_verification_outcome(preflight: list[PreflightCheck],
                               verification: Optional[Verification]) -> bool:
    """verification-present / verification-outcome / gates-seven-pass."""
    if verification is None:
        return _check(preflight, "verification-present", False, "verification run not found")
    _check(preflight, "verification-present", True, verification.id)
    done = verification.state == "done" and verification.outcome == "VERIFIED"
    if not _check(
        preflight, "verification-outcome", done,
        detail=f"state={verification.state} outcome={verification.outcome}",
    ):
        return False
    verdicts = [(g.gate, g.name, g.verdict) for g in verification.gates]
    seven = (
        len(verdicts) == 7
        and [g.gate for g in verification.gates] == [1, 2, 3, 4, 5, 6, 7]
        and all(g.verdict == "PASS" for g in verification.gates)
        and all(g.measurements for g in verification.gates)
    )
    detail = ", ".join(f"{name}:{verdict}" for _, name, verdict in verdicts) or "no gates"
    if seven and any(not g.measurements for g in verification.gates):
        seven = False
        detail += " (a gate has no measurements)"
    return _check(preflight, "gates-seven-pass", seven, detail=detail)


def check_live_evidence(preflight: list[PreflightCheck], verification: Verification) -> bool:
    """live-evidence: artifacts + bytes + sandbox markers + gate evaluations."""
    recorded = {a.name: a for a in verification.artifacts}
    missing = [name for name in REQUIRED_ARTIFACTS if name not in recorded]
    if missing:
        return _check(preflight, "live-evidence", False,
                       detail=f"missing recorded artifacts: {', '.join(missing)}")
    for name in REQUIRED_ARTIFACTS:
        artifact = recorded[name]
        try:
            path = verify_store.safe_artifact_path(verification.id, artifact.stored_filename)
        except ValueError:
            return _check(preflight, "live-evidence", False,
                           detail=f"artifact {name}: unsafe stored filename")
        if not path.exists():
            return _check(preflight, "live-evidence", False,
                           detail=f"artifact {name}: bytes missing on disk")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact.sha256:
            return _check(preflight, "live-evidence", False,
                           detail=f"artifact {name}: bytes do not match recorded sha256")
    gate_names = {a for g in verification.gates for a in g.artifacts}
    unrecorded = sorted(gate_names - set(recorded))
    if unrecorded:
        return _check(preflight, "live-evidence", False,
                       detail=f"gate evidence not recorded: {', '.join(unrecorded)}")
    messages = [(e.phase, e.message) for e in verification.events]
    for phase in SANDBOX_PHASES:
        if not any(p == phase and "sandbox terminated" in m for p, m in messages):
            return _check(preflight, "live-evidence", False,
                           detail=f"no {phase} sandbox-terminated marker (cleanup unproven)")
    for num in range(1, 8):
        if not any(p == f"gate-{num}" for p, _ in messages):
            return _check(preflight, "live-evidence", False,
                           detail=f"no trusted gate-{num} evaluation event")
    if any("cancellation requested" in m or "cancelled" in m for _, m in messages):
        return _check(preflight, "live-evidence", False,
                       detail="run shows a cancellation marker")
    if verification.tested is None or not verification.tested.file_hashes:
        return _check(preflight, "live-evidence", False,
                       detail="no tested file identity recorded")
    if not verification.policy_hash:
        return _check(preflight, "live-evidence", False,
                       detail="no verification policy hash recorded")
    return _check(preflight, "live-evidence", True,
                   detail=f"{len(REQUIRED_ARTIFACTS)} artifacts verified, 4 sandboxes terminated")


def check_candidate(preflight: list[PreflightCheck], candidate: Any) -> None:
    """candidate-present / candidate-approved / diff-hash / candidate-validation."""
    _check(preflight, "candidate-present", True, candidate.id)
    approval = candidate.approval
    approved = (
        candidate.state == "approved"
        and approval is not None
        and approval.candidate_id == candidate.id
        and approval.diff_sha256 == candidate.diff_sha256
    )
    _check(preflight, "candidate-approved", approved,
           detail=f"state={candidate.state}" if approval is None
           else f"state={candidate.state} approved_at={approval.approved_at.isoformat()}")
    current = hashlib.sha256(candidate.diff.encode("utf-8")).hexdigest()
    _check(preflight, "diff-hash", current == candidate.diff_sha256,
           detail=f"sha256={current[:16]}… expected={candidate.diff_sha256[:16]}…")
    valid = candidate.validation is not None and candidate.validation.ok
    _check(preflight, "candidate-validation", valid,
           detail="structural validation passed" if valid else "validation missing or failed")


def check_binding(preflight: list[PreflightCheck], verification: Verification,
                  candidate: Any) -> bool:
    """verification-binding: the run tested exactly this candidate/baseline/contract."""
    snapshot = verification.approval
    if snapshot is None:
        return _check(preflight, "verification-binding", False,
                       detail="verification recorded no approval snapshot")
    problems: list[str] = []
    if snapshot.candidate_id != candidate.id:
        problems.append("approval snapshot names another candidate")
    if snapshot.diff_sha256 != candidate.diff_sha256:
        problems.append("approval snapshot binds another diff hash")
    tested = verification.tested
    if tested is None:
        problems.append("no tested identity")
    else:
        if tested.diff_sha256 != candidate.diff_sha256:
            problems.append("tested diff hash differs")
        if tested.baseline_sha != candidate.baseline_sha:
            problems.append("tested baseline differs")
    if verification.contract_hash != candidate.contract_hash:
        problems.append("contract hash differs")
    return _check(preflight, "verification-binding", not problems,
                   detail="; ".join(problems) if problems
                   else "candidate+diff+baseline+contract bound")


def check_provenance(preflight: list[PreflightCheck], verification: Verification,
                     candidate: Any) -> bool:
    """provenance-chain: linked records exist and agree on the incident pin."""
    incident = incident_store.get_incident(candidate.incident_id)
    run = repro_store.get_run(candidate.run_id)
    inv = inv_store.get(candidate.investigation_id)
    if incident is None or run is None or inv is None:
        return _check(preflight, "provenance-chain", False,
                       detail="incident, run, or investigation record is missing")
    problems: list[str] = []
    for label, got, want in (
        ("verification incident", verification.incident_id, candidate.incident_id),
        ("verification run", verification.run_id, candidate.run_id),
        ("verification investigation", verification.investigation_id, candidate.investigation_id),
        ("verification repo", verification.repo, candidate.repo),
    ):
        if got != want:
            problems.append(f"{label} disagrees")
    if candidate.baseline_sha != incident.commit_sha:
        problems.append("candidate baseline differs from incident pin")
    if run.commit_sha != incident.commit_sha:
        problems.append("repro run baseline differs from incident pin")
    if run.contract_hash != candidate.contract_hash:
        problems.append("repro contract differs from candidate contract")
    if inv.commit_sha != incident.commit_sha or inv.contract_hash != candidate.contract_hash:
        problems.append("investigation baseline/contract differs")
    return _check(preflight, "provenance-chain", not problems,
                   detail="; ".join(problems) if problems
                   else f"pin={incident.commit_sha}")


def check_repository(preflight: list[PreflightCheck], repo: str) -> bool:
    """repository-approved: only the configured repository is publishable."""
    approved = config.get_github_repo()
    ok = bool(approved) and repo == approved
    return _check(preflight, "repository-approved", ok,
                   detail=f"target={repo} approved={approved or '(not configured)'}")


def check_not_superseded(preflight: list[PreflightCheck], candidate: Any) -> bool:
    """not-superseded: no newer approval for the same incident."""
    if candidate.approval is None:
        return _check(preflight, "not-superseded", False, "candidate has no approval")
    for other in patch_store.list_for_incident(candidate.incident_id):
        if other.id == candidate.id or other.state != "approved" or other.approval is None:
            continue
        if other.approval.approved_at > candidate.approval.approved_at:
            return _check(preflight, "not-superseded", False,
                           detail=f"candidate {other.id[:12]} approved later")
    return _check(preflight, "not-superseded", True, detail="latest approval on incident")


def compute_change(candidate: Any, token: str,
                   transport: Optional[httpx.BaseTransport] = None
                   ) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Fetch pinned bytes and apply the diff; return (pinned, patched).

    Uses the same trusted applier Step 8 used. Raises PreflightError
    (sanitized) when the pinned tree is unreadable or the diff does
    not apply cleanly.
    """
    try:
        tree = fetch_tree(candidate.repo, candidate.baseline_sha, token, transport=transport)
    except GitHubSourceError as exc:
        raise PreflightError(f"pinned tree unreadable: {exc}")
    by_path = {entry.path: entry for entry in tree}
    pinned_lines: dict[str, list[str]] = {}
    trailing: dict[str, bool] = {}
    pinned_bytes: dict[str, bytes] = {}
    for path in candidate.changed_paths:
        entry = by_path.get(path)
        if entry is None or entry.type != "blob" or entry.is_symlink:
            raise PreflightError(f"{path} is not a readable blob at the pinned SHA")
        try:
            data = fetch_blob(candidate.repo, entry.sha, token, transport=transport)
        except GitHubSourceError as exc:
            raise PreflightError(f"{path}: blob fetch failed: {exc}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise PreflightError(f"{path} is not UTF-8 at the pinned SHA")
        pinned_lines[path] = text.splitlines()
        trailing[path] = text.endswith("\n")
        pinned_bytes[path] = data
    try:
        parsed = parse_diff(candidate.diff)
    except DiffError as exc:
        raise PreflightError(f"candidate diff is malformed: {exc}")
    if {f.path for f in parsed} != set(candidate.changed_paths):
        raise PreflightError("diff files differ from the recorded changed paths")
    patched: dict[str, bytes] = {}
    for item in parsed:
        try:
            new_lines = apply_in_memory(pinned_lines[item.path], item.hunks)
        except (DiffError, KeyError) as exc:
            raise PreflightError(f"{item.path}: diff does not apply: {exc}")
        text = "\n".join(new_lines)
        if trailing.get(item.path):
            text += "\n"
        patched[item.path] = text.encode("utf-8")
    return pinned_bytes, patched


def check_tested_bytes(preflight: list[PreflightCheck], verification: Verification,
                       candidate: Any, token: str,
                       transport: Optional[httpx.BaseTransport] = None) -> bool:
    """tested-bytes: recomputed change matches the recorded tested hashes."""
    if not token:
        return _check(preflight, "tested-bytes", False,
                       detail="GITHUB_TOKEN is not configured; cannot read pinned bytes")
    if verification.tested is None:
        return _check(preflight, "tested-bytes", False,
                       detail="verification recorded no tested identity")
    try:
        _pinned, patched = compute_change(candidate, token, transport=transport)
    except PreflightError as exc:
        return _check(preflight, "tested-bytes", False, detail=str(exc))
    expected = verification.tested.file_hashes
    if set(patched) != set(expected):
        return _check(preflight, "tested-bytes", False,
                       detail="recomputed files differ from the tested file set")
    for path, data in patched.items():
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected[path]:
            return _check(preflight, "tested-bytes", False,
                           detail=f"{path}: recomputed {digest[:12]} != tested {expected[path][:12]}")
    return _check(preflight, "tested-bytes", True,
                   detail=f"{len(patched)} file(s) reproduce the tested bytes")


def check_remote_base(preflight: list[PreflightCheck], candidate: Any, base_branch: str,
                      token: str,
                      transport: Optional[httpx.BaseTransport] = None) -> bool:
    """remote-base-pinned: the target branch still points at the baseline."""
    if not token:
        return _check(preflight, "remote-base-pinned", False,
                       detail="GITHUB_TOKEN is not configured; cannot read remote base")
    try:
        head = github.get_branch_sha(candidate.repo, base_branch, token, transport=transport)
    except github.GitHubPublishError as exc:
        return _check(preflight, "remote-base-pinned", False, detail=f"remote read failed: {exc}")
    if head is None:
        return _check(preflight, "remote-base-pinned", False,
                       detail=f"base branch {base_branch} does not exist remotely")
    if head != candidate.baseline_sha:
        return _check(preflight, "remote-base-pinned", False,
                       detail=f"remote {base_branch} is {head[:12]}, verified baseline "
                       f"is {candidate.baseline_sha[:12]}; refusing to transplant")
    return _check(preflight, "remote-base-pinned", True,
                   detail=f"{base_branch} == {head[:12]}")


def check_branch_state(preflight: list[PreflightCheck], candidate: Any, branch: str,
                       token: str,
                       transport: Optional[httpx.BaseTransport] = None) -> bool:
    """branch-state: report the repair branch so publish can verify reuse."""
    if not token:
        return _check(preflight, "branch-state", False,
                       detail="GITHUB_TOKEN is not configured; cannot read repair branch")
    try:
        sha = github.get_branch_sha(candidate.repo, branch, token, transport=transport)
    except github.GitHubPublishError as exc:
        return _check(preflight, "branch-state", False, detail=f"remote read failed: {exc}")
    if sha is None:
        return _check(preflight, "branch-state", True,
                       detail=f"{branch} is absent; publish will create it")
    return _check(preflight, "branch-state", True,
                   detail=f"{branch} already points at {sha[:12]}; "
                   "publish will verify identity before reuse")


def local_preflight(candidate_id: str, verification_id: str
                    ) -> tuple[list[PreflightCheck], Any, Optional[Verification]]:
    """Run every check that needs no network; return (checks, candidate, verification).

    Failed checks are reported in the list (so previews can show them);
    only genuinely missing or mismatched records return None.
    """
    preflight: list[PreflightCheck] = []
    verification = verify_store.get(verification_id)
    candidate = patch_store.get(candidate_id)
    if verification is None or candidate is None:
        _check(preflight, "verification-present", False,
               detail="verification or candidate record not found")
        return preflight, None, None
    if verification.candidate_id != candidate.id:
        _check(preflight, "verification-present", False,
               detail="verification belongs to another candidate")
        return preflight, None, None
    check_verification_outcome(preflight, verification)
    check_live_evidence(preflight, verification)
    check_candidate(preflight, candidate)
    check_binding(preflight, verification, candidate)
    check_provenance(preflight, verification, candidate)
    check_repository(preflight, candidate.repo)
    check_not_superseded(preflight, candidate)
    return preflight, candidate, verification


def remote_preflight(preflight: list[PreflightCheck], candidate: Any,
                     verification: Verification, branch: str, base_branch: str,
                     token: str,
                     transport: Optional[httpx.BaseTransport] = None) -> None:
    """Append the GitHub-read checks (pinned bytes, base HEAD, branch)."""
    check_tested_bytes(preflight, verification, candidate, token, transport=transport)
    check_remote_base(preflight, candidate, base_branch, token, transport=transport)
    check_branch_state(preflight, candidate, branch, token, transport=transport)
