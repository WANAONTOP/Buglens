"""Tests for the Modal reproduction pipeline (Step 5).

All GitHub and Modal interactions are mocked: no network, no sandbox, no
credentials. The fake Modal module exercises the full runner path — fetch,
verify, contract freeze, install, readiness, driver, artifact pull, classify.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_BYTES = (REPO_ROOT / ".buglens.json").read_bytes()
MANIFEST_SHA256 = hashlib.sha256(MANIFEST_BYTES).hexdigest()

SHA = "ab" * 20
REPO = "WANAONTOP/Buglens"


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    import app.providers.modal as modal_adapter

    monkeypatch.setattr(modal_adapter, "_local_profile_present", lambda: False)


@pytest.fixture()
def repro_env(monkeypatch, tmp_path):
    """Isolated data dir + dummy (non-secret) tokens."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BUGLENS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("BUGLENS_API_TOKEN", "test-token-0123456789abcdef01")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-dummy")
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-dummy")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-dummy")
    import app.repro.jobs as jobs

    jobs.reset_for_tests()
    yield SimpleNamespace(
        data_dir=data_dir,
        headers={"Authorization": "Bearer test-token-0123456789abcdef01"},
    )
    jobs.reset_for_tests()


@pytest.fixture()
def client(repro_env):
    import app.main as main

    return TestClient(main.app)


def _entry(path: str, sha: str, entry_type: str = "blob", mode: str = "100644"):
    from app.repro.github_source import TreeEntry

    return TreeEntry(path=path, type=entry_type, mode=mode, sha=sha)


def make_tree(manifest_sha: str = "m" * 40, extra=(), files=None):
    files = files or [
        "fixtures/storefront/server.js",
        "fixtures/storefront/reproduce.mjs",
        "fixtures/storefront/package.json",
        "fixtures/storefront/package-lock.json",
        "fixtures/storefront/public/index.html",
        "fixtures/storefront/public/storefront.js",
        "fixtures/storefront/public/styles.css",
        "backend/app/guards.py",
        "backend/tests/test_manifest.py",
    ]
    entries = [_entry(".buglens.json", manifest_sha)]
    for i, path in enumerate(files):
        entries.append(_entry(path, f"{i:040x}"))
    entries.extend(extra)
    return entries


def stub_github(monkeypatch, manifest_bytes: bytes = MANIFEST_BYTES, tree=None, blob_targets=None):
    import app.routes_repro as routes

    tree = make_tree() if tree is None else tree
    blob_targets = blob_targets or {}

    monkeypatch.setattr(
        routes, "resolve_ref_sha", lambda repo, ref="main", token=None: SHA
    )
    monkeypatch.setattr(routes, "fetch_tree", lambda repo, sha, token=None: tree)

    def fake_blob(repo, sha, token=None):
        if sha == "m" * 40:
            return manifest_bytes
        return blob_targets.get(sha, b"x")

    monkeypatch.setattr(routes, "fetch_blob", fake_blob)


class FakeProc:
    def __init__(self, code=0, stdout="", stderr=""):
        self._code = code
        self._out = stdout
        self._err = stderr

    def poll(self):
        return self._code

    @property
    def returncode(self):
        return self._code

    @property
    def stdout(self):
        return SimpleNamespace(read=lambda: self._out)

    @property
    def stderr(self):
        return SimpleNamespace(read=lambda: self._err)


def _points(hit: bool) -> list[dict]:
    return [{"hitsButton": hit} for _ in range(9)]


def reproduced_result() -> dict:
    return {
        "routes": {
            "broken": {
                "route": "/checkout",
                "buttonPresent": True,
                "hitTest": {"points": _points(False)},
                "clicked": False,
                "confirmationObserved": False,
                "occlusion": {"tag": "DIV", "id": "veil", "testid": "overlay-veil"},
            },
            "control": {
                "route": "/checkout-control",
                "clicked": True,
                "confirmationObserved": True,
                "confirmationText": "Order confirmed",
            },
        },
    }


class FakeModal:
    """Canned modal module: scriptable exec + full call recording."""

    gate: threading.Event | None = None
    created_kwargs: dict | None = None
    created_image = None
    terminated: bool = False
    seen: list = []

    def __init__(self, files: dict, health_ok: bool = True):
        self.files = dict(files)
        self.health_ok = health_ok
        FakeModal.created_kwargs = None
        FakeModal.terminated = False
        FakeModal.seen = []

    def module(self) -> ModuleType:
        outer = self
        mod = ModuleType("modal")

        class Image:
            @staticmethod
            def from_registry(tag):
                FakeModal.created_image = tag
                return object()

        class _Volume:
            def batch_upload(self):
                class _Batch:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                    def put_file(self, local, remote):
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
            @staticmethod
            def lookup(name, create_if_missing=False):
                return object()

        class Sandbox:
            @staticmethod
            def create(**kwargs):
                FakeModal.created_kwargs = dict(kwargs)
                return Sandbox()

            def exec(self, *command, **kwargs):
                line = " ".join(command)
                FakeModal.seen.append(line)
                if "tar -xzf" in line:
                    return FakeProc(0, "", "")
                if "sha256sum" in line:
                    return FakeProc(0, f"{MANIFEST_SHA256}\n", "")
                if "npm install" in line:
                    return FakeProc(0, "installed\n", "")
                if "install-deps" in line:
                    return FakeProc(0, "", "")
                if "server.js" in line and "&" in line:
                    return FakeProc(0, "", "")
                if "fetch(" in line and "healthz" in line:
                    if outer.health_ok:
                        return FakeProc(0, "", "")
                    return FakeProc(1, "", "refused")
                if "driver.cjs" in line:
                    if FakeModal.gate is not None:
                        gate = FakeModal.gate

                        class GatedProc(FakeProc):
                            def poll(self):
                                return 0 if gate.is_set() else None

                            @property
                            def returncode(self):
                                return 0 if gate.is_set() else None

                        return GatedProc(0, "driver done\n", "")
                    return FakeProc(0, "driver done\n", "")
                if "stat -c %s" in line:
                    for name, data in outer.files.items():
                        if line.rstrip().endswith(f"/tmp/out/{name}'"):
                            return FakeProc(0, f"{len(data)}\n", "")
                    return FakeProc(1, "", "missing")
                if "base64" in line:
                    for name, data in outer.files.items():
                        if line.rstrip().endswith(f"/tmp/out/{name}'"):
                            return FakeProc(0, base64.b64encode(data).decode(), "")
                    return FakeProc(1, "", "missing")
                if "kill $(" in line:
                    return FakeProc(0, "", "")
                if "cat /tmp/server.log" in line:
                    return FakeProc(0, "server log line\n", "")
                return FakeProc(1, "", f"unexpected: {line}")

            def terminate(self):
                FakeModal.terminated = True

        mod.Image = Image
        mod.Volume = Volume
        mod.App = App
        mod.Sandbox = Sandbox
        return mod


def canned_files(result=None):
    result = reproduced_result() if result is None else result
    return {
        "result.json": json.dumps(result).encode(),
        "broken-before.png": b"PNG-BEFORE-B",
        "control-before.png": b"PNG-BEFORE-C",
        "broken-after.png": b"PNG-AFTER-B",
        "control-after.png": b"PNG-AFTER-C",
        "broken-trace.zip": b"ZIP-B",
        "control-trace.zip": b"ZIP-C",
    }


@pytest.fixture()
def fake_modal(monkeypatch):
    import sys

    from app.repro.contract import freeze_contract

    contract = freeze_contract(REPO, SHA, MANIFEST_BYTES, make_tree())
    files = canned_files()
    fake = FakeModal(files)
    mod = fake.module()
    monkeypatch.setitem(sys.modules, "modal", mod)
    return SimpleNamespace(fake=fake, module=mod, contract=contract, files=files)


def wait_terminal(client, headers, run_id, deadline=60.0):
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        resp = client.get(f"/api/reproductions/{run_id}", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        if body["state"] in ("done", "cancelled", "error"):
            return body
        time.sleep(0.2)
    raise AssertionError("run did not reach a terminal state")


def start_incident(client, headers, **overrides):
    payload = {
        "title": "blocked pay button",
        "observed_behavior": "click does nothing",
        "expected_behavior": "confirmation appears",
        "reproduction_steps": ["open checkout", "click Pay now"],
        "repo": REPO,
        "ref": "main",
    }
    payload.update(overrides)
    resp = client.post("/api/incidents", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------- contract


def test_freeze_valid_contract_deterministic():
    from app.repro.contract import freeze_contract

    first = freeze_contract(REPO, SHA, MANIFEST_BYTES, make_tree())
    second = freeze_contract(REPO, SHA, MANIFEST_BYTES, make_tree())
    assert first.contract_hash == second.contract_hash
    assert len(first.contract_hash) == 64
    assert first.repo == REPO and first.sha == SHA
    assert ".buglens.json" in first.manifest.protectedPaths
    # Same bytes always freeze to the same hash; reordered keys parse to the
    # same manifest but MUST hash differently (fidelity is byte-exact).
    shuffled = json.dumps(json.loads(MANIFEST_BYTES.decode()), sort_keys=True).encode()
    reserialized = freeze_contract(REPO, SHA, shuffled, make_tree())
    assert reserialized.manifest == first.manifest
    assert reserialized.manifest_bytes_sha256 != first.manifest_bytes_sha256
    assert reserialized.contract_hash != first.contract_hash


def test_freeze_rejects_bad_manifest_and_protection():
    import copy

    from app.repro.contract import ContractError, freeze_contract

    with pytest.raises(ContractError):
        freeze_contract(REPO, SHA, b"not json", make_tree())

    doc = json.loads(MANIFEST_BYTES.decode())
    doc["permittedPatchPaths"] = ["../escape"]
    with pytest.raises(ContractError):
        freeze_contract(REPO, SHA, json.dumps(doc).encode(), make_tree())

    # Manifest must be pinned inside the tree at the SHA.
    with pytest.raises(ContractError):
        freeze_contract(REPO, SHA, MANIFEST_BYTES, [_entry("other.json", "0" * 40)])

    # Permitted set must cover real fixture sources.
    doc2 = copy.deepcopy(doc)
    doc2["permittedPatchPaths"] = ["fixtures/storefront/README.md"]
    with pytest.raises(ContractError):
        freeze_contract(REPO, SHA, json.dumps(doc2).encode(), make_tree())

    # Protected files must exist at the SHA.
    with pytest.raises(ContractError):
        freeze_contract(REPO, SHA, MANIFEST_BYTES, make_tree(files=["x"]))


def test_freeze_rejects_symlink_escape_and_accepts_interior_link():
    from app.repro.contract import ContractError, freeze_contract

    evil = _entry("fixtures/storefront/evil", "e" * 40, mode="120000")
    ok = _entry("fixtures/storefront/ok", "f" * 40, mode="120000")
    with pytest.raises(ContractError):
        freeze_contract(
            REPO,
            SHA,
            MANIFEST_BYTES,
            make_tree(extra=[evil]),
            {"fixtures/storefront/evil": "../outside"},
        )
    with pytest.raises(ContractError):
        # Unverified target is also rejected.
        freeze_contract(REPO, SHA, MANIFEST_BYTES, make_tree(extra=[ok]), {})
    contract = freeze_contract(
        REPO,
        SHA,
        MANIFEST_BYTES,
        make_tree(extra=[ok]),
        {"fixtures/storefront/ok": "public/index.html"},
    )
    assert contract.contract_hash


# ---------------------------------------------------------------- classify


def test_classify_matrix():
    from app.repro.runner import REQUIRED_ARTIFACTS, classify

    fetched = set(REQUIRED_ARTIFACTS)
    verdict, detail = classify(reproduced_result(), fetched)
    assert verdict == "REPRODUCED"
    assert "covers the button" in detail

    broken_ok = reproduced_result()["routes"]["broken"]
    control_ok = reproduced_result()["routes"]["control"]

    # Broken click succeeds -> failure not established.
    clicked = {
        "routes": {
            "broken": {**broken_ok, "clicked": True, "confirmationObserved": True,
                        "hitTest": {"points": _points(True)}},
            "control": control_ok,
        }
    }
    assert classify(clicked, fetched)[0] == "NEEDS_REPRODUCTION"

    # Button not occluded -> failure not established.
    open_ = {
        "routes": {
            "broken": {**broken_ok, "hitTest": {"points": _points(True)}},
            "control": control_ok,
        }
    }
    assert classify(open_, fetched)[0] == "NEEDS_REPRODUCTION"

    # Oracle broken (control click failed) -> not established.
    oracle = {
        "routes": {
            "broken": broken_ok,
            "control": {**control_ok, "clicked": False, "confirmationObserved": False},
        }
    }
    assert classify(oracle, fetched)[0] == "NEEDS_REPRODUCTION"

    # Driver errors, missing artifacts, missing button -> BLOCKED.
    assert classify({**reproduced_result(), "driverErrors": ["fatal: boom"]}, fetched)[0] == "BLOCKED"
    assert classify(reproduced_result(), {"result.json"})[0] == "BLOCKED"
    assert classify("not-an-object", fetched)[0] == "BLOCKED"
    nobtn = {"routes": {"broken": {**broken_ok, "buttonPresent": False}, "control": control_ok}}
    assert classify(nobtn, fetched)[0] == "BLOCKED"
    fatal = {"routes": {"broken": {**broken_ok, "fatal": "nav failed"}, "control": control_ok}}
    assert classify(fatal, fetched)[0] == "BLOCKED"


# ---------------------------------------------------------------- endpoints


def test_start_runs_full_pipeline_and_persists(client, repro_env, fake_modal, monkeypatch):
    stub_github(monkeypatch)
    incident_id = start_incident(client, repro_env.headers)
    resp = client.post(f"/api/incidents/{incident_id}/reproductions", headers=repro_env.headers)
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["id"]

    body = wait_terminal(client, repro_env.headers, run_id)
    assert body["state"] == "done"
    assert body["verdict"] == "REPRODUCED"
    assert body["contract_hash"] == fake_modal.contract.contract_hash
    assert body["repo"] == REPO and body["commit_sha"] == SHA
    assert body["artifacts"]
    assert [e["phase"] for e in body["events"]]

    # Incident pinned to the immutable SHA.
    got = client.get(f"/api/incidents/{incident_id}", headers=repro_env.headers)
    assert got.json()["commit_sha"] == SHA

    # Frozen contract persisted beside the run.
    import app.repro.store as repro_store

    saved = repro_store.load_contract(run_id)
    assert saved is not None and saved["contract_hash"] == fake_modal.contract.contract_hash

    # Sandbox received no secrets and honored bounds.
    import app.config as config

    kwargs = FakeModal.created_kwargs
    assert kwargs is not None and FakeModal.terminated
    blob = json.dumps(kwargs, default=str)
    assert "ak-dummy" not in blob and "as-dummy" not in blob and "ghp-dummy" not in blob
    executed = "\n".join(FakeModal.seen)
    assert "ak-dummy" not in executed
    assert "as-dummy" not in executed
    assert "ghp-dummy" not in executed
    assert kwargs["cpu"] == config.REPRO_SANDBOX_CPU
    assert kwargs["memory"] == config.REPRO_SANDBOX_MEMORY_MB
    assert "node" in str(FakeModal.created_image)

    # Artifact download round-trips stored bytes.
    names = [a["name"] for a in body["artifacts"]]
    assert "result.json" in names
    dl = client.get(
        f"/api/reproductions/{run_id}/artifacts/result.json/download",
        headers=repro_env.headers,
    )
    assert dl.status_code == 200
    assert json.loads(dl.content)["routes"]["control"]["confirmationObserved"] is True

    # Unknown artifact name -> 404, never a traversal.
    missing = client.get(
        f"/api/reproductions/{run_id}/artifacts/../run.json/download",
        headers=repro_env.headers,
    )
    assert missing.status_code == 404


def test_start_validates_and_reports_setup(client, repro_env, monkeypatch):
    stub_github(monkeypatch)
    # Unknown incident.
    resp = client.post(f"/api/incidents/{uuid.uuid4().hex}/reproductions", headers=repro_env.headers)
    assert resp.status_code == 404

    # Draft without repo/ref cannot start.
    draft = start_incident(client, repro_env.headers, repo=None, ref=None)
    resp = client.post(f"/api/incidents/{draft}/reproductions", headers=repro_env.headers)
    assert resp.status_code == 422

    # Missing Modal credentials -> explicit setup error.
    good = start_incident(client, repro_env.headers)
    monkeypatch.delenv("MODAL_TOKEN_ID")
    monkeypatch.delenv("MODAL_TOKEN_SECRET")
    resp = client.post(f"/api/incidents/{good}/reproductions", headers=repro_env.headers)
    assert resp.status_code == 409
    assert "Modal" in resp.json()["detail"]


def test_capacity_limit_rejects_second_run(client, repro_env, fake_modal, monkeypatch):
    stub_github(monkeypatch)
    FakeModal.gate = threading.Event()
    try:
        first = start_incident(client, repro_env.headers)
        resp = client.post(f"/api/incidents/{first}/reproductions", headers=repro_env.headers)
        assert resp.status_code == 202
        first_id = resp.json()["id"]
        time.sleep(1.0)  # let the worker reach the gated driver phase

        second = start_incident(client, repro_env.headers)
        resp = client.post(f"/api/incidents/{second}/reproductions", headers=repro_env.headers)
        assert resp.status_code == 429
    finally:
        FakeModal.gate.set()
        FakeModal.gate = None
    body = wait_terminal(client, repro_env.headers, first_id)
    assert body["state"] == "done"


def test_cancel_running_run(client, repro_env, fake_modal, monkeypatch):
    stub_github(monkeypatch)
    FakeModal.gate = threading.Event()
    try:
        incident_id = start_incident(client, repro_env.headers)
        resp = client.post(f"/api/incidents/{incident_id}/reproductions", headers=repro_env.headers)
        run_id = resp.json()["id"]
        time.sleep(1.0)
        cancel = client.post(f"/api/reproductions/{run_id}/cancel", headers=repro_env.headers)
        assert cancel.status_code == 200
        # Gate stays closed: the worker must observe cancellation itself.
        body = wait_terminal(client, repro_env.headers, run_id)
        assert body["state"] == "cancelled"
        assert FakeModal.terminated
    finally:
        FakeModal.gate.set()
        FakeModal.gate = None


def test_run_status_errors_and_guards(client, repro_env):
    bad_id = uuid.uuid4().hex
    resp = client.get(f"/api/reproductions/{bad_id}", headers=repro_env.headers)
    assert resp.status_code == 404
    resp = client.post(f"/api/reproductions/{bad_id}/cancel", headers=repro_env.headers)
    assert resp.status_code == 404
    resp = client.get(f"/api/reproductions/{bad_id}/artifacts/x/download", headers=repro_env.headers)
    assert resp.status_code == 404
    resp = client.get(f"/api/incidents/{bad_id}/reproductions", headers=repro_env.headers)
    assert resp.status_code == 404
    # Malformed ids never touch the store.
    resp = client.get("/api/reproductions/nope", headers=repro_env.headers)
    assert resp.status_code == 404
    # No token -> unauthorized.
    resp = client.get(f"/api/reproductions/{bad_id}")
    assert resp.status_code == 401


def test_reconcile_orphaned_running_run(client, repro_env):
    import app.repro.store as repro_store
    from app.repro.models import ReproRun

    now = repro_store.utcnow()
    run = ReproRun(
        id=uuid.uuid4().hex,
        incident_id=uuid.uuid4().hex,
        repo=REPO,
        commit_sha=SHA,
        contract_hash="h" * 64,
        state="running",
        detail="stale worker",
        created_at=now,
        updated_at=now,
    )
    repro_store.create_run(run)
    resp = client.get(f"/api/reproductions/{run.id}", headers=repro_env.headers)
    assert resp.status_code == 200
    assert resp.json()["state"] == "cancelled"


def test_store_helpers(repro_env):
    import app.repro.store as repro_store
    from app.repro.models import ReproRun

    now = repro_store.utcnow()
    run = ReproRun(
        id=uuid.uuid4().hex,
        incident_id=uuid.uuid4().hex,
        repo=REPO,
        commit_sha=SHA,
        contract_hash="h" * 64,
        state="queued",
        created_at=now,
        updated_at=now,
    )
    repro_store.create_run(run)
    repro_store.add_event(run.id, "phase", "msg")
    artifact = repro_store.add_artifact(run.id, "a.txt", "text/plain", b"hello", "stored-a.txt")
    assert artifact.sha256
    loaded = repro_store.get_run(run.id)
    assert loaded is not None and len(loaded.events) == 1
    assert repro_store.safe_run_artifact_path(run.id, "stored-a.txt").read_bytes() == b"hello"
    with pytest.raises(ValueError):
        repro_store.safe_run_artifact_path(run.id, "../evil")
    repro_store.save_contract(
        run.id, {"repo": REPO, "sha": SHA, "contract_hash": "h", "manifest": {}}
    )
    assert repro_store.load_contract(run.id)["contract_hash"] == "h"


# ---------------------------------------------------------------- github_source


def test_github_source_happy_path_and_errors():
    from app.repro.github_source import (
        GitHubSourceError,
        fetch_blob,
        fetch_tree,
        resolve_ref_sha,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/git/ref/heads/main"):
            assert request.headers["Authorization"] == "Bearer t"
            return httpx.Response(200, json={"object": {"sha": SHA}})
        if "/git/trees/" in path:
            return httpx.Response(
                200,
                json={"truncated": False, "tree": [
                    {"path": ".buglens.json", "type": "blob", "mode": "100644", "sha": "m" * 40}
                ]},
            )
        if "/git/blobs/" in path:
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"hi").decode()})
        return httpx.Response(404, json={"message": "nope"})

    transport = httpx.MockTransport(handler)
    assert resolve_ref_sha(REPO, "main", "t", transport=transport) == SHA
    tree = fetch_tree(REPO, SHA, "t", transport=transport)
    assert tree[0].path == ".buglens.json"
    assert fetch_blob(REPO, "m" * 40, "t", transport=transport) == b"hi"

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad credentials"})

    with pytest.raises(GitHubSourceError, match="HTTP 401"):
        resolve_ref_sha(REPO, "main", "bad", transport=httpx.MockTransport(bad))

    def trunc(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"truncated": True, "tree": []})

    with pytest.raises(GitHubSourceError, match="truncated"):
        fetch_tree(REPO, SHA, "t", transport=httpx.MockTransport(trunc))
