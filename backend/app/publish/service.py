"""Publication orchestration: preview assembly and idempotent publish.

Publish is synchronous and serialized (one GitHub write at a time,
like verification). Every call re-runs server-side preflight from
stored records and live GitHub reads; the stored publication intent
is only a recovery record, never a substitute for validation.

Idempotency: a candidate that already has a ``published`` record is
returned as-is (no new GitHub writes). A ``publishing`` record means
a previous attempt died mid-flight: publish reconciles it against
GitHub (branch + open PR) and completes the record instead of
duplicating the branch or PR. Concurrent calls serialize on the
worker semaphore; the loser gets ``PublishBusy`` (HTTP 429).
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any, NoReturn, Optional

import httpx

from .. import config
from .. import store as incident_store
from ..investigate import store as inv_store
from ..providers.base import sanitize
from ..repro.github_source import GitHubSourceError, fetch_blob, fetch_tree
from . import github, preflight, prbody, store
from .models import PreflightCheck, Publication, PublicationPreview
from .preflight import PreflightError

BASE_BRANCH = "main"

_capacity_lock = threading.Lock()
_capacity: Optional[threading.Semaphore] = None

_live_lock = threading.Lock()
_live: set[str] = set()  # publication ids with a worker inside publish()


class PublishError(Exception):
    """Publication refused or failed (safe detail for the API response)."""


class PublishBusy(Exception):
    """Another publication holds the worker; retry shortly."""


class PublishConflict(Exception):
    """GitHub already has conflicting content; no write was forced."""


def _semaphore() -> threading.Semaphore:
    global _capacity
    with _capacity_lock:
        if _capacity is None:
            _capacity = threading.Semaphore(max(1, config.PUBLISH_MAX_CONCURRENT))
        return _capacity


def reset_for_tests() -> None:
    """Drop the cached semaphore (tests that republish serially)."""
    global _capacity
    with _capacity_lock:
        _capacity = None


def branch_name(candidate_id: str) -> str:
    return f"buglens/fix-{candidate_id[:8]}"


_TITLE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


def check_title(title: str) -> str:
    """Validate a developer-supplied PR title (single line, bounded)."""
    cleaned = (title or "").strip()
    if not _TITLE_RE.match(cleaned):
        raise PublishError("PR title must be one line of 1..200 printable characters.")
    return cleaned


def _default_title(incident: Any) -> str:
    return (incident.title or "BugLens verified fix").strip() or "BugLens verified fix"


def build_preview(candidate_id: str, verification_id: str, pr_title: str = "",
                  base_branch: str = BASE_BRANCH, token: str = "",
                  transport: Optional[httpx.BaseTransport] = None,
                  notes: str = "") -> PublicationPreview:
    """Assemble the exact publish preview, including a full preflight.

    Read-only: performs GitHub reads but no writes. Raises PublishError
    when the records needed for a preview are missing.
    """
    checks, candidate, verification = preflight.local_preflight(candidate_id, verification_id)
    if candidate is None or verification is None:
        raise PublishError("Candidate or verification record is missing or mismatched.")
    branch = branch_name(candidate.id)
    preflight.remote_preflight(checks, candidate, verification, branch, base_branch,
                               token, transport=transport)
    incident = incident_store.get_incident(candidate.incident_id)
    investigation = inv_store.get(candidate.investigation_id)
    if incident is None or investigation is None:
        raise PublishError("Linked incident or investigation record is missing.")
    title = check_title(pr_title) if pr_title else _default_title(incident)
    body = prbody.build_pr_body(incident=incident, candidate=candidate,
                                verification=verification, investigation=investigation,
                                notes=notes)
    return PublicationPreview(
        repo=candidate.repo, base_branch=base_branch, base_sha=candidate.baseline_sha,
        branch=branch, changed_files=list(candidate.changed_paths), diff=candidate.diff,
        diff_sha256=candidate.diff_sha256, pr_title=title, pr_body=body,
        verification_id=verification.id, candidate_id=candidate.id,
        preflight=checks, preflight_ok=all(c.ok for c in checks))


def _failed_checks(checks: list[PreflightCheck]) -> str:
    failed = [c for c in checks if not c.ok]
    return "; ".join(f"{c.name}: {c.detail}".rstrip() for c in failed) or "unknown"


def commit_message_for(pr_title: str, verification_id: str) -> str:
    return f"{pr_title}\n\nVerified-by: BugLens {verification_id}"


def _verify_recovered_branch(record: Publication, candidate: Any, branch_sha: str,
                             token: str,
                             transport: Optional[httpx.BaseTransport]) -> None:
    """Confirm an adopted branch holds exactly the authorized change.

    Read-only: checks the commit message (authorized title +
    verification id), the single parent (pinned base), and the changed
    file bytes (recomputed tested bytes). Raises PublishError when the
    branch cannot be proven ours.
    """
    if not record.pr_title:
        raise PublishError("intent predates title binding; refusing to guess")
    try:
        detail = github.get_commit(record.repo, branch_sha, token, transport=transport)
    except github.GitHubPublishError as exc:
        raise PublishError(f"branch commit unreadable: {exc}")
    if detail.parents != (record.base_sha,):
        raise PublishError("branch commit has an unexpected parent")
    if detail.message != commit_message_for(record.pr_title, record.verification_id):
        raise PublishError("branch commit message differs from the authorized preview")
    try:
        _pinned, patched = preflight.compute_change(candidate, token, transport=transport)
    except PreflightError as exc:
        raise PublishError(f"change recomputation failed: {exc}")
    try:
        tree = fetch_tree(record.repo, detail.tree_sha, token, transport=transport)
    except GitHubSourceError as exc:
        raise PublishError(f"branch tree unreadable: {exc}")
    by_path = {entry.path: entry for entry in tree}
    for path, want in patched.items():
        entry = by_path.get(path)
        if entry is None or entry.type != "blob":
            raise PublishError(f"branch tree lacks {path}")
        try:
            got = fetch_blob(record.repo, entry.sha, token, transport=transport)
        except GitHubSourceError as exc:
            raise PublishError(f"branch blob unreadable: {exc}")
        if got != want:
            raise PublishError(f"branch content for {path} differs from the tested bytes")


def _reconcile_existing(candidate_id: str, token: str,
                        transport: Optional[httpx.BaseTransport]) -> Optional[Publication]:
    """Complete an orphaned ``publishing`` record from live GitHub state.

    Returns the completed record, or None when no orphaned record
    exists. Never creates branches or PRs — reads only.
    """
    for record in store.list_for_candidate(candidate_id):
        if record.state != "publishing" or record.id in _live:
            continue
        store.add_event(record.id, "recover", "reconciling orphaned publish intent")
        local_checks, local_candidate, _ = preflight.local_preflight(
            candidate_id, record.verification_id)
        if local_candidate is None or not all(c.ok for c in local_checks):
            failed = next((c.name for c in local_checks if not c.ok), "records missing")

            def mark_stale(item: Publication, reason: str = failed) -> None:
                item.state = "failed"
                item.detail = f"orphaned intent no longer valid: {reason}"

            store.update(record.id, mark_stale)
            continue
        assert local_candidate is not None
        try:
            branch_sha = github.get_branch_sha(record.repo, record.branch, token,
                                               transport=transport)
        except github.GitHubPublishError as exc:
            store.add_event(record.id, "recover", f"branch read failed: {exc}")
            continue
        if branch_sha is None:
            def mark_failed(item: Publication) -> None:
                item.state = "failed"
                item.detail = "orphaned intent: no repair branch on GitHub; safe to retry"

            store.update(record.id, mark_failed)
            continue
        try:
            _verify_recovered_branch(record, local_candidate, branch_sha, token, transport)
        except PublishError as exc:
            store.add_event(record.id, "recover", f"branch identity check failed: {exc}")

            def mark_unproven(item: Publication, reason: str = str(exc)) -> None:
                item.state = "failed"
                item.detail = f"orphaned intent: existing branch unproven: {reason}"

            store.update(record.id, mark_unproven)
            continue
        try:
            existing = github.find_open_pr(record.repo, record.branch, token,
                                           transport=transport)
        except github.GitHubPublishError as exc:
            store.add_event(record.id, "recover", f"PR read failed: {exc}")
            continue
        if existing is None:
            def mark_failed(item: Publication) -> None:
                item.state = "failed"
                item.detail = (f"orphaned intent: verified branch {record.branch} exists "
                                "with no open PR; a fresh attempt will reuse it")

            store.update(record.id, mark_failed)
            continue
        number = int(existing["number"])
        url = str(existing.get("html_url") or "")

        def mark_published(item: Publication) -> None:
            item.state = "published"
            item.commit_sha = branch_sha
            item.pr_number = number
            item.pr_url = url
            item.detail = "recovered: identity-verified branch and PR already on GitHub"

        updated = store.update(record.id, mark_published)
        if updated is not None:
            store.add_event(record.id, "recover", f"recovered PR #{number}")
            return updated
    return None


def publish(candidate_id: str, verification_id: str, pr_title: str,
            base_branch: str = BASE_BRANCH, token: str = "",
            transport: Optional[httpx.BaseTransport] = None,
            notes: str = "") -> Publication:
    """Publish the verified repair (explicit Publish only — performs writes).

    Re-runs full preflight, builds the repair commit from the pinned
    baseline plus the exact recomputed bytes, creates (or verified-
    reuses) the repair branch, and opens (or reuses) the PR. Never
    force-pushes, never auto-merges.
    """
    if not _semaphore().acquire(blocking=False):
        raise PublishBusy("Another publication is running; retry shortly.")
    try:
        return _publish_locked(candidate_id, verification_id, pr_title, base_branch,
                               token, transport, notes=notes)
    finally:
        _semaphore().release()


def _recorded_pr_matches_branch(record: Publication, token: str,
                                transport: Optional[httpx.BaseTransport]) -> bool:
    """Confirm a published record's PR was really filed from its branch."""
    if not record.pr_number:
        return True  # legacy record without a number: nothing to check
    pr = github.get_pr(record.repo, record.pr_number, token, transport=transport)
    if pr is None:
        return False
    return (pr.get("head") or {}).get("ref") == record.branch


def _publish_locked(candidate_id: str, verification_id: str, pr_title: str,
                    base_branch: str, token: str,
                    transport: Optional[httpx.BaseTransport],
                    notes: str = "") -> Publication:
    if not token:
        raise PublishError("GITHUB_TOKEN is not configured; publication refused.")
    for record in store.list_for_candidate(candidate_id):
        if record.state == "published" and record.verification_id == verification_id:
            if _recorded_pr_matches_branch(record, token, transport):
                return record  # idempotent: exact same publication already done
            # A "published" record pointing at another branch's PR is not
            # this publication: retire it and continue to a fresh one.

            def mark_mismatch(item: Publication,
                              number: Optional[int] = record.pr_number) -> None:
                item.state = "failed"
                item.detail = (f"superseded: recorded PR #{number} belongs to a "
                               "different branch; republishing")

            store.update(record.id, mark_mismatch)
            store.add_event(record.id, "recover",
                            f"recorded PR #{record.pr_number} mismatched {record.branch}")
    recovered = _reconcile_existing(candidate_id, token, transport)
    if recovered is not None and recovered.verification_id == verification_id:
        return recovered

    try:
        preview = build_preview(candidate_id, verification_id, pr_title=pr_title,
                                base_branch=base_branch, token=token, transport=transport,
                                notes=notes)
    except PublishError as exc:
        raise PublishError(f"Preflight failed: {exc}")
    if not preview.preflight_ok:
        raise PublishError(f"Preflight failed: {_failed_checks(preview.preflight)}")
    title = preview.pr_title  # validated title (explicit or incident default)

    from ..patches import store as patch_store

    candidate = patch_store.get(candidate_id)
    if candidate is None:  # pragma: no cover - preview validated it exists
        raise PublishError("Candidate record vanished mid-publish.")
    now = store.utcnow()
    record = Publication(
        id=uuid.uuid4().hex, incident_id=candidate.incident_id,
        candidate_id=preview.candidate_id, verification_id=preview.verification_id,
        repo=preview.repo, base_branch=base_branch, base_sha=preview.base_sha,
        branch=preview.branch, pr_title=title, state="publishing",
        detail="intent persisted; GitHub writes starting",
        preflight=preview.preflight, created_at=now, updated_at=now)
    store.create(record)
    store.add_event(record.id, "intent", f"publish authorized for {record.branch}")
    with _live_lock:
        _live.add(record.id)
    try:
        return _execute(record, candidate, title, preview.pr_body, token, transport)
    finally:
        with _live_lock:
            _live.discard(record.id)


def _execute(record: Publication, candidate: Any, title: str, body: str, token: str,
             transport: Optional[httpx.BaseTransport]) -> Publication:
    try:
        _pinned, patched = preflight.compute_change(candidate, token, transport=transport)
    except PreflightError as exc:
        return _fail(record, f"change recomputation failed: {exc}")
    store.add_event(record.id, "change",
                    f"{len(patched)} file(s) recomputed from pinned bytes")
    try:
        base_tree = github.get_commit_tree(record.repo, record.base_sha, token,
                                           transport=transport)
        texts = {path: data.decode("utf-8") for path, data in sorted(patched.items())}
        tree_sha = github.create_tree(record.repo, base_tree, texts, token,
                                      transport=transport)
        commit_message = commit_message_for(title, record.verification_id)
        commit_sha = github.create_commit(record.repo, commit_message, tree_sha,
                                          record.base_sha, token, transport=transport)
    except github.GitHubPublishError as exc:
        return _fail(record, f"repair commit failed: {exc}")
    store.add_event(record.id, "commit", f"repair commit {commit_sha[:12]} created")

    try:
        branch_sha = github.get_branch_sha(record.repo, record.branch, token,
                                           transport=transport)
    except github.GitHubPublishError as exc:
        return _fail(record, f"branch read failed: {exc}")
    if branch_sha is None:
        try:
            github.create_branch(record.repo, record.branch, commit_sha, token,
                                 transport=transport)
        except github.GitHubPublishError as exc:
            # Lost a race with another publisher: re-read before deciding.
            if "already exists" not in sanitize(str(exc), [token]).lower():
                return _fail(record, f"branch creation failed: {exc}")
            try:
                branch_sha = github.get_branch_sha(record.repo, record.branch, token,
                                                   transport=transport)
            except github.GitHubPublishError as exc2:
                return _fail(record, f"branch re-read failed: {exc2}")
            if branch_sha is None:
                return _fail(record, "branch vanished during re-read; safe to retry")
        else:
            branch_sha = commit_sha
            store.add_event(record.id, "branch", f"{record.branch} created")
    if branch_sha != commit_sha:
        # A crashed attempt may have left our exact commit (same tree,
        # parent, message) under a different SHA (timestamps differ).
        # Adopt it only on full equivalence; otherwise never overwrite.
        try:
            detail = github.get_commit(record.repo, branch_sha, token,
                                       transport=transport)
        except github.GitHubPublishError as exc:
            return _fail(record, f"branch commit read failed: {exc}")
        equivalent = (detail.tree_sha == tree_sha
                      and detail.parents == (record.base_sha,)
                      and detail.message == commit_message)
        if not equivalent:
            return _fail(record, (
                f"branch {record.branch} points at {branch_sha[:12]}, not the repair "
                f"commit {commit_sha[:12]}; refusing to overwrite"), conflict=True)
        commit_sha = branch_sha
        store.add_event(record.id, "branch",
                        f"{record.branch} adopted at equivalent commit {branch_sha[:12]}")
    else:
        store.add_event(record.id, "branch",
                        f"{record.branch} verified at {branch_sha[:12]}")

    try:
        existing = github.find_open_pr(record.repo, record.branch, token,
                                       transport=transport)
    except github.GitHubPublishError as exc:
        return _fail(record, f"PR search failed: {exc}")
    if existing is not None:
        number, url = int(existing["number"]), str(existing.get("html_url") or "")
        store.add_event(record.id, "pr", f"reusing open PR #{number}")
    else:
        try:
            created = github.create_pull(record.repo, title, body, record.branch,
                                         record.base_branch, token, transport=transport)
        except github.GitHubPublishError as exc:
            if "already exists" not in sanitize(str(exc), [token]).lower():
                return _fail(record, f"PR creation failed: {exc}")
            try:
                existing = github.find_open_pr(record.repo, record.branch, token,
                                               transport=transport)
            except github.GitHubPublishError as exc2:
                return _fail(record, f"PR re-read failed: {exc2}")
            if existing is None:
                return _fail(record, "PR creation conflicted but no open PR was found")
            number, url = int(existing["number"]), str(existing.get("html_url") or "")
            store.add_event(record.id, "pr", f"recovered open PR #{number} after conflict")
        else:
            number, url = int(created["number"]), str(created.get("html_url") or "")
            store.add_event(record.id, "pr", f"opened PR #{number}")

    def mark_published(item: Publication) -> None:
        item.state = "published"
        item.commit_sha = commit_sha
        item.pr_number = number
        item.pr_url = url
        item.detail = "published; never auto-merged"

    updated = store.update(record.id, mark_published)
    if updated is None:  # pragma: no cover - record created above
        raise PublishError("Publication record vanished mid-publish.")
    return updated


def _fail(record: Publication, detail: str, conflict: bool = False) -> NoReturn:
    def mark_failed(item: Publication) -> None:
        item.state = "failed"
        item.detail = detail[:500]

    updated = store.update(record.id, mark_failed)
    store.add_event(record.id, "failed", detail[:300])
    raise (PublishConflict if conflict else PublishError)(detail)
