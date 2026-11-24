# MVP Implementation Tasks — Premier League Intelligence Platform

**Status:** Not started | **Source:** `docs/PRD.md` v1.1, `docs/TRD.md` v1.0, `docs/SECURITY.md`  
**Workflow:** One checkbox = one short, approved Codex session. Work strictly in order; do not start the next task automatically. Mark complete only after tests and review. All paths are relative to repository root. Paths are proposed; if a file does not exist yet, create it only in its task.  
**Constraints:** Public GitHub repository; zero paid/card-required services; 8 GB RAM; no Docker, always-on server, or local browser-test installation. Python 3.12; frontend versions pinned during setup after approval.

> **Dependency rule:** Each task depends only on earlier numbered tasks. A blocked feasibility gate blocks production publishing, not isolated prototype development. Do not fabricate fixtures, permissions, or forecasts.

## Phase 1 — Foundation and feasibility

- [x] **T01 — Repository skeleton, Git hygiene, and documentation**
  - **Goal:** Initialize Git (if needed) and create the minimal directory structure without implementing features.
  - **Files:** `.gitignore`, `.env.example`, `README.md`, `frontend/`, `engine/`, `docs/`, `.github/workflows/` (placeholder directories only as needed).
  - **Acceptance:**
    - [x] `.gitignore` excludes `.env*` except `.env.example`, `.venv/`, `node_modules/`, `dist/`, `*.sqlite*`, caches, raw API dumps, and local credentials; does not accidentally exclude approved public forecast JSON.
    - [x] `.env.example` contains placeholder variable **names only**, no real values; actual `.env` files are never opened or modified by Codex.
    - [x] `README.md` documents the repository layout and zero-cost restrictions; `docs/PRD.md`, `docs/TRD.md`, `docs/SECURITY.md`, and this task list are present.
    - [x] `git status` and `git check-ignore` confirm ignored secret/local paths.
  - **Test:** `git status --short`; `git check-ignore .env .venv/test node_modules/test.sqlite` (adapt sample paths to ignored patterns); manually inspect staged files for secrets.

- [x] **T02 — Lightweight frontend and Python toolchain**
  - **Goal:** Scaffold a runnable React/Vite/TypeScript frontend and Python package with basic lint/test/build commands; request approval before adding dependencies.
  - **Files:** `.nvmrc`, `frontend/package.json`, `frontend/package-lock.json`, `frontend/tsconfig*.json`, `frontend/vite.config.ts`, `frontend/src/*`, `engine/pyproject.toml`, `engine/requirements.lock`, `engine/src/pl_intel/__init__.py`, `engine/tests/test_smoke.py`.
  - **Acceptance:**
    - [x] Active Node LTS major and exact npm/Python dependencies are pinned and documented; Python runtime is 3.12.
    - [x] Frontend scripts `dev`, `test`, `lint`, `build` work; TypeScript strict mode is enabled.
    - [x] pytest and Ruff run on a tiny Python smoke test; no Docker, database server, or paid service.
    - [x] Basic GitHub CI runs lint, type check, tests, and build using standard free runners with minimal permissions.
  - **Test:** `cd frontend && npm ci && npm run test -- --run && npm run lint && npx tsc --noEmit && npm run build`; `python -m pytest engine/tests -q`; `python -m ruff check engine`; verify CI result.

- [ ] **T03 — Data, rules, and hosting feasibility register (release gate)**
  - **Goal:** Verify rather than assume the legal and operational prerequisites before production data use.
  - **Files:** `docs/DATA_SOURCES.md`, `docs/FEASIBILITY.md` (or one consolidated register), `engine/tests/fixtures/` (small permitted samples only).
  - **Acceptance:**
    - [ ] Record dated evidence/links for OpenFootball historical coverage and permitted use, football-data.org free EPL access, rate limits, delayed scores, and derived-data redistribution rights.
    - [ ] Verify the active season's **20 teams and complete 380-fixture inventory**, with postponed/cancelled status semantics and canonical IDs.
    - [x] Record official season-specific tiebreak rules and what cannot be determined from available data.
    - [ ] Verify at least one completed backtest season and a free **no-card** static-hosting path; mark each gate PASS/BLOCKED with evidence.
    - [x] If any gate is blocked, explicitly prohibit production publication; do not substitute guessed facts.
  - **Test:** Review evidence URLs, terms, fixture counts, and PASS/BLOCKED statuses manually; no production fetch or deploy until approved.

## Phase 2 — Data and model

- [x] **T04 — Canonical data contracts and temporary SQLite schema**
  - **Goal:** Define stable teams, seasons, fixtures, statuses, and relationships independently of provider naming.
  - **Files:** `engine/src/pl_intel/{schemas,db,validate}.py`, `engine/tests/unit/test_schema.py`, `engine/tests/fixtures/`.
  - **Acceptance:**
    - [x] Schema has stable canonical team IDs, season ID, provider fixture ID, UTC kickoff, home/away IDs, status, and nullable final scores.
    - [x] SQLite enforces unique season/team and provider fixture identities, foreign keys, and parameterized queries.
    - [x] Reject duplicate/unmapped teams, invalid scores, impossible statuses, and inconsistent fixture records; no raw untrusted data in public JSON.
  - **Test:** `python -m pytest engine/tests/unit/test_schema.py -q`; include duplicate-key, malformed-score, and foreign-key cases.

- [x] **T05 — Historical and current-source adapters**
  - **Goal:** Parse approved OpenFootball files and fetch approved football-data.org fixtures/results into canonical records.
  - **Files:** `engine/src/pl_intel/sources/{openfootball,football_data}.py`, `engine/src/pl_intel/normalize.py`, `engine/tests/unit/test_sources.py`, `engine/tests/fixtures/`.
  - **Acceptance:**
    - [x] Source adapters use bounded HTTP timeouts, rate-aware retry/backoff, and explicit status/error handling.
    - [x] API key comes only from runtime environment/GitHub Secrets, never frontend or logs.
    - [x] Team alias mapping is explicit and fails closed on unknown clubs; frozen fixtures require no network or credentials.
    - [x] Complete inventory and duplicate-ID checks are enforced before accepting season input.
  - **Test:** `python -m pytest engine/tests/unit/test_sources.py -q` with mocked API responses, 429/5xx, partial inventory, and alias errors.

- [x] **T06 — Active-season resolver and league ranking rules**
  - **Goal:** Choose the valid active season and implement verified season-specific standings/tiebreak behavior.
  - **Files:** `engine/src/pl_intel/{season,ranking}.py`, `engine/src/pl_intel/rules/`, `engine/tests/unit/test_season_ranking.py`.
  - **Acceptance:**
    - [x] Resolve season from provider calendar/metadata, not hardcoded year; off-season/incomplete next season returns explicit pending state.
    - [x] Standings count finished matches only and support uneven games played/postponements.
    - [x] Verified league tiebreaks are deterministic; unresolved exceptional tie procedures block publication rather than guessing.
  - **Test:** `python -m pytest engine/tests/unit/test_season_ranking.py -q`; cover season transition, postponement, equal-points ties, and incomplete rules.
  - **Completion note**
    - Implementation merged at `fe9f48a`. [PR #7](https://github.com/DaniyalHussain02/premier-league-intelligence/pull/7)
    - Verification passed for 37 focused T06 tests, 58 T05 regression tests, and 145 engine tests in total. Ruff lint, Ruff format, and Git whitespace checks passed.
    - Live provider behavior remains unverified. Official adjustments, qualification boundaries, and exceptional decisions still require independently verified evidence. T03 continues to block production publication.

- [ ] **T07 — Elo strength updates**
  - **Goal:** Build a deterministic, time-ordered team-strength baseline.
  - **Files:** `engine/src/pl_intel/elo.py`, `engine/tests/unit/test_elo.py`.
  - **Acceptance:**
    - [ ] Ratings update only from matches completed by the specified cutoff; stable ordering for same-day matches.
    - [ ] Explicit initialization and season-transition policy; reproducible outputs from frozen data.
    - [ ] No future results or final-season labels leak into earlier cutoffs.
  - **Test:** `python -m pytest engine/tests/unit/test_elo.py -q`; check cutoff invariance and repeatability.

- [ ] **T08 — Poisson match probabilities**
  - **Goal:** Convert pre-match team strengths into valid home/draw/away and scoreline probabilities.
  - **Files:** `engine/src/pl_intel/poisson.py`, `engine/tests/unit/test_poisson.py`.
  - **Acceptance:**
    - [ ] Nonnegative, finite score probabilities; 1X2 probabilities each in [0,1] and sum to 1 within tolerance.
    - [ ] Document home advantage, truncation/tail handling, and any fitted parameters.
    - [ ] Same input and model version produce the same probabilities; no post-cutoff inputs.
  - **Test:** `python -m pytest engine/tests/unit/test_poisson.py -q`; test extreme strengths and normalization.

- [ ] **T09 — Seeded season Monte Carlo simulator**
  - **Goal:** Simulate remaining fixtures without altering actual completed results.
  - **Files:** `engine/src/pl_intel/simulate.py`, `engine/tests/unit/test_simulate.py`.
  - **Acceptance:**
    - [ ] Default 10,000 seeded simulations in bounded batches; all 380 fixtures appear exactly once per season realization.
    - [ ] Finished results are fixed; unfinished/postponed fixtures are simulated only once.
    - [ ] Each simulation uses verified official ranking rules; title winner and bottom three counted correctly.
    - [ ] Probabilities in [0,1], unrounded title total = 1, relegation total = 3; expected points >= actual points.
  - **Test:** `python -m pytest engine/tests/unit/test_simulate.py -q`; completed season, partial season, postponements, reproducibility, and totals.

- [ ] **T10 — Representative predicted table assignment**
  - **Goal:** Produce a unique predicted rank 1–20 from the 20×20 finishing-position probability matrix.
  - **Files:** `engine/src/pl_intel/assignment.py`, `engine/tests/unit/test_assignment.py`.
  - **Acceptance:**
    - [ ] SciPy maximum-weight assignment maximizes summed position probabilities, not expected points.
    - [ ] Exactly one club per rank; deterministic stable-team-ID tie resolution, including equal-optimum cases.
    - [ ] Projected points remain separate from representative predicted position; explanation is available for UI.
  - **Test:** `python -m pytest engine/tests/unit/test_assignment.py -q`; competing modal ranks, exact ties, and permutation stability.

- [ ] **T11 — Leakage-free chronological backtesting**
  - **Goal:** Evaluate match-outcome probabilities on at least one verified completed season against a simple baseline.
  - **Files:** `engine/src/pl_intel/backtest.py`, `engine/tests/unit/test_backtest.py`, `docs/METHODOLOGY.md`.
  - **Acceptance:**
    - [ ] Each prediction uses only data available at its historical cutoff; evaluation season, sample size, cutoff policy, and model version recorded.
    - [ ] Multiclass Brier score for model and documented baseline uses exactly the same test matches.
    - [ ] Backtest output is explicitly retrospective, never presented as live published forecast history.
    - [ ] Limitations and calibration concerns are documented; no live-season final-rank accuracy before season end.
  - **Test:** `python -m pytest engine/tests/unit/test_backtest.py -q`; assert no leakage and matched observation IDs.

## Phase 3 — Publishing and user experience

- [ ] **T12 — Static JSON schema and validated serialization**
  - **Goal:** Define the frontend-readable contract for latest forecast, history, seasons, and evaluation.
  - **Files:** `engine/src/pl_intel/{schemas,publish}.py`, `frontend/src/lib/{types,dataClient}.ts`, `engine/tests/unit/test_json_contract.py`.
  - **Acceptance:**
    - [ ] Versioned JSON contract includes season, generated-at UTC, data cutoff, simulation count, model version, team metrics, and 20×20 position probabilities where needed.
    - [ ] Separate season index, latest manifest, immutable snapshot, history index, and backtest JSON; paths under `frontend/public/data/`.
    - [ ] Validate exactly 20 unique clubs, rank uniqueness, finite numbers, probability totals, and permitted publishable fields before serialization.
    - [ ] Frontend client loads static HTTP GET JSON only; no dynamic API or credentials in browser.
  - **Test:** `python -m pytest engine/tests/unit/test_json_contract.py -q`; `cd frontend && npx tsc --noEmit`.

- [ ] **T13 — Idempotent and failure-safe snapshot publishing**
  - **Goal:** Make changed-data updates append-only and leave the previous valid forecast untouched on errors.
  - **Files:** `engine/src/pl_intel/{publish,cli}.py`, `engine/tests/integration/test_publish.py`, `frontend/public/data/` (approved sample artifacts only).
  - **Acceptance:**
    - [ ] Canonical input-result hash prevents duplicate snapshots on unchanged source data.
    - [ ] Changed valid input creates immutable season-namespaced snapshot and updates indexes/manifests only after all validations pass.
    - [ ] Any fetch, validation, simulation, or write failure preserves the prior latest pointer, bytes, and original timestamps.
    - [ ] Published artifacts contain no raw licensed inputs or secrets; publishing is blocked by unresolved feasibility gates.
  - **Test:** `python -m pytest engine/tests/integration/test_publish.py -q`; inject failures and compare prior artifact hashes.

- [ ] **T14 — Responsive current forecast dashboard**
  - **Goal:** Display current and representative predicted standings, title/relegation probabilities, and freshness metadata.
  - **Files:** `frontend/src/{App.tsx,pages/Dashboard.tsx,components/ForecastTable.tsx,components/StatusBanner.tsx,styles/*}`, `frontend/src/lib/{dataClient,format}.ts`, frontend tests.
  - **Acceptance:**
    - [ ] All 20 teams, actual rank/points/games played, projected points, assigned predicted rank, title/relegation odds appear with correct labels.
    - [ ] Show season, generated time, data cutoff, simulation count, stale/pending states, and plain-language representative-table explanation.
    - [ ] Accessible semantic table, keyboard focus, readable probabilities without color, usable at 320 px; no blank-screen failure on missing JSON.
  - **Test:** `cd frontend && npm run test -- --run && npm run lint && npx tsc --noEmit && npm run build`; manual 320 px inspection.

- [ ] **T15 — Forecast history and methodology pages**
  - **Goal:** Show club probability history and transparent retrospective evaluation without inventing prior forecasts.
  - **Files:** `frontend/src/components/ProbabilityHistory.tsx`, `frontend/src/pages/Methodology.tsx`, `frontend/src/lib/dataClient.ts`, frontend tests, `docs/METHODOLOGY.md`.
  - **Acceptance:**
    - [ ] Club selector and matchweek/snapshot selector use immutable launch-forward snapshots; lazy-load history.
    - [ ] With fewer than two snapshots, show “History will appear after the next update.”
    - [ ] Methodology displays Brier score, baseline, season, sample size, cutoff policy, model version, and uncertainty/source limitations.
    - [ ] Charts have accessible text/table alternatives; retrospective and live histories are visually and textually separate.
  - **Test:** `cd frontend && npm run test -- --run && npm run lint && npx tsc --noEmit && npm run build`; test zero/one/multiple snapshots.

## Phase 4 — Operations, review, launch

- [ ] **T16 — Weekly CI forecast workflow and safe publication**
  - **Goal:** Run the approved batch pipeline on a free standard GitHub Actions runner, on schedule and manually.
  - **Files:** `.github/workflows/{ci,forecast}.yml`, `engine/src/pl_intel/cli.py`, `docs/OPERATIONS.md`, workflow tests/fixtures.
  - **Acceptance:**
    - [ ] Weekly cron and `workflow_dispatch`; pinned actions, minimal permissions, concurrency guard, bounded timeouts, and no paid runners.
    - [ ] Runtime season resolution; secret supplied only via GitHub Actions Secrets; no secrets in logs/artifacts.
    - [ ] No-change run makes no commit; successful changed-data run commits only validated permitted artifacts; failure keeps prior public forecast.
    - [ ] Document manual rerun, stale-data diagnosis, API quota handling, and how to disable schedule.
  - **Test:** Workflow syntax check; run `workflow_dispatch` on approved safe fixture/data and inspect job permissions, logs, diff, and no-change/failure behavior.

- [ ] **T17 — Security, cost, and release review**
  - **Goal:** Complete `docs/SECURITY.md` MUST checks and verify all PRD release gates before deployment.
  - **Files:** `docs/SECURITY.md`, `docs/FEASIBILITY.md`, `docs/OPERATIONS.md`, relevant tests/workflows.
  - **Acceptance:**
    - [ ] All applicable MUST security items checked with evidence; no real `.env`, leaked key, public secret, or overly broad Actions permission.
    - [ ] `npm audit`, `pip-audit`, and GitHub Dependabot findings reviewed; critical/high findings addressed or release blocked with documented rationale.
    - [ ] No card/billing required; free quotas and failure behavior verified; laptop avoids Docker/heavy local scanning.
    - [ ] 20-team/380-fixture checks, tiebreaks, idempotency, backtesting, history, and failure preservation pass; all unresolved release gates explicitly block launch.
  - **Test:** CI unit/integration/lint/type/build checks, dependency audits, manual checklist inspection; report exact commands/results and unresolved risks.

- [ ] **T18 — Free static deployment and production smoke test**
  - **Goal:** Publish the read-only dashboard through verified free, no-card hosting and verify end-to-end behavior.
  - **Files:** Hosting configuration as required, `README.md`, `docs/OPERATIONS.md`, `.github/workflows/` (only deployment changes), frontend tests.
  - **Acceptance:**
    - [ ] Use Cloudflare Pages free if signup/limits are confirmed; otherwise GitHub Pages if confirmed; never enable paid features.
    - [ ] Production serves `frontend/dist` over HTTPS; correct base path and immutable JSON assets; no frontend API token.
    - [ ] Latest forecast, title/relegation odds, history empty state, methodology/backtest, pending/stale states, and mobile layout work at public URL.
    - [ ] Manual refresh and failed-refresh preservation verified; site retains original timestamp after failed update.
    - [ ] Document URL, deployment procedure, rollback, quota monitoring, and any unverified performance targets (do not claim measured speed without measuring).
  - **Test:** CI build + browser smoke on hosted URL (Playwright in CI if configured), HTTP GET checks for manifest/snapshot, mobile viewport and accessibility spot-check; verify no billable configuration.

## Completion protocol (every task)

- [ ] Before changes: Codex states task ID, plan, expected files, tests, and dependency additions; **waits for explicit approval**.
- [ ] During changes: touch only approved task files; follow `docs/SECURITY.md`; no secret access, paid services, or unapproved dependencies.
- [ ] After changes: run relevant tests, lint, type checks, and build; report commands and outcomes honestly.
- [ ] Before checking task off: verify its acceptance checkboxes, update documentation/contracts if changed, summarize files and remaining risks, and ask before proceeding.

**Release condition:** T01–T18 completed and PRD F1–F5 satisfied; unresolved legal/data/rules/hosting gates mean **not production ready**.
