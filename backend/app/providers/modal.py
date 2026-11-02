"""Modal adapter: start and terminate a minimal sandbox.

SDK usage verified against the official docs on 2026-11-03
(modal.com/docs/sdk/py/latest, modal.com/docs/guide/sandbox):

- ``modal.App.lookup("name", create_if_missing=True)`` builds the
  initialized app object sandboxes attach to (a bare ``App("name")`` is
  rejected as uninitialized by the installed SDK).
- ``modal.Sandbox.create(app=app, timeout=...)`` (``timeout`` = maximum
  sandbox lifetime in seconds).
- ``sandbox.exec(*args, timeout=...)`` takes variadic command strings and
  returns a ``ContainerProcess``; ``proc.wait()`` returns the exit code and
  ``proc.stdout.read()`` drains piped stdout.
- ``sandbox.terminate()`` stops the sandbox.

Authentication accepts either explicit environment credentials
(``MODAL_TOKEN_ID``/``MODAL_TOKEN_SECRET``) or the SDK's authenticated
local profile created by ``modal setup`` (``~/.modal.toml``); only the
presence of the profile file is inspected, never its contents. Per
AGENTS.md rule 5, no provider credentials are passed into the sandbox:
the smoke command is a fixed ``echo`` with no environment, files, or
repository code.
"""

from __future__ import annotations

import concurrent.futures
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import config
from .base import CheckResult, ProviderReport, sanitize

SETUP_DETAIL = (
    "Modal authentication missing: run `modal setup` (or `python -m modal setup` "
    "in the project venv) to create the SDK's local profile, or set "
    "MODAL_TOKEN_ID and MODAL_TOKEN_SECRET in .env from tokens created in the "
    "Modal dashboard; also `pip install modal` if the package is absent."
)

_SMOKE_APP = "buglens-smoke"
_SMOKE_COMMAND = ("echo", "buglens-smoke")
_SMOKE_EXEC_TIMEOUT_S = 60


def _local_profile_present() -> bool:
    """True when the SDK's local profile file exists and is non-empty.

    Presence only: the file holds secrets and its contents are never read.
    """
    try:
        return Path.home().joinpath(".modal.toml").stat().st_size > 0
    except OSError:
        return False


class ModalAdapter:
    name = "modal"

    def __init__(self, token_id: str = "", token_secret: str = "") -> None:
        self._token_id = token_id
        self._token_secret = token_secret

    def _auth_source(self) -> str:
        if self._token_id and self._token_secret:
            return "environment credentials"
        if _local_profile_present():
            return "local SDK profile (~/.modal.toml)"
        return ""

    def _secrets(self) -> list[str]:
        return [v for v in (self._token_id, self._token_secret) if v]

    def status(self) -> ProviderReport:
        source = self._auth_source()
        if not source:
            return ProviderReport(
                name=self.name,
                state="not_configured",
                detail=SETUP_DETAIL,
                checks=[
                    CheckResult(name="sandbox", state="not_configured", detail=SETUP_DETAIL)
                ],
            )
        return ProviderReport(
            name=self.name,
            state="configured_untested",
            detail=f"Modal authentication present ({source}); no smoke test has run.",
            checks=[CheckResult(name="sandbox", state="configured_untested")],
        )

    def smoke(self) -> ProviderReport:
        source = self._auth_source()
        if not source:
            return self.status()
        try:
            import modal  # lazy: the package is optional until Modal is used
        except ImportError:
            return self._report(
                "failed", "modal package not installed; run `pip install modal` first."
            )
        bound = config.MODAL_SMOKE_TIMEOUT_S
        started = time.perf_counter()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(self._run_sandbox, modal)
            try:
                detail = future.result(timeout=bound)
            except concurrent.futures.TimeoutError:
                return self._report(
                    "failed",
                    f"sandbox smoke exceeded the {bound:g}s bound "
                    f"(the sandbox self-terminates via its {120}s lifetime)",
                    started,
                )
            except Exception as exc:
                return self._report(
                    "failed",
                    sanitize(f"Modal error: {exc}", self._secrets()),
                    started,
                )
        finally:
            # Never wait for a stuck worker: smoke() itself must return
            # within the bound. The sandbox's server-side lifetime still
            # applies if creation succeeded before the stall.
            pool.shutdown(wait=False, cancel_futures=True)
        return self._report("verified", f"{detail} (via {source})", started)

    def _run_sandbox(self, modal: Any) -> str:
        """Create, echo, verify completion, terminate. Raises on any failure,
        including cleanup failure: a sandbox that cannot be terminated must
        never report verified."""
        app = modal.App.lookup(_SMOKE_APP, create_if_missing=True)
        sandbox = modal.Sandbox.create(app=app, timeout=120)
        exec_error: Optional[Exception] = None
        try:
            proc = sandbox.exec(*_SMOKE_COMMAND, timeout=_SMOKE_EXEC_TIMEOUT_S)
            exit_code = proc.wait()
            stdout = proc.stdout.read()
            if exit_code != 0 or "buglens-smoke" not in str(stdout):
                raise RuntimeError(f"sandbox echo failed (exit code {exit_code})")
        except Exception as exc:
            exec_error = exc
        try:
            sandbox.terminate()
        except Exception as exc:
            raise RuntimeError(f"sandbox cleanup (terminate) failed: {exc}") from exec_error
        if exec_error is not None:
            raise exec_error
        return "sandbox created, echo exit 0 with marker, terminated"

    def _report(self, state: str, detail: str, started: Optional[float] = None) -> ProviderReport:
        latency_ms = None if started is None else int((time.perf_counter() - started) * 1000)
        return ProviderReport(
            name=self.name,
            state=state,  # type: ignore[arg-type]
            detail=detail,
            checks=[CheckResult(name="sandbox", state=state, detail=detail, latency_ms=latency_ms)],  # type: ignore[arg-type]
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
