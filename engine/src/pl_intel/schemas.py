"""Provider-independent records for Premier League batch processing."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, TypedDict


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


@dataclass(frozen=True, slots=True)
class ReschedulingApproval:
    """An exception supplied from a reviewed official fixture amendment."""

    season_id: str
    fixture_id: str
    kickoff_utc: datetime
    source_ref: str


class StaticDocument(TypedDict):
    schema_version: Literal[1]


class SeasonIndex(StaticDocument):
    active_season_id: str | None
    display_season_id: str | None
    season_status: Literal["active", "pending", "blocked"]
    latest_manifest_path: str | None


class SnapshotMetadata(StaticDocument):
    season_id: str
    snapshot_id: str
    generated_at: str
    data_cutoff_at: str
    model_version: str


class LatestManifest(SnapshotMetadata):
    snapshot_path: str
    input_hash: str


class ForecastTeam(TypedDict):
    """Public points use the same adjusted basis for actual and expected values."""

    team_id: str
    name: str
    actual_rank: int
    played: int
    actual_points: int
    expected_points: float
    predicted_rank: int
    title_probability: float
    relegation_probability: float
    position_probabilities: list[float]


class ForecastSnapshot(SnapshotMetadata):
    simulation_count: int
    representative_table_method: str
    teams: list[ForecastTeam]


class HistoryEntry(TypedDict):
    snapshot_id: str
    generated_at: str
    data_cutoff_at: str
    matchweek_label: str
    path: str


class HistoryIndex(StaticDocument):
    season_id: str
    snapshots: list[HistoryEntry]


class BacktestEntry(TypedDict):
    backtest_id: str
    season_id: str
    model_version: str
    path: str


class BacktestIndex(StaticDocument):
    reports: list[BacktestEntry]


class BacktestSummary(StaticDocument):
    season_id: str
    model_version: str
    training_period: list[str] | None
    cutoff_policy: str
    sample_size: int
    baseline: str
    multiclass_brier_model: float
    multiclass_brier_baseline: float
    limitations: list[str]
