# Product Requirements Document — Premier League Intelligence Platform

**Version:** 1.1 (MVP — approved decisions)  
**Status:** Finalized product scope; data feasibility pending  
**Target delivery:** 6 weeks at approximately 2 hours/day, subject to data-source feasibility  
**Primary audience:** Premier League fans  
**Scope:** English Premier League only; automatically select the active season

## 1. Overview

Premier League Intelligence Platform is a free, public website that forecasts the final Premier League table, each club's probability of winning the title, and its probability of relegation. Forecasts use completed match results and simulated remaining fixtures, refresh after each matchweek, and retain earlier snapshots so fans can see how expectations change. Fans currently lack a simple, transparent way to compare season-long probabilities over time; this product prioritizes interpretable predictions and clearly labeled uncertainty rather than claiming to know the final outcome.

## 2. Target users

- **Primary:** Premier League fans who want to compare all 20 clubs' likely final positions and title/relegation chances without creating an account.
- **Secondary:** Data-curious fans who want to inspect prior forecasts and understand whether the model has performed well historically.

**Needs:** A complete league-wide forecast, a visible data-cutoff timestamp, clear percentage probabilities, historical comparisons, and a plain-language explanation of model limitations.

## 3. Goals and success criteria

| Goal | MVP success criterion |
|---|---|
| Complete forecasts | Show all 20 active clubs with predicted rank based on finishing-position probabilities, projected points, title probability, and relegation probability. |
| Mathematically consistent outputs | Title probabilities sum to 100% ±0.5 percentage points; relegation probabilities sum to 300% ±0.5 points; each probability is within 0–100%. |
| Correct simulation | Every simulated season contains all 380 fixtures exactly once (completed actual results plus simulated remaining fixtures); league ordering uses official tiebreak rules. |
| Weekly refresh | Scheduled job checks match completion at least once weekly; publishes a new snapshot within 24 hours of the configured matchweek-completion check when source data is available. |
| Historical transparency | Display at least two distinct forecast snapshots when two matchweek updates exist; do not fabricate historical snapshots. |
| Evaluation | Run leakage-free, time-ordered backtests on at least one completed historical season; publish the evaluated season, sample size, baseline, Brier score, and limitations. |
| Reliable publishing | Failed fetches or invalid results leave the previous valid forecast online and display its original timestamp. |
| Zero cost | Public deployment and scheduled pipeline operate with no paid services, payment-card-required trials, or heavy local infrastructure. |

**Launch gate:** If legal usage, data completeness, or the no-card hosting requirement cannot be verified, do not claim a production-ready launch; resolve or replace the dependency first.

## 4. User stories

1. As a **fan**, I want to see the predicted final league table so that I can understand where each club is expected to finish.
2. As a **fan**, I want to see each club's title probability so that I can compare championship contenders.
3. As a **fan**, I want to see each club's relegation probability so that I can assess the relegation battle.
4. As a **fan**, I want to know when the forecast was generated and which matches it includes so that I can judge its freshness.
5. As a **fan**, I want to compare forecasts from different matchweeks so that I can see how expectations have changed.
6. As a **data-curious fan**, I want to see historical evaluation metrics and an explanation of uncertainty so that I can judge the model's reliability.
7. As a **site maintainer**, I want scheduled updates and validation checks so that forecasts update without manual editing and invalid data cannot overwrite valid output.

## 5. MVP features and acceptance criteria

### F1. Predicted final league table

Display the active season's 20 clubs, current points and rank, projected final points, and predicted final rank. Compute each team's probability of finishing in each position (1–20) from simulated tables. The displayed **predicted table** must be a valid one-team-per-position assignment maximizing the sum of the assigned finishing-position probabilities across all clubs (a maximum-weight assignment); break equally optimal assignments deterministically by stable team ID. This is the most-likely-position-based *representative table*, **not** the single most probable complete table, and an individual team's assigned rank need not equal its own highest-probability rank when clubs compete for the same position. Do not sort by expected points to choose predicted ranks. Display expected points as a separate statistic and clearly distinguish projected values from actual standings.

- [ ] Exactly 20 unique clubs from the validated active-season roster appear.
- [ ] Current standings reflect completed matches only, including postponed-match differences in games played.
- [ ] Projected points are derived from simulations and are not below actual earned points.
- [ ] Predicted ranks are unique from 1 to 20, computed by maximum-weight assignment from finishing-position probabilities; deterministic tie handling is tested.
- [ ] The UI explains that the representative predicted table may differ from expected-points order and that individual modal ranks may conflict.
- [ ] Forecast identifies season, generation timestamp, data cutoff, and simulation count.
- [ ] The active season is resolved from a validated competition calendar and source metadata, not a hardcoded season string; transitions between seasons are covered by tests.
- [ ] If the next season is not yet confirmed or its fixture inventory is incomplete, keep the last valid season visible with a clear status rather than silently selecting an invalid season.
- [ ] Desktop and mobile layouts show all essential fields without overlapping text.

### F2. Title and relegation probabilities

For each simulation, award title to the club ranked first and relegation status to the clubs ranked 18–20 after applying league tiebreakers. Aggregate results across a default of 10,000 simulations, using a fixed seed for reproducibility.

- [ ] Every club displays title and relegation probabilities rounded to one decimal place.
- [ ] Underlying unrounded title probabilities sum to 1 and relegation probabilities sum to 3 (within floating-point tolerance).
- [ ] Each club's probability is between 0 and 1 inclusive.
- [ ] A 100%/0% outcome is allowed when mathematically determined; otherwise probabilities are labeled as estimates.
- [ ] Simulation tests cover a completed season, an unfinished season, postponed matches, and tiebreak cases.

### F3. Weekly automated forecast updates

Use a scheduled GitHub Actions workflow in a **public GitHub repository** to fetch current fixtures/results, validate inputs, update team-strength estimates, simulate the remaining season, and publish static JSON artifacts. The job checks whether relevant match results are available; it does not assume that every club has played the same number of matches or that a calendar week equals a complete matchweek.

- [ ] Schedule runs at least weekly and supports a manually triggered rerun through GitHub Actions (`workflow_dispatch`); a manual rerun follows the same validation and publishing rules.
- [ ] The job identifies the active season at runtime, and a season change creates a separate season namespace without overwriting prior-season snapshots.
- [ ] Fetch and processing are idempotent: unchanged data does not create duplicate snapshots.
- [ ] Match IDs and statuses distinguish scheduled, postponed, cancelled, and finished fixtures.
- [ ] Pipeline rejects missing/duplicate clubs, duplicate fixture IDs, invalid scores, and incomplete fixture inventories.
- [ ] API credentials exist only in repository secrets; never in public JSON, logs, or frontend code.
- [ ] On source/API failure, previously published forecast remains available and no false freshness timestamp is shown.
- [ ] Each successful changed-data run produces a versioned snapshot and updates a `latest` manifest.

### F4. Prediction history

Provide a simple matchweek selector and a chart or table showing how a chosen club's title and relegation probabilities changed between stored snapshots. **Public prediction history begins at launch**, with no reconstructed pre-launch forecasts mixed into that timeline. Historical backtests are displayed separately and labeled as retrospective evaluations.

- [ ] Users can select any club and available historical snapshot.
- [ ] Each snapshot displays its actual generation time and match-data cutoff.
- [ ] History is sourced from immutable saved forecasts beginning at launch, not reconstructed from today's model.
- [ ] Retrospective backtest results are stored and labeled separately from live, as-published forecast history.
- [ ] No duplicate snapshot is saved when input results have not changed.
- [ ] If fewer than two snapshots exist, display an explicit “History will appear after the next update” message.

### F5. Accuracy and methodology

Publish a concise methodology and evaluation page. Backtest with chronological splits: at each historical cutoff, use only data known at that time. For match-outcome probabilities, compare against a documented simple baseline (for example, home/draw/away historical frequencies) using multiclass Brier score; optionally show final-rank mean absolute error on completed historical seasons.

- [ ] At least one completed season is backtested with no future-result leakage.
- [ ] Report dataset period, cutoff policy, sample size, model version, baseline, and Brier score.
- [ ] Report the model and baseline on the same test observations.
- [ ] Explain that Monte Carlo probabilities are estimates, not guaranteed outcomes.
- [ ] Explain input limitations, source delays, and how predicted rank (maximum-weight assignment of finishing-position probabilities) and projected points (simulation mean) are calculated.
- [ ] Do not show a live-season final-rank accuracy score before the season finishes.

## 6. Non-goals (excluded from MVP)

- Player-level goals, assists, top scorer, or top assist forecasts.
- Live scores, in-play odds, real-time updates, or betting recommendations.
- What-if simulations, fantasy football, or transfer/injury prediction.
- Login, profiles, notifications, email collection, or personalized accounts.
- Other football leagues, native mobile apps, chatbots, or local LLMs.
- Paid APIs, paid hosting, payment-card-required trials, Docker, Kubernetes, Spark, or an always-on backend.

## 7. Primary user flow

1. Fan opens the public website; no login is required.
2. Homepage shows the active season, latest forecast timestamp, data cutoff, and predicted final table.
3. Fan scans each club's projected points, title odds, and relegation odds.
4. Fan selects a club to view its probability history across available matchweeks.
5. Fan opens the methodology/accuracy section to see backtest metrics, baseline comparison, and limitations.
6. On the next successful matchweek update, the site serves a new snapshot while retaining earlier ones.

**Failure flow:** If updates fail, visitors continue seeing the last valid forecast with its original timestamp and a stale-data notice when applicable.

## 8. Non-functional requirements

### Performance and resource use

- Static frontend and precomputed JSON; no server-side inference per page visit.
- Target first useful dashboard content within 3 seconds on a typical 4G connection, measured in production using browser performance tools; report measured results rather than guaranteeing network speed.
- Target compressed initial frontend assets plus latest forecast JSON at or below 500 KB; load history snapshots on demand.
- Local development must run on 8 GB RAM without Docker, a local database server, or concurrent heavy jobs.
- Run scheduled data processing and simulations on free standard GitHub-hosted runners; SQLite is a temporary batch artifact, not a hosted database service.

### Privacy and security

- No accounts, cookies for tracking, or collection of personal data in the MVP.
- HTTPS hosting; no API secrets in browser bundles, public repositories, or logs.
- Pin CI dependency versions, restrict workflow permissions to minimum required, and validate external data before publishing.
- Publish only permitted derived data; document data provenance and comply with provider terms.

### Accessibility

- Keyboard-operable navigation, selectors, and links.
- Semantic table headers, visible focus indicators, and text alternatives for charts.
- Target WCAG 2.2 AA contrast and responsive layouts at 320 px viewport width and above.
- Probabilities must be readable as text, not communicated by color alone.

### Free-tier and deployment controls

- **GitHub Actions:** Use standard runners in the **approved public repository**, where standard Actions usage is free subject to GitHub policies and quotas; do not use paid larger runners or billable extras. Configure both a weekly cron schedule and a manual `workflow_dispatch` trigger.
- **Cloudflare Pages:** Candidate free static hosting; verify current build, file, and account/signup limits and absence of card requirement before adoption. Use GitHub Pages as a fallback after verifying its applicable limits.
- **football-data.org:** Candidate free API; verify competition access, historical coverage, delayed-score policy, rate limits, redistribution terms, and whether signup requires a payment card.
- **OpenFootball:** Candidate historical data; verify the specific season files, CC0 licensing, completeness, and update freshness.
- Do not enable any optional paid features, paid overages, or services that require payment credentials.

## 9. Risks, assumptions, and mitigations

| Risk or assumption | Impact | Mitigation / decision gate |
|---|---|---|
| Free current-season results are incomplete or delayed | High | Validate fixture inventory and completed-match coverage before training or launch; show data cutoff. |
| Historical results and current fixtures disagree on club identities | High | Stable canonical team IDs and mapping tests; reject unmapped clubs. |
| Data usage or redistribution is not permitted | High | Review current terms; switch to licensed alternatives or do not launch. |
| Probability model is poorly calibrated | High | Compare against simple baselines with chronological backtests; report limitations. |
| Official tiebreak rules are implemented incorrectly | Medium | Maintain season-specific rule configuration and deterministic tests. |
| Matchweek scheduling includes postponements | Medium | Use fixture completion states and data changes rather than a fixed equal-games assumption. |
| Scheduled workflow fails or is delayed | Medium | Preserve previous forecast, retry manually, and display timestamp/stale notice. |
| Free service limits or signup conditions change | Medium | Keep output as portable static files and retain an alternate host. |
| Six-week schedule is insufficient | Medium | Prioritize F1–F3, then F4–F5; do not expand MVP scope. |

**Assumptions to verify:** At least one completed historical season can be obtained with adequate legal rights; the active season has a complete 380-fixture inventory; a free no-card deployment path remains available; weekly checks and maintainer-initiated manual reruns are sufficient for fans.

## 10. Future ideas (not MVP commitments)

- Top scorer and assist forecasts, conditional on legally usable player-level data.
- Best attacking/defensive team projections and expected-goals analysis.
- What-if result simulator, top-four/European qualification odds, and match previews.
- Injury, transfer, and lineup-aware models when reliable data becomes available.
- More advanced calibrated models and SHAP explanations after baseline evaluation.
- Other leagues, user preferences, and optional notifications.

## 11. Delivery approach for Codex

Build in small, independently testable increments, each with explicit acceptance tests:

1. **Data feasibility gate:** validate legal access, complete fixtures, historical data, and no-card hosting.
2. **Data normalization:** canonical teams, fixtures, results, and validation tests.
3. **Model baseline:** Elo/Poisson probabilities with chronological evaluation.
4. **Season simulator:** full fixture accounting, tiebreak rules, seeded Monte Carlo tests.
5. **Static forecast contract:** schema, versioned snapshots, `latest` manifest, idempotency.
6. **Frontend:** table, probability views, history, methodology, responsive/accessibility tests.
7. **Automation/deployment:** weekly workflow, failure preservation, public static hosting, smoke tests.

**Definition of done:** All F1–F5 acceptance criteria pass; no secret leaks; no paid/card-required dependency; production website is accessible; forecast provenance and limitations are visible.

## 12. Confirmed product decisions

1. **Season:** Automatically target the active Premier League season; verify season boundaries and fixture readiness.
2. **Repository:** Public GitHub repository.
3. **Prediction history:** Start immutable published history at launch; show retrospective historical backtests separately.
4. **Predicted table:** Use finishing-position probabilities and a unique-rank maximum-weight assignment; do not rank by expected points.
5. **Updates:** Weekly scheduled check plus maintainer-triggered manual rerun.

## 13. Remaining implementation validation gates (not product-choice questions)

- Confirm the selected free data sources provide the required current-season fixture inventory and permitted historical use.
- Confirm the selected static host can be registered and deployed without a payment card, and its free quotas are sufficient.
- Confirm the exact active-season resolution policy for off-season gaps against the chosen source's season metadata; show an explicit unavailable/pending state rather than inventing a season.
- Confirm source and official rules support season-specific tiebreak configuration; block publication if rules cannot be applied correctly.

No additional product-owner choices are required to start the data-feasibility task.
