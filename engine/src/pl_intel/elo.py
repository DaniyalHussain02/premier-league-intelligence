"""Replay independently verified results in the order they became available."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from types import MappingProxyType

from pl_intel.normalize import canonical_fixture_id
from pl_intel.schemas import Fixture, FixtureStatus
from pl_intel.sources.openfootball import VerifiedHistoricalResult
from pl_intel.validate import require_text, validate_fixture

_MAX_RECORDS = 100_000


@dataclass(frozen=True, slots=True)
class EloConfig:
    """Fixed provisional model parameters awaiting backtest evaluation."""

    initial_rating: float = 1500.0
    home_advantage: float = 60.0
    scale: float = 400.0
    k_factor: float = 20.0

    def __post_init__(self) -> None:
        for name, value, minimum, maximum in (
            ("initial rating", self.initial_rating, 1, 10_000),
            ("home advantage", self.home_advantage, 0, 400),
            ("scale", self.scale, 1, 2_000),
            ("K-factor", self.k_factor, 1, 100),
        ):
            if type(value) not in {int, float} or not minimum <= value <= maximum:
                raise ValueError(
                    f"Elo {name} must be a finite number in [{minimum}, {maximum}]"
                )


@dataclass(frozen=True, slots=True)
class EloMatch:
    """A verified result with an independently reviewed canonical identity binding."""

    result: VerifiedHistoricalResult
    fixture: Fixture
    identity_source_ref: str


@dataclass(frozen=True, slots=True)
class EloSnapshot:
    cutoff: datetime
    ratings: Mapping[str, float]
    matches_applied: int


def _require_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Expected a timezone-aware UTC timestamp")


def _validate_eligible_match(match: EloMatch) -> None:
    fixture, result = match.fixture, match.result
    if not isinstance(fixture, Fixture):
        raise TypeError("Elo requires a reviewed canonical fixture")
    validate_fixture(fixture)
    require_text(match.identity_source_ref)
    require_text(result.source_ref)
    if fixture.status is not FixtureStatus.FINISHED:
        raise ValueError("Elo requires a finished fixture")
    if (
        result.fixture_id != fixture.fixture_id
        or fixture.fixture_id
        != canonical_fixture_id(
            fixture.season_id, fixture.home_team_id, fixture.away_team_id
        )
    ):
        raise ValueError(
            "Verified result does not match the canonical fixture identity"
        )
    if result.available_at <= fixture.kickoff_utc:
        raise ValueError("Verified result availability must be strictly after kickoff")
    goals = (result.home_goals, result.away_goals)
    if any(type(score) is not int or not 0 <= score < 2**63 for score in goals):
        raise ValueError("Elo requires two valid nonnegative integer scores")
    if goals != (fixture.home_goals, fixture.away_goals):
        raise ValueError("Verified score does not match the reviewed fixture")


def _home_expectation(home: float, away: float, config: EloConfig) -> float:
    exponent = (away - home - config.home_advantage) / config.scale
    weight = 10.0 ** -abs(exponent)
    return (weight if exponent > 0 else 1.0) / (1.0 + weight)


def rate_teams(
    matches: Iterable[EloMatch],
    *,
    cutoff: datetime,
    config: EloConfig = EloConfig(),
) -> EloSnapshot:
    """Rate clubs using verified results strictly before cutoff.

    The caller independently verifies score availability and fixture identity
    for the same score version. Kickoff and finished status validate the
    binding. Archive observation time and updated_at cannot establish availability.
    Replay follows (available_at, fixture_id), so delayed results enter when
    known and cannot alter an earlier snapshot. Within the 100,000-record cap,
    future records with valid UTC availability are skipped before inspecting
    scores or fixture metadata to preserve cutoff invariance.

    Record the model configuration, cutoff, input revision, and score and
    identity evidence references when integrating this baseline.
    New clubs enter at the initial rating on their first eligible result.
    Ratings carry across seasons and absences. The replay uses no roster labels.
    """
    _require_utc(cutoff)
    if not isinstance(config, EloConfig):
        raise TypeError("Expected an EloConfig")

    eligible = []
    seen_ids = set()
    for count, match in enumerate(matches, 1):
        if count > _MAX_RECORDS:
            raise ValueError("Elo input exceeds 100,000 supplied records")
        if not isinstance(match, EloMatch) or not isinstance(
            match.result, VerifiedHistoricalResult
        ):
            raise TypeError("Elo requires an independently verified result fact")
        _require_utc(match.result.available_at)
        if match.result.available_at >= cutoff:
            continue
        _validate_eligible_match(match)
        if match.result.fixture_id in seen_ids:
            raise ValueError("Duplicate eligible result for canonical fixture")
        seen_ids.add(match.result.fixture_id)
        eligible.append(match)
    eligible.sort(
        key=lambda match: (match.result.available_at, match.result.fixture_id)
    )

    ratings: dict[str, float] = {}
    for match in eligible:
        home_id, away_id = match.fixture.home_team_id, match.fixture.away_team_id
        home = ratings.get(home_id, config.initial_rating)
        away = ratings.get(away_id, config.initial_rating)
        home_goals, away_goals = match.result.home_goals, match.result.away_goals
        if home_goals > away_goals:
            score = 1.0
        elif home_goals == away_goals:
            score = 0.5
        else:
            score = 0.0
        delta = config.k_factor * (score - _home_expectation(home, away, config))
        next_home, next_away = home + delta, away - delta
        if not (isfinite(next_home) and isfinite(next_away)):
            raise ValueError("Elo update produced a non-finite rating")
        ratings[home_id] = next_home
        ratings[away_id] = next_away

    return EloSnapshot(
        cutoff, MappingProxyType(dict(sorted(ratings.items()))), len(eligible)
    )
