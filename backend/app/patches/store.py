"""Candidate persistence (one directory per candidate).

Layout under the data dir: ``candidates/<candidate_id>/record.json``.
Each record write is atomic (temp file + ``os.replace``) under an
in-process lock, and reads take the same lock (Windows
``os.replace`` races). Same single-process caveats as the other stores.

Records are append-only in practice: generation fills the diff and
validation once, and review transitions the state once. Nothing ever
rewrites the diff, baseline, or contract of an existing record — any
such change must be a new candidate, which is what keeps approvals
bound to exact bytes.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import config
from .models import Candidate, CandidateEvent

_lock = threading.Lock()


def _base_dir() -> Path:
    return config.get_data_dir() / "candidates"


def _record_path(candidate_id: str) -> Path:
    return _base_dir() / candidate_id / "record.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_record(candidate: Candidate) -> None:
    directory = _record_path(candidate.id).parent
    directory.mkdir(parents=True, exist_ok=True)
    target = _record_path(candidate.id)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix="cand-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(candidate.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_record(candidate_id: str) -> Optional[Candidate]:
    path = _record_path(candidate_id)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as handle:
        return Candidate(**json.load(handle))


def create(candidate: Candidate) -> Candidate:
    with _lock:
        if _record_path(candidate.id).exists():
            raise KeyError(candidate.id)
        _write_record(candidate)
    return candidate


def get(candidate_id: str) -> Optional[Candidate]:
    with _lock:
        return _read_record(candidate_id)


def list_for_incident(incident_id: str) -> list[Candidate]:
    found: list[Candidate] = []
    base = _base_dir()
    if not base.is_dir():
        return found
    with _lock:
        for child in base.iterdir():
            record = child / "record.json"
            if not record.is_file():
                continue
            try:
                with open(record, encoding="utf-8") as handle:
                    candidate = Candidate(**json.load(handle))
            except (ValueError, OSError):
                continue
            if candidate.incident_id == incident_id:
                found.append(candidate)
    return sorted(found, key=lambda c: c.created_at, reverse=True)


def update(candidate_id: str, mutate: Callable[[Candidate], None]) -> Optional[Candidate]:
    """Atomically mutate one record (mutation runs under the lock)."""
    with _lock:
        candidate = _read_record(candidate_id)
        if candidate is None:
            return None
        mutate(candidate)
        candidate.updated_at = utcnow()
        _write_record(candidate)
    return candidate


def add_event(candidate_id: str, phase: str, message: str) -> None:
    def mutate(candidate: Candidate) -> None:
        candidate.events.append(CandidateEvent(at=utcnow(), phase=phase, message=message))

    update(candidate_id, mutate)
