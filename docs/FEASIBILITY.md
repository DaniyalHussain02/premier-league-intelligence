# T03 feasibility register

Evidence reviewed 24 November 2026 (Asia/Karachi). **Production publication is
blocked while any gate below is BLOCKED.** This review used public documentation,
repository metadata, excerpts, and league publications. It made no authenticated
data requests, downloaded no production datasets, and deployed nothing.

| Check | Status | Basis |
|---|---|---|
| G1a · OpenFootball historical licensing | PASS | The pinned England repository applies CC0 1.0 to its data. |
| G1b · football-data.org free-tier EPL access, rate and delay policy | PASS | The provider advertises EPL access, 10 requests/minute for registered free clients, and delayed scores and schedules. Account access and actual coverage are tracked separately. |
| G1c · Rights to publish derived football-data.org outputs | BLOCKED | The published terms leave project-specific redistribution and retention rights unresolved. |
| G2a · Official 2026/27 roster and fixture baseline | PASS | Counted 20 distinct clubs and 380 distinct directed pairings in the league's published schedule. |
| G2b · Free API inventory, statuses and ID reconciliation | BLOCKED | No authorized season response or provider completeness report was examined. |
| G3a · Official 2026/27 normal ranking and tiebreak rules | PASS | Section C of the season handbook specifies the order and the scope of the special tiebreakers. |
| G3b · Handling exceptional rankings and authoritative adjustments | BLOCKED | Regulation scores alone cannot resolve all official ties or special decisions. A reviewed handling policy and required official inputs are outstanding. |
| G4a · Documented free, no-card static hosting path | PASS | Cloudflare explicitly advertises a no-card Pages path. |
| G4b · Project account eligibility and no-card API activation | BLOCKED | Provider documentation was reviewed. Account activation and account-level eligibility have yet to be confirmed. |
| G5a · Completed historical season candidate | PASS | The official final table confirms 2024/25 is complete, and a pinned OpenFootball season file contains dated results through Matchday 38. |
| G5b · Historical file completeness and reproducible backtest | BLOCKED | Every historical row has yet to be validated, and the chronological backtest is a later task. |

## Historical data and permitted use

OpenFootball's England repository identifies its national league data as public
domain. At commit `9ee7a0c351805c99cc334478a9bf04b7d0432fec`, the root license is
CC0 1.0 Universal and applies to the selected repository data. CC0 waives the
affirmer's rights to the extent possible. This assessment covers the
`2024-25/1-premierleague.txt` results file, whose Git blob is
`ca0fe4923f0164d0f879f796ab0759253e13bc06`. The project should record the source
and revision as provenance. Club crests and other third-party branding are
outside this assessment. [Repository README](https://github.com/openfootball/england/blob/9ee7a0c351805c99cc334478a9bf04b7d0432fec/README.md) · [Pinned CC0 license](https://github.com/openfootball/england/blob/9ee7a0c351805c99cc334478a9bf04b7d0432fec/LICENSE.md) · [Pinned season file](https://github.com/openfootball/england/blob/9ee7a0c351805c99cc334478a9bf04b7d0432fec/2024-25/1-premierleague.txt)

The league's 2024/25 final table identifies 20 clubs with 38 matches each.
OpenFootball's rendered file shows matchdays, calendar dates, named home and away
clubs, and final scores, including the final round on 25 May 2025. The league
table verifies that the competition finished. The file metadata and excerpts
establish a candidate, while exact record completeness and chronological
data availability remain open. Before using the file for a backtest, verify
380 unique final scores, a 20-club roster, date and postponed-match handling,
and totals reconciled to the official final table. Historical match dates
alone do not prove when results or corrections first became available.
[Official 2024/25 final table](https://www.premierleague.com/en/tables/premier-league/2024-25?round=L_1) · [OpenFootball final-round excerpt](https://github.com/openfootball/england/blob/9ee7a0c351805c99cc334478a9bf04b7d0432fec/2024-25/1-premierleague.txt#L595)

## Current data, access and redistribution

The football-data.org coverage page lists the English Premier League on its
free tier. Pricing lists €0 per month with delayed schedules and scores.
The public pages reviewed provide no fixed delay guarantee. The v4 policy
limits registered free clients to 10 requests per minute. Anonymous clients
are limited to 100 requests per 24 hours and can access only area and
competition lists. The public signup form asks for account details and
acceptance of terms and shows no payment fields. The terms' section 5.1
describes recurring card payments for subscription fees. Free-token issuance
without a card remains unverified. [Coverage](https://www.football-data.org/coverage) · [Pricing](https://www.football-data.org/pricing) · [API policies](https://docs.football-data.org/general/v4/policies.html) · [Registration and terms](https://www.football-data.org/client/register)

Section 7.1 requires visible attribution. Section 9.1 restricts continued
reference to sourced football data after subscription cancellation, and
section 9.2 reserves rights in graphics. The cited terms do not settle
whether the project may publish forecasts, projected standings, backtest
metrics, and immutable historical JSON in a public repository and CDN,
or retain them after suspension or cancellation. Obtain written confirmation
covering those outputs, attribution, and retention before publishing any
artifact derived from this API. Exclude raw payloads, provider credentials,
logos, and photos from published artifacts. [Registration and terms, sections 6–9](https://www.football-data.org/client/register)

## Season and fixture validation

The league's 2026/27 by-date publication is the schedule baseline. Extracting
its fixture rows with `pdftotext -layout` and comparing ordered home/away
pairs against all distinct pairs yielded **380 rows, 380 unique pairs,
20 clubs, zero duplicate pairs, zero missing pairs, and zero self-matches**.
Every club appears 19 times at home and 19 away. The league states that
fixtures can change and publishes amendment circulars. This count establishes
the league schedule baseline only. [Official by-date fixture publication](https://resources.premierleague.pulselive.com/premierleague/document/2026/06/29/37fdab52-4913-4df1-8e52-b5272f6d9a62/Premier-League-Season-2026-27-By-Date.pdf) · [Fixture announcements and amendments](https://www.premierleague.com/en/news/62625)

The v4 docs describe competition matches and teams resources, numeric resource
IDs, and a match response count. They distinguish `SCHEDULED` (provisional
date), `TIMED` (confirmed kickoff), and `FINISHED` (completed match), and
list `POSTPONED`, `CANCELLED`, `SUSPENDED`, and `AWARDED` as distinct
statuses. Documentation of those values does not verify how this season's
postponements and replacements are represented or whether a replacement
retains the original match ID. [Competition resource](https://docs.football-data.org/general/v4/competition.html) · [Match resource](https://docs.football-data.org/general/v4/match.html) · [Status and ID lookup](https://docs.football-data.org/general/v4/lookup_tables.html)

To clear G2b, obtain an authorized free-tier inventory or equivalent
provider evidence for competition `PL`, season starting 2026. Verify 20 unique
provider team IDs and 380 distinct match IDs and pairings against the league
baseline and amendments. Map provider team IDs to stable internal club IDs.
Treat the season plus ordered home and away club IDs as the fixture slot,
independent of kickoff. Reconcile postponed and replacement records to that
slot once, and hold cancelled, suspended or awarded records for review when
their official disposition is unclear. Record whether ID stability across
rescheduling is evidenced and block forecasts if any pairing, status,
or mapping remains unresolved.

## Official season-specific standings rules

Section C of the 2026/27 Handbook (published 21 October 2026), printed
pp. 117–121, specifies the following.

1. Rules C.2 and C.4–C.6 rank clubs by points (three for a win and one for a
   draw), then goal difference, then goals scored, all descending.
2. Rule C.7 ordinarily gives clubs that remain tied the same league
   position. C.17 applies at season end when an unresolved tie determines
   the champion, relegation, or qualification for another competition.
3. C.17.1 uses points in league matches between the tied clubs that season.
   C.17.2 uses away goals in those meetings. C.17.3 provides a neutral-ground
   playoff for two still-tied clubs, with details decided by the Board.
4. C.15 and C.25–C.30 address membership and curtailment exceptions. Under
   C.30, if sporting consequences must be considered after curtailment,
   relative position uses average points per game, goal difference,
   average goals per game, then the C.17 procedures.

Official sanctions, Board decisions, playoff outcome, and how any residual
tie of more than two clubs is resolved require evidence beyond regular
match scores. The publishing policy must define how forecasts handle those
cases and how ordinary shared ranks feed into the required unique
representative table. A stable-ID assignment tie-break cannot determine
official standings. Verify official adjustments and block publication when
a material outcome cannot be resolved under an approved policy.
[Official handbook, Section C](https://resources.premierleague.pulselive.com/premierleague/document/2026/10/22/24902579-d9e8-44b3-a9e4-777c7b3d7134/TM2040_PL-Handbook-and-Collateral-2026-27_Digital_21.10.pdf) · [Publication record](https://www.premierleague.com/en/news/62625)

## Free static hosting

Cloudflare's Pages product page explicitly advertises a free path without
a credit card, and the account guide describes email/password signup and
email verification. The Pages Free limits are 500 builds per month, one
concurrent build, a 20-minute build timeout, 20,000 files per site and
25 MiB per asset. Use static assets on the default `pages.dev` subdomain
and verify the account and the deployment trigger at the approved deployment
stage. [Cloudflare Pages](https://www.cloudflare.com/products/pages/) · [Account setup](https://developers.cloudflare.com/fundamentals/account/create-account/) · [Pages limits](https://developers.cloudflare.com/pages/platform/limits/)

GitHub Pages is an alternative for a public repository on GitHub Free.
Its limits include a 1 GB published site, 100 GB/month soft bandwidth cap,
and 10-minute deployment timeout. GitHub documents free standard hosted
Actions runners for public repositories. The fallback's account-specific
no-card setup and deployment were not exercised. Recheck limits and keep
paid features disabled before configuring either host. [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits) · [GitHub Actions billing](https://docs.github.com/en/actions/concepts/billing-and-usage)

## Release decision

Do not publish production forecasts, derived data or a production deployment
while any gate above is BLOCKED. Remaining evidence includes provider
permission, verified free-tier account access and fixture reconciliation,
handling for exceptional standings, and complete historical validation
with a leakage-free backtest. Recheck the selected host's no-card
eligibility and limits with the maintainer before deployment.
