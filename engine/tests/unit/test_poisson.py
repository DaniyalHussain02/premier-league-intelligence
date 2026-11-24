from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from math import exp, factorial, fsum, isfinite

import pytest

from pl_intel import poisson
from pl_intel.elo import EloConfig, EloMatch, rate_teams
from pl_intel.normalize import canonical_fixture_id
from pl_intel.poisson import PoissonConfig, predict_match, scoreline_distribution
from pl_intel.schemas import Fixture, FixtureStatus
from pl_intel.sources.openfootball import VerifiedHistoricalResult

KICKOFF = datetime(2040, 8, 6, 18, tzinfo=UTC)


def historical_result(season, available_at, score=(2, 1)):
    fixture_id = canonical_fixture_id(season, "club-a", "club-b")
    fixture = Fixture(
        fixture_id,
        season,
        "synthetic",
        fixture_id,
        "club-a",
        "club-b",
        available_at - timedelta(hours=3),
        1,
        FixtureStatus.FINISHED,
        available_at,
        *score,
    )
    return EloMatch(
        VerifiedHistoricalResult(
            fixture_id, *score, available_at, "synthetic score evidence"
        ),
        fixture,
        "synthetic identity evidence",
    )


def forecast(matches=(), **options):
    defaults = {
        "home_team_id": "club-a",
        "away_team_id": "club-b",
        "kickoff_utc": KICKOFF,
        "cutoff": KICKOFF,
    }
    return predict_match(matches, **(defaults | options))


def test_neutral_equal_strength_is_symmetric():
    prediction = forecast(elo_config=EloConfig(home_advantage=0))
    assert prediction.model_version == "poisson-elo-v1"
    assert prediction.home_rate == prediction.away_rate == 1.35
    assert prediction.home_win == pytest.approx(prediction.away_win, abs=1e-15)
    assert 0 < prediction.draw < 1


def test_home_advantage_applied_once_at_elo_scale():
    prediction = forecast()
    assert prediction.home_rate == pytest.approx(1.35 * 10 ** (0.5 * 60 / 400))
    assert prediction.away_rate == pytest.approx(1.35 * 10 ** (-0.5 * 60 / 400))
    assert prediction.home_win > prediction.away_win


@pytest.mark.parametrize("score,home_is_stronger", [((2, 0), True), ((0, 2), False)])
def test_extreme_strength_differences_and_rate_bounds(score, home_is_stronger):
    config = EloConfig(home_advantage=0, scale=50, k_factor=100)
    records = tuple(
        historical_result(f"season-{i}", KICKOFF - timedelta(days=40 - i), score)
        for i in range(32)
    )
    ratings = rate_teams(records, cutoff=KICKOFF, config=config).ratings
    assert abs(ratings["club-a"] - ratings["club-b"]) > 2 * config.scale
    prediction = forecast(records, elo_config=config)
    expected = (8, 0.135) if home_is_stronger else (0.135, 8)
    assert (prediction.home_rate, prediction.away_rate) == pytest.approx(expected)
    floor = forecast(records, elo_config=config, poisson_config=PoissonConfig(0.05, 1))
    assert min(floor.home_rate, floor.away_rate) == 0.05


def test_unseen_club_uses_supplied_initial_rating():
    config = EloConfig(initial_rating=1200, scale=200, home_advantage=20, k_factor=25)
    records = (historical_result("synthetic-season", KICKOFF - timedelta(days=1)),)
    prediction = forecast(records, away_team_id="club-new", elo_config=config)
    expected_elo_home = 1 / (1 + 10 ** (-20 / 200))
    strength = (25 * (1 - expected_elo_home) + 20) / 200
    assert prediction.home_rate == pytest.approx(1.35 * 10 ** (0.5 * strength))
    assert prediction.away_rate == pytest.approx(1.35 * 10 ** (-0.5 * strength))


def test_two_zero_rates_make_certain_zero_zero_draw():
    result = scoreline_distribution(0, 0)
    assert result.scorelines == ((1.0,),)
    assert (result.home_win, result.draw, result.away_win) == (0.0, 1.0, 0.0)
    assert (result.retained_mass, result.omitted_mass, result.tail_bound) == (
        1.0,
        0.0,
        0.0,
    )


def test_conditional_cells_match_theoretical_pmf_after_restoring_retained_mass():
    result = scoreline_distribution(1.5, 0.7)
    assert result.scorelines[0][0] * result.retained_mass == pytest.approx(
        exp(-2.2), abs=1e-15
    )
    assert result.scorelines[2][1] * result.retained_mass == pytest.approx(
        exp(-2.2) * 1.5**2 * 0.7 / 2, abs=1e-15
    )
    with pytest.raises(FrozenInstanceError):
        result.home_rate = 2
    with pytest.raises(TypeError):
        result.scorelines[0][0] = 0


@pytest.mark.parametrize(
    "home,away", [(0, 8), (1e-15, 0.05), (0.05, 0.1), (1.35, 1.35), (8, 8)]
)
def test_tail_bound_and_normalization(home, away):
    result = scoreline_distribution(home, away)
    grid = result.scorelines
    assert 1 <= len(grid) <= 65
    assert all(1 <= len(row) == len(grid[0]) <= 65 for row in grid)
    assert all(isfinite(cell) and 0 <= cell <= 1 for row in grid for cell in row)
    indexed = [(h, a, cell) for h, row in enumerate(grid) for a, cell in enumerate(row)]
    assert fsum(cell for _, _, cell in indexed) == pytest.approx(1, abs=1e-12, rel=0)
    assert (result.home_win, result.draw, result.away_win) == (
        fsum(p for h, a, p in indexed if h > a),
        fsum(p for h, a, p in indexed if h == a),
        fsum(p for h, a, p in indexed if h < a),
    )
    assert fsum((result.home_win, result.draw, result.away_win)) == pytest.approx(
        1, abs=1e-12, rel=0
    )
    assert 0 <= result.omitted_mass <= result.tail_bound + 1e-14
    assert result.tail_bound <= 1e-12
    assert result.retained_mass + result.omitted_mass == pytest.approx(
        1, abs=1e-14, rel=0
    )

    def marginal_tail(rate, first_omitted):
        if rate == 0:
            return 0
        return fsum(
            exp(-rate) * rate**k / factorial(k) for k in range(first_omitted, 100)
        )

    h_tail = marginal_tail(home, len(grid))
    a_tail = marginal_tail(away, len(grid[0]))
    true_tail = h_tail + a_tail - h_tail * a_tail
    assert true_tail <= result.tail_bound + 1e-26
    assert result.omitted_mass == pytest.approx(true_tail, abs=1e-14, rel=0)


def test_goal_cap_fails_when_geometric_ratio_is_not_below_one(monkeypatch):
    monkeypatch.setattr(poisson, "_MAX_GOALS", 6)
    # At this cap and rate the subsequent ratio is exactly one.
    with pytest.raises(ArithmeticError, match="goal cap"):
        scoreline_distribution(8, 0)


@pytest.mark.parametrize(
    "rate",
    [True, float("nan"), float("inf"), -0.01, 8.01, "1.5", Decimal("1.5"), 10**1000],
)
def test_invalid_rates_are_rejected(rate):
    with pytest.raises(ValueError):
        scoreline_distribution(rate, 1.0)
    with pytest.raises(ValueError):
        scoreline_distribution(1.0, rate)


@pytest.mark.parametrize(
    "config",
    [
        {"baseline_goals": 0},
        {"baseline_goals": 8.1},
        {"baseline_goals": True},
        {"strength_sensitivity": -0.1},
        {"strength_sensitivity": 1.1},
        {"strength_sensitivity": float("nan")},
        {"strength_sensitivity": float("inf")},
        {"strength_sensitivity": Decimal("0.5")},
    ],
)
def test_invalid_poisson_configuration(config):
    with pytest.raises(ValueError):
        PoissonConfig(**config)


@pytest.mark.parametrize(
    "options",
    [
        {"home_team_id": "CLUB-A"},
        {"away_team_id": "club-a"},
        {"kickoff_utc": KICKOFF.replace(tzinfo=None)},
        {"cutoff": KICKOFF.astimezone(timezone(timedelta(hours=1)))},
        {"cutoff": KICKOFF + timedelta(microseconds=1)},
    ],
)
def test_invalid_prediction_identity_or_cutoff(options):
    with pytest.raises(ValueError):
        forecast(**options)


def test_incompatible_configuration_types_fail_closed():
    with pytest.raises(TypeError):
        forecast(elo_config=PoissonConfig())
    with pytest.raises(TypeError):
        forecast(poisson_config=EloConfig())


def test_cutoff_invariance_and_replay_repeatability():
    old = historical_result("season-old", KICKOFF - timedelta(days=2))
    known = historical_result("season-known", KICKOFF - timedelta(seconds=1), (0, 1))
    at_cutoff = historical_result("season-equal", KICKOFF)
    later = historical_result("season-late", KICKOFF + timedelta(hours=1))
    expected = forecast((old, known))
    assert expected != forecast((old,))
    assert forecast((later, at_cutoff, known, old)) == expected
    assert forecast((known, old, at_cutoff, later)) == expected
