# Technical Requirements Document (TRD)
## Premier League Intelligence Platform

**Version:** 1.0 | **Date:** 2026-11-23 | **Status:** Proposed implementation specification; feasibility gates open  
**Source of truth:** PRD v1.1 | **Target:** 6 weeks, ~2 hours/day | **Repository:** public GitHub repository  
**Principle:** Zero paid services, no payment card, static public delivery, batch-only computation.

## 1. Scope and engineering acceptance gates

Build a public, read-only English Premier League forecasting dashboard for the validated active season: a representative final table, expected points, title/relegation probabilities, launch-forward immutable forecast history, and separately labeled retrospective backtests. Weekly scheduled checks and manual reruns produce forecasts only when validated results change. No accounts, live scores, player predictions, betting advice, chatbot, what-if simulator, or additional leagues.

**Non-negotiable gates before production:** (G1) legally permitted historical input and public derived-output usage; (G2) current season's complete 380-fixture inventory and 20-team roster; (G3) season-specific official ranking/tiebreak configuration, including any exceptional resolution procedures; (G4) confirmed free, no-card hosting and API signup; (G5) reproducible backtest with no temporal leakage. A failed gate blocks production launch, not local prototyping. No substitute data, invented fixtures, fabricated history, or guessed rules.

**Technical acceptance:** all 380 fixtures counted exactly once per simulated season; unique predicted ranks 1–20; probabilities in [0,1], unrounded title sums to 1 and relegation sums to 3; current points never exceed projected points; 10,000 seeded simulations by default; idempotent publishing; prior valid snapshot survives all failures; actual data cutoff and generation timestamps remain visible; 320px accessible layout; compressed initial app plus latest JSON target <=500 KB; measured first useful content target <3 seconds on typical 4G (not guaranteed).

## 2. Technology stack and free-tier controls

| Component | Choice and one-line reason | Free limit / failure behavior |
|---|---|---|
| Frontend | React + TypeScript + Vite: familiar, typed, fast static build. | OSS, no service quota; local Node/Vite may consume hundreds of MB; close other heavy apps. |
| UI | Plain CSS / CSS Modules: zero component-runtime dependency, accessible tables. | OSS, no quota; implement mobile layout manually. |
| Charts | Recharts (lazy-loaded): popular React history charts; provide text/table alternative. | OSS, no quota; bundle budget monitored. |
| Batch engine | Python 3.12 + NumPy + SciPy: widely supported Poisson distributions, assignment optimization, and seeded simulation. | OSS; CPU/memory bounded by chunked simulation; use standard cloud runner. |
| Validation | Python standard library + explicit validators (optional Pydantic only if useful): clear data contracts. | OSS, no quota. |
| Temporary storage | SQLite via Python `sqlite3`: transactional batch normalization, no database server. | No service quota; ephemeral CI filesystem; do not treat it as permanent history storage. |
| Historical data | OpenFootball, conditional on file-level license and completeness checks. | No hosted SLA or guaranteed season coverage; fail gate or switch to another **verified permitted free** source. |
| Current fixtures/results | football-data.org free API: EPL fixtures/results without paid data. | Official free tier advertises 10 calls/minute and delayed scores/schedules; on 429 back off, on 403 stop, never upgrade automatically. Historical access/redistribution still unverified. |
| Scheduled computation | GitHub Actions, public repo, standard `ubuntu-24.04`: offload CPU and RAM from laptop. | Standard public-repo runners are free under GitHub policy; job, concurrency, storage, abuse and artifact limits still apply; if blocked, pause jobs rather than buy capacity. |
| Publishing/hosting (primary) | Cloudflare Pages free, Git-connected: CDN for static assets and JSON. | As documented 2026-11-19: 500 builds/month, 1 concurrent build, 20-minute build timeout; 20,000 files/site, 25 MiB/file; excess builds wait/fail and oversized assets cannot deploy; verify signup without card. |
| Hosting fallback | GitHub Pages, Actions-built static site: avoids another account if Cloudflare signup fails. | Docs: 1 GB published site, soft 100 GB/month bandwidth, deployment timeout 10 minutes; exceeding soft limits can cause throttling/contact, not a guaranteed free scale-up. |
| Tests | pytest, Vitest, React Testing Library, Playwright (limited smoke), Ruff, `tsc`: mainstream free tooling. | OSS; browser installs consume disk/RAM, run Playwright in CI only. |
| Version control | Git + GitHub public repository: code review, history, workflow triggers. | Repository/storage and acceptable-use policies apply; do not commit large raw datasets or generated DBs. |

**Billing policy:** no credit card added, no paid plans, no larger runners, no Cloudflare Workers/R2/D1, no premium API upgrades, no custom purchased domain. Recheck provider policies before account creation and launch. Free quotas are **not** guarantees of uninterrupted service. GitHub Actions standard public runner currently lists 4 vCPU / 16 GB RAM for `ubuntu-24.04`, but runtime availability may change.

## 3. System architecture and data flow

```text
OpenFootball historical files ─┐
                              ├─> GitHub Actions (weekly + manual)
football-data.org EPL API ────┘      │
                                     ▼
                           fetch → normalize → validate
                                     │
                             ephemeral SQLite DB
                                     │
                        fit Elo/Poisson (past only)
                                     │
                        simulate remaining fixtures
                                     │
                        validate probabilities/tables
                                     │
                         write versioned JSON snapshots
                                     │
                        atomic Git commit / manifest update
                                     │
                               static site build
                                     ▼
                         Cloudflare Pages (or GH Pages)
                                     │ HTTPS GET JSON
                                     ▼
                         React browser dashboard
                         table | history | methodology
```

**No live backend.** A browser never contacts the football API. The only public 'API' is static, versioned JSON. A pipeline run creates a staging directory, validates every artifact, then commits snapshots and updates `latest.json` **last in the same atomic Git commit**. Build/deploy must succeed before the new manifest is served; deployment failures retain the previously deployed site. Concurrent runs are serialized. A separate frontend deployment on unrelated commits must not publish unvalidated forecast files.

**Publication semantics:** snapshots are immutable by content-addressed ID; manifest references existing files. The source-data hash is computed from normalized completed fixtures and full fixture inventory plus season, model version, configuration, and training-data version. If this fingerprint equals the last published fingerprint, do not create another snapshot. Model/code changes can deliberately create a new version even without new match results, explicitly labeled a model revision; weekly no-change runs do not.

**Staleness:** show `generated_at` and `data_cutoff_at` from last published snapshot; optional `checked_at` is operational metadata and must never masquerade as forecast freshness. Without an always-on service, the UI can calculate age from the embedded timestamp and show a configurable warning, not claim to know that the upstream feed is currently delayed.

## 4. Data model and relationships

SQLite is an **internal, disposable build-time database**. Published JSON is the persistent product data. Use stable canonical team IDs, UTC timestamps in ISO-8601, and season IDs such as `2026-2027` only after validated source resolution.

| Table | Fields (PK marked *) | Constraints / relationship |
|---|---|---|
| `seasons` | `season_id* TEXT`, `start_date`, `end_date`, `status`, `ruleset_version`, `source_ref` | Exactly one validated active candidate; status `active/pending/complete`; do not auto-promote an incomplete next season. |
| `teams` | `team_id* TEXT`, `canonical_name`, `short_name` | IDs never based on mutable display name. |
| `season_teams` | `season_id*`, `team_id*` | Composite PK; FK seasons/teams; exactly 20 for eligible EPL season. |
| `team_source_ids` | `provider`, `external_id`, `team_id`, `valid_from`, `valid_to` | FK teams. Each provider/external ID can have multiple non-overlapping validity ranges with inclusive endpoints and optional open bounds. Resolution requires one mapping valid on the requested date and a club on the season roster. |
| `fixtures` | `fixture_id*`, `season_id`, `provider`, `provider_fixture_id`, `home_team_id`, `away_team_id`, `kickoff_utc`, `matchweek`, `status`, `home_goals NULL`, `away_goals NULL`, `updated_at`, `rescheduling_source_ref NULL` | Season-roster FKs; unique `(provider, provider_fixture_id)` and `(season_id, home_team_id, away_team_id)` for normal double round robin; `status` enum scheduled/postponed/cancelled/finished; scores nonnegative integers only when finished. Exactly 380 validated active fixtures. |
| `ingestion_runs` | `run_id*`, `started_at`, `completed_at`, `source`, `input_hash`, `status`, `error_code` | Internal audit only; do not publish raw API responses by default. |
| `model_runs` | `model_run_id*`, `season_id`, `input_hash`, `model_version`, `config_hash`, `seed`, `simulation_count`, `data_cutoff_at`, `generated_at`, `status` | One reproducible model run per accepted input/config fingerprint. |
| `forecast_teams` | `model_run_id*`, `team_id*`, `actual_rank`, `played`, `actual_points`, `expected_points`, `predicted_rank`, `title_probability`, `relegation_probability`, `position_probabilities_json` | Composite PK; position probabilities array length 20 sums to 1 per team; unique predicted rank per run. |
| `backtest_runs` | `backtest_id*`, `model_version`, `season_id`, `cutoff_policy`, `train_period`, `test_period`, `sample_size`, `baseline_name`, `model_brier`, `baseline_brier`, `limitations_json` | Retrospective only; never placed in live forecast history. |

An out-of-season kickoff requires a reviewed official amendment matching the season ID, canonical fixture ID, and exact UTC kickoff. Store its source reference with the fixture and apply the normal fixture validation.

Additional integrity: no home=away, exactly one home and one away meeting per pair, unique source fixture IDs, no duplicate completed results, every team has 38 scheduled fixture slots, match status changes are handled deterministically. Check cancelled/replaced matches against a validated canonical inventory instead of blindly counting both. Official deductions or other league adjustments require explicit season-specific input; absent reliable adjustment data, block publication rather than silently assume zero.

**JSON persistence:** `public/data/seasons/{season_id}/snapshots/{snapshot_id}.json` immutable; `public/data/seasons/{season_id}/latest.json` manifest; `public/data/seasons/{season_id}/history.json` ordered index; `public/data/backtests/{backtest_id}.json`; `public/data/index.json` global active-season pointer. `snapshot_id` is a short SHA-256 digest of canonical input/config identity (not current wall-clock time). Keep all published snapshots in Git; do not rely on temporary CI artifacts or hosting deployment retention for history.

## 5. Static API contract (HTTP GET, no dynamic server)

All paths are relative to site origin; `application/json`; `schema_version: 1`; no auth. Browser requests use same-origin URLs. No POST/PUT/DELETE routes, no server-side query execution, and no CORS dependency. Optional UI selectors filter locally.

| GET route | Input | Output / behavior |
|---|---|---|
| `/data/index.json` | none | `{schema_version, active_season_id, display_season_id, season_status, latest_manifest_path}`; `active_season_id` nullable in offseason; `display_season_id` retains last valid. |
| `/data/seasons/{season_id}/latest.json` | validated season path segment | `{schema_version, season_id, snapshot_id, snapshot_path, input_hash, generated_at, data_cutoff_at, model_version}`. Short cache TTL. |
| `/data/seasons/{season_id}/snapshots/{snapshot_id}.json` | IDs obtained from manifests, not arbitrary user paths | `{schema_version, season_id, snapshot_id, generated_at, data_cutoff_at, simulation_count, model_version, representative_table_method, teams:[...]}`. Immutable and long-cacheable. |
| `/data/seasons/{season_id}/history.json` | season ID | `{schema_version, season_id, snapshots:[{snapshot_id, generated_at, data_cutoff_at, matchweek_label, path}]}`; launch-forward only. |
| `/data/backtests/index.json` | none | List of evaluated seasons, model versions and report paths. |
| `/data/backtests/{backtest_id}.json` | ID from index | `{schema_version, season_id, model_version, training_period, cutoff_policy, sample_size, baseline, multiclass_brier_model, multiclass_brier_baseline, limitations}`. |

Example forecast team record:

```json
{
  "team_id": "team-example",
  "name": "Example FC",
  "actual_rank": 8,
  "played": 19,
  "actual_points": 28,
  "expected_points": 54.7,
  "predicted_rank": 7,
  "title_probability": 0.014,
  "relegation_probability": 0.031,
  "position_probabilities": [0.014, 0.02, 0.03, 0.04, 0.06, 0.08, 0.1, 0.11, 0.1, 0.09, 0.08, 0.07, 0.06, 0.045, 0.035, 0.025, 0.02, 0.011, 0.007, 0.003]
}
```

*Illustrative only: example position probabilities are not a validated forecast and must not be used as test fixture for probability-sum assertions.* API consumers validate JSON schema and render a friendly unavailable state on malformed/missing data. Cache `index/latest/history` conservatively (`no-cache` or short max-age); hashed snapshots can be `immutable`. Because static deployment and browser caching can temporarily disagree, resolve `snapshot_path` from the latest manifest, retry once on 404, and fall back to the last successfully loaded snapshot in memory. No service worker in MVP.

## 6. Forecasting and evaluation requirements

1. **Data availability:** ingest all historical games used for training with match dates, team identities, and final scores; use only matches available before each evaluation cutoff. Current fixtures must include 380 distinct EPL games and complete statuses.
2. **Baseline model:** implement deterministic Elo strength updates on chronologically ordered completed games; use a documented Poisson goals model with home advantage and bounded parameters to produce home/draw/away and scoreline probabilities. Start with simple fixed hyperparameters; avoid a complex optimizer until backtests justify it.
3. **Simulation:** for each of 10,000 seeded trials, preserve completed scores and sample scores only for remaining fixtures; recompute full league table using season-specific official rules. Stream/chunk trials to avoid storing 10,000 × 380 detailed scoreline records. Record 20×20 position frequencies and aggregate points online.
4. **Tie rules:** implement validated EPL points, goal difference, goals scored, and applicable head-to-head/other ranking criteria for the season. If official rules require a playoff or other undecidable tie resolution, do **not** silently use alphabetical order or arbitrary coin flips; record a defined and documented handling policy only after rules verification, or block publication for unresolved cases. Deterministic stable IDs may break ties in the **assignment optimization**, not replace official league rules.
5. **Representative table:** maximize `sum(position_probability[team, assigned_rank])` subject to one team per rank, using `scipy.optimize.linear_sum_assignment` on negated weights. For exact multiple optima, implement deterministic lexicographic selection by stable team ID, verified by explicit tests (do not assume SciPy's default tie behavior is contractual). Do **not** sort expected points to choose ranks.
6. **Probabilities:** title = simulated first-place frequency; relegation = simulated bottom-three frequency; calculate from unrounded counts. Display rounded percentages to one decimal; totals should be asserted on unrounded values.
7. **Backtests:** evaluate at chronological cutoffs from at least one completed season. At each cutoff refit/update using prior-known data only; compare 1X2 match probabilities to empirical home/draw/away frequency baseline on identical future test matches. Report multiclass Brier definition (sum of three squared probability errors, optionally divided by 3—choose and disclose one convention), sample count, dates, model version, baseline and limitations. Final-rank MAE optional only for completed seasons.
8. **Reproducibility:** pin Python packages, RNG seed, source file hashes, canonical sorting, model/config versions and output schema. Document that results can change when historical data is corrected or software versions change.

## 7. Repository layout

```text
premier-league-intelligence/
├── README.md
├── docs/
│   ├── PRD.md
│   ├── TRD.md
│   ├── DATA_SOURCES.md
│   └── METHODOLOGY.md
├── frontend/
│   ├── package.json
│   ├── package-lock.json
│   ├── index.html
│   ├── vite.config.ts
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── components/{ForecastTable,ProbabilityHistory,StatusBanner}.tsx
│   │   ├── pages/{Dashboard,Methodology}.tsx
│   │   ├── lib/{dataClient,types,format}.ts
│   │   └── styles/
│   └── public/data/                  # generated, validated JSON only
├── engine/
│   ├── pyproject.toml
│   ├── requirements.lock             # pinned resolved dependencies
│   ├── src/pl_intel/
│   │   ├── sources/{openfootball,football_data}.py
│   │   ├── {normalize,validate,db,season,elo,poisson,simulate,ranking,assignment}.py
│   │   ├── {backtest,schemas,publish,cli}.py
│   │   └── rules/                    # verified season-specific rules
│   └── tests/{unit,integration,fixtures}/
├── .github/workflows/{ci,forecast}.yml
├── .gitignore
└── .env.example                     # placeholder names only
```

The tree is a conceptual target; create folders incrementally. Avoid duplicate `public/data` roots: the authoritative static artifact directory is `frontend/public/data`. SQLite files, API raw dumps, caches, `.env`, and virtual environments are ignored; checked-in JSON snapshots are explicitly permitted **only after rights review**. GitHub Actions commits generated artifacts through a dedicated restricted workflow; site builds from that commit.

## 8. Authentication and authorization

**Visitors:** no authentication, sessions, JWT, OAuth, user records, or privileged browser routes. All published forecast JSON is public. **Maintainer:** GitHub account with MFA and repository permissions; GitHub Actions `workflow_dispatch` restricted by GitHub repository access. **Automation:** short-lived `GITHUB_TOKEN` with `contents: write` only on the publishing job, `contents: read` elsewhere; do not use personal access tokens unless strictly unavoidable. Hosting deployment integration uses least privilege. Branch protection/review rules are recommended where supported free of charge. No API secret is shipped to the browser.

## 9. Security, data integrity, and abuse controls

- Store `FOOTBALL_DATA_API_TOKEN` in GitHub Actions repository secret; never in `VITE_*` variables, frontend source, JSON, screenshots, CI output or git history. `.env.example` contains names only. Rotate immediately if exposed.
- HTTP requests: HTTPS only, fixed provider base URLs, connect/read timeouts, max response size, capped retries with exponential backoff and 429 `Retry-After`; enforce <=10 calls/minute (prefer substantially fewer). Never blindly loop over IDs.
- Validate external JSON types, timestamps, status enums, goals, unique IDs, team mappings, complete 380-fixture inventory and allowable season boundaries. Reject and alert on unexpected schema/roster/rule changes. Treat remote names as untrusted text; React escapes text by default, never render unsanitized HTML.
- Limit public abuse surface by having **no writable endpoints**. Browser requests use CDN caching; rate limiting is provider-managed static-host protection, not an app-level API quota. Avoid enabling Cloudflare Workers/functions just to add rate limiting.
- Pin GitHub Actions to reviewed commit SHAs where practical, lock Python/npm dependencies, run `npm audit --omit=dev` and `pip-audit` (report and triage; do not auto-install incompatible updates), enable free Dependabot alerts if available, and restrict workflow token permissions. Do not execute arbitrary data-source scripts.
- Publishing job must stage, schema-check, verify invariant totals and references, then publish all-or-nothing. No partial updates on failed fetch, validation, model, or deploy steps. Keep previous snapshot untouched.
- Data provenance records provider, license review date, source dataset versions/hashes, and cutoff. Do not republish raw API payloads, team crests or third-party graphics without explicit rights.

## 10. Testing and quality gates

| Layer | Free tool | Required checks |
|---|---|---|
| Data/unit | pytest | mapping aliases, duplicate/missing fixtures, bad scores, postponed/cancelled statuses, 20 clubs, 380 fixture inventory, offseason transitions. |
| Model/unit | pytest + NumPy/SciPy | Elo updates, Poisson 1X2 sum, fixed-seed reproducibility, completed season, partial season, postponed games, all official tie scenarios, probability sums, assignment uniqueness and deterministic ties. |
| Integration | pytest + local SQLite + frozen JSON fixtures | source normalization → DB → simulation → schema-valid snapshot; no real API required for CI tests. |
| Backtest | pytest + fixed historical fixtures | no future leakage; same observations for model/baseline; Brier calculation and sample size; stable repeat run. |
| Publish | pytest + temp directories | same input gives no duplicate; changed input creates new snapshot; failed run never modifies latest; manifest references existing files. |
| Frontend | Vitest + React Testing Library | all 20 rows, percentages, timestamps, loading/error/stale states, empty history, season pending state, accessibility semantics. |
| E2E | Playwright Chromium on CI only | desktop/mobile dashboard, keyboard navigation, history selector, methodology, static JSON 404 fallback. |
| Static checks | Ruff, `tsc --noEmit`, ESLint, `npm run build` | lint, types, bundling, bundle-size budget, no committed secrets. |
| Deployment | curl / scripted smoke test | HTTPS, index and latest manifest, 20 teams, valid JSON, public access, freshness labels; smoke test deployed URL before calling launch complete. |

Run fast unit/type checks on each PR; full simulations/backtests in scheduled/manual CI or scoped release checks. Do not require live API tokens for pull-request tests. Accessibility target: WCAG 2.2 AA, 320px responsive, semantic headers, visible focus, chart data as text. Performance: test a production build on throttled 4G; record observed results and adjust bundle/lazy loading.

## 11. Deployment and environment configuration

**Preconditions:** validate the no-card signup path for the chosen host and football-data.org; verify license/redistribution and source coverage. If any fail, stop and document the blocker.

1. Create **public** GitHub repository with main branch, secrets, and workflows. Do not commit `.env`, SQLite, raw licensed data, or credentials.
2. In `forecast.yml`, run weekly UTC cron and `workflow_dispatch`; `concurrency: {group: forecast-publish, cancel-in-progress: false}`; standard `ubuntu-24.04`; `timeout-minutes` capped (e.g. 30); `permissions: contents: write` only for the job committing validated artifacts. On no changed input exit successfully without commit. API failures exit nonzero without touching published data.
3. Configure Cloudflare Pages to build from main: **root directory** `frontend`, **build command** `npm ci && npm run build`, **output directory** `dist`; use free `*.pages.dev` domain. Because the forecast workflow pushes JSON commits, verify Pages builds those commits; do not depend on `GITHUB_TOKEN` pushes triggering other GitHub workflows. If Cloudflare Git integration cannot meet the workflow, choose a supported deploy trigger with no additional paid services.
4. Fallback GitHub Pages: `npm ci && npm run build` in Actions, deploy `frontend/dist` with official Pages deployment action and appropriate base path in Vite (`/repo-name/` for project sites). Confirm public-repo Pages eligibility and applicable limits.
5. On launch, inspect site JSON and compare against validated model run. Test a failed refresh and a manual rerun; previous forecast must remain visible.

| Variable | Where | Secret? | Purpose |
|---|---|---|---|
| `FOOTBALL_DATA_API_TOKEN` | GitHub Actions secret | Yes | Provider authentication, batch only. |
| `PYTHONHASHSEED` | Workflow env | No | Determinism aid; explicit RNG seed remains mandatory. |
| `FORECAST_SEED` | Workflow env / config | No | Default deterministic seed, e.g. `42`. |
| `SIMULATION_COUNT` | Workflow env / config | No | Default `10000`; production cannot silently reduce count. |
| `FORECAST_STALE_HOURS` | Frontend build config | No | UI age-warning threshold; not a claim of upstream freshness. |
| `VITE_BASE_PATH` | Frontend build config | No | `/` on Cloudflare, `/repo-name/` on GitHub Pages. |
| `GITHUB_TOKEN` | Auto-provisioned Actions token | Yes, managed | Restricted commit/deploy permissions. |

**Important:** Never set `VITE_FOOTBALL_DATA_API_TOKEN`. `VITE_*` variables are embedded in client bundles. Prefer non-secret configuration files for model parameters; keep a single documented configuration source.

## 12. Architectural decisions, rejected alternatives, and risks

| Decision | Why | Rejected alternative / consequence |
|---|---|---|
| Static JSON instead of REST server | Zero idle compute and no browser-side secret. | FastAPI/Node server requires persistent hosting and runtime quotas; no live API in MVP. |
| Batch SQLite instead of hosted database | Small dataset, no daemon, easy testing. | Postgres/Supabase/Firebase add auth, quotas, external services and potential billing; Git history is snapshot persistence. |
| GitHub Actions simulations | Keeps 8 GB laptop free. | Local full backtests and Docker stack risk memory pressure. |
| Elo + Poisson before advanced ML | Interpretable, reproducible, cheap and testable. | Neural nets/XGBoost/LLMs increase complexity and don't guarantee calibrated probabilities. |
| Fixed 10k Monte Carlo | Consistent baseline and manageable batch work. | Per-visitor simulation would require backend and amplify cost. |
| Max-weight assignment | Required valid unique predicted table. | Expected-points sorting violates approved PRD semantics. |
| Immutable launch-forward history | Honest as-published predictions. | Retrofitting prelaunch snapshots from today's model misrepresents historical forecasts. |
| Cloudflare Pages with GH Pages fallback | Portable static assets. | Workers, serverless functions, paid CDN, custom domain unnecessary. |

### PRD feasibility challenges and explicit resolutions

1. **Weekly update is not the same as after every matchweek.** Fixtures are postponed, and cron can be delayed. Implement weekly checks + manual reruns, publish when completed-match fingerprint changes, and measure the PRD's 24-hour objective relative to the configured check **when source data is available**. Do not promise instantaneous post-matchweek publication.
2. **A complete 380-fixture inventory may not exist early in the season or during an offseason.** Publication must wait or show last valid season/pending state. Do not invent missing fixtures.
3. **Historical training and backtesting need legally reusable dated match results.** Free current API is not a guaranteed historical archive. Validate OpenFootball coverage and the precise license of files actually consumed; verify whether provider terms permit publishing derived probabilities and any fixture metadata.
4. **Official EPL tie resolution is not always a trivial points/GD/goals sort.** Maintain verified season-specific rules; ambiguous unresolved ties can block publication rather than introduce false precision.
5. **Forecast 'accuracy' is not proven by running simulations.** A simple model may underperform the frequency baseline; publish measured Brier results honestly and do not promise superior performance.
6. **'100% free' does not mean unlimited usage.** Free tiers can throttle or suspend service; never enable overage billing, and keep an alternate static host. Six weeks is conditional on passing gates, not a guarantee.
7. **History persistence through Git can grow.** At one snapshot per weekly changed-data run, sizes should be modest; enforce file budgets and monitor repository growth. Hosting deployment retention is not archival storage.
8. **Static site cannot know the last attempted refresh in real time.** A failure can preserve prior valid data, and the UI can show age-based stale warning, but a precise live 'pipeline failed' status requires separately published operational state and is not required for MVP.

### Cost and laptop RAM guardrails

- **Potential charges:** private-repo Actions compute overage, larger runners, paid Cloudflare upgrades, Workers/R2, purchased domains, paid football-data.org plans, and paid GitHub features. **Prevention:** public repository, standard runners only, free static hosting, no card/payment method, no auto-upgrades, monitor account settings and quotas monthly.
- **Potential throttling (not necessarily billing):** API 429 at 10 calls/minute, Pages build/file quotas, GitHub Pages bandwidth soft cap, GitHub Actions policy/limits. **Prevention:** one batched fixture request where possible, exponential backoff, weekly cron, small JSON, no aggressive polling, skip no-change deploys.
- **Potential high RAM:** full historical DataFrames, storing all simulation trajectories, Playwright browsers, parallel Python workers, local Docker/DB, multiple Vite servers. **Prevention:** `sqlite3` cursors, NumPy arrays/chunks, online aggregates, single-process simulations, browser E2E only in CI, no Docker, run local frontend or small Python tests—not both heavy suites concurrently.
- **Potential disk/repo growth:** Git-tracked history, raw JSON caches, Python/npm dependencies, Playwright browsers. **Prevention:** commit only compressed-in-transit lean forecast JSON, ignore caches/DBs, lazy-load historical snapshots, periodically check asset count and Git size without deleting immutable public history.

## 13. Suggested build order: 10 small Codex tasks

Each task is a separate reviewable PR/commit; give Codex only the listed scope, require tests, and avoid simultaneous cross-layer refactors.

1. **Feasibility report:** create `docs/DATA_SOURCES.md` with verified current-season fixture count, historical seasons, license/redistribution evidence, official tie rules and no-card hosting; mark gates pass/fail. **Done:** no unsupported assumptions; blocked gates explicitly recorded.
2. **Repo scaffold:** initialize Vite React TS, Python package, pytest/Vitest/Ruff, CI, `.gitignore`, `.env.example`. **Done:** local dev server works; CI runs basic tests without secrets.
3. **Canonical schema and ingestion:** build teams/season/fixture SQLite schema and frozen OpenFootball/API adapters. **Done:** 20-team/380-fixture tests, alias mapping and malformed-data rejection pass.
4. **Active-season resolver and rules:** validate provider season metadata, offseason pending behavior, and verified ranking rules. **Done:** transition and tiebreak fixture tests pass.
5. **Elo/Poisson baseline:** chronological strength updates and 1X2 probabilities. **Done:** probability normalization, determinism and no-future-input unit tests pass.
6. **Monte Carlo + representative table:** 10k seeded simulations, official ordering, 20×20 position matrix, deterministic max-weight assignment. **Done:** 380-fixture accounting, title/relegation totals and uniqueness tests pass.
7. **Chronological backtest:** one complete historical season, baseline comparison, Brier report. **Done:** cutoff audit and same-sample baseline/model assertions pass.
8. **JSON contracts and safe publisher:** generate validated snapshots, history index, active-season index and latest manifests. **Done:** no-change idempotency and failed-run rollback tests pass.
9. **Accessible dashboard:** predicted table, title/relegation percentages, club history, methodology/backtest view and pending/stale states. **Done:** responsive 320px, unit tests and CI browser smoke pass.
10. **Weekly automation + free deploy:** cron/manual Actions, protected secrets, Cloudflare Pages or GH Pages, post-deploy smoke checks. **Done:** public URL, no-card billing audit, verified end-to-end refresh, failed-refresh preservation, all PRD F1–F5 gates met.

**Release rule:** If task 1 fails a hard feasibility gate, stop public launch and fix the dependency before implementing production publishing. Do not compensate by paying for a service or weakening data integrity.

## 14. Official references and verification register

Provider limits are time-sensitive; recheck at deployment. Reviewed 2026-11-23:

- GitHub Actions billing: https://docs.github.com/en/actions/concepts/billing-and-usage
- Standard runner specifications: https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- Cloudflare Pages limits: https://developers.cloudflare.com/pages/platform/limits/
- GitHub Pages limits: https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits
- football-data.org pricing: https://www.football-data.org/pricing
- football-data.org API policy: https://docs.football-data.org/general/v4/policies.html
- football-data.org signup terms: https://www.football-data.org/client/register

**Not yet verified:** actual account-level no-card registration, current-season 380-fixture completeness, OpenFootball exact file licenses/coverage, provider derived-data redistribution rights, and the official season-specific tie rules. These remain launch-blocking gates, not assumed facts.
