"""Frozen verification policy: thresholds fixed before execution.

The policy binds the measurements every gate is judged by — viewports,
pixel tolerances, protected screenshot regions, required elements, the
expected confirmation, and the Gate 4 suite command — plus the manifest
scope it was derived from. It is hashed (sha256 over canonical JSON)
before any sandbox runs; candidate code cannot widen exclusions or move
thresholds afterwards, and the hash is persisted on the run for audit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class VerifyPolicy:
    viewports: tuple[tuple[int, int], ...]
    pixel_diff_fraction: float
    pixel_channel_tolerance: int
    protected_regions: tuple[tuple[int, int, int, int], ...]  # x,y,w,h exclusions
    required_elements: tuple[str, ...]
    expected_selector: str
    expected_text: str
    gate4_command: tuple[str, ...]
    permitted: tuple[str, ...]
    protected: tuple[str, ...]
    policy_hash: str


def freeze_policy(
    *,
    manifest: dict,
    pixel_diff_fraction: float,
    pixel_channel_tolerance: int,
) -> VerifyPolicy:
    """Build and hash the policy from the frozen manifest plus tolerances."""
    viewport = manifest.get("viewport", {})
    try:
        viewports = ((int(viewport["width"]), int(viewport["height"])),)
    except (KeyError, TypeError, ValueError):
        raise ValueError("frozen manifest has no usable viewport")
    expected = manifest.get("expectedConfirmation", {})
    body = {
        "viewports": [list(v) for v in viewports],
        "pixel_diff_fraction": pixel_diff_fraction,
        "pixel_channel_tolerance": pixel_channel_tolerance,
        "protected_regions": [],
        "required_elements": list(manifest.get("requiredElements", [])),
        "expected_selector": str(expected.get("selector", "")),
        "expected_text": str(expected.get("textContains", "")),
        "gate4_command": ["python", "-m", "pytest", "backend/tests", "-q", "--tb=no", "-rf",
                          "-p", "no:cacheprovider"],
        "permitted": list(manifest.get("permittedPatchPaths", [])),
        "protected": list(manifest.get("protectedPaths", [])),
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return VerifyPolicy(
        viewports=viewports,
        pixel_diff_fraction=pixel_diff_fraction,
        pixel_channel_tolerance=pixel_channel_tolerance,
        protected_regions=(),
        required_elements=tuple(body["required_elements"]),
        expected_selector=body["expected_selector"],
        expected_text=body["expected_text"],
        gate4_command=tuple(body["gate4_command"]),
        permitted=tuple(body["permitted"]),
        protected=tuple(body["protected"]),
        policy_hash=digest,
    )


def policy_to_json(policy: VerifyPolicy) -> str:
    """Canonical JSON rendering of the frozen policy (persisted as evidence)."""
    return json.dumps(
        {
            "viewports": [list(v) for v in policy.viewports],
            "pixel_diff_fraction": policy.pixel_diff_fraction,
            "pixel_channel_tolerance": policy.pixel_channel_tolerance,
            "protected_regions": [list(r) for r in policy.protected_regions],
            "required_elements": list(policy.required_elements),
            "expected_selector": policy.expected_selector,
            "expected_text": policy.expected_text,
            "gate4_command": list(policy.gate4_command),
            "permitted": list(policy.permitted),
            "protected": list(policy.protected),
            "policy_hash": policy.policy_hash,
        },
        indent=2,
        sort_keys=True,
    )
