"""Validate canonical records before passing them to SQLite."""

import re
from datetime import date, datetime, timedelta

from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    ReschedulingApproval,
    Season,
    SeasonStatus,
    TeamSourceId,
)

_MAX_SQLITE_INTEGER = 2**63 - 1
_CANONICAL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def require_text(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\0" in value
    ):
        raise ValueError("Expected nonempty text without surrounding whitespace or NUL")


def require_canonical_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not _CANONICAL_ID.fullmatch(value)
    ):
        raise ValueError("Invalid canonical identifier")


def require_date(value: date) -> None:
    if type(value) is not date:
        raise ValueError("Expected a calendar date")


def validate_season(season: Season) -> None:
    require_canonical_id(season.season_id)
    for value in (season.ruleset_version, season.source_ref):
        require_text(value)
    require_date(season.start_date)
    require_date(season.end_date)
    if season.start_date > season.end_date:
        raise ValueError("Season start must not follow its end")
    if not isinstance(season.status, SeasonStatus):
        raise TypeError("Unknown season status")


def validate_team_source(mapping: TeamSourceId) -> None:
    require_canonical_id(mapping.team_id)
    for value in (mapping.provider, mapping.external_id):
        require_text(value)
    for boundary in (mapping.valid_from, mapping.valid_to):
        if boundary is not None:
            require_date(boundary)
    if (
        mapping.valid_from is not None
        and mapping.valid_to is not None
        and mapping.valid_from > mapping.valid_to
    ):
        raise ValueError("Team mapping start must not follow its end")


def _require_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Expected a timezone-aware UTC timestamp")


def validate_rescheduling_approval(approval: ReschedulingApproval) -> None:
    require_canonical_id(approval.season_id)
    require_canonical_id(approval.fixture_id)
    _require_utc(approval.kickoff_utc)
    require_text(approval.source_ref)


def validate_fixture(fixture: Fixture) -> None:
    for value in (
        fixture.fixture_id,
        fixture.season_id,
        fixture.home_team_id,
        fixture.away_team_id,
    ):
        require_canonical_id(value)
    require_text(fixture.provider)
    require_text(fixture.provider_fixture_id)
    _require_utc(fixture.kickoff_utc)
    _require_utc(fixture.updated_at)
    if fixture.home_team_id == fixture.away_team_id:
        raise ValueError("A club cannot play itself")
    if type(fixture.matchweek) is not int or not 1 <= fixture.matchweek <= 38:
        raise ValueError("Matchweek must be an integer from 1 to 38")
    if not isinstance(fixture.status, FixtureStatus):
        raise TypeError("Unknown fixture status")

    goals = (fixture.home_goals, fixture.away_goals)
    if fixture.status is FixtureStatus.FINISHED:
        if fixture.updated_at < fixture.kickoff_utc:
            raise ValueError("A finished fixture cannot predate its kickoff")
        if any(
            type(score) is not int or not 0 <= score <= _MAX_SQLITE_INTEGER
            for score in goals
        ):
            raise ValueError(
                "A finished fixture requires two nonnegative integer scores"
            )
    elif goals != (None, None):
        raise ValueError("Only finished fixtures may contain final scores")
