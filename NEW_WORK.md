# NEW_WORK.md — foundations and event work log

## Existing foundations (before this step)

- None. `D:\buglens-ai` was inspected on 2026-11-02 and found empty.
- Toolchain present on the machine: Node v26.13.0, npm 11.20.0,
  Python 3.13.14, Git 2.55.0.windows.2.

## Step 1 — Foundation (2026-11-02)

New in this step:

- `backend/app/` — FastAPI service with `GET /health`; CORS allows exactly
  `http://localhost:5173` (see `backend/app/config.py`).
- `backend/tests/test_health.py` — health endpoint test.
- `frontend/` — React + TypeScript + Vite app; the page fetches
  `http://127.0.0.1:8000/health` and shows connected / unreachable status.
- `.env.example` — empty credential placeholders (Meta API, GitHub, Modal,
  Baseten) plus local URLs.
- `.gitignore` — protects `.env`, `.venv`, `node_modules`, and private
  runtime artifacts (uploads, recordings, logs).
- `AGENTS.md` — binding engineering rules (no fabricated results, BLOCKED
  on missing evidence, pinned commits, credential hygiene, no auto-merge).
- `README.md` — exact Windows startup commands.
- `PLAN.md` — M0–M5 milestones with acceptance checks.

Explicitly not started: model calls, patch generation, PR publishing,
simulated investigations.

Verification results (2026-11-02):

- `pytest backend\tests`: 1 passed.
- `npm run build` (tsc + vite): success.
- Live backend: `GET /health` returned
  `{"status":"ok","service":"buglens-ai-backend"}`.
- CORS preflight with `Origin: http://localhost:5173` returned
  `access-control-allow-origin: http://localhost:5173`.
- Live frontend dev server served the page containing the React root.
- Both servers stopped after verification; ports 8000/5173 free.
- In-browser rendering of the status page left for human confirmation.

## Step 2 — M1 evidence intake (2026-11-02)

New in this step:

- Backend: `app/models.py` (incident/evidence schemas, `blocked` /
  `intake_complete` derivation), `app/media.py` (magic-byte validation,
  dimension parsing, filename sanitizing), `app/store.py` (atomic JSON
  record file + UUID artifact bytes, concurrency limits documented),
  `app/auth.py` (bearer token, constant-time compare), `app/routes_incidents.py`
  (create/list/get/pin/upload/download). `main.py` loads `.env` and mounts
  `/api`; CORS now also allows PATCH.
- `backend/requirements.txt`: added `python-multipart`, `python-dotenv`.
- `backend/tests/test_incidents.py`: 48 tests (schemas, uploads, caps,
  auth, traversal, restart persistence).
- Frontend: `api.ts` (typed client, XHR upload with progress), `types.ts`,
  `IncidentForm.tsx`, `IncidentList.tsx`, `IncidentDetail.tsx`,
  `EvidenceCapture.tsx` (attach, getDisplayMedia recorder with timer,
  preview, click-to-mark, scrub + frame capture, local-vs-uploaded lists);
  `App.tsx` adds token card, hash-deep-link reopen, backend error display.
- `.env.example`: `BUGLENS_API_TOKEN`, optional `BUGLENS_DATA_DIR`.
- `.gitignore`: `backend/data/`.
- `README.md`: auth setup, API table, storage limits, manual check steps.

Explicitly not started: Meta/Baseten/GitHub/Modal calls, analysis, source
mappings, patches, verification gates.

Verification results (2026-11-02):

- `pytest backend\tests`: 49 passed (1 health + 48 intake).
- `npm run build` (tsc + vite): success.
- Live probe against an isolated data dir (deleted after): unauthenticated
  `/api` → 401; create → `blocked`; PNG upload → 201 with server-parsed
  1×1 dims; pin SHA → `intake_complete`; download bytes identical
  (SHA-256 match); after a real backend restart the incident + evidence
  persisted; frontend dev server served the app. Ports free afterwards.
- Manual browser checks NOT run here (no browser automation in this
  environment): screenshot attach + click mark, screen recording allow/deny,
  scrub + frame capture, refresh-reopen. Steps documented in README.md.

## Step 2 fix — draft-first intake (2026-11-03)

Problem: the report form demanded repository + branch before any evidence
could be attached, so screenshot-only intake was impossible.

Changes:

- Backend: `repo`, `ref`, `browser_viewport` optional in `IncidentCreate`
  and `Incident` (blanks normalize to missing; present values still
  validated; old records with repo/ref read unchanged). New computed
  `investigation_blockers` plus `app/guards.py::require_investigation_ready`
  (409 + missing list) which M3+ execution must call. Nothing invents
  repository details.
- Frontend: `EvidenceCapture` gained pending mode (`incidentId: null`,
  shared `uploadLocalItem`, `initialItems`, `onPendingChange`) and is now
  rendered on the new-report screen. New `NewReportForm.tsx` (draft fields
  + save → create → upload-each flow, failures carried to detail for
  retry); `IncidentForm.tsx` removed. Detail shows investigation blockers;
  capture panel remounts per incident; pinned-commit label restructured
  with title/hint spans to fix wrapping. Styling otherwise untouched.
- `README.md` (draft semantics, rewritten manual checks), `PLAN.md` (M1-fix
  checklist).

Verification results (2026-11-03):

- `pytest backend\tests`: 53 passed (49 existing + 4 draft/gate tests).
- `npm run build` (tsc + vite): success.
- Live probe (isolated data dir, deleted after): minimal draft → 201 with
  4 investigation blockers; garbage upload → 415 with evidence count held
  at 0; retry → 201, count 1, exactly 1 incident; after a real restart the
  draft + evidence + blockers persisted; frontend served; no leftover
  listeners. Two stale probe servers found squatting on ports 8000/5173
  from an aborted run were verified by command line and stopped.
- Manual browser checks NOT run here (no browser automation): pre-save
  attach + preview, draft save with upload, failure carryover + retry,
  refresh/restart reopen. Steps in README.md.

## Step 3 — provider configuration and connection tests (2026-11-03)

Schemas verified against official docs before implementation: Meta Model
API (`https://api.meta.ai/v1`, Bearer auth, `POST /v1/responses` for
muse-spark-1.3, muse-wonder-1.0, and sam-3.1 with `input_text` + `input_image`
parts; dev.meta.ai docs), GitHub REST (`GET /user`, `GET /repos/{o}/{r}`),
Modal SDK (`Sandbox.create`/`terminate`), Baseten OpenAI-compatible
(`https://inference.baseten.co/v1`, docs.baseten.co).

Changes:

- New `backend/app/providers/`: `base.py` (states, sanitizer, bounded HTTP
  with timeout + retries), `meta.py`, `github.py`, `modal.py` (lazy SDK
  import, echo-only sandbox, credentials never enter it), `baseten.py`,
  `registry.py` (in-memory verified/failed outcomes), plus
  `routes_providers.py` (`GET /api/providers/status`,
  `POST /api/providers/{name}/smoke`, all app-authenticated).
- `config.py`: provider env getters and bounds; `.env.example`: commented
  `META_API_BASE_URL` pin (placeholders only).
- `backend/tests/test_providers.py`: 18 mocked tests (missing config,
  401/timeout/incomplete/429-then-ok, traversal-free sanitizer, Modal
  setup/missing-package/fake-SDK with credential isolation).

Verification results (2026-11-03):

- `pytest backend\tests`: 71 passed. `npm run build`: success (no UI changes).
- Live bounded smokes via the real adapters: Meta spark/wonder/sam all
  `verified` (~1.1–1.6s each; SAM returned 753 output chars on the owned
  generated test image); GitHub auth `verified`, repo `not_configured`
  (`GITHUB_REPO` unset — no value invented); Modal `not_configured`
  (no tokens — setup reported, no sandbox run); Baseten `verified`
  (21 models listed, zero inference spend). No repo code run, no PRs.
- Follow-up read-only check against `WANONTOP/Buglens` (repo passed as a
  parameter; `.env` untouched): auth `verified`, repo read `failed` with
  HTTP 404, commit check blocked on readability. The owner spelling was
  corrected to `WANAONTOP/Buglens` (see Step 3 corrections).

## Step 3 corrections (2026-11-03)

- GitHub: `GITHUB_REPO=WANAONTOP/Buglens` set in `.env` (blind edit,
  verified by exact-match boolean only). Live read-only checks: auth +
  repo read `verified` (default branch `main`); commits endpoint returned
  409, so commit verification is `pending` (repository has no commits).
  No commits, branches, or PRs created. Target stays configurable via env.
- Modal: adapter now accepts the SDK local profile (`~/.modal.toml`,
  presence-only check) as well as env tokens. SDK usage re-verified
  against modal.com docs and fixed: `App.lookup(..., create_if_missing=True)`
  (bare `App("name")` is rejected uninitialized), variadic
  `exec("echo", ..., timeout=...)`, `proc.wait()` exit code plus stdout
  marker check, `terminate()` whose failure forces `failed`. Timeout bound
  moved to `config.MODAL_SMOKE_TIMEOUT_S` with non-waiting pool shutdown
  so the call itself always returns within the limit. Live smoke:
  `verified` in ~7s via the local profile (create → echo exit 0 →
  terminated).
- SAM: `parse_sam_output` implements the documented output grammar
  (`<Nf>`, `id+box+mask` records, inclusive pixels) and validates every box
  against the owned 96x64 test image; zero matches stays a normal pass.
  Live re-smoke: 1 match, box 24,16-71,47 — exactly the generated square.
- Baseten: check renamed to `listing`; reports state listing access
  verified while inference capability stays untested (optional).
- `pytest backend\tests`: 86 passed. `npm run build`: success. UI unchanged.

## Step 4 — committed reproduction target (2026-11-03)

Baseline status: committed locally as `817a897e`; publication to
`WANAONTOP/Buglens` is pending until a push is actually confirmed. Local
reproduction succeeded as an evaluation: broken route `FAIL_BLOCKED`,
control route `PASS`. The overlay bug has not been repaired.

Changes:

- `fixtures/storefront/`: dependency-free Node server, `checkout.html`
  (transparent `#promo-overlay` blocks `#pay-button`), `checkout-control.html`
  (no overlay), `site.css` (annotated bug site), `reproduce.mjs`
  (Playwright matrix with PASS/FAIL_BLOCKED/FAIL_SETUP verdicts and
  `elementFromPoint` occlusion evidence), fixture README. Playwright 1.64.1
  pinned; run here via Edge channel (`PLAYWRIGHT_CHANNEL=msedge`).
- `.buglens.json`: workdir, install/start (opaque, never backend-executed),
  port 4103, both routes, viewport, actions, confirmation, required
  elements, permitted `public/` only, protected harness/manifest/verifier.
- `backend/app/manifest.py` + `backend/tests/test_manifest.py`: schema,
  traversal/overlap/self-protection enforcement, repo-consistency check.
- SAM correction: parser still accepts empty output, but the smoke over
  the known red square now fails without expected geometry (new test).
- Git: repo initialized on `main`, remote `origin` set to
  `https://github.com/WANAONTOP/Buglens.git` (none existed), 58 reviewed
  files staged explicitly, secret scan clean (6 hits, all known fake test
  constants), initial commit `817a897e` created locally. NOT pushed.

Verification results (2026-11-03):

- `node reproduce.mjs`: broken `FAIL_BLOCKED` (top element
  `div#promo-overlay` at button center), control `PASS`
  (`Order confirmed: #BL-0001`), matrix exit 0.
- Negative probe (port squatted by a stub): `FAIL_SETUP` with a clear
  reason, matrix exit 1 — never mistaken for the blocked-click failure.
- `pytest backend\tests`: 93 passed. `npm run build`: success.
- Follow-up: Step 4 documentation committed separately from the initial
  source commit, which is preserved as-is (no amend, no force-push).

## Later steps

Append one section per milestone with files touched and verification results.
