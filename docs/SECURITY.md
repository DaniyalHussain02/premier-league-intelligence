# Security Checklist — Premier League Intelligence Platform

**Version:** 1.0  
**Basis:** PRD v1.1 and TRD v1.0 (2026-11-23)  
**Scope:** Public React/Vite/TypeScript static site; Python/NumPy/SciPy batch forecasting; disposable SQLite; static JSON; GitHub Actions; Cloudflare Pages or GitHub Pages.  
**Owner:** Repository maintainer. **Release rule:** Every **MUST** item must be checked or explicitly marked *not applicable* with evidence before production launch. **SHOULD** items are recommended, not launch blockers unless a discovered risk makes them necessary.

> **Threat model:** The main risks are leaked football-data.org credentials, malicious or malformed upstream match data, compromised dependencies/CI workflows, incorrect or unauthorized publication of forecast snapshots, excessive API requests, and unintentional disclosure of licensed source data. The MVP has **no user accounts, user-submitted data, dynamic backend, or writable public API**. Do not add security machinery for features that do not exist.

## 1. Secrets and credentials

- [ ] **MUST** Store `FOOTBALL_DATA_API_TOKEN` only in **GitHub repository Actions secrets** (Settings → Secrets and variables → Actions), available only to the trusted scheduled/manual forecast job.
- [ ] **MUST** Use a local untracked `.env` only if running the Python ingestion script locally; load it only in Python, never in Vite or browser code. Prefer a local shell environment variable when practical.
- [ ] **MUST** Include `.env`, `.env.*`, `!.env.example`, `.venv/`, `venv/`, `*.db`, `*.sqlite`, `*.sqlite3`, `.pytest_cache/`, `__pycache__/`, `node_modules/`, `frontend/dist/`, `.cache/`, raw API responses and local temporary outputs in `.gitignore`. Verify the ignore rules do **not** exclude approved `frontend/public/data/**/*.json` snapshots.
- [ ] **MUST** Keep `.env.example` limited to variable names and harmless placeholders; do not paste working tokens into sample files, issues, screenshots, prompts, or Codex task text.
- [ ] **MUST** Never define `VITE_FOOTBALL_DATA_API_TOKEN` or any other secret in `VITE_*` variables: Vite embeds them into public JavaScript.
- [ ] **MUST** Ensure Cloudflare Pages build variables contain no football-data.org token; frontend builds need no upstream API credentials. If a hosting deploy token becomes necessary, use the hosting provider's secret storage or GitHub Actions secrets, scoped to deployment only.
- [ ] **MUST** Use the built-in short-lived `GITHUB_TOKEN`; do not create a personal access token unless a documented blocker makes it necessary. Restrict its job permissions (read-only for tests, `contents: write` only for the trusted publisher).
- [ ] **MUST** Add a secret-leak response procedure: immediately revoke/rotate the exposed token at its provider; replace the GitHub secret/local value; stop affected jobs; check Actions logs, commits, PRs, artifacts, and deployed bundles; remove exposed material; notify the provider if required; document the incident. Removing a secret from the latest commit alone does **not** remove it from Git history—coordinate history cleanup if needed.
- [ ] **SHOULD** Run a lightweight free secret scanner such as `gitleaks` in CI or use a targeted `git grep` review; never print scanner matches containing full secrets to public logs.

## 2. Authentication, authorization, and access boundaries

- [ ] **MUST** Keep the public dashboard and JSON endpoints anonymous and **read-only**; no sign-up, passwords, sessions, cookies for login, JWTs, OAuth, or per-user data in the MVP.
- [ ] **MUST** Treat all files under `frontend/public/data/` as publicly downloadable. Publish only reviewed, permitted derived forecast/backtest fields; never put private metadata or tokens there.
- [ ] **MUST** Require GitHub account authentication and MFA/2FA for the maintainer; review repository collaborators and remove unnecessary write access.
- [ ] **MUST** Restrict manual `workflow_dispatch` execution and changes to publishing workflows to trusted repository maintainers using GitHub permissions and review controls available on the free plan.
- [ ] **MUST** Do not expose a browser-triggered forecast generation or publish route. Only the scheduled/manual trusted GitHub Actions job may publish forecasts.
- [ ] **MUST** Do not execute secrets-bearing jobs on untrusted fork pull-request code or `pull_request_target` with an untrusted checkout.
- [ ] **SHOULD** Enable branch protection/rulesets and required reviews where available free for the repository; document any platform limitation rather than assuming a control exists.
- [ ] **MUST — N/A for MVP** Password hashing, password resets, session expiry, JWT verification, object ownership checks, and “users may access only their own records” do not apply because **no users or private records exist**. If accounts are ever introduced, create a separate security design and tests *before* implementation; never add dummy auth.

## 3. Input validation, sanitization, and data integrity

- [ ] **MUST** Validate every upstream fixture against a typed/explicit schema: stable fixture ID, canonical home/away team IDs, valid competition and season, known status, valid UTC timestamps, and nonnegative integer scores only when finished.
- [ ] **MUST** Reject duplicate fixture IDs, unknown or duplicate clubs, a club playing itself, inconsistent statuses/scores, impossible or incomplete 380-fixture inventories, and season mismatches; do not publish partial forecasts.
- [ ] **MUST** Map provider team names/IDs to a reviewed canonical mapping; reject unmapped values rather than guessing or using them as executable code.
- [ ] **MUST** Treat API payloads, OpenFootball files, JSON snapshots, and query parameters as untrusted. Parse as data only; do not use `eval`, shell interpolation, `exec`, or dynamic imports based on upstream strings.
- [ ] **MUST** Use React's ordinary escaped text rendering for club names and descriptions; do not use `dangerouslySetInnerHTML` with upstream content. Do not interpolate upstream strings into raw HTML or SVG markup.
- [ ] **MUST** Validate static JSON against a versioned schema **before publication** and check mathematical invariants: 20 clubs, 380 fixtures accounted for, unique ranks, probabilities in [0,1], title sum ≈1, relegation sum ≈3, and valid referenced snapshot paths.
- [ ] **MUST** Validate browser-loaded JSON (at least `schema_version`, required fields, types, and bounded lengths) before rendering; display a safe error state on invalid data rather than crashing or showing misleading figures.
- [ ] **MUST** Restrict season/snapshot selectors to IDs listed in validated published indexes; reject `../`, absolute URLs, and unexpected characters. Build fetch paths only from validated identifiers, not arbitrary user-supplied URLs.
- [ ] **MUST — N/A for MVP** There are no user file uploads or writable forms. Do not add upload endpoints. If added later, require an independent file-type, size, storage, malware, and access-control review.
- [ ] **SHOULD** Set sensible HTTP fetch timeouts and response-size limits for ingestion; reject unexpectedly large or malformed provider responses.

## 4. Public API, transport, and error handling

- [ ] **MUST** Serve only static same-origin `GET` JSON files (e.g. `/data/index.json`, `/data/seasons/{season_id}/latest.json`, `/data/seasons/{season_id}/snapshots/{snapshot_id}.json`, `/data/seasons/{season_id}/history.json`, `/data/backtests/{backtest_id}.json`); do not deploy a live API or write methods.
- [ ] **MUST** Use HTTPS on Cloudflare Pages/GitHub Pages and verify HTTP redirects to HTTPS for the final public URL.
- [ ] **MUST** Keep browser fetches same-origin. No permissive `Access-Control-Allow-Origin: *` configuration is needed for the app; public static JSON may still be fetched by other clients, so **CORS is not an access-control boundary**.
- [ ] **MUST** Keep the football-data.org token server-side in the batch job. Browsers must never call football-data.org directly using that token.
- [ ] **MUST** For upstream API calls, enforce a conservative client-side request budget below the provider's verified free limit; honor 429 and `Retry-After`, back off with a bounded retry count, and stop on persistent 401/403 without upgrading a plan.
- [ ] **MUST** On fetch, parse, validation, simulation, or deployment failure, preserve the previous valid published snapshot and its original `generated_at`/`data_cutoff_at`. Do not replace it with a partial result or fake freshness timestamp.
- [ ] **MUST** Show friendly UI error messages without stack traces, secret values, file paths, or raw upstream responses. In CI logs, record sanitized error codes and operational context only.
- [ ] **SHOULD** Configure static security headers when supported by the selected host: `X-Content-Type-Options: nosniff`, a tested restrictive Content Security Policy, `Referrer-Policy`, and appropriate `Permissions-Policy`. Avoid a CSP that breaks Vite bundles or charts.
- [ ] **SHOULD** Use CDN caching for immutable snapshot URLs; keep `latest.json` and indexes on shorter cache lifetimes so updates become visible. Do not add a paid edge rate-limiting product just for a read-only static site.

## 5. SQLite and published JSON storage

- [ ] **MUST** Use SQLite only inside local/CI batch processing; do not expose its file, a database listener, or SQL execution endpoint to the public.
- [ ] **MUST** Use `sqlite3` bound parameters (`?` placeholders) for all data values; never construct SQL using string-concatenated upstream fields. Allowlist any unavoidable dynamic table/column names.
- [ ] **MUST** Restrict SQLite file access to the current processing user and a temporary workspace; delete transient DBs after successful processing or CI cleanup.
- [ ] **MUST** Do not upload SQLite databases, raw licensed provider payloads, API tokens, or CI debug dumps as public build artifacts.
- [ ] **MUST** Keep published snapshots immutable; calculate content fingerprints and prevent unchanged inputs from producing duplicate history entries. Update the `latest` manifest only after all referenced outputs pass validation and are committed/deployed consistently.
- [ ] **MUST** Restrict repository write access and publishing-job token permissions; this is the equivalent of least privilege for the static JSON store.
- [ ] **MUST — N/A for MVP** Database user roles, row-level security (RLS), and per-user ownership policies are not applicable to a disposable local SQLite database with no user data. Reassess if a hosted multi-user database is introduced.
- [ ] **SHOULD** Add tests proving failed publication cannot delete or corrupt the last valid snapshot and that all manifest references resolve.

## 6. Dependency and CI supply-chain safety

- [ ] **MUST** Commit `package-lock.json` and a pinned Python dependency lock/constraints file; use `npm ci` in CI, not an unconstrained install.
- [ ] **MUST** Run `npm audit --omit=dev` for production npm dependencies and review any high/critical findings; also review development-tool vulnerabilities that can affect CI/build execution.
- [ ] **MUST** Run free `pip-audit` against the resolved Python dependencies in CI or a scheduled security check; triage vulnerabilities before launch.
- [ ] **MUST** Enable GitHub Dependabot security alerts and security updates where available on the free public repository; review generated PRs and run tests before merging.
- [ ] **MUST** Pin third-party GitHub Actions to reviewed full commit SHAs where practical, especially actions handling secrets or publishing; keep workflow `permissions` minimal.
- [ ] **MUST** Avoid unreviewed npm install scripts, Python setup scripts, remote `curl | bash` installers, and execution of scripts embedded in downloaded football datasets.
- [ ] **MUST** Do not suppress audit failures or ignore a vulnerability without recording affected package, exploitability, compensating control, and remediation decision.
- [ ] **SHOULD** Use `ruff`, ESLint, and TypeScript checking in CI to catch suspicious or unsafe code patterns; these are not substitutes for dependency audits.
- [ ] **SHOULD** Run security scans in GitHub Actions rather than keeping heavy local scanners/browser installations running on the 8 GB laptop.

## 7. Free-tier, abuse, and resource controls

- [ ] **MUST** Use a **public** GitHub repository with standard GitHub-hosted runners only; never select paid larger runners, paid storage, or paid build minutes.
- [ ] **MUST** Configure the forecast workflow with weekly `cron`, manual `workflow_dispatch`, `timeout-minutes` (e.g. 30), and serialized `concurrency` with `cancel-in-progress: false`.
- [ ] **MUST** Stop after unchanged-data fingerprint checks; do not rerun 10,000 simulations or publish new snapshots when inputs/model configuration have not changed.
- [ ] **MUST** Bound HTTP retries, API calls, data sizes, and simulation memory. Run full-season T09 workloads on free standard cloud runners instead of the 8 GB laptop. Use the seeded standard-library sampler in bounded batches with online aggregation. Record runtime and configuration provenance, preserve batch-size reproducibility, and keep official ranking and consequential-tie blockers enforced.
- [ ] **MUST** Do not enable billing, attach a payment card, accept paid overages, or automatically upgrade football-data.org, Cloudflare, or GitHub services. If a free quota is exhausted, let the update/build fail safely and retain the last valid site.
- [ ] **MUST** Verify current no-card signup, quota, and permitted-use terms for the selected hosting provider and data sources before launch; the TRD's numerical limits are **not permanent guarantees**.
- [ ] **MUST** Do not enable Cloudflare Workers, R2, D1, paid WAF, analytics add-ons, premium GitHub runners, or a hosted database for MVP.
- [ ] **MUST** Keep generated data and Git history within free hosting/repository constraints; avoid storing raw season datasets or large CI artifacts. Load history JSON on demand.
- [ ] **SHOULD** Check GitHub Actions usage, Pages deployment status, API rate-limit responses, and repository size monthly; use provider dashboards/free notifications where available.
- [ ] **SHOULD** Run Playwright browser smoke tests in CI only; keep local development to VS Code, Node/Vite, a small Python venv, and optional lightweight SQLite inspection.

## 8. Privacy and data rights

- [ ] **MUST** Store **no fan accounts, passwords, email addresses, profiles, IP-address databases, behavioral tracking identifiers, or personal prediction histories** in the MVP.
- [ ] **MUST** Do not add third-party analytics, advertising trackers, tracking cookies, or user-identifying telemetry by default.
- [ ] **MUST** Explain in a short public privacy note that the application does not intentionally collect user account data; hosting/CDN providers may still process ordinary access logs under their own policies. Do not claim zero logging anywhere.
- [ ] **MUST** Publish only the minimum derived football data required for standings, title/relegation odds, history, and backtest transparency; review each source's redistribution and derivative-use permissions first.
- [ ] **MUST** Keep raw API responses and ingestion debug information out of public JSON and public Actions artifacts; publish source attribution and data cutoff without credentials.
- [ ] **SHOULD** Avoid external fonts, embedded third-party widgets, and unnecessary external requests to reduce tracking surface and dependency risk.

## 9. Pre-launch security and integrity gate

- [ ] **MUST** All MUST items above are checked, or explicitly marked not applicable with a reason and reviewer/date.
- [ ] **MUST** Git history, tracked files, public JSON, built JS/CSS, Actions logs, and deploy previews are checked for exposed secrets; any found tokens are rotated.
- [ ] **MUST** Repository collaborator permissions, MFA, workflow permissions, and branch controls are reviewed.
- [ ] **MUST** `npm audit` and `pip-audit` findings are reviewed and critical exploitable issues are resolved or launch is blocked.
- [ ] **MUST** Untrusted fixture payload tests cover duplicate IDs, unknown teams, bad scores, malformed JSON, path traversal identifiers, and incomplete 380-match inventory.
- [ ] **MUST** Tests prove no invalid forecast can replace the last valid published snapshot; manifests reference existing immutable files.
- [ ] **MUST** Production HTTPS works; browser network inspection confirms no secret-bearing calls or third-party tracker requests.
- [ ] **MUST** Public site handles missing/corrupt JSON with safe messages and preserves original forecast timestamps.
- [ ] **MUST** Legal use of historical/current data, public derived outputs, and any graphics is documented and approved; if unresolved, block launch.
- [ ] **MUST** Confirm current free/no-card GitHub Actions, hosting, and data API conditions; no billing method or paid feature is enabled.
- [ ] **MUST** Run one successful end-to-end scheduled/manual publish and one intentionally failed update; verify the failed update leaves the prior deployed forecast intact.
- [ ] **SHOULD** Verify a restrictive CSP and security headers in the deployed response and test a mobile browser view.
- [ ] **SHOULD** Record reviewer, review date, deployed commit SHA, test run link, and any accepted residual risks below.

**Review record:** Reviewer: `________` · Date (UTC): `________` · Commit SHA: `________` · CI run: `________` · Open risks: `________`

## 10. AI coding agent: never-do rules

- [ ] **MUST** Never hardcode or print secrets, paste them into prompts, or expose them through `VITE_*`, public JSON, frontend bundles, test snapshots, or logs.
- [ ] **MUST** Never add authentication, a live backend, writable public endpoints, hosted database, Docker, or paid security services without an explicit approved scope change.
- [ ] **MUST** Never disable tests, validation, workflow permissions, or security checks just to make a build pass.
- [ ] **MUST** Never use `eval`, unsafe deserialization, raw string-built SQL, `dangerouslySetInnerHTML` with external data, or arbitrary shell commands constructed from provider input.
- [ ] **MUST** Never grant write permissions to untrusted PR workflows or expose repository/API credentials to forked PRs.
- [ ] **MUST** Never commit raw licensed data, temporary SQLite files, `.env` files, credentials, or unreviewed third-party assets.
- [ ] **MUST** Never publish a forecast when input completeness, source permissions, schema checks, mathematical invariants, or deployment checks fail.
- [ ] **MUST** Never alter immutable historical snapshots or replace real timestamps with fabricated freshness data.
- [ ] **MUST** Never bypass API rate limits, enable billable features, or silently introduce dependencies that exceed the 8 GB local RAM constraint.
- [ ] **SHOULD** For every security-sensitive change, report the files changed, tests run, security implications, and any unresolved risk in the Codex task summary.

---

**Scope-change trigger:** If the project later adds accounts, uploads, user-specific forecasts, a live API, or a hosted database, this checklist must be revised **before** those features are deployed. In particular, password storage, session/token security, authorization and ownership tests, RLS, CSRF, and server-side rate limiting then become applicable requirements.
