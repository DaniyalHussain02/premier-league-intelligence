# Retrospective match-outcome evaluation

T11 compares the Elo/Poisson model with an empirical home/draw/away baseline on the same 380 fixtures from one verified completed Premier League season. Each report is labeled **RETROSPECTIVE**. These reconstructed predictions are separate from forecasts that were actually published live.

## Walk-forward training

Freeze the evaluation cohort, training start, cutoff policy, baseline policy and model configuration before examining the scores. The prior training window begins at the declared UTC training start, inclusive, and ends at the evaluation season's start date at 00:00 UTC, exclusive. Prior fixture membership is based on actual kickoff and must come from a different season. Independent coverage review must substantiate that window, including a verified empty prior window where applicable. The complete evaluation cohort supplies the evaluation season's contribution to the expanding history. The combined prior and evaluation result inventory has a 3,800-record limit. An oversized study is blocked without dropping records.

At each prediction cutoff, admit results with independently verified availability strictly earlier than the cutoff. Replay Elo in `(available_at, fixture_id)` order. Earlier evaluation fixtures become eligible after the exact score version is independently known. The target's own score and simultaneous or delayed results stay out until eligible for a later prediction. Model and baseline receive the same eligible prefix. Archive observation times and provider update timestamps cannot establish result availability.

## First eligible prediction timer

Replay each canonical fixture's authenticated schedule and status revisions in UTC knowledge-time order. Apply revisions before a timer at the same timestamp. Equivalent claims at the same instant retain their evidence references. Conflicting claims that affect whether a timer fires block the study. A confirmed schedule sets a timer one hour before its kickoff if the schedule is known by that deadline. A superseding revision replaces the schedule, and a postponement or cancellation clears it. A replacement announced after its one-hour deadline cannot create a retroactive prediction.

Lock the first timer that fires. A later schedule change can make that prediction stale, but the locked forecast stays in the study and is scored if the same canonical fixture eventually finishes. This measures the first forecast available under the declared policy, including subsequent disruption. The independently verified actual kickoff must be later than the cutoff. Actual kickoff and eventual outcome are used for validation and scoring, never to reconstruct the historical schedule or pick a different timer.

## Baseline and score

The baseline uses empirical home-win, draw and away-win frequencies from the shared eligible history. Below 20 eligible results, it uses fixed uniform probabilities of one third each. Each match contributes the sum of the three squared errors against the one-hot outcome. Average that sum over the same fixtures for both methods. Scores range from 0 to 2, and lower is better. This comparison alone does not establish calibration, superiority on other seasons, or production performance.

The internal report records the study and prediction cutoff policies, the training and evaluation periods, eligible training membership as ordered IDs and a prefix length per prediction, the paired prediction and outcome records, the sample and fallback counts, source revisions, review references, configurations and runtime. These audit records have no public serialization path in T11.

## Evidence and release status

Upstream independent review must authenticate the exact canonical inventory, completion, permitted source revisions, training coverage, cutoff-specific schedule and status histories, actual kickoffs, score versions and availability. The study cutoff limits which review evidence can substantiate the run. Rescheduling approvals are covered by the season review. Evidence references and a historical label alone do not authenticate a dataset. Incomplete required evidence blocks the whole report without partial scores.

Synthetic tests demonstrate implementation correctness. A verified historical evaluation is still outstanding, including full reconciliation and timestamp evidence for the candidate season. There are no real-world accuracy results from this task. G5b and T03 production publication remain **BLOCKED**.
