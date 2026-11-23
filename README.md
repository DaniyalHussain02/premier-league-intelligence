# premier-league-intelligence

A planned public Premier League forecasting dashboard with projected standings,
title and relegation probabilities, forecast history, and historical backtests.

## Repository layout

- `frontend/` will contain the static dashboard and its approved public forecast JSON.
- `engine/` will contain the Python forecasting batch job.
- `.github/workflows/` will contain CI and forecast publishing workflows.
- `docs/` contains the [product requirements](docs/PRD.md), [technical requirements](docs/TRD.md),
  [security checklist](docs/SECURITY.md), and [task list](docs/TASKS.md).

## Cost and local development

Use free services that require no payment card. Use standard GitHub-hosted runners
in the public repository. Keep local development within 8 GB of RAM and run
full simulations and browser tests in CI. The project excludes Docker, hosted
databases, and always-on servers. Verify provider terms, quotas, and data rights
before enabling production publishing.

`.env.example` lists the batch credential name. Keep its value in GitHub Actions
Secrets for the trusted forecast job. Keep credentials, raw API responses, and
temporary databases out of Git. Publish only validated and legally permitted
forecast data.

## Verify Git hygiene

Run these commands from the repository root. The first ignore check should
report a rule for every listed path. Each of the final two checks should exit
with status 1, which means Git does not ignore the path.

```sh
git status --short
git check-ignore --no-index -v -- .env .env.local .venv/test node_modules/test frontend/dist/test engine/test.sqlite3 raw-api-dumps/test.json
git check-ignore --no-index -- .env.example
git check-ignore --no-index -- frontend/public/data/seasons/test/snapshots/test.json
```
