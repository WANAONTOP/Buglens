"""Tests for Step 6 investigation (all providers mocked).

Covers the B8 matrix: malformed provider output, empty segmentation,
out-of-bounds geometry, missing evidence, invalid source citations, wrong
commit identity, and unauthorized access. Real model calls happen only in
the live run, never here.
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

SHA = "ab" * 20
REPO = "WANAONTOP/Buglens"
TOKEN = "test-token-0123456789abcdef01"

HTML = """<html><body>
<button type="button" id="pay-button" data-testid="pay-button" data-buglens="pay-button">Pay now</button>
<div id="promo-overlay" data-testid="promo-overlay" data-buglens="promo-overlay"></div>
</body></html>
"""
CSS = "#promo-overlay { position: fixed; z-index: 50; opacity: 0; }\n.pay-note { color: red; }\n"
JS = 'document.getElementById("pay-button").addEventListener("click", confirm);\n'


def _points(top: str, hit: bool) -> list[dict]:
    return [{"x": 1, "y": 2, "topTag": "div", "topId": top,
             "topTestid": top, "hitsButton": hit} for _ in range(9)]


def canned_result() -> dict:
    button = {
        "tag": "button", "id": "pay-button", "testid": "pay-button", "buglens": "pay-button",
        "rect": {"x": 360, "y": 189, "width": 105, "height": 44},
        "style": {"position": "static", "zIndex": "auto", "opacity": "1",
                  "pointerEvents": "auto", "display": "inline-block", "visibility": "visible"},
    }
    overlay = {
        "tag": "div", "id": "promo-overlay", "testid": "promo-overlay",
        "buglens": "promo-overlay",
        "rect": {"x": 0, "y": 0, "width": 1280, "height": 800},
        "style": {"position": "fixed", "zIndex": "50", "opacity": "0",
                  "pointerEvents": "auto", "display": "block", "visibility": "visible"},
    }
    return {
        "routes": {
            "broken": {
                "route": "/checkout", "buttonPresent": True,
                "hitTest": {"present": True, "points": _points("promo-overlay", False)},
                "occlusion": overlay, "button": button,
                "clicked": False, "clickError": "intercepts pointer events",
                "confirmationObserved": False, "confirmationText": None,
                "consoleErrors": [], "pageErrors": [],
            },
            "control": {
                "route": "/checkout-control", "buttonPresent": True,
                "hitTest": {"present": True, "points": _points("pay-button", True)},
                "occlusion": None, "button": button,
                "clicked": True, "confirmationObserved": True,
                "confirmationText": "Order confirmed",
                "consoleErrors": [], "pageErrors": [],
            },
        },
        "driverErrors": [],
    }


@pytest.fixture()
def inv_env(monkeypatch, tmp_path):
    """Isolated data dir + dummy (non-secret) tokens."""
    from app.investigate import jobs as inv_jobs

    monkeypatch.setenv("BUGLENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BUGLENS_API_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-dummy")
    monkeypatch.setenv("META_API_KEY", "meta-dummy")
    inv_jobs.reset_for_tests()
    yield SimpleNamespace(headers={"Authorization": f"Bearer {TOKEN}"})
    inv_jobs.reset_for_tests()


@pytest.fixture()
def client(inv_env):
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


def make_run(incident_id, result=None, verdict="REPRODUCED", state="done", sha=SHA,
             artifacts=True):
    import app.repro.store as repro_store
    from app.repro.models import ReproRun

    now = repro_store.utcnow()
    run = ReproRun(
        id=uuid.uuid4().hex, incident_id=incident_id, repo=REPO,
        commit_sha=sha, contract_hash="c" * 64, state=state, verdict=verdict,
        created_at=now, updated_at=now,
    )
    repro_store.create_run(run)
    if not artifacts:
        return run
    from app.providers.meta import make_sam_test_image

    png = make_sam_test_image()
    blobs = {
        "result.json": json.dumps(result if result is not None else canned_result()).encode(),
        "broken-before.png": png,
        "control-before.png": png,
        "broken-after.png": png,
        "control-after.png": png,
        "broken-trace.zip": b"ZIP-B",
        "control-trace.zip": b"ZIP-C",
    }
    for name, data in blobs.items():
        suffix = "." + name.rsplit(".", 1)[-1]
        content = {"png": "image/png", "json": "application/json", "zip": "application/zip"}[suffix[1:]]
        repro_store.add_artifact(run.id, name, content, data, f"stored-{name}")
    return repro_store.get_run(run.id)


def github_transport(blobs=None):
    files = {"index.html": HTML, "styles.css": CSS, "app.js": JS}
    files.update(blobs or {})
    paths = sorted(files)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/git/trees/" in url:
            return httpx.Response(200, json={
                "truncated": False,
                "tree": [{"path": p, "type": "blob", "mode": "100644",
                          "sha": f"{i:040x}"} for i, p in enumerate(paths)],
            })
        if "/git/blobs/" in url:
            sha = url.rsplit("/", 1)[-1]
            data = files[paths[int(sha, 16)]].encode()
            return httpx.Response(200, json={
                "encoding": "base64", "content": base64.b64encode(data).decode()})
        return httpx.Response(404, json={"message": "nope"})

    return httpx.MockTransport(handler)


def meta_transport(spark_text=None, sam_text=None, wonder_text=None, status=200):
    bodies = {
        "muse-spark-1.3": spark_text if spark_text is not None else (
            "OBSERVATIONS:\n- button visible, no confirmation\nHYPOTHESES:\n- overlay may cover it\n"),
        "sam-3.1": sam_text if sam_text is not None else (
            "<0f>0<|box;x1=24;y1=16;x2=71;y2=47;w=96;h=64|>"
            "<|mask;x=0;y=0;data=48,32,AAAA|>"),
        "muse-wonder-1.0": wonder_text if wonder_text is not None else json.dumps([{
            "cause": "overlay intercepts clicks",
            "mechanism": "fixed full-viewport div with pointer-events auto sits above the button",
            "supporting_evidence": ["E1"],
            "conflicting_or_missing": [],
            "locations": [{"file": "styles.css", "start_line": 1, "end_line": 1}],
            "repair_direction": "make the overlay non-interactive or remove it",
        }]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content.decode())["model"]
        return httpx.Response(status, json={"status": "completed", "output_text": bodies[model],
                                            "usage": {"input_tokens": 10}})

    return httpx.MockTransport(handler)


def make_inv(incident_id, run, **over):
    import app.investigate.store as inv_store
    from app.investigate.models import Investigation

    now = inv_store.utcnow()
    inv = Investigation(
        id=over.get("id", uuid.uuid4().hex), incident_id=incident_id, run_id=run.id,
        repo=run.repo, commit_sha=run.commit_sha, contract_hash=run.contract_hash,
        state=over.get("state", "queued"), created_at=now, updated_at=now,
    )
    return inv_store.create(inv)


# ---------------------------------------------------------------- gate


def test_gate_rejects_unready_runs(inv_env):
    import app.investigate.store as inv_store
    from app.investigate.evidence import InvestigationBlocked, build_package
    from app.investigate.runner import run_investigation

    incident = make_incident()
    # Wrong verdict.
    run = make_run(incident.id, verdict="NEEDS_REPRODUCTION")
    with pytest.raises(InvestigationBlocked, match="done/REPRODUCED"):
        build_package(incident, run)
    # Missing artifacts.
    run2 = make_run(incident.id, artifacts=False)
    with pytest.raises(InvestigationBlocked, match="missing required artifact"):
        build_package(incident, run2)
    # Wrong commit identity.
    run3 = make_run(incident.id, sha="c" * 40)
    with pytest.raises(InvestigationBlocked, match="provenance"):
        build_package(incident, run3)
    # Runner records the block honestly (no model calls made).
    inv = make_inv(incident.id, run3)
    run_investigation(inv.id, threading.Event(),
                      meta_transport(), github_transport())
    final = inv_store.get(inv.id)
    assert final is not None and final.state == "blocked"
    assert "provenance" in final.detail


def test_full_pipeline_with_mocked_providers(inv_env):
    import app.investigate.store as inv_store
    from app.investigate.runner import run_investigation

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    run_investigation(inv.id, threading.Event(), meta_transport(), github_transport())
    final = inv_store.get(inv.id)
    assert final is not None
    assert final.state == "done", final.detail
    refs = [item.ref for item in final.evidence]
    assert refs == [f"E{i}" for i in range(1, len(refs) + 1)]
    assert {item.origin for item in final.evidence} >= {"baseline", "broken", "control"}
    assert final.absent_evidence  # absence notes are explicit
    assert final.spark is not None and final.spark.parsed
    assert final.spark.call.model == "muse-spark-1.3"
    assert final.spark.call.usage == {"input_tokens": 10}
    assert final.sam is not None and len(final.sam.matches) == 1
    box = final.sam.matches[0]
    assert (box.x1, box.y1, box.x2, box.y2) == (24, 16, 71, 47)
    assert (box.frame_w, box.frame_h) == (96, 64)
    assert final.mappings
    overlay = next(m for m in final.mappings if "promo-overlay" in m.target)
    assert overlay.certainty == "exact" and overlay.file == "index.html"
    assert final.wonder is not None and final.wonder.parsed
    hyp = final.wonder.hypotheses[0]
    assert hyp.cause and hyp.mechanism and hyp.repair_direction
    assert hyp.locations and hyp.locations[0].file == "styles.css"
    assert hyp.unvalidated_citations == []


# ---------------------------------------------------------------- providers


def test_malformed_model_output_is_kept_raw(inv_env):
    import app.investigate.store as inv_store
    from app.investigate.runner import run_investigation

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    run_investigation(
        inv.id, threading.Event(),
        meta_transport(spark_text="just some prose, no sections",
                       wonder_text="not json at all"),
        github_transport(),
    )
    final = inv_store.get(inv.id)
    assert final is not None and final.state == "done"
    assert final.spark is not None and not final.spark.parsed
    assert "no OBSERVATIONS" in final.spark.parse_note
    assert final.spark.raw_text == "just some prose, no sections"
    assert final.wonder is not None and not final.wonder.parsed
    assert "not valid JSON" in final.wonder.parse_note


def test_empty_segmentation_is_normal(inv_env):
    import app.investigate.store as inv_store
    from app.investigate.runner import run_investigation

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    run_investigation(inv.id, threading.Event(),
                      meta_transport(sam_text=""), github_transport())
    final = inv_store.get(inv.id)
    assert final is not None and final.state == "done"
    assert final.sam is not None and final.sam.matches == []
    assert "0 matches" in final.sam.note


def test_out_of_bounds_geometry_blocks(inv_env):
    import app.investigate.store as inv_store
    from app.investigate.runner import run_investigation
    from app.providers.meta import SamParseError, parse_sam_output

    with pytest.raises(SamParseError, match="outside"):
        parse_sam_output(
            "<0f>0<|box;x1=0;y1=0;x2=500;y2=10;w=96;h=64|><|mask;x=0;y=0;data=1,1,A|>",
            frame_w=96, frame_h=64,
        )
    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    run_investigation(
        inv.id, threading.Event(),
        meta_transport(sam_text="<0f>0<|box;x1=0;y1=0;x2=500;y2=10;w=96;h=64|>"
                                "<|mask;x=0;y=0;data=1,1,A|>"),
        github_transport(),
    )
    final = inv_store.get(inv.id)
    assert final is not None and final.state == "blocked"
    assert "outside" in final.detail


def test_invalid_citations_are_flagged(inv_env):
    import app.investigate.store as inv_store
    from app.investigate.runner import run_investigation

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    wonder_text = json.dumps([{
        "cause": "c", "mechanism": "m", "supporting_evidence": [],
        "conflicting_or_missing": [],
        "locations": [
            {"file": "nope.css", "start_line": 1, "end_line": 2},
            {"file": "styles.css", "start_line": 99, "end_line": 100},
        ],
        "repair_direction": "r",
    }])
    run_investigation(inv.id, threading.Event(),
                      meta_transport(wonder_text=wonder_text), github_transport())
    final = inv_store.get(inv.id)
    assert final is not None and final.state == "done"
    hyp = final.wonder.hypotheses[0]
    assert hyp.locations == []
    assert len(hyp.unvalidated_citations) == 2


def test_mapping_certainty_levels():
    from app.investigate.mapping import PinnedFile, map_identity

    files = [PinnedFile("index.html", HTML.splitlines()),
             PinnedFile("styles.css", CSS.splitlines()),
             PinnedFile("app.js", JS.splitlines())]
    exact = map_identity("div#promo-overlay", "promo-overlay", files, ["E2"])
    assert exact.certainty == "exact" and exact.file == "index.html"
    # Multi-line element definitions are detected (tag start line cited).
    multiline = PinnedFile("multi.html", [
        "<button", '  type="button"', '  id="multi-btn"', ">Go</button>",
    ])
    found = map_identity("button#multi-btn", "multi-btn",
                         files + [multiline], [])
    assert found.certainty == "inferred"  # defined but never referenced
    assert (found.file, found.start_line) == ("multi.html", 1)
    # Referenced in JS but never defined as an element -> inferred.
    inferred = map_identity("button#confirm", "confirm", files, [])
    assert inferred.certainty == "inferred"
    unknown = map_identity("div#ghost", "ghost", files, [])
    assert unknown.certainty == "unknown" and unknown.file is None


def test_model_call_failure_blocks_without_fabrication(inv_env, monkeypatch):
    import app.investigate.store as inv_store
    from app.investigate.runner import run_investigation

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    run_investigation(inv.id, threading.Event(),
                      meta_transport(status=500), github_transport())
    final = inv_store.get(inv.id)
    assert final is not None and final.state == "blocked"
    assert final.spark is None  # nothing stored from a failed call


# ---------------------------------------------------------------- endpoints


def test_start_validates_and_guards(client, inv_env):
    incident = make_incident()
    run = make_run(incident.id)
    # Unknown incident / run.
    resp = client.post(f"/api/incidents/{uuid.uuid4().hex}/investigations",
                       json={"run_id": run.id}, headers=inv_env.headers)
    assert resp.status_code == 404
    resp = client.post(f"/api/incidents/{incident.id}/investigations",
                       json={"run_id": uuid.uuid4().hex}, headers=inv_env.headers)
    assert resp.status_code == 404
    # Draft without target pins cannot start.
    draft = make_incident(repo=None, ref=None, commit_sha=None)
    run_draft = make_run(draft.id)
    resp = client.post(f"/api/incidents/{draft.id}/investigations",
                       json={"run_id": run_draft.id}, headers=inv_env.headers)
    assert resp.status_code == 422
    # Run of another incident is not addressable here.
    other = make_incident()
    resp = client.post(f"/api/incidents/{other.id}/investigations",
                       json={"run_id": run.id}, headers=inv_env.headers)
    assert resp.status_code == 404
    # No token -> unauthorized.
    resp = client.post(f"/api/incidents/{incident.id}/investigations",
                       json={"run_id": run.id})
    assert resp.status_code == 401


def test_get_list_cancel_flows(client, inv_env):
    import app.investigate.store as inv_store

    incident = make_incident()
    run = make_run(incident.id)
    inv = make_inv(incident.id, run, state="blocked")
    resp = client.get(f"/api/investigations/{inv.id}", headers=inv_env.headers)
    assert resp.status_code == 200 and resp.json()["state"] == "blocked"
    resp = client.get(f"/api/incidents/{incident.id}/investigations", headers=inv_env.headers)
    assert resp.status_code == 200 and len(resp.json()) == 1
    resp = client.post(f"/api/investigations/{inv.id}/cancel", headers=inv_env.headers)
    assert resp.status_code == 409  # terminal already
    bad = uuid.uuid4().hex
    assert client.get(f"/api/investigations/{bad}", headers=inv_env.headers).status_code == 404
    assert client.get(f"/api/incidents/{bad}/investigations", headers=inv_env.headers).status_code == 404
    assert client.get(f"/api/investigations/{bad}").status_code == 401
    # Orphaned active record reconciles instead of hanging.
    orphan = make_inv(incident.id, run, state="running")
    resp = client.get(f"/api/investigations/{orphan.id}", headers=inv_env.headers)
    assert resp.json()["state"] == "blocked"


def test_jobs_capacity_and_cancel():
    from app.investigate import jobs as inv_jobs

    inv_jobs.reset_for_tests()
    started = threading.Event()
    release = threading.Event()

    def slow(event):
        started.set()
        release.wait(10)

    assert inv_jobs.try_start("a", slow) is True
    assert started.wait(10)
    assert inv_jobs.has_capacity() is False
    assert inv_jobs.try_start("b", slow) is False
    assert inv_jobs.cancel("a") is True
    assert inv_jobs.cancel("missing") is False
    release.set()
