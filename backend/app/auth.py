"""Application authentication for incident and artifact endpoints.

All ``/api/*`` routes require ``Authorization: Bearer <token>`` where the
token equals the server-side ``BUGLENS_API_TOKEN`` environment variable
(loaded from ``.env`` for local development). Comparison is constant-time.
``/health`` stays public so load balancers and the frontend status card can
probe liveness without credentials.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from . import config


def require_auth(request: Request) -> None:
    server_token = config.get_api_token()
    if not server_token or len(server_token) < config.MIN_API_TOKEN_LENGTH:
        raise HTTPException(
            status_code=503,
            detail=(
                "Server misconfigured: set BUGLENS_API_TOKEN to at least "
                f"{config.MIN_API_TOKEN_LENGTH} characters."
            ),
        )
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(presented.encode("utf-8"), server_token.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="Invalid API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
