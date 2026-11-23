"""Private, in-memory SQLite storage for a single processing run."""

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import date

from pl_intel.schemas import Fixture, ReschedulingApproval, Season, Team, TeamSourceId
from pl_intel.validate import (
    require_canonical_id,
    require_date,
    require_text,
    validate_fixture,
    validate_rescheduling_approval,
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
    CHECK (date(valid_from, '+0 days') IS valid_from),
    CHECK (date(valid_to, '+0 days') IS valid_to),
    CHECK (valid_from <= valid_to)
) STRICT;

CREATE INDEX team_source_lookup ON team_source_ids(provider, external_id);

CREATE TRIGGER reject_overlapping_source_insert
BEFORE INSERT ON team_source_ids
WHEN EXISTS (
    SELECT 1 FROM team_source_ids AS existing
    WHERE existing.provider = NEW.provider AND existing.external_id = NEW.external_id
      AND (existing.valid_to IS NULL OR NEW.valid_from IS NULL
           OR existing.valid_to >= NEW.valid_from)
      AND (NEW.valid_to IS NULL OR existing.valid_from IS NULL
           OR NEW.valid_to >= existing.valid_from)
)
BEGIN
    SELECT RAISE(ABORT, 'Overlapping provider team mapping');
END;

CREATE TRIGGER reject_overlapping_source_update
BEFORE UPDATE ON team_source_ids
WHEN EXISTS (
    SELECT 1 FROM team_source_ids AS existing
    WHERE existing.rowid <> OLD.rowid
      AND existing.provider = NEW.provider AND existing.external_id = NEW.external_id
      AND (existing.valid_to IS NULL OR NEW.valid_from IS NULL
           OR existing.valid_to >= NEW.valid_from)
      AND (NEW.valid_to IS NULL OR existing.valid_from IS NULL
           OR NEW.valid_to >= existing.valid_from)
)
BEGIN
    SELECT RAISE(ABORT, 'Overlapping provider team mapping');
END;

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
    rescheduling_source_ref TEXT,
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


@contextmanager
def _season_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("SQLite foreign key enforcement is unavailable")
    connection.execute("SAVEPOINT store_season")
    try:
        yield
        connection.execute("RELEASE store_season")
    except BaseException:
        connection.execute("ROLLBACK TO store_season")
        connection.execute("RELEASE store_season")
        raise


def store_season(
    connection: sqlite3.Connection,
    season: Season,
    teams: Iterable[Team],
    *,
    team_sources: Iterable[TeamSourceId] = (),
    fixtures: Iterable[Fixture] = (),
    rescheduling_approvals: Iterable[ReschedulingApproval] = (),
) -> None:
    """Insert one season and its supplied records atomically.

    A canonical club can be reused by another season with the same metadata.
    Exact provider mappings can be reused. Rescheduling approvals must come
    from independently reviewed official amendments supplied by the caller.
    """
    validate_season(season)
    seen_teams: set[str] = set()
    seen_sources: set[tuple[str, str, str | None, str | None]] = set()
    approvals: dict[str, ReschedulingApproval] = {}
    for approval in rescheduling_approvals:
        validate_rescheduling_approval(approval)
        if approval.season_id != season.season_id:
            raise ValueError("Rescheduling approval belongs to a different season")
        if approval.fixture_id in approvals:
            raise ValueError("Duplicate rescheduling approval")
        approvals[approval.fixture_id] = approval
    with _season_transaction(connection):
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
            require_canonical_id(team.team_id)
            for value in (team.canonical_name, team.short_name):
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
            start = (
                source.valid_from.isoformat() if source.valid_from is not None else None
            )
            end = source.valid_to.isoformat() if source.valid_to is not None else None
            key = (source.provider, source.external_id, start, end)
            if key in seen_sources:
                raise ValueError("Duplicate provider team mapping period")
            seen_sources.add(key)
            if source.team_id not in seen_teams:
                raise ValueError(
                    "Provider team mapping refers to a club outside the roster"
                )
            stored = connection.execute(
                """SELECT 1 FROM team_source_ids
                   WHERE provider = ? AND external_id = ?
                     AND valid_from IS ? AND valid_to IS ? AND team_id = ?""",
                (*key, source.team_id),
            ).fetchone()
            if stored is None:
                connection.execute(
                    """INSERT INTO team_source_ids
                       (provider, external_id, valid_from, valid_to, team_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (*key, source.team_id),
                )

        for fixture in fixtures:
            validate_fixture(fixture)
            if fixture.season_id != season.season_id:
                raise ValueError("Fixture belongs to a different season")
            kickoff = fixture.kickoff_utc.isoformat()
            source_ref = None
            if not season.start_date <= fixture.kickoff_utc.date() <= season.end_date:
                approval = approvals.get(fixture.fixture_id)
                if approval is None or approval.kickoff_utc.isoformat() != kickoff:
                    raise ValueError(
                        "Fixture kickoff is outside the season's date range without a matching approval"
                    )
                source_ref = approval.source_ref
            connection.execute(
                """INSERT INTO fixtures
                   (fixture_id, season_id, provider, provider_fixture_id, home_team_id,
                    away_team_id, kickoff_utc, matchweek, status, updated_at, home_goals,
                    away_goals, rescheduling_source_ref)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fixture.fixture_id,
                    fixture.season_id,
                    fixture.provider,
                    fixture.provider_fixture_id,
                    fixture.home_team_id,
                    fixture.away_team_id,
                    kickoff,
                    fixture.matchweek,
                    fixture.status.value,
                    fixture.updated_at.isoformat(),
                    fixture.home_goals,
                    fixture.away_goals,
                    source_ref,
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
    require_canonical_id(season_id)
    for value in (provider, external_id):
        require_text(value)
    require_date(on_date)
    day = on_date.isoformat()
    rows = connection.execute(
        """SELECT source.team_id FROM team_source_ids AS source
           JOIN season_teams AS roster ON roster.team_id = source.team_id
           WHERE roster.season_id = ? AND source.provider = ? AND source.external_id = ?
             AND (source.valid_from IS NULL OR source.valid_from <= ?)
             AND (source.valid_to IS NULL OR source.valid_to >= ?)""",
        (season_id, provider, external_id, day, day),
    ).fetchmany(2)
    if not rows:
        raise ValueError("Unmapped provider team for this season and date")
    if len(rows) != 1:
        raise ValueError("Ambiguous provider team mapping")
    return rows[0][0]
