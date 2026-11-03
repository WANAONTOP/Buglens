"""Investigation persistence (one directory per investigation).

Layout under the data dir: ``investigations/<inv_id>/record.json``.
Each record write is atomic (temp file + ``os.replace``) under an
in-process lock, and reads take the same lock: on Windows an unlocked
read racing ``os.replace`` fails with PermissionError (seen in Step 5).
Same single-process caveats as the other stores apply.
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
from .models import Investigation, InvestigationEvent

_lock = threading.Lock()


def _base_dir() -> Path:
    return config.get_data_dir() / "investigations"


def _record_path(inv_id: str) -> Path:
    return _base_dir() / inv_id / "record.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_record(inv: Investigation) -> None:
    directory = _record_path(inv.id).parent
    directory.mkdir(parents=True, exist_ok=True)
    target = _record_path(inv.id)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix="inv-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(inv.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_record(inv_id: str) -> Optional[Investigation]:
    path = _record_path(inv_id)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as handle:
        return Investigation(**json.load(handle))


def create(inv: Investigation) -> Investigation:
    with _lock:
        if _record_path(inv.id).exists():
            raise KeyError(inv.id)
        _write_record(inv)
    return inv


def get(inv_id: str) -> Optional[Investigation]:
    with _lock:
        return _read_record(inv_id)


def list_for_incident(incident_id: str) -> list[Investigation]:
    found: list[Investigation] = []
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
                    inv = Investigation(**json.load(handle))
            except (ValueError, OSError):
                continue
            if inv.incident_id == incident_id:
                found.append(inv)
    return sorted(found, key=lambda i: i.created_at, reverse=True)


def update(inv_id: str, mutate: Callable[[Investigation], None]) -> Optional[Investigation]:
    """Atomically mutate one record (mutation runs under the lock)."""
    with _lock:
        inv = _read_record(inv_id)
        if inv is None:
            return None
        mutate(inv)
        inv.updated_at = utcnow()
        _write_record(inv)
    return inv


def add_event(inv_id: str, phase: str, message: str) -> None:
    def mutate(inv: Investigation) -> None:
        inv.events.append(InvestigationEvent(at=utcnow(), phase=phase, message=message))

    update(inv_id, mutate)
