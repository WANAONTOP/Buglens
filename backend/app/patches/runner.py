"""Candidate orchestration: prereqs, context, Wonder, validate, propose.

One candidate runs its stages sequentially in a background worker. The
diff is validated structurally against the exact pinned baseline before
anything can be approved; malformed replies, out-of-scope diffs, and
provider failures all land in BLOCKED with reasons — never in a
fabricated or coerced proposal. Approval (a separate developer action)
authorizes a later verification run; it is not verification.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Optional

import httpx

from .. import config
from ..providers.meta import MetaAdapter, MetaInferenceError
from . import store as patch_store
from .context import CandidateBlocked, collect_context, resolve_prereqs
from .generate import run_wonder_candidate
from .models import Candidate
from .validate import validate_diff


class Cancelled(Exception):
    """Candidate generation was cancelled by the user."""


def _check(cancel_event: threading.Event, deadline: float) -> None:
    if cancel_event.is_set():
        raise Cancelled()
    if time.monotonic() > deadline:
        raise CandidateBlocked(
            f"generation exceeded the {config.PATCH_JOB_TIMEOUT_S:g}s job bound"
        )


def _finish(candidate_id: str, state: str, detail: str) -> None:
    def mutate(candidate: Candidate) -> None:
        candidate.state = state  # type: ignore[assignment]
        candidate.detail = detail

    patch_store.update(candidate_id, mutate)
    patch_store.add_event(candidate_id, "lifecycle", f"finished: {state}")


def approval_intact(candidate: Candidate) -> bool:
    """True when the stored diff still matches its recorded hash binding."""
    if candidate.approval is None:
        return False
    current = hashlib.sha256(candidate.diff.encode("utf-8")).hexdigest()
    return current == candidate.diff_sha256 == candidate.approval.diff_sha256


def run_candidate(
    candidate_id: str,
    cancel_event: threading.Event,
    meta_transport: Optional[httpx.BaseTransport] = None,
    github_transport: Optional[httpx.BaseTransport] = None,
) -> None:
    """Generate and validate one candidate (worker entry point)."""
    candidate = patch_store.get(candidate_id)
    if candidate is None:  # pragma: no cover - created before submit
        return
    deadline = time.monotonic() + config.PATCH_JOB_TIMEOUT_S

    def event(phase: str, message: str) -> None:
        patch_store.add_event(candidate_id, phase, message)

    def save(**fields: Any) -> None:
        def mutate(item: Candidate) -> None:
            for key, value in fields.items():
                setattr(item, key, value)

        patch_store.update(candidate_id, mutate)

    try:
        save(state="generating")
        event("lifecycle", "worker started")
        try:
            prereqs = resolve_prereqs(
                candidate.incident_id, candidate.run_id,
                candidate.investigation_id, candidate.hypothesis_rank,
            )
        except CandidateBlocked as exc:
            raise exc
        _check(cancel_event, deadline)
        token = config.get_github_token()
        if not token:
            raise CandidateBlocked("Set GITHUB_TOKEN in .env to read the pinned sources.")
        context = collect_context(prereqs, token, transport=github_transport)
        event("context", f"{len(context.files)} pinned file(s), "
                         f"{len(context.combined_excerpt)} chars"
                         + "".join(f"; {note}" for note in context.notes))
        _check(cancel_event, deadline)
        if not config.get_meta_api_key():
            raise CandidateBlocked("Set META_API_KEY in .env to run model calls.")
        adapter = MetaAdapter(
            api_key=config.get_meta_api_key(),
            base_url=config.get_meta_base_url(),
            transport=meta_transport,
        )
        hypothesis = next(
            h for h in prereqs.investigation.wonder.hypotheses  # type: ignore[union-attr]
            if h.rank == candidate.hypothesis_rank
        )
        parsed = run_wonder_candidate(adapter, prereqs, context, hypothesis)
        save(provider=parsed.call, raw_text=parsed.raw_text,
             parsed=parsed.parsed, parse_note=parsed.parse_note)
        if not parsed.parsed:
            raise CandidateBlocked(f"model reply unusable: {parsed.parse_note}")
        event("wonder", f"{parsed.call.model} returned a candidate "
                        f"({len(parsed.diff)} chars) in {parsed.call.latency_ms}ms")
        digest = hashlib.sha256(parsed.diff.encode("utf-8")).hexdigest()
        validation, changed = validate_diff(
            parsed.diff,
            permitted=context.permitted,
            protected=context.protected,
            pinned=context.files,
            entry_types=context.entry_types,
        )
        save(diff=parsed.diff, diff_sha256=digest, changed_paths=changed,
             explanations=parsed.explanations, evidence_refs=parsed.evidence_refs,
             expected_effect=parsed.expected_effect,
             regression_risks=parsed.regression_risks, validation=validation)
        if not validation.ok:
            raise CandidateBlocked(
                "candidate failed validation: " + "; ".join(validation.reasons)
            )
        event("validate", f"diff valid: {', '.join(changed)}")
        _finish(candidate_id, "proposed", f"proposed: {', '.join(changed)}")
    except Cancelled:
        _finish(candidate_id, "blocked", "cancelled by user")
    except CandidateBlocked as exc:
        _finish(candidate_id, "blocked", str(exc))
    except MetaInferenceError as exc:
        _finish(candidate_id, "blocked", f"model call failed honestly: {exc}")
    except Exception as exc:  # unexpected crash, never raw internals
        from ..providers.base import sanitize

        _finish(candidate_id, "error", sanitize(f"generation crashed: {exc}", []))
