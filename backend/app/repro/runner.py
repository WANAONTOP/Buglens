"""Modal sandbox runner for frozen reproduction contracts.

Pipeline (each phase emits run events; cancellation is honored between
phases and inside poll loops):

1. Ephemeral volume: upload the BugLens driver + frozen contract JSON
   (server-side SDK calls; credentials never enter the sandbox).
2. Sandbox (Debian Node image, bounded CPU/memory/time): fetch the public
   repository tarball at the exact pinned SHA, verify the extracted
   manifest hash against the frozen value.
3. Run the frozen manifest install, install Playwright Chromium + system
   deps, start the fixture server, wait for readiness with a timeout.
4. Run the driver (both routes, evidence to ``/tmp/out``), fetch artifacts
   back over bounded base64 transfers, kill the server, terminate.
5. Classify server-side: REPRODUCED / NEEDS_REPRODUCTION / BLOCKED.

Only frozen manifest ``install``/``start`` strings ever execute, and only
inside the sandbox. No status here claims a bug is verified or fixed.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .. import config
from ..providers.base import sanitize
from ..providers.modal import ModalAdapter
from . import store as repro_store
from .contract import FrozenContract

_REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

REQUIRED_ARTIFACTS = (
    "result.json",
    "broken-before.png",
    "control-before.png",
    "broken-trace.zip",
    "control-trace.zip",
)
OPTIONAL_ARTIFACTS = ("broken-after.png", "control-after.png", "server.log", "driver.log")

_ARTIFACT_CONTENT_TYPES = {
    ".json": "application/json",
    ".png": "image/png",
    ".zip": "application/zip",
    ".log": "text/plain",
}

_TARBALL_URL = "https://api.github.com/repos/{repo}/tarball/{sha}"


class ReproBlocked(Exception):
    """Expected failure: setup, browser, dependency, or evidence unavailable."""


class Cancelled(Exception):
    """The run was cancelled by the user."""


def _shq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _check_cancel(cancel_event: threading.Event, deadline: float) -> None:
    if cancel_event.is_set():
        raise Cancelled()
    if time.monotonic() > deadline:
        raise ReproBlocked(
            f"reproduction exceeded the {config.REPRO_JOB_TIMEOUT_S:g}s job bound"
        )


def classify(result: object, fetched: set[str]) -> tuple[str, str]:
    """Map driver facts to (verdict, detail). Never claims fixed/verified."""
    missing = [name for name in REQUIRED_ARTIFACTS if name not in fetched]
    if missing:
        return "BLOCKED", f"missing required artifact(s): {', '.join(missing)}"
    if not isinstance(result, dict):
        return "BLOCKED", "driver result.json is not an object"
    errors = result.get("driverErrors")
    if errors:
        first = errors[0] if isinstance(errors, list) else errors
        return "BLOCKED", f"driver failed: {str(first)[:300]}"
    routes = result.get("routes")
    if not isinstance(routes, dict):
        return "BLOCKED", "driver result has no routes"
    broken = routes.get("broken")
    control = routes.get("control")
    if not isinstance(broken, dict) or not isinstance(control, dict):
        return "BLOCKED", "driver result is missing a route outcome"
    for label, route in (("broken", broken), ("control", control)):
        if route.get("fatal"):
            return "BLOCKED", f"{label} route error: {str(route['fatal'])[:300]}"
    if not broken.get("buttonPresent"):
        return "BLOCKED", "broken route: pay button missing (required evidence unavailable)"
    points = (broken.get("hitTest") or {}).get("points") or []
    center = points[4] if isinstance(points, list) and len(points) == 9 else None
    if center is None:
        return "BLOCKED", "broken route: hit-test evidence missing"
    occluded = not center.get("hitsButton", True)
    broken_blocked = (
        occluded and not broken.get("clicked") and not broken.get("confirmationObserved")
    )
    control_ok = bool(control.get("clicked")) and bool(control.get("confirmationObserved"))
    if broken_blocked and control_ok:
        top = broken.get("occlusion") or {}
        who = f"{top.get('tag', '?')}#{top.get('id', '?')}[{top.get('testid', '?')}]"
        return (
            "REPRODUCED",
            f"blocked click reproduced: {who} covers the button on "
            f"{broken.get('route')}; control confirmed "
            f"{str(control.get('confirmationText'))[:80]!r}",
        )
    if broken.get("clicked") and broken.get("confirmationObserved"):
        return (
            "NEEDS_REPRODUCTION",
            "broken route click succeeded with confirmation; reported failure not established",
        )
    if not occluded:
        return (
            "NEEDS_REPRODUCTION",
            "broken route button not occluded; reported failure not established",
        )
    if not control.get("clicked"):
        return (
            "NEEDS_REPRODUCTION",
            "control route click failed; oracle broken, failure not established",
        )
    return (
        "NEEDS_REPRODUCTION",
        "control route produced no confirmation; oracle broken, failure not established",
    )


class _Phase:
    """Per-run execution context: events, command log, cancellation."""

    def __init__(self, run_id: str, cancel_event: threading.Event, deadline: float) -> None:
        self.run_id = run_id
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.log_lines: list[str] = []

    def check(self) -> None:
        _check_cancel(self.cancel_event, self.deadline)

    def event(self, phase: str, message: str) -> None:
        repro_store.add_event(self.run_id, phase, message)

    def log_command(self, phase: str, cmd: str, exit_code: int, ms: int) -> None:
        self.log_lines.append(f"[{phase}] exit={exit_code} ms={ms} :: {cmd[:300]}")

    def exec_checked(
        self,
        sandbox: Any,
        phase: str,
        cmd: list[str],
        timeout_s: float,
        log_output: bool = False,
    ) -> tuple[int, str, str]:
        """Run a sandbox command with cancel/deadline-aware waiting."""
        self.check()
        display = " ".join(cmd)
        started = time.monotonic()
        try:
            # Modal's exec timeout takes an int; truncate, never round up.
            proc = sandbox.exec(*cmd, timeout=max(1, int(timeout_s)))
            while proc.poll() is None:
                self.check()
                time.sleep(0.5)
            code = proc.returncode if proc.returncode is not None else proc.poll()
            stdout = str(proc.stdout.read() or "")
            stderr = str(proc.stderr.read() or "")
        except (Cancelled, ReproBlocked):
            raise
        except Exception as exc:
            raise ReproBlocked(f"{phase}: command failed to complete: {exc}")
        ms = int((time.monotonic() - started) * 1000)
        self.log_command(phase, display, int(code), ms)
        if log_output:
            if stdout.strip():
                self.log_lines.append(stdout.strip()[:2000])
            if stderr.strip():
                self.log_lines.append(f"STDERR: {stderr.strip()[:2000]}")
        return int(code), stdout, stderr


def _driver_contract(frozen: FrozenContract) -> dict:
    manifest = frozen.manifest
    clicks = [a.selector for a in manifest.actions if a.action == "click" and a.selector]
    return {
        "baseUrl": f"http://127.0.0.1:{manifest.port}",
        "routes": {"broken": manifest.route, "control": manifest.controlRoute},
        "viewport": {"width": manifest.viewport.width, "height": manifest.viewport.height},
        "requiredElements": manifest.requiredElements,
        "clickSelector": clicks[0],
        "expectedConfirmation": {
            "selector": manifest.expectedConfirmation.selector,
            "textContains": manifest.expectedConfirmation.textContains,
        },
        "outDir": "/tmp/out",
        "clickTimeoutMs": 8000,
    }


def run_reproduction(
    run_id: str,
    cancel_event: threading.Event,
    frozen: Optional[FrozenContract] = None,
    modal_module: Any = None,
) -> None:
    """Execute one frozen contract (worker entry point)."""
    run = repro_store.get_run(run_id)
    if run is None:  # pragma: no cover - created before submit
        return
    deadline = time.monotonic() + config.REPRO_JOB_TIMEOUT_S
    ctx = _Phase(run_id, cancel_event, deadline)
    if frozen is None:
        payload = repro_store.load_contract(run_id)
        if payload is None:
            _finish(run_id, "error", None, "frozen contract missing for run", ctx)
            return
        from .contract import freeze_from_dict

        try:
            frozen = freeze_from_dict(payload)
        except (KeyError, ValueError) as exc:
            _finish(run_id, "error", None, f"frozen contract unreadable: {exc}", ctx)
            return
    repro_store.update_run(run_id, lambda r: setattr(r, "state", "running"))
    ctx.event("lifecycle", f"worker started at commit {frozen.sha[:7]}")

    if modal_module is None:
        try:
            import modal as modal_module  # lazy: optional until Modal runs
        except ImportError:
            _finish(run_id, "done", "BLOCKED", "modal package not installed", ctx)
            return
    adapter = ModalAdapter(
        token_id=config.get_modal_token_id(),
        token_secret=config.get_modal_token_secret(),
    )
    if adapter.status().state == "not_configured":
        _finish(run_id, "done", "BLOCKED", adapter.status().detail, ctx)
        return

    box: dict[str, str] = {}
    try:
        verdict, detail = _execute(ctx, frozen, modal_module, box)
        final = ("done", verdict, detail)
    except Cancelled:
        final = ("cancelled", None, "cancelled by user")
    except ReproBlocked as exc:
        final = ("done", "BLOCKED", str(exc))
    except Exception as exc:  # unexpected crash, never raw internals
        final = ("error", None, sanitize(f"runner crashed: {exc}", []))
    state, verdict, detail = final
    terminate_warning = box.get("terminate_warning", "")
    if terminate_warning:
        ctx.event("cleanup", terminate_warning)
        if state == "done":
            detail = f"{detail}; WARNING: {terminate_warning}"
    _finish(run_id, state, verdict, detail, ctx)


def _finish(
    run_id: str, state: str, verdict: Optional[str], detail: str, ctx: _Phase
) -> None:
    def mutate(run: Any) -> None:
        run.state = state
        run.verdict = verdict
        run.detail = detail

    repro_store.update_run(run_id, mutate)
    ctx.event("lifecycle", f"finished: {state}" + (f" {verdict}" if verdict else ""))


def _execute(
    ctx: _Phase, frozen: FrozenContract, modal: Any, box: dict[str, str]
) -> tuple[str, str]:
    manifest = frozen.manifest
    if not _REPO_RE.match(frozen.repo) or not _SHA_RE.match(frozen.sha):
        raise ReproBlocked("frozen contract has an invalid repo or SHA")
    ctx.check()

    driver_bytes = (Path(__file__).parent / "driver.cjs").read_bytes()
    contract_json = json.dumps(_driver_contract(frozen), indent=2).encode("utf-8")

    sandbox: Any = None
    try:
        with modal.Volume.ephemeral() as volume:
            with volume.batch_upload() as batch:
                batch.put_file(io.BytesIO(driver_bytes), "/driver.cjs")
                batch.put_file(io.BytesIO(contract_json), "/contract.json")
        ctx.event("volume", "driver + contract uploaded to ephemeral volume")
        ctx.check()

        app = modal.App.lookup("buglens-repro", create_if_missing=True)
        image = modal.Image.from_registry(config.REPRO_SANDBOX_IMAGE)
        sandbox = modal.Sandbox.create(
            app=app,
            image=image,
            volumes={"/buglens": volume},
            cpu=config.REPRO_SANDBOX_CPU,
            memory=config.REPRO_SANDBOX_MEMORY_MB,
            timeout=int(config.REPRO_SANDBOX_TIMEOUT_S),
        )
        ctx.event("sandbox", "sandbox created (node image, bounded cpu/memory/time)")
        return _run_phases(ctx, frozen, sandbox)
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate()
                ctx.event("cleanup", "sandbox terminated")
            except Exception as exc:
                box["terminate_warning"] = f"sandbox termination failed: {exc}"[:300]


def _run_phases(ctx: _Phase, frozen: FrozenContract, sandbox: Any) -> tuple[str, str]:
    manifest = frozen.manifest
    workdir = manifest.workingDirectory
    repo_app = f"/repo/{workdir}"

    # 1. Fetch the exact pinned commit (public tarball; no credentials).
    # node:22-slim ships no curl, so the download uses node itself.
    tarball = _TARBALL_URL.format(repo=frozen.repo, sha=frozen.sha)
    fetch_js = (
        f"fetch({_shq(tarball)})"
        ".then(r=>{if(!r.ok)throw new Error('http '+r.status);return r.arrayBuffer()})"
        ".then(b=>require('fs').writeFileSync('/tmp/repo.tgz',Buffer.from(b)))"
        ".catch(e=>{console.error(String(e));process.exit(1)})"
    )
    code, _out, _err = ctx.exec_checked(
        sandbox,
        "fetch",
        [
            "sh",
            "-c",
            "rm -rf /tmp/repo.tgz /repo && mkdir -p /repo"
            f" && node -e {_shq(fetch_js)}"
            " && tar -xzf /tmp/repo.tgz -C /repo --strip-components=1",
        ],
        timeout_s=180.0,
    )
    if code != 0:
        raise ReproBlocked(f"source fetch at {frozen.sha[:7]} failed (exit {code})")
    ctx.event("fetch", f"source extracted at {frozen.sha[:7]}")

    # 2. Fidelity: the extracted manifest must match the frozen hash.
    code, out, _err = ctx.exec_checked(
        sandbox,
        "fidelity",
        ["sh", "-c", "sha256sum /repo/.buglens.json | cut -d' ' -f1"],
        timeout_s=30.0,
    )
    if code != 0 or out.strip() != frozen.manifest_bytes_sha256:
        raise ReproBlocked("source integrity mismatch: extracted manifest differs from frozen contract")
    ctx.event("fidelity", "extracted manifest matches frozen hash")

    # 3. Frozen install command (repo root, sandbox only).
    code, _out, _err = ctx.exec_checked(
        sandbox,
        "install",
        ["sh", "-c", f"cd /repo && {manifest.install}"],
        timeout_s=config.REPRO_INSTALL_TIMEOUT_S,
        log_output=True,
    )
    if code != 0:
        raise ReproBlocked(f"dependency install failed (exit {code})")
    ctx.event("install", "dependencies installed")

    # 4. Playwright Chromium for Linux (never msedge/Windows paths).
    playwright_bin = f"{repo_app}/node_modules/.bin/playwright"
    code, _out, _err = ctx.exec_checked(
        sandbox,
        "browser",
        [
            "sh",
            "-c",
            f"{_shq(playwright_bin)} install-deps chromium"
            f" && {_shq(playwright_bin)} install chromium",
        ],
        timeout_s=config.REPRO_BROWSER_INSTALL_TIMEOUT_S,
        log_output=True,
    )
    if code != 0:
        raise ReproBlocked(f"chromium install failed (exit {code})")
    ctx.event("browser", "chromium ready")

    # 5. Start the fixture server in the background; wait for readiness.
    sandbox.exec(
        "sh",
        "-c",
        f"cd {_shq(repo_app)} && node server.js >/tmp/server.log 2>&1 & echo $! >/tmp/srv.pid; wait",
    )
    health_url = f"http://127.0.0.1:{manifest.port}/healthz"
    ready = False
    started = time.monotonic()
    while time.monotonic() - started < config.REPRO_SERVER_WAIT_S:
        ctx.check()
        code, _out, _err = ctx.exec_checked(
            sandbox,
            "readiness",
            [
                "node",
                "-e",
                f"fetch({_shq(health_url)}).then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
            ],
            timeout_s=10.0,
        )
        if code == 0:
            ready = True
            break
        time.sleep(1.0)
    if not ready:
        _fetch_optional_log(ctx, sandbox)
        raise ReproBlocked("fixture server never became ready")
    ctx.event("readiness", "fixture server answered /healthz")

    # 6. Run the driver (both routes, evidence to /tmp/out).
    code, driver_out, driver_err = ctx.exec_checked(
        sandbox,
        "drive",
        [
            "sh",
            "-c",
            f"cd {_shq(repo_app)}"
            f" && NODE_PATH={_shq(f'{repo_app}/node_modules')}"
            " node /buglens/driver.cjs",
        ],
        timeout_s=config.REPRO_DRIVER_TIMEOUT_S,
        log_output=True,
    )
    _store_log_artifact(ctx, "driver.log", f"{driver_out}\n{driver_err}")
    if code != 0:
        raise ReproBlocked(f"evidence driver exited {code}")
    ctx.event("drive", "driver completed both routes")

    # 7. Stop the server (best effort; sandbox termination is the backstop).
    try:
        ctx.exec_checked(sandbox, "stop", ["sh", "-c", "kill $(cat /tmp/srv.pid)"], timeout_s=15.0)
    except (ReproBlocked, Cancelled):
        raise
    except Exception:
        ctx.event("stop", "server kill skipped (already gone)")
    else:
        ctx.event("stop", "fixture server stopped")

    # 8. Fetch artifacts back over bounded base64 transfers.
    fetched: dict[str, bytes] = {}
    total = 0
    for name in REQUIRED_ARTIFACTS + OPTIONAL_ARTIFACTS:
        if name in ("driver.log", "server.log", "run.log"):
            continue  # handled separately below
        data = _fetch_artifact(ctx, sandbox, name, required=name in REQUIRED_ARTIFACTS)
        if data is not None:
            fetched[name] = data
            total += len(data)
    if total > config.REPRO_MAX_TOTAL_ARTIFACT_BYTES:
        raise ReproBlocked("artifact budget exceeded; evidence incomplete")
    _fetch_optional_log(ctx, sandbox)
    _store_log_artifact(ctx, "run.log", "\n".join(ctx.log_lines))
    result_bytes = fetched.get("result.json")
    if result_bytes is None:  # pragma: no cover - required check already failed
        raise ReproBlocked("missing required artifact(s): result.json")
    try:
        result = json.loads(result_bytes.decode("utf-8"))
    except ValueError:
        raise ReproBlocked("driver result.json is not valid JSON")
    return classify(result, set(fetched))


def _fetch_artifact(
    ctx: _Phase, sandbox: Any, name: str, required: bool
) -> Optional[bytes]:
    if not _ARTIFACT_NAME_RE.match(name):
        raise ReproBlocked(f"refusing unsafe artifact name: {name!r}")
    remote = f"/tmp/out/{name}"
    code, out, _err = ctx.exec_checked(
        sandbox, "artifacts", ["sh", "-c", f"stat -c %s {_shq(remote)}"], timeout_s=30.0
    )
    if code != 0:
        if required:
            raise ReproBlocked(f"missing required artifact(s): {name}")
        ctx.event("artifacts", f"optional artifact absent: {name}")
        return None
    try:
        size = int(out.strip().split()[0])
    except (ValueError, IndexError):
        raise ReproBlocked(f"could not stat artifact: {name}")
    if size > config.REPRO_MAX_ARTIFACT_BYTES:
        if required:
            raise ReproBlocked(f"artifact {name} exceeds size bound; evidence incomplete")
        ctx.event("artifacts", f"optional artifact too large, omitted: {name}")
        return None
    code, out, _err = ctx.exec_checked(
        sandbox, "artifacts", ["sh", "-c", f"base64 -w0 {_shq(remote)}"], timeout_s=60.0
    )
    if code != 0:
        if required:
            raise ReproBlocked(f"could not read artifact: {name}")
        return None
    try:
        data = base64.b64decode(out.strip())
    except (binascii.Error, ValueError):
        raise ReproBlocked(f"artifact transfer corrupt: {name}")
    _persist_artifact(ctx.run_id, name, data)
    ctx.event("artifacts", f"fetched {name} ({len(data)} bytes)")
    return data


def _fetch_optional_log(ctx: _Phase, sandbox: Any) -> None:
    try:
        code, out, _err = ctx.exec_checked(
            sandbox, "artifacts", ["sh", "-c", "cat /tmp/server.log"], timeout_s=30.0
        )
    except (ReproBlocked, Cancelled):
        raise
    except Exception:
        return
    if code == 0 and out.strip():
        _store_log_artifact(ctx, "server.log", out.strip()[:65536])


def _persist_artifact(run_id: str, name: str, data: bytes) -> None:
    suffix = Path(name).suffix.lower()
    content_type = _ARTIFACT_CONTENT_TYPES.get(suffix, "application/octet-stream")
    stored = f"{uuid.uuid4().hex[:12]}-{name}"
    repro_store.add_artifact(run_id, name, content_type, data, stored)


def _store_log_artifact(ctx: _Phase, name: str, text: str) -> None:
    _persist_artifact(ctx.run_id, name, text.encode("utf-8")[:65536])
    ctx.event("artifacts", f"recorded {name}")
