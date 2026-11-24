from dataclasses import replace
from datetime import UTC, date, datetime, time
from itertools import permutations

import pytest

from pl_intel.normalize import canonical_fixture_id
from pl_intel.ranking import (
    AdjustmentEvidence,
    AdjustmentStatus,
    PointsAdjustment,
    QualificationEvidence,
    RankingBlocker,
    StandingsPhase,
    calculate_standings,
)
from pl_intel.rules import RankingProcedure, Ruleset, RuleVerification, verified_ruleset
from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    ReschedulingApproval,
    Season,
    SeasonStatus,
    Team,
)
from pl_intel.season import (
    ResolutionReason,
    ResolutionStatus,
    SeasonCandidate,
    resolve_active_season,
)

SEASON = Season(
    "2040-2041",
    date(2040, 8, 1),
    date(2041, 5, 31),
    SeasonStatus.ACTIVE,
    "synthetic-v1",
    "synthetic",
)
NEXT = replace(
    SEASON,
    season_id="2041-2042",
    start_date=date(2041, 8, 1),
    end_date=date(2042, 5, 31),
)
ROSTER = tuple(Team(f"club-{n:02}", f"Synthetic Club {n}", f"S{n}") for n in range(20))
SYNTHETIC = Ruleset(
    SEASON.season_id,
    SEASON.ruleset_version,
    RuleVerification.SYNTHETIC,
    "synthetic rules for tests only",
    RankingProcedure.EPL_SECTION_C,
    relegation_places=1,
)
ZERO = AdjustmentEvidence(
    SEASON.season_id, AdjustmentStatus.VERIFIED_ZERO, "synthetic zero evidence"
)
QUALIFICATION = QualificationEvidence(
    SEASON.season_id, (), "synthetic boundary evidence"
)


def fixture(
    home: Team,
    away: Team,
    score: tuple[int, int] | None = None,
    *,
    season: Season = SEASON,
    day: date | None = None,
    status: FixtureStatus = FixtureStatus.SCHEDULED,
) -> Fixture:
    identity = canonical_fixture_id(season.season_id, home.team_id, away.team_id)
    kickoff = datetime.combine(day or season.start_date, time(15), UTC)
    goals = score or (None, None)
    return Fixture(
        identity,
        season.season_id,
        "synthetic",
        identity,
        home.team_id,
        away.team_id,
        kickoff,
        1,
        FixtureStatus.FINISHED if score is not None else status,
        kickoff,
        *goals,
    )


def candidate(season: Season = SEASON) -> SeasonCandidate:
    games = tuple(
        fixture(home, away, season=season) for home, away in permutations(ROSTER, 2)
    )
    return SeasonCandidate("PL", f"provider-{season.season_id}", season, ROSTER, games)


@pytest.mark.parametrize("day", [SEASON.start_date, SEASON.end_date])
def test_active_season_includes_exact_calendar_boundaries(day):
    result = resolve_active_season((candidate(NEXT), candidate()), as_of=day)
    assert result.status is ResolutionStatus.ACTIVE
    assert result.active_season_id == result.display_season_id == SEASON.season_id
    assert result.reason is None


def test_transition_and_offseason_keep_last_valid_display():
    records = (candidate(), candidate(NEXT))
    fallback = {"last_valid_display_season_id": SEASON.season_id}
    result = resolve_active_season(records, as_of=date(2041, 6, 1), **fallback)
    assert result.status is ResolutionStatus.PENDING
    assert (
        result.active_season_id is None and result.display_season_id == SEASON.season_id
    )
    assert result.reason is ResolutionReason.OFF_SEASON
    result = resolve_active_season(records, as_of=NEXT.start_date, **fallback)
    assert result.active_season_id == result.display_season_id == NEXT.season_id


def test_incomplete_inventory_is_pending_and_duplicate_inventory_is_blocked():
    record = candidate(NEXT)
    fallback = {
        "as_of": NEXT.start_date,
        "last_valid_display_season_id": SEASON.season_id,
    }
    result = resolve_active_season(
        (replace(record, fixtures=record.fixtures[:-1]),), **fallback
    )
    assert result.status is ResolutionStatus.PENDING
    assert result.reason is ResolutionReason.INCOMPLETE_INVENTORY
    assert result.display_season_id == SEASON.season_id
    result = resolve_active_season(
        (replace(record, fixtures=record.fixtures[:-1] + record.fixtures[:1]),),
        **fallback,
    )
    assert result.status is ResolutionStatus.BLOCKED
    assert result.reason is ResolutionReason.INVALID_INVENTORY


@pytest.mark.parametrize(
    "changes", [{"competition_code": "FA"}, {"provider_season_id": ""}]
)
def test_invalid_metadata_fails_closed(changes):
    result = resolve_active_season(
        (replace(candidate(), **changes),), as_of=SEASON.start_date
    )
    assert result.status is ResolutionStatus.BLOCKED
    assert result.reason is ResolutionReason.INVALID_METADATA


def test_rejects_invalid_dates_and_overlapping_calendars():
    bad = candidate(replace(NEXT, start_date=NEXT.end_date, end_date=NEXT.start_date))
    assert (
        resolve_active_season((bad,), as_of=NEXT.start_date).reason
        is ResolutionReason.INVALID_METADATA
    )
    overlapping = candidate(replace(NEXT, start_date=SEASON.end_date))
    result = resolve_active_season((candidate(), overlapping), as_of=SEASON.end_date)
    assert result.reason is ResolutionReason.OVERLAPPING_CALENDARS


def test_pending_provider_status_is_explicit():
    record = candidate(replace(NEXT, status=SeasonStatus.PENDING))
    assert (
        resolve_active_season((record,), as_of=NEXT.start_date).reason
        is ResolutionReason.UNCONFIRMED_SEASON
    )


def test_approved_rescheduling_extends_season_and_requires_confirmed_completion():
    record = candidate()
    late = fixture(ROSTER[0], ROSTER[1], day=date(2041, 6, 1))
    approval = ReschedulingApproval(
        SEASON.season_id, late.fixture_id, late.kickoff_utc, "synthetic amendment"
    )
    extended = replace(
        record,
        fixtures=(late, *record.fixtures[1:]),
        rescheduling_approvals=(approval,),
    )
    result = resolve_active_season((extended,), as_of=late.kickoff_utc.date())
    assert result.active_season_id == SEASON.season_id
    assert (
        resolve_active_season((extended,), as_of=date(2041, 6, 2)).reason
        is ResolutionReason.UNCONFIRMED_COMPLETION
    )
    bogus = replace(approval, source_ref=" ")
    assert (
        resolve_active_season(
            (replace(extended, rescheduling_approvals=(bogus,)),),
            as_of=date(2041, 6, 1),
        ).reason
        is ResolutionReason.INVALID_INVENTORY
    )


def standings(games, *, teams=ROSTER[:3], **options):
    defaults = {"adjustments": ZERO, "rules": SYNTHETIC, "qualification": QUALIFICATION}
    return calculate_standings(SEASON, teams, games, **(defaults | options))


def test_only_finished_matches_count_and_unequal_games_played_are_valid():
    a, b, c, d = ROSTER[:4]
    games = (
        fixture(a, b, (3, 1)),
        fixture(a, c, (2, 2)),
        fixture(c, b, status=FixtureStatus.POSTPONED),
        fixture(b, c, status=FixtureStatus.CANCELLED),
        fixture(d, a),
    )
    table = standings(games, teams=(a, b, c, d))
    rows = {row.team_id: row for row in table.rows}
    assert [
        (
            r.played,
            r.wins,
            r.draws,
            r.losses,
            r.goals_for,
            r.goals_against,
            r.goal_difference,
            r.earned_points,
        )
        for r in (rows[a.team_id], rows[b.team_id], rows[c.team_id], rows[d.team_id])
    ] == [
        (2, 1, 1, 0, 5, 3, 2, 4),
        (1, 0, 0, 1, 1, 3, -2, 0),
        (1, 0, 1, 0, 2, 2, 0, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
    ]
    assert [row.team_id for row in table.rows] == [
        a.team_id,
        c.team_id,
        d.team_id,
        b.team_id,
    ]
    assert (
        sum(r.wins for r in rows.values()) == sum(r.losses for r in rows.values()) == 1
    )
    assert (
        sum(r.goals_for for r in rows.values())
        == sum(r.goals_against for r in rows.values())
        == 8
    )
    assert sum(r.earned_points for r in rows.values()) == 5
    assert sum(r.played for r in rows.values()) == 4
    assert standings(reversed(games), teams=(d, c, b, a)) == table


def test_verified_deduction_and_unknown_adjustments_are_distinct():
    a, b, _ = ROSTER[:3]
    games = (fixture(a, b, (2, 0)),)
    deduction = PointsAdjustment(
        SEASON.season_id, a.team_id, -4, "synthetic board decision"
    )
    adjustments = replace(
        ZERO, status=AdjustmentStatus.VERIFIED_ADJUSTMENTS, adjustments=(deduction,)
    )
    changed = standings(games, adjustments=adjustments)
    club = next(row for row in changed.rows if row.team_id == a.team_id)
    assert club.earned_points == 3 and club.adjusted_points == -1 and club.rank == 3
    unknown = standings(
        games,
        adjustments=AdjustmentEvidence(SEASON.season_id, AdjustmentStatus.UNKNOWN),
    )
    assert RankingBlocker.UNKNOWN_ADJUSTMENTS in unknown.blockers
    assert all(row.adjusted_points is None for row in unknown.rows)
    assert all(
        row.adjusted_points == row.earned_points for row in standings(games).rows
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("season_id", "wrong-season"),
        ("team_id", "unknown-club"),
        ("points_delta", True),
        ("points_delta", 0),
        ("evidence_ref", ""),
    ],
)
def test_malformed_adjustment_is_rejected(field, value):
    valid = PointsAdjustment(SEASON.season_id, ROSTER[0].team_id, -1, "synthetic")
    adjustment = replace(valid, **{field: value})
    with pytest.raises(ValueError):
        standings(
            (),
            adjustments=replace(
                ZERO,
                status=AdjustmentStatus.VERIFIED_ADJUSTMENTS,
                adjustments=(adjustment,),
            ),
        )


def test_duplicate_adjustments_and_unsubstantiated_zero_are_rejected():
    decision = PointsAdjustment(SEASON.season_id, ROSTER[0].team_id, -1, "synthetic")
    with pytest.raises(ValueError, match="Duplicate"):
        standings(
            (),
            adjustments=replace(
                ZERO,
                status=AdjustmentStatus.VERIFIED_ADJUSTMENTS,
                adjustments=(decision, decision),
            ),
        )
    with pytest.raises(ValueError):
        standings((), adjustments=replace(ZERO, evidence_ref=None))


def all_games(scores):
    return tuple(
        fixture(home, away, score)
        for (home, away), score in zip(permutations(ROSTER[:3], 2), scores, strict=True)
    )


@pytest.mark.parametrize(
    "scores",
    [
        ((0, 0), (0, 0), (2, 1), (0, 0), (0, 1), (1, 0)),
        ((1, 1), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0)),
    ],
    ids=["head-to-head-points", "head-to-head-away-goals"],
)
def test_final_sporting_tiebreak_uses_evidence_supported_order(scores):
    games = all_games(scores)
    current = standings(games)
    assert [row.rank for row in current.rows] == [1, 1, 3]
    final = standings(games, phase=StandingsPhase.FINAL)
    assert final.rows[0].team_id == ROSTER[1].team_id
    assert [row.rank for row in final.rows] == [1, 2, 3]
    assert final.blockers == (RankingBlocker.SYNTHETIC_RULES,)


def test_unresolved_multi_club_tie_and_incomplete_head_to_head_block_final_outcome():
    games = all_games(((0, 0),) * 6)
    final = standings(games, phase=StandingsPhase.FINAL)
    assert [row.rank for row in final.rows] == [1, 1, 1]
    assert RankingBlocker.UNRESOLVED_FINAL_TIE in final.blockers
    # One completed fixture per pair leaves all three clubs level with missing return legs.
    incomplete = standings((games[0], games[3], games[4]), phase=StandingsPhase.FINAL)
    assert RankingBlocker.INCOMPLETE_FINAL_INVENTORY in incomplete.blockers
    assert RankingBlocker.INCOMPLETE_HEAD_TO_HEAD in incomplete.blockers
    assert [row.rank for row in incomplete.rows] == [1, 1, 1]


def test_unknown_qualification_and_unsupported_procedure_block():
    games = all_games(((0, 0),) * 6)
    result = standings(games, phase=StandingsPhase.FINAL, qualification=None)
    assert RankingBlocker.UNKNOWN_QUALIFICATION_BOUNDARIES in result.blockers
    result = standings(games, phase=StandingsPhase.FINAL, exceptional_procedure=True)
    assert RankingBlocker.UNSUPPORTED_OFFICIAL_PROCEDURE in result.blockers
    assert [row.rank for row in result.rows] == [1, 1, 1]


def test_rules_require_matching_verified_version_and_evidence():
    assert standings((), rules=None).blockers == (RankingBlocker.UNVERIFIED_RULES,)
    assert standings((), rules=replace(SYNTHETIC, version="incorrect")).blockers == (
        RankingBlocker.UNVERIFIED_RULES,
    )
    assert standings((), rules=replace(SYNTHETIC, evidence_ref="")).blockers == (
        RankingBlocker.UNVERIFIED_RULES,
    )
    assert standings((), rules=replace(SYNTHETIC, procedure="unknown")).blockers == (
        RankingBlocker.UNSUPPORTED_RULES,
    )
    assert standings(
        (), rules=replace(SYNTHETIC, verification=RuleVerification.VERIFIED)
    ).blockers == (RankingBlocker.UNVERIFIED_RULES,)

    official = verified_ruleset("2026-2027", "pl-2026-2027-section-c")
    assert official is not None and official.verification is RuleVerification.VERIFIED
    season = replace(
        SEASON, season_id=official.season_id, ruleset_version=official.version
    )
    result = calculate_standings(
        season,
        ROSTER,
        (),
        rules=official,
        adjustments=AdjustmentEvidence(season.season_id, AdjustmentStatus.UNKNOWN),
    )
    assert result.blockers == (RankingBlocker.UNKNOWN_ADJUSTMENTS,)
    assert {row.rank for row in result.rows} == {1}


def test_completed_extended_season_returns_offseason_without_selecting_next_early():
    record = candidate()
    late = fixture(ROSTER[0], ROSTER[1], (1, 1), day=date(2041, 6, 1))
    amendment = ReschedulingApproval(
        SEASON.season_id, late.fixture_id, late.kickoff_utc, "synthetic amendment"
    )
    complete = replace(
        record,
        season=replace(SEASON, status=SeasonStatus.COMPLETE),
        fixtures=(
            late,
            *(
                replace(game, status=FixtureStatus.FINISHED, home_goals=0, away_goals=0)
                for game in record.fixtures[1:]
            ),
        ),
        rescheduling_approvals=(amendment,),
    )
    resolution = resolve_active_season(
        (candidate(NEXT), complete),
        as_of=date(2041, 6, 2),
        last_valid_display_season_id=SEASON.season_id,
    )
    assert resolution.reason is ResolutionReason.OFF_SEASON
    assert resolution.active_season_id is None
    assert resolution.display_season_id == SEASON.season_id


def test_final_ordinary_tie_keeps_shared_positions():
    a, _, _ = ROSTER[:3]
    scores = [
        (1, 0) if home == a else (0, 1) if away == a else (0, 0)
        for home, away in permutations(ROSTER[:3], 2)
    ]
    games = all_games(scores)
    no_relegation_synthetic = replace(SYNTHETIC, relegation_places=0)
    result = standings(games, phase=StandingsPhase.FINAL, rules=no_relegation_synthetic)
    assert [row.rank for row in result.rows] == [1, 2, 2]
    assert result.blockers == (RankingBlocker.SYNTHETIC_RULES,)
