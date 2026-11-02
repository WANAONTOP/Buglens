"""Step 4 reproduction-contract tests: the real manifest validates cleanly."""

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.manifest import BugLensManifest, check_repository, load_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]


def real_manifest_dict():
    import json

    with open(REPO_ROOT / ".buglens.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_real_manifest_validates_and_matches_repo():
    manifest = load_manifest(REPO_ROOT / ".buglens.json")
    assert manifest.workingDirectory == "fixtures/storefront"
    assert manifest.route != manifest.controlRoute
    assert check_repository(manifest, REPO_ROOT) == []


def test_manifest_rejects_traversal_and_absolute_paths():
    payload = real_manifest_dict()
    payload["workingDirectory"] = "../escape"
    with pytest.raises(ValidationError):
        BugLensManifest(**payload)
    payload = real_manifest_dict()
    payload["permittedPatchPaths"] = ["/abs/path/"]
    with pytest.raises(ValidationError):
        BugLensManifest(**payload)


def test_manifest_requires_self_protection():
    payload = real_manifest_dict()
    payload["protectedPaths"] = [p for p in payload["protectedPaths"] if p != ".buglens.json"]
    with pytest.raises(ValidationError):
        BugLensManifest(**payload)


def test_manifest_rejects_unknown_or_malformed_actions():
    payload = real_manifest_dict()
    payload["actions"] = [{"action": "runScript", "selector": "x"}]
    with pytest.raises(ValidationError):
        BugLensManifest(**payload)
    payload = real_manifest_dict()
    payload["actions"] = [{"action": "click"}]
    with pytest.raises(ValidationError):
        BugLensManifest(**payload)
    payload = real_manifest_dict()
    payload["actions"] = [{"action": "goto", "selector": "x"}]
    with pytest.raises(ValidationError):
        BugLensManifest(**payload)


def test_manifest_rejects_bad_viewport_and_routes():
    payload = real_manifest_dict()
    payload["viewport"] = {"width": 10, "height": 800}
    with pytest.raises(ValidationError):
        BugLensManifest(**payload)
    payload = real_manifest_dict()
    payload["route"] = "no-leading-slash"
    with pytest.raises(ValidationError):
        BugLensManifest(**payload)


def test_repository_check_flags_escape_and_overlap():
    manifest = load_manifest(REPO_ROOT / ".buglens.json")
    hacked = copy.deepcopy(manifest)
    hacked.permittedPatchPaths = ["fixtures/"]
    problems = check_repository(hacked, REPO_ROOT)
    assert any("protected path inside permitted path" in p for p in problems)

    hacked = copy.deepcopy(manifest)
    hacked.permittedPatchPaths = ["backend/"]
    problems = check_repository(hacked, REPO_ROOT)
    assert any("escapes working directory" in p for p in problems)

    hacked = copy.deepcopy(manifest)
    hacked.protectedPaths = [".buglens.json", "fixtures/storefront/does-not-exist.js"]
    problems = check_repository(hacked, REPO_ROOT)
    assert any("protected path missing" in p for p in problems)
