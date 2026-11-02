"""Baseten adapter (optional integration; docs: https://docs.baseten.co).

Baseten's Model APIs are OpenAI-compatible at
``https://inference.baseten.co/v1`` with ``Authorization: Bearer``. The
smoke test lists models (``GET /v1/models``), which verifies reachability
and key validity without running inference or spending anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from .. import config
from .base import (
    CheckResult,
    ProviderReport,
    bounded_request,
    error_detail,
    sanitize,
)


class BasetenAdapter:
    name = "baseten"
    optional = True

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url or config.BASETEN_API_BASE_URL
        self._transport = transport

    def _secrets(self) -> list[str]:
        return [self._api_key] if self._api_key else []

    def status(self) -> ProviderReport:
        if not self._api_key:
            return ProviderReport(
                name=self.name,
                state="not_configured",
                detail="BASETEN_API_KEY not set (optional; Baseten stays disabled).",
                checks=[
                    CheckResult(
                        name="listing",
                        state="not_configured",
                        detail="BASETEN_API_KEY missing (optional)",
                    )
                ],
            )
        return ProviderReport(
            name=self.name,
            state="configured_untested",
            detail="BASETEN_API_KEY is set; no smoke test has run in this process.",
            checks=[CheckResult(name="listing", state="configured_untested")],
        )

    def smoke(self) -> ProviderReport:
        if not self._api_key:
            return self.status()
        headers = {"Authorization": f"Bearer {self._api_key}"}
        with httpx.Client(transport=self._transport) as client:
            outcome = bounded_request(
                client,
                "GET",
                f"{self._base_url}/models",
                headers=headers,
                secrets=self._secrets(),
                timeout_s=config.PROVIDER_TIMEOUT_S,
                max_retries=config.PROVIDER_MAX_RETRIES,
            )
        if not outcome.ok:
            check = CheckResult(name="listing", state="failed", detail=outcome.detail)
        elif outcome.status == 200:
            count = outcome.body.get("data") if isinstance(outcome.body, dict) else None
            shown = len(count) if isinstance(count, list) else "?"
            check = CheckResult(
                name="listing",
                state="verified",
                detail=(
                    f"Baseten listing access verified in {outcome.latency_ms}ms "
                    f"({shown} models listed); inference capability untested — "
                    "Baseten inference remains optional"
                ),
                latency_ms=outcome.latency_ms,
            )
        else:
            check = CheckResult(
                name="listing",
                state="failed",
                detail=sanitize(
                    error_detail(outcome.status, outcome.body, "Baseten rejected the key"),
                    self._secrets(),
                ),
                latency_ms=outcome.latency_ms,
            )
        state = check.state
        return ProviderReport(
            name=self.name,
            state=state,
            detail=check.detail,
            checks=[check],
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
