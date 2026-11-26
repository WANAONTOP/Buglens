import anyio.to_thread
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config, deploy
from .routes_incidents import router as incidents_router
from .routes_investigate import router as investigate_router
from .routes_patches import router as patches_router
from .routes_providers import router as providers_router
from .routes_publish import router as publish_router
from .routes_repro import router as repro_router
from .routes_verify import router as verify_router

# Local development: read backend-adjacent .env (repo-root .env when started
# from the root). Real environment variables always take precedence, which is
# what the test suite relies on. On Modal, secrets arrive as real environment
# variables and no .env file exists (there is nothing to load).
load_dotenv()

app = FastAPI(title="BugLens AI", version="0.1.0")

# CORS: the local Vite origin by default, env-overridable for a hosted
# frontend (BUGLENS_CORS_ORIGINS, comma-separated).
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.get_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)


@app.middleware("http")
async def volume_sync_middleware(request: Request, call_next):
    """Deploy mode: reload before each request, commit after each write.

    Local mode is untouched (both helpers are no-ops there). Reloading
    per request keeps every read fresh; committing after mutating
    methods publishes the request's writes to workers and polls. The
    blocking volume calls run in a worker thread so the event loop
    never stalls on them.
    """
    if deploy.is_modal():
        try:
            await anyio.to_thread.run_sync(deploy.sync_before_read)
        except deploy.DeployError as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})
    response = await call_next(request)
    if (
        deploy.is_modal()
        and request.method in ("POST", "PATCH", "PUT", "DELETE")
        and response.status_code < 500
    ):
        try:
            await anyio.to_thread.run_sync(deploy.sync_after_write)
        except deploy.DeployError as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})
    return response


@app.exception_handler(deploy.ConflictError)
async def conflict_handler(_request: Request, exc: deploy.ConflictError) -> JSONResponse:
    """A cross-container write conflict is a retryable 409, never silent loss."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(deploy.DeployError)
async def deploy_handler(_request: Request, exc: deploy.DeployError) -> JSONResponse:
    """Deploy plumbing (volume sync, spawn, poll) failed: retryable 503."""
    return JSONResponse(status_code=503, content={"detail": str(exc)})

app.include_router(incidents_router)
app.include_router(providers_router)
app.include_router(repro_router)
app.include_router(investigate_router)
app.include_router(patches_router)
app.include_router(verify_router)
app.include_router(publish_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "buglens-ai-backend"}
