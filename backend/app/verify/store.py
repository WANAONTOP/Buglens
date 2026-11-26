"""Verification persistence (one directory per verification + artifacts).

Layout under the data dir: ``verifications/<id>/record.json`` plus
``verifications/<id>/artifacts/<stored>``. Record writes are atomic
(temp file + ``os.replace``) under an in-process lock, and reads take
the same lock (Windows ``os.replace`` races). Same single-process
caveats as the other stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import config, deploy
from .models import Verification, VerificationEvent

_lock = threading.Lock()

TERMINAL_STATES = frozenset({"done", "blocked", "error"})


def _base_dir() -> Path:
    return config.get_data_dir() / "verifications"


def _record_path(verification_id: str) -> Path:
    return _base_dir() / verification_id / "record.json"


def _artifacts_dir(verification_id: str) -> Path:
    return _base_dir() / verification_id / "artifacts"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_record(verification: Verification) -> None:
    directory = _record_path(verification.id).parent
    directory.mkdir(parents=True, exist_ok=True)
    target = _record_path(verification.id)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix="ver-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(verification.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    deploy.sync_after_write()


def _read_record(verification_id: str) -> Optional[Verification]:
    path = _record_path(verification_id)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as handle:
        return Verification(**json.load(handle))


def create(verification: Verification) -> Verification:
    with _lock:
        if _record_path(verification.id).exists():
            raise KeyError(verification.id)
        _write_record(verification)
    return verification


def get(verification_id: str) -> Optional[Verification]:
    with _lock:
        return _read_record(verification_id)


def list_for_candidate(candidate_id: str) -> list[Verification]:
    found: list[Verification] = []
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
                    verification = Verification(**json.load(handle))
            except (ValueError, OSError):
                continue
            if verification.candidate_id == candidate_id:
                found.append(verification)
    return sorted(found, key=lambda v: v.created_at, reverse=True)


def update(
    verification_id: str,
    mutate: Callable[[Verification], None],
    expect_states: Optional[tuple[str, ...]] = None,
) -> Optional[Verification]:
    """Mutate one record; None when missing or the guard refuses.

    ``expect_states`` refuses the write unless the record is in one of
    those states (handoff preconditions, both modes). Deploy mode
    additionally reloads first and refuses every write to a terminal
    record, so a stale container aborts instead of resurrecting or
    overwriting newer state.
    """
    with _lock:
        if deploy.is_modal():
            deploy.sync_before_read()
        verification = _read_record(verification_id)
        if verification is None:
            return None
        if expect_states is not None and verification.state not in expect_states:
            return None
        if deploy.is_modal() and verification.state in TERMINAL_STATES:
            return None
        mutate(verification)
        verification.updated_at = utcnow()
        _write_record(verification)
    return verification


def add_event(verification_id: str, phase: str, message: str) -> None:
    def mutate(verification: Verification) -> None:
        verification.events.append(
            VerificationEvent(at=utcnow(), phase=phase, message=message)
        )

    update(verification_id, mutate)


def save_artifact(verification_id: str, name: str, data: bytes) -> str:
    """Persist artifact bytes under a server-generated name; returns it."""
    artifacts = _artifacts_dir(verification_id)
    artifacts.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:12]
    stored = f"{digest}-{name}".replace("/", "_")
    target = safe_artifact_path(verification_id, stored)
    fd, tmp_name = tempfile.mkstemp(dir=str(artifacts), prefix="art-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return stored


def record_artifact(verification_id: str, name: str, content_type: str, data: bytes) -> None:
    """Persist artifact bytes and append their record (skips duplicates)."""
    from .models import VerifyArtifact

    stored = save_artifact(verification_id, name, data)

    def mutate(verification: Verification) -> None:
        if any(a.name == name for a in verification.artifacts):
            return
        verification.artifacts.append(
            VerifyArtifact(
                name=name, stored_filename=stored, content_type=content_type,
                size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
            )
        )

    update(verification_id, mutate)


def safe_artifact_path(verification_id: str, stored_filename: str) -> Path:
    """Resolve a storage filename, guaranteeing it stays in the run dir."""
    base = _artifacts_dir(verification_id).resolve()
    candidate = (base / stored_filename).resolve()
    if candidate.parent != base or candidate.name != Path(stored_filename).name:
        raise ValueError(f"refusing unsafe artifact path: {stored_filename!r}")
    return candidate
