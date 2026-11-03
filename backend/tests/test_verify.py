"""Tests for Step 8 seven-gate verification (Modal + GitHub mocked).

Covers gates (pass/fail/blocked each), the PNG decoder, policy freezing,
the approval gate, stale/tampered candidates, cancellation, and
endpoint guards. No real sandboxes and no real approvals are created
here; the live candidate stays unapproved.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import struct
import threading
import uuid
import zlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_BYTES = (REPO_ROOT / ".buglens.json").read_bytes()

SHA = "ab" * 20
REPO = "WANAONTOP/Buglens"
TOKEN = "test-token-0123456789abcdef01"
TARGET = "fixtures/storefront/public/checkout.html"
PINNED_HTML = "<html>\n<body>\n<div>old</div>\n</body>\n</html>\n"

DIFF = """diff --git a/fixtures/storefront/public/checkout.html b/fixtures/storefront/public/checkout.html
--- a/fixtures/storefront/public/checkout.html
+++ b/fixtures/storefront/public/checkout.html
@@ -1,5 +1,5 @@
 <html>
 <body>
-<div>old</div>
+<div>new</div>
 </body>
 </html>
"""


def encode_png(width, height, rows, channels=3):
    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(ctype, payload):
        return (struct.pack(">I", len(payload)) + ctype + payload
                + struct.pack(">I", binascii.crc32(ctype + payload) & 0xFFFFFFFF))

    color = 2 if channels == 3 else 6
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    sig = bytes.fromhex("89504e470d0a1a0a")
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


FLAT = encode_png(4, 2, [[10, 20, 30] * 4] * 2)
FLAT_OTHER = encode_png(4, 2, [[10, 20, 30] * 3 + [200, 200, 200]] + [[10, 20, 30] * 4])


def _points(top, hit):
    return [{"x": 1, "y": 2, "topTag": "div", "topId": top,
             "topTestid": top, "hitsButton": hit} for _ in range(9)]


def _button():
    return {
        "tag": "button", "id": "pay-button", "testid": "pay-button",
        "rect": {"x": 360, "y": 189, "width": 105, "height": 44},
        "style": {"position": "static", "zIndex": "auto", "opacity": "1",
                  "pointerEvents": "auto", "display": "inline-block",
                  "visibility": "visible"},
    }


def _overlay():
    return {
        "tag": "div", "id": "promo-overlay", "testid": "promo-overlay",
        "rect": {"x": 0, "y": 0, "width": 1280, "height": 800},
        "style": {"position": "fixed", "zIndex": "50", "opacity": "0",
                  "pointerEvents": "auto", "display": "block", "visibility": "visible"},
    }


def bl_result():
    return {"routes": {
        "broken": {"route": "/b", "buttonPresent": True, "missingElements": [],
                   "hitTest": {"points": _points("promo-overlay", False)},
                   "occlusion": _overlay(), "button": _button(),
                   "clicked": False, "confirmationObserved": False,
                   "confirmationText": None, "consoleErrors": [], "pageErrors": []},
        "control": {"route": "/c", "buttonPresent": True, "missingElements": [],
                    "hitTest": {"points": _points("pay-button", True)},
                    "button": _button(), "clicked": True, "confirmationObserved": True,
                    "confirmationText": "Order confirmed",
                    "consoleErrors": [], "pageErrors": []}}}


def cd_result():
    body = bl_result()
    broken = body["routes"]["broken"]
    broken["hitTest"] = {"points": _points("pay-button", True)}
    broken["occlusion"] = None
    broken["clicked"] = True
    broken["confirmationObserved"] = True
    broken["confirmationText"] = "Order confirmed: #1"
    return body


PYTEST_PASS = "[exit 0]\n....\n4 passed in 1.0s\n"
PYTEST_FAIL = "[exit 1]\n.F..\nFAILED backend/tests/test_x.py::test_y\n1 failed, 3 passed\n"
COLLECT = "backend/tests/test_x.py::test_y\nbackend/tests/test_x.py::test_z\n"


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    import app.providers.modal as modal_adapter

    monkeypatch.setattr(modal_adapter, "_local_profile_present", lambda: False)


@pytest.fixture()
def ver_env(monkeypatch, tmp_path):
    """Isolated data dir + dummy (non-secret) tokens."""
    from app.verify import jobs as verify_jobs

    monkeypatch.setenv("BUGLENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BUGLENS_API_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-dummy")
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-dummy")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-dummy")
    verify_jobs.reset_for_tests()
    yield {"headers": {"Authorization": f"Bearer {TOKEN}"}}
    verify_jobs.reset_for_tests()


@pytest.fixture()
def client(ver_env):
    import app.main as main

    return TestClient(main.app)


def make_records(**over):
    import app.store as incident_store
    from app.investigate.models import Investigation
    from app.investigate.store import utcnow as inv_now
    from app.models import Incident
    from app.repro.models import ReproRun

    now = incident_store.utcnow()
    incident = Incident(
        id=uuid.uuid4().hex, title="t", observed_behavior="o",
        expected_behavior="e", reproduction_steps=["s"],
        repo=over.get("repo", REPO), ref="main", commit_sha=over.get("sha", SHA),
        browser_viewport=None, permitted_source_paths=[],
        created_at=now, updated_at=now)
    incident_store.create_incident(incident)
    import app.repro.store as repro_store

    run = ReproRun(
        id=uuid.uuid4().hex, incident_id=incident.id, repo=REPO,
        commit_sha=over.get("sha", SHA), contract_hash="c" * 64,
        state="done", verdict="REPRODUCED", created_at=now, updated_at=now)
    repro_store.create_run(run)
    repro_store.save_contract(run.id, {
        "repo": REPO, "sha": run.commit_sha, "contract_hash": run.contract_hash,
        "manifest_bytes_sha256": hashlib.sha256(MANIFEST_BYTES).hexdigest(),
        "blob_count": 2, "manifest": json.loads(MANIFEST_BYTES.decode())})
    import app.investigate.store as inv_store

    inv = Investigation(
        id=uuid.uuid4().hex, incident_id=incident.id, run_id=run.id,
        repo=REPO, commit_sha=over.get("sha", SHA), contract_hash="c" * 64,
        state="done", created_at=inv_now(), updated_at=inv_now())
    inv_store.create(inv)
    return incident, run, inv


def make_candidate(incident, run, inv, state="approved", diff=DIFF):
    import app.patches.store as patch_store
    from app.patches.models import Approval, Candidate, ValidationResult

    now = patch_store.utcnow()
    digest = hashlib.sha256(diff.encode()).hexdigest()
    candidate = Candidate(
        id=uuid.uuid4().hex, incident_id=incident.id, run_id=run.id,
        investigation_id=inv.id, hypothesis_rank=1, repo=REPO,
        baseline_sha=run.commit_sha, contract_hash=run.contract_hash,
        state=state, diff=diff, diff_sha256=digest,
        changed_paths=[TARGET] if diff else [],
        validation=ValidationResult(ok=True) if state in ("proposed", "approved") else None,
        approval=(Approval(candidate_id="", diff_sha256=digest, approved_at=now)
                  if state == "approved" else None),
        created_at=now, updated_at=now)
    if candidate.approval is not None:
        candidate.approval.candidate_id = candidate.id
    return patch_store.create(candidate)


def github_transport():
    blobs = {".buglens.json": MANIFEST_BYTES, TARGET: PINNED_HTML.encode()}
    paths = sorted(blobs)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/git/trees/" in url:
            return httpx.Response(200, json={
                "truncated": False,
                "tree": [{"path": p, "type": "blob", "mode": "100644",
                          "sha": f"{i:040x}"} for i, p in enumerate(paths)]})
        if "/git/blobs/" in url:
            data = blobs[paths[int(url.rsplit("/", 1)[-1], 16)]]
            return httpx.Response(200, json={
                "encoding": "base64", "content": base64.b64encode(data).decode()})
        return httpx.Response(404, json={"message": "nope"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- png


def test_png_roundtrip_and_rejections():
    from app.verify.png import PngError, compare_pixels, decode_png

    decoded = decode_png(FLAT)
    assert (decoded.width, decoded.height, decoded.channels) == (4, 2, 3)
    same = decode_png(FLAT)
    diff, compared, frac = compare_pixels(decoded, same, channel_tolerance=8)
    assert (diff, compared, frac) == (0, 8, 0.0)
    other = decode_png(FLAT_OTHER)
    diff2, _, frac2 = compare_pixels(decoded, other, channel_tolerance=8)
    assert diff2 == 1 and frac2 == 1 / 8
    # Protected region excludes the differing pixel.
    _, _, frac3 = compare_pixels(decoded, other, channel_tolerance=8,
                                 protected_regions=((3, 0, 1, 1),))
    assert frac3 == 0.0
    with pytest.raises(PngError):
        decode_png(b"not a png")
    with pytest.raises(PngError):
        decode_png(FLAT[:40])
    big = encode_png(2, 2, [[0, 0, 0] * 2] * 2)
    with pytest.raises(PngError):
        compare_pixels(decoded, decode_png(big), channel_tolerance=8)


def test_policy_freezes_deterministically():
    from app.verify.policy import freeze_policy

    manifest = json.loads(MANIFEST_BYTES.decode())
    first = freeze_policy(manifest=manifest, pixel_diff_fraction=0.001,
                          pixel_channel_tolerance=8)
    second = freeze_policy(manifest=manifest, pixel_diff_fraction=0.001,
                           pixel_channel_tolerance=8)
    assert first.policy_hash == second.policy_hash and len(first.policy_hash) == 64
    assert first.viewports == ((1280, 800),)
    assert first.gate4_command[0:3] == ("python", "-m", "pytest")


# ---------------------------------------------------------------- gates


def _evidence(**over):
    from app.verify.gates import GateEvidence

    base = dict(
        bl_result=bl_result(), cd_result=cd_result(),
        shots={"bl-broken-before.png": FLAT, "bl-broken-after.png": FLAT,
               "cd-broken-before.png": FLAT, "cd-broken-after.png": FLAT_OTHER},
        gate4={"bl": {"tests": PYTEST_PASS, "collect": COLLECT},
               "cd": {"tests": PYTEST_PASS, "collect": COLLECT}},
        changed_paths=[TARGET],
        tested_hashes={TARGET: "h"}, expected_hashes={TARGET: "h"},
        diff_sha256="d", approved_sha256="d")
    base.update(over)
    return GateEvidence(**base)


def _policy():
    from app.verify.policy import freeze_policy

    return freeze_policy(manifest=json.loads(MANIFEST_BYTES.decode()),
                         pixel_diff_fraction=0.001, pixel_channel_tolerance=8)


def test_gates_all_pass_on_clean_evidence():
    from app.verify.gates import evaluate_all

    results = evaluate_all(_evidence(), _policy())
    assert [(g.gate, g.verdict) for g in results] == [(i, "PASS") for i in range(1, 8)]


def test_gates_fail_modes():
    from app.verify.gates import evaluate_all

    # Regression: candidate control broke.
    bad = cd_result()
    bad["routes"]["control"]["clicked"] = False
    assert evaluate_all(_evidence(cd_result=bad), _policy())[0].verdict == "FAIL"
    # Target removal: candidate button gone.
    gone = cd_result()
    gone["routes"]["broken"]["buttonPresent"] = False
    assert evaluate_all(_evidence(cd_result=gone), _policy())[1].verdict == "FAIL"
    # Unconditional confirmation: candidate before == after.
    same = dict(_evidence().shots, **{"cd-broken-after.png": FLAT})
    assert evaluate_all(_evidence(shots=same), _policy())[1].verdict == "FAIL"
    # New repo test failure.
    gate4 = {"bl": {"tests": PYTEST_PASS, "collect": COLLECT},
             "cd": {"tests": PYTEST_FAIL, "collect": COLLECT}}
    assert evaluate_all(_evidence(gate4=gate4), _policy())[3].verdict == "FAIL"
    # New runtime error.
    noisy = cd_result()
    noisy["routes"]["broken"]["consoleErrors"] = ["boom"]
    assert evaluate_all(_evidence(cd_result=noisy), _policy())[4].verdict == "FAIL"
    # Before-click render drifted.
    drift = dict(_evidence().shots, **{"cd-broken-before.png": FLAT_OTHER})
    assert evaluate_all(_evidence(shots=drift), _policy())[5].verdict == "FAIL"
    # Protected file touched.
    prot = _evidence(changed_paths=[".buglens.json"],
                     tested_hashes={".buglens.json": "h"},
                     expected_hashes={".buglens.json": "h"})
    assert evaluate_all(prot, _policy())[6].verdict == "FAIL"
    # Tested bytes differ from validated patch.
    tamper = _evidence(tested_hashes={TARGET: "other"})
    assert evaluate_all(tamper, _policy())[6].verdict == "FAIL"


def test_gates_block_on_missing_evidence():
    from app.verify.gates import evaluate_all

    assert evaluate_all(_evidence(bl_result=None), _policy())[0].verdict == "BLOCKED"
    assert evaluate_all(_evidence(shots={}), _policy())[1].verdict == "BLOCKED"
    assert evaluate_all(_evidence(gate4={}), _policy())[3].verdict == "BLOCKED"
    broken = bl_result()
    del broken["routes"]["broken"]["consoleErrors"]
    del broken["routes"]["broken"]["pageErrors"]
    assert evaluate_all(_evidence(bl_result=broken), _policy())[4].verdict == "BLOCKED"
    assert evaluate_all(_evidence(diff_sha256=""), _policy())[6].verdict == "BLOCKED"


# ---------------------------------------------------------------- runner


class _FakeProc:
    def __init__(self, code=0, stdout="", stderr=""):
        self._code, self._out, self._err = code, stdout, stderr

    def poll(self):
        return self._code

    @property
    def returncode(self):
        return self._code

    @property
    def stdout(self):
        from types import SimpleNamespace

        return SimpleNamespace(read=lambda: self._out)

    @property
    def stderr(self):
        from types import SimpleNamespace

        return SimpleNamespace(read=lambda: self._out)


class _FakeSandbox:
    terminated = 0
    cp_sources: list = []
    put_remotes: list = []

    def __init__(self):
        self.is_candidate = False

    def exec(self, *command, **kwargs):
        line = " ".join(command)
        if "tar -xzf" in line or "npm install" in line or "install-deps" in line:
            return _FakeProc(0, "", "")
        if "sha256sum" in line and ".buglens.json" in line:
            return _FakeProc(0, hashlib.sha256(MANIFEST_BYTES).hexdigest() + "\n", "")
        if "cp '/buglens/overlay/" in line:
            self.is_candidate = True
            # Regression guard: the cp source must be the volume path
            # (/overlay/...) resolved under the /buglens mount.
            _FakeSandbox.cp_sources.append(line)
            want = hashlib.sha256(
                PINNED_HTML.replace("<div>old</div>", "<div>new</div>").encode()).hexdigest()
            return _FakeProc(0, want + "\n", "")
        if "server.js" in line and "&" in line:
            return _FakeProc(0, "", "")
        if "fetch(" in line and "healthz" in line:
            return _FakeProc(0, "", "")
        if "driver.cjs" in line:
            return _FakeProc(0, "", "")
        if "kill $(" in line or "cat /tmp/server.log" in line:
            return _FakeProc(0, "log\n", "")
        if "stat -c %s" in line:
            name = line.rsplit("/tmp/out/", 1)[-1].rstrip("'")
            payloads = self._payloads()
            if name in payloads:
                return _FakeProc(0, f"{len(payloads[name])}\n", "")
            return _FakeProc(1, "", "missing")
        if "base64" in line:
            name = line.rsplit("/tmp/out/", 1)[-1].rstrip("'")
            payloads = self._payloads()
            if name in payloads:
                return _FakeProc(0, base64.b64encode(payloads[name]).decode(), "")
            return _FakeProc(1, "", "missing")
        if "pip install" in line:
            return _FakeProc(0, "", "")
        if "pytest" in line and "collect-only" in line:
            return _FakeProc(0, COLLECT, "")
        if "pytest" in line:
            return _FakeProc(0, "4 passed in 1.0s\n", "")
        if line.startswith("python3 -c"):
            return _FakeProc(0, "", "")
        return _FakeProc(1, "", f"unexpected: {line[:80]}")

    def _payloads(self):
        result = cd_result() if self.is_candidate else bl_result()
        before = FLAT
        after = FLAT_OTHER if self.is_candidate else FLAT
        return {
            "result.json": json.dumps(result).encode(),
            "broken-before.png": before, "broken-after.png": after,
            "control-before.png": before, "control-after.png": after,
            "broken-trace.zip": b"ZIP", "control-trace.zip": b"ZIP",
        }

    def terminate(self):
        _FakeSandbox.terminated += 1


def _fake_modal_module():
    from types import ModuleType

    mod = ModuleType("modal")

    class Image:
        @staticmethod
        def from_registry(tag):
            return object()

    class _Volume:
        def batch_upload(self):
            class _Batch:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def put_file(self, local, remote):
                    _FakeSandbox.put_remotes.append(remote)
                    return None

            return _Batch()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Volume:
        @staticmethod
        def ephemeral():
            return _Volume()

    class App:
        created = []

        @staticmethod
        def lookup(name, create_if_missing=False):
            App.created.append(name)
            return object()

    class Sandbox:
        instances = []

        @staticmethod
        def create(**kwargs):
            box = _FakeSandbox()
            Sandbox.instances.append(box)
            return box

    mod.Image, mod.Volume, mod.App, mod.Sandbox = Image, Volume, App, Sandbox
    return mod


def test_full_runner_verifies_with_fakes(ver_env, monkeypatch):
    import sys

    import app.verify.store as verify_store
    from app.verify.models import Verification
    from app.verify.runner import run_verification

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    now = verify_store.utcnow()
    record = Verification(
        id=uuid.uuid4().hex, incident_id=incident.id, run_id=run.id,
        investigation_id=inv.id, candidate_id=candidate.id, repo=REPO,
        baseline_sha=run.commit_sha, contract_hash=run.contract_hash,
        state="queued", created_at=now, updated_at=now)
    verify_store.create(record)
    _FakeSandbox.terminated = 0
    _FakeSandbox.cp_sources = []
    _FakeSandbox.put_remotes = []
    monkeypatch.setitem(sys.modules, "modal", _fake_modal_module())
    run_verification(record.id, threading.Event(), github_transport())
    final = verify_store.get(record.id)
    assert final is not None
    assert final.state == "done", final.detail
    # Every overlay cp reads the uploaded path under the /buglens mount.
    uploaded = {remote for remote in _FakeSandbox.put_remotes if remote.startswith("/overlay/")}
    assert uploaded, "candidate files were never uploaded"
    for source in _FakeSandbox.cp_sources:
        assert any(f"/buglens{remote}" in source for remote in uploaded), source
    assert final.outcome == "VERIFIED", [f"{g.gate}:{g.verdict}" for g in final.gates]
    assert [g.verdict for g in final.gates] == ["PASS"] * 7
    assert final.policy_hash and final.tested is not None
    assert final.tested.diff_sha256 == candidate.diff_sha256
    assert _FakeSandbox.terminated == 4
    assert {a.name for a in final.artifacts} >= {
        "policy.json", "bl-result.json", "cd-result.json",
        "bl-broken-before.png", "cd-broken-after.png",
        "gate4-bl-tests.txt", "gate4-cd-tests.txt", "run.log"}
    assert "buglens-verify" in sys.modules["modal"].App.created


def test_runner_refuses_unapproved_and_drifted(ver_env, monkeypatch):
    import sys

    import app.verify.store as verify_store
    from app.verify.models import Verification
    from app.verify.runner import run_verification

    def _record_for(candidate, incident, run, inv):
        now = verify_store.utcnow()
        record = Verification(
            id=uuid.uuid4().hex, incident_id=incident.id, run_id=run.id,
            investigation_id=inv.id, candidate_id=candidate.id, repo=REPO,
            baseline_sha=run.commit_sha, contract_hash=run.contract_hash,
            state="queued", created_at=now, updated_at=now)
        return verify_store.create(record)

    incident, run, inv = make_records()
    proposed = make_candidate(incident, run, inv, state="proposed")
    record = _record_for(proposed, incident, run, inv)
    monkeypatch.setitem(sys.modules, "modal", _fake_modal_module())
    run_verification(record.id, threading.Event(), github_transport())
    final = verify_store.get(record.id)
    assert final is not None and final.state == "blocked"
    assert "not approved" in final.detail
    assert final.outcome == "BLOCKED"


# ---------------------------------------------------------------- endpoints


def test_start_guards_and_flows(client, ver_env, monkeypatch):
    import app.routes_verify as routes
    import app.verify.store as verify_store

    incident, run, inv = make_records()
    headers = ver_env["headers"]
    approved = make_candidate(incident, run, inv, state="approved")
    proposed = make_candidate(incident, run, inv, state="proposed")

    assert client.post(f"/api/candidates/{uuid.uuid4().hex}/verifications",
                       headers=headers).status_code == 404
    resp = client.post(f"/api/candidates/{proposed.id}/verifications", headers=headers)
    assert resp.status_code == 409  # unapproved
    resp = client.post(f"/api/candidates/{approved.id}/verifications")
    assert resp.status_code == 401

    # Tampered diff invalidates the start.
    import app.patches.store as patch_store

    patch_store.update(approved.id, lambda item: setattr(item, "diff", item.diff + "\n"))
    resp = client.post(f"/api/candidates/{approved.id}/verifications", headers=headers)
    assert resp.status_code == 409

    # Approved start submits (worker stubbed; no Modal here).
    fresh = make_candidate(incident, run, inv, state="approved")
    monkeypatch.setattr(routes, "run_verification", lambda vid, event: None)
    resp = client.post(f"/api/candidates/{fresh.id}/verifications", headers=headers)
    assert resp.status_code == 202, resp.text
    vid = resp.json()["id"]
    import time

    time.sleep(0.5)  # let the stub worker release capacity
    resp = client.get(f"/api/verifications/{vid}", headers=headers)
    assert resp.status_code == 200
    resp = client.get(f"/api/candidates/{fresh.id}/verifications", headers=headers)
    assert resp.status_code == 200 and len(resp.json()) == 1
    bad = uuid.uuid4().hex
    assert client.get(f"/api/verifications/{bad}", headers=headers).status_code == 404
    assert client.get(f"/api/verifications/{bad}").status_code == 401
    assert client.get(f"/api/candidates/{bad}/verifications", headers=headers).status_code == 404

    # Orphaned active record reconciles; never verified.
    now = verify_store.utcnow()
    from app.verify.models import Verification

    orphan = Verification(
        id=uuid.uuid4().hex, incident_id=incident.id, run_id=run.id,
        investigation_id=inv.id, candidate_id=fresh.id, repo=REPO,
        baseline_sha=run.commit_sha, contract_hash=run.contract_hash,
        state="running", created_at=now, updated_at=now)
    verify_store.create(orphan)
    resp = client.get(f"/api/verifications/{orphan.id}", headers=headers)
    assert resp.json()["state"] == "blocked" and resp.json()["outcome"] == "BLOCKED"

    # Artifact download round-trips stored bytes.
    verify_store.record_artifact(orphan.id, "bl-result.json", "application/json", b"{}")
    resp = client.get(f"/api/verifications/{orphan.id}/artifacts/bl-result.json/download",
                      headers=headers)
    assert resp.status_code == 200 and resp.content == b"{}"
    resp = client.get(f"/api/verifications/{orphan.id}/artifacts/nope/download",
                      headers=headers)
    assert resp.status_code == 404
