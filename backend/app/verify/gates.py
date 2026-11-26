"""The seven evidence-based gates (trusted controller logic).

Each gate inspects measurements from the baseline and candidate runs and
returns PASS, FAIL, or BLOCKED with measurements and artifact IDs.
Required missing evidence never passes — it blocks. Repository-produced
reports (result.json, pytest output) are treated as untrusted inputs:
every field a gate relies on is presence- and type-checked first, and
individual assertions are recorded rather than trusting any overall
exit code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..patches.validate import _BACKSTOP_EXACT, _BACKSTOP_PREFIXES
from .models import GateResult
from .png import PngError, compare_pixels, decode_png
from .policy import VerifyPolicy

_RF_FAILED_RE = re.compile(r"^FAILED (\S+)")
_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error)")


class _Missing(Exception):
    """A required measurement is absent (gate must BLOCK, never guess)."""


@dataclass
class GateEvidence:
    bl_result: Optional[dict] = None
    cd_result: Optional[dict] = None
    shots: dict[str, bytes] = field(default_factory=dict)
    gate4: dict[str, dict] = field(default_factory=dict)  # bl/cd -> outputs
    changed_paths: list[str] = field(default_factory=list)
    tested_hashes: dict[str, str] = field(default_factory=dict)
    expected_hashes: dict[str, str] = field(default_factory=dict)
    diff_sha256: str = ""
    approved_sha256: str = ""


def _route(result: Optional[dict], label: str) -> dict:
    if not isinstance(result, dict):
        raise _Missing("result.json is not an object")
    routes = result.get("routes")
    if not isinstance(routes, dict):
        raise _Missing("result has no routes object")
    route = routes.get(label)
    if not isinstance(route, dict):
        raise _Missing(f"result has no {label} route")
    return route


def _center_hit(route: dict) -> bool:
    points = (route.get("hitTest") or {}).get("points")
    if not isinstance(points, list) or len(points) != 9:
        raise _Missing("hit-test grid is not 9 points")
    center = points[4]
    if not isinstance(center, dict) or "hitsButton" not in center:
        raise _Missing("hit-test center has no hitsButton flag")
    return bool(center["hitsButton"])


def _rect(route: dict, key: str) -> dict:
    node = route.get(key)
    if not isinstance(node, dict) or not isinstance(node.get("rect"), dict):
        raise _Missing(f"{key} geometry is missing")
    rect = node["rect"]
    if not all(isinstance(rect.get(k), (int, float)) for k in ("x", "y", "width", "height")):
        raise _Missing(f"{key} rect is not numeric")
    return rect


def _gate(num: int, name: str, verdict: str, detail: str,
          measurements: Optional[dict] = None, artifacts: Optional[list[str]] = None) -> GateResult:
    return GateResult(gate=num, name=name, verdict=verdict, detail=detail,  # type: ignore[arg-type]
                      measurements=measurements or {}, artifacts=artifacts or [])


def gate_reproduction(ev: GateEvidence, policy: VerifyPolicy) -> GateResult:
    """Gate 1: frozen actions fail-blocked on baseline, pass on candidate."""
    name = "reproduction"
    artifacts = ["bl-result.json", "cd-result.json"]
    try:
        bl_broken = _route(ev.bl_result, "broken")
        cd_broken = _route(ev.cd_result, "broken")
        bl_control = _route(ev.bl_result, "control")
        cd_control = _route(ev.cd_result, "control")
        checks = {
            "baseline button present": bool(bl_broken.get("buttonPresent")),
            "baseline click intercepted": not _center_hit(bl_broken),
            "baseline click failed": not bool(bl_broken.get("clicked")),
            "baseline no confirmation": not bool(bl_broken.get("confirmationObserved")),
            "candidate clicked": bool(cd_broken.get("clicked")),
            "candidate confirmed": bool(cd_broken.get("confirmationObserved")),
            "candidate text matches": policy.expected_text in str(cd_broken.get("confirmationText") or ""),
            "baseline control ok": bool(bl_control.get("clicked")) and bool(bl_control.get("confirmationObserved")),
            "candidate control ok": bool(cd_control.get("clicked")) and bool(cd_control.get("confirmationObserved")),
        }
    except _Missing as exc:
        return _gate(1, name, "BLOCKED", f"missing evidence: {exc}", {}, artifacts)
    failed = [k for k, v in checks.items() if not v]
    if failed:
        return _gate(1, name, "FAIL", f"failed assertions: {'; '.join(failed)}", checks, artifacts)
    return _gate(1, name, "PASS", "baseline fails blocked, candidate + controls confirm", checks, artifacts)


def gate_target(ev: GateEvidence, policy: VerifyPolicy) -> GateResult:
    """Gate 2: feature intact; confirmation comes from the real interaction."""
    name = "target-correctness"
    artifacts = ["cd-result.json", "cd-broken-before.png", "cd-broken-after.png",
                 "bl-broken-before.png", "bl-broken-after.png"]
    try:
        cd_broken = _route(ev.cd_result, "broken")
        if not bool(cd_broken.get("buttonPresent")):
            return _gate(2, name, "FAIL", "candidate pay button is missing", {}, artifacts)
        style = (cd_broken.get("button") or {}) if isinstance(cd_broken.get("button"), dict) else {}
        button_style = style.get("style") if isinstance(style, dict) else None
        if not isinstance(button_style, dict):
            raise _Missing("candidate button style is missing")
        if button_style.get("visibility") != "visible" or button_style.get("display") == "none":
            return _gate(2, name, "FAIL", "candidate button is hidden", {}, artifacts)
        if cd_broken.get("missingElements"):
            return _gate(2, name, "FAIL",
                         f"candidate missing elements: {cd_broken['missingElements']}", {}, artifacts)
        if policy.expected_text not in str(cd_broken.get("confirmationText") or ""):
            return _gate(2, name, "FAIL", "candidate confirmation text does not match", {}, artifacts)
        for key in artifacts[1:]:
            if key not in ev.shots:
                raise _Missing(f"screenshot {key} was not captured")
        cd_before = decode_png(ev.shots["cd-broken-before.png"])
        cd_after = decode_png(ev.shots["cd-broken-after.png"])
        bl_before = decode_png(ev.shots["bl-broken-before.png"])
        bl_after = decode_png(ev.shots["bl-broken-after.png"])
        cd_diff, _, cd_frac = compare_pixels(cd_before, cd_after,
                                            channel_tolerance=policy.pixel_channel_tolerance)
        bl_diff, _, bl_frac = compare_pixels(bl_before, bl_after,
                                            channel_tolerance=policy.pixel_channel_tolerance)
    except _Missing as exc:
        return _gate(2, name, "BLOCKED", f"missing evidence: {exc}", {}, artifacts)
    except PngError as exc:
        return _gate(2, name, "BLOCKED", f"screenshot unreadable: {exc}", {}, artifacts)
    measurements = {"candidate_before_after_frac": cd_frac, "baseline_before_after_frac": bl_frac}
    if cd_diff == 0:
        return _gate(2, name, "FAIL",
                     "candidate before/after frames identical: no interaction effect visible",
                     measurements, artifacts)
    if bl_frac > policy.pixel_diff_fraction:
        return _gate(2, name, "FAIL",
                     f"baseline before/after differ ({bl_frac:.4f}): no-op inconsistency",
                     measurements, artifacts)
    return _gate(2, name, "PASS",
                 "feature present; candidate change is interaction-caused", measurements, artifacts)


def gate_layout(ev: GateEvidence, policy: VerifyPolicy) -> GateResult:
    """Gate 3: explicit geometry/interaction assertions at declared viewports."""
    name = "layout-interaction"
    artifacts = ["cd-result.json"]
    try:
        cd_broken = _route(ev.cd_result, "broken")
        cd_control = _route(ev.cd_result, "control")
        rect = _rect(cd_broken, "button")
        vw, vh = policy.viewports[0]
        checks = {
            "button inside viewport": (
                rect["x"] >= 0 and rect["y"] >= 0
                and rect["x"] + rect["width"] <= vw and rect["y"] + rect["height"] <= vh),
            "button has size": rect["width"] > 0 and rect["height"] > 0,
            "required elements present": not cd_broken.get("missingElements"),
            "center hits button": _center_hit(cd_broken),
            "control route intact": bool(cd_control.get("clicked")) and bool(cd_control.get("confirmationObserved")),
        }
        measurements = {"button_rect": rect, "viewport": [vw, vh], **{k: v for k, v in checks.items()}}
    except _Missing as exc:
        return _gate(3, name, "BLOCKED", f"missing evidence: {exc}", {}, artifacts)
    failed = [k for k, v in checks.items() if not v]
    if failed:
        return _gate(3, name, "FAIL", f"failed assertions: {'; '.join(failed)}", measurements, artifacts)
    return _gate(3, name, "PASS", "geometry and interaction hold at 1280x800", measurements, artifacts)


def parse_pytest_rf(output: str) -> tuple[bool, list[str]]:
    """Parse `pytest -q --tb=no -rf` output: (completed, failed node IDs)."""
    if output is None:
        return False, []
    completed = bool(_SUMMARY_RE.search(output))
    failed = [m.group(1) for line in output.splitlines() if (m := _RF_FAILED_RE.match(line.strip()))]
    # An internal error or collection failure still "completes" the run
    # only when pytest prints its summary; otherwise completion is unknown.
    return completed, failed


def parse_collect_ids(output: str) -> list[str]:
    """Parse `pytest --collect-only -q` node IDs (best effort, order kept)."""
    ids: list[str] = []
    if not output:
        return ids
    for line in output.splitlines():
        stripped = line.strip()
        if "::" in stripped and not stripped.startswith(("=", "-", "_")):
            ids.append(stripped.split()[0])
    # Deduplicate, keep order.
    return list(dict.fromkeys(ids))


def gate_repo_tests(ev: GateEvidence, policy: VerifyPolicy) -> GateResult:
    """Gate 4: same pinned repo suite on both checkouts; no new failures."""
    name = "repo-tests"
    artifacts = ["gate4-bl-tests.txt", "gate4-cd-tests.txt"]
    bl = ev.gate4.get("bl") or {}
    cd = ev.gate4.get("cd") or {}
    if not bl.get("tests") or not cd.get("tests"):
        return _gate(4, name, "BLOCKED", "pytest output missing for a checkout", {}, artifacts)
    bl_done, bl_failed = parse_pytest_rf(bl["tests"])
    cd_done, cd_failed = parse_pytest_rf(cd["tests"])
    measurements = {
        "baseline_completed": bl_done, "candidate_completed": cd_done,
        "baseline_failed": bl_failed, "candidate_failed": cd_failed,
        "baseline_collected": len(parse_collect_ids(bl.get("collect") or "")),
        "candidate_collected": len(parse_collect_ids(cd.get("collect") or "")),
        "command": list(policy.gate4_command),
    }
    if not (bl_done and cd_done):
        return _gate(4, name, "BLOCKED", "a suite run did not complete", measurements, artifacts)
    new_failures = [t for t in cd_failed if t not in bl_failed]
    measurements["new_failures"] = new_failures
    if new_failures:
        return _gate(4, name, "FAIL", f"new failures: {'; '.join(new_failures)}",
                     measurements, artifacts)
    return _gate(4, name, "PASS",
                 f"no new failures ({len(cd_failed)} candidate failures, all pre-existing)",
                 measurements, artifacts)


def _error_set(route: dict) -> set[str]:
    errors = list(route.get("consoleErrors") or []) + list(route.get("pageErrors") or [])
    return {str(e) for e in errors}


def gate_runtime_errors(ev: GateEvidence, policy: VerifyPolicy) -> GateResult:
    """Gate 5: no new runtime errors on the candidate flow."""
    name = "runtime-errors"
    artifacts = ["bl-result.json", "cd-result.json"]
    try:
        bl_broken = _route(ev.bl_result, "broken")
        bl_control = _route(ev.bl_result, "control")
        cd_broken = _route(ev.cd_result, "broken")
        cd_control = _route(ev.cd_result, "control")
        for label, route in (("bl/broken", bl_broken), ("bl/control", bl_control),
                             ("cd/broken", cd_broken), ("cd/control", cd_control)):
            if "consoleErrors" not in route or "pageErrors" not in route:
                raise _Missing(f"{label} error telemetry is missing")
    except _Missing as exc:
        return _gate(5, name, "BLOCKED", f"missing evidence: {exc}", {}, artifacts)
    bl_errors = _error_set(bl_broken) | _error_set(bl_control)
    cd_errors = _error_set(cd_broken) | _error_set(cd_control)
    new_errors = sorted(cd_errors - bl_errors)
    measurements = {"baseline_errors": sorted(bl_errors), "candidate_errors": sorted(cd_errors),
                    "new_errors": new_errors}
    if new_errors:
        return _gate(5, name, "FAIL", f"new errors: {'; '.join(new_errors)}",
                     measurements, artifacts)
    return _gate(5, name, "PASS", "no new runtime errors", measurements, artifacts)


def gate_protected_ui(ev: GateEvidence, policy: VerifyPolicy) -> GateResult:
    """Gate 6: before-click render unchanged (no visible change is correct here)."""
    name = "protected-ui"
    artifacts = ["bl-broken-before.png", "cd-broken-before.png"]
    try:
        if "bl-broken-before.png" not in ev.shots or "cd-broken-before.png" not in ev.shots:
            raise _Missing("before-click screenshots were not captured for both runs")
        first = decode_png(ev.shots["bl-broken-before.png"])
        second = decode_png(ev.shots["cd-broken-before.png"])
        differing, compared, fraction = compare_pixels(
            first, second, channel_tolerance=policy.pixel_channel_tolerance,
            protected_regions=policy.protected_regions)
    except _Missing as exc:
        return _gate(6, name, "BLOCKED", f"missing evidence: {exc}", {}, artifacts)
    except PngError as exc:
        return _gate(6, name, "BLOCKED", f"screenshot unreadable: {exc}", {}, artifacts)
    measurements = {"differing": differing, "compared": compared, "fraction": fraction,
                    "tolerance": policy.pixel_diff_fraction}
    if fraction > policy.pixel_diff_fraction:
        return _gate(6, name, "FAIL",
                     f"before-click render differs ({fraction:.4f} > {policy.pixel_diff_fraction})",
                     measurements, artifacts)
    return _gate(6, name, "PASS",
                 "before-click render unchanged (correct: invisible repair)", measurements, artifacts)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def gate_scope(ev: GateEvidence, policy: VerifyPolicy) -> GateResult:
    """Gate 7: tested code is exactly the approved change; protected intact."""
    name = "scope-integrity"
    artifacts = ["policy.json"]
    if not ev.diff_sha256 or not ev.tested_hashes:
        return _gate(7, name, "BLOCKED", "tested code identity was not recorded", {}, artifacts)
    problems: list[str] = []
    if ev.diff_sha256 != ev.approved_sha256:
        problems.append("tested diff hash does not match the approval binding")
    for path in ev.changed_paths:
        if not any(_under(path, prefix) for prefix in policy.permitted):
            problems.append(f"{path} is outside the permitted patch paths")
        if any(_under(path, guard) or _under(guard, path) for guard in policy.protected):
            problems.append(f"{path} touches a protected path")
        if path in _BACKSTOP_EXACT or any(
            path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _BACKSTOP_PREFIXES
        ):
            problems.append(f"{path} touches a harness/test/verifier path")
        expected = ev.expected_hashes.get(path)
        if expected is None:
            problems.append(f"{path} has no expected tested hash")
        elif ev.tested_hashes.get(path) != expected:
            problems.append(f"{path} tested bytes differ from the validated patch")
    measurements = {"changed_paths": ev.changed_paths, "tested_hashes": ev.tested_hashes,
                    "diff_sha256": ev.diff_sha256}
    if problems:
        return _gate(7, name, "FAIL", "; ".join(problems), measurements, artifacts)
    return _gate(7, name, "PASS", "tested code matches the approved candidate", measurements, artifacts)


GATES = (
    (1, "reproduction", gate_reproduction),
    (2, "target-correctness", gate_target),
    (3, "layout-interaction", gate_layout),
    (4, "repo-tests", gate_repo_tests),
    (5, "runtime-errors", gate_runtime_errors),
    (6, "protected-ui", gate_protected_ui),
    (7, "scope-integrity", gate_scope),
)


def evaluate_all(ev: GateEvidence, policy: VerifyPolicy) -> list[GateResult]:
    """Run all seven gates in order; every gate always returns a verdict."""
    return [fn(ev, policy) for _, _, fn in GATES]
