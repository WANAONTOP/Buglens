"""Spark / SAM / Wonder calls for one investigation (real model requests).

Prompts carry only the incident's own content: expected behavior, evidence
summaries with stable references, bounded source excerpts, and selected
frames. Credentials never enter a prompt. Every response is parsed
defensively: malformed output is stored raw with ``parsed=false`` and a
note — never coerced into a fabricated structure, never retried into
shape.
"""

from __future__ import annotations

import base64
import json
from typing import Optional

from .. import config
from ..models import Incident
from ..providers.meta import (
    SPARK_MODEL,
    WONDER_MODEL,
    InferenceResult,
    MetaAdapter,
    SamMatch,
)
from .evidence import EvidencePackage
from .mapping import PinnedFile, excerpt_for_wonder, validate_citation
from .models import (
    ModelCall,
    SamBox,
    SamResult,
    SourceMapping,
    SparkResult,
    WonderHypothesis,
    WonderResult,
)

_SAM_CONCEPT = "Pay now button"


def _call_meta(call: InferenceResult) -> ModelCall:
    return ModelCall(
        model=call.model,
        status=call.status,
        latency_ms=call.latency_ms,
        attempts=call.attempts,
        usage=call.usage,
    )


def _shot_ref(package: EvidencePackage, artifact: str) -> Optional[str]:
    for item in package.items:
        if item.artifact == artifact:
            return item.ref
    return None


def spark_prompt(incident: Incident, package: EvidencePackage) -> tuple[str, list[str]]:
    """Prompt text plus the evidence refs of the frames the caller must attach."""
    broken_ref = _shot_ref(package, "broken-before.png")
    control_ref = _shot_ref(package, "control-after.png")
    lines = [
        "You are inspecting two UI screenshots from a reproduced web bug.",
        f"Frame 1 ({broken_ref}): the broken route before clicking.",
        f"Frame 2 ({control_ref}): the control route after a successful click.",
        f"Expected behavior: {incident.expected_behavior}",
        "Observed on the broken route: clicking the Pay button does nothing; "
        "no confirmation appears.",
        "",
        "Reply with exactly two sections, using the evidence references:",
        "OBSERVATIONS: visible facts only (layout, button state, confirmation, "
        "anything visibly covering the button). One per line starting with '- '.",
        "HYPOTHESES: possible causes, clearly separated from observations. "
        "One per line starting with '- '. Do not claim certainty.",
    ]
    return "\n".join(lines), [r for r in (broken_ref, control_ref) if r]


def parse_spark(text: str) -> tuple[list[str], list[str], bool, str]:
    """Split a Spark reply into (observations, hypotheses, parsed, note)."""
    observations: list[str] = []
    hypotheses: list[str] = []
    section: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().rstrip(":") == "OBSERVATIONS":
            section = "obs"
            continue
        if line.upper().rstrip(":") == "HYPOTHESES":
            section = "hyp"
            continue
        if section and line.startswith("- "):
            target = observations if section == "obs" else hypotheses
            target.append(line[2:].strip())
    if section is None:
        return [], [], False, "reply has no OBSERVATIONS/HYPOTHESES sections; raw text kept"
    return observations, hypotheses, True, ""


def run_spark(
    adapter: MetaAdapter,
    incident: Incident,
    package: EvidencePackage,
    control_after: bytes,
) -> SparkResult:
    """Call Spark with the broken-before and control-after frames."""
    prompt, frame_refs = spark_prompt(incident, package)
    images = [("image/png", base64.b64encode(package.broken_frame).decode("ascii"))]
    if control_after:
        images.append(("image/png", base64.b64encode(control_after).decode("ascii")))
    call = adapter.complete(
        SPARK_MODEL, prompt, images, max_output_tokens=config.INVESTIGATE_SPARK_MAX_TOKENS
    )
    observations, hypotheses, parsed, note = parse_spark(call.text)
    return SparkResult(
        call=_call_meta(call),
        frames=frame_refs,
        observations=observations,
        hypotheses=hypotheses,
        raw_text=call.text,
        parsed=parsed,
        parse_note=note,
    )


def run_sam(adapter: MetaAdapter, package: EvidencePackage) -> SamResult:
    """Segment the concrete Pay button; never the transparent overlay.

    The overlay has opacity 0, so a visual segmenter cannot see it — its
    presence is established by DOM geometry and hit-testing instead
    (recorded in the note). Zero SAM matches is a normal result here.
    """
    matches, call = adapter.segment(
        _SAM_CONCEPT,
        package.broken_frame,
        "image/png",
        frame_w=package.broken_frame_w,
        frame_h=package.broken_frame_h,
    )
    frame_ref = _shot_ref(package, package.broken_frame_artifact) or ""
    boxes = [
        SamBox(
            object_id=match.object_id,
            x1=match.x1,
            y1=match.y1,
            x2=match.x2,
            y2=match.y2,
            frame_w=match.frame_w,
            frame_h=match.frame_h,
            mask_h=match.mask_h,
            mask_w=match.mask_w,
        )
        for match in matches
    ]
    note = (
        "segmented the visible Pay button only; the transparent overlay "
        "(opacity 0) is established by DOM/hit-test evidence, not vision. "
        + (f"{len(boxes)} match(es)." if boxes else "0 matches: button not segmented.")
    )
    return SamResult(
        call=_call_meta(call),
        concept=_SAM_CONCEPT,
        frame_ref=frame_ref,
        frame_artifact=package.broken_frame_artifact,
        frame_w=package.broken_frame_w,
        frame_h=package.broken_frame_h,
        matches=boxes,
        note=note,
    )


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    return stripped


def wonder_prompt(
    incident: Incident,
    package: EvidencePackage,
    mappings: list[SourceMapping],
    files: list[PinnedFile],
) -> str:
    """Bounded prompt: evidence refs, validated mappings, source excerpts."""
    evidence_lines = [
        f"{item.ref} [{item.origin}/{item.kind}]: {item.summary}" for item in package.items
    ]
    mapping_lines = [
        f"{m.target}: {m.certainty}"
        + (f" {m.file}:{m.start_line}-{m.end_line}" if m.file else "")
        + f" ({m.note})"
        for m in mappings
    ]
    excerpts, truncated = excerpt_for_wonder(
        mappings, files, config.INVESTIGATE_MAX_SOURCE_CHARS
    )
    return "\n".join(
        [
            "You are diagnosing a reproduced web bug. Only the evidence below "
            "is real; cite it by reference (E-numbers).",
            f"Expected behavior: {incident.expected_behavior}",
            f"Observed: {incident.observed_behavior}",
            "",
            "EVIDENCE:",
            *evidence_lines,
            "",
            "SOURCE MAPPINGS (certainty exact/inferred/unknown):",
            *(mapping_lines or ["none"]),
            "",
            "PINNED SOURCE EXCERPTS:",
            excerpts or "(no excerpts)",
            "",
            "Reply with a JSON array (no other text) of 1-3 hypotheses. Each:",
            '{"cause": "...", "mechanism": "...",',
            ' "supporting_evidence": ["E1", ...],',
            ' "conflicting_or_missing": ["..."],',
            ' "locations": [{"file": "path", "start_line": n, "end_line": m}],',
            ' "repair_direction": "one sentence, no diff"}',
            "Use only files/evidence listed above. Unknown mappings stay unknown.",
        ]
    )


def _coerce_locations(
    raw: object,
    tree_paths: list[str],
    files: list[PinnedFile],
    evidence_refs: list[str],
) -> tuple[list[SourceMapping], list[str]]:
    locations: list[SourceMapping] = []
    unvalidated: list[str] = []
    if not isinstance(raw, list):
        return locations, ["locations is not a list"]
    for entry in raw:
        if not isinstance(entry, dict):
            unvalidated.append(f"non-object location: {str(entry)[:80]}")
            continue
        cited_file = entry.get("file")
        start = entry.get("start_line")
        end = entry.get("end_line")
        ok, reason = validate_citation(
            cited_file if isinstance(cited_file, str) else None,
            start if isinstance(start, int) else None,
            end if isinstance(end, int) else None,
            tree_paths,
            files,
        )
        label = f"{cited_file}:{start}-{end}"
        if not ok:
            unvalidated.append(f"{label} ({reason})")
            continue
        assert isinstance(cited_file, str) and isinstance(start, int) and isinstance(end, int)
        locations.append(
            SourceMapping(
                target=label,
                file=cited_file,
                start_line=start,
                end_line=end,
                certainty="inferred",
                evidence_refs=evidence_refs,
                note="cited by the model; file and range validated at the pinned SHA",
            )
        )
    return locations, unvalidated


def parse_wonder(
    text: str, tree_paths: list[str], files: list[PinnedFile]
) -> tuple[list[WonderHypothesis], bool, str]:
    """Parse Wonder's JSON array; validate every citation (never invent)."""
    try:
        payload = json.loads(_strip_fences(text))
    except ValueError as exc:
        return [], False, f"reply is not valid JSON ({exc}); raw text kept"
    if not isinstance(payload, list) or not payload:
        return [], False, "reply is not a non-empty JSON array; raw text kept"
    hypotheses: list[WonderHypothesis] = []
    for rank, entry in enumerate(payload[:3], start=1):
        if not isinstance(entry, dict):
            continue
        cause = entry.get("cause")
        mechanism = entry.get("mechanism")
        if not cause or not mechanism:
            continue
        supporting = entry.get("supporting_evidence")
        supporting_refs = [str(r) for r in supporting] if isinstance(supporting, list) else []
        conflicting = entry.get("conflicting_or_missing")
        conflicting_list = [str(c) for c in conflicting] if isinstance(conflicting, list) else []
        locations, unvalidated = _coerce_locations(
            entry.get("locations"), tree_paths, files, supporting_refs
        )
        repair = entry.get("repair_direction")
        hypotheses.append(
            WonderHypothesis(
                rank=rank,
                cause=str(cause),
                mechanism=str(mechanism),
                supporting_evidence=supporting_refs,
                conflicting_or_missing=conflicting_list,
                locations=locations,
                repair_direction=str(repair or ""),
                unvalidated_citations=unvalidated,
            )
        )
    if not hypotheses:
        return [], False, "no hypothesis had both cause and mechanism; raw text kept"
    return hypotheses, True, ""


def run_wonder(
    adapter: MetaAdapter,
    incident: Incident,
    package: EvidencePackage,
    mappings: list[SourceMapping],
    files: list[PinnedFile],
    tree_paths: list[str],
) -> WonderResult:
    """Call Wonder for ranked root-cause hypotheses (no diffs)."""
    prompt = wonder_prompt(incident, package, mappings, files)
    call = adapter.complete(
        WONDER_MODEL, prompt, None, max_output_tokens=config.INVESTIGATE_WONDER_MAX_TOKENS
    )
    hypotheses, parsed, note = parse_wonder(call.text, tree_paths, files)
    return WonderResult(
        call=_call_meta(call),
        hypotheses=hypotheses,
        raw_text=call.text,
        parsed=parsed,
        parse_note=note,
    )
