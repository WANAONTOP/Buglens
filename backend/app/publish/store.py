"""Publication persistence (one directory per publication).

Layout under the data dir: ``publications/<id>/record.json``. Record
writes are atomic (temp file + ``os.replace``) under an in-process
lock, and reads take the same lock (Windows ``os.replace`` races).
Same single-process caveats as the other stores.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import config, deploy
from .models import Publication, PublicationEvent

_lock = threading.Lock()


def _base_dir() -> Path:
    return config.get_data_dir() / "publications"


def _record_path(publication_id: str) -> Path:
    return _base_dir() / publication_id / "record.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_record(publication: Publication) -> None:
    directory = _record_path(publication.id).parent
    directory.mkdir(parents=True, exist_ok=True)
    target = _record_path(publication.id)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix="pub-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(publication.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    deploy.sync_after_write()


def _read_record(publication_id: str) -> Optional[Publication]:
    path = _record_path(publication_id)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as handle:
        return Publication(**json.load(handle))


def create(publication: Publication) -> Publication:
    with _lock:
        if _record_path(publication.id).exists():
            raise KeyError(publication.id)
        _write_record(publication)
    return publication


def get(publication_id: str) -> Optional[Publication]:
    with _lock:
        return _read_record(publication_id)


def list_for_candidate(candidate_id: str) -> list[Publication]:
    found: list[Publication] = []
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
                    publication = Publication(**json.load(handle))
            except (ValueError, OSError):
                continue
            if publication.candidate_id == candidate_id:
                found.append(publication)
    return sorted(found, key=lambda p: p.created_at, reverse=True)


def update(publication_id: str, mutate: Callable[[Publication], None]) -> Optional[Publication]:
    """Atomically mutate one record (mutation runs under the lock)."""
    with _lock:
        publication = _read_record(publication_id)
        if publication is None:
            return None
        mutate(publication)
        publication.updated_at = utcnow()
        _write_record(publication)
    return publication


def add_event(publication_id: str, phase: str, message: str) -> None:
    def mutate(publication: Publication) -> None:
        publication.events.append(
            PublicationEvent(at=utcnow(), phase=phase, message=message)
        )

    update(publication_id, mutate)
