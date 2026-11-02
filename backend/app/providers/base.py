"""Shared provider plumbing: readiness states, sanitized reports, bounded HTTP.

Readiness vocabulary (a present key alone is never proof of connection):

- ``not_configured``: required credentials/settings are missing.
- ``configured_untested``: credentials present, no successful smoke test yet.
- ``verified``: a bounded smoke test succeeded against the live provider.
- ``failed``: the last smoke test failed; ``detail`` says what was observed.

Every human-facing string passes through :func:`sanitize`, which redacts
known secret values and bearer tokens. Provider credentials are only ever
read from server-side environment variables.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

ProviderState = Literal["not_configured", "configured_untested", "verified", "failed"]

RETRIABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_DETAIL_CHARS = 2000


class CheckResult(BaseModel):
    name: str
    state: ProviderState
    detail: str = ""
    latency_ms: Optional[int] = None


class ProviderReport(BaseModel):
    name: str
    state: ProviderState
    detail: str = ""
    checks: list[CheckResult] = Field(default_factory=list)
    checked_at: Optional[str] = None  # ISO timestamp of the last smoke run


def sanitize(text: str, secrets: list[str]) -> str:
    """Redact secret values and bearer tokens from a human-facing string."""
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in out:
            out = out.replace(secret, "[redacted]")
    out = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[redacted]", out)
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_.~+/=-]{8,}", r"\1[redacted]", out)
    return out[:_MAX_DETAIL_CHARS]


@dataclass
class HttpOutcome:
    ok: bool  # a response arrived (any status)
    status: int  # 0 when no response arrived
    body: Any = None
    detail: str = ""
    latency_ms: int = 0
    attempts: int = 1


def bounded_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Any = None,
    secrets: Optional[list[str]] = None,
    timeout_s: float = 60.0,
    max_retries: int = 2,
) -> HttpOutcome:
    """One HTTP call with timeout, bounded retries, and sanitized errors.

    Retries transient failures (timeouts, connection errors, 429/5xx) with a
    short linear backoff. Other 4xx responses are returned, never retried.
    """
    secrets = secrets or []
    timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=10.0)
    attempts = 1 + max(0, max_retries)
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            response = client.request(
                method, url, headers=headers, json=json_body, timeout=timeout
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            detail = f"{type(exc).__name__}: no response within {timeout_s:g}s"
            if attempt < attempts:
                time.sleep(0.5 * attempt)
                continue
            return HttpOutcome(
                ok=False,
                status=0,
                detail=sanitize(detail, secrets),
                latency_ms=int((time.perf_counter() - started) * 1000),
                attempts=attempt,
            )
        except Exception as exc:  # defensive: never leak internals raw
            return HttpOutcome(
                ok=False,
                status=0,
                detail=sanitize(f"request failed: {exc}", secrets),
                latency_ms=int((time.perf_counter() - started) * 1000),
                attempts=attempt,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code in RETRIABLE_STATUSES and attempt < attempts:
            time.sleep(0.5 * attempt)
            continue
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        return HttpOutcome(
            ok=True, status=response.status_code, body=body, latency_ms=latency_ms,
            attempts=attempt,
        )
    raise AssertionError("unreachable")  # pragma: no cover


def summarize(name: str, checks: list[CheckResult], detail: str = "") -> ProviderReport:
    """Roll check states up to one provider state (worst wins)."""
    states = {check.state for check in checks}
    if "failed" in states:
        state: ProviderState = "failed"
    elif "configured_untested" in states or "not_configured" in states:
        state = "configured_untested" if "verified" in states else (
            "not_configured" if states == {"not_configured"} else "configured_untested"
        )
    else:
        state = "verified"
    if not detail:
        if state == "verified":
            detail = "all smoke checks passed"
        elif state == "not_configured":
            detail = checks[0].detail if checks else "not configured"
        else:
            detail = "see individual checks"
    return ProviderReport(name=name, state=state, detail=detail, checks=checks)


def error_detail(status: int, body: Any, fallback: str) -> str:
    """Best-effort one-line summary of a provider error payload."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"HTTP {status}: {error['message']}"
        if isinstance(error, str) and error:
            return f"HTTP {status}: {error}"
        if isinstance(body.get("message"), str) and body["message"]:
            return f"HTTP {status}: {body['message']}"
    return f"HTTP {status}: {fallback}"
