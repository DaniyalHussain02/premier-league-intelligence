"""Compute standings and report unsupported sporting decisions explicitly."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby, permutations

from pl_intel.db import store_season, temporary_database
from pl_intel.normalize import (
    IncompleteInventory,
    inventory_team_ids,
    validate_fixture_pairings,
)
from pl_intel.rules import RankingProcedure, Ruleset, RuleVerification, verified_ruleset
from pl_intel.schemas import Fixture, FixtureStatus, ReschedulingApproval, Season, Team
from pl_intel.validate import require_text


class AdjustmentStatus(StrEnum):
    UNKNOWN = "unknown"
    VERIFIED_ZERO = "verified_zero"
    VERIFIED_ADJUSTMENTS = "verified_adjustments"


@dataclass(frozen=True, slots=True)
class PointsAdjustment:
    season_id: str
    team_id: str
    points_delta: int
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class AdjustmentEvidence:
    season_id: str
    status: AdjustmentStatus
    evidence_ref: str | None = None
    adjustments: tuple[PointsAdjustment, ...] = ()


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    season_id: str
    after_positions: tuple[int, ...]
    evidence_ref: str


class StandingsPhase(StrEnum):
    CURRENT = "current"
    FINAL = "final"


class RankingBlocker(StrEnum):
    UNVERIFIED_RULES = "unverified_rules"
    UNSUPPORTED_RULES = "unsupported_rules"
    SYNTHETIC_RULES = "synthetic_rules"
    UNKNOWN_ADJUSTMENTS = "unknown_adjustments"
    INCOMPLETE_FINAL_INVENTORY = "incomplete_final_inventory"
    UNKNOWN_QUALIFICATION_BOUNDARIES = "unknown_qualification_boundaries"
    INCOMPLETE_HEAD_TO_HEAD = "incomplete_head_to_head"
    UNRESOLVED_FINAL_TIE = "unresolved_final_tie"
    UNSUPPORTED_OFFICIAL_PROCEDURE = "unsupported_official_procedure"


@dataclass(frozen=True, slots=True)
class Standing:
    team_id: str
    rank: int | None
    played: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int
    earned_points: int
    adjusted_points: int | None

    @property
    def goal_difference(self) -> int:
        return self.goals_for - self.goals_against


@dataclass(frozen=True, slots=True)
class StandingsResult:
    phase: StandingsPhase
    rows: tuple[Standing, ...]
    blockers: tuple[RankingBlocker, ...]


@dataclass(slots=True)
class _Totals:
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0

    def record(self, goals_for: int, goals_against: int) -> None:
        self.wins += goals_for > goals_against
        self.draws += goals_for == goals_against
        self.losses += goals_for < goals_against
        self.goals_for += goals_for
        self.goals_against += goals_against

    @property
    def points(self) -> int:
        return self.wins * 3 + self.draws


def _adjustment_points(
    season_id: str,
    roster: set[str],
    evidence: AdjustmentEvidence,
) -> dict[str, int] | None:
    if evidence.season_id != season_id or not isinstance(
        evidence.status, AdjustmentStatus
    ):
        raise ValueError("Invalid season or status for points adjustment evidence")
    if evidence.status is AdjustmentStatus.UNKNOWN:
        if evidence.adjustments or evidence.evidence_ref is not None:
            raise ValueError(
                "Unknown adjustment evidence cannot contain verified records"
            )
        return None
    require_text(evidence.evidence_ref)
    if bool(evidence.adjustments) != (
        evidence.status is AdjustmentStatus.VERIFIED_ADJUSTMENTS
    ):
        raise ValueError("Adjustment records do not match the evidence status")

    points: dict[str, int] = {}
    for adjustment in evidence.adjustments:
        require_text(adjustment.evidence_ref)
        if adjustment.season_id != season_id or adjustment.team_id not in roster:
            raise ValueError(
                "Points adjustment does not match the season or club roster"
            )
        if adjustment.team_id in points:
            raise ValueError("Duplicate points adjustment for club")
        if type(adjustment.points_delta) is not int or not adjustment.points_delta:
            raise ValueError("Points adjustment must be a nonzero integer")
        points[adjustment.team_id] = adjustment.points_delta
    return points


def _qualification_boundaries(
    season_id: str,
    evidence: QualificationEvidence | None,
    team_count: int,
) -> tuple[int, ...] | None:
    if evidence is None:
        return None
    require_text(evidence.evidence_ref)
    positions = evidence.after_positions
    if (
        evidence.season_id != season_id
        or any(
            type(position) is not int or not 1 <= position < team_count
            for position in positions
        )
        or len(set(positions)) != len(positions)
    ):
        raise ValueError("Invalid qualification boundaries for season")
    return positions


def _rules_blocker(season: Season, rules: Ruleset | None) -> RankingBlocker | None:
    if rules is None or not isinstance(rules.verification, RuleVerification):
        return RankingBlocker.UNVERIFIED_RULES
    if rules.season_id != season.season_id or rules.version != season.ruleset_version:
        return RankingBlocker.UNVERIFIED_RULES
    try:
        require_text(rules.evidence_ref)
    except ValueError:
        return RankingBlocker.UNVERIFIED_RULES
    if rules.procedure is not RankingProcedure.EPL_SECTION_C or (
        type(rules.relegation_places) is not int or rules.relegation_places < 0
    ):
        return RankingBlocker.UNSUPPORTED_RULES
    if rules.verification is RuleVerification.SYNTHETIC:
        return RankingBlocker.SYNTHETIC_RULES
    if rules.verification is not RuleVerification.VERIFIED or rules != verified_ruleset(
        season.season_id, season.ruleset_version
    ):
        return RankingBlocker.UNVERIFIED_RULES
    return None


def _head_to_head(
    clubs: tuple[str, ...],
    scores: dict[tuple[str, str], tuple[int, int]],
) -> dict[str, tuple[int, int]] | None:
    totals = {club: _Totals() for club in clubs}
    away_goals = dict.fromkeys(clubs, 0)
    for home_id, away_id in permutations(clubs, 2):
        match = scores.get((home_id, away_id))
        if match is None:
            return None
        home, away = match
        totals[home_id].record(home, away)
        totals[away_id].record(away, home)
        away_goals[away_id] += away
    return {club: (totals[club].points, away_goals[club]) for club in clubs}


def _crosses(start: int, count: int, boundaries: tuple[int, ...]) -> bool:
    return any(start <= boundary < start + count - 1 for boundary in boundaries)


def _final_groups(
    clubs: tuple[str, ...],
    start: int,
    *,
    boundaries: tuple[int, ...],
    qualification_known: bool,
    nonrelegated: int,
    scores: dict[tuple[str, str], tuple[int, int]],
    complete: bool,
    blockers: set[RankingBlocker],
) -> Iterable[tuple[str, ...]]:
    needs_tiebreak = _crosses(start, len(clubs), boundaries)
    if not qualification_known and _crosses(
        start, len(clubs), tuple(range(1, nonrelegated))
    ):
        blockers.add(RankingBlocker.UNKNOWN_QUALIFICATION_BOUNDARIES)
    if not needs_tiebreak:
        return (clubs,)
    keys = _head_to_head(clubs, scores)
    if keys is None:
        blockers.add(RankingBlocker.INCOMPLETE_HEAD_TO_HEAD)
        return (clubs,)
    if not complete:
        return (clubs,)

    ordered = sorted(clubs, key=lambda club: (-keys[club][0], -keys[club][1], club))
    groups = []
    for _, tied in groupby(ordered, key=keys.__getitem__):
        group = tuple(tied)
        if _crosses(start, len(group), boundaries):
            blockers.add(RankingBlocker.UNRESOLVED_FINAL_TIE)
        groups.append(group)
        start += len(group)
    return groups


def calculate_standings(
    season: Season,
    teams: Iterable[Team],
    fixtures: Iterable[Fixture],
    *,
    adjustments: AdjustmentEvidence,
    rules: Ruleset | None = None,
    phase: StandingsPhase = StandingsPhase.CURRENT,
    qualification: QualificationEvidence | None = None,
    rescheduling_approvals: Iterable[ReschedulingApproval] = (),
    exceptional_procedure: bool = False,
) -> StandingsResult:
    """Rank a supplied fixture snapshot. Blockers make its output provisional."""
    if not isinstance(phase, StandingsPhase) or type(exceptional_procedure) is not bool:
        raise ValueError("Invalid standings phase or procedure")
    roster = tuple(teams)
    records = tuple(fixtures)
    with temporary_database() as db:
        store_season(
            db,
            season,
            roster,
            fixtures=records,
            rescheduling_approvals=rescheduling_approvals,
        )
    team_ids = {team.team_id for team in roster}
    approved_points = _adjustment_points(season.season_id, team_ids, adjustments)
    places = _qualification_boundaries(season.season_id, qualification, len(roster))
    rules_blocker = _rules_blocker(season, rules)
    blockers: set[RankingBlocker] = set()
    if rules_blocker is not None:
        blockers.add(rules_blocker)
    if approved_points is None:
        blockers.add(RankingBlocker.UNKNOWN_ADJUSTMENTS)
    if exceptional_procedure:
        blockers.add(RankingBlocker.UNSUPPORTED_OFFICIAL_PROCEDURE)

    totals = {team_id: _Totals() for team_id in team_ids}
    scores = {}
    for fixture in records:
        if fixture.status is FixtureStatus.FINISHED:
            home, away = fixture.home_goals, fixture.away_goals
            assert home is not None and away is not None
            scores[fixture.home_team_id, fixture.away_team_id] = (home, away)
            totals[fixture.home_team_id].record(home, away)
            totals[fixture.away_team_id].record(away, home)

    final_complete = False
    if phase is StandingsPhase.FINAL:
        try:
            if rules_blocker is not RankingBlocker.SYNTHETIC_RULES:
                inventory_team_ids(roster)
            validate_fixture_pairings(team_ids, scores)
            final_complete = bool(team_ids)
        except IncompleteInventory:
            pass
        if not final_complete:
            blockers.add(RankingBlocker.INCOMPLETE_FINAL_INVENTORY)

    def key(team_id: str) -> tuple[int, int, int]:
        club = totals[team_id]
        points = club.points + (approved_points or {}).get(team_id, 0)
        return -points, club.goals_against - club.goals_for, -club.goals_for

    supported = rules_blocker in {None, RankingBlocker.SYNTHETIC_RULES}
    ordered = sorted(team_ids, key=lambda club: (key(club), club))
    nonrelegated = len(roster) - rules.relegation_places if supported and rules else 0
    if supported and not 0 < nonrelegated <= len(roster):
        supported = False
        blockers.add(RankingBlocker.UNSUPPORTED_RULES)

    rows = []
    rank = 1
    for _, tied in groupby(ordered, key=key):
        clubs = tuple(tied)
        groups: Iterable[tuple[str, ...]] = (clubs,)
        if (
            phase is StandingsPhase.FINAL
            and supported
            and len(clubs) > 1
            and approved_points is not None
            and not exceptional_procedure
        ):
            groups = _final_groups(
                clubs,
                rank,
                boundaries=(1, nonrelegated, *(places or ())),
                qualification_known=places is not None,
                nonrelegated=nonrelegated,
                scores=scores,
                complete=final_complete,
                blockers=blockers,
            )
        for group in groups:
            for team_id in group:
                club = totals[team_id]
                rows.append(
                    Standing(
                        team_id,
                        rank if supported else None,
                        club.wins + club.draws + club.losses,
                        club.wins,
                        club.draws,
                        club.losses,
                        club.goals_for,
                        club.goals_against,
                        club.points,
                        club.points + approved_points.get(team_id, 0)
                        if approved_points is not None
                        else None,
                    )
                )
            rank += len(group)

    return StandingsResult(phase, tuple(rows), tuple(sorted(blockers)))
