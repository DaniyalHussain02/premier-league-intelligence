from dataclasses import replace
from fractions import Fraction
from itertools import permutations
from math import inf, nan, nextafter
from random import Random

import pytest

from pl_intel import assignment
from pl_intel.assignment import assign_representative_table
from pl_intel.simulate import ClubForecast, SimulationBlocked, SimulationResult


def _forecast(block=()):
    clubs = []
    for row in range(20):
        weights = [float(row == column) for column in range(20)]
        if row < len(block):
            weights[: len(block)] = block[row]
        clubs.append(
            ClubForecast(
                f"club-{row:02}",
                tuple(weights),
                0.0,
                0.0,
                0,
                0,
                float(row),
                float(row) - 0.5,
            )
        )
    # Assignment consumes club rows only. No simulator run is needed for this fixture.
    return SimulationResult(tuple(clubs), None)


def _replace_club(forecast, index, **changes):
    clubs = list(forecast.clubs)
    clubs[index] = replace(clubs[index], **changes)
    return replace(forecast, clubs=tuple(clubs))


def _assert_exact_optimum(block, result):
    size = len(block)
    candidates = []
    for rows_at_rank in permutations(range(size)):
        score = sum(
            Fraction(block[row][position]) for position, row in enumerate(rows_at_rank)
        )
        candidates.append((score, rows_at_rank))
    best = max(score for score, _ in candidates)
    winner = min(order for score, order in candidates if score == best)
    assert tuple(row.team_id for row in result.rows[:size]) == tuple(
        f"club-{row:02}" for row in winner
    )
    assert sum(Fraction(row.assigned_probability) for row in result.rows[:size]) == best


def test_competing_modal_positions_require_global_optimum():
    block = ((0.50, 0.45, 0.05), (0.48, 0.32, 0.20), (0.02, 0.23, 0.75))
    result = assign_representative_table(_forecast(block))
    assert [row.team_id for row in result.rows[:3]] == ["club-01", "club-00", "club-02"]
    assert result.total_assigned_probability == pytest.approx(17 + 0.48 + 0.45 + 0.75)
    assert result.total_assigned_probability > 17 + 0.50 + 0.32 + 0.75
    _assert_exact_optimum(block, result)


def test_uniform_matrix_assigns_twenty_unique_positions_by_team_id():
    forecast = _forecast(((0.05,) * 20,) * 20)
    reversed_forecast = replace(forecast, clubs=forecast.clubs[::-1])
    result = assign_representative_table(reversed_forecast)
    assert result == assign_representative_table(forecast)
    assert [row.predicted_position for row in result.rows] == list(range(1, 21))
    assert [row.team_id for row in result.rows] == [
        club.team_id for club in forecast.clubs
    ]
    assert result.total_assigned_probability == 1.0


def test_multiple_optima_choose_lexicographic_sequence_in_position_order():
    block = ((0.5, 0.5, 0), (0.5, 0, 0.5), (0, 0.5, 0.5))
    forecast = _forecast(block)
    shuffled = list(forecast.clubs)
    Random(42).shuffle(shuffled)
    result = assign_representative_table(forecast)
    assert (
        assign_representative_table(replace(forecast, clubs=tuple(shuffled))) == result
    )
    assert [row.team_id for row in result.rows[:3]] == ["club-00", "club-02", "club-01"]
    _assert_exact_optimum(block, result)


def test_near_tie_preserves_strictly_better_exact_score():
    up = nextafter(0.5, 1.0)
    block = ((0.5, up), (0.5, 0.5))
    assert sum((up, 0.5)) == sum((0.5, 0.5))
    result = assign_representative_table(_forecast(block))
    assert result.rows[0].team_id == "club-01"
    _assert_exact_optimum(block, result)


@pytest.mark.parametrize("size", [2, 3, 4, 5])
def test_exact_fallback_and_tie_resolution_against_exhaustive_oracle(monkeypatch, size):
    rng = Random(size)
    block = [[0.0] * size for _ in range(size)]
    for _ in range(16):
        columns = list(range(size))
        rng.shuffle(columns)
        for row, column in enumerate(columns):
            block[row][column] += 1 / 16

    original = assignment.linear_sum_assignment
    fallback = assignment._exact_assignment
    recovered = []

    def solve(costs):
        if len(costs) == 20:
            assert costs[0][0] == -block[0][0]
            # Swap two disjoint identity rows to force a suboptimal candidate.
            return range(20), [*range(18), 19, 18]
        return original(costs)

    def record_fallback(weights):
        recovered.append(True)
        return fallback(weights)

    monkeypatch.setattr(assignment, "linear_sum_assignment", solve)
    monkeypatch.setattr(assignment, "_exact_assignment", record_fallback)
    result = assign_representative_table(_forecast(block))
    assert recovered == [True]
    _assert_exact_optimum(block, result)
    assert [row.team_id for row in result.rows[size:]] == [
        f"club-{row:02}" for row in range(size, 20)
    ]


def test_expected_points_are_preserved_without_affecting_positions():
    forecast = _forecast()
    first = assign_representative_table(forecast)
    changed = _replace_club(
        forecast, 0, expected_earned_points=115, expected_adjusted_points=-3
    )
    second = assign_representative_table(changed)
    assert second.rows[0].expected_earned_points == 115
    assert second.rows[0].expected_adjusted_points == -3
    assert first.rows[1:] == second.rows[1:]
    assert second.rows[0].team_id == first.rows[0].team_id
    assert first.total_assigned_probability == second.total_assigned_probability == 20
    assert "the probability of the entire table" in second.explanation


@pytest.mark.parametrize("count", [19, 21])
def test_invalid_club_count(count):
    forecast = _forecast()
    with pytest.raises(ValueError, match="exactly 20 clubs"):
        assign_representative_table(
            replace(forecast, clubs=(forecast.clubs + forecast.clubs)[:count])
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"team_id": "club-01"}, "Duplicate club ID"),
        ({"team_id": "Not a canonical ID"}, "canonical"),
        ({"positions": (1.0,)}, "exactly 20 position"),
        ({"positions": (1.0,) + (0.0,) * 20}, "exactly 20 position"),
        ({"expected_earned_points": inf}, "Expected points"),
        ({"expected_adjusted_points": nan}, "Expected points"),
        ({"expected_earned_points": True}, "Expected points"),
    ],
)
def test_invalid_club_rows(changes, message):
    with pytest.raises(ValueError, match=message):
        assign_representative_table(_replace_club(_forecast(), 0, **changes))


@pytest.mark.parametrize("invalid", [nan, inf, -inf, -0.01, 1.01, True, "0.5"])
def test_invalid_probability(invalid):
    with pytest.raises(ValueError, match="finite and in"):
        assign_representative_table(
            _replace_club(_forecast(), 0, positions=(invalid,) + (0.0,) * 19)
        )


def test_invalid_column_totals_even_when_each_row_sums_to_one():
    with pytest.raises(ValueError, match="columns"):
        assign_representative_table(
            _replace_club(_forecast(), 0, positions=(0.0, 1.0) + (0.0,) * 18)
        )


@pytest.mark.parametrize("within", [False, True])
def test_normalization_tolerance_boundaries(within):
    # Find the adjacent representable totals that straddle the tolerance.
    accepted = 1.0 - 1e-12
    rejected = nextafter(accepted, 0.0)
    assert 1.0 - accepted <= 1e-12 < 1.0 - rejected
    value = accepted if within else rejected
    forecast = _replace_club(_forecast(), 0, positions=(value,) + (0.0,) * 19)
    if within:
        assert (
            assign_representative_table(forecast).rows[0].assigned_probability == value
        )
    else:
        with pytest.raises(ValueError, match="rows"):
            assign_representative_table(forecast)


def test_rejects_blocked_simulation():
    with pytest.raises(TypeError, match="successful simulation"):
        assign_representative_table(SimulationBlocked(("unresolved_final_tie",)))
