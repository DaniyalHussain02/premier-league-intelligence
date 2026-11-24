"""Offline walk-forward evaluation of independently reviewed match evidence."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import islice
from math import fsum, isfinite
from platform import python_version

from pl_intel.db import store_season, temporary_database
from pl_intel.elo import EloConfig, EloMatch
from pl_intel.normalize import (
    IncompleteInventory,
    canonical_fixture_id,
    inventory_team_ids,
    validate_fixture_pairings,
)
from pl_intel.poisson import MODEL_VERSION, PoissonConfig, predict_match
from pl_intel.schemas import (
    FixtureStatus,
    ReschedulingApproval,
    Season,
    SeasonStatus,
    Team,
)
from pl_intel.sources.openfootball import VerifiedHistoricalResult
from pl_intel.validate import require_text, validate_fixture, validate_season

_MAX_RESULTS = 3_800
_MAX_REVISIONS = 64
_LEAD = timedelta(hours=1)
_MIN_BASELINE_HISTORY = 20
_UNIFORM = (1 / 3, 1 / 3, 1 / 3)
_CUTOFF_POLICY = "first-confirmed-schedule-one-hour-v1"
_BASELINE_POLICY = "empirical-1x2-uniform-below-20-v1"
_LIMITATIONS = (
    "The first forecast can become stale after a subsequent postponement.",
    "A paired Brier comparison alone does not establish calibration or production performance.",
)


class StudyKind(StrEnum):
    SYNTHETIC = "synthetic"
    VERIFIED_HISTORICAL = "verified-historical"


class Outcome(StrEnum):
    HOME = "home"
    DRAW = "draw"
    AWAY = "away"


_OUTCOMES = tuple(Outcome)
Probabilities = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class ReviewedEvidence:
    """Opaque upstream evidence reference and its independent review timestamp."""

    reference: str
    reviewed_at: datetime


@dataclass(frozen=True, slots=True)
class ResultEvidence:
    """Binds the exact result version, actual kickoff, and canonical identity."""

    match: EloMatch
    review: ReviewedEvidence


@dataclass(frozen=True, slots=True)
class FixtureRevision:
    """A schedule/status claim for the enclosing canonical slot."""

    fixture_id: str
    known_at: datetime
    status: FixtureStatus
    scheduled_kickoff: datetime | None
    review: ReviewedEvidence


@dataclass(frozen=True, slots=True)
class FixtureObservation:
    result: ResultEvidence | None
    revisions: tuple[FixtureRevision, ...]
    revision_coverage: ReviewedEvidence | None
    coverage_through: datetime


@dataclass(frozen=True, slots=True)
class BacktestDataset:
    kind: StudyKind
    season: Season
    teams: tuple[Team, ...]
    observations: tuple[FixtureObservation, ...]
    prior_results: tuple[ResultEvidence, ...]
    source_revisions: tuple[str, ...]
    season_review: ReviewedEvidence | None
    training_coverage: ReviewedEvidence | None
    rescheduling_approvals: tuple[ReschedulingApproval, ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    training_start: datetime
    study_evidence_cutoff: datetime
    elo: EloConfig = field(default_factory=EloConfig)
    poisson: PoissonConfig = field(default_factory=PoissonConfig)


# The caller supplies a trusted independent review integration for historical
# studies. It must authenticate the exact dataset and frozen study policy.
EvidenceVerifier = Callable[[BacktestDataset, BacktestConfig], bool]


@dataclass(frozen=True, slots=True)
class ObservationScore:
    fixture_id: str
    cutoff: datetime
    scheduled_kickoff: datetime
    revision_known_at: datetime
    revision_refs: tuple[str, ...]
    eligible_count: int
    model: Probabilities
    baseline: Probabilities
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class BacktestReport:
    kind: StudyKind
    season_id: str
    config: BacktestConfig
    source_revisions: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    # Each audit row uses exactly the first eligible_count IDs in this order.
    training_order: tuple[str, ...]
    training_kickoff_span: tuple[datetime, datetime] | None
    test_cutoff_span: tuple[datetime, datetime]
    observations: tuple[ObservationScore, ...]
    sample_count: int
    fallback_count: int
    model_brier: float
    baseline_brier: float
    label: str = "RETROSPECTIVE"
    cutoff_policy: str = _CUTOFF_POLICY
    baseline_policy: str = _BASELINE_POLICY
    training_policy: str = "expanding-from-inclusive-kickoff-start-v1"
    model_version: str = MODEL_VERSION
    runtime: str = python_version()
    limitations: tuple[str, ...] = _LIMITATIONS


@dataclass(frozen=True, slots=True)
class BacktestBlocked:
    reason: str
    fixture_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Timer:
    cutoff: datetime
    kickoff: datetime
    known_at: datetime
    refs: tuple[str, ...]


def _utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Expected timezone-aware UTC timestamp")


def _review(evidence: ReviewedEvidence, study_cutoff: datetime) -> None:
    if not isinstance(evidence, ReviewedEvidence):
        raise TypeError("Expected independently reviewed evidence record")
    require_text(evidence.reference)
    _utc(evidence.reviewed_at)
    if evidence.reviewed_at > study_cutoff:
        raise ValueError("Evidence review is later than the study cutoff")


class _ConflictingFixtureRevisionsError(ValueError):
    """Same-time revisions disagree on the fixture status or schedule."""


def _select_timer(
    revisions: tuple[FixtureRevision, ...], study_cutoff: datetime
) -> _Timer | None:
    """Process schedule knowledge before timers at equal timestamps."""
    timeline: dict[datetime, list[FixtureRevision]] = {}
    for revision in revisions:
        _review(revision.review, study_cutoff)
        _utc(revision.known_at)
        if revision.known_at > revision.review.reviewed_at:
            raise ValueError("Revision knowledge postdates its evidence review")
        if revision.status not in (
            FixtureStatus.SCHEDULED,
            FixtureStatus.POSTPONED,
            FixtureStatus.CANCELLED,
        ) or not isinstance(revision.status, FixtureStatus):
            raise ValueError("Expected a pre-match fixture status")
        if revision.scheduled_kickoff is not None:
            _utc(revision.scheduled_kickoff)
        if (
            revision.status is FixtureStatus.SCHEDULED
            and revision.scheduled_kickoff is None
        ):
            raise ValueError("Confirmed schedule requires a kickoff")
        timeline.setdefault(revision.known_at, []).append(revision)

    pending = None
    for known_at, claims in sorted(timeline.items()):
        claim = claims[0]
        if any(
            (item.status, item.scheduled_kickoff)
            != (claim.status, claim.scheduled_kickoff)
            for item in claims
        ):
            raise _ConflictingFixtureRevisionsError
        # Later groups still need validation, but cannot change a fired timer.
        if pending is not None and pending.cutoff < known_at:
            continue
        kickoff = claim.scheduled_kickoff
        if claim.status is not FixtureStatus.SCHEDULED or kickoff is None:
            pending = None
            continue
        if pending is not None and pending.kickoff == kickoff:
            # The latest equivalent active revision is the evidence applicable
            # at the cutoff, while the originally scheduled timer stays put.
            cutoff = pending.cutoff
        else:
            cutoff = kickoff - _LEAD
        pending = (
            _Timer(
                cutoff,
                kickoff,
                known_at,
                tuple(sorted({item.review.reference for item in claims})),
            )
            if known_at <= cutoff
            else None
        )
    return pending


def _validate_result(evidence: ResultEvidence, config: BacktestConfig) -> EloMatch:
    _review(evidence.review, config.study_evidence_cutoff)
    match = evidence.match
    if not isinstance(match, EloMatch) or not isinstance(
        match.result, VerifiedHistoricalResult
    ):
        raise TypeError("Expected a verified historical result fact")
    fixture, result = match.fixture, match.result
    validate_fixture(fixture)
    require_text(match.identity_source_ref)
    require_text(result.source_ref)
    _utc(result.available_at)
    if (
        fixture.status is not FixtureStatus.FINISHED
        or result.fixture_id != fixture.fixture_id
        or fixture.fixture_id
        != canonical_fixture_id(
            fixture.season_id, fixture.home_team_id, fixture.away_team_id
        )
        or any(
            type(goals) is not int for goals in (result.home_goals, result.away_goals)
        )
        or (result.home_goals, result.away_goals)
        != (fixture.home_goals, fixture.away_goals)
    ):
        raise ValueError("Result evidence does not match the exact finished fixture")
    if (
        not fixture.kickoff_utc < result.available_at <= evidence.review.reviewed_at
        or fixture.updated_at > evidence.review.reviewed_at
    ):
        raise ValueError("Result evidence chronology is inconsistent")
    return match


def _outcome(result: VerifiedHistoricalResult) -> Outcome:
    if result.home_goals > result.away_goals:
        return Outcome.HOME
    if result.home_goals < result.away_goals:
        return Outcome.AWAY
    return Outcome.DRAW


def brier_score(probabilities: Probabilities, outcome: Outcome) -> float:
    """Return the sum of squared 1X2 errors for one observed outcome."""
    if not isinstance(outcome, Outcome):
        raise TypeError("Expected a 1X2 outcome")
    if (
        len(probabilities) != 3
        or any(
            type(p) not in (int, float) or not 0 <= p <= 1 or not isfinite(p)
            for p in probabilities
        )
        or abs(fsum(probabilities) - 1.0) > 1e-12
    ):
        raise ValueError("Expected three finite, normalized 1X2 probabilities")
    return fsum(
        (probability - (label is outcome)) ** 2
        for probability, label in zip(probabilities, _OUTCOMES, strict=True)
    )


def run_backtest(
    dataset: BacktestDataset,
    *,
    config: BacktestConfig,
    verify_evidence: EvidenceVerifier | None = None,
) -> BacktestReport | BacktestBlocked:
    """Evaluate a frozen cohort offline. Reports are internal audit records.

    For a historical run, a trusted independent verifier must authenticate
    the exact revisions, scores, kickoff evidence, source rights, and coverage
    of both the revision timelines and declared training window. Timestamps
    and references here are consistency checks, not authentication.
    """
    _utc(config.training_start)
    _utc(config.study_evidence_cutoff)
    if config.training_start >= config.study_evidence_cutoff:
        raise ValueError("Training start must precede study cutoff")
    if not isinstance(dataset.kind, StudyKind):
        raise TypeError("Unknown study classification")
    if not isinstance(config.elo, EloConfig) or not isinstance(
        config.poisson, PoissonConfig
    ):
        raise TypeError("Invalid backtest model configuration")
    validate_season(dataset.season)
    if len(dataset.prior_results) + len(dataset.observations) > _MAX_RESULTS:
        return BacktestBlocked("combined_result_cap_exceeded")
    if len(dataset.observations) > 380:
        raise ValueError("Evaluation inventory exceeds 380 fixtures")
    if (
        dataset.season.status is not SeasonStatus.COMPLETE
        or len(dataset.observations) < 380
    ):
        return BacktestBlocked("incomplete_evaluation_season")
    try:
        team_ids = inventory_team_ids(dataset.teams)
    except IncompleteInventory:
        return BacktestBlocked("incomplete_evaluation_season")
    if (
        dataset.season_review is None
        or dataset.training_coverage is None
        or not dataset.source_revisions
    ):
        return BacktestBlocked("missing_study_or_training_coverage")
    study_cutoff = config.study_evidence_cutoff
    for revision in dataset.source_revisions:
        require_text(revision)
    _review(dataset.season_review, study_cutoff)
    _review(dataset.training_coverage, study_cutoff)
    if dataset.training_coverage.reviewed_at.date() < dataset.season.start_date:
        raise ValueError("Training coverage review precedes the prior window end")
    if dataset.season_review.reviewed_at.date() < dataset.season.end_date:
        raise ValueError("Completion review precedes the season end")

    refs = {
        dataset.season_review.reference,
        dataset.training_coverage.reference,
        dataset.season.source_ref,
    }
    registry: dict[str, EloMatch] = {}
    provider_ids: set[tuple[str, str]] = set()

    def register(evidence: ResultEvidence) -> EloMatch:
        match = _validate_result(evidence, config)
        fixture = match.fixture
        provider_id = fixture.provider, fixture.provider_fixture_id
        if fixture.fixture_id in registry or provider_id in provider_ids:
            raise ValueError("Duplicate canonical or provider fixture identity")
        registry[fixture.fixture_id] = match
        provider_ids.add(provider_id)
        refs.update(
            (
                evidence.review.reference,
                match.identity_source_ref,
                match.result.source_ref,
            )
        )
        return match

    for evidence in dataset.prior_results:
        match = register(evidence)
        if (
            match.fixture.season_id == dataset.season.season_id
            or not config.training_start <= match.fixture.kickoff_utc
            or match.fixture.kickoff_utc.date() >= dataset.season.start_date
        ):
            raise ValueError(
                "Prior result lies outside the declared prior training window"
            )

    targets = []
    for observation in dataset.observations:
        if observation.result is None:
            return BacktestBlocked("missing_evaluation_result")
        match = register(observation.result)
        fixture = match.fixture
        if fixture.season_id != dataset.season.season_id:
            raise ValueError("Evaluation result belongs to a different season")
        if fixture.kickoff_utc > dataset.season_review.reviewed_at:
            raise ValueError("Completion review precedes an evaluation kickoff")
        if observation.revision_coverage is None:
            return BacktestBlocked("missing_revision_coverage", fixture.fixture_id)
        _review(observation.revision_coverage, study_cutoff)
        _utc(observation.coverage_through)
        if observation.coverage_through > observation.revision_coverage.reviewed_at:
            raise ValueError("Coverage extends past its evidence review")
        if len(observation.revisions) > _MAX_REVISIONS:
            raise ValueError("Fixture revision history exceeds 64 records")
        if any(
            revision.fixture_id != fixture.fixture_id
            for revision in observation.revisions
        ):
            raise ValueError("Schedule revision belongs to a different fixture")
        try:
            timer = _select_timer(observation.revisions, study_cutoff)
        except _ConflictingFixtureRevisionsError:
            return BacktestBlocked("conflicting_fixture_revisions", fixture.fixture_id)
        if timer is None or timer.cutoff > observation.coverage_through:
            return BacktestBlocked("unverified_prediction_timer", fixture.fixture_id)
        if not config.training_start <= timer.cutoff < fixture.kickoff_utc:
            raise ValueError("Locked timer is outside the pre-kickoff study window")
        refs.update(timer.refs)
        refs.add(observation.revision_coverage.reference)
        refs.update(revision.review.reference for revision in observation.revisions)
        targets.append((timer, match))

    fixtures = tuple(match.fixture for _, match in targets)
    validate_fixture_pairings(
        team_ids, ((f.home_team_id, f.away_team_id) for f in fixtures)
    )
    if any(
        match.result.available_at > dataset.training_coverage.reviewed_at
        for match in registry.values()
        if match.fixture.season_id != dataset.season.season_id
    ):
        raise ValueError("Prior training coverage review predates a supplied result")
    if len(dataset.rescheduling_approvals) > len(fixtures):
        raise ValueError("Too many rescheduling approvals")
    with temporary_database() as db:
        store_season(
            db,
            dataset.season,
            dataset.teams,
            fixtures=fixtures,
            rescheduling_approvals=dataset.rescheduling_approvals,
        )
    refs.update(approval.source_ref for approval in dataset.rescheduling_approvals)
    if dataset.kind is StudyKind.VERIFIED_HISTORICAL and (
        verify_evidence is None or verify_evidence(dataset, config) is not True
    ):
        return BacktestBlocked("independent_authentication_required")

    history = sorted(
        (
            match
            for match in registry.values()
            if match.fixture.kickoff_utc >= config.training_start
        ),
        key=lambda match: (match.result.available_at, match.result.fixture_id),
    )
    targets.sort(key=lambda target: (target[0].cutoff, target[1].fixture.fixture_id))
    index = 0
    counts = dict.fromkeys(_OUTCOMES, 0)
    rows = []
    model_losses = []
    baseline_losses = []
    fallback_count = 0
    for timer, target in targets:
        fixture = target.fixture
        while (
            index < len(history) and history[index].result.available_at < timer.cutoff
        ):
            counts[_outcome(history[index].result)] += 1
            index += 1
        # A target's result is strictly after actual kickoff, which is
        # strictly after this timer. It cannot lie in the eligible prefix.
        prediction = predict_match(
            islice(history, index),
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            kickoff_utc=timer.kickoff,
            cutoff=timer.cutoff,
            elo_config=config.elo,
            poisson_config=config.poisson,
        )
        if prediction.model_version != MODEL_VERSION:
            raise ValueError("Unexpected backtest model version")
        model = prediction.home_win, prediction.draw, prediction.away_win
        if index < _MIN_BASELINE_HISTORY:
            baseline = _UNIFORM
            fallback_count += 1
        else:
            baseline = (
                counts[Outcome.HOME] / index,
                counts[Outcome.DRAW] / index,
                counts[Outcome.AWAY] / index,
            )
        outcome = _outcome(target.result)
        model_losses.append(brier_score(model, outcome))
        baseline_losses.append(brier_score(baseline, outcome))
        rows.append(
            ObservationScore(
                fixture.fixture_id,
                timer.cutoff,
                timer.kickoff,
                timer.known_at,
                timer.refs,
                index,
                model,
                baseline,
                outcome,
            )
        )

    used = history[:index]
    kickoffs = [match.fixture.kickoff_utc for match in used]
    return BacktestReport(
        kind=dataset.kind,
        season_id=dataset.season.season_id,
        config=config,
        source_revisions=tuple(sorted(set(dataset.source_revisions))),
        evidence_refs=tuple(sorted(refs)),
        training_order=tuple(match.result.fixture_id for match in used),
        training_kickoff_span=(min(kickoffs), max(kickoffs)) if kickoffs else None,
        test_cutoff_span=(rows[0].cutoff, rows[-1].cutoff),
        observations=tuple(rows),
        sample_count=len(rows),
        fallback_count=fallback_count,
        model_brier=fsum(model_losses) / len(rows),
        baseline_brier=fsum(baseline_losses) / len(rows),
        limitations=_LIMITATIONS
        + (
            (
                "Synthetic results establish correctness only and are ineligible for publication.",
            )
            if dataset.kind is StudyKind.SYNTHETIC
            else ()
        ),
    )
