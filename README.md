# BugLens AI

Developers upload a screenshot or screen recording of a UI bug. BugLens
reproduces the bug against a pinned GitHub commit, connects visual and
browser evidence to source code, proposes a patch, verifies it inside Modal,
and creates a GitHub PR only after verification passes.

Built for the Meta hackathon.

**Links:** live website
<https://frontend-jade-rho-52.vercel.app> ·
submission walkthrough
[SUBMISSION.md](SUBMISSION.md) ·
current implementation branch
<https://github.com/WANAONTOP/Buglens/tree/step12/deploy-prep>.
Review that branch, not `main`: `main` remains the pinned
evaluation baseline (`9174a46…`), while all application work
continues on `step12/deploy-prep`.

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
the "Connect to workspace" dialog (auto-opened when signed out) and keeps it
in that tab's session storage only; Settings holds sign-out and diagnostics.

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

## Investigation (Step 6, read-only — no patches)

An investigation runs on a `done` / `REPRODUCED` reproduction whose SHA
matches the incident pin and whose artifacts are complete; anything else
is reported BLOCKED. It builds a stable evidence package (E-numbers over
uploaded, baseline, broken-route, and control-route items), calls
`muse-spark-1.3` on the broken/control frames, segments the concrete Pay
button with `sam-3.1` (the transparent overlay is established by DOM and
hit-testing, never by vision), maps DOM identities to source at the
pinned commit (exact / inferred / unknown), and asks `muse-wonder-1.0`
for ranked root-cause hypotheses with evidence refs and validated
citations. One background job at a time; results persist across
refresh/restart and appear in the incident detail page.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/api/incidents/{id}/investigations` | Start on a run (202; body `{"run_id"}`) |
| GET | `/api/incidents/{id}/investigations` | List investigations for an incident |
| GET | `/api/investigations/{inv_id}` | Full record: evidence, models, mappings |
| POST | `/api/investigations/{inv_id}/cancel` | Request cancellation |

Live result on run `ae35c096…`: Spark observed the enabled button with no
visible cover; SAM boxed the button at (360,189)-(465,233), matching the
DOM rect; `div#promo-overlay` mapped exact to
`fixtures/storefront/public/checkout.html:36`; Wonder's top hypothesis
names the transparent full-viewport interceptor with 11 supporting
evidence refs and zero unvalidated citations. No status claims PATCHED,
FIXED, or VERIFIED.

## Candidate patches (Step 7 — generation + review, no verification)

A candidate is generated from a completed, parsed investigation: the
incident/run/investigation chain must agree on the SHA and contract,
sources are fetched at the pinned commit, and `muse-wonder-1.0` returns
one minimal unified diff with explanations, evidence refs, expected
effect, and regression risks. Every diff is validated structurally
against the pinned baseline (canonical paths, manifest scope, protected
exclusions, no renames/deletes/mode/binary changes, size bounds, strict
in-memory apply) before it can be proposed. A proposed patch is not a
verified fix. At most three candidates per incident.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/api/incidents/{id}/candidates` | Generate (202; body `{"investigation_id", "hypothesis_rank?"}`) |
| GET | `/api/incidents/{id}/candidates` | List candidates for an incident |
| GET | `/api/candidates/{candidate_id}` | Full record: diff, validation, provenance |
| POST | `/api/candidates/{candidate_id}/approve` | Approve, bound to the exact diff hash |
| POST | `/api/candidates/{candidate_id}/reject` | Reject with a reason |
| POST | `/api/candidates/{candidate_id}/cancel` | Cancel a running generation |

States are `PROPOSED`, `APPROVED`, `REJECTED`, or `BLOCKED`. Approval
binds the candidate ID to the diff SHA-256 and re-verifies the binding;
any change to the diff invalidates it. Approval authorizes a later
verification run; it is not verification.

Live result on investigation `bbe7be7e…`: candidate `ecd72616…`
`PROPOSED` — one file, `fixtures/storefront/public/checkout.html`,
adding `style="pointer-events: none;"` to the overlay div so clicks
reach the Pay button; 6 evidence refs, all 5 validation checks pass.
Developer approval is pending.

## Seven-gate verification (Step 8 — no PR publication)

Verification runs only on a validly approved candidate: the worker
re-checks state, approval record, diff hash, baseline pin, and contract
agreement, and refuses stale or changed content. Each run freezes a
hashed policy (viewport, pixel tolerances, required elements, expected
confirmation, Gate 4 suite command), then drives the frozen reproduction
on baseline and candidate checkouts in fresh node sandboxes and the
pinned repo suite on both checkouts in fresh python sandboxes — four
sandboxes, no credentials inside, working tree untouched. All seven
gates must PASS for a `VERIFIED` outcome (limited to the tested scope);
results persist with screenshots, traces, logs, and tested code hashes.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/api/candidates/{id}/verifications` | Start (202; approved only) |
| GET | `/api/candidates/{id}/verifications` | List runs for a candidate |
| GET | `/api/verifications/{vid}` | Full record: gates, outcome, provenance |
| POST | `/api/verifications/{vid}/cancel` | Request cancellation |
| GET | `/api/verifications/{vid}/artifacts/{name}/download` | Download evidence |

The seven gates: reproduction, target correctness, layout/interaction,
repo tests (same pinned suite both checkouts, no new failures),
runtime errors, protected UI (before-click render unchanged — correct
for an invisible repair), scope/integrity (tested bytes equal the
approved diff). Status: live verification `1acefb6a…` of approved candidate
`ecd72616…` returned `VERIFIED` — all seven gates passed on fresh
Modal sandboxes (see NEW_WORK.md). No PR publication in this milestone.

## Verified PR publication (Step 9 — explicit Publish only, never auto-merge)

The publisher files a GitHub PR for a completed live `VERIFIED` run
and nothing else. Before any write it re-runs a server-side preflight
entirely from stored records plus live GitHub reads: completed live
verification with 7/7 PASS over verified artifact bytes and four
sandbox-terminated markers, valid approval binding, candidate /
baseline / contract / policy / tested-byte / diff-hash agreement,
approved repository, not superseded or cancelled, recomputed change
bytes equal to the tested bytes, remote base branch still at the
verified baseline, and repair-branch state. A moved base stops
publication rather than transplanting the verification.

The repair commit is built from the pinned baseline plus the exact
recomputed bytes (`buglens/fix-<candidate8>`); publish verifies an
existing branch before reuse (same commit, or an equivalent commit
with identical tree/parent/message) and never force-pushes. Publication
is idempotent per candidate + verification, serialized to one worker,
and orphaned intents recover from live GitHub state. The PR body is a
sanitized evidence summary (baseline, verification ID, seven gates,
suite counts, viewport, limitations, fixture notice) with no
credentials, traces, or localhost links — and no GitHub CI claim.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/api/publications/preview` | Read-only preview + full preflight (body `{"candidate_id", "verification_id", "pr_title?"}`) |
| POST | `/api/publications` | Publish the exact preview (explicit Publish; 201; 409 on refusal) |
| GET | `/api/candidates/{id}/publications` | List publication records |
| GET | `/api/publications/{pid}` | Full record: real PR URL only after GitHub confirms |

In the UI, open the candidate, pick the verified run, and choose
"Load preview & preflight". "Publish this exact preview to GitHub"
stays disabled until every server check passes, and asks for
confirmation before writing. Status: authorized single PR created
and read-back verified — PR #1,
https://github.com/WANAONTOP/Buglens/pull/1
("Fix checkout overlay intercepting Pay now clicks"), head
`b59b1d7d…` on `buglens/fix-ecd72616`, exactly the tested one-line
change; open and unmerged, merge left to a human. This is an owned
evaluation-fixture repair verified within the tested scope; checks
ran in Modal, not GitHub CI (see NEW_WORK.md).

## Hackathon submission

Start here: [SUBMISSION.md](SUBMISSION.md) — live links, demo
walkthrough, architecture, event-vs-foundation scope, and honest
limitations. The submitted source is branch `step12/deploy-prep`,
not `main`.

## Modal deployment (Steps 12-13 — LIVE single-developer demo)

The backend is deployed to Modal and the production frontend is
hosted on Vercel (bounded live smoke below). This remains a single-developer authenticated demo: one shared
`BUGLENS_API_TOKEN` (typed in by the user, kept in tab session
storage, never in the bundle), one API container, CORS pinned to the
exact production origin (no wildcard). Local uvicorn
development is unchanged and remains the default; deploy behavior
activates only when `BUGLENS_MODAL=1` and `BUGLENS_JOBS=modal` are
both set (the Modal app sets both remotely).

Live backend URL (public endpoint; `/api/*` still requires the
Bearer token):

```text
https://gbishwakarma18--buglens-ai-api.us-east.modal.run
```

Live website (Step 13):

```text
https://frontend-jade-rho-52.vercel.app
```

Sign-in: open the website — the "Connect to workspace" dialog opens
automatically when signed out. Paste the `BUGLENS_API_TOKEN` value
from your local `.env` into "Access token" and choose "Connect". The
token is validated against the backend and kept only in that tab's
session storage; Settings holds sign-out, token change, and backend
diagnostics. Invalid tokens reopen the dialog. Never put tokens in
`VITE_*` variables.

The production bundle sets `VITE_BUGLENS_API_URL` to the backend
URL at build time and refuses to load without it (no localhost
fallback in production). Hosted browser checks (15/15 scripted,
headless Edge): load, refresh, sign-in, remote smoke incident +
`REPRODUCED` run + artifacts, deep-link survival, screenshot upload
with byte-identical retrieval, real browser-encoded WebM upload
with preview and frame capture, logout gating. Real Record Screen
interaction (OS picker → Stop → playback → marking) still needs a
headed browser + human and is pending; only cosmetic issue found is
a missing `/favicon.ico` (404). Remote Meta/Baseten credentials are
not configured: `/api/providers/status` reports `not_configured`
with setup guidance and no secret values; no model, verification,
or publication calls ran in this step.

UI redeploy (commit `e921b9d`, Vercel deployment
`frontend-6s29z15t6`, same production hostname): redesigned
Reports/Investigation workspace with per-item upload states
(Preparing → Ready → Uploading → Uploaded/failed), upload timeout +
cancel, finalize indicator, wizard double-save guard. Deployed via a
fresh remote build with `--build-env VITE_BUGLENS_API_URL=<modal
backend>` (no project env changes; `vercel build` is unusable on this
machine — EPERM symlinking its builder cache). Live bundle verified:
Modal URL baked, zero secret values/names. Hosted checks passed:
sign-in incl. wrong-token 401 handling, existing Step 14 demo (7/7
gates, PR #2 link), one "Hosted UI test" incident with screenshot
upload + refresh persistence, desktop/mobile overflow, settings,
navigation. No paid jobs, approvals, PRs, or backend redeploy.
Screen-recording acceptance passed locally and on hosted (observed
9.0s take → 201 → byte-exact retrieval → refresh persistence; an
earlier instant-stop take produced a 110B file the UI correctly
flags as needing attention).

What exists: `backend/modal_app.py` (Modal App `buglens-ai`: one
`max_containers=1` ASGI function plus `run_repro_worker`,
`run_investigate_worker`, `run_candidate_worker`,
`run_verify_worker`, all sharing Volume `buglens-data` at `/data`
and Secret `buglens-secrets`); `backend/app/deploy.py` (flags,
volume sync, remote spawn/cancel, capacity counts, incidents-registry
optimistic-concurrency guard → HTTP 409); `backend/app/workers.py`
(guarded remote entry points, crash marking, no auto-retry);
per-request volume reload/commit middleware; `worker_call_id` on job
records; `BUGLENS_CORS_ORIGINS` allowlist; frontend
`VITE_BUGLENS_API_URL` (see `frontend/.env.example`).

The shared Volume is not a transactional database. Write safety
comes from ownership and lifecycle ordering (Step 12 review): the
API runs as a single container so its in-process locks serialize
API-side writes; `incidents.json` is API-write-only (workers only
read it; the 409 OCC guard stays as a tripwire); each job record is
phase-owned (API while queued, the one spawned worker while active,
terminal states immutable); the worker writes nothing until the
API's spawn commit is visible; cancel kills (with container
termination) and confirms death before marking; the stale reconcile
marks a record lost only when its worker call is confirmed finished
or was never recorded. Shared-token `BUGLENS_API_TOKEN` auth is a
single-developer demo limitation. Sandboxes still receive no
credentials.

Modal Secret `buglens-secrets` holds exactly these names (values
never in the repo, logs, or images): `BUGLENS_API_TOKEN`,
`GITHUB_TOKEN`, `GITHUB_REPO`, `MODAL_TOKEN_ID`,
`MODAL_TOKEN_SECRET`, `BUGLENS_CORS_ORIGINS` (exactly
`https://frontend-jade-rho-52.vercel.app`, no wildcard).
`META_API_KEY` and `BASETEN_API_KEY` are present locally but were
deliberately left out of the secret — model workers are unverified
remotely, and no model call has run against the deployment.

Exact PowerShell deployment commands (Modal SDK 1.6.3, from the repo
root, using the project environment). Create `.env.modal` (gitignored
via `.env.*`) holding the secret names above, then delete it after:

```powershell
.venv\Scripts\modal.exe volume create buglens-data
.venv\Scripts\modal.exe secret create buglens-secrets --from-dotenv .env.modal
Remove-Item .env.modal
.venv\Scripts\modal.exe deploy backend/modal_app.py
```

Then point a hosted frontend at the deployed API URL with
`VITE_BUGLENS_API_URL` (no trailing slash) and rebuild.

Bounded live smoke procedure (Step 12 — RAN, all passed):
1. `GET /health` on the deployed URL returns ok.
2. Missing/wrong auth rejected (401); preflight echoes the allowed
   origin and not an evil one.
3. Create an incident (authenticated), upload a small screenshot,
   confirm the recorded hash matches the bytes.
4. Start one reproduction (202 in seconds); an immediate duplicate
   POST is rejected (429) with no second worker.
5. Poll to terminal; download one artifact and verify its hash.
6. Redeploy the API (new container); confirm the incident, evidence,
   completed worker results, and byte-identical artifacts persist.

Remaining blockers: remote investigate/candidate/verify workers
and GitHub publication from the deployment are unverified (one
reproduction only, no model calls yet); 30-minute verifications vs
the 2400s worker timeout need a live run; multi-replica API would
break in-process locks (held at `max_containers=1` by design); real
Record Screen interaction is pending-manual; `/favicon.ico` 404s
(cosmetic).

## Configuration

`.env` holds local secrets (see `.env.example`). Never commit it.

## Project layout

- `frontend/src/` — React + TypeScript + Vite app (incident form, evidence
  capture, screen recorder, Modal reproduction panel).
- `backend/app/` — FastAPI service (`/health`, `/api/*`).
- `backend/app/repro/` — published-source resolution, contract freeze,
  Modal runner, background jobs, run store.
- `backend/modal_app.py` — Modal deployment entry (ASGI + 4 workers;
  local dev stays on uvicorn).
- `backend/tests/` — backend test suite.
- `AGENTS.md` — binding engineering rules.
- `PLAN.md` — milestones and acceptance checks.
- `NEW_WORK.md` — record of existing foundations and new work.
- `fixtures/storefront/` — owned broken evaluation app + Playwright repro.
- `.buglens.json` — reproduction contract (validated, never auto-executed).
