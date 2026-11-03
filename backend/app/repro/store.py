"""Reproduction run persistence (one directory per run).

Layout under the data dir: ``repro_runs/<run_id>/run.json`` plus
``repro_runs/<run_id>/artifacts/<stored>``. Per-run files keep concurrent
runs (and incident writes) from contending; each record write is atomic
(temp file + ``os.replace``) under an in-process lock. Same single-process
caveats as the incident store apply.
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

from .. import config
from .models import ReproArtifact, ReproEvent, ReproRun

_lock = threading.Lock()


def _runs_dir() -> Path:
    return config.get_data_dir() / "repro_runs"


def _run_dir(run_id: str) -> Path:
    return _runs_dir() / run_id


def _record_path(run_id: str) -> Path:
    return _run_dir(run_id) / "run.json"


def _artifacts_dir(run_id: str) -> Path:
    return _run_dir(run_id) / "artifacts"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_record(run: ReproRun) -> None:
    directory = _run_dir(run.id)
    directory.mkdir(parents=True, exist_ok=True)
    target = _record_path(run.id)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix="run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(run.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def create_run(run: ReproRun) -> ReproRun:
    with _lock:
        if _record_path(run.id).exists():
            raise KeyError(run.id)
        _write_record(run)
    return run


def _read_record(run_id: str) -> Optional[ReproRun]:
    path = _record_path(run_id)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as handle:
        return ReproRun(**json.load(handle))


def get_run(run_id: str) -> Optional[ReproRun]:
    # Locked: on Windows an unlocked read racing os.replace fails with
    # PermissionError, and status polls run concurrently with worker writes.
    with _lock:
        return _read_record(run_id)


def list_runs_for_incident(incident_id: str) -> list[ReproRun]:
    runs: list[ReproRun] = []
    base = _runs_dir()
    if not base.is_dir():
        return runs
    with _lock:
        for child in base.iterdir():
            record = child / "run.json"
            if not record.is_file():
                continue
            try:
                with open(record, encoding="utf-8") as handle:
                    run = ReproRun(**json.load(handle))
            except (ValueError, OSError):
                continue
            if run.incident_id == incident_id:
                runs.append(run)
    return sorted(runs, key=lambda r: r.created_at, reverse=True)


def update_run(run_id: str, mutate: Callable[[ReproRun], None]) -> Optional[ReproRun]:
    """Atomically mutate one run record (mutation runs under the lock)."""
    with _lock:
        run = _read_record(run_id)
        if run is None:
            return None
        mutate(run)
        run.updated_at = utcnow()
        _write_record(run)
    return run


def add_event(run_id: str, phase: str, message: str) -> None:
    def mutate(run: ReproRun) -> None:
        run.events.append(ReproEvent(at=utcnow(), phase=phase, message=message))

    update_run(run_id, mutate)


def add_artifact(
    run_id: str,
    name: str,
    content_type: str,
    data: bytes,
    stored_filename: str,
) -> ReproArtifact:
    """Persist artifact bytes and record them on the run."""
    artifacts = _artifacts_dir(run_id)
    artifacts.mkdir(parents=True, exist_ok=True)
    target = safe_run_artifact_path(run_id, stored_filename)
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
    artifact = ReproArtifact(
        name=name,
        stored_filename=stored_filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )

    def mutate(run: ReproRun) -> None:
        run.artifacts = [a for a in run.artifacts if a.name != name] + [artifact]

    if update_run(run_id, mutate) is None:  # pragma: no cover - run exists by now
        raise KeyError(run_id)
    return artifact


def save_contract(run_id: str, payload: dict) -> None:
    """Persist the frozen contract beside the run record (internal file)."""
    directory = _run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "contract.json"
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix="contract-", suffix=".tmp")
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


def load_contract(run_id: str) -> Optional[dict]:
    path = _run_dir(run_id) / "contract.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def safe_run_artifact_path(run_id: str, stored_filename: str) -> Path:
    """Resolve a storage filename, guaranteeing it stays in the run dir."""
    base = _artifacts_dir(run_id).resolve()
    candidate = (base / stored_filename).resolve()
    if candidate.parent != base or candidate.name != Path(stored_filename).name:
        raise ValueError(f"refusing unsafe artifact path: {stored_filename!r}")
    return candidate
