"""Published-source resolution over the GitHub REST API (server-side only).

Resolves the moving branch to an immutable commit SHA, lists the exact
tree at that SHA, and fetches blob bytes (manifest, symlink targets).
The caller's token never leaves this process except in Authorization
headers, and error details are sanitized.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .. import config
from ..providers.base import bounded_request, error_detail, sanitize

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class GitHubSourceError(Exception):
    """Raised when published source cannot be resolved or fetched."""


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    type: str  # blob | tree | commit
    sha: str
    size: Optional[int] = None

    @property
    def is_symlink(self) -> bool:
        return self.type == "blob" and self.mode == "120000"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "buglens-ai",
    }


def _client(transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    return httpx.Client(transport=transport)


def resolve_ref_sha(
    repo: str, ref: str, token: str, transport: Optional[httpx.BaseTransport] = None
) -> str:
    """Resolve a branch (usually ``main``) to its full commit SHA (empty repo -> error)."""
    with _client(transport) as client:
        outcome = bounded_request(
            client,
            "GET",
            f"{config.GITHUB_API_BASE_URL}/repos/{repo}/git/ref/heads/{ref}",
            headers=_headers(token),
            secrets=[token],
            timeout_s=config.PROVIDER_TIMEOUT_S,
            max_retries=config.PROVIDER_MAX_RETRIES,
        )
    if not outcome.ok:
        raise GitHubSourceError(outcome.detail)
    if outcome.status == 404:
        raise GitHubSourceError(
            f"branch {ref} not found in {repo} (repository may have no commits)"
        )
    sha = outcome.body.get("object", {}).get("sha") if isinstance(outcome.body, dict) else None
    if outcome.status != 200 or not (isinstance(sha, str) and _SHA_PATTERN.match(sha)):
        raise GitHubSourceError(
            sanitize(
                error_detail(outcome.status, outcome.body, f"could not resolve {ref}"),
                [token],
            )
        )
    return sha


def fetch_tree(
    repo: str, sha: str, token: str, transport: Optional[httpx.BaseTransport] = None
) -> list[TreeEntry]:
    """List every tree entry at an exact commit SHA (refuses truncation)."""
    with _client(transport) as client:
        outcome = bounded_request(
            client,
            "GET",
            f"{config.GITHUB_API_BASE_URL}/repos/{repo}/git/trees/{sha}?recursive=1",
            headers=_headers(token),
            secrets=[token],
            timeout_s=config.PROVIDER_TIMEOUT_S,
            max_retries=config.PROVIDER_MAX_RETRIES,
        )
    if not outcome.ok:
        raise GitHubSourceError(outcome.detail)
    body = outcome.body if isinstance(outcome.body, dict) else {}
    if outcome.status != 200 or not isinstance(body.get("tree"), list):
        raise GitHubSourceError(
            sanitize(
                error_detail(outcome.status, outcome.body, f"could not list tree at {sha[:7]}"),
                [token],
            )
        )
    if body.get("truncated"):
        raise GitHubSourceError("repository tree truncated by GitHub; refusing partial verification")
    entries = []
    for item in body["tree"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        entries.append(
            TreeEntry(
                path=item["path"],
                mode=str(item.get("mode", "")),
                type=str(item.get("type", "")),
                sha=str(item.get("sha", "")),
                size=item.get("size") if isinstance(item.get("size"), int) else None,
            )
        )
    return entries


def fetch_blob(
    repo: str, blob_sha: str, token: str, transport: Optional[httpx.BaseTransport] = None
) -> bytes:
    """Fetch exact blob bytes for a tree entry."""
    with _client(transport) as client:
        outcome = bounded_request(
            client,
            "GET",
            f"{config.GITHUB_API_BASE_URL}/repos/{repo}/git/blobs/{blob_sha}",
            headers=_headers(token),
            secrets=[token],
            timeout_s=config.PROVIDER_TIMEOUT_S,
            max_retries=config.PROVIDER_MAX_RETRIES,
        )
    if not outcome.ok:
        raise GitHubSourceError(outcome.detail)
    body = outcome.body if isinstance(outcome.body, dict) else {}
    if outcome.status != 200 or body.get("encoding") != "base64" or not isinstance(
        body.get("content"), str
    ):
        raise GitHubSourceError(
            sanitize(
                error_detail(outcome.status, outcome.body, "could not fetch blob"),
                [token],
            )
        )
    try:
        return base64.b64decode(body["content"])
    except (binascii.Error, ValueError) as exc:
        raise GitHubSourceError(f"blob payload is not valid base64: {exc}")
