"""Tests for Step 7 candidate patches (all providers mocked).

Covers the milestone matrix: malformed diffs, forbidden paths, traversal,
protected-file changes, incorrect baseline, stale approval, unauthorized
access, and persistence. The live Wonder call happens only in the live
run, never here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

SHA = "ab" * 20
REPO = "WANAONTOP/Buglens"
TOKEN = "test-token-0123456789abcdef01"

CSS = "#box {\n  opacity: 0;\n}\n"
HTML = '<div id="box">hi</div>\n'

VALID_DIFF = """diff --git a/public/app.css b/public/app.css
--- a/public/app.css
+++ b/public/app.css
@@ -1,3 +1,3 @@
 #box {
-  opacity: 0;
+  opacity: 1;
 }
"""


@pytest.fixture()
def patch_env(monkeypatch, tmp_path):
    """Isolated data dir + dummy (non-secret) tokens."""
    from app.patches import jobs as patch_jobs

    monkeypatch.setenv("BUGLENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BUGLENS_API_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-dummy")
    monkeypatch.setenv("META_API_KEY", "meta-dummy")
    patch_jobs.reset_for_tests()
    yield {"headers": {"Authorization": f"Bearer {TOKEN}"}}
    patch_jobs.reset_for_tests()


@pytest.fixture()
def client(patch_env):
    import app.main as main

    return TestClient(main.app)


def make_incident(**over):
    import app.store as incident_store
    from app.models import Incident

    now = incident_store.utcnow()
    incident = Incident(
        id=over.get("id", uuid.uuid4().hex),
        title=over.get("title", "t"),
        observed_behavior=over.get("observed_behavior", "click fails"),
        expected_behavior=over.get("expected_behavior", "confirmation shows"),
        reproduction_steps=over.get("reproduction_steps", ["open", "click"]),
        repo=over.get("repo", REPO),
        ref=over.get("ref", "main"),
        commit_sha=over.get("commit_sha", SHA),
        browser_viewport=None,
        permitted_source_paths=[],
        created_at=now,
        updated_at=now,
    )
    return incident_store.create_incident(incident)


def make_run(incident_id, **over):
    import app.repro.store as repro_store
    from app.repro.models import ReproRun

    now = repro_store.utcnow()
    run = ReproRun(
        id=over.get("id", uuid.uuid4().hex), incident_id=incident_id, repo=REPO,
        commit_sha=over.get("sha", SHA), contract_hash=over.get("contract", "c" * 64),
        state=over.get("state", "done"), verdict=over.get("verdict", "REPRODUCED"),
        created_at=now, updated_at=now,
    )
    repro_store.create_run(run)
    repro_store.save_contract(run.id, {
        "repo": REPO, "sha": run.commit_sha, "contract_hash": run.contract_hash,
        "manifest_bytes_sha256": "m" * 64, "blob_count": 2,
        "manifest": {
            "workingDirectory": "public", "route": "/b", "controlRoute": "/c",
            "requiredElements": ["#box"],
            "expectedConfirmation": {"selector": "#ok", "textContains": "ok"},
            "permittedPatchPaths": ["public/"],
            "protectedPaths": [".buglens.json", "public/reproduce.mjs"],
        },
    })
    return run


def make_inv(incident_id, run, **over):
    import app.investigate.store as inv_store
    from app.investigate.models import (
        Investigation, ModelCall, SourceMapping, WonderHypothesis, WonderResult,
    )

    now = inv_store.utcnow()
    hyp = WonderHypothesis(
        rank=1, cause="c", mechanism="m", supporting_evidence=["E1"],
        conflicting_or_missing=[],
        locations=[SourceMapping(target="t", file="public/app.css", start_line=2,
                                 end_line=2, certainty="inferred")],
        repair_direction="r",
    )
    wonder = WonderResult(
        call=ModelCall(model="muse-wonder-1.0", status="completed"),
        hypotheses=[hyp], raw_text="{}", parsed=True,
    )
    inv = Investigation(
        id=over.get("id", uuid.uuid4().hex), incident_id=incident_id, run_id=run.id,
        repo=run.repo, commit_sha=over.get("sha", run.commit_sha),
        contract_hash=over.get("contract", run.contract_hash),
        state=over.get("state", "done"), wonder=wonder,
        mappings=[SourceMapping(target="div#box", file="public/page.html",
                                start_line=1, end_line=1, certainty="exact")],
        created_at=now, updated_at=now,
    )
    return inv_store.create(inv)


def github_transport(files=None):
    blobs = {"public/app.css": CSS, "public/page.html": HTML}
    blobs.update(files or {})
    paths = sorted(blobs)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/git/trees/" in url:
            return httpx.Response(200, json={
                "truncated": False,
                "tree": [{"path": p, "type": "blob", "mode": "100644",
                          "sha": f"{i:040x}"} for i, p in enumerate(paths)],
            })
        if "/git/blobs/" in url:
            data = blobs[paths[int(url.rsplit("/", 1)[-1], 16)]].encode()
            return httpx.Response(200, json={
                "encoding": "base64", "content": base64.b64encode(data).decode()})
        return httpx.Response(404, json={"message": "nope"})

    return httpx.MockTransport(handler)


def meta_transport(reply=None):
    if reply is None:
        reply = json.dumps({
            "diff": VALID_DIFF,
            "changes": [{"path": "public/app.css", "explanation": "restore opacity"}],
            "evidence_refs": ["E1"], "expected_effect": "clicks land",
            "regression_risks": ["none known"],
        })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "completed", "output_text": reply})

    return httpx.MockTransport(handler)


def make_candidate(incident_id, run, inv, **over):
    import app.patches.store as patch_store
    from app.patches.models import Candidate

    now = patch_store.utcnow()
    candidate = Candidate(
        id=over.get("id", uuid.uuid4().hex), incident_id=incident_id, run_id=run.id,
        investigation_id=inv.id, hypothesis_rank=1, repo=run.repo,
        baseline_sha=run.commit_sha, contract_hash=run.contract_hash,
        state=over.get("state", "queued"), created_at=now, updated_at=now,
    )
    return patch_store.create(candidate)


# ---------------------------------------------------------------- diff parse


def test_parse_diff_accepts_valid_and_rejects_garbage():
    from app.patches.validate import DiffError, parse_diff

    files = parse_diff(VALID_DIFF)
    assert [f.path for f in files] == ["public/app.css"]
    assert files[0].added == 1 and files[0].removed == 1

    bad_cases = [
        "Here is your fix:\n" + VALID_DIFF,  # preamble
        "just prose, no diff",
        VALID_DIFF.replace("diff --git a/public/app.css b/public/app.css",
                           "diff --git a/public/app.css b/public/other.css"),  # rename
        VALID_DIFF.replace("--- a/public/app.css", "--- /dev/null"),  # delete
        "diff --git a/public/new.css b/public/new.css\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/public/new.css\n@@ -0,0 +1 @@\n+x\n",  # new file
        VALID_DIFF.replace("@@ -1,3 +1,3 @@", "@@ -1,9 +1,3 @@"),  # count mismatch
        VALID_DIFF.replace("public/app.css", "/abs/app.css"),  # absolute
        VALID_DIFF.replace("public/app.css", "../escape.css"),  # traversal
        "diff --git a/public/a.bin b/public/a.bin\nBinary files differ\n",  # binary
        VALID_DIFF + "trailing garbage line\n",  # trailing text
    ]
    for text in bad_cases:
        with pytest.raises(DiffError):
            parse_diff(text)


def test_validate_diff_scope_and_applicability(monkeypatch):
    import app.config as config
    from app.patches.validate import validate_diff

    pinned = {"public/app.css": CSS.splitlines(), "public/page.html": HTML.splitlines()}
    types = {p: "blob" for p in pinned}
    kw = dict(permitted=["public/"], protected=[".buglens.json", "public/reproduce.mjs"],
              pinned=pinned, entry_types=types)

    result, changed = validate_diff(VALID_DIFF, **kw)
    assert result.ok and changed == ["public/app.css"]

    outside = VALID_DIFF.replace("public/app.css", "other/app.css")
    result, _ = validate_diff(outside, **kw)
    assert not result.ok and any("outside" in r for r in result.reasons)

    guarded = VALID_DIFF.replace("public/app.css", "public/reproduce.mjs")
    result, _ = validate_diff(guarded, **{**kw, "pinned": {**pinned, "public/reproduce.mjs": CSS.splitlines()},
                                          "entry_types": {**types, "public/reproduce.mjs": "blob"}})
    assert not result.ok and any("protected" in r for r in result.reasons)

    manifest = VALID_DIFF.replace("public/app.css", ".buglens.json")
    result, _ = validate_diff(manifest, **{**kw, "permitted": [""]})
    assert not result.ok  # backstop holds even with a permissive manifest

    wrong_base = VALID_DIFF.replace("  opacity: 0;", "  opacity: 9;")
    result, _ = validate_diff(wrong_base, **kw)
    assert not result.ok and any("mismatch" in r for r in result.reasons)

    symlink_kw = {**kw, "entry_types": {**types, "public/app.css": "symlink"}}
    result, _ = validate_diff(VALID_DIFF, **symlink_kw)
    assert not result.ok and any("symlink" in r for r in result.reasons)

    monkeypatch.setattr(config, "PATCH_MAX_CHANGED_LINES", 1)
    result, _ = validate_diff(VALID_DIFF, **kw)
    assert not result.ok and any("changed lines" in r for r in result.reasons)


# ---------------------------------------------------------------- pipeline


def test_full_pipeline_proposes_valid_candidate(patch_env):
    import app.patches.store as patch_store
    from app.patches.runner import run_candidate

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    candidate = make_candidate(incident.id, run, inv)
    run_candidate(candidate.id, threading.Event(), meta_transport(), github_transport())
    final = patch_store.get(candidate.id)
    assert final is not None
    assert final.state == "proposed", final.detail
    assert final.diff_sha256 == hashlib.sha256(final.diff.encode()).hexdigest()
    assert final.changed_paths == ["public/app.css"]
    assert final.validation is not None and final.validation.ok
    assert final.provider is not None and final.provider.model == "muse-wonder-1.0"
    assert final.explanations and final.evidence_refs == ["E1"]


def test_malformed_reply_and_bad_diff_block(patch_env):
    import app.patches.store as patch_store
    from app.patches.runner import run_candidate

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    candidate = make_candidate(incident.id, run, inv)
    run_candidate(candidate.id, threading.Event(),
                  meta_transport("not json"), github_transport())
    final = patch_store.get(candidate.id)
    assert final is not None and final.state == "blocked"
    assert "not valid JSON" in final.detail
    assert final.raw_text == "not json"

    evil = VALID_DIFF.replace("public/app.css", "../evil.css")
    reply = json.dumps({"diff": evil, "changes": [], "evidence_refs": [],
                        "expected_effect": "", "regression_risks": []})
    candidate2 = make_candidate(incident.id, run, inv)
    run_candidate(candidate2.id, threading.Event(),
                  meta_transport(reply), github_transport())
    final2 = patch_store.get(candidate2.id)
    assert final2 is not None and final2.state == "blocked"
    assert final2.validation is not None and not final2.validation.ok


def test_prereq_mismatch_blocks(patch_env):
    import app.patches.store as patch_store
    from app.patches.runner import run_candidate

    incident = make_incident()
    run = make_run(incident.id, verdict="NEEDS_REPRODUCTION")
    inv = make_inv(incident.id, run)
    candidate = make_candidate(incident.id, run, inv)
    run_candidate(candidate.id, threading.Event(), meta_transport(), github_transport())
    final = patch_store.get(candidate.id)
    assert final is not None and final.state == "blocked"
    assert "REPRODUCED" in final.detail

    run2 = make_run(incident.id)
    inv2 = make_inv(incident.id, run2, sha="c" * 40)  # drifted investigation
    candidate2 = make_candidate(incident.id, run2, inv2)
    run_candidate(candidate2.id, threading.Event(), meta_transport(), github_transport())
    final2 = patch_store.get(candidate2.id)
    assert final2 is not None and final2.state == "blocked"
    assert "drift" in final2.detail


# ---------------------------------------------------------------- endpoints


def test_start_validates_and_limits_attempts(client, patch_env):
    import app.patches.store as patch_store

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    headers = patch_env["headers"]

    resp = client.post(f"/api/incidents/{uuid.uuid4().hex}/candidates",
                       json={"investigation_id": inv.id}, headers=headers)
    assert resp.status_code == 404
    resp = client.post(f"/api/incidents/{incident.id}/candidates",
                       json={"investigation_id": uuid.uuid4().hex}, headers=headers)
    assert resp.status_code == 404
    resp = client.post(f"/api/incidents/{incident.id}/candidates",
                       json={"investigation_id": inv.id, "hypothesis_rank": 9}, headers=headers)
    assert resp.status_code == 422
    resp = client.post(f"/api/incidents/{incident.id}/candidates",
                       json={"investigation_id": inv.id})
    assert resp.status_code == 401

    draft = make_incident(repo=None, ref=None, commit_sha=None)
    run_draft = make_run(draft.id)
    inv_draft = make_inv(draft.id, run_draft)
    resp = client.post(f"/api/incidents/{draft.id}/candidates",
                       json={"investigation_id": inv_draft.id}, headers=headers)
    assert resp.status_code == 422

    for _ in range(3):
        make_candidate(incident.id, run, inv, state="proposed")
    resp = client.post(f"/api/incidents/{incident.id}/candidates",
                       json={"investigation_id": inv.id}, headers=headers)
    assert resp.status_code == 409
    assert "budget" in resp.json()["detail".lower()]


def _proposed_candidate(incident_id, run, inv, diff=VALID_DIFF):
    import app.patches.store as patch_store
    from app.patches.models import ValidationResult

    candidate = make_candidate(incident_id, run, inv, state="proposed")
    digest = hashlib.sha256(diff.encode()).hexdigest()

    def mutate(item):
        item.diff = diff
        item.diff_sha256 = digest
        item.changed_paths = ["public/app.css"]
        item.validation = ValidationResult(ok=True)

    patch_store.update(candidate.id, mutate)
    return patch_store.get(candidate.id)


def test_approve_reject_and_stale_binding(client, patch_env):
    import app.patches.store as patch_store

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    headers = patch_env["headers"]

    candidate = _proposed_candidate(incident.id, run, inv)
    digest = hashlib.sha256(VALID_DIFF.encode()).hexdigest()
    resp = client.post(f"/api/candidates/{candidate.id}/approve",
                       json={"diff_sha256": digest, "note": "looks right"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "approved"

    # Second decision on a decided candidate is refused.
    resp = client.post(f"/api/candidates/{candidate.id}/reject",
                       json={"note": "changed mind"}, headers=headers)
    assert resp.status_code == 409

    # Tampered diff invalidates approval.
    victim = _proposed_candidate(incident.id, run, inv)
    patch_store.update(victim.id, lambda item: setattr(item, "diff", victim.diff + "\n# evil\n"))
    resp = client.post(f"/api/candidates/{victim.id}/approve",
                       json={"diff_sha256": digest}, headers=headers)
    assert resp.status_code == 409
    assert "hash" in resp.json()["Detail".lower()]

    # Wrong hash presented is refused even when bytes are intact.
    other = _proposed_candidate(incident.id, run, inv)
    resp = client.post(f"/api/candidates/{other.id}/approve",
                       json={"diff_sha256": "0" * 64}, headers=headers)
    assert resp.status_code == 409

    # Reject flow records the reason.
    dupe = _proposed_candidate(incident.id, run, inv)
    resp = client.post(f"/api/candidates/{dupe.id}/reject",
                       json={"note": "too broad"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["state"] == "rejected"

    # Unauthorized review is refused.
    fresh = _proposed_candidate(incident.id, run, inv)
    assert client.post(f"/api/candidates/{fresh.id}/approve",
                       json={"diff_sha256": digest}).status_code == 401


def test_get_list_persist_and_guards(client, patch_env):
    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    candidate = _proposed_candidate(incident.id, run, inv)
    headers = patch_env["headers"]

    resp = client.get(f"/api/candidates/{candidate.id}", headers=headers)
    assert resp.status_code == 200 and resp.json()["diff"] == VALID_DIFF
    resp = client.get(f"/api/incidents/{incident.id}/candidates", headers=headers)
    assert resp.status_code == 200 and len(resp.json()) == 1
    bad = uuid.uuid4().hex
    assert client.get(f"/api/candidates/{bad}", headers=headers).status_code == 404
    assert client.get(f"/api/incidents/{bad}/candidates", headers=headers).status_code == 404
    assert client.get(f"/api/candidates/{bad}").status_code == 401
    orphan = make_candidate(incident.id, run, inv, state="generating")
    resp = client.get(f"/api/candidates/{orphan.id}", headers=headers)
    assert resp.json()["state"] == "blocked"
