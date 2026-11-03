"""Tests for Step 9 verified GitHub PR publication (GitHub mocked).

Covers the server-side preflight (every refusal: unapproved, failed/
blocked/mocked verification, stale baseline, changed diff, wrong repo,
moved remote base, superseded, cancelled, missing bytes), the exact
repair commit (tree content equals the recomputed tested bytes),
idempotent publish, foreign-branch refusal, orphaned-intent recovery,
concurrency, and endpoint guards. No real GitHub writes happen here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
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
BASE_TREE = "t" * 40
REPAIR_TREE = "T" * 40
REPAIR_COMMIT = "C" * 40
BRANCH_TREE = "E" * 40
PINNED_BLOB = "b0" * 20
PATCHED_BLOB = "b1" * 20
MANIFEST_BLOB = "b2" * 20

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

GATE_NAMES = ["reproduction", "target-correctness", "layout-interaction",
              "repo-tests", "runtime-errors", "protected-ui", "scope-integrity"]


@pytest.fixture()
def pub_env(monkeypatch, tmp_path):
    """Isolated data dir + dummy (non-secret) tokens + approved repo."""
    from app.publish import service as publish_service

    monkeypatch.setenv("BUGLENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BUGLENS_API_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-dummy")
    monkeypatch.setenv("GITHUB_REPO", REPO)
    publish_service.reset_for_tests()
    yield {"headers": {"Authorization": f"Bearer {TOKEN}"}}
    publish_service.reset_for_tests()


@pytest.fixture()
def client(pub_env):
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
        id=uuid.uuid4().hex, title="Overlay blocks Pay now",
        observed_behavior="Clicking Pay now does nothing.",
        expected_behavior="Order confirmation appears.",
        reproduction_steps=["open checkout", "click Pay now"],
        repo=over.get("repo", REPO), ref="main", commit_sha=over.get("sha", SHA),
        browser_viewport=None, permitted_source_paths=[],
        created_at=now, updated_at=now)
    incident_store.create_incident(incident)
    import app.repro.store as repro_store

    run = ReproRun(
        id=uuid.uuid4().hex, incident_id=incident.id, repo=over.get("repo", REPO),
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
        repo=over.get("repo", REPO), commit_sha=over.get("sha", SHA),
        contract_hash="c" * 64, state="done",
        created_at=inv_now(), updated_at=inv_now())
    inv_store.create(inv)
    return incident, run, inv


def make_candidate(incident, run, inv, state="approved", diff=DIFF):
    import app.patches.store as patch_store
    from app.patches.models import Approval, Candidate, ValidationResult

    now = patch_store.utcnow()
    digest = hashlib.sha256(diff.encode()).hexdigest()
    candidate = Candidate(
        id=uuid.uuid4().hex, incident_id=incident.id, run_id=run.id,
        investigation_id=inv.id, hypothesis_rank=1, repo=incident.repo or REPO,
        baseline_sha=run.commit_sha, contract_hash=run.contract_hash,
        state=state, diff=diff, diff_sha256=digest,
        changed_paths=[TARGET] if diff else [],
        expected_effect="clicks reach the button",
        regression_risks=["none known"],
        validation=ValidationResult(ok=True) if state in ("proposed", "approved") else None,
        approval=(Approval(candidate_id="", diff_sha256=digest, approved_at=now)
                  if state == "approved" else None),
        created_at=now, updated_at=now)
    if candidate.approval is not None:
        candidate.approval.candidate_id = candidate.id
    return patch_store.create(candidate)


def patched_bytes():
    """Expected tested bytes, via the same trusted applier production uses."""
    from app.patches.validate import apply_in_memory, parse_diff

    parsed = parse_diff(DIFF)
    new_lines = apply_in_memory(PINNED_HTML.splitlines(), parsed[0].hunks)
    return ("\n".join(new_lines) + "\n").encode("utf-8")


def make_verification(candidate, incident, run, inv, **over):
    """A complete live-shaped VERIFIED record (artifacts + sandbox markers)."""
    import app.verify.store as verify_store
    from app.verify.models import (
        ApprovalSnapshot,
        GateResult,
        TestedIdentity,
        Verification,
    )

    now = verify_store.utcnow()
    digest = hashlib.sha256(patched_bytes()).hexdigest()
    gates = []
    for num, name in enumerate(GATE_NAMES, start=1):
        measurements: dict = {"checked": True}
        artifacts = ["bl-result.json", "cd-result.json"]
        if name == "layout-interaction":
            measurements = {"viewport": [1280, 800], "checked": True}
        if name == "repo-tests":
            measurements = {"candidate_collected": 93,
                            "candidate_failed": ["backend/tests/test_env.py::test_needs_win"],
                            "baseline_failed": ["backend/tests/test_env.py::test_needs_win"],
                            "new_failures": [], "checked": True}
            artifacts = ["gate4-bl-tests.txt", "gate4-cd-tests.txt"]
        gates.append(GateResult(gate=num, name=name,
                                verdict=over.get("verdict", "PASS"),
                                detail=f"{name} ok", measurements=measurements,
                                artifacts=artifacts))
    record = Verification(
        id=uuid.uuid4().hex, incident_id=incident.id, run_id=run.id,
        investigation_id=inv.id, candidate_id=candidate.id, repo=candidate.repo,
        contract_hash=candidate.contract_hash,
        policy_hash=over.get("policy_hash", "p" * 64),
        approval=ApprovalSnapshot(candidate_id=candidate.id,
                                  diff_sha256=candidate.diff_sha256,
                                  approved_at=candidate.approval.approved_at
                                  if candidate.approval else now),
        state=over.get("state", "done"), outcome=over.get("outcome", "VERIFIED"),
        gates=gates,
        tested=TestedIdentity(diff_sha256=candidate.diff_sha256,
                              file_hashes={TARGET: digest},
                              baseline_sha=candidate.baseline_sha),
        created_at=now, updated_at=now)
    verify_store.create(record)
    if not over.get("bare"):
        from app.publish.preflight import REQUIRED_ARTIFACTS

        for name in REQUIRED_ARTIFACTS:
            verify_store.record_artifact(record.id, name, "application/octet-stream",
                                         f"bytes-of-{name}".encode())
        for phase, message in (("bl", "node sandbox terminated"),
                               ("cd", "node sandbox terminated"),
                               ("gate4-bl", "python sandbox terminated"),
                               ("gate4-cd", "python sandbox terminated")):
            verify_store.add_event(record.id, phase, message)
        for num, name in enumerate(GATE_NAMES, start=1):
            verify_store.add_event(record.id, f"gate-{num}", f"{name}: PASS")
        verify_store.add_event(record.id, "lifecycle", "finished: done/VERIFIED")
    return verify_store.get(record.id)


class FakeGitHub:
    """Scriptable GitHub git-database + PR stub over MockTransport."""

    def __init__(self, *, base_sha=SHA, pinned_html=PINNED_HTML,
                 open_pr=None, commit_sha=REPAIR_COMMIT,
                 ref_exists_race=False, pr_exists_race=False, delay_s=0.0):
        self.base_sha = base_sha
        self.pinned_html = pinned_html
        self.branches = {}  # branch -> sha
        self.commit_details = {}  # sha -> full commit object
        self.open_pr = open_pr  # dict or None
        self.commit_sha = commit_sha
        self.ref_exists_race = ref_exists_race
        self.pr_exists_race = pr_exists_race
        self.delay_s = delay_s
        self.calls: list[tuple[str, str]] = []
        self.posted_trees: list[dict] = []
        self.posted_prs: list[dict] = []

    def seed_branch(self, branch, sha):
        self.branches[branch] = sha

    def seed_branch_commit(self, sha, tree_sha, message):
        self.commit_details[sha] = {
            "sha": sha, "tree": {"sha": tree_sha},
            "parents": [{"sha": SHA}], "message": message}

    def transport(self):
        blob_bytes = {
            MANIFEST_BLOB: MANIFEST_BYTES,
            PINNED_BLOB: self.pinned_html.encode(),
            PATCHED_BLOB: patched_bytes(),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if self.delay_s:
                import time as _time

                _time.sleep(self.delay_s)
            url = str(request.url)
            self.calls.append((request.method, url))
            if request.method == "GET" and "/git/trees/" in url:
                arg = url.split("/git/trees/", 1)[1].split("?")[0]
                if arg == BRANCH_TREE:
                    entries = [(TARGET, PATCHED_BLOB)]
                else:
                    entries = [(".buglens.json", MANIFEST_BLOB),
                               (TARGET, PINNED_BLOB)]
                return httpx.Response(200, json={
                    "truncated": False,
                    "tree": [{"path": p, "type": "blob", "mode": "100644",
                              "sha": sha} for p, sha in entries]})
            if request.method == "GET" and "/git/blobs/" in url:
                sha = url.rsplit("/", 1)[-1].split("?")[0]
                if sha not in blob_bytes:
                    return httpx.Response(404, json={"message": "nope"})
                return httpx.Response(200, json={
                    "encoding": "base64",
                    "content": base64.b64encode(blob_bytes[sha]).decode()})
            if request.method == "GET" and "/git/ref/heads/main" in url:
                if self.base_sha is None:
                    return httpx.Response(404, json={"message": "nope"})
                return httpx.Response(200, json={"object": {"sha": self.base_sha}})
            if request.method == "GET" and "/git/ref/heads/" in url:
                branch = url.split("/git/ref/heads/", 1)[1].split("?")[0]
                sha = self.branches.get(branch)
                if sha is None:
                    return httpx.Response(404, json={"message": "nope"})
                return httpx.Response(200, json={"object": {"sha": sha}})
            if request.method == "GET" and "/git/commits/" in url:
                sha = url.rsplit("/", 1)[-1].split("?")[0]
                if sha in self.commit_details:
                    return httpx.Response(200, json=self.commit_details[sha])
                return httpx.Response(200, json={
                    "sha": self.base_sha, "tree": {"sha": BASE_TREE},
                    "parents": [], "message": "base"})
            if request.method == "POST" and url.rstrip("/").endswith("/git/trees"):
                self.posted_trees.append(json.loads(request.content.decode()))
                return httpx.Response(201, json={"sha": REPAIR_TREE})
            if request.method == "POST" and url.rstrip("/").endswith("/git/commits"):
                return httpx.Response(201, json={"sha": self.commit_sha})
            if request.method == "POST" and url.rstrip("/").endswith("/git/refs"):
                if self.ref_exists_race:
                    return httpx.Response(
                        422, json={"message": "Reference already exists"})
                body = json.loads(request.content.decode())
                self.branches[body["ref"].split("refs/heads/", 1)[1]] = body["sha"]
                return httpx.Response(201, json={"ref": body["ref"]})
            if request.method == "GET" and "/pulls?" in url:
                pulls = [self.open_pr] if self.open_pr else []
                return httpx.Response(200, json=pulls)
            if request.method == "POST" and url.rstrip("/").endswith("/pulls"):
                if self.pr_exists_race:
                    return httpx.Response(
                        422, json={"message": "A pull request already exists."})
                body = json.loads(request.content.decode())
                self.posted_prs.append(body)
                number = 7
                return httpx.Response(201, json={
                    "number": number,
                    "html_url": f"https://github.com/{REPO}/pull/{number}"})
            return httpx.Response(404, json={"message": "nope"})

        return httpx.MockTransport(handler)

    def posts(self, suffix):
        return [url for method, url in self.calls
                if method == "POST" and url.rstrip("/").endswith(suffix)]


def check_map(preview):
    return {c.name: c for c in preview.preflight}


# ------------------------------------------------------------- preview


def test_preview_ok_shows_exact_change(pub_env):
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    preview = build_preview(candidate.id, verification.id, pr_title="",
                            token="ghp-dummy", transport=fake.transport())
    assert preview.preflight_ok, [c for c in preview.preflight if not c.ok]
    assert preview.repo == REPO
    assert preview.base_branch == "main"
    assert preview.base_sha == SHA
    assert preview.branch == f"buglens/fix-{candidate.id[:8]}"
    assert preview.changed_files == [TARGET]
    assert preview.diff == DIFF
    assert preview.diff_sha256 == candidate.diff_sha256
    assert preview.pr_title == incident.title  # blank title uses incident default
    assert SHA in preview.pr_body
    assert verification.id in preview.pr_body
    assert "repo-tests" in preview.pr_body
    assert "93 collected" in preview.pr_body
    assert "1280x800" in preview.pr_body
    assert "intentionally broken evaluation" in preview.pr_body
    assert "backend/tests/test_env.py::test_needs_win" in preview.pr_body
    assert "identical on baseline and candidate" in preview.pr_body
    assert "ghp-dummy" not in preview.pr_body
    assert "127.0.0.1" not in preview.pr_body
    assert "localhost" not in preview.pr_body.lower()


def test_preview_refuses_unapproved(pub_env):
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv, state="proposed")
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    preview = build_preview(candidate.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["candidate-approved"].ok is False


def test_preview_refuses_failed_and_blocked_gates(pub_env):
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    failed = make_verification(candidate, incident, run, inv, verdict="FAIL")
    fake = FakeGitHub()
    preview = build_preview(candidate.id, failed.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["gates-seven-pass"].ok is False

    blocked = make_verification(candidate, incident, run, inv,
                                state="blocked", outcome="BLOCKED")
    preview = build_preview(candidate.id, blocked.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["verification-outcome"].ok is False


def test_preview_refuses_mocked_verification(pub_env):
    """done/VERIFIED with no artifacts, bytes, or sandbox markers fails."""
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    bare = make_verification(candidate, incident, run, inv, bare=True)
    fake = FakeGitHub()
    preview = build_preview(candidate.id, bare.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["live-evidence"].ok is False


def test_preview_refuses_missing_artifact_bytes(pub_env):
    import app.verify.store as verify_store
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    stored = next(a.stored_filename for a in verification.artifacts
                  if a.name == "run.log")
    verify_store.safe_artifact_path(verification.id, stored).unlink()
    fake = FakeGitHub()
    preview = build_preview(candidate.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert "run.log" in check_map(preview)["live-evidence"].detail


def test_preview_refuses_cancelled_run(pub_env):
    import app.verify.store as verify_store
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    verify_store.add_event(verification.id, "lifecycle", "cancellation requested")
    fake = FakeGitHub()
    preview = build_preview(candidate.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["live-evidence"].ok is False


def test_preview_refuses_stale_baseline_and_changed_diff(pub_env):
    import app.patches.store as patch_store
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()

    def drift(item):
        item.baseline_sha = "cc" * 20

    patch_store.update(candidate.id, drift)
    preview = build_preview(candidate.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["provenance-chain"].ok is False

    def tamper(item):
        item.baseline_sha = run.commit_sha
        item.diff = item.diff.replace("<div>new</div>", "<div>evil</div>")

    patch_store.update(candidate.id, tamper)
    preview = build_preview(candidate.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["diff-hash"].ok is False


def test_preview_refuses_wrong_repository(pub_env):
    from app.publish.service import build_preview

    incident, run, inv = make_records(repo="someone/else")
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    preview = build_preview(candidate.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["repository-approved"].ok is False


def test_preview_refuses_superseded_candidate(pub_env):
    import app.patches.store as patch_store
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    first = make_candidate(incident, run, inv)
    verification = make_verification(first, incident, run, inv)
    second = make_candidate(incident, run, inv)

    def later(item):
        item.approval.approved_at = patch_store.utcnow()

    time.sleep(0.01)
    patch_store.update(second.id, later)
    fake = FakeGitHub()
    preview = build_preview(first.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["not-superseded"].ok is False


def test_preview_refuses_moved_remote_base_without_writes(pub_env):
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub(base_sha="dd" * 20)
    preview = build_preview(candidate.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["remote-base-pinned"].ok is False
    assert fake.posts("/git/refs") == []
    assert fake.posts("/pulls") == []


def test_preview_refuses_diverged_tested_bytes(pub_env):
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub(pinned_html=PINNED_HTML.replace("old", "changed"))
    preview = build_preview(candidate.id, verification.id, token="ghp-dummy",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["tested-bytes"].ok is False


def test_preview_without_token_fails_remote_checks(pub_env):
    from app.publish.service import build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    preview = build_preview(candidate.id, verification.id, token="",
                            transport=fake.transport())
    assert not preview.preflight_ok
    assert check_map(preview)["tested-bytes"].ok is False
    assert check_map(preview)["remote-base-pinned"].ok is False
    assert fake.calls == []  # no network without a token


def test_preview_rejects_bad_title(pub_env):
    from app.publish.service import PublishError, build_preview

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    with pytest.raises(PublishError):
        build_preview(candidate.id, verification.id, pr_title="line one\nline two",
                      token="ghp-dummy", transport=fake.transport())


# ------------------------------------------------------------- publish


def test_publish_creates_exact_repair_commit_and_pr(pub_env):
    from app.publish.service import publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    record = publish(candidate.id, verification.id, "Fix the overlay",
                     token="ghp-dummy", transport=fake.transport())
    assert record.state == "published"
    assert record.commit_sha == REPAIR_COMMIT
    assert record.pr_number == 7
    assert record.pr_url == f"https://github.com/{REPO}/pull/7"
    # The tree carries exactly the recomputed tested bytes (not a regen).
    assert len(fake.posted_trees) == 1
    posted = fake.posted_trees[0]
    assert posted["base_tree"] == BASE_TREE
    assert posted["tree"] == [{
        "path": TARGET, "mode": "100644", "type": "blob",
        "content": patched_bytes().decode("utf-8")}]
    assert fake.posts("/git/refs") != []
    assert len(fake.posted_prs) == 1
    assert fake.posted_prs[0]["head"] == record.branch
    assert fake.posted_prs[0]["base"] == "main"
    assert fake.posted_prs[0]["title"] == "Fix the overlay"
    assert fake.posted_prs[0].get("maintainer_can_modify") is False
    assert SHA in fake.posted_prs[0]["body"]


def test_publish_is_idempotent(pub_env):
    from app.publish import store as publish_store
    from app.publish.service import publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    first = publish(candidate.id, verification.id, "Fix the overlay",
                    token="ghp-dummy", transport=fake.transport())
    second = publish(candidate.id, verification.id, "Fix the overlay",
                     token="ghp-dummy", transport=fake.transport())
    assert second.id == first.id
    assert second.state == "published"
    assert len(fake.posted_prs) == 1
    assert len(publish_store.list_for_candidate(candidate.id)) == 1


def test_publish_reuses_existing_branch_and_pr(pub_env):
    """Partial success (branch + PR exist) is adopted, never duplicated."""
    from app.publish.service import branch_name, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    branch = branch_name(candidate.id)
    fake = FakeGitHub()
    fake.seed_branch(branch, REPAIR_COMMIT)
    fake.open_pr = {"number": 9,
                    "html_url": f"https://github.com/{REPO}/pull/9"}
    record = publish(candidate.id, verification.id, "Fix the overlay",
                     token="ghp-dummy", transport=fake.transport())
    assert record.state == "published"
    assert record.pr_number == 9
    assert fake.posts("/git/refs") == []
    assert fake.posted_prs == []


def test_publish_recovers_orphaned_intent(pub_env):
    """A crashed publish (intent left behind) completes from GitHub state."""
    from app.publish import store as publish_store
    from app.publish.models import Publication
    from app.publish.service import branch_name, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    branch = branch_name(candidate.id)
    now = publish_store.utcnow()
    orphan = Publication(
        id=uuid.uuid4().hex, incident_id=incident.id, candidate_id=candidate.id,
        verification_id=verification.id, repo=REPO, base_branch="main",
        base_sha=SHA, branch=branch, pr_title="Fix the overlay",
        state="publishing", detail="crashed mid-flight",
        created_at=now, updated_at=now)
    publish_store.create(orphan)
    fake = FakeGitHub()
    fake.seed_branch(branch, REPAIR_COMMIT)
    fake.seed_branch_commit(
        REPAIR_COMMIT, BRANCH_TREE,
        f"Fix the overlay\n\nVerified-by: BugLens {verification.id}")
    fake.open_pr = {"number": 11,
                    "html_url": f"https://github.com/{REPO}/pull/11"}
    record = publish(candidate.id, verification.id, "Fix the overlay",
                     token="ghp-dummy", transport=fake.transport())
    assert record.id == orphan.id
    assert record.state == "published"
    assert record.pr_number == 11
    assert record.commit_sha == REPAIR_COMMIT
    assert fake.posted_prs == []
    assert fake.posts("/git/refs") == []


def test_publish_retries_after_orphan_with_no_branch(pub_env):
    from app.publish import store as publish_store
    from app.publish.models import Publication
    from app.publish.service import branch_name, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    now = publish_store.utcnow()
    orphan = Publication(
        id=uuid.uuid4().hex, incident_id=incident.id, candidate_id=candidate.id,
        verification_id=verification.id, repo=REPO, base_branch="main",
        base_sha=SHA, branch=branch_name(candidate.id), state="publishing",
        detail="crashed before any write", created_at=now, updated_at=now)
    publish_store.create(orphan)
    fake = FakeGitHub()
    record = publish(candidate.id, verification.id, "Fix the overlay",
                     token="ghp-dummy", transport=fake.transport())
    assert record.state == "published"
    assert record.id != orphan.id
    assert publish_store.get(orphan.id).state == "failed"


def test_publish_adopts_equivalent_commit(pub_env):
    """A crashed attempt's identical commit (other SHA) is adopted, not rebuilt."""
    from app.publish.service import branch_name, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    branch = branch_name(candidate.id)
    other_sha = "f0" * 20
    fake.seed_branch(branch, other_sha)
    fake.commit_details[other_sha] = {
        "sha": other_sha, "tree": {"sha": REPAIR_TREE},
        "parents": [{"sha": SHA}],
        "message": f"Fix the overlay\n\nVerified-by: BugLens {verification.id}"}
    record = publish(candidate.id, verification.id, "Fix the overlay",
                     token="ghp-dummy", transport=fake.transport())
    assert record.state == "published"
    assert record.commit_sha == other_sha
    assert fake.posts("/git/refs") == []
    assert len(fake.posted_prs) == 1


def test_recovery_refuses_unproven_branch(pub_env):
    """A branch with the wrong bytes is never adopted by recovery."""
    from app.publish import store as publish_store
    from app.publish.models import Publication
    from app.publish.service import PublishConflict, branch_name, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    branch = branch_name(candidate.id)
    now = publish_store.utcnow()
    orphan = Publication(
        id=uuid.uuid4().hex, incident_id=incident.id, candidate_id=candidate.id,
        verification_id=verification.id, repo=REPO, base_branch="main",
        base_sha=SHA, branch=branch, pr_title="Fix the overlay",
        state="publishing", detail="crashed mid-flight",
        created_at=now, updated_at=now)
    publish_store.create(orphan)
    fake = FakeGitHub()
    other_sha = "e1" * 20
    fake.seed_branch(branch, other_sha)
    # Right message shape, but the tree still holds unpatched bytes.
    fake.seed_branch_commit(
        other_sha, "0" * 40,
        f"Fix the overlay\n\nVerified-by: BugLens {verification.id}")
    with pytest.raises(PublishConflict):
        publish(candidate.id, verification.id, "Fix the overlay",
                token="ghp-dummy", transport=fake.transport())
    assert publish_store.get(orphan.id).state == "failed"
    assert fake.posted_prs == []


def test_publish_refuses_foreign_branch(pub_env):
    from app.publish.service import PublishConflict, branch_name, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    fake.seed_branch(branch_name(candidate.id), "ee" * 20)
    with pytest.raises(PublishConflict):
        publish(candidate.id, verification.id, "Fix the overlay",
                token="ghp-dummy", transport=fake.transport())
    assert fake.posted_prs == []  # never filed on top of foreign content


def test_publish_refuses_without_preflight(pub_env):
    from app.publish.service import PublishError, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv, state="proposed")
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    with pytest.raises(PublishError):
        publish(candidate.id, verification.id, "Fix the overlay",
                token="ghp-dummy", transport=fake.transport())
    assert fake.posts("/git/trees") == []
    assert fake.posts("/git/refs") == []
    assert fake.posts("/pulls") == []


def test_publish_requires_token(pub_env):
    from app.publish.service import PublishError, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub()
    with pytest.raises(PublishError):
        publish(candidate.id, verification.id, "Fix the overlay",
                token="", transport=fake.transport())
    assert fake.calls == []


def test_concurrent_publish_single_flight(pub_env):
    from app.publish.service import PublishBusy, publish

    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    fake = FakeGitHub(delay_s=0.2)
    results: dict[str, object] = {}

    def attempt(slot: str):
        try:
            results[slot] = publish(candidate.id, verification.id,
                                    "Fix the overlay", token="ghp-dummy",
                                    transport=fake.transport())
        except Exception as exc:  # noqa: BLE001 - asserting below
            results[slot] = exc

    first = threading.Thread(target=attempt, args=("first",))
    first.start()
    assert first.is_alive()
    time.sleep(0.6)  # let the first attempt take the worker
    attempt("second")
    first.join(timeout=30)
    assert isinstance(results["second"], PublishBusy)
    assert getattr(results["first"], "state", None) == "published"
    assert len(fake.posted_prs) == 1


# ------------------------------------------------------------ endpoints


def test_preview_endpoint_ok_and_guarded(client, pub_env):
    incident, run, inv = make_records()
    candidate = make_candidate(incident, run, inv)
    verification = make_verification(candidate, incident, run, inv)
    payload = {"candidate_id": candidate.id,
               "verification_id": verification.id, "pr_title": "T"}
    denied = client.post("/api/publications/preview", json=payload)
    assert denied.status_code == 401
    # Empty token: local checks pass, remote checks fail fast, no network.
    import app.routes_publish as routes

    real_token = routes.config.get_github_token
    routes.config.get_github_token = lambda: ""
    try:
        response = client.post("/api/publications/preview", json=payload,
                               headers=pub_env["headers"])
    finally:
        routes.config.get_github_token = real_token
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_id"] == candidate.id
    assert body["preflight_ok"] is False
    assert any(not c["ok"] and c["name"] == "tested-bytes"
               for c in body["preflight"])


def test_publish_endpoint_guards(client, pub_env):
    import app.routes_publish as routes

    payload = {"candidate_id": "f" * 32, "verification_id": "e" * 32,
               "pr_title": "T"}
    assert client.post("/api/publications", json=payload).status_code == 401
    assert client.get(f"/api/publications/{'f' * 32}").status_code == 401

    real_token = routes.config.get_github_token
    routes.config.get_github_token = lambda: ""
    try:
        refused = client.post("/api/publications", json=payload,
                              headers=pub_env["headers"])
        unknown = client.get(f"/api/publications/{'f' * 32}",
                             headers=pub_env["headers"])
        bad_id = client.get("/api/publications/not-an-id",
                            headers=pub_env["headers"])
    finally:
        routes.config.get_github_token = real_token
    assert refused.status_code == 409  # no token: refused before any write
    assert unknown.status_code == 404
    assert bad_id.status_code == 404
