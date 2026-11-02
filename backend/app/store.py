"""File-backed incident store.

STORAGE CONCURRENCY LIMITS (M1, local development only):

- Run the backend with a single worker (``--workers 1``, the uvicorn
  default). Every operation reads ``incidents.json`` fresh from disk and
  writes go through an in-process ``threading.Lock`` with atomic replace
  (temp file + ``os.replace``), so concurrent requests in ONE process are
  safe and readers never see a torn file.
- Multiple worker processes are NOT safe: two writers in different
  processes can interleave read-modify-write cycles and the last writer
  wins, silently dropping the other's record update. Artifact BYTES are
  immune (UUID filenames, one file per upload, never overwritten), but the
  JSON record could lose an evidence entry.
- No quotas are enforced across restarts beyond what ``incidents.json``
  records; per-incident byte/count caps are checked on upload only.
- M5 (Modal deployment) must replace this module with a transactional
  store. The route layer only depends on the functions below.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config
from .models import Evidence, Incident

_lock = threading.Lock()


def _records_path() -> Path:
    return config.get_data_dir() / "incidents.json"


def _artifacts_dir() -> Path:
    return config.get_data_dir() / "artifacts"


def _read_all() -> dict[str, Incident]:
    path = _records_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    incidents: dict[str, Incident] = {}
    for incident_id, payload in raw.get("incidents", {}).items():
        incidents[incident_id] = Incident(**payload)
    return incidents


def _write_all(incidents: dict[str, Incident]) -> None:
    data_dir = config.get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "incidents": {
            incident_id: incident.model_dump(mode="json")
            for incident_id, incident in incidents.items()
        }
    }
    target = _records_path()
    fd, tmp_name = tempfile.mkstemp(
        dir=str(data_dir), prefix="incidents-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_incident(incident: Incident) -> Incident:
    with _lock:
        incidents = _read_all()
        incidents[incident.id] = incident
        _write_all(incidents)
    return incident


def get_incident(incident_id: str) -> Optional[Incident]:
    return _read_all().get(incident_id)


def list_incidents() -> list[Incident]:
    return sorted(_read_all().values(), key=lambda i: i.created_at, reverse=True)


def update_commit_sha(incident_id: str, commit_sha: str) -> Optional[Incident]:
    with _lock:
        incidents = _read_all()
        incident = incidents.get(incident_id)
        if incident is None:
            return None
        incident.commit_sha = commit_sha
        incident.updated_at = _utcnow()
        incidents[incident_id] = incident
        _write_all(incidents)
    return incident


def add_evidence(incident_id: str, evidence: Evidence, staged_path: Path) -> Evidence:
    """Atomically move a staged upload into artifacts/ and record it."""
    artifacts = _artifacts_dir()
    artifacts.mkdir(parents=True, exist_ok=True)
    final_path = safe_artifact_path(evidence.stored_filename)
    os.replace(staged_path, final_path)
    try:
        with _lock:
            incidents = _read_all()
            incident = incidents.get(incident_id)
            if incident is None:  # pragma: no cover - checked before upload
                raise KeyError(incident_id)
            incident.evidence.append(evidence)
            incident.updated_at = _utcnow()
            incidents[incident_id] = incident
            _write_all(incidents)
    except BaseException:
        try:
            os.unlink(final_path)
        except OSError:
            pass
        raise
    return evidence


def safe_artifact_path(stored_filename: str) -> Path:
    """Resolve a storage filename, guaranteeing it stays inside artifacts/.

    Storage names are server-generated, so this is defense in depth; it
    raises ``ValueError`` on anything that does not resolve inside the
    artifacts directory (path traversal attempts).
    """
    artifacts = _artifacts_dir().resolve()
    candidate = (artifacts / stored_filename).resolve()
    if candidate.parent != artifacts or candidate.name != Path(stored_filename).name:
        raise ValueError(f"refusing unsafe artifact path: {stored_filename!r}")
    return candidate


def utcnow() -> datetime:
    return _utcnow()
