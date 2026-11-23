import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from pl_intel.db import resolve_team_id, store_season, temporary_database
from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    Season,
    SeasonStatus,
    Team,
    TeamSourceId,
)

SEASON = Season(
    "sample-season",
    date(2020, 8, 1),
    date(2021, 5, 31),
    SeasonStatus.COMPLETE,
    "sample-rules",
    "synthetic",
)
HOME = Team("club-001", "Sample Home", "Home")
AWAY = Team("club-002", "Sample Away", "Away")
KICKOFF = datetime(2020, 8, 8, 15, tzinfo=UTC)
FIXTURE = Fixture(
    "fixture-001",
    SEASON.season_id,
    "sample",
    "42",
    HOME.team_id,
    AWAY.team_id,
    KICKOFF,
    1,
    FixtureStatus.FINISHED,
    KICKOFF + timedelta(hours=2),
    0,
    2,
)


def test_canonical_fixture_and_provider_mapping() -> None:
    alias = "x' OR 1=1 --"
    source = TeamSourceId("sample", alias, HOME.team_id, KICKOFF.date(), KICKOFF.date())
    with temporary_database() as db:
        store_season(
            db, SEASON, (HOME, AWAY), team_sources=(source,), fixtures=(FIXTURE,)
        )
        assert (
            resolve_team_id(db, SEASON.season_id, "sample", alias, KICKOFF.date())
            == HOME.team_id
        )
        assert db.execute(
            "SELECT provider_fixture_id, kickoff_utc, status, home_goals, away_goals FROM fixtures"
        ).fetchone() == ("42", KICKOFF.isoformat(), "finished", 0, 2)
        for external_id, day in (
            ("unknown", KICKOFF.date()),
            (alias, date(2020, 8, 9)),
        ):
            with pytest.raises(ValueError, match="Unmapped"):
                resolve_team_id(db, SEASON.season_id, "sample", external_id, day)


@pytest.mark.parametrize(
    "status",
    [FixtureStatus.SCHEDULED, FixtureStatus.POSTPONED, FixtureStatus.CANCELLED],
)
def test_unfinished_fixture_has_null_scores(status) -> None:
    fixture = replace(FIXTURE, status=status, home_goals=None, away_goals=None)
    with temporary_database() as db:
        store_season(db, SEASON, (HOME, AWAY), fixtures=(fixture,))
        assert db.execute(
            "SELECT status, home_goals, away_goals FROM fixtures"
        ).fetchone() == (
            status.value,
            None,
            None,
        )


@pytest.mark.parametrize("score", [None, "2", True, 1.0, -1, 2**63])
def test_malformed_final_score_rolls_back(score) -> None:
    malformed = replace(FIXTURE, home_goals=score)
    with temporary_database() as db:
        with pytest.raises(ValueError, match="integer scores"):
            store_season(db, SEASON, (HOME, AWAY), fixtures=(malformed,))
        assert db.execute("SELECT count(*) FROM seasons").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM teams").fetchone() == (0,)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": FixtureStatus.POSTPONED}, "Only finished"),
        ({"matchweek": True}, "Matchweek"),
        ({"kickoff_utc": KICKOFF.replace(tzinfo=None)}, "UTC timestamp"),
        (
            {"kickoff_utc": KICKOFF.astimezone(timezone(timedelta(hours=1)))},
            "UTC timestamp",
        ),
        ({"away_team_id": HOME.team_id}, "cannot play itself"),
        ({"season_id": "different"}, "different season"),
        ({"kickoff_utc": KICKOFF - timedelta(days=30)}, "date range"),
        ({"updated_at": KICKOFF - timedelta(minutes=1)}, "predate its kickoff"),
    ],
)
def test_rejects_inconsistent_fixture(changes, message) -> None:
    with temporary_database() as db, pytest.raises(ValueError, match=message):
        store_season(db, SEASON, (HOME, AWAY), fixtures=(replace(FIXTURE, **changes),))


@pytest.mark.parametrize(
    "duplicate",
    [
        replace(
            FIXTURE,
            provider_fixture_id="99",
            home_team_id=AWAY.team_id,
            away_team_id=HOME.team_id,
        ),
        replace(
            FIXTURE,
            fixture_id="fixture-002",
            home_team_id=AWAY.team_id,
            away_team_id=HOME.team_id,
        ),
        replace(FIXTURE, fixture_id="fixture-002", provider_fixture_id="99"),
    ],
    ids=["canonical-fixture-id", "provider-fixture-id", "home-away-pair"],
)
def test_rejects_duplicate_fixture_and_rolls_back(duplicate) -> None:
    with temporary_database() as db:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            store_season(db, SEASON, (HOME, AWAY), fixtures=(FIXTURE, duplicate))
        assert db.execute("SELECT count(*) FROM fixtures").fetchone() == (0,)


def test_rejects_unknown_fixture_status() -> None:
    fixture = replace(FIXTURE, status="AWARDED")
    with (
        temporary_database() as db,
        pytest.raises(TypeError, match="Unknown fixture status"),
    ):
        store_season(db, SEASON, (HOME, AWAY), fixtures=(fixture,))


def test_team_identity_is_reused_across_seasons() -> None:
    earlier = replace(SEASON, season_id="earlier")
    alias = TeamSourceId("sample", "home", HOME.team_id)
    with temporary_database() as db:
        store_season(db, earlier, (HOME,), team_sources=(alias,))
        with pytest.raises(ValueError, match="conflicting mapping"):
            store_season(
                db,
                SEASON,
                (AWAY,),
                team_sources=(replace(alias, team_id=AWAY.team_id),),
            )
        store_season(db, SEASON, (HOME,), team_sources=(alias,))
        assert (
            resolve_team_id(db, SEASON.season_id, "sample", "home", KICKOFF.date())
            == HOME.team_id
        )
        assert db.execute("SELECT count(*) FROM teams").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM season_teams").fetchone() == (2,)


def test_duplicate_team_and_ambiguous_alias() -> None:
    alias = TeamSourceId("sample", "home", HOME.team_id)
    with temporary_database() as db:
        with pytest.raises(ValueError, match="Duplicate team"):
            store_season(db, SEASON, (HOME, HOME))
        with pytest.raises(ValueError, match="Duplicate provider team mapping"):
            store_season(
                db,
                SEASON,
                (HOME, AWAY),
                team_sources=(alias, replace(alias, team_id=AWAY.team_id)),
            )
        with pytest.raises(ValueError, match="outside the roster"):
            store_season(db, SEASON, (AWAY,), team_sources=(alias,))


def test_foreign_keys_require_season_membership() -> None:
    earlier = replace(SEASON, season_id="earlier")
    with temporary_database() as db:
        store_season(db, earlier, (AWAY,))
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store_season(db, SEASON, (HOME,), fixtures=(FIXTURE,))
        assert db.execute("SELECT season_id FROM seasons").fetchall() == [
            (earlier.season_id,)
        ]


def test_database_checks_survive_direct_sql() -> None:
    with temporary_database() as db:
        store_season(db, SEASON, (HOME, AWAY), fixtures=(FIXTURE,))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            db.execute(
                "INSERT INTO season_teams VALUES (?, ?)",
                (SEASON.season_id, HOME.team_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute("UPDATE fixtures SET home_goals = ?", ("2",))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute("UPDATE fixtures SET status = ?", ("postponed",))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
