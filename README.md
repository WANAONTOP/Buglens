# BugLens AI

Developers upload a screenshot or screen recording of a UI bug. BugLens
reproduces the bug against a pinned GitHub commit, connects visual and
browser evidence to source code, proposes a patch, verifies it inside Modal,
and creates a GitHub PR only after verification passes.

Built for the Meta hackathon.

## Prerequisites

- Node.js + npm
- Python 3.12+
- Git

## First-time setup (Windows, PowerShell)

```powershell
# From the repository root D:\buglens-ai

# Backend environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt

# Frontend dependencies
cd frontend
npm install
cd ..
```

## Application auth (required for incidents and evidence)

All `/api/*` endpoints require a bearer token. Generate one and store it in
`.env` (never commit `.env`):

```powershell
# From the repository root D:\buglens-ai
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
# Paste the output as BUGLENS_API_TOKEN in .env (minimum 16 characters)
```

The backend loads `.env` on startup. The frontend asks for the same token in
the "API token" card and keeps it in that tab's session storage only.

## Start the backend

```powershell
# From the repository root D:\buglens-ai
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Backend health check: http://127.0.0.1:8000/health

## Start the frontend

```powershell
# From the repository root D:\buglens-ai
cd frontend
npm run dev
```

Then open http://localhost:5173 in a browser. The page reports the live
backend connection status, then incident reporting once the API token is saved.

## Incident API (all authenticated except `/health`)

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Public liveness probe |
| POST | `/api/incidents` | Create an incident report (JSON) |
| GET | `/api/incidents` | List incidents, newest first |
| GET | `/api/incidents/{id}` | Fetch one incident with its evidence |
| PATCH | `/api/incidents/{id}/commit` | Pin the 40-hex target commit SHA |
| POST | `/api/incidents/{id}/evidence` | Upload screenshot / video / frame (multipart) |
| GET | `/api/incidents/{id}/evidence/{eid}/download` | Download stored evidence bytes |

An incident is `blocked` until a commit SHA is pinned AND at least one
evidence artifact is stored; then it becomes `intake_complete`. A saved
report is not a reproduced or verified bug.

Draft intake: only title, observed/expected behavior, and reproduction steps
are required. `repo`, `ref`, and `browser_viewport` are optional at creation
so a screenshot-only report saves without repository details. Missing repo,
ref, commit, or permitted source paths do not block capture or storage —
they are listed in `investigation_blockers` and must block investigation and
patch execution instead (see `backend/app/guards.py`). Repository details are
never invented.

Upload limits: images (PNG/JPEG/WebP) 10 MiB, video (MP4/WebM) 100 MiB,
20 evidence items and 250 MiB total per incident. Media is validated by
magic bytes; storage names are server-generated UUIDs.

## Storage and concurrency limits

Records live in `backend/data/incidents.json`, bytes in
`backend/data/artifacts/` (git-ignored; override with `BUGLENS_DATA_DIR`).
Writes are serialized in-process and applied atomically, but multi-worker
servers are NOT safe (last writer wins on the JSON record). Always run a
single worker (`--workers 1`, the uvicorn default). See the
`backend/app/store.py` docstring; M5 must replace this with a transactional
store.

## Manual checks: draft-first evidence intake

Backend and frontend running, token saved in the page:

1. On "Report a bug", choose "Attach screenshot" BEFORE saving and pick a
   PNG. It appears under "On this device — not uploaded yet" with a preview.
   Click the preview to mark the click point (red ring).
2. Fill in only title, observed/expected behavior, and steps — leave
   repository and ref blank. Press "Save report": the incident is created,
   the screenshot uploads, and the detail screen shows it under "Uploaded
   to backend" immediately.
3. The detail screen lists investigation blockers (missing repository, ref,
   commit, permitted paths). Confirm nothing invents those values.
4. Failure retry: attach a non-image file renamed to `.png` and save a new
   draft. The upload fails; the incident still opens with the file kept in
   the local list showing the error. Discard it and upload a real
   screenshot — no duplicate incident is created.
5. Press "Record screen", pick a window/screen in the browser picker, then
   Stop. Scrub the preview, press "Capture frame at current position",
   upload the frame.
6. Pin a 40-character commit SHA. Status flips to "Intake complete" (with
   evidence present).
7. Refresh the page: the same incident and its uploaded evidence reopen
   from the URL hash. Restart the backend and refresh again: same result.
   (Unsent local attachments cannot survive a refresh; uploaded evidence
   always does.)

## Providers (Step 3)

Separate backend adapters live in `backend/app/providers/` (Meta, GitHub,
Modal, Baseten-optional). Application auth (`BUGLENS_API_TOKEN`) stays
separate from provider credentials; keys live only in server-side
environment/`.env` and never appear in responses, logs, bundles, or prompts.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/api/providers/status` | Readiness per provider (no network traffic) |
| POST | `/api/providers/{name}/smoke` | Run the bounded smoke test for one provider |

Readiness states: `not_configured` (credentials missing), `configured_untested`
(key present, no passing smoke yet), `verified` (smoke passed), `failed`
(last smoke failed; detail says what was observed).

Setup per provider:

- Meta: `META_API_KEY` from dev.meta.ai → API keys. Smoke: one short
  `muse-spark-1.3` request, one short `muse-wonder-1.0` request, one
  `sam-3.1` segmentation over a generated owned test image whose returned
  boxes are parsed and checked against the image bounds.
- GitHub: `GITHUB_TOKEN` (fine-grained, repository read) plus `GITHUB_REPO`
  set to the approved `owner/name`. Smoke: `GET /user`, then read the repo.
- Modal: `python -m modal setup` in the venv (local SDK profile) or
  `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` in `.env`, plus `pip install modal`.
  Smoke creates a sandbox, checks an echo's exit code and marker output,
  terminates (cleanup failure never reports verified). No credentials enter
  the sandbox.
- Baseten (optional): `BASETEN_API_KEY`. Smoke lists models, which proves
  listing access only — inference capability stays untested and optional.

## Reproduction target (Step 4)

`fixtures/storefront/` is an owned, intentionally broken evaluation app —
not a customer incident. `/checkout` has a transparent overlay blocking
the Pay now button; `/checkout-control` is the working oracle.

```powershell
# From the repository root D:\buglens-ai
cd fixtures\storefront
npm install
$env:PLAYWRIGHT_CHANNEL = 'msedge'  # or install a Playwright browser instead
node reproduce.mjs
```

App URL during checks: http://127.0.0.1:4103/checkout (control:
`/checkout-control`). Expected matrix: broken route `FAIL_BLOCKED` with
occlusion evidence, control route `PASS` with `Order confirmed: #BL-0001`.
Server/element problems report `FAIL_SETUP`, never `FAIL_BLOCKED`.

The reproduction contract is the repo-root `.buglens.json` (validated by
`backend/app/manifest.py`; never executed by backend code — repository
commands will run only inside the Modal sandbox). The fixture runner is
separate from the BugLens app test suite: `pytest backend\tests` must stay
green regardless of the fixture's expected failure.

## Modal reproduction (Step 5)

Target: `WANAONTOP/Buglens`, branch `main`, app `fixtures/storefront/`,
manifest repo-root `.buglens.json`. Starting a reproduction resolves `main`
to its immutable commit SHA via GitHub, pins the SHA on the incident,
validates + freezes the contract (hash stored on the run), then executes it
as a background job inside a Modal sandbox (Linux Playwright Chromium —
never msedge/Windows paths). One run at a time; extra starts get HTTP 429.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/api/incidents/{id}/reproductions` | Freeze + start (202); pins SHA |
| GET | `/api/incidents/{id}/reproductions` | List runs for an incident |
| GET | `/api/reproductions/{run_id}` | Run state, verdict, events, artifacts |
| POST | `/api/reproductions/{run_id}/cancel` | Request cancellation |
| GET | `/api/reproductions/{run_id}/artifacts` | List fetched artifacts |
| GET | `/api/reproductions/{run_id}/artifacts/{name}/download` | Download one artifact |

Verdicts are evaluation-only: `REPRODUCED` (blocked click observed +
control confirmed), `NEEDS_REPRODUCTION` (failure not established), or
`BLOCKED` (setup/browser/evidence unavailable). No status claims the bug is
verified or fixed. Credentials never enter the sandbox; the incident detail
page shows runs, live status, events, and artifact downloads.

Status: complete — backend suite passes (105) and the first live Modal
run returned `REPRODUCED` at the published SHA (see NEW_WORK.md).

## Configuration

`.env` holds local secrets (see `.env.example`). Never commit it.

## Project layout

- `frontend/src/` — React + TypeScript + Vite app (incident form, evidence
  capture, screen recorder, Modal reproduction panel).
- `backend/app/` — FastAPI service (`/health`, `/api/*`).
- `backend/app/repro/` — published-source resolution, contract freeze,
  Modal runner, background jobs, run store.
- `backend/tests/` — backend test suite.
- `AGENTS.md` — binding engineering rules.
- `PLAN.md` — milestones and acceptance checks.
- `NEW_WORK.md` — record of existing foundations and new work.
- `fixtures/storefront/` — owned broken evaluation app + Playwright repro.
- `.buglens.json` — reproduction contract (validated, never auto-executed).
