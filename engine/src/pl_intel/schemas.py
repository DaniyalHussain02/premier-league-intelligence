"""Provider-independent records for Premier League batch processing."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class SeasonStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"


class FixtureStatus(StrEnum):
    SCHEDULED = "scheduled"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class Season:
    season_id: str
    start_date: date
    end_date: date
    status: SeasonStatus
    ruleset_version: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class Team:
    team_id: str
    canonical_name: str
    short_name: str


@dataclass(frozen=True, slots=True)
class TeamSourceId:
    provider: str
    external_id: str
    team_id: str
    valid_from: date | None = None
    valid_to: date | None = None


@dataclass(frozen=True, slots=True)
class Fixture:
    fixture_id: str
    season_id: str
    provider: str
    provider_fixture_id: str
    home_team_id: str
    away_team_id: str
    kickoff_utc: datetime
    matchweek: int
    status: FixtureStatus
    updated_at: datetime
    home_goals: int | None = None
    away_goals: int | None = None
