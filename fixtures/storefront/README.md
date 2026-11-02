# Storefront fixture (intentionally broken evaluation target)

This is a **synthetic evaluation fixture owned by BugLens AI**, not a real
customer incident. It exists so reproduction, patching, and verification can
be tested against a known bug with a known fix location.

The bug: on `/checkout`, a transparent promotional overlay
(`#promo-overlay`) covers the **Pay now** button and intercepts pointer
events, so checkout can never complete. `/checkout-control` is the identical
page without the overlay and checks out fine.

- App: dependency-free Node static server (`server.js`, stdlib only).
- Pages: `public/checkout.html` (broken), `public/checkout-control.html`
  (working control), `public/site.css` (the overlay rule is the bug site).
- Reproduction: `node reproduce.mjs` (reads the repo-root `.buglens.json`,
  starts the server, runs Playwright over both routes).
- Data: fixed order (`BugLens T-Shirt`, `#BL-0001`); no external services,
  no credentials, no randomness.

Expected result: broken route reports `FAIL_BLOCKED` (click intercepted,
button present), control route reports `PASS` (confirmation shown).
