"""Investigation orchestration: gate, evidence, Spark, SAM, map, Wonder.

One investigation runs its stages sequentially in a background worker,
persisting each stage as it completes so refresh always shows real
progress. Model calls use the incident's own content only; any failure
(blocked gate, missing key, provider error, bad geometry, cancellation)
is recorded honestly on the investigation — never papered over, never
retried into a fabricated result.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import httpx

from .. import config
from .. import store as incident_store
from ..providers.base import sanitize
from ..providers.meta import MetaAdapter, MetaInferenceError, SamParseError
from ..repro import store as repro_store
from ..repro.github_source import GitHubSourceError
from . import jobs as inv_jobs
from . import store as inv_store
from .evidence import (
    EvidencePackage,
    InvestigationBlocked,
    build_package,
    optional_artifact_bytes,
)
from .inference import run_sam, run_spark, run_wonder
from .mapping import PinnedFile, load_pinned_sources, map_identity
from .models import Investigation


class Cancelled(Exception):
    """The investigation was cancelled by the user."""


def _check(cancel_event: threading.Event, deadline: float) -> None:
    if cancel_event.is_set():
        raise Cancelled()
    if time.monotonic() > deadline:
        raise InvestigationBlocked(
            f"investigation exceeded the {config.INVESTIGATE_JOB_TIMEOUT_S:g}s job bound"
        )


def _missing_target(incident: Any) -> list[str]:
    missing = []
    if not incident.repo:
        missing.append("missing repository: set the owner/name to investigate")
    if not incident.ref:
        missing.append("missing ref: set the branch or ref to investigate")
    if not incident.commit_sha:
        missing.append("missing commit_sha: pin the target commit")
    return missing


def _identities(package: EvidencePackage) -> list[tuple[str, str, list[str]]]:
    """(target label, identifier, evidence refs) from route annotations."""
    found: list[tuple[str, str, list[str]]] = []
    seen: set[str] = set()
    routes = package.result.get("routes", {})
    for label in ("broken", "control"):
        route = routes.get(label, {})
        if not isinstance(route, dict):
            continue
        refs = [i.ref for i in package.items if i.route == label and i.kind == "annotation"]
        for key in ("button", "occlusion"):
            node = route.get(key)
            if not isinstance(node, dict):
                continue
            identifier = node.get("buglens") or node.get("testid") or node.get("id")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            tag = node.get("tag") or "element"
            found.append((f"{tag}#{identifier}", str(identifier), refs))
    return found


def _finish(inv_id: str, state: str, detail: str) -> None:
    def mutate(inv: Investigation) -> None:
        inv.state = state  # type: ignore[assignment]
        inv.detail = detail

    inv_store.update(inv_id, mutate)
    inv_store.add_event(inv_id, "lifecycle", f"finished: {state}")


def run_investigation(
    inv_id: str,
    cancel_event: threading.Event,
    meta_transport: Optional[httpx.BaseTransport] = None,
    github_transport: Optional[httpx.BaseTransport] = None,
) -> None:
    """Execute one investigation (worker entry point)."""
    inv = inv_store.get(inv_id)
    if inv is None:  # pragma: no cover - created before submit
        return
    deadline = time.monotonic() + config.INVESTIGATE_JOB_TIMEOUT_S

    def event(phase: str, message: str) -> None:
        inv_store.add_event(inv_id, phase, message)

    def save(**fields: Any) -> None:
        def mutate(candidate: Investigation) -> None:
            for key, value in fields.items():
                setattr(candidate, key, value)

        inv_store.update(inv_id, mutate)

    try:
        incident = incident_store.get_incident(inv.incident_id)
        run = repro_store.get_run(inv.run_id)
        if incident is None or run is None:
            raise InvestigationBlocked("incident or run record is gone; cannot investigate")
        missing = _missing_target(incident)
        if missing:
            raise InvestigationBlocked(
                "investigation needs the incident target first: " + "; ".join(missing)
            )
        assert incident.repo is not None and incident.commit_sha is not None
        save(state="running")
        event("lifecycle", f"worker started on run {run.id[:12]} at {run.commit_sha[:7]}")
        _check(cancel_event, deadline)

        package = build_package(incident, run)
        save(evidence=package.items, absent_evidence=package.absent)
        event("evidence", f"package built: {len(package.items)} items, "
                          f"{len(package.absent)} absence note(s)")
        _check(cancel_event, deadline)

        token = config.get_github_token()
        if not token:
            raise InvestigationBlocked("Set GITHUB_TOKEN in .env to read the pinned sources.")
        try:
            files, tree_paths = load_pinned_sources(
                incident.repo, incident.commit_sha, token, transport=github_transport
            )
        except GitHubSourceError as exc:
            raise InvestigationBlocked(f"pinned source unreadable: {exc}")
        event("sources", f"loaded {len(files)} pinned source file(s) for mapping")
        mappings = [
            map_identity(target, identifier, files, refs)
            for target, identifier, refs in _identities(package)
        ]
        save(mappings=mappings)
        event("mapping", f"{len(mappings)} source mapping(s): "
                         + (", ".join(f"{m.target}={m.certainty}" for m in mappings) or "none"))
        _check(cancel_event, deadline)

        adapter = MetaAdapter(
            api_key=config.get_meta_api_key(),
            base_url=config.get_meta_base_url(),
            transport=meta_transport,
        )
        if not config.get_meta_api_key():
            raise InvestigationBlocked("Set META_API_KEY in .env to run model calls.")

        spark = run_spark(
            adapter, incident, package, optional_artifact_bytes(run, "control-after.png")
        )
        save(spark=spark)
        event("spark", f"{spark.call.model}: {len(spark.observations)} observation(s), "
                       f"{len(spark.hypotheses)} causal guesses in {spark.call.latency_ms}ms"
                       + ("" if spark.parsed else f" (unparsed: {spark.parse_note})"))
        _check(cancel_event, deadline)

        sam = run_sam(adapter, package)
        save(sam=sam)
        event("sam", f"{sam.call.model}: {len(sam.matches)} match(es) in "
                     f"{sam.call.latency_ms}ms ({sam.note})")
        _check(cancel_event, deadline)

        wonder = run_wonder(adapter, incident, package, mappings, files, tree_paths)
        save(wonder=wonder)
        event("wonder", f"{wonder.call.model}: {len(wonder.hypotheses)} "
                        f"root-cause hypotheses in {wonder.call.latency_ms}ms"
                        + ("" if wonder.parsed else f" (unparsed: {wonder.parse_note})"))
        _finish(inv_id, "done", f"{len(wonder.hypotheses)} root-cause hypotheses recorded")
    except Cancelled:
        _finish(inv_id, "cancelled", "cancelled by user")
    except InvestigationBlocked as exc:
        _finish(inv_id, "blocked", str(exc))
    except (MetaInferenceError, SamParseError) as exc:
        _finish(inv_id, "blocked", f"model call failed honestly: {exc}")
    except Exception as exc:  # unexpected crash, never raw internals
        _finish(inv_id, "error", sanitize(f"investigation crashed: {exc}", []))
