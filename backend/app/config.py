"""Backend configuration.

Local development only. Production settings will be introduced with the
Modal deployment milestone.
"""

import os
from pathlib import Path

# Exact origin served by `npm run dev` in frontend/ (Vite default).
FRONTEND_ORIGIN = "http://localhost:5173"

# Backend host/port used in README startup commands.
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8000

# --- M1 evidence intake limits ------------------------------------------------
# Referenced dynamically as ``config.MAX_...`` so tests can monkeypatch them.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_EVIDENCE_PER_INCIDENT = 20
MAX_INCIDENT_BYTES = 250 * 1024 * 1024

# Minimum accepted length for BUGLENS_API_TOKEN. Generate with:
#   python -c "import secrets; print(secrets.token_hex(32))"
MIN_API_TOKEN_LENGTH = 16

# --- Step 3 provider endpoints ------------------------------------------------
# Official bases; Meta's is overridable for explicit pinning only.
META_API_BASE_URL = "https://api.meta.ai/v1"
GITHUB_API_BASE_URL = "https://api.github.com"
BASETEN_API_BASE_URL = "https://inference.baseten.co/v1"

# Bounded provider calls: per-read timeout plus linear-backoff retries.
PROVIDER_TIMEOUT_S = 60.0
PROVIDER_MAX_RETRIES = 2

# Wall-clock bound for the Modal sandbox smoke (create + echo + terminate).
# Referenced dynamically as ``config.MODAL_SMOKE_TIMEOUT_S`` so tests can
# monkeypatch it.
MODAL_SMOKE_TIMEOUT_S = 180.0

# --- Step 5 Modal reproduction bounds -----------------------------------------
# All referenced dynamically as ``config.REPRO_*`` so tests can monkeypatch.
REPRO_SANDBOX_IMAGE = "node:22-slim"
REPRO_SANDBOX_CPU = 1.0
REPRO_SANDBOX_MEMORY_MB = 2048
REPRO_SANDBOX_TIMEOUT_S = 900.0
REPRO_JOB_TIMEOUT_S = 1200.0
REPRO_MAX_CONCURRENT = 1
REPRO_INSTALL_TIMEOUT_S = 300.0
REPRO_BROWSER_INSTALL_TIMEOUT_S = 420.0
REPRO_SERVER_WAIT_S = 60.0
REPRO_DRIVER_TIMEOUT_S = 300.0
REPRO_MAX_ARTIFACT_BYTES = 12 * 1024 * 1024
REPRO_MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024


def get_api_token() -> str:
    """Application API token from the environment (there is no default)."""
    return os.environ.get("BUGLENS_API_TOKEN", "")


def get_meta_api_key() -> str:
    return os.environ.get("META_API_KEY", "")


def get_meta_base_url() -> str:
    return os.environ.get("META_API_BASE_URL", "") or META_API_BASE_URL


def get_github_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def get_github_repo() -> str:
    return os.environ.get("GITHUB_REPO", "")


def get_modal_token_id() -> str:
    return os.environ.get("MODAL_TOKEN_ID", "")


def get_modal_token_secret() -> str:
    return os.environ.get("MODAL_TOKEN_SECRET", "")


def get_baseten_api_key() -> str:
    return os.environ.get("BASETEN_API_KEY", "")


def get_data_dir() -> Path:
    """Directory for incident records and evidence artifacts.

    Defaults to ``backend/data`` (git-ignored). Override with
    ``BUGLENS_DATA_DIR`` (used by tests and live probes).
    """
    override = os.environ.get("BUGLENS_DATA_DIR", "")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data"
