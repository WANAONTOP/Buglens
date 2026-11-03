# PLAN.md — BugLens AI milestones

Scope guard: one approved repository. Support overflow, clipped-navigation,
and blocked-click bugs before attempting cross-repository reasoning.

## M0 — Foundation (this step)

- Backend `/health` endpoint; frontend displays actual backend status.
- Local CORS pinned to the exact frontend origin.
- `.env.example`, `.gitignore`, `README.md`, `AGENTS.md`, `NEW_WORK.md`.

Acceptance checks:

- [x] `python -m pytest backend/tests` passes.
- [x] `GET /health` returns `{"status": "ok", ...}`.
- [x] Frontend at http://localhost:5173 shows "Backend connected".
- [x] Stopping the backend flips the frontend to "Backend unreachable".
- [x] No credentials in the repo; `.env` ignored.

## M1 — Evidence intake

- Upload screenshot / screen recording; store as run artifacts.
- Pin the target GitHub commit for the run.

Acceptance checks:

- [x] Upload round-trips and is listed under the run.
- [x] Run record stores the pinned commit SHA.
- [x] Missing upload or SHA marks the run BLOCKED, not failed-open.

## M1 fix — draft-first intake

- Capture/attach evidence on the new-report screen before saving.
- Drafts save with title/behaviors/steps only; repo/ref/viewport optional.
- Save creates the incident, then uploads pending evidence; failures carry
  over to the detail screen for retry without duplicate incidents.
- Missing repo/ref/commit/paths surface as investigation blockers and gate
  future investigation/patch execution via `guards.py`.

Acceptance checks:

- [x] Draft saves without repository details (backend test + live probe).
- [x] Screenshot attaches pre-save and uploads on save (live probe).
- [x] Failed upload preserves incident + file; retry creates no duplicates.
- [x] Refresh and backend restart reopen incident + uploaded evidence.
- [x] Incomplete drafts fail `require_investigation_ready` with 409 + list.
- [x] Backend suite and frontend build pass.

## Step 3 — provider configuration and connection tests

- Adapters for Meta, GitHub, Modal (+ optional Baseten) with sanitized
  readiness (`not_configured` / `configured_untested` / `verified` / `failed`).
- Bounded smoke tests with timeouts, retries, and redacted errors.

Acceptance checks:

- [x] Meta Spark + Wonder + SAM smoke verified live (2026-11-03).
- [x] SAM geometry parsed and checked against the owned image (exact box).
- [x] GitHub auth + `WANAONTOP/Buglens` read verified; commits pending (empty repo).
- [x] Modal sandbox smoke verified via local SDK profile (echo exit 0, terminated).
- [x] Baseten listing verified and labeled listing-only; inference untested.
- [x] Missing-config and provider-error tests pass; no repo code run, no PRs.

## Step 4 — committed reproduction target

- Owned `fixtures/storefront/` app: overlay-blocked `/checkout`, working
  `/checkout-control`, deterministic data, documented as a fixture.
- `.buglens.json` contract + backend validator; permitted paths confined
  to fixture source; manifest/harness/verifier protected.
- Playwright matrix: broken must `FAIL_BLOCKED` (occlusion evidence),
  control must `PASS`; setup failures report `FAIL_SETUP`.
- SAM smoke requires the expected red-square geometry (zero matches fails).
- Baseline committed locally; publication to `WANAONTOP/Buglens` is pending
  until a push is actually confirmed (no auto-push, no PRs).
- The overlay bug is not repaired in this milestone.

Acceptance checks:

- [x] Fixture runs locally; broken `FAIL_BLOCKED`, control `PASS` observed.
- [x] Negative probe: dead server reports `FAIL_SETUP`, not `FAIL_BLOCKED`.
- [x] Manifest validates; traversal/overlap/self-protection enforced.
- [x] Backend suite (93) and frontend build pass; SAM geometry test added.
- [x] Secret scan clean (6 known fake-fixture hits reviewed); initial commit
  created locally, push left to the user.

## Step 5 — Modal reproduction (M3)

- Resolve `main` to a pinned SHA; freeze + hash the validated contract.
- Modal sandbox runner (Linux Chromium): install, serve, drive both routes,
  pull evidence, classify `REPRODUCED` / `NEEDS_REPRODUCTION` / `BLOCKED`.
- Authenticated start/status/cancel/artifact endpoints; one background job
  at a time; frontend panel with live polling and downloads.

Acceptance checks:

- [x] Repro endpoints, job runner, and results panel implemented.
- [x] Frontend build (tsc + vite) passes with the new panel.
- [x] Backend suite passes including `backend/tests/test_repro.py` (105).
- [x] Live Modal run observed: `REPRODUCED` at the pinned SHA (recorded).
- [x] No status claims verified or fixed.

## Step 6 — Investigation and source mapping (M2, read-only)

- Evidence package (E-refs) gated on done/REPRODUCED + complete artifacts.
- Spark visual interpretation, SAM button segmentation, pinned-commit
  source mapping (exact/inferred/unknown), Wonder ranked hypotheses.
- No patch generation; no PATCHED/FIXED/VERIFIED status.

Acceptance checks:

- [x] Investigation endpoints, background job, and panel implemented.
- [x] Live investigation done: Spark/SAM/Wonder real calls recorded.
- [x] Overlay mapped exact to checkout.html:36; citations validated.
- [x] Backend suite (116) and frontend build pass.

## M2 — Visual interpretation

- muse-spark-1.3 describes the bug; sam-3.1 segments UI regions.
- Map regions to DOM nodes via Playwright snapshots.

Acceptance checks:

- [x] Interpretation stored per run with model name and version.
- [x] At least one DOM node linked to each reported region.
- [x] Unmapped regions reported explicitly, never silently dropped.

## M3 — Reproduction

- Playwright script reproduces overflow / clipped-nav / blocked-click.
- Reproduction test frozen before any patch attempt.

Acceptance checks:

- [ ] Repro fails on the pinned commit, passes-or-skips rationale recorded.
- [ ] Frozen repro hash stored; patch attempts cannot modify it.

## Step 7 — Scoped candidate generation + review (M4, no verification)

- One Wonder call per candidate from a completed investigation.
- Structural diff validation against the pinned baseline before proposal.
- Developer Approve/Reject bound to the exact diff hash.

Acceptance checks:

- [x] Candidate endpoints, background job, and review panel implemented.
- [x] Live candidate proposed: 1 in-scope file, validation passed.
- [x] Attempt budget (3) and approval hash binding enforced + tested.
- [x] Backend suite (124) and frontend build pass.
- [x] No approval fabricated; no verification or PR in this milestone.

## M4 — Patch candidates (muse-wonder-1.0)

- Up to three candidates; approved source files only.
- Candidates cannot touch protected tests or the verifier.

Acceptance checks:

- [x] Every candidate lists files changed; all within the approved set.
- [x] Attempt count enforced (max three) with time/cost budget logged.

## Step 8 — Seven-gate verification (M5, no PR)

- Approval-gated execution across four fresh Modal sandboxes.
- Trusted server-side gate decisions; persisted evidence + identity.
- Frontend verify action with per-gate results and comparisons.

Acceptance checks:

- [x] Verifier, gates, policy freeze, and panel implemented.
- [x] Mocked end-to-end run reaches VERIFIED; negatives covered.
- [x] Live start on the unapproved candidate refused (409, no record).
- [x] Backend suite (132) and frontend build pass.
- [x] Live Modal verification after developer approval: VERIFIED.
- [ ] PR creation (later milestone).

## M5 — Modal verification + PR

- Run frozen repro + patch inside Modal; open GitHub PR only on pass.
- Publish exactly the tested change; never auto-merge.

Acceptance checks:

- [x] Modal run log attached to the run record.
- [ ] PR created only after a passing verification; PR URL is the real API result.
- [ ] Merge left to a human reviewer.
