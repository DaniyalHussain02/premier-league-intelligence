"""Replay independently verified results in the order they became available."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from types import MappingProxyType

from pl_intel.normalize import canonical_fixture_id
from pl_intel.sources.openfootball import VerifiedHistoricalResult
from pl_intel.validate import require_canonical_id, require_text


@dataclass(frozen=True, slots=True)
class EloConfig:
    """Fixed provisional model parameters awaiting backtest evaluation."""

    initial_rating: float = 1500.0
    home_advantage: float = 60.0
    scale: float = 400.0
    k_factor: float = 20.0

    def __post_init__(self) -> None:
        for value in (
            self.initial_rating,
            self.home_advantage,
            self.scale,
            self.k_factor,
        ):
            try:
                valid = type(value) in {int, float} and isfinite(value)
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("Elo parameters must be finite real numbers")
        if self.initial_rating <= 0 or self.scale <= 0 or self.k_factor <= 0:
            raise ValueError("Initial rating, scale, and K-factor must be positive")
        if self.home_advantage < 0:
            raise ValueError("Home advantage must be nonnegative")


@dataclass(frozen=True, slots=True)
class EloMatch:
    """A verified result with an independently reviewed canonical identity binding."""

    result: VerifiedHistoricalResult
    season_id: str
    home_team_id: str
    away_team_id: str
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
    for value in (
        match.season_id,
        match.home_team_id,
        match.away_team_id,
        match.result.fixture_id,
    ):
        require_canonical_id(value)
    require_text(match.identity_source_ref)
    require_text(match.result.source_ref)
    if match.home_team_id == match.away_team_id:
        raise ValueError("Elo requires distinct home and away clubs")
    if match.result.fixture_id != canonical_fixture_id(
        match.season_id, match.home_team_id, match.away_team_id
    ):
        raise ValueError(
            "Verified result does not match the canonical fixture identity"
        )
    for goals in (match.result.home_goals, match.result.away_goals):
        if type(goals) is not int or not 0 <= goals < 2**63:
            raise ValueError("Elo requires two valid nonnegative integer scores")


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

    The caller establishes score availability and historical fixture identity.
    New clubs enter at the initial rating on their first eligible result.
    Ratings carry across seasons and absences. The replay uses no roster labels.
    """
    _require_utc(cutoff)
    if not isinstance(config, EloConfig):
        raise TypeError("Expected an EloConfig")

    eligible = []
    seen_ids = set()
    for match in matches:
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
        home = ratings.get(match.home_team_id, config.initial_rating)
        away = ratings.get(match.away_team_id, config.initial_rating)
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
        ratings[match.home_team_id] = next_home
        ratings[match.away_team_id] = next_away

    return EloSnapshot(
        cutoff, MappingProxyType(dict(sorted(ratings.items()))), len(eligible)
    )
