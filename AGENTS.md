# AGENTS.md — Premier League Intelligence Platform

## Project
Free, public, read-only Premier League forecasting dashboard showing a representative final table, title/relegation probabilities, forecast history, and retrospective accuracy. A scheduled Python batch job validates match data, runs Elo/Poisson + seeded Monte Carlo forecasts, and publishes immutable static JSON for a React site. No live backend, accounts, paid services, or local heavy infrastructure.

## Read before every task
1. `docs/PRD.md` — approved product scope and acceptance criteria.
2. `docs/TRD.md` — architecture, schemas, pipeline, deployment, feasibility gates.
3. `docs/SECURITY.md` — mandatory security checklist.
4. `docs/TASKS.md` — task queue; if absent, **stop and ask** for it. Do not invent task status.

If docs conflict, flag the conflict and request a decision; do not silently override approved requirements.

## Stack and version policy
- Frontend: React, TypeScript, Vite, CSS/CSS Modules; Recharts lazy-loaded for history charts.
- Batch: **Python 3.12**, Python built-in `sqlite3`, pytest and Ruff. T09 uses the standard library for seeded, bounded-batch simulation on free standard cloud runners. NumPy and SciPy remain planned for later work where applicable, including SciPy assignment in T10, subject to separate dependency approval.
- Frontend quality: Vitest, React Testing Library, ESLint, TypeScript compiler; Playwright smoke tests in CI only.
- CI: GitHub Actions standard **`ubuntu-24.04`** runner; public GitHub repository.
- Hosting: Cloudflare Pages free (subject to no-card validation); GitHub Pages fallback.
- Inputs: OpenFootball history and football-data.org free fixtures/results, **only after rights and completeness checks**.
- Node.js: use the active LTS major recorded in `.nvmrc`/`frontend/package.json` engines once scaffolded; **TRD does not specify a numeric Node version**. Pin exact npm dependencies in `frontend/package-lock.json` and Python dependencies in `engine/requirements.lock`; do not guess package versions.

## Commands (after scaffold exists)
```bash
# Frontend (from repo root)
cd frontend && npm ci
cd frontend && npm run dev
cd frontend && npm run test -- --run
cd frontend && npm run lint
cd frontend && npx tsc --noEmit
cd frontend && npm run build

# Python (from repo root; create venv once)
python -m venv .venv
# Windows PowerShell: .venv\Scripts\python.exe -m pip install -r engine/requirements.lock
# macOS/Linux: .venv/bin/python -m pip install -r engine/requirements.lock
.venv/bin/python -m pytest engine/tests -q
.venv/bin/python -m ruff check engine

# Free dependency checks (once installed)
cd frontend && npm audit --omit=dev
# Python: run pip-audit in CI against engine/requirements.lock
```
Commands assume scripts/configuration and lockfiles have been created; on Windows use `.venv\Scripts\python.exe` in place of `.venv/bin/python`. If a command is missing, report it and propose a fix; do not claim it passed. Do not install Playwright browsers locally by default.

## Repository layout
- `docs/` — PRD, TRD, SECURITY, TASKS, feasibility evidence, methodology.
- `frontend/src/components/`, `pages/`, `lib/`, `styles/` — UI, views, static JSON client/types, styling.
- `frontend/public/data/` — **validated, legally publishable** versioned JSON snapshots/manifests only.
- `engine/src/pl_intel/` — sources, normalization, validation, SQLite, Elo/Poisson, simulation, backtesting, publishing.
- `engine/tests/` — unit, integration, and frozen fixture tests.
- `.github/workflows/` — CI and scheduled/manual forecast publishing.

## Code conventions
- Prefer small typed functions, explicit schemas, stable team IDs, UTC ISO-8601 timestamps, deterministic ordering and seeded RNG.
- TypeScript strict typing; avoid `any`; Python type hints; format/lint using configured project tools.
- Keep data ingestion, model, publishing, and UI separate. No browser access to football-data.org or secrets.
- Parameterize SQLite queries; treat upstream data as untrusted; use React escaped text, not raw HTML.
- Preserve published snapshot immutability; never fabricate fixtures/history or update timestamps on failed runs.
- Test 20 teams, 380 fixtures, official season-specific tiebreaks, probability sums, unique max-weight assigned ranks, chronological leakage prevention, and idempotent publication where relevant.

## Agent operating rules
1. Work on **ONE** item from `docs/TASKS.md` at a time; state task ID and exact scope.
2. **Before writing or modifying code**, propose a brief plan, files affected, tests, and risks; **wait for my explicit approval**.
3. Ask permission before adding, removing, or upgrading **any dependency**.
4. Use only OSS and verified free/no-credit-card services. Never enable billing, trials, premium runners, or paid API tiers.
5. Keep 8 GB laptop usage light: no Docker, local model, database server, or heavy local scan; prefer CI for compute/browser tests.
6. Never read, create, modify, print, or commit `.env` files or real credentials; `.env.example` may contain placeholders only.
7. Follow `docs/SECURITY.md`; never weaken validation, access controls, CI permissions, or tests to force a pass.
8. Change only task-related files. Do not rewrite unrelated files, docs, snapshots, or lockfiles without approval.
9. Add/update tests for every behavior change; run relevant tests, lint/type checks, and build before marking complete. Report any checks not run.
10. After each task, summarize changed files, commands/results, remaining risks, and the next proposed task; do not start it automatically.
11. Stop and report failed data/license/fixture/rules/hosting feasibility gates; do not invent a workaround or publish invalid forecasts.

## Definition of done
- [ ] Approved task scope implemented; no unrelated changes or unapproved dependencies.
- [ ] Relevant acceptance criteria met and tests added/updated; executed checks pass (or task remains blocked).
- [ ] Security checklist respected; no secrets, paid services, or unvalidated public data.
- [ ] Output is deterministic where required; existing valid snapshots survive failures.
- [ ] Documentation updated where behavior/contracts changed; task results and limitations summarized.
