"""GitHub REST API adapter (docs: https://docs.github.com/rest).

Smoke tests are read-only and bounded:

- ``auth``: ``GET /user`` proves the token authenticates (records the
  login, which is not a secret).
- ``repo``: ``GET /repos/{owner}/{name}`` proves read access to the
  explicitly configured repository. No repository is ever invented:
  without ``GITHUB_REPO`` this check reports ``not_configured`` with the
  exact setup needed.

No code is run, deployed, or published; no PRs are created.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .. import config
from .base import (
    CheckResult,
    ProviderReport,
    bounded_request,
    error_detail,
    sanitize,
    summarize,
)

REPO_SETUP = "Set GITHUB_REPO=owner/name in .env to the approved repository."


class GitHubAdapter:
    name = "github"

    def __init__(
        self,
        token: str = "",
        repo: str = "",
        base_url: str = "",
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._token = token
        self._repo = (repo or "").strip()
        self._base_url = base_url or config.GITHUB_API_BASE_URL
        self._transport = transport

    def _secrets(self) -> list[str]:
        return [self._token] if self._token else []

    def status(self) -> ProviderReport:
        if not self._token:
            return ProviderReport(
                name=self.name,
                state="not_configured",
                detail="Set GITHUB_TOKEN in .env (a fine-grained token with repository read access).",
                checks=[
                    CheckResult(name="auth", state="not_configured", detail="GITHUB_TOKEN missing"),
                    CheckResult(name="repo", state="not_configured", detail="GITHUB_TOKEN missing"),
                ],
            )
        repo_state = "configured_untested"
        repo_detail = REPO_SETUP if not self._repo else ""
        return ProviderReport(
            name=self.name,
            state="configured_untested",
            detail="GITHUB_TOKEN is set; no smoke test has run in this process.",
            checks=[
                CheckResult(name="auth", state="configured_untested"),
                CheckResult(
                    name="repo",
                    state="not_configured" if not self._repo else repo_state,
                    detail=repo_detail,
                ),
            ],
        )

    def smoke(self) -> ProviderReport:
        if not self._token:
            return self.status()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "buglens-ai",
        }
        with httpx.Client(transport=self._transport) as client:
            auth_check = self._smoke_auth(client, headers)
            checks = [auth_check]
            if not self._repo:
                checks.append(
                    CheckResult(name="repo", state="not_configured", detail=REPO_SETUP)
                )
            elif auth_check.state != "verified":
                checks.append(
                    CheckResult(
                        name="repo",
                        state="failed",
                        detail="skipped: authentication check failed first",
                    )
                )
            else:
                checks.append(self._smoke_repo(client, headers))
        report = summarize(self.name, checks)
        report.checked_at = datetime.now(timezone.utc).isoformat()
        return report

    def _smoke_auth(self, client: httpx.Client, headers: dict[str, str]) -> CheckResult:
        outcome = bounded_request(
            client,
            "GET",
            f"{self._base_url}/user",
            headers=headers,
            secrets=self._secrets(),
            timeout_s=config.PROVIDER_TIMEOUT_S,
            max_retries=config.PROVIDER_MAX_RETRIES,
        )
        if not outcome.ok:
            return CheckResult(name="auth", state="failed", detail=outcome.detail)
        login = outcome.body.get("login") if isinstance(outcome.body, dict) else None
        if outcome.status == 200 and login:
            return CheckResult(
                name="auth",
                state="verified",
                detail=f"authenticated as {login} in {outcome.latency_ms}ms",
                latency_ms=outcome.latency_ms,
            )
        return CheckResult(
            name="auth",
            state="failed",
            detail=sanitize(
                error_detail(outcome.status, outcome.body, "GitHub rejected the token"),
                self._secrets(),
            ),
            latency_ms=outcome.latency_ms,
        )

    def _smoke_repo(self, client: httpx.Client, headers: dict[str, str]) -> CheckResult:
        outcome = bounded_request(
            client,
            "GET",
            f"{self._base_url}/repos/{self._repo}",
            headers=headers,
            secrets=self._secrets(),
            timeout_s=config.PROVIDER_TIMEOUT_S,
            max_retries=config.PROVIDER_MAX_RETRIES,
        )
        if not outcome.ok:
            return CheckResult(name="repo", state="failed", detail=outcome.detail)
        body: Any = outcome.body if isinstance(outcome.body, dict) else {}
        if outcome.status == 200:
            return CheckResult(
                name="repo",
                state="verified",
                detail=(
                    f"read access to {body.get('full_name', self._repo)} "
                    f"(default branch {body.get('default_branch', '?')}) "
                    f"in {outcome.latency_ms}ms"
                ),
                latency_ms=outcome.latency_ms,
            )
        if outcome.status == 404:
            hint = "repository not found, private without access, or name misspelled"
        else:
            hint = "repository read failed"
        return CheckResult(
            name="repo",
            state="failed",
            detail=sanitize(
                error_detail(outcome.status, outcome.body, hint), self._secrets()
            ),
            latency_ms=outcome.latency_ms,
        )
