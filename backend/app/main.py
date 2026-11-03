from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import FRONTEND_ORIGIN
from .routes_incidents import router as incidents_router
from .routes_investigate import router as investigate_router
from .routes_providers import router as providers_router
from .routes_repro import router as repro_router

# Local development: read backend-adjacent .env (repo-root .env when started
# from the root). Real environment variables always take precedence, which is
# what the test suite relies on.
load_dotenv()

app = FastAPI(title="BugLens AI", version="0.1.0")

# Local development CORS: allow exactly the Vite frontend origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

app.include_router(incidents_router)
app.include_router(providers_router)
app.include_router(repro_router)
app.include_router(investigate_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "buglens-ai-backend"}
