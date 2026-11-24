"""Deterministic independent-goals match probabilities with provisional parameters."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import exp, fsum

from pl_intel.elo import EloConfig, EloMatch, rate_teams
from pl_intel.validate import require_canonical_id

MODEL_VERSION = "poisson-elo-v1"
_DEFAULT_ELO_CONFIG = EloConfig()
_MAX_GOALS = 64
_MAX_RATE = 8.0
_MIN_MODELED_RATE = 0.05
_TAIL_TOLERANCE = 1e-12
_NORMALIZATION_TOLERANCE = 1e-12
# Allow for accumulated PMF recurrence rounding and subtraction from one.
_MASS_ROUNDOFF = 1e-14


@dataclass(frozen=True, slots=True)
class PoissonConfig:
    """Fixed, provisional parameters awaiting chronological backtesting."""

    baseline_goals: float = 1.35
    strength_sensitivity: float = 0.5

    def __post_init__(self) -> None:
        _require_number(
            "Baseline goals", self.baseline_goals, _MIN_MODELED_RATE, _MAX_RATE
        )
        _require_number("Strength sensitivity", self.strength_sensitivity, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class MatchProbabilities:
    """The conditional grid and 1X2 share the same retained scoreline support.

    Index scorelines by [home_goals][away_goals]. Rates can be used for
    full-support independent Poisson sampling by a downstream simulator.
    """

    model_version: str
    home_rate: float
    away_rate: float
    scorelines: tuple[tuple[float, ...], ...]
    retained_mass: float
    omitted_mass: float
    tail_bound: float
    home_win: float
    draw: float
    away_win: float


def _require_number(name: str, value: float, minimum: float, maximum: float) -> None:
    if type(value) not in {int, float} or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be a finite number in [{minimum}, {maximum}]")


def _marginal(rate: float) -> tuple[list[float], float]:
    terms = [exp(-rate)]
    for goals in range(_MAX_GOALS + 1):
        first_omitted = terms[-1] * rate / (goals + 1)
        # Ratios decline beyond the first omitted term. The geometric
        # bound is valid only once the largest remaining ratio is below one.
        ratio = rate / (goals + 2)
        if ratio < 1:
            bound = first_omitted / (1 - ratio)
            if bound <= _TAIL_TOLERANCE / 2:
                return terms, bound
        if goals < _MAX_GOALS:
            terms.append(first_omitted)
    raise ArithmeticError("Poisson goal cap cannot meet the tail tolerance")


def _validate_distribution(probabilities: Iterable[float]) -> None:
    values = tuple(probabilities)
    if any(not 0 <= value <= 1 for value in values) or (
        abs(fsum(values) - 1) > _NORMALIZATION_TOLERANCE
    ):
        raise ArithmeticError("Invalid normalized probability distribution")


def scoreline_distribution(home_rate: float, away_rate: float) -> MatchProbabilities:
    """Condition two independent Poisson goal counts on the retained grid."""
    _require_number("Home rate", home_rate, 0.0, _MAX_RATE)
    _require_number("Away rate", away_rate, 0.0, _MAX_RATE)
    home, home_tail = _marginal(home_rate)
    away, away_tail = _marginal(away_rate)
    tail_bound = fsum((home_tail, away_tail))
    if tail_bound > _TAIL_TOLERANCE:
        raise ArithmeticError("Joint Poisson tail exceeds the tolerance")

    raw = tuple(tuple(h * a for a in away) for h in home)
    retained = fsum(value for row in raw for value in row)
    omitted = 1.0 - retained
    if retained <= 0 or not -_MASS_ROUNDOFF <= omitted <= tail_bound + _MASS_ROUNDOFF:
        raise ArithmeticError("Retained scoreline mass disagrees with the tail bound")

    grid = tuple(tuple(value / retained for value in row) for row in raw)
    _validate_distribution(value for row in grid for value in row)
    home_win = fsum(
        value for h, row in enumerate(grid) for a, value in enumerate(row) if h > a
    )
    draw = fsum(row[i] for i, row in enumerate(grid) if i < len(row))
    away_win = fsum(
        value for h, row in enumerate(grid) for a, value in enumerate(row) if h < a
    )
    _validate_distribution((home_win, draw, away_win))
    return MatchProbabilities(
        MODEL_VERSION,
        float(home_rate),
        float(away_rate),
        grid,
        min(1.0, retained),
        max(0.0, omitted),
        tail_bound,
        home_win,
        draw,
        away_win,
    )


def predict_match(
    matches: Iterable[EloMatch],
    *,
    home_team_id: str,
    away_team_id: str,
    kickoff_utc: datetime,
    cutoff: datetime,
    elo_config: EloConfig = _DEFAULT_ELO_CONFIG,
    poisson_config: PoissonConfig = PoissonConfig(),
) -> MatchProbabilities:
    """Predict from independently reviewed kickoff and historical result evidence.

    T07 admits results available strictly before cutoff. Within its input cap,
    adding later results with valid UTC availability leaves predictions intact.
    The caller verifies the kickoff and historical evidence. Stored Elo ratings
    carry no venue offset, so this conversion applies home advantage once.
    Clamp (home - away + advantage) / Elo scale to [-2, 2], multiply
    by strength sensitivity, then scale the baseline by 10 to that power
    for the home rate and its reciprocal for the away rate. Clamp both
    modeled rates to [0.05, 8].
    """
    for value in (kickoff_utc, cutoff):
        if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
            raise ValueError("Expected timezone-aware UTC kickoff and cutoff")
    if cutoff > kickoff_utc:
        raise ValueError("Prediction cutoff must be on or before kickoff")
    require_canonical_id(home_team_id)
    require_canonical_id(away_team_id)
    if home_team_id == away_team_id:
        raise ValueError("Prediction requires distinct home and away clubs")
    if not isinstance(poisson_config, PoissonConfig):
        raise TypeError("Expected a PoissonConfig")

    snapshot = rate_teams(matches, cutoff=cutoff, config=elo_config)
    home = snapshot.ratings.get(home_team_id, elo_config.initial_rating)
    away = snapshot.ratings.get(away_team_id, elo_config.initial_rating)
    difference = (home - away + elo_config.home_advantage) / elo_config.scale
    strength = poisson_config.strength_sensitivity * max(-2.0, min(2.0, difference))
    baseline = poisson_config.baseline_goals
    home_rate = max(_MIN_MODELED_RATE, min(_MAX_RATE, baseline * 10.0**strength))
    away_rate = max(_MIN_MODELED_RATE, min(_MAX_RATE, baseline * 10.0**-strength))
    return scoreline_distribution(home_rate, away_rate)
