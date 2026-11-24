from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import permutations
from math import fsum

import pytest

from pl_intel import simulate
from pl_intel.elo import EloMatch
from pl_intel.normalize import canonical_fixture_id
from pl_intel.ranking import (
    AdjustmentEvidence,
    AdjustmentStatus,
    PointsAdjustment,
    QualificationEvidence,
)
from pl_intel.rules import verified_ruleset
from pl_intel.schemas import Fixture, FixtureStatus, Season, SeasonStatus, Team
from pl_intel.simulate import (
    ReviewedFixture,
    SeasonSnapshot,
    SimulationBlocked,
    SimulationConfig,
    SimulationResult,
    simulate_season,
)
from pl_intel.sources.openfootball import VerifiedHistoricalResult

CUTOFF = datetime(2027, 5, 1, tzinfo=UTC)
PAST = CUTOFF - timedelta(days=2)
FUTURE = CUTOFF + timedelta(days=2)


def season_input(
    *, all_draws=False, unfinished=None, shared_rank=False, title_tiebreak=False
):
    season = Season(
        "2026-2027",
        date(2026, 8, 1),
        date(2027, 5, 31),
        SeasonStatus.ACTIVE,
        "pl-2026-2027-section-c",
        "synthetic-calendar",
    )
    rules = verified_ruleset(season.season_id, season.ruleset_version)
    teams = tuple(Team(f"club-{i:02}", f"Club {i}", f"C{i}") for i in range(20))
    fixtures = []
    history = []
    pairs = list(permutations(teams, 2))
    pairs.remove((teams[5], teams[10]))
    pairs.insert(0, (teams[5], teams[10]))
    for index, (home, away) in enumerate(pairs):
        scheduled = index == 0 and unfinished is not None
        goals = (
            (0, 0) if all_draws else (2, 0) if home.team_id < away.team_id else (0, 2)
        )
        if shared_rank and {home.team_id, away.team_id} == {"club-05", "club-06"}:
            goals = (0, 0)
        if title_tiebreak:
            goals = {
                ("club-00", "club-01"): (2, 1),
                ("club-01", "club-00"): (2, 0),
                ("club-00", "club-02"): (3, 0),
                ("club-01", "club-02"): (2, 1),
            }.get((home.team_id, away.team_id), goals)
        fixture = Fixture(
            canonical_fixture_id(season.season_id, home.team_id, away.team_id),
            season.season_id,
            "synthetic",
            str(index),
            home.team_id,
            away.team_id,
            FUTURE if scheduled else PAST,
            index % 38 + 1,
            unfinished if scheduled else FixtureStatus.FINISHED,
            PAST,
            *((None, None) if scheduled else goals),
        )
        fixtures.append(
            ReviewedFixture(
                fixture,
                PAST,
                "synthetic-fixture",
                scheduled and unfinished is FixtureStatus.POSTPONED,
            )
        )
        if not scheduled:
            history.append(
                EloMatch(
                    VerifiedHistoricalResult(
                        fixture.fixture_id,
                        *goals,
                        PAST + timedelta(hours=3),
                        "synthetic-score",
                    ),
                    fixture,
                    "synthetic-identity",
                )
            )
    return SeasonSnapshot(
        season,
        teams,
        tuple(fixtures),
        CUTOFF,
        "synthetic-v1",
        "synthetic-snapshot",
        rules,
        AdjustmentEvidence(
            season.season_id, AdjustmentStatus.VERIFIED_ZERO, "synthetic-zero"
        ),
        QualificationEvidence(season.season_id, (), "synthetic-qualification"),
    ), tuple(history)


def test_completed_season_and_deduction():
    snapshot, history = season_input()
    adjustment = PointsAdjustment(
        snapshot.season.season_id, "club-00", -1, "synthetic-decision"
    )
    snapshot = replace(
        snapshot,
        adjustments=AdjustmentEvidence(
            snapshot.season.season_id,
            AdjustmentStatus.VERIFIED_ADJUSTMENTS,
            "synthetic-adjustments",
            (adjustment,),
        ),
    )
    result = simulate_season(snapshot, history, cutoff=CUTOFF)
    assert isinstance(result, SimulationResult)
    winner = result.clubs[0]
    assert winner.title_probability == 1
    assert winner.actual_earned_points == winner.expected_earned_points == 114
    assert winner.actual_adjusted_points == winner.expected_adjusted_points == 113
    assert [club.team_id for club in result.clubs if club.relegation_probability] == [
        "club-17",
        "club-18",
        "club-19",
    ]


@pytest.mark.parametrize("status", [FixtureStatus.SCHEDULED, FixtureStatus.POSTPONED])
def test_partial_season_reproducible_across_batches_and_input_order(status):
    snapshot, history = season_input(unfinished=status)
    result = simulate_season(
        snapshot, history, cutoff=CUTOFF, config=SimulationConfig(7, 2, 12)
    )
    reversed_snapshot = replace(
        snapshot, teams=snapshot.teams[::-1], fixtures=snapshot.fixtures[::-1]
    )
    other = simulate_season(
        reversed_snapshot,
        reversed(history),
        cutoff=CUTOFF,
        config=SimulationConfig(7, 3, 12),
    )
    assert isinstance(result, SimulationResult)
    assert result.clubs == other.clubs
    assert result.provenance.input_fingerprint == other.provenance.input_fingerprint
    assert all(fsum(club.positions) == pytest.approx(1) for club in result.clubs)
    assert all(
        fsum(column) == pytest.approx(1)
        for column in zip(*(c.positions for c in result.clubs), strict=True)
    )
    assert fsum(c.title_probability for c in result.clubs) == 1
    assert fsum(c.relegation_probability for c in result.clubs) == 3
    assert all(
        c.expected_adjusted_points >= c.actual_adjusted_points for c in result.clubs
    )


def test_cutoff_excludes_future_history_and_rejects_missing_exact_result():
    snapshot, history = season_input()
    later = replace(
        history[0],
        result=replace(history[0].result, available_at=CUTOFF, home_goals=99),
    )
    expected = simulate_season(snapshot, history, cutoff=CUTOFF)
    assert simulate_season(snapshot, (*history, later), cutoff=CUTOFF) == expected
    blocked = simulate_season(snapshot, (*history[1:], later), cutoff=CUTOFF)
    assert blocked == SimulationBlocked(
        ("missing_exact_verified_score",), history[0].fixture.fixture_id
    )


@pytest.mark.parametrize(
    ("status", "kickoff", "reason"),
    [
        (FixtureStatus.CANCELLED, FUTURE, "unresolved_cancellation"),
        (FixtureStatus.SCHEDULED, PAST, "overdue_unfinished_fixture"),
        (FixtureStatus.POSTPONED, FUTURE, "unconfirmed_replacement_kickoff"),
    ],
)
def test_unresolved_fixtures_block(status, kickoff, reason):
    snapshot, history = season_input(unfinished=status)
    reviewed = snapshot.fixtures[0]
    changed = replace(
        reviewed,
        fixture=replace(reviewed.fixture, kickoff_utc=kickoff),
        replacement_kickoff_confirmed=False,
    )
    result = simulate_season(
        replace(snapshot, fixtures=(changed, *snapshot.fixtures[1:])),
        history,
        cutoff=CUTOFF,
    )
    assert result == SimulationBlocked((reason,), reviewed.fixture.fixture_id)


def test_invalid_inventories_and_configuration():
    snapshot, history = season_input()
    for fixtures in (
        snapshot.fixtures[:-1],
        (*snapshot.fixtures[:-1], snapshot.fixtures[0]),
    ):
        with pytest.raises(ValueError):
            simulate_season(
                replace(snapshot, fixtures=fixtures), history, cutoff=CUTOFF
            )
    for options in ({"batch_size": 513}, {"trials": 50_001}, {"seed": True}):
        with pytest.raises(ValueError):
            SimulationConfig(**options)
    with pytest.raises(ValueError, match="UTC"):
        simulate_season(snapshot, history, cutoff=CUTOFF.replace(tzinfo=None))


def test_unknown_adjustment_and_consequential_tie_block():
    snapshot, history = season_input(all_draws=True)
    unknown = replace(
        snapshot,
        adjustments=AdjustmentEvidence(
            snapshot.season.season_id, AdjustmentStatus.UNKNOWN
        ),
    )
    assert simulate_season(unknown, history, cutoff=CUTOFF) == SimulationBlocked(
        ("unknown_adjustments",)
    )
    result = simulate_season(snapshot, history, cutoff=CUTOFF)
    assert isinstance(result, SimulationBlocked)
    assert "unresolved_final_tie" in result.reasons


def test_samples_each_pending_slot_once_and_never_returns_partial_result(monkeypatch):
    snapshot, history = season_input(unfinished=FixtureStatus.SCHEDULED)
    original_predict = simulate.predict_match
    original_rank = simulate.calculate_standings
    predictions = []
    finals = []

    def predict(*args, **kwargs):
        predictions.append(kwargs)
        return original_predict(*args, **kwargs)

    def rank(season, teams, fixtures, **kwargs):
        result = original_rank(season, teams, fixtures, **kwargs)
        if kwargs["phase"] is simulate.StandingsPhase.FINAL:
            finals.append(fixtures)
            assert len({f.fixture_id for f in fixtures}) == 380
            assert all(f.status is FixtureStatus.FINISHED for f in fixtures)
            assert {m.fixture for m in history}.issubset(fixtures)
            if len(finals) == 2:
                return replace(result, blockers=("unresolved_final_tie",))
        return result

    monkeypatch.setattr(simulate, "predict_match", predict)
    monkeypatch.setattr(simulate, "calculate_standings", rank)
    result = simulate_season(
        snapshot, history, cutoff=CUTOFF, config=SimulationConfig(4, 3)
    )
    assert result == SimulationBlocked(("unresolved_final_tie",), trial=2)
    assert len(predictions) == 1
    assert len(finals) == 2


def test_ordinary_shared_rank_fractionally_occupies_both_positions():
    snapshot, history = season_input(shared_rank=True)
    result = simulate_season(snapshot, history, cutoff=CUTOFF)
    assert isinstance(result, SimulationResult)
    for club in result.clubs[5:7]:
        assert club.positions[5:7] == (0.5, 0.5)
        assert sum(club.positions) == 1
        assert club.title_probability == club.relegation_probability == 0


def test_future_fixture_revision_is_blocked():
    snapshot, history = season_input(unfinished=FixtureStatus.SCHEDULED)
    changed = replace(snapshot.fixtures[0], known_at=FUTURE)
    result = simulate_season(
        replace(snapshot, fixtures=(changed, *snapshot.fixtures[1:])),
        history,
        cutoff=CUTOFF,
    )
    assert result == SimulationBlocked(
        ("fixture_state_not_known_at_cutoff",), changed.fixture.fixture_id
    )


def test_verified_head_to_head_away_goals_resolve_title():
    snapshot, history = season_input(title_tiebreak=True)
    result = simulate_season(snapshot, history, cutoff=CUTOFF)
    assert isinstance(result, SimulationResult)
    first, second = result.clubs[:2]
    assert first.actual_earned_points == second.actual_earned_points
    assert second.title_probability == 1
    assert first.positions[1] == 1
    assert result.provenance.ruleset_version == snapshot.rules.version
    assert "synthetic-score" in result.provenance.evidence_refs
