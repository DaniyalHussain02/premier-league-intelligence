"""Private, in-memory SQLite storage for a single processing run."""

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import date

from pl_intel.schemas import Fixture, Season, Team, TeamSourceId
from pl_intel.validate import (
    require_date,
    require_text,
    validate_fixture,
    validate_season,
    validate_team_source,
)

_SCHEMA = """
CREATE TABLE seasons (
    season_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'complete')),
    ruleset_version TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    CHECK (start_date <= end_date)
) STRICT;

CREATE UNIQUE INDEX one_active_season ON seasons(status) WHERE status = 'active';

CREATE TABLE teams (
    team_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    short_name TEXT NOT NULL
) STRICT;

CREATE TABLE season_teams (
    season_id TEXT NOT NULL REFERENCES seasons(season_id),
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    PRIMARY KEY (season_id, team_id)
) STRICT;

CREATE TABLE team_source_ids (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    valid_from TEXT,
    valid_to TEXT,
    CHECK (valid_from <= valid_to),
    PRIMARY KEY (provider, external_id)
) STRICT;

CREATE TABLE fixtures (
    fixture_id TEXT PRIMARY KEY,
    season_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_fixture_id TEXT NOT NULL,
    home_team_id TEXT NOT NULL,
    away_team_id TEXT NOT NULL,
    kickoff_utc TEXT NOT NULL,
    matchweek ANY NOT NULL CHECK (
        typeof(matchweek) = 'integer' AND matchweek BETWEEN 1 AND 38
    ),
    status TEXT NOT NULL CHECK (
        status IN ('scheduled', 'postponed', 'cancelled', 'finished')
    ),
    updated_at TEXT NOT NULL,
    home_goals ANY,
    away_goals ANY,
    UNIQUE (provider, provider_fixture_id),
    UNIQUE (season_id, home_team_id, away_team_id),
    FOREIGN KEY (season_id, home_team_id) REFERENCES season_teams(season_id, team_id),
    FOREIGN KEY (season_id, away_team_id) REFERENCES season_teams(season_id, team_id),
    CHECK (home_team_id <> away_team_id),
    CHECK (
        (status = 'finished'
         AND typeof(home_goals) = 'integer' AND home_goals >= 0
         AND typeof(away_goals) = 'integer' AND away_goals >= 0)
        OR (status <> 'finished' AND home_goals IS NULL AND away_goals IS NULL)
    )
) STRICT;
"""


@contextmanager
def temporary_database() -> Iterator[sqlite3.Connection]:
    """Open a fresh database and release it on normal or exceptional exit."""
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise RuntimeError("SQLite foreign key enforcement is unavailable")
        connection.executescript(_SCHEMA)
        yield connection


def store_season(
    connection: sqlite3.Connection,
    season: Season,
    teams: Iterable[Team],
    *,
    team_sources: Iterable[TeamSourceId] = (),
    fixtures: Iterable[Fixture] = (),
) -> None:
    """Insert one season and its supplied records atomically.

    A canonical club can be reused by another season with the same metadata.
    Provider aliases already registered to the same club can be reused as well.
    """
    validate_season(season)
    seen_teams: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    with connection:
        connection.execute(
            """INSERT INTO seasons
               (season_id, start_date, end_date, status, ruleset_version, source_ref)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                season.season_id,
                season.start_date.isoformat(),
                season.end_date.isoformat(),
                season.status.value,
                season.ruleset_version,
                season.source_ref,
            ),
        )
        for team in teams:
            for value in (team.team_id, team.canonical_name, team.short_name):
                require_text(value)
            if team.team_id in seen_teams:
                raise ValueError("Duplicate team in season roster")
            seen_teams.add(team.team_id)
            identity = (team.team_id, team.canonical_name, team.short_name)
            connection.execute(
                """INSERT INTO teams (team_id, canonical_name, short_name)
                   VALUES (?, ?, ?) ON CONFLICT (team_id) DO NOTHING""",
                identity,
            )
            stored = connection.execute(
                "SELECT canonical_name, short_name FROM teams WHERE team_id = ?",
                (team.team_id,),
            ).fetchone()
            if stored != identity[1:]:
                raise ValueError("Canonical team ID has conflicting metadata")
            connection.execute(
                "INSERT INTO season_teams (season_id, team_id) VALUES (?, ?)",
                (season.season_id, team.team_id),
            )

        for source in team_sources:
            validate_team_source(source)
            key = (source.provider, source.external_id)
            if key in seen_sources:
                raise ValueError("Duplicate provider team mapping")
            seen_sources.add(key)
            if source.team_id not in seen_teams:
                raise ValueError(
                    "Provider team mapping refers to a club outside the roster"
                )
            mapping = (
                source.team_id,
                source.valid_from.isoformat()
                if source.valid_from is not None
                else None,
                source.valid_to.isoformat() if source.valid_to is not None else None,
            )
            connection.execute(
                """INSERT INTO team_source_ids
                   (provider, external_id, team_id, valid_from, valid_to)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT (provider, external_id) DO NOTHING""",
                (*key, *mapping),
            )
            stored = connection.execute(
                """SELECT team_id, valid_from, valid_to FROM team_source_ids
                   WHERE provider = ? AND external_id = ?""",
                key,
            ).fetchone()
            if stored != mapping:
                raise ValueError("Provider team identity has a conflicting mapping")

        for fixture in fixtures:
            validate_fixture(fixture)
            if fixture.season_id != season.season_id:
                raise ValueError("Fixture belongs to a different season")
            if not season.start_date <= fixture.kickoff_utc.date() <= season.end_date:
                raise ValueError("Fixture kickoff is outside the season's date range")
            connection.execute(
                """INSERT INTO fixtures
                   (fixture_id, season_id, provider, provider_fixture_id, home_team_id,
                    away_team_id, kickoff_utc, matchweek, status, updated_at, home_goals,
                    away_goals)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fixture.fixture_id,
                    fixture.season_id,
                    fixture.provider,
                    fixture.provider_fixture_id,
                    fixture.home_team_id,
                    fixture.away_team_id,
                    fixture.kickoff_utc.isoformat(),
                    fixture.matchweek,
                    fixture.status.value,
                    fixture.updated_at.isoformat(),
                    fixture.home_goals,
                    fixture.away_goals,
                ),
            )


def resolve_team_id(
    connection: sqlite3.Connection,
    season_id: str,
    provider: str,
    external_id: str,
    on_date: date,
) -> str:
    """Resolve a provider ID that is valid on the date and on the season roster."""
    for value in (season_id, provider, external_id):
        require_text(value)
    require_date(on_date)
    day = on_date.isoformat()
    row = connection.execute(
        """SELECT source.team_id FROM team_source_ids AS source
           JOIN season_teams AS roster ON roster.team_id = source.team_id
           WHERE roster.season_id = ? AND source.provider = ? AND source.external_id = ?
             AND (source.valid_from IS NULL OR source.valid_from <= ?)
             AND (source.valid_to IS NULL OR source.valid_to >= ?)""",
        (season_id, provider, external_id, day, day),
    ).fetchone()
    if row is None:
        raise ValueError("Unmapped provider team for this season and date")
    return row[0]
