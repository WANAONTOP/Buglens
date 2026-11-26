"""Tests for Step 11 Modal deployment preparation (all local/mocked).

Covers configuration parsing, deploy-mode flags, the Modal app module
structure (real SDK objects, constructed lazily — no network), volume
sync hooks, the incidents-registry optimistic-concurrency guard,
remote dispatch/cancel/reconcile with faked Modal calls, worker claim
and crash marking, and sandbox credential isolation. Nothing here
touches live Modal; remote execution is verified only by the bounded
live smoke procedure in README (not run in this step).
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-0123456789abcdef01"


@pytest.fixture()
def deploy_env(monkeypatch, tmp_path):
    """Isolated data dir + dummy tokens + reset deploy caches."""
    from app import deploy

    monkeypatch.setenv("BUGLENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BUGLENS_API_TOKEN", TOKEN)
    monkeypatch.delenv("BUGLENS_MODAL", raising=False)
    monkeypatch.delenv("BUGLENS_JOBS", raising=False)
    monkeypatch.delenv("BUGLENS_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FRONTEND_ORIGIN", raising=False)
    deploy.reset_for_tests()
    yield {"headers": {"Authorization": f"Bearer {TOKEN}"}}
    deploy.reset_for_tests()


@pytest.fixture()
def client(deploy_env):
    import app.main as main

    return TestClient(main.app)


class FakeVolume:
    def __init__(self):
        self.commits = 0
        self.reloads = 0

    def commit(self):
        self.commits += 1

    def reload(self):
        self.reloads += 1


@pytest.fixture()
def fake_volume(monkeypatch):
    from app import deploy

    volume = FakeVolume()
    monkeypatch.setattr(deploy, "volume", lambda: volume)
    return volume


def make_incident(**over):
    import app.store as incident_store
    from app.models import Incident

    now = incident_store.utcnow()
    incident = Incident(
        id=over.get("id", uuid.uuid4().hex),
        title=over.get("title", "probe"),
        observed_behavior="o",
        expected_behavior="e",
        reproduction_steps=["s"],
        created_at=now,
        updated_at=now,
    )
    return incident_store.create_incident(incident)


def make_repro_run(state="queued", **over):
    import app.repro.store as repro_store
    from app.repro.models import ReproRun

    now = over.get("updated_at", repro_store.utcnow())
    run = ReproRun(
        id=over.get("id", uuid.uuid4().hex),
        incident_id=over.get("incident_id", uuid.uuid4().hex),
        repo=over.get("repo", "o/r"),
        commit_sha=over.get("commit_sha", "a" * 40),
        contract_hash=over.get("contract_hash", "c" * 64),
        state=state,
        created_at=over.get("created_at", now),
        updated_at=now,
        worker_call_id=over.get("worker_call_id", ""),
    )
    return repro_store.create_run(run)


# ------------------------------------------------- configuration


def test_cors_origins_default_and_override(deploy_env, monkeypatch):
    from app import config

    assert config.get_cors_origins() == [config.FRONTEND_ORIGIN]
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://app.example")
    assert config.get_cors_origins() == ["https://app.example"]
    monkeypatch.setenv("BUGLENS_CORS_ORIGINS", "https://a.example, https://b.example,, ")
    assert config.get_cors_origins() == ["https://a.example", "https://b.example"]


def test_deploy_mode_flags(deploy_env, monkeypatch):
    from app import deploy

    assert deploy.is_modal() is False
    assert deploy.jobs_mode() == "local"
    assert deploy.use_remote_workers() is False
    monkeypatch.setenv("BUGLENS_MODAL", "1")
    monkeypatch.setenv("BUGLENS_JOBS", "modal")
    assert deploy.is_modal() is True
    assert deploy.use_remote_workers() is True


def test_remote_jobs_without_modal_refuses(deploy_env, monkeypatch):
    from app import deploy

    monkeypatch.setenv("BUGLENS_JOBS", "modal")
    with pytest.raises(deploy.DeployError):
        deploy.use_remote_workers()


def test_modal_app_structure():
    """The deploy module builds real SDK objects lazily (no network)."""
    import modal

    import modal_app

    assert modal_app.MODAL_APP_NAME == "buglens-ai"
    assert isinstance(modal_app.app, modal.App)
    for name in ("api", "run_repro_worker", "run_investigate_worker",
                 "run_candidate_worker", "run_verify_worker"):
        assert isinstance(getattr(modal_app, name), modal.Function), name
    assert modal_app.ENV["BUGLENS_DATA_DIR"] == "/data"
    assert modal_app.ENV["BUGLENS_JOBS"] == "modal"
    assert modal_app.ENV["BUGLENS_MODAL"] == "1"


# ------------------------------------------------- volume sync


def test_store_write_commits_only_in_modal(deploy_env, fake_volume, monkeypatch):
    import app.repro.store as repro_store

    make_repro_run()
    assert fake_volume.commits == 0  # local mode: no-op
    monkeypatch.setenv("BUGLENS_MODAL", "1")
    make_repro_run()
    assert fake_volume.commits == 1


def test_middleware_reloads_and_commits(client, deploy_env, fake_volume, monkeypatch):
    monkeypatch.setenv("BUGLENS_MODAL", "1")
    listed = client.get("/api/incidents", headers=deploy_env["headers"])
    assert listed.status_code == 200
    assert fake_volume.reloads == 1
    assert fake_volume.commits == 0  # reads never commit
    created = client.post(
        "/api/incidents",
        json={"title": "t", "observed_behavior": "o", "expected_behavior": "e",
              "reproduction_steps": ["s"]},
        headers=deploy_env["headers"],
    )
    assert created.status_code == 201
    assert fake_volume.reloads == 2
    # One commit from the store write hook, one from the middleware safety net.
    assert fake_volume.commits == 2


def test_failed_sync_surfaces_503(client, deploy_env, monkeypatch):
    from app import deploy

    class BrokenVolume(FakeVolume):
        def reload(self):
            raise RuntimeError("volume unreachable")

    monkeypatch.setenv("BUGLENS_MODAL", "1")
    monkeypatch.setattr(deploy, "volume", lambda: BrokenVolume())
    resp = client.get("/api/incidents", headers=deploy_env["headers"])
    assert resp.status_code == 503


def test_auth_survives_deploy_mode(client, deploy_env, fake_volume, monkeypatch):
    monkeypatch.setenv("BUGLENS_MODAL", "1")
    assert client.get("/api/incidents").status_code == 401
    assert client.get("/api/incidents", headers=deploy_env["headers"]).status_code == 200


# ------------------------------------------------- OCC guard


def test_registry_conflict_raises_and_maps_to_409(client, deploy_env, fake_volume, monkeypatch):
    import app.store as incident_store
    from app import deploy

    make_incident()
    stale = incident_store.current_version()
    make_incident()  # moves the version underneath the first reader
    with pytest.raises(deploy.ConflictError):
        incident_store.create_incident(_bare_incident(), expected=stale)

    # Route level: a stale base version fails the request loudly.
    monkeypatch.setenv("BUGLENS_MODAL", "1")
    monkeypatch.setattr(deploy, "store_version_for_write", lambda: "stale-version")
    resp = client.post(
        "/api/incidents",
        json={"title": "t", "observed_behavior": "o", "expected_behavior": "e",
              "reproduction_steps": ["s"]},
        headers=deploy_env["headers"],
    )
    assert resp.status_code == 409


def _bare_incident():
    import app.store as incident_store
    from app.models import Incident

    now = incident_store.utcnow()
    return Incident(id=uuid.uuid4().hex, title="t", observed_behavior="o",
                    expected_behavior="e", reproduction_steps=["s"],
                    created_at=now, updated_at=now)


def test_registry_write_succeeds_on_fresh_version(deploy_env, fake_volume, monkeypatch):
    import app.store as incident_store

    monkeypatch.setenv("BUGLENS_MODAL", "1")
    incident_store.create_incident(
        _bare_incident(), expected=incident_store.current_version())
    assert len(incident_store.list_incidents()) == 1


# ------------------------------------------------- remote dispatch


def _modal_env(monkeypatch):
    monkeypatch.setenv("BUGLENS_MODAL", "1")
    monkeypatch.setenv("BUGLENS_JOBS", "modal")


def test_remote_start_spawns_and_records_call(deploy_env, fake_volume, monkeypatch):
    from app import deploy
    from app.repro import jobs as repro_jobs
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        deploy, "spawn_worker",
        lambda worker, record_id: calls.append((worker, record_id)) or "call-123")
    run = make_repro_run()
    ran = []
    assert repro_jobs.try_start(run.id, ran.append) is True
    assert calls == [("run_repro_worker", run.id)]
    assert ran == []  # the local callable never executes remotely
    assert repro_store.get_run(run.id).worker_call_id == "call-123"


def test_remote_spawn_failure_raises_not_capacity(deploy_env, fake_volume, monkeypatch):
    from app import deploy
    from app.repro import jobs as repro_jobs

    _modal_env(monkeypatch)

    def boom(worker, record_id):
        raise deploy.DeployError("modal unreachable")

    monkeypatch.setattr(deploy, "spawn_worker", boom)
    run = make_repro_run()
    with pytest.raises(deploy.DeployError):
        repro_jobs.try_start(run.id, lambda event: None)


def test_remote_capacity_counts_active_records(deploy_env, fake_volume, monkeypatch):
    from app.repro import jobs as repro_jobs

    _modal_env(monkeypatch)
    assert repro_jobs.has_capacity() is True
    make_repro_run(state="running")
    assert repro_jobs.has_capacity() is False
    assert repro_jobs.try_start(make_repro_run().id, lambda event: None) is False


def test_count_active_skips_corrupt_files(deploy_env):
    from app import deploy

    data = Path(os.environ["BUGLENS_DATA_DIR"]) / "repro_runs"
    (data / "good").mkdir(parents=True)
    (data / "good" / "record.json").write_text(json.dumps({"state": "running"}))
    (data / "broken").mkdir(parents=True)
    (data / "broken" / "record.json").write_text("{not json")
    assert deploy.count_active("repro_runs", ("queued", "running")) == 1
    assert deploy.count_active("no-such-area", ("queued",)) == 0
    assert deploy.count_active("repro_runs", ("queued", "running"), exclude="good") == 0


def test_remote_cancel_terminates_and_marks(deploy_env, fake_volume, monkeypatch):
    from app import deploy
    from app.repro import jobs as repro_jobs
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(deploy, "cancel_call", lambda call_id, **kw: cancelled.append(call_id) or True)
    monkeypatch.setattr(deploy, "wait_for_call_end", lambda call_id, timeout_s=5.0: True)
    run = make_repro_run(state="running")
    repro_store.update_run(run.id, lambda item: setattr(item, "worker_call_id", "call-9"))
    assert repro_jobs.cancel(run.id) is True
    assert cancelled == ["call-9"]
    stored = repro_store.get_run(run.id)
    assert stored.state == "cancelled"
    assert any(e.message == "cancellation requested" for e in stored.events)
    assert repro_jobs.cancel(run.id) is False  # terminal records stay put


def test_remote_cancel_without_call_id_still_marks(deploy_env, fake_volume, monkeypatch):
    from app.repro import jobs as repro_jobs
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    run = make_repro_run(state="queued")
    assert repro_jobs.cancel(run.id) is True
    assert repro_store.get_run(run.id).state == "cancelled"


def test_deploy_reconcile_marks_only_stale(deploy_env, fake_volume, monkeypatch):
    import app.repro.store as repro_store
    from app.repro import jobs as repro_jobs

    _modal_env(monkeypatch)
    old = repro_store.utcnow() - timedelta(seconds=deploy_stale_plus())
    stale = make_repro_run(state="running", updated_at=old)
    fresh = make_repro_run(state="running")
    assert repro_jobs.reconcile(repro_store.get_run(stale.id)).state == "cancelled"
    assert "remote worker lost" in repro_store.get_run(stale.id).detail
    assert repro_jobs.reconcile(repro_store.get_run(fresh.id)).state == "running"


def deploy_stale_plus():
    from app import deploy

    return deploy.DEPLOY_STALE_AFTER_S + 60.0


# ------------------------------------------------- worker claim/crash


def test_worker_claim_missing_duplicate_and_crash():
    from app import workers

    assert workers._guarded("nope", lambda rid: None, lambda: True,
                            lambda: None, lambda event: None) == "missing"
    ran: list[str] = []
    outcome = workers._guarded(
        "r1",
        lambda rid: SimpleNamespace(state="running"),
        lambda: ran.append("claimed") or True,
        lambda: ran.append("marked"),
        lambda event: ran.append("invoked"),
    )
    assert outcome == "duplicate"
    assert ran == []
    outcome = workers._guarded(  # lost claim race claims nothing, spends nothing
        "r1",
        lambda rid: SimpleNamespace(state="queued"),
        lambda: False,
        lambda: ran.append("marked"),
        lambda event: ran.append("invoked"),
    )
    assert outcome == "duplicate"
    assert ran == []
    outcome = workers._guarded(
        "r1",
        lambda rid: SimpleNamespace(state="queued"),
        lambda: ran.append("claimed") or True,
        lambda: ran.append("marked"),
        lambda event: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert outcome.startswith("error:")
    assert ran == ["claimed", "marked"]


def test_repro_worker_crash_marks_error(deploy_env, monkeypatch):
    import app.repro.store as repro_store
    from app import workers
    from app.repro import runner as repro_runner

    def boom(rid, event):
        raise RuntimeError("boom")

    run = make_repro_run()
    monkeypatch.setattr(repro_runner, "run_reproduction", boom)
    assert workers.run_repro_guarded(run.id).startswith("error:")
    stored = repro_store.get_run(run.id)
    assert stored.state == "error"
    assert any(e.phase == "lifecycle" for e in stored.events)
    assert any(e.message == "worker claimed queued run" for e in stored.events)


def test_verify_worker_crash_marks_blocked(deploy_env, monkeypatch):
    import app.verify.store as verify_store
    from app import workers
    from app.verify import runner as verify_runner
    from app.verify.models import Verification

    now = verify_store.utcnow()
    record = Verification(
        id=uuid.uuid4().hex, incident_id=uuid.uuid4().hex, run_id=uuid.uuid4().hex,
        investigation_id=uuid.uuid4().hex, candidate_id=uuid.uuid4().hex,
        repo="o/r", contract_hash="c" * 64, state="queued",
        created_at=now, updated_at=now)
    verify_store.create(record)

    def boom(vid, event):
        raise RuntimeError("boom")

    monkeypatch.setattr(verify_runner, "run_verification", boom)
    assert workers.run_verification_guarded(record.id).startswith("error:")
    stored = verify_store.get(record.id)
    assert stored.state == "error"
    assert stored.outcome == "BLOCKED"


# ------------------------------------------------- coordination (Step 12)


def test_duplicate_start_spawns_once(deploy_env, fake_volume, monkeypatch):
    from app import deploy
    from app.repro import jobs as repro_jobs
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    spawns: list[str] = []
    monkeypatch.setattr(
        deploy, "spawn_worker",
        lambda worker, record_id: spawns.append(record_id) or "call-1")
    run = make_repro_run()
    assert repro_jobs.try_start(run.id, lambda event: None) is True
    assert repro_jobs.try_start(run.id, lambda event: None) is True
    assert spawns == [run.id]  # second submission dispatched nothing new
    assert repro_store.get_run(run.id).worker_call_id == "call-1"


def test_concurrent_duplicate_start_spawns_once(deploy_env, fake_volume, monkeypatch):
    import threading
    import time
    from app import deploy
    from app.repro import jobs as repro_jobs

    _modal_env(monkeypatch)
    spawns: list[str] = []

    def slow_spawn(worker, record_id):
        time.sleep(0.2)  # force the check-then-act windows to overlap
        spawns.append(record_id)
        return "call-1"

    monkeypatch.setattr(deploy, "spawn_worker", slow_spawn)
    run = make_repro_run()
    results: list[bool] = []

    def attempt():
        results.append(repro_jobs.try_start(run.id, lambda event: None))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert results == [True, True]
    assert len(spawns) == 1


def test_deploy_update_refuses_terminal_write(deploy_env, fake_volume, monkeypatch):
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    run = make_repro_run(state="done")
    # A stale progress write against a terminal record is refused ...
    assert repro_store.update_run(run.id, lambda item: None) is None
    repro_store.add_event(run.id, "progress", "late heartbeat")
    stored = repro_store.get_run(run.id)
    assert stored.state == "done"
    assert stored.events == []
    # ... while local single-process semantics still append.
    monkeypatch.delenv("BUGLENS_MODAL")
    monkeypatch.delenv("BUGLENS_JOBS")
    repro_store.add_event(run.id, "progress", "late heartbeat")
    assert len(repro_store.get_run(run.id).events) == 1


def test_update_honors_expect_states(deploy_env):
    import app.repro.store as repro_store

    run = make_repro_run(state="running")
    assert repro_store.update_run(
        run.id, lambda item: setattr(item, "detail", "x"),
        expect_states=("queued",)) is None
    assert repro_store.get_run(run.id).detail == ""
    assert repro_store.update_run(
        run.id, lambda item: setattr(item, "detail", "x"),
        expect_states=("queued", "running")) is not None
    assert repro_store.get_run(run.id).detail == "x"


def test_deploy_reconcile_checks_worker_liveness(deploy_env, fake_volume, monkeypatch):
    from app import deploy
    from app.repro import jobs as repro_jobs
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    statuses = {"call-alive": "running", "call-gone": "finished",
                "call-mystery": "unknown"}
    monkeypatch.setattr(deploy, "call_status", lambda call_id: statuses[call_id])
    old = repro_store.utcnow() - timedelta(seconds=deploy_stale_plus())
    alive = make_repro_run(state="running", updated_at=old, worker_call_id="call-alive")
    gone = make_repro_run(state="running", updated_at=old, worker_call_id="call-gone")
    mystery = make_repro_run(state="running", updated_at=old, worker_call_id="call-mystery")
    assert repro_jobs.reconcile(repro_store.get_run(alive.id)).state == "running"
    assert repro_jobs.reconcile(repro_store.get_run(gone.id)).state == "cancelled"
    assert "remote worker lost" in repro_store.get_run(gone.id).detail
    assert repro_jobs.reconcile(repro_store.get_run(mystery.id)).state == "running"


def test_remote_cancel_after_completion_is_noop(deploy_env, fake_volume, monkeypatch):
    from app import deploy
    from app.repro import jobs as repro_jobs
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    kills: list[str] = []
    monkeypatch.setattr(deploy, "cancel_call", lambda call_id, **kw: kills.append(call_id) or True)
    monkeypatch.setattr(deploy, "wait_for_call_end", lambda call_id, timeout_s=5.0: True)
    run = make_repro_run(state="done", worker_call_id="call-1")
    assert repro_jobs.cancel(run.id) is False
    assert kills == []
    assert repro_store.get_run(run.id).state == "done"


def test_worker_uncommitted_spawn_exits_without_writing(
        deploy_env, fake_volume, monkeypatch):
    from app import workers
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    monkeypatch.setattr(workers, "CALL_ID_TIMEOUT_S", 0.0)
    monkeypatch.setattr(workers, "CALL_ID_POLL_S", 0.01)
    run = make_repro_run()  # queued, API never committed the spawn
    invoked: list[str] = []
    outcome = workers._guarded(
        run.id, repro_store.get_run,
        lambda: invoked.append("claimed") or True,
        lambda: invoked.append("marked"),
        lambda event: invoked.append("invoked"),
    )
    assert outcome == "uncommitted"
    assert invoked == []
    stored = repro_store.get_run(run.id)
    assert stored.state == "queued"
    assert stored.events == []


def test_worker_proceeds_when_call_id_committed(deploy_env, fake_volume, monkeypatch):
    from app import workers
    import app.repro.store as repro_store

    _modal_env(monkeypatch)
    run = make_repro_run(worker_call_id="call-1")
    invoked: list[str] = []
    outcome = workers._guarded(
        run.id, repro_store.get_run,
        lambda: invoked.append("claimed") or True,
        lambda: invoked.append("marked"),
        lambda event: invoked.append("invoked"),
    )
    assert outcome == "done"
    assert invoked == ["claimed", "invoked"]


def test_call_status_classification(deploy_env, monkeypatch):
    import modal
    from modal import exception as modal_exception
    from app import deploy

    script: dict[str, object] = {"behavior": None}

    class FakeCall:
        def get(self, timeout=None, index=0):
            behavior = script["behavior"]
            if behavior == "timeout":
                raise TimeoutError()
            if behavior == "value":
                return "done"
            assert isinstance(behavior, Exception)
            raise behavior

    class FakeFunctionCall:
        @classmethod
        def from_id(cls, call_id, client=None):
            assert call_id == "call-1"
            return FakeCall()

    monkeypatch.setattr(modal, "FunctionCall", FakeFunctionCall)
    script["behavior"] = "timeout"
    assert deploy.call_status("call-1") == "running"
    script["behavior"] = "value"
    assert deploy.call_status("call-1") == "finished"
    script["behavior"] = RuntimeError("worker raised")
    assert deploy.call_status("call-1") == "finished"
    script["behavior"] = modal_exception.NotFoundError("gone")
    assert deploy.call_status("call-1") == "finished"
    script["behavior"] = modal_exception.OutputExpiredError("expired")
    assert deploy.call_status("call-1") == "finished"
    script["behavior"] = modal_exception.ConnectionError("down")
    assert deploy.call_status("call-1") == "unknown"
    script["behavior"] = modal_exception.AuthError("nope")
    assert deploy.call_status("call-1") == "unknown"


def test_wait_for_call_end(deploy_env, monkeypatch):
    from app import deploy

    monkeypatch.setattr(deploy, "call_status", lambda call_id: "finished")
    assert deploy.wait_for_call_end("call-1") is True
    monkeypatch.setattr(deploy, "call_status", lambda call_id: "unknown")
    assert deploy.wait_for_call_end("call-1") is False
    monkeypatch.setattr(deploy, "call_status", lambda call_id: "running")
    assert deploy.wait_for_call_end("call-1", timeout_s=0.0) is False


def test_workers_never_write_incident_registry():
    root = Path(__file__).resolve().parent.parent / "app"
    worker_paths = [
        "repro/runner.py",
        "investigate/runner.py",
        "investigate/evidence.py",
        "patches/runner.py",
        "patches/context.py",
        "verify/runner.py",
        "workers.py",
    ]
    mutators = (
        "incident_store.create_incident",
        "incident_store.update_commit_sha",
        "incident_store.add_evidence",
        "store.create_incident(",
        "store.update_commit_sha(",
        "store.add_evidence(",
    )
    for rel in worker_paths:
        text = (root / rel).read_text(encoding="utf-8")
        for mutator in mutators:
            assert mutator not in text, (
                f"{rel} must not write the incident registry ({mutator}): "
                "the registry is API-write-only"
            )


def test_deploy_error_maps_to_503(client, deploy_env, monkeypatch):
    from app import deploy
    import app.repro.store as repro_store

    def boom(run_id):
        raise deploy.DeployError("volume gone")

    monkeypatch.setattr(repro_store, "get_run", boom)
    resp = client.get(f"/api/reproductions/{'b' * 32}", headers=deploy_env["headers"])
    assert resp.status_code == 503
    assert "volume gone" in resp.json()["detail"]


# ------------------------------------------------- isolation


def test_sandbox_fetch_targets_carry_no_credentials():
    from app.verify.runner import _node_fetch_js

    script = _node_fetch_js("someone/private", "d" * 40)
    lowered = script.lower()
    assert "token" not in lowered
    assert "bearer" not in lowered
    assert "authorization" not in lowered
    assert "api.github.com/repos/someone/private/tarball/" in script


def test_worker_side_github_auth_uses_headers_not_urls():
    import httpx

    from app.repro.github_source import fetch_blob

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        payload = base64.b64encode(b"{}").decode()
        return httpx.Response(200, json={"encoding": "base64", "content": payload})

    fetch_blob("o/r", "a" * 40, "ghp-sentinel-value", transport=httpx.MockTransport(handler))
    assert seen["auth"] == "Bearer ghp-sentinel-value"
    assert "ghp-sentinel-value" not in seen["url"]
