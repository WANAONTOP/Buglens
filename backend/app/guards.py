"""Gates that investigation and patch execution must pass (M3+).

M1 has no execution to gate yet; this module exists so the workflow fix can
state the rule in code rather than prose: missing repository, ref, commit,
or permitted source paths prevent investigation and patch execution — never
evidence capture or draft storage. Execution routes added in later
milestones must call :func:`require_investigation_ready` before doing any
work. Repository details are never invented to satisfy the gate.
"""

from __future__ import annotations

from fastapi import HTTPException

from .models import Incident, investigation_blockers_for


def require_investigation_ready(incident: Incident) -> None:
    """Raise 409 listing what is missing, or return silently when ready."""
    missing = investigation_blockers_for(incident)
    if missing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Investigation blocked: record the missing prerequisites first.",
                "missing": missing,
            },
        )
