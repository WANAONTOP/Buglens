"""Modal deployment entry point for the BugLens AI backend.

Local development keeps using uvicorn (see README). This module is only
used by ``modal serve`` / ``modal deploy`` from the repository root::

    modal deploy backend/modal_app.py

It defines one ASGI function serving the FastAPI app plus four worker
functions (reproduce, investigate, generate, verify), all sharing one
Volume for incidents/artifacts and one Secret for backend credentials.
Target-code sandboxes are unchanged: workers create them via the SDK
and still pass no credentials into them.

Deploy prerequisites (names only; see README "Deploying to Modal"):

- Modal authentication (``modal setup`` or ``MODAL_TOKEN_ID`` /
  ``MODAL_TOKEN_SECRET``) with the ``buglens-ai`` app.
- Volume ``buglens-data`` (v2) holding ``/data``.
- Secret ``buglens-secrets`` with the backend credential names.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # backend/ locally
# Remotely Modal uploads this file to /root while the backend directory is
# baked at /root/backend, so resolve whichever layout actually holds `app`.
for _candidate in (HERE, HERE / "backend"):
    if (_candidate / "app" / "__init__.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

import modal  # noqa: E402

from app.deploy import (  # noqa: E402
    DATA_MOUNT,
    MODAL_APP_NAME,
    MODAL_SECRET_NAME,
    MODAL_VOLUME_NAME,
)

# Remote image: the same dependency pins as backend/requirements.txt
# (uvicorn/pytest are local-only; Modal serves ASGI itself), with this
# directory baked in so `app.*` imports resolve remotely.
IMAGE = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install(
        "fastapi>=0.115,<0.120",
        "httpx>=0.27,<0.29",
        "python-multipart>=0.0.18,<0.0.22",
        "python-dotenv>=1.0,<2",
    )
    .add_local_dir(
        HERE,
        remote_path="/root/backend",
        # Never bake records, evidence, tests, or anything secret-shaped
        # into the image: records live on the Volume, and credentials /
        # evidence must not leak into images.
        ignore=[
            "data",
            "tests",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".env*",
            "*.log",
            "*.pem",
            "*.key",
        ],
    )
)
VOLUME = modal.Volume.from_name(MODAL_VOLUME_NAME)
SECRETS = modal.Secret.from_name(MODAL_SECRET_NAME)
ENV = {
    "BUGLENS_MODAL": "1",
    "BUGLENS_JOBS": "modal",
    "BUGLENS_DATA_DIR": DATA_MOUNT,
}

app = modal.App(MODAL_APP_NAME)


def _base_kwargs(**overrides):
    kwargs = {
        "image": IMAGE,
        "volumes": {DATA_MOUNT: VOLUME},
        "secrets": [SECRETS],
        "env": dict(ENV),
    }
    kwargs.update(overrides)
    return kwargs


def _worker_kwargs(**overrides):
    # Never auto-retry paid/GitHub operations (web functions like `api`
    # reject the retries parameter entirely, so it lives here).
    return _base_kwargs(retries=0, **overrides)


@app.function(**_base_kwargs(max_containers=1, timeout=900))
@modal.asgi_app()
def api():
    """The FastAPI backend (single container: in-process locks stay valid)."""
    from app.main import app as fastapi_app

    return fastapi_app


@app.function(**_worker_kwargs(timeout=1500))
def run_repro_worker(run_id: str) -> None:
    """Execute one frozen reproduction (job budget 1200s + headroom)."""
    from app.workers import run_repro_guarded

    run_repro_guarded(run_id)


@app.function(**_worker_kwargs(timeout=900))
def run_investigate_worker(inv_id: str) -> None:
    """Execute one investigation (job budget 600s + headroom)."""
    from app.workers import run_investigation_guarded

    run_investigation_guarded(inv_id)


@app.function(**_worker_kwargs(timeout=900))
def run_candidate_worker(candidate_id: str) -> None:
    """Generate one candidate (job budget 600s + headroom)."""
    from app.workers import run_candidate_guarded

    run_candidate_guarded(candidate_id)


@app.function(**_worker_kwargs(timeout=2400))
def run_verify_worker(verification_id: str) -> None:
    """Execute one seven-gate verification (job budget 1800s + headroom)."""
    from app.workers import run_verification_guarded

    run_verification_guarded(verification_id)
