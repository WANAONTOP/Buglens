"""Provider readiness reporting and bounded smoke tests (all authenticated)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from . import providers
from .auth import require_auth
from .providers.base import ProviderReport

router = APIRouter(prefix="/api/providers", dependencies=[Depends(require_auth)])


@router.get("/status", response_model=list[ProviderReport])
def provider_status() -> list[ProviderReport]:
    """Readiness for every provider. Performs no network traffic itself."""
    reports: list[ProviderReport] = []
    for name in providers.PROVIDER_NAMES:
        adapter = providers.get_adapter(name)
        assert adapter is not None  # names are exhaustive by construction
        fresh = adapter.status()
        if fresh.state == "not_configured":
            reports.append(fresh)
            continue
        recorded = providers.registry.get(name)
        reports.append(recorded if recorded is not None else fresh)
    return reports


@router.post("/{name}/smoke", response_model=ProviderReport)
def provider_smoke(name: str) -> ProviderReport:
    """Run the provider's bounded smoke test and record a verified/failed outcome."""
    if name not in providers.PROVIDER_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown provider {name!r}.")
    adapter = providers.get_adapter(name)
    assert adapter is not None
    report = adapter.smoke()
    providers.registry.record(report)
    return report
