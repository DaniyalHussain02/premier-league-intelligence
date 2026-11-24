"""Bounded, offline season simulation over independently reviewed snapshots."""

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import islice
from json import dumps
from math import fsum, lcm
from platform import python_version
from random import Random

from pl_intel.elo import EloConfig, EloMatch, rate_teams
from pl_intel.normalize import (
    canonical_fixture_id,
    inventory_team_ids,
    validate_fixture_pairings,
)
from pl_intel.poisson import MODEL_VERSION, PoissonConfig, predict_match
from pl_intel.ranking import (
    AdjustmentEvidence,
    QualificationEvidence,
    StandingsPhase,
    calculate_standings,
)
from pl_intel.rules import Ruleset
from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    ReschedulingApproval,
    Season,
    SeasonStatus,
    Team,
)
from pl_intel.sources.openfootball import VerifiedHistoricalResult
from pl_intel.validate import require_text, validate_fixture

_POSITION_UNITS = lcm(*range(1, 21))
_TOLERANCE = 1e-12
_DEFAULT_ELO = EloConfig()
_DEFAULT_POISSON = PoissonConfig()


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    trials: int = 10_000
    batch_size: int = 128
    seed: int = 0

    def __post_init__(self) -> None:
        for value, low, high in (
            (self.trials, 1, 50_000),
            (self.batch_size, 1, 512),
            (self.seed, 0, 2**64 - 1),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid simulation configuration")


@dataclass(frozen=True, slots=True)
class ReviewedFixture:
    """An upstream claim about the exact fixture state known at a given time.

    Finished states carry the reviewed result fact and its identity binding.
    References are independently authenticated upstream; this module only
    checks that the supplied claims agree.
    """

    fixture: Fixture
    known_at: datetime
    evidence_ref: str
    replacement_kickoff_confirmed: bool = False
    finished_result: VerifiedHistoricalResult | None = None
    result_identity_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SeasonSnapshot:
    """Upstream-reviewed season, policy and fixture inventory as of known_at.

    The evidence reference covers the supplied roster, rules, adjustments,
    qualification evidence and rescheduling approvals. Reviewers authenticate
    these claims and select the exact versions applicable at the cutoff.
    """

    season: Season
    teams: tuple[Team, ...]
    fixtures: tuple[ReviewedFixture, ...]
    known_at: datetime
    source_revision: str
    evidence_ref: str
    rules: Ruleset
    adjustments: AdjustmentEvidence
    qualification: QualificationEvidence | None = None
    rescheduling_approvals: tuple[ReschedulingApproval, ...] = ()
    exceptional_procedure: bool = False


@dataclass(frozen=True, slots=True)
class SimulationProvenance:
    """Source inputs, execution identity, and forecast-output equivalence.

    The output equivalence fingerprint is for comparing forecast outputs only.
    Never use it as an immutable publication or snapshot ID. T13 owns that ID.
    """

    season_id: str
    cutoff: datetime
    source_revision: str
    evidence_ref: str
    input_fingerprint: str
    run_fingerprint: str
    output_equivalence_fingerprint: str
    simulation: SimulationConfig
    elo: EloConfig
    poisson: PoissonConfig
    max_fixture_tail_bound: float
    ruleset_version: str
    evidence_refs: tuple[str, ...]
    model_version: str = MODEL_VERSION
    simulator_version: str = "season-monte-carlo-v2"
    rng: str = "random.Random/MT19937"
    cutoff_policy: str = (
        "verified available_at < cutoff; replay by availability and fixture ID"
    )
    python: str = python_version()


@dataclass(frozen=True, slots=True)
class ClubForecast:
    team_id: str
    positions: tuple[float, ...]
    title_probability: float
    relegation_probability: float
    actual_earned_points: int
    actual_adjusted_points: int
    expected_earned_points: float
    expected_adjusted_points: float


@dataclass(frozen=True, slots=True)
class SimulationResult:
    clubs: tuple[ClubForecast, ...]
    provenance: SimulationProvenance


@dataclass(frozen=True, slots=True)
class SimulationBlocked:
    reasons: tuple[str, ...]
    fixture_id: str | None = None
    trial: int | None = None


def _utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Expected timezone-aware UTC timestamp")


def _bounded_history(
    history: Iterable[EloMatch], cutoff: datetime
) -> tuple[EloMatch, ...]:
    eligible = []
    for count, match in enumerate(history, 1):
        if count > 100_000:
            raise ValueError("Elo input exceeds 100,000 supplied records")
        if not isinstance(match, EloMatch) or not isinstance(
            match.result, VerifiedHistoricalResult
        ):
            raise TypeError("Expected independently verified result evidence")
        _utc(match.result.available_at)
        if match.result.available_at < cutoff:
            eligible.append(match)
    return tuple(eligible)


def _fixture_blocker(
    reviewed: ReviewedFixture,
    cutoff: datetime,
    snapshot_known_at: datetime,
    scores: dict[str, EloMatch],
) -> str | None:
    fixture = reviewed.fixture
    _utc(reviewed.known_at)
    require_text(reviewed.evidence_ref)
    # Provider timestamps can contradict a reviewed version. Only the
    # independent result fact establishes when its score became available.
    if reviewed.known_at > cutoff or fixture.updated_at > reviewed.known_at:
        return "fixture_state_not_known_at_cutoff"
    if reviewed.known_at > snapshot_known_at:
        return "fixture_revision_after_snapshot"
    if fixture.status is FixtureStatus.FINISHED:
        match = scores.get(fixture.fixture_id)
        if match is None:
            return "missing_exact_verified_score"
        if (
            match.fixture != fixture
            or match.result != reviewed.finished_result
            or match.identity_source_ref != reviewed.result_identity_ref
        ):
            return "fixture_result_evidence_mismatch"
        if match.result.available_at > reviewed.known_at:
            return "result_not_known_at_fixture_review_time"
        return None
    if reviewed.finished_result is not None or reviewed.result_identity_ref is not None:
        return "result_evidence_on_unfinished_fixture"
    if fixture.fixture_id in scores:
        return "verified_score_conflicts_with_fixture_state"
    if fixture.status is FixtureStatus.CANCELLED:
        return "unresolved_cancellation"
    if (
        fixture.status is FixtureStatus.POSTPONED
        and reviewed.replacement_kickoff_confirmed is not True
    ):
        return "unconfirmed_replacement_kickoff"
    if fixture.kickoff_utc < cutoff:
        return "overdue_unfinished_fixture"
    return None


def _draw_table(grid: tuple[tuple[float, ...], ...]) -> tuple[list[float], int]:
    width = len(grid[0])
    probabilities = [p for row in grid for p in row]
    if abs(fsum(probabilities) - 1.0) > _TOLERANCE:
        raise ArithmeticError("Scoreline distribution is not normalized")
    cumulative = []
    total = 0.0
    for probability in probabilities:
        total += probability
        cumulative.append(min(total, 1.0))
    cumulative[-1] = 1.0
    return cumulative, width


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        _utc(value)
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError("Unsupported forecast fingerprint value")


def _fingerprint(schema: str, fields: object) -> str:
    # Bump the affected schema version before changing its canonical encoding.
    encoded = dumps(
        {"schema": schema, "fields": fields},
        default=_canonical_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(encoded.encode("ascii")).hexdigest()


def simulate_season(
    snapshot: SeasonSnapshot,
    history: Iterable[EloMatch],
    *,
    cutoff: datetime,
    config: SimulationConfig = SimulationConfig(),
    elo_config: EloConfig = _DEFAULT_ELO,
    poisson_config: PoissonConfig = _DEFAULT_POISSON,
) -> SimulationResult | SimulationBlocked:
    """Freeze a reviewed season at cutoff and either return all trials or block.

    Upstream callers must independently authenticate exact cutoff-specific
    fixture revisions and result facts before supplying them. Matching
    references alone cannot establish authenticity. Missing authentication
    evidence blocks production publication.

    A fixture revision must be known by the snapshot as-of time, which must
    be on or before the run cutoff. A finished score must be independently
    available by the fixture review time and strictly before the run cutoff.

    A postponed record's evidence must establish that its kickoff is the
    replacement time. Cancelled slots require a reconciled replacement upstream.
    Trial data is local to the ranking call and never becomes historical input.
    """
    _utc(cutoff)
    _utc(snapshot.known_at)
    require_text(snapshot.source_revision)
    require_text(snapshot.evidence_ref)
    if not isinstance(config, SimulationConfig) or not isinstance(
        poisson_config, PoissonConfig
    ):
        raise TypeError("Invalid simulation or Poisson configuration")
    if snapshot.known_at > cutoff or snapshot.season.status is SeasonStatus.PENDING:
        return SimulationBlocked(("season_state_not_confirmed_at_cutoff",))
    if len(snapshot.teams) != 20 or len(snapshot.fixtures) != 380:
        raise ValueError("Expected 20 clubs and 380 fixtures")
    teams = tuple(sorted(snapshot.teams, key=lambda team: team.team_id))
    team_ids = inventory_team_ids(teams)
    reviewed = tuple(
        sorted(snapshot.fixtures, key=lambda record: record.fixture.fixture_id)
    )
    fixtures = tuple(record.fixture for record in reviewed)
    validate_fixture_pairings(
        team_ids, ((f.home_team_id, f.away_team_id) for f in fixtures)
    )
    for fixture in fixtures:
        validate_fixture(fixture)
        if (
            fixture.fixture_id
            != canonical_fixture_id(
                snapshot.season.season_id, fixture.home_team_id, fixture.away_team_id
            )
            or fixture.season_id != snapshot.season.season_id
        ):
            raise ValueError("Fixture identity does not match the season")
    history = _bounded_history(history, cutoff)
    rate_teams(history, cutoff=cutoff, config=elo_config)
    history = tuple(
        sorted(history, key=lambda m: (m.result.available_at, m.result.fixture_id))
    )
    scores = {match.fixture.fixture_id: match for match in history}
    for record in reviewed:
        reason = _fixture_blocker(record, cutoff, snapshot.known_at, scores)
        if reason:
            return SimulationBlocked((reason,), fixture_id=record.fixture.fixture_id)

    def rank(records: Iterable[Fixture], phase: StandingsPhase):
        return calculate_standings(
            snapshot.season,
            teams,
            records,
            adjustments=snapshot.adjustments,
            rules=snapshot.rules,
            qualification=snapshot.qualification,
            phase=phase,
            rescheduling_approvals=snapshot.rescheduling_approvals,
            exceptional_procedure=snapshot.exceptional_procedure,
        )

    current = rank(fixtures, StandingsPhase.CURRENT)
    if current.blockers:
        return SimulationBlocked(tuple(current.blockers))
    pending = tuple(f for f in fixtures if f.status is not FixtureStatus.FINISHED)
    if pending and snapshot.season.status is SeasonStatus.COMPLETE:
        return SimulationBlocked(("complete_season_has_unfinished_fixtures",))
    fixed = tuple(f for f in fixtures if f.status is FixtureStatus.FINISHED)
    tables = []
    tail_bound = 0.0
    for fixture in pending:
        probabilities = predict_match(
            history,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            kickoff_utc=fixture.kickoff_utc,
            cutoff=cutoff,
            elo_config=elo_config,
            poisson_config=poisson_config,
        )
        tables.append(_draw_table(probabilities.scorelines))
        tail_bound = max(tail_bound, probabilities.tail_bound)

    rng = Random(config.seed)
    positions = {team.team_id: [0] * 20 for team in teams}
    earned = dict.fromkeys(positions, 0)
    adjusted = dict.fromkeys(positions, 0)
    trials = config.trials if pending else 1
    weight = 1 if pending else config.trials
    trial_numbers = iter(range(1, trials + 1))
    while batch := tuple(islice(trial_numbers, config.batch_size)):
        for trial in batch:
            sampled = []
            for fixture, (cdf, width) in zip(pending, tables, strict=True):
                home, away = divmod(bisect_right(cdf, rng.random()), width)
                sampled.append(
                    replace(
                        fixture,
                        status=FixtureStatus.FINISHED,
                        updated_at=fixture.kickoff_utc,
                        home_goals=home,
                        away_goals=away,
                    )
                )
            final = rank((*fixed, *sampled), StandingsPhase.FINAL)
            if final.blockers:
                return SimulationBlocked(tuple(final.blockers), trial=trial)
            counts = {}
            for row in final.rows:
                counts[row.rank] = counts.get(row.rank, 0) + 1
            for row in final.rows:
                assert row.rank is not None and row.adjusted_points is not None
                # Shared slots split statistical occupancy equally. This
                # leaves official ranks and consequential outcomes with T06.
                size = counts[row.rank]
                units = weight * (_POSITION_UNITS // size)
                for index in range(row.rank - 1, row.rank - 1 + size):
                    positions[row.team_id][index] += units
                earned[row.team_id] += weight * row.earned_points
                adjusted[row.team_id] += weight * row.adjusted_points

    denominator = config.trials * _POSITION_UNITS
    clubs = []
    for row in sorted(current.rows, key=lambda row: row.team_id):
        club = row.team_id
        slots = positions[club]
        assert row.adjusted_points is not None
        if (
            sum(slots) != denominator
            or earned[club] < config.trials * row.earned_points
            or adjusted[club] < config.trials * row.adjusted_points
        ):
            raise ArithmeticError("Invalid accumulated positions or points")
        clubs.append(
            ClubForecast(
                club,
                tuple(value / denominator for value in slots),
                slots[0] / denominator,
                sum(slots[-3:]) / denominator,
                row.earned_points,
                row.adjusted_points,
                earned[club] / config.trials,
                adjusted[club] / config.trials,
            )
        )
    if any(
        sum(column) != denominator for column in zip(*positions.values(), strict=True)
    ):
        raise ArithmeticError("Invalid accumulated position occupancy")

    input_fingerprint = _fingerprint(
        "t09-source-v1",
        (
            snapshot.season,
            teams,
            reviewed,
            snapshot.known_at,
            snapshot.source_revision,
            snapshot.evidence_ref,
            snapshot.rules,
            snapshot.adjustments.status,
            snapshot.adjustments.evidence_ref,
            tuple(sorted(snapshot.adjustments.adjustments, key=lambda a: a.team_id)),
            snapshot.qualification,
            snapshot.exceptional_procedure,
            tuple(sorted(snapshot.rescheduling_approvals, key=lambda a: a.fixture_id)),
            history,
        ),
    )
    # This is a content comparison key for a future publisher. T09 allocates
    # no snapshot IDs and makes no publication or freshness decisions.
    output_equivalence_fingerprint = _fingerprint(
        "t09-forecast-content-v1",
        (
            snapshot.season.season_id,
            snapshot.rules.version,
            MODEL_VERSION,
            tuple(clubs),
        ),
    )
    refs = {
        snapshot.evidence_ref,
        snapshot.season.source_ref,
        snapshot.rules.evidence_ref,
    }
    refs.update(record.evidence_ref for record in reviewed)
    refs.update(match.result.source_ref for match in history)
    refs.update(match.identity_source_ref for match in history)
    refs.update(approval.source_ref for approval in snapshot.rescheduling_approvals)
    refs.update(
        adjustment.evidence_ref for adjustment in snapshot.adjustments.adjustments
    )
    if snapshot.adjustments.evidence_ref is not None:
        refs.add(snapshot.adjustments.evidence_ref)
    if snapshot.qualification is not None:
        refs.add(snapshot.qualification.evidence_ref)
    provenance = SimulationProvenance(
        snapshot.season.season_id,
        cutoff,
        snapshot.source_revision,
        snapshot.evidence_ref,
        input_fingerprint,
        "",
        output_equivalence_fingerprint,
        config,
        elo_config,
        poisson_config,
        tail_bound,
        snapshot.rules.version,
        tuple(sorted(refs)),
    )
    run_fields = asdict(provenance)
    del run_fields["run_fingerprint"]
    del run_fields["output_equivalence_fingerprint"]
    return SimulationResult(
        tuple(clubs),
        replace(provenance, run_fingerprint=_fingerprint("t09-run-v1", run_fields)),
    )
