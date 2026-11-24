from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from pl_intel.elo import EloConfig, EloMatch, rate_teams
from pl_intel.normalize import canonical_fixture_id
from pl_intel.sources.openfootball import VerifiedHistoricalResult

KNOWN = datetime(2040, 8, 6, 18, tzinfo=UTC)
LATER = KNOWN + timedelta(hours=3)


def match(
    home: str = "club-a",
    away: str = "club-b",
    score: tuple[int, int] = (2, 1),
    *,
    when: datetime = KNOWN,
    season: str = "2040-2041",
) -> EloMatch:
    return EloMatch(
        VerifiedHistoricalResult(
            canonical_fixture_id(season, home, away),
            *score,
            when,
            "synthetic result evidence",
        ),
        season,
        home,
        away,
        "synthetic identity evidence",
    )


@pytest.mark.parametrize(
    ("score", "home_score"), [((3, 0), 1), ((1, 1), 0.5), ((0, 2), 0)]
)
def test_default_formula_and_equal_opposite_prematch_updates(score, home_score):
    config = EloConfig()
    assert (
        config.initial_rating,
        config.home_advantage,
        config.scale,
        config.k_factor,
    ) == (
        1500,
        60,
        400,
        20,
    )
    expected_home = 1 / (1 + 10 ** (-60 / 400))
    delta = 20 * (home_score - expected_home)
    snapshot = rate_teams((match(score=score),), cutoff=LATER)
    assert snapshot.cutoff == LATER
    assert snapshot.matches_applied == 1
    assert snapshot.ratings == pytest.approx(
        {"club-a": 1500 + delta, "club-b": 1500 - delta}
    )
    assert sum(snapshot.ratings.values()) == pytest.approx(3000)
    with pytest.raises(TypeError):
        snapshot.ratings["club-a"] = 0


def test_cutoff_is_strict_and_late_results_enter_when_known():
    early = match()
    delayed = match("club-b", "club-a", when=LATER)
    for boundary in (KNOWN - timedelta(seconds=1), KNOWN):
        assert rate_teams((early, delayed), cutoff=boundary).ratings == {}
    snapshot = rate_teams((delayed, early), cutoff=LATER)
    assert snapshot == rate_teams((early,), cutoff=LATER)
    assert (
        rate_teams(
            (early, delayed), cutoff=LATER + timedelta(microseconds=1)
        ).matches_applied
        == 2
    )


def test_adding_future_scores_or_metadata_cannot_change_past_snapshot():
    prior = match()
    future = match("promoted-club", "club-a", when=LATER, season="2041-2042")
    future_correction = replace(
        prior, result=replace(prior.result, available_at=LATER, home_goals=0)
    )
    malformed_future = replace(future, home_team_id="INVALID future identity")
    base = rate_teams((prior,), cutoff=LATER)
    assert (
        rate_teams((malformed_future, future_correction, future, prior), cutoff=LATER)
        == base
    )
    assert "promoted-club" not in base.ratings


def test_equal_timestamps_replay_by_fixture_id_independent_of_input_order():
    left, right = sorted(
        (match("club-a", "club-b"), match("club-a", "club-c", score=(0, 1))),
        key=lambda record: record.result.fixture_id,
    )
    sequential = replace(
        right, result=replace(right.result, available_at=KNOWN + timedelta(seconds=1))
    )
    expected = rate_teams((left, sequential), cutoff=LATER)
    assert rate_teams((right, left), cutoff=LATER) == expected
    assert rate_teams((left, right), cutoff=LATER) == expected
    assert list(expected.ratings) == ["club-a", "club-b", "club-c"]


def test_season_carryover_initializes_new_clubs_and_retains_returning_clubs():
    config = EloConfig(home_advantage=0)
    first = match()
    second = match("club-a", "promoted-club", when=LATER, season="2041-2042")
    third_time = LATER + timedelta(days=365)
    third = match(
        "club-b", "promoted-club", (0, 0), when=third_time, season="2042-2043"
    )
    mid = rate_teams((third, second, first), cutoff=third_time, config=config)
    assert mid.matches_applied == 2
    assert mid.ratings["club-b"] == 1490
    expected_second = 1 / (1 + 10 ** ((1500 - 1510) / 400))
    delta_second = 20 * (1 - expected_second)
    assert mid.ratings["club-a"] == pytest.approx(1510 + delta_second)
    assert mid.ratings["promoted-club"] == pytest.approx(1500 - delta_second)
    final = rate_teams(
        (third, second, first), cutoff=third_time + timedelta(seconds=1), config=config
    )
    expected_third = 1 / (1 + 10 ** ((mid.ratings["promoted-club"] - 1490) / 400))
    assert final.ratings["club-b"] == pytest.approx(1490 + 20 * (0.5 - expected_third))
    assert final.ratings["club-a"] == mid.ratings["club-a"]


@pytest.mark.parametrize(
    "value",
    [None, KNOWN.replace(tzinfo=None), KNOWN.astimezone(timezone(timedelta(hours=1)))],
)
def test_cutoff_and_availability_require_aware_utc(value):
    with pytest.raises(ValueError, match="UTC timestamp"):
        rate_teams((), cutoff=value)
    invalid = replace(match(), result=replace(match().result, available_at=value))
    with pytest.raises(ValueError, match="UTC timestamp"):
        rate_teams((invalid,), cutoff=LATER)


@pytest.mark.parametrize(
    "change",
    [
        {"home_team_id": "CLUB-A"},
        {"away_team_id": "club-a"},
        {"away_team_id": "club-c"},
        {"season_id": "2041-2042"},
        {"identity_source_ref": ""},
    ],
)
def test_rejects_invalid_canonical_identity_binding(change):
    with pytest.raises(ValueError):
        rate_teams((replace(match(), **change),), cutoff=LATER)


@pytest.mark.parametrize(
    "change",
    [
        {"fixture_id": "different-fixture"},
        {"source_ref": ""},
        {"home_goals": True},
        {"home_goals": -1},
        {"home_goals": 1.0},
        {"away_goals": None},
        {"away_goals": 2**63},
    ],
)
def test_rejects_malformed_verified_result(change):
    record = match()
    with pytest.raises(ValueError):
        rate_teams(
            (replace(record, result=replace(record.result, **change)),), cutoff=LATER
        )


def test_rejects_unverified_records_and_duplicate_eligible_score_versions():
    record = match()
    with pytest.raises(TypeError, match="verified result fact"):
        rate_teams((replace(record, result=None),), cutoff=LATER)
    with pytest.raises(ValueError, match="Duplicate eligible result"):
        rate_teams((record, record), cutoff=LATER)
    correction = replace(record, result=replace(record.result, home_goals=0))
    with pytest.raises(ValueError, match="Duplicate eligible result"):
        rate_teams((record, correction), cutoff=LATER)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_rating", 0),
        ("scale", 0),
        ("k_factor", -1),
        ("home_advantage", -1),
        ("initial_rating", float("nan")),
        ("home_advantage", float("inf")),
        ("scale", float("-inf")),
        ("k_factor", True),
        ("k_factor", "20"),
        ("k_factor", 10**1000),
    ],
)
def test_rejects_invalid_model_configuration(field, value):
    with pytest.raises(ValueError):
        EloConfig(**{field: value})
