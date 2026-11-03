"""Evidence package: gate the run, then collect stable evidence references.

The gate requires a ``done`` / ``REPRODUCED`` run whose SHA matches the
incident pin and whose required artifacts are all present and readable.
Anything less is ``BLOCKED`` — investigation never proceeds on missing
artifacts or inconsistent provenance.

Every emitted item gets a stable ``E<n>`` reference. Origins distinguish
uploaded developer evidence, baseline captures (frozen contract, logs),
broken-route observations, and control-route observations. Facts absent
from the capture (per-action timestamps) are listed explicitly instead of
being silently omitted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .. import config
from .. import media
from .. import store as incident_store
from ..models import Incident
from ..repro import store as repro_store
from ..repro.models import ReproRun
from .models import EvidenceItem

REQUIRED_ARTIFACTS = (
    "result.json",
    "broken-before.png",
    "control-before.png",
    "broken-trace.zip",
    "control-trace.zip",
)


class InvestigationBlocked(Exception):
    """The run cannot be investigated (gate failed, honestly reported)."""


@dataclass
class EvidencePackage:
    items: list[EvidenceItem]
    absent: list[str]
    result: dict
    broken_frame: bytes
    broken_frame_artifact: str
    broken_frame_w: int
    broken_frame_h: int


def optional_artifact_bytes(run: ReproRun, name: str) -> bytes:
    """Read an optional artifact's bytes (b"" when absent or unreadable)."""
    try:
        return _artifact_bytes(run, name)
    except (InvestigationBlocked, OSError, ValueError):
        return b""


def _artifact_bytes(run: ReproRun, name: str) -> bytes:
    artifact = next((a for a in run.artifacts if a.name == name), None)
    if artifact is None:
        raise InvestigationBlocked(f"run is missing required artifact: {name}")
    try:
        path = repro_store.safe_run_artifact_path(run.id, artifact.stored_filename)
    except ValueError:
        raise InvestigationBlocked(f"run has an unsafe stored path for: {name}")
    if not path.exists():
        raise InvestigationBlocked(f"artifact bytes missing from disk: {name}")
    return path.read_bytes()


def _png_dims(run: ReproRun, name: str) -> tuple[int, int]:
    artifact = next((a for a in run.artifacts if a.name == name), None)
    if artifact is None:  # pragma: no cover - gated earlier
        raise InvestigationBlocked(f"run is missing required artifact: {name}")
    path = repro_store.safe_run_artifact_path(run.id, artifact.stored_filename)
    try:
        info = media.identify_media(path, "image/png", "image")
    except media.MediaValidationError as exc:
        raise InvestigationBlocked(f"screenshot {name} is unreadable: {exc}")
    if info.width is None or info.height is None:  # pragma: no cover - PNG always has dims
        raise InvestigationBlocked(f"screenshot {name} has no dimensions")
    return info.width, info.height


def _rect_text(rect: object) -> str:
    if not isinstance(rect, dict):
        return "?"
    def num(key: str) -> str:
        value = rect.get(key)
        return f"{value:g}" if isinstance(value, (int, float)) else "?"
    return f"x={num('x')} y={num('y')} w={num('width')} h={num('height')}"


def _style_text(style: object) -> str:
    if not isinstance(style, dict):
        return "?"
    keys = ("position", "zIndex", "opacity", "pointerEvents", "display", "visibility")
    return ", ".join(f"{key}={style.get(key, '?')}" for key in keys)


def build_package(incident: Incident, run: ReproRun) -> EvidencePackage:
    """Gate the run and collect its evidence package (raises when blocked)."""
    if run.incident_id != incident.id:
        raise InvestigationBlocked("run does not belong to this incident")
    if run.state != "done" or run.verdict != "REPRODUCED":
        raise InvestigationBlocked(
            f"run is {run.state}/{run.verdict}; investigation requires done/REPRODUCED"
        )
    if not incident.commit_sha or run.commit_sha != incident.commit_sha:
        raise InvestigationBlocked(
            "inconsistent provenance: run SHA does not match the incident pin"
        )
    if run.repo != (incident.repo or ""):
        raise InvestigationBlocked("inconsistent provenance: run repo differs from incident")
    for name in REQUIRED_ARTIFACTS:
        if not any(a.name == name for a in run.artifacts):
            raise InvestigationBlocked(f"run is missing required artifact: {name}")
    try:
        result = json.loads(_artifact_bytes(run, "result.json").decode("utf-8"))
    except ValueError:
        raise InvestigationBlocked("driver result.json is not valid JSON")
    if not isinstance(result, dict) or not isinstance(result.get("routes"), dict):
        raise InvestigationBlocked("driver result has no routes")
    routes = result["routes"]
    if not isinstance(routes.get("broken"), dict) or not isinstance(routes.get("control"), dict):
        raise InvestigationBlocked("driver result is missing a route outcome")

    items: list[EvidenceItem] = []
    counter = 0

    def add(origin: str, kind: str, summary: str, **fields: object) -> str:
        nonlocal counter
        counter += 1
        ref = f"E{counter}"
        items.append(EvidenceItem(ref=ref, origin=origin, kind=kind, summary=summary, **fields))  # type: ignore[arg-type]
        return ref

    for evidence in incident.evidence:
        dims = ""
        if evidence.width is not None and evidence.height is not None:
            dims = f" {evidence.width}x{evidence.height}"
        add(
            "uploaded",
            "screenshot" if evidence.kind != "video" else "video",
            f"developer {evidence.kind}: {evidence.original_filename} ({evidence.size_bytes}B{dims})",
            data={"evidence_id": evidence.id, "sha256": evidence.sha256},
        )
    add(
        "baseline",
        "annotation",
        f"frozen contract at {run.commit_sha[:12]} (contract {run.contract_hash[:12]})",
        data={"commit_sha": run.commit_sha, "contract_hash": run.contract_hash},
    )
    if any(a.name == "run.log" for a in run.artifacts):
        add("baseline", "log", "reproduction command log (run.log)", artifact="run.log")

    for label, origin in (("broken", "broken"), ("control", "control")):
        route = routes[label]
        for shot in ("before", "after"):
            name = f"{label}-{shot}.png"
            if any(a.name == name for a in run.artifacts):
                width, height = _png_dims(run, name)
                add(
                    origin,
                    "screenshot",
                    f"{label} route {shot}-click frame ({width}x{height})",
                    route=label,
                    artifact=name,
                    data={"width": width, "height": height},
                )
        button = route.get("button") or {}
        occlusion = route.get("occlusion") or {}
        if isinstance(button, dict) and button.get("rect"):
            add(
                origin,
                "geometry",
                f"{label} pay button rect: {_rect_text(button.get('rect'))}",
                route=label,
                data={"rect": button.get("rect"), "tag": button.get("tag"),
                      "testid": button.get("testid"), "buglens": button.get("buglens")},
            )
            add(
                origin,
                "style",
                f"{label} pay button style: {_style_text(button.get('style'))}",
                route=label,
                data={"style": button.get("style")},
            )
        if isinstance(occlusion, dict) and occlusion.get("rect"):
            add(
                origin,
                "geometry",
                f"{label} topmost element rect: {_rect_text(occlusion.get('rect'))}",
                route=label,
                data={"rect": occlusion.get("rect"), "tag": occlusion.get("tag"),
                      "testid": occlusion.get("testid"), "buglens": occlusion.get("buglens")},
            )
            add(
                origin,
                "style",
                f"{label} topmost element style: {_style_text(occlusion.get('style'))}",
                route=label,
                data={"style": occlusion.get("style")},
            )
        points = (route.get("hitTest") or {}).get("points") or []
        if isinstance(points, list) and points:
            missed = sum(1 for p in points if isinstance(p, dict) and not p.get("hitsButton", True))
            tops = sorted({str(p.get("topTestid") or p.get("topId") or "?") for p in points if isinstance(p, dict)})
            add(
                origin,
                "hittest",
                f"{label} hit-test: {missed}/{len(points)} points miss the button "
                f"(topmost: {', '.join(tops)})",
                route=label,
                data={"points": points},
            )
        console = list(route.get("consoleErrors") or []) + list(route.get("pageErrors") or [])
        add(
            origin,
            "console",
            f"{label} console/page errors: {len(console)} captured"
            + (f" (first: {str(console[0])[:160]})" if console else ""),
            route=label,
            data={"errors": console[:5]},
        )
        if route.get("clickError"):
            add(
                origin,
                "log",
                f"{label} click outcome: clicked={bool(route.get('clicked'))}, "
                f"confirmation={bool(route.get('confirmationObserved'))}; "
                f"driver note: {str(route.get('clickError'))[:200]}",
                route=label,
                data={"clicked": bool(route.get("clicked")),
                      "confirmationObserved": bool(route.get("confirmationObserved"))},
            )
        else:
            add(
                origin,
                "log",
                f"{label} click outcome: clicked={bool(route.get('clicked'))}, "
                f"confirmation={bool(route.get('confirmationObserved'))}",
                route=label,
                data={"clicked": bool(route.get("clicked")),
                      "confirmationObserved": bool(route.get("confirmationObserved"))},
            )
        names = [a for a in (f"{label}-trace.zip",) if any(x.name == a for x in run.artifacts)]
        for name in names:
            add(origin, "trace", f"{label} full browser trace ({name})", route=label, artifact=name)
        annotations = sorted({
            str(route.get(key, {}).get("buglens") or "")
            for key in ("button", "occlusion") if isinstance(route.get(key), dict)
        } - {""})
        if annotations:
            add(
                origin,
                "annotation",
                f"{label} source annotations: {', '.join('data-buglens=' + a for a in annotations)}",
                route=label,
                data={"annotations": annotations},
            )

    if len(items) > config.INVESTIGATE_MAX_EVIDENCE_ITEMS:
        raise InvestigationBlocked("evidence package exceeds the item budget")
    absent = [
        "per-action timestamps were not captured by the driver; phase timing "
        "comes from run events and the full traces instead",
    ]
    frame_w, frame_h = _png_dims(run, "broken-before.png")
    return EvidencePackage(
        items=items,
        absent=absent,
        result=result,
        broken_frame=_artifact_bytes(run, "broken-before.png"),
        broken_frame_artifact="broken-before.png",
        broken_frame_w=frame_w,
        broken_frame_h=frame_h,
    )
