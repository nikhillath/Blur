# Testing

Last full run (Windows 11, Python 3.12, Node 24, PostgreSQL 17, Microsoft Edge for browser tests):

| Suite | Command | Result |
|---|---|---|
| Backend (pytest) | `cd backend && .venv\Scripts\python -m pytest` | **366 passed** (about 2.5 min) |
| Frontend unit (vitest) | `cd frontend && npm test` | **19 passed** |
| Browser end-to-end (Playwright + axe) | `cd frontend && npx playwright test` | **12 passed** (about 1.2 min) |
| Type check / lint / build | `npx tsc -b`, `npm run lint`, `npm run build` | clean |
| Dependency audit | `pip-audit -r requirements.txt`, `npm audit` | 0 known vulnerabilities |

## Isolation
* Backend tests refuse to run unless `TEST_DATABASE_URL` points to a database whose name ends in `_test`. The suite
  migrates it from scratch (down to base, then up to head) at the start, so migrations are exercised every run, and
  truncates every table after each test. Files go to a temporary directory.
* Browser tests start their own backend and Vite server, use the `_test` database and `storage-e2e/`, and disable the
  per-IP rate limit (it is covered by backend tests). They never touch development data.
* Tests use fictional data only (`tests/fixtures/`, regenerate with `python -m scripts.make_fixtures`).

## What is covered
* **Backend:** auth and sessions (hashing, rotation, replay detection, logout, disabled users, forged tokens), role-based
  access, database constraints, upload validation and storage failure handling, text extraction for TXT/PDF/DOCX/images
  including real OCR, rotated and scanned PDFs, 16 detection rules with positive/negative/noisy cases, the Transformer
  decoder (tiny model + the real fine-tuned model), conflict resolution, review decisions (history, bulk, manual spans,
  concurrency), redaction for every format with output verification (including a deliberately broken engine), dashboard.
* **ML library:** label mapping, normalisation, BIO alignment round trip, metrics, split/leakage logic.
* **Security (`tests/test_security.py`):** every route requires a token (enumerated from the OpenAPI schema, so new
  routes are covered automatically); a cross-user matrix calls every ID-taking route as three other users and requires
  404/403 with the victim's data unchanged; admin-only routes; injection-style input; error sanitisation; security headers;
  CORS; cookie flags; cross-origin cookie requests; weak-secret refusal; rate limits; no secrets in logs.
* **Frontend unit:** API client (refresh and retry, error text), forms, route guards, dashboard values from the API,
  safe rendering of hostile document text, review actions and confirmation.
* **Browser end-to-end:** register, upload, scan, review, redact, download and check the file contents; session reload;
  PDF flow; declined confirmation; cross-user access; reviewer assignment; admin; bad upload and failed scan recovery;
  empty review state; axe accessibility scan (WCAG A/AA, serious and critical) on the main pages; keyboard-only login;
  no horizontal scroll at 375 px.

## Bugs found by testing in this phase (all fixed)
1. A NUL byte in the document search box caused a 500; now a clean 422 (and the search strips it).
2. Validation errors echoed the submitted value (for example a rejected password); responses no longer include it.
3. The UI showed raw JSON for validation errors; it now shows readable text.
4. Links inside body text relied on colour alone (axe, `link-in-text-block`); now underlined.
5. Tables overflowed horizontally on a phone-width screen; cards now scroll them.
6. Logging out landed on the login page instead of the landing page.
7. Earlier phases: session tokens were bound to a rotating row (broke other tabs); decision history order could disagree with
   the final status under concurrency; a startup race silently left the app rules-only; rotated-PDF redaction missed text.

## Not tested or limited
* **Docker:** Docker is not installed on the development machine, so the Dockerfiles and Compose file are untested.
* **Tesseract:** not implemented (RapidOCR is the only OCR engine); the OCR interface allows adding it.
* **Browsers:** only Chromium-based Edge was run. No Firefox or Safari. No screen-reader testing; axe finds only part of
  the accessibility problems that exist.
* **Scale and load:** no load test. Scans run in the web process, one at a time per document.
* **One unexplained browser-test failure:** in one of five full runs, "a document without PII shows an empty review
  state" failed after the first run on a fresh server (passed alone and in the next three full runs). Cause not found;
  re-run before trusting a single red result.
* **Model quality** is measured by the ML evaluation, not by these tests (see `docs/ml-pipeline.md`).
