from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from pl_intel.elo import EloConfig, EloMatch, rate_teams
from pl_intel.normalize import canonical_fixture_id
from pl_intel.schemas import Fixture, FixtureStatus
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
    kickoff: datetime | None = None,
) -> EloMatch:
    identity = canonical_fixture_id(season, home, away)
    return EloMatch(
        VerifiedHistoricalResult(identity, *score, when, "synthetic result evidence"),
        Fixture(
            identity,
            season,
            "synthetic",
            identity,
            home,
            away,
            kickoff if kickoff is not None else when - timedelta(hours=2),
            1,
            FixtureStatus.FINISHED,
            when,
            *score,
        ),
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
    malformed_future = replace(
        future, fixture=replace(future.fixture, home_team_id="INVALID future identity")
    )
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
        record = match()
        invalid = (
            replace(record, **change)
            if "identity_source_ref" in change
            else replace(record, fixture=replace(record.fixture, **change))
        )
        rate_teams((invalid,), cutoff=LATER)


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
    correction = replace(
        record,
        result=replace(record.result, home_goals=0),
        fixture=replace(record.fixture, home_goals=0),
    )
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


@pytest.mark.parametrize("offset", [timedelta(seconds=1), timedelta(0)])
def test_verified_availability_must_be_strictly_after_kickoff(offset):
    fixture_time = KNOWN + offset
    record = match(kickoff=fixture_time)
    record = replace(record, fixture=replace(record.fixture, updated_at=LATER))
    with pytest.raises(ValueError, match="strictly after kickoff"):
        rate_teams((record,), cutoff=LATER)


@pytest.mark.parametrize(
    "status",
    [FixtureStatus.SCHEDULED, FixtureStatus.POSTPONED, FixtureStatus.CANCELLED],
)
def test_evidence_cannot_make_unfinished_fixture_eligible(status):
    record = match()
    fixture_record = replace(
        record.fixture, status=status, home_goals=None, away_goals=None
    )
    with pytest.raises(ValueError, match="finished fixture"):
        rate_teams((replace(record, fixture=fixture_record),), cutoff=LATER)


def test_exact_score_version_must_match_reviewed_fixture():
    record = match()
    changed_score = replace(record, result=replace(record.result, home_goals=3))
    with pytest.raises(ValueError, match="score does not match"):
        rate_teams((changed_score,), cutoff=LATER)


def test_earlier_kickoff_with_delayed_evidence_replays_when_known():
    later_kickoff = match("club-a", "club-b", when=KNOWN)
    earlier_kickoff = match(
        "club-a",
        "club-c",
        (0, 1),
        when=LATER,
        kickoff=KNOWN - timedelta(days=1),
    )
    midway = rate_teams((earlier_kickoff, later_kickoff), cutoff=LATER)
    assert midway == rate_teams((later_kickoff,), cutoff=LATER)
    end = rate_teams(
        (earlier_kickoff, later_kickoff), cutoff=LATER + timedelta(seconds=1)
    )
    home = midway.ratings["club-a"]
    probability = 1 / (1 + 10 ** ((1500 - home - 60) / 400))
    assert end.ratings["club-a"] == pytest.approx(home - 20 * probability)


def test_update_timestamp_never_substitutes_for_verified_availability():
    record = match()
    later_observation = replace(
        record, fixture=replace(record.fixture, updated_at=LATER + timedelta(days=1))
    )
    assert rate_teams((record,), cutoff=LATER) == rate_teams(
        (later_observation,), cutoff=LATER
    )


def test_input_cap_counts_future_records_and_does_not_drain_iterable():
    future = match(when=LATER)

    def records(count):
        for _ in range(count):
            yield future
        pytest.fail("Oversized iterator was drained")

    assert (
        rate_teams((future for _ in range(100_000)), cutoff=LATER).matches_applied == 0
    )
    with pytest.raises(ValueError, match="100,000 supplied records"):
        rate_teams(records(100_001), cutoff=LATER)


@pytest.mark.parametrize(
    "field,low,high",
    [
        ("initial_rating", 1, 10_000),
        ("home_advantage", 0, 400),
        ("scale", 1, 2_000),
        ("k_factor", 1, 100),
    ],
)
def test_config_bounds_are_inclusive(field, low, high):
    assert getattr(EloConfig(**{field: low}), field) == low
    assert getattr(EloConfig(**{field: high}), field) == high
    for invalid in (low - 0.5, high + 0.5):
        with pytest.raises(ValueError):
            EloConfig(**{field: invalid})
