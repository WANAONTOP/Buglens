"""In-memory record of smoke-test outcomes.

Readiness otherwise derives from configuration on every call, so a restart
resets providers to ``not_configured``/``configured_untested`` until a smoke
test runs again. Only ``verified``/``failed`` outcomes are recorded.
"""

from __future__ import annotations

from .base import ProviderReport

_records: dict[str, ProviderReport] = {}


def record(report: ProviderReport) -> None:
    if report.state in ("verified", "failed"):
        _records[report.name] = report


def get(name: str) -> ProviderReport | None:
    return _records.get(name)


def clear() -> None:
    _records.clear()
