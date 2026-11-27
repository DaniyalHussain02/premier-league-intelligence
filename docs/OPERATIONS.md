# Forecast workflow operations

T03 and G5b block production publication. The weekly workflow verifies the pipeline with synthetic inputs and an isolated temporary Git repository. It has read-only permissions on GitHub, uses no provider credential, and never pushes forecast files or deploys the site. The production CLI exits nonzero while this gate is in place.

## Schedule and manual rerun

The verification runs on Tuesdays at 06:17 UTC on a standard `ubuntu-24.04` runner. GitHub can delay scheduled starts. Once the workflow is on the default branch, a maintainer can open **Actions → Forecast fixture verification → Run workflow**, select the reviewed branch, and inspect the fixture verification step. Test failures stop the run. Successful runs verify a changed update, an unchanged run, archive-backed reversion, and commit and concurrency failures against the temporary repository. Its Git commits and source fixtures stay on the runner.

To pause the schedule, choose **Disable workflow** from the workflow’s Actions menu. This also disables manual dispatch. To pause only the schedule, remove the cron trigger through a reviewed pull request.

## Stale data and API quota diagnosis

Compare the forecast's original generation time and data cutoff with the last workflow run. An age warning measures the age of the snapshot. Inspect the run conclusion and look for a no-change result, an input validation failure, or a stale prior revision before retrying. An unchanged or failed update must preserve the latest pointer and the original timestamps.

The current workflow has no provider access. After production approval, store the provider token only in GitHub Actions Secrets as `FOOTBALL_DATA_API_TOKEN` and pass it only to the trusted ingestion step. On HTTP 429, honor the provider's retry delay within the existing bounded backoff. Stop on authorization errors and inspect the configured tier before retrying. A failed fetch leaves the prior forecast in place. Branch checks and fixture runs require no provider secrets.

## Production handoff

The fixture adapter verifies prior bytes from Git blobs, retrieves an original manifest and snapshot from the same archive commit, and conditionally advances an isolated ref after constructing the complete commit. Before production use, verify the trusted destination and protected checkout, enforce all release gates at the production write boundary, and configure a separately reviewed deployment that preserves the prior site if build or deployment fails. The fixture results do not establish production publication or deployment readiness.
