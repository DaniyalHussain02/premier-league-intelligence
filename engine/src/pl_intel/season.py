"""Resolve a Premier League season from supplied calendar and fixture evidence."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import pairwise

from pl_intel.db import store_season, temporary_database
from pl_intel.normalize import (
    IncompleteInventory,
    inventory_team_ids,
    validate_fixture_pairings,
)
from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    ReschedulingApproval,
    Season,
    SeasonStatus,
    Team,
)
from pl_intel.validate import (
    require_canonical_id,
    require_date,
    require_text,
    validate_season,
)


class ResolutionStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"
    BLOCKED = "blocked"


class ResolutionReason(StrEnum):
    OFF_SEASON = "off_season"
    INCOMPLETE_INVENTORY = "incomplete_inventory"
    UNCONFIRMED_SEASON = "unconfirmed_season"
    UNCONFIRMED_COMPLETION = "unconfirmed_completion"
    INVALID_METADATA = "invalid_metadata"
    OVERLAPPING_CALENDARS = "overlapping_calendars"
    INVALID_INVENTORY = "invalid_inventory"


@dataclass(frozen=True, slots=True)
class SeasonCandidate:
    """Canonical season and the supplied provider identity and inventory."""

    competition_code: str
    provider_season_id: str
    season: Season
    teams: tuple[Team, ...] = ()
    fixtures: tuple[Fixture, ...] = ()
    rescheduling_approvals: tuple[ReschedulingApproval, ...] = ()


@dataclass(frozen=True, slots=True)
class SeasonResolution:
    active_season_id: str | None
    display_season_id: str | None
    status: ResolutionStatus
    reason: ResolutionReason | None


def _validate_inventory(candidate: SeasonCandidate) -> None:
    season = candidate.season
    with temporary_database() as db:
        store_season(
            db,
            season,
            candidate.teams,
            fixtures=candidate.fixtures,
            rescheduling_approvals=candidate.rescheduling_approvals,
        )
    teams = inventory_team_ids(candidate.teams)
    validate_fixture_pairings(
        teams, ((f.home_team_id, f.away_team_id) for f in candidate.fixtures)
    )


def resolve_active_season(
    candidates: Iterable[SeasonCandidate],
    *,
    as_of: date,
    last_valid_display_season_id: str | None = None,
) -> SeasonResolution:
    """Use a UTC calendar date and preserve the caller's last valid display ID."""
    require_date(as_of)
    if last_valid_display_season_id is not None:
        require_canonical_id(last_valid_display_season_id)

    def pending(reason: ResolutionReason) -> SeasonResolution:
        return SeasonResolution(
            None, last_valid_display_season_id, ResolutionStatus.PENDING, reason
        )

    def blocked(reason: ResolutionReason) -> SeasonResolution:
        return SeasonResolution(
            None, last_valid_display_season_id, ResolutionStatus.BLOCKED, reason
        )

    calendar = tuple(candidates)
    season_ids = set()
    provider_ids = set()
    try:
        for candidate in calendar:
            season = candidate.season
            validate_season(season)
            require_text(candidate.provider_season_id)
            if (
                candidate.competition_code != "PL"
                or season.season_id in season_ids
                or candidate.provider_season_id in provider_ids
            ):
                return blocked(ResolutionReason.INVALID_METADATA)
            season_ids.add(season.season_id)
            provider_ids.add(candidate.provider_season_id)
    except (AttributeError, TypeError, ValueError):
        return blocked(ResolutionReason.INVALID_METADATA)

    ordered = sorted(calendar, key=lambda item: item.season.start_date)
    if any(
        left.season.end_date >= right.season.start_date
        for left, right in pairwise(ordered)
    ):
        return blocked(ResolutionReason.OVERLAPPING_CALENDARS)

    active = []
    awaiting_completion = False
    for candidate in ordered:
        season = candidate.season
        if as_of < season.start_date:
            continue
        in_window = as_of <= season.end_date
        if not in_window and season.status is not SeasonStatus.ACTIVE:
            continue
        if not in_window and not candidate.rescheduling_approvals:
            continue
        try:
            _validate_inventory(candidate)
        except IncompleteInventory:
            return pending(ResolutionReason.INCOMPLETE_INVENTORY)
        except (AttributeError, TypeError, ValueError, sqlite3.IntegrityError):
            return blocked(ResolutionReason.INVALID_INVENTORY)

        if season.status is SeasonStatus.PENDING:
            return pending(ResolutionReason.UNCONFIRMED_SEASON)
        if season.status is SeasonStatus.COMPLETE:
            if any(f.status is not FixtureStatus.FINISHED for f in candidate.fixtures):
                return blocked(ResolutionReason.INVALID_INVENTORY)
            continue

        late_fixtures = [
            f for f in candidate.fixtures if f.kickoff_utc.date() > season.end_date
        ]
        if in_window or any(
            f.kickoff_utc.date() >= as_of and f.status is not FixtureStatus.CANCELLED
            for f in late_fixtures
        ):
            active.append(season.season_id)
        elif late_fixtures:
            awaiting_completion = True

    if len(active) > 1:
        return blocked(ResolutionReason.OVERLAPPING_CALENDARS)
    if awaiting_completion:
        return pending(ResolutionReason.UNCONFIRMED_COMPLETION)
    if active:
        return SeasonResolution(active[0], active[0], ResolutionStatus.ACTIVE, None)
    return pending(ResolutionReason.OFF_SEASON)
