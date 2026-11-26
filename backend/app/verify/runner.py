"""Verification orchestration: fresh sandboxes, trusted gate decisions.

Flow per verification: re-check approval binding (stale or changed
content refuses to run), freeze + hash the verification policy, run the
frozen driver on baseline and candidate checkouts in fresh node
sandboxes, run the pinned repo suite on both checkouts in fresh python
sandboxes, evaluate the seven gates server-side, and persist everything.
No credentials enter any sandbox; the developer working tree is never
touched (patched bytes are applied to fetched blobs in memory and
uploaded through ephemeral volumes).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import threading
import time
from typing import Any, Optional

from .. import config
from .. import store as incident_store
from ..investigate import store as inv_store
from ..patches import store as patch_store
from ..patches.validate import apply_in_memory, parse_diff, validate_diff
from ..providers.base import sanitize
from ..providers.modal import SETUP_DETAIL, ModalAdapter
from ..repro import store as repro_store
from ..repro.github_source import GitHubSourceError, fetch_blob, fetch_tree
from ..repro.runner import _driver_contract as _repro_driver_contract
from . import store as verify_store
from .gates import GateEvidence, evaluate_all
from .models import Verification
from .policy import VerifyPolicy, freeze_policy, policy_to_json

_TARBALL_URL = "https://api.github.com/repos/{repo}/tarball/{sha}"


class VerifyBlocked(Exception):
    """Verification cannot proceed or complete (honestly recorded)."""


class Cancelled(Exception):
    """The verification was cancelled by the user."""


def _shq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _check(cancel_event: threading.Event, deadline: float) -> None:
    if cancel_event.is_set():
        raise Cancelled()
    if time.monotonic() > deadline:
        raise VerifyBlocked(
            f"verification exceeded the {config.VERIFY_JOB_TIMEOUT_S:g}s job bound"
        )


class _Ctx:
    """Per-run execution context: events, command log, cancellation."""

    def __init__(self, verification_id: str, cancel_event: threading.Event,
                 deadline: float) -> None:
        self.verification_id = verification_id
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.log_lines: list[str] = []

    def check(self) -> None:
        _check(self.cancel_event, self.deadline)

    def event(self, phase: str, message: str) -> None:
        verify_store.add_event(self.verification_id, phase, message)

    def exec_checked(self, sandbox: Any, phase: str, cmd: list[str],
                     timeout_s: float) -> tuple[int, str, str]:
        """Run a sandbox command with cancel/deadline-aware waiting."""
        self.check()
        display = " ".join(cmd)
        started = time.monotonic()
        try:
            proc = sandbox.exec(*cmd, timeout=max(1, int(timeout_s)))
            while proc.poll() is None:
                self.check()
                time.sleep(0.5)
            code = proc.returncode if proc.returncode is not None else proc.poll()
            stdout = str(proc.stdout.read() or "")
            stderr = str(proc.stderr.read() or "")
        except (Cancelled, VerifyBlocked):
            raise
        except Exception as exc:
            raise VerifyBlocked(f"{phase}: command failed to complete: {exc}")
        ms = int((time.monotonic() - started) * 1000)
        self.log_lines.append(f"[{phase}] exit={code} ms={ms} :: {display[:300]}")
        return int(code), stdout, stderr


def _node_fetch_js(repo: str, sha: str) -> str:
    tarball = _TARBALL_URL.format(repo=repo, sha=sha)
    return (
        f"fetch({_shq(tarball)})"
        ".then(r=>{if(!r.ok)throw new Error('http '+r.status);return r.arrayBuffer()})"
        ".then(b=>require('fs').writeFileSync('/tmp/repo.tgz',Buffer.from(b)))"
        ".catch(e=>{console.error(String(e));process.exit(1)})"
    )


_PY_FETCH = (
    "import urllib.request;"
    "urllib.request.urlretrieve('{url}','/tmp/repo.tgz')"
)


def _fetch_and_verify(ctx: _Ctx, sandbox: Any, label: str, repo: str, sha: str,
                      manifest_sha: str, node: bool) -> None:
    if node:
        fetch_cmd = f"node -e {_shq(_node_fetch_js(repo, sha))}"
    else:
        fetch_cmd = f"python3 -c {_shq(_PY_FETCH.format(url=_TARBALL_URL.format(repo=repo, sha=sha)))}"
    code, _, _ = ctx.exec_checked(
        sandbox, f"{label}-fetch",
        ["sh", "-c", "rm -rf /tmp/repo.tgz /repo && mkdir -p /repo"
         f" && {fetch_cmd} && tar -xzf /tmp/repo.tgz -C /repo --strip-components=1"],
        timeout_s=180.0,
    )
    if code != 0:
        raise VerifyBlocked(f"{label}: source fetch at {sha[:7]} failed (exit {code})")
    code, out, _ = ctx.exec_checked(
        sandbox, f"{label}-fidelity",
        ["sh", "-c", "sha256sum /repo/.buglens.json | cut -d' ' -f1"], timeout_s=30.0)
    if code != 0 or out.strip() != manifest_sha:
        raise VerifyBlocked(f"{label}: extracted manifest differs from frozen contract")
    ctx.event(f"{label}-fetch", "source extracted and verified")


def _overlay_candidate_files(ctx: _Ctx, sandbox: Any, label: str,
                             patched: dict[str, bytes]) -> None:
    for path, data in patched.items():
        digest = hashlib.sha256(data).hexdigest()
        # Volume paths (/overlay/...) appear under the /buglens mount.
        remote = f"/buglens/overlay/{path}"
        code, out, err = ctx.exec_checked(
            sandbox, f"{label}-overlay",
            ["sh", "-c", f"cp {_shq(remote)} {_shq(f'/repo/{path}')}"
                         f" && sha256sum {_shq(f'/repo/{path}')} | cut -d' ' -f1"],
            timeout_s=30.0)
        if code != 0:
            raise VerifyBlocked(f"{label}: overlay copy failed for {path} (exit {code}): "
                                f"{err.strip()[:160]}")
        if out.strip() != digest:
            raise VerifyBlocked(f"{label}: candidate checkout diverged for {path} "
                                f"(hash {out.strip()[:12]} != expected {digest[:12]})")
    ctx.event(f"{label}-overlay", f"{len(patched)} patched file(s) verified in checkout")


def _pull_file(ctx: _Ctx, sandbox: Any, label: str, remote: str) -> bytes:
    code, out, _ = ctx.exec_checked(
        sandbox, label, ["sh", "-c", f"stat -c %s {_shq(remote)}"], timeout_s=30.0)
    if code != 0:
        raise VerifyBlocked(f"{label}: missing remote file {remote}")
    try:
        size = int(out.strip().split()[0])
    except (ValueError, IndexError):
        raise VerifyBlocked(f"{label}: could not stat {remote}")
    if size > config.REPRO_MAX_ARTIFACT_BYTES:
        raise VerifyBlocked(f"{label}: {remote} exceeds the size bound")
    code, out, _ = ctx.exec_checked(
        sandbox, label, ["sh", "-c", f"base64 -w0 {_shq(remote)}"], timeout_s=60.0)
    if code != 0:
        raise VerifyBlocked(f"{label}: could not read {remote}")
    try:
        return base64.b64decode(out.strip())
    except (binascii.Error, ValueError):
        raise VerifyBlocked(f"{label}: transfer corrupt for {remote}")


def _run_node_checkout(ctx: _Ctx, modal: Any, frozen: Any, manifest: Any,
                       label: str, patched: dict[str, bytes]) -> dict[str, bytes]:
    """Drive one checkout (baseline or candidate) in a fresh node sandbox."""
    from pathlib import Path as _Path

    driver_bytes = (_Path(__file__).parent.parent / "repro" / "driver.cjs").read_bytes()
    contract_json = json.dumps(_repro_driver_contract(frozen), indent=2).encode("utf-8")
    workdir = manifest["workingDirectory"]
    repo_app = f"/repo/{workdir}"
    sandbox: Any = None
    try:
        with modal.Volume.ephemeral() as volume:
            with volume.batch_upload() as batch:
                batch.put_file(io.BytesIO(driver_bytes), "/driver.cjs")
                batch.put_file(io.BytesIO(contract_json), "/contract.json")
                for path, data in patched.items():
                    batch.put_file(io.BytesIO(data), f"/overlay/{path}")
        ctx.check()
        app = modal.App.lookup("buglens-verify", create_if_missing=True)
        image = modal.Image.from_registry(config.REPRO_SANDBOX_IMAGE)
        sandbox = modal.Sandbox.create(
            app=app, image=image, volumes={"/buglens": volume},
            cpu=config.REPRO_SANDBOX_CPU, memory=config.REPRO_SANDBOX_MEMORY_MB,
            timeout=int(config.VERIFY_SANDBOX_TIMEOUT_S))
        ctx.event(label, "node sandbox created")
        _fetch_and_verify(ctx, sandbox, label, frozen.repo, frozen.sha,
                          frozen.manifest_bytes_sha256, node=True)
        if patched:
            _overlay_candidate_files(ctx, sandbox, label, patched)
        code, _, _ = ctx.exec_checked(
            sandbox, label, ["sh", "-c", f"cd /repo && {manifest['install']}"],
            timeout_s=config.REPRO_INSTALL_TIMEOUT_S)
        if code != 0:
            raise VerifyBlocked(f"{label}: dependency install failed (exit {code})")
        playwright_bin = f"{repo_app}/node_modules/.bin/playwright"
        code, _, _ = ctx.exec_checked(
            sandbox, label,
            ["sh", "-c", f"{_shq(playwright_bin)} install-deps chromium"
                         f" && {_shq(playwright_bin)} install chromium"],
            timeout_s=config.REPRO_BROWSER_INSTALL_TIMEOUT_S)
        if code != 0:
            raise VerifyBlocked(f"{label}: chromium install failed (exit {code})")
        sandbox.exec("sh", "-c",
                     f"cd {_shq(repo_app)} && node server.js >/tmp/server.log 2>&1 & echo $! >/tmp/srv.pid; wait")
        health_url = f"http://127.0.0.1:{manifest['port']}/healthz"
        ready = False
        started = time.monotonic()
        while time.monotonic() - started < config.REPRO_SERVER_WAIT_S:
            ctx.check()
            code, _, _ = ctx.exec_checked(
                sandbox, label,
                ["node", "-e", f"fetch({_shq(health_url)}).then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],
                timeout_s=10.0)
            if code == 0:
                ready = True
                break
            time.sleep(1.0)
        if not ready:
            raise VerifyBlocked(f"{label}: fixture server never became ready")
        code, _, _ = ctx.exec_checked(
            sandbox, label,
            ["sh", "-c", f"cd {_shq(repo_app)} && NODE_PATH={_shq(f'{repo_app}/node_modules')}"
                         " node /buglens/driver.cjs"],
            timeout_s=config.REPRO_DRIVER_TIMEOUT_S)
        if code != 0:
            raise VerifyBlocked(f"{label}: evidence driver exited {code}")
        try:
            ctx.exec_checked(sandbox, label, ["sh", "-c", "kill $(cat /tmp/srv.pid)"], timeout_s=15.0)
        except (VerifyBlocked, Cancelled):
            raise
        except Exception:
            pass
        artifacts: dict[str, bytes] = {}
        for name in ("result.json", "broken-before.png", "broken-after.png",
                     "control-before.png", "control-after.png",
                     "broken-trace.zip", "control-trace.zip"):
            artifacts[name] = _pull_file(ctx, sandbox, label, f"/tmp/out/{name}")
        try:
            code, out, _ = ctx.exec_checked(
                sandbox, label, ["sh", "-c", "cat /tmp/server.log"], timeout_s=30.0)
            if code == 0 and out.strip():
                artifacts["server.log"] = out.strip()[:65536].encode("utf-8")
        except (VerifyBlocked, Cancelled):
            raise
        except Exception:
            pass
        ctx.event(label, f"checkout drove both routes ({len(artifacts)} artifacts)")
        return artifacts
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate()
                ctx.event(label, "node sandbox terminated")
            except Exception as exc:
                ctx.event(label, f"sandbox termination failed: {exc}"[:200])


def _run_pytest_checkout(ctx: _Ctx, modal: Any, repo: str, sha: str,
                         manifest_sha: str, label: str,
                         patched: dict[str, bytes], gate4_command: list[str]) -> dict[str, str]:
    """Run the pinned repo suite on one checkout in a fresh python sandbox."""
    sandbox: Any = None
    try:
        with modal.Volume.ephemeral() as volume:
            with volume.batch_upload() as batch:
                for path, data in patched.items():
                    batch.put_file(io.BytesIO(data), f"/overlay/{path}")
        ctx.check()
        app = modal.App.lookup("buglens-verify", create_if_missing=True)
        image = modal.Image.from_registry(config.VERIFY_PYTHON_IMAGE)
        sandbox = modal.Sandbox.create(
            app=app, image=image, volumes={"/buglens": volume},
            cpu=config.REPRO_SANDBOX_CPU, memory=config.REPRO_SANDBOX_MEMORY_MB,
            timeout=int(config.VERIFY_SANDBOX_TIMEOUT_S))
        ctx.event(label, "python sandbox created")
        _fetch_and_verify(ctx, sandbox, label, repo, sha, manifest_sha, node=False)
        if patched:
            _overlay_candidate_files(ctx, sandbox, label, patched)
        code, _, _ = ctx.exec_checked(
            sandbox, label,
            ["sh", "-c", "cd /repo && pip install -q -r backend/requirements.txt"],
            timeout_s=config.VERIFY_PYTEST_TIMEOUT_S)
        if code != 0:
            raise VerifyBlocked(f"{label}: suite dependencies failed (exit {code})")
        outputs: dict[str, str] = {}
        for name, cmd in (("collect", gate4_command[:-5] + ["--collect-only", "-q"]),
                          ("tests", gate4_command)):
            code, out, err = ctx.exec_checked(
                sandbox, label, ["sh", "-c", "cd /repo && " + " ".join(_shq(c) for c in cmd)],
                timeout_s=config.VERIFY_PYTEST_TIMEOUT_S)
            outputs[name] = f"[exit {code}]\n{out}\n{err}".strip()[:200000]
        ctx.event(label, "pinned suite executed")
        return outputs
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate()
                ctx.event(label, "python sandbox terminated")
            except Exception as exc:
                ctx.event(label, f"sandbox termination failed: {exc}"[:200])


def _finish(verification_id: str, state: str, outcome: Optional[str], detail: str) -> None:
    def mutate(item: Verification) -> None:
        item.state = state  # type: ignore[assignment]
        item.outcome = outcome  # type: ignore[assignment]
        item.detail = detail

    verify_store.update(verification_id, mutate)
    verify_store.add_event(verification_id, "lifecycle", f"finished: {state}/{outcome}")


def _patched_bytes(candidate_diff: str, pinned: dict[str, list[str]],
                   trailing_newline: dict[str, bool]) -> dict[str, bytes]:
    files = parse_diff(candidate_diff)
    patched: dict[str, bytes] = {}
    for parsed in files:
        new_lines = apply_in_memory(pinned[parsed.path], parsed.hunks)
        text = "\n".join(new_lines)
        if trailing_newline.get(parsed.path):
            text += "\n"
        patched[parsed.path] = text.encode("utf-8")
    return patched


def run_verification(
    verification_id: str,
    cancel_event: threading.Event,
    github_transport: Any = None,
) -> None:
    """Execute one seven-gate verification (worker entry point)."""
    record = verify_store.get(verification_id)
    if record is None:  # pragma: no cover - created before submit
        return
    deadline = time.monotonic() + config.VERIFY_JOB_TIMEOUT_S
    ctx = _Ctx(verification_id, cancel_event, deadline)

    def save(**fields: Any) -> None:
        def mutate(item: Verification) -> None:
            for key, value in fields.items():
                setattr(item, key, value)

        verify_store.update(verification_id, mutate)

    try:
        candidate = patch_store.get(record.candidate_id)
        incident = incident_store.get_incident(record.incident_id)
        run = repro_store.get_run(record.run_id)
        inv = inv_store.get(record.investigation_id)
        if candidate is None or incident is None or run is None or inv is None:
            raise VerifyBlocked("incident, run, investigation, or candidate record is missing")
        # Approval + provenance recheck at execution time (stale refuses).
        if candidate.state != "approved" or candidate.approval is None:
            raise VerifyBlocked("candidate is not approved; execution refused")
        current_hash = hashlib.sha256(candidate.diff.encode("utf-8")).hexdigest()
        if current_hash != candidate.diff_sha256:
            raise VerifyBlocked("candidate diff changed since validation; execution refused")
        if candidate.approval.diff_sha256 != candidate.diff_sha256:
            raise VerifyBlocked("approval binding is stale; execution refused")
        if candidate.baseline_sha != incident.commit_sha or run.commit_sha != incident.commit_sha:
            raise VerifyBlocked("baseline drift: records disagree with the incident pin")
        if run.contract_hash != candidate.contract_hash:
            raise VerifyBlocked("contract drift between run and candidate")
        if candidate.validation is None or not candidate.validation.ok:
            raise VerifyBlocked("candidate failed validation; execution refused")
        save(state="running")
        ctx.event("lifecycle", f"worker started on candidate {candidate.id[:12]} "
                               f"(approval {candidate.approval.approved_at.isoformat()})")
        ctx.check()

        contract = repro_store.load_contract(run.id)
        if contract is None or contract.get("contract_hash") != run.contract_hash:
            raise VerifyBlocked("frozen contract record is missing or inconsistent")
        manifest = contract["manifest"]
        token = config.get_github_token()
        if not token:
            raise VerifyBlocked("Set GITHUB_TOKEN in .env to read the pinned sources.")
        try:
            tree = fetch_tree(run.repo, run.commit_sha, token, transport=github_transport)
        except GitHubSourceError as exc:
            raise VerifyBlocked(f"pinned tree unreadable: {exc}")
        by_path = {entry.path: entry for entry in tree}
        pinned: dict[str, list[str]] = {}
        trailing: dict[str, bool] = {}
        entry_types: dict[str, str] = {}
        for path in candidate.changed_paths:
            entry = by_path.get(path)
            if entry is None or entry.type != "blob":
                raise VerifyBlocked(f"{path} is not a readable blob at the pinned SHA")
            try:
                data = fetch_blob(run.repo, entry.sha, token, transport=github_transport)
            except GitHubSourceError as exc:
                raise VerifyBlocked(f"{path}: blob fetch failed: {exc}")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise VerifyBlocked(f"{path} is not UTF-8 at the pinned SHA")
            pinned[path] = text.splitlines()
            trailing[path] = text.endswith("\n")
            entry_types[path] = "symlink" if entry.is_symlink else entry.type
        # Defense in depth: re-validate the exact diff before execution.
        revalidation, _ = validate_diff(
            candidate.diff, permitted=manifest.get("permittedPatchPaths", []),
            protected=manifest.get("protectedPaths", []),
            pinned=pinned, entry_types=entry_types)
        if not revalidation.ok:
            raise VerifyBlocked("candidate re-validation failed: " + "; ".join(revalidation.reasons))
        patched = _patched_bytes(candidate.diff, pinned, trailing)
        expected_hashes = {p: hashlib.sha256(b).hexdigest() for p, b in patched.items()}
        ctx.event("context", f"{len(patched)} patched file(s) computed from pinned bytes")

        policy = freeze_policy(
            manifest=manifest,
            pixel_diff_fraction=config.VERIFY_PIXEL_DIFF_FRACTION,
            pixel_channel_tolerance=config.VERIFY_PIXEL_CHANNEL_TOLERANCE)
        save(policy_hash=policy.policy_hash)
        verify_store.record_artifact(verification_id, "policy.json", "application/json",
                                     policy_to_json(policy).encode("utf-8"))
        from .models import ApprovalSnapshot, TestedIdentity

        save(approval=ApprovalSnapshot(
            candidate_id=candidate.id, diff_sha256=candidate.diff_sha256,
            approved_at=candidate.approval.approved_at),
            tested=TestedIdentity(diff_sha256=candidate.diff_sha256,
                                  file_hashes=dict(expected_hashes),
                                  baseline_sha=candidate.baseline_sha))
        ctx.event("policy", f"verification policy frozen ({policy.policy_hash[:12]})")
        ctx.check()

        adapter = ModalAdapter(token_id=config.get_modal_token_id(),
                               token_secret=config.get_modal_token_secret())
        if adapter.status().state == "not_configured":
            raise VerifyBlocked("Modal authentication missing: " + SETUP_DETAIL)
        try:
            import modal as modal_module
        except ImportError:
            raise VerifyBlocked("Modal SDK is not installed in the backend environment")
        from ..repro.contract import freeze_from_dict

        frozen = freeze_from_dict(contract)

        bl_art = _run_node_checkout(ctx, modal_module, frozen, manifest, "bl", {})
        ctx.check()
        cd_art = _run_node_checkout(ctx, modal_module, frozen, manifest, "cd", patched)
        ctx.check()
        gate4_bl = _run_pytest_checkout(ctx, modal_module, run.repo, run.commit_sha,
                                        frozen.manifest_bytes_sha256, "gate4-bl", {},
                                        list(policy.gate4_command))
        ctx.check()
        gate4_cd = _run_pytest_checkout(ctx, modal_module, run.repo, run.commit_sha,
                                        frozen.manifest_bytes_sha256, "gate4-cd", patched,
                                        list(policy.gate4_command))
        ctx.check()

        def content_type(name: str) -> str:
            if name.endswith(".png"):
                return "image/png"
            if name.endswith(".json"):
                return "application/json"
            if name.endswith(".zip"):
                return "application/zip"
            return "text/plain"

        count = 0
        for prefix, bundle in (("bl", bl_art), ("cd", cd_art)):
            for name, data in bundle.items():
                verify_store.record_artifact(verification_id, f"{prefix}-{name}",
                                             content_type(name), data)
                count += 1
        for label, output in (("bl", gate4_bl["tests"]), ("cd", gate4_cd["tests"])):
            verify_store.record_artifact(verification_id, f"gate4-{label}-tests.txt",
                                         "text/plain", output.encode("utf-8"))
            count += 1
        verify_store.record_artifact(verification_id, "run.log", "text/plain",
                                     "\n".join(ctx.log_lines).encode("utf-8"))
        ctx.event("artifacts", f"{count} evidence artifacts persisted")

        try:
            bl_result = json.loads(bl_art["result.json"].decode("utf-8"))
            cd_result = json.loads(cd_art["result.json"].decode("utf-8"))
        except ValueError:
            raise VerifyBlocked("a checkout returned invalid result.json")
        evidence = GateEvidence(
            bl_result=bl_result if isinstance(bl_result, dict) else None,
            cd_result=cd_result if isinstance(cd_result, dict) else None,
            shots={f"bl-{k}": v for k, v in bl_art.items() if k.endswith(".png")}
                  | {f"cd-{k}": v for k, v in cd_art.items() if k.endswith(".png")},
            gate4={"bl": gate4_bl, "cd": gate4_cd},
            changed_paths=list(candidate.changed_paths),
            tested_hashes=dict(expected_hashes),
            expected_hashes=dict(expected_hashes),
            diff_sha256=candidate.diff_sha256,
            approved_sha256=candidate.approval.diff_sha256,
        )
        gates = evaluate_all(evidence, policy)
        save(gates=gates)
        for gate in gates:
            ctx.event(f"gate-{gate.gate}", f"{gate.name}: {gate.verdict} — {gate.detail[:160]}")
        if all(g.verdict == "PASS" for g in gates):
            _finish(verification_id, "done", "VERIFIED",
                    "all seven gates passed within the tested scope")
        elif any(g.verdict == "FAIL" for g in gates):
            failed = [str(g.gate) for g in gates if g.verdict == "FAIL"]
            _finish(verification_id, "done", "NOT_VERIFIED",
                    f"gates failed: {', '.join(failed)}")
        else:
            _finish(verification_id, "done", "BLOCKED",
                    "a gate is blocked on missing evidence")
    except Cancelled:
        _finish(verification_id, "blocked", "BLOCKED", "cancelled by user")
    except VerifyBlocked as exc:
        _finish(verification_id, "blocked", "BLOCKED", str(exc))
    except Exception as exc:  # unexpected crash, never raw internals
        _finish(verification_id, "error", "BLOCKED", sanitize(f"verification crashed: {exc}", []))
