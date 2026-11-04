# BugLens AI — Hackathon Submission

**What it is:** developers upload a screenshot of a UI bug; BugLens
reproduces it against a pinned commit, connects visual/browser
evidence to source, proposes a patch, verifies it in Modal, and
creates a GitHub PR only after seven evidence-based gates pass.

## Live links

- Website (hosted frontend):
  <https://frontend-jade-rho-52.vercel.app>
- API (hosted Modal backend):
  <https://gbishwakarma18--buglens-ai-api.us-east.modal.run>
- Source (this submission — review this branch, **not** `main`):
  <https://github.com/WANAONTOP/Buglens/tree/step12/deploy-prep>
- This file:
  <https://github.com/WANAONTOP/Buglens/blob/step12/deploy-prep/SUBMISSION.md>
- Repair PRs (open, unmerged — alternatives, see below):
  [#1 — pointer-events fix](https://github.com/WANAONTOP/Buglens/pull/1),
  [#2 — remove overlay](https://github.com/WANAONTOP/Buglens/pull/2)

Interactive access needs a separately supplied demo API token:
the hosted URL alone does not grant authenticated access. When
signed out, the site opens a "Connect to workspace" dialog: paste
the token into "Access token" and choose "Connect". The token is
validated, then kept only in that tab's session storage; Settings
holds sign-out, token change, and backend diagnostics. The token
is not published in this repository.

## Demo walkthrough (hosted incident)

Direct link (token required):
<https://frontend-jade-rho-52.vercel.app/#incident=54ce59632c254778bbb8fb3b2f8a8c79>

1. **Sign in** with the demo token (see above).
2. **Incident** `54ce…8c79`: "Checkout blocked by overlay" on
   fixture `fixtures/storefront/`, pinned baseline `9174a460…`.
   The incident detail shows the uploaded developer screenshot,
   reproduction steps, and permitted paths.
3. **Reproduction run** `8ebd…` (tab/panel "Reproduction"):
   REPRODUCED — broken route blocks the Pay button click (overlay
   hit-test evidence), control route passes; before-screenshots,
   DOM geometry, console output, and the contract hash are listed
   with downloadable artifacts.
4. **Investigation** `b1af…`: Spark's visual observations of the
   selected frames, SAM segmentation (with the explicit caveat
   that an invisible overlay is established by DOM/hit-test
   evidence, not pixels), DOM→source mapping, and Wonder's ranked
   root-cause hypotheses with evidence IDs.
5. **Candidate** `b7ea…` ("Candidates"): the proposed diff —
   **remove the single `<div id="promo-overlay">` line** from
   `fixtures/storefront/public/checkout.html` — with validation
   outcome, provenance (baseline, contract, diff hash), and the
   review Approve/Reject actions.
6. **Verification** `8830…` ("Verification"): all seven gates PASS
   (see Gate results below), with baseline-vs-candidate evidence
   and tested code identity.
7. **Publication**: the persisted record shows PR #2's URL;
   opening it shows the branch, tested commit, and PR body.

## Architecture and model roles

- **Frontend** (React + TypeScript + Vite, hosted on Vercel):
  incident intake, evidence capture/playback, and review of
  reproduction, investigation, candidate, verification, and
  publication state. Fail-closed production bundle: no localhost
  fallback; API base comes from `VITE_BUGLENS_API_URL`.
- **Backend** (FastAPI, deployed as one Modal app): REST API plus
  Modal worker functions for long-running jobs
  (`Function.spawn`, cancel, timeouts). Single-container API with
  phase-owned records on a Modal Volume; cross-container safety
  via reload-first reads and terminal-state refusal rather than
  locks, documented as a single-developer demo design.
- **muse-spark-1.3** (visual interpretation): structured
  observations of selected reproduction frames, separated from
  causal hypotheses, with evidence references.
- **sam-3.1** (segmentation): geometric masks for visible
  objects/points, validated against original image dimensions;
  never treated as proof a transparent overlay exists or is fixed.
- **muse-wonder-1.0** (reasoning + patches): root-cause
  hypotheses from the evidence package and pinned source, then
  one minimal diff per candidate. Repository text and model
  output are untrusted data — they never widen allowed paths or
  touch protected files.
- **Modal**: hosts the API, runs worker jobs, and provides
  isolated Linux/Playwright sandboxes for reproduction and
  verification. Credentials (Modal Secrets) never enter
  target-code sandboxes.

Gate results (verification `8830…`, candidate `b7ea…`):
Reproduction PASS · Target correctness PASS · Layout/interaction
PASS · Existing tests PASS (91 passed, 2 identical pre-existing
failures, 0 new — see Limitations) · Runtime errors PASS ·
Protected UI PASS · Scope/integrity PASS.

## The two repair PRs (alternatives, neither merged)

Both repair the **same intentional fixture bug** (transparent
overlay over Pay now) with **different** patches:

- **PR #1**: adds `style="pointer-events: none;"` to the overlay
  (keeps the element, makes clicks pass through).
- **PR #2**: removes the overlay element entirely.

Each was independently verified (different verification runs,
frozen to its own patch) and each PR discloses its tested scope.
Neither is merged; PR #2 must not be read as superseding PR #1.

## Event work vs dependencies

Everything in this repository was built during this project:
the git history on `step12/deploy-prep` runs from the initial
source commit through intake, providers, fixture, reproduction,
investigation, candidate, verification, PR #1, Modal deployment,
hosted frontend, Meta configuration, the full hosted
screenshot→reproduction→investigation→candidate→verification→PR
workflow, publication fixes, this submission document, and the
UI/upload release (see `NEW_WORK.md` for the dated per-step
log; commits are dated 2026-11-03/04). Earlier milestones are
part of the same build, not prior work. Only third-party
services (Meta models, Modal, GitHub, Vercel) are dependencies,
not our work.

## Limitations (read before judging)

- **Intentional fixture**: the repaired bug is deliberately
  planted in `fixtures/storefront/` for evaluation — not a real
  customer incident. Claims cover one approved repository and
  the overlay/blocked-click class, not arbitrary repositories.
- **Tested viewport**: verification assertions ran at the
  fixture's declared viewport, 1280×800 (see `.buglens.json`);
  other sizes/layouts are untested.
- **Two baseline failures**: the fixture's own suite has 2
  pre-existing failures, identical at baseline and candidate,
  with zero new failures. They are disclosed in both PR bodies;
  checks ran in Modal, never GitHub CI.
- **Screenshot contribution**: the uploaded demo screenshot is
  stored and included in the evidence package, but the diagnosis
  was grounded in reproduction frames and DOM/hit-test evidence
  — the upload did not directly drive the root cause.
- **Access**: single shared demo token, entered manually;
  multi-user auth/roles are out of scope.
- **Screen recording**: passed with a real OS-picker recording on
  the hosted site — a 9.036-second, 1246×800 WebM with successful
  upload, byte-exact retrieval, and persistence after refresh
  (an earlier instant-stop take produced a 110-byte file the UI
  correctly flags as needing attention).
- Baseten was implemented as optional and was not needed;
  the live demo used Spark, SAM, and Wonder only.
