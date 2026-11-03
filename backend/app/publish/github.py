"""GitHub git-database + PR client for the trusted publisher.

All calls go through the bounded, sanitized provider transport; the
token never appears in errors or logs. Reads (refs, commits, trees,
blobs, PR search) back preflight; writes (refs, trees, commits, PRs)
run only from an explicit Publish after a passing preflight. Updates
are never forced: a branch that points at unexpected content is a
conflict, never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .. import config
from ..providers.base import bounded_request, error_detail, sanitize


class GitHubPublishError(Exception):
    """A GitHub read or write failed (sanitized, no credentials)."""


@dataclass(frozen=True)
class CommitDetail:
    sha: str
    tree_sha: str
    parents: tuple[str, ...]
    message: str


def _client(transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    if transport is not None:
        return httpx.Client(transport=transport)
    return httpx.Client()


def _call(
    method: str,
    path: str,
    token: str,
    json_body: Optional[dict] = None,
    transport: Optional[httpx.BaseTransport] = None,
    expect: tuple[int, ...] = (200,),
    action: str = "GitHub request",
) -> Any:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "buglens-ai",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    with _client(transport) as client:
        outcome = bounded_request(
            client,
            method,
            f"{config.GITHUB_API_BASE_URL}{path}",
            headers=headers,
            json_body=json_body,
            secrets=[token],
            timeout_s=config.PROVIDER_TIMEOUT_S,
            max_retries=config.PROVIDER_MAX_RETRIES,
        )
    if not outcome.ok:
        raise GitHubPublishError(outcome.detail)
    if outcome.status == 404 and 404 in expect:
        return None
    if outcome.status not in expect:
        raise GitHubPublishError(
            sanitize(error_detail(outcome.status, outcome.body, action), [token])
        )
    return outcome.body


def get_branch_sha(repo: str, branch: str, token: str,
                   transport: Optional[httpx.BaseTransport] = None) -> Optional[str]:
    """Current HEAD SHA of a branch, or None when the branch is absent."""
    body = _call("GET", f"/repos/{repo}/git/ref/heads/{branch}", token,
                 transport=transport, expect=(200, 404), action="read branch ref")
    if body is None:
        return None
    sha = (body.get("object") or {}).get("sha")
    if not sha:
        raise GitHubPublishError(f"branch {branch} ref has no commit SHA")
    return str(sha)


def get_commit_tree(repo: str, sha: str, token: str,
                    transport: Optional[httpx.BaseTransport] = None) -> str:
    """Tree SHA of a commit (base for the repair commit)."""
    body = _call("GET", f"/repos/{repo}/git/commits/{sha}", token,
                 transport=transport, action="read base commit")
    tree = (body.get("tree") or {}).get("sha")
    if not tree:
        raise GitHubPublishError(f"commit {sha[:12]} has no tree SHA")
    return str(tree)


def get_commit(repo: str, sha: str, token: str,
               transport: Optional[httpx.BaseTransport] = None) -> CommitDetail:
    """Full commit detail (tree, parents, message) for equivalence checks."""
    body = _call("GET", f"/repos/{repo}/git/commits/{sha}", token,
                 transport=transport, action="read commit detail")
    try:
        tree_sha = str(body["tree"]["sha"])
        parents = tuple(str(p["sha"]) for p in body.get("parents", []))
        message = str(body.get("message", ""))
    except (KeyError, TypeError, AttributeError):
        raise GitHubPublishError(f"commit {sha[:12]} detail is malformed")
    return CommitDetail(sha=sha, tree_sha=tree_sha, parents=parents, message=message)


def create_tree(repo: str, base_tree: str, files: dict[str, str], token: str,
                transport: Optional[httpx.BaseTransport] = None) -> str:
    """New tree = base tree + exact file contents; returns the tree SHA."""
    body = _call(
        "POST", f"/repos/{repo}/git/trees", token,
        json_body={
            "base_tree": base_tree,
            "tree": [
                {"path": path, "mode": "100644", "type": "blob", "content": content}
                for path, content in sorted(files.items())
            ],
        },
        transport=transport, expect=(201,), action="create repair tree")
    sha = body.get("sha")
    if not sha:
        raise GitHubPublishError("tree creation returned no SHA")
    return str(sha)


def create_commit(repo: str, message: str, tree_sha: str, parent_sha: str, token: str,
                  transport: Optional[httpx.BaseTransport] = None) -> str:
    """One repair commit on the base; returns the commit SHA."""
    body = _call(
        "POST", f"/repos/{repo}/git/commits", token,
        json_body={"message": message, "tree": tree_sha, "parents": [parent_sha]},
        transport=transport, expect=(201,), action="create repair commit")
    sha = body.get("sha")
    if not sha:
        raise GitHubPublishError("commit creation returned no SHA")
    return str(sha)


def create_branch(repo: str, branch: str, sha: str, token: str,
                  transport: Optional[httpx.BaseTransport] = None) -> None:
    """Point a new branch at a commit (ref must not already exist)."""
    _call("POST", f"/repos/{repo}/git/refs", token,
          json_body={"ref": f"refs/heads/{branch}", "sha": sha},
          transport=transport, expect=(201,), action="create repair branch")


def find_open_pr(repo: str, branch: str, token: str,
                 transport: Optional[httpx.BaseTransport] = None) -> Optional[dict]:
    """Open PR (if any) already filed from the repair branch."""
    body = _call("GET", f"/repos/{repo}/pulls?head={branch}&state=open", token,
                 transport=transport, action="search existing PRs")
    if not isinstance(body, list) or not body:
        return None
    first = body[0]
    if not isinstance(first, dict) or not first.get("number"):
        return None
    return first


def create_pull(repo: str, title: str, body_text: str, head: str, base: str, token: str,
                transport: Optional[httpx.BaseTransport] = None) -> dict:
    """Open the repair PR; returns the created PR object."""
    body = _call(
        "POST", f"/repos/{repo}/pulls", token,
        json_body={"title": title, "body": body_text, "head": head, "base": base,
                   "maintainer_can_modify": False},
        transport=transport, expect=(201,), action="create repair PR")
    if not isinstance(body, dict) or not body.get("number"):
        raise GitHubPublishError("PR creation returned no PR number")
    return body
