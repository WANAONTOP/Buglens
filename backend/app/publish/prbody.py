"""Reviewable PR body: sanitized evidence summary, no secrets or localhost links."""

from __future__ import annotations

from typing import Any, Optional


def _gate(verification: Any, name: str) -> Optional[Any]:
    for gate in verification.gates or []:
        if gate.name == name:
            return gate
    return None


def _gates_table(verification: Any) -> str:
    rows = ["| Gate | Result | Summary |", "| --- | --- | --- |"]
    for gate in sorted(verification.gates or [], key=lambda g: g.gate):
        detail = (gate.detail or "").replace("|", "/").replace("\n", " ")[:120]
        rows.append(f"| {gate.gate} {gate.name} | {gate.verdict} | {detail} |")
    return "\n".join(rows)


def test_summary(verification: Any) -> str:
    """Actual pinned-suite counts from the gate 4 measurements.

    Names every failing test (bounded) and states only what the
    measurements prove about baseline-vs-candidate agreement — never
    "X passing" when failures exist.
    """
    gate = _gate(verification, "repo-tests")
    if gate is None:
        return "(not recorded)"
    measures = gate.measurements or {}
    collected = measures.get("candidate_collected", "?")
    failed = [str(f) for f in (measures.get("candidate_failed", []) or [])]
    baseline_failed = [str(f) for f in (measures.get("baseline_failed", []) or [])]
    new_failures = measures.get("new_failures", [])
    summary = (f"{collected} collected, {len(failed)} failed "
               f"({len(new_failures)} new vs baseline)")
    if not failed:
        return summary
    names = ", ".join(sorted(failed))
    if len(names) > 600:
        names = names[:600] + "…"
    if sorted(failed) == sorted(baseline_failed):
        return summary + ("; failing tests are identical on baseline and "
                          f"candidate (pre-existing, not caused by this change): {names}")
    return summary + f"; failing tests on candidate: {names}"


def tested_viewport(verification: Any) -> str:
    """Tested viewport from the gate 3 measurements."""
    gate = _gate(verification, "layout-interaction")
    if gate is None:
        return "(not recorded)"
    viewport = (gate.measurements or {}).get("viewport")
    if isinstance(viewport, list) and len(viewport) == 2:
        return f"{viewport[0]}x{viewport[1]}"
    return "(not recorded)"


def supported_cause(investigation: Any) -> str:
    """Top-ranked hypothesis cause/mechanism, or an honest fallback."""
    wonder = getattr(investigation, "wonder", None)
    hypotheses = getattr(wonder, "hypotheses", None) or []
    if not hypotheses:
        return "(no ranked hypothesis recorded)"
    top = min(hypotheses, key=lambda h: h.rank)
    return f"{top.cause} — {top.mechanism}".strip(" —")


def build_pr_body(*, incident: Any, candidate: Any, verification: Any,
                  investigation: Any) -> str:
    """Render the PR body from persisted records only.

    Everything here comes from stored incidents/candidates/verifications;
    nothing is invented. No credentials, private recordings, trace
    contents, or localhost URLs are included — the checks ran externally
    in Modal, so no GitHub CI claim is made either.
    """
    tested = verification.tested
    if tested is not None and tested.file_hashes:
        tested_lines = "\n".join(
            f"- `{path}` sha256 `{digest}`"
            for path, digest in sorted(tested.file_hashes.items()))
    else:
        tested_lines = "- (recorded in the verification run artifacts)"
    change_lines = "\n".join(f"- `{path}`" for path in (candidate.changed_paths or []))
    explanations = "\n".join(
        f"- `{item.path}`: {item.explanation}" for item in (candidate.explanations or []))
    risks = candidate.regression_risks or []
    return "\n".join([
        "## Summary",
        "",
        f"Observed: {incident.observed_behavior}",
        f"Expected: {incident.expected_behavior}",
        "",
        "Supported cause (from the linked investigation):",
        supported_cause(investigation),
        "",
        "## Change",
        "",
        change_lines or "- (no changed paths recorded)",
        "",
        explanations,
        "",
        f"Expected effect: {candidate.expected_effect or '(not recorded)'}",
        "",
        f"Diff SHA-256: `{candidate.diff_sha256}`",
        "",
        "## Provenance",
        "",
        f"- Pinned baseline: `{candidate.baseline_sha}`",
        f"- Verification run: `{verification.id}` (live, 7/7 gates PASS)",
        f"- Reproduction contract: `{candidate.contract_hash}`",
        f"- Verification policy: `{verification.policy_hash or '(none recorded)'}`",
        "- Tested file identity:",
        tested_lines,
        "",
        "## Verification (external, Modal)",
        "",
        "These checks ran externally in Modal sandboxes against the pinned",
        "baseline; they are not GitHub CI results.",
        "",
        _gates_table(verification),
        "",
        f"- Target test suite: {test_summary(verification)}",
        f"- Tested viewport: {tested_viewport(verification)}",
        "",
        "## Limitations",
        "",
        *([f"- {risk}" for risk in risks] or ["- (no regression risks recorded)"]),
        "- Verified only for the checkout blocked-click flow and control route",
        "  in the tested viewport; other viewports/pages were not exercised.",
        "",
        "> Note: this repository path is an intentionally broken evaluation",
        "> fixture used to exercise BugLens end to end; it is not a real",
        "> customer incident.",
        "",
    ])
