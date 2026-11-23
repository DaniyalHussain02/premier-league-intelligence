import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from pl_intel.db import resolve_team_id, store_season, temporary_database
from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    ReschedulingApproval,
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
        with pytest.raises(sqlite3.IntegrityError, match="Overlapping"):
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


def test_nonoverlapping_open_ended_mappings_resolve_at_inclusive_boundaries() -> None:
    last_old_day = date(2020, 12, 31)
    first_new_day = date(2021, 1, 2)
    old = TeamSourceId("sample", "reused", HOME.team_id, valid_to=last_old_day)
    new = TeamSourceId("sample", "reused", AWAY.team_id, valid_from=first_new_day)
    with temporary_database() as db:
        store_season(db, SEASON, (HOME, AWAY), team_sources=(new, old))
        assert (
            resolve_team_id(db, SEASON.season_id, "sample", "reused", last_old_day)
            == HOME.team_id
        )
        assert (
            resolve_team_id(db, SEASON.season_id, "sample", "reused", first_new_day)
            == AWAY.team_id
        )
        with pytest.raises(ValueError, match="Unmapped"):
            resolve_team_id(db, SEASON.season_id, "sample", "reused", date(2021, 1, 1))


def test_sqlite_rejects_insert_and_update_at_a_shared_boundary() -> None:
    cutoff = date(2021, 1, 1)
    old = TeamSourceId("sample", "reused", HOME.team_id, valid_to=cutoff)
    new = TeamSourceId(
        "sample", "reused", AWAY.team_id, valid_from=cutoff + timedelta(days=1)
    )
    with temporary_database() as db:
        store_season(db, SEASON, (HOME, AWAY), team_sources=(old, new))
        with pytest.raises(sqlite3.IntegrityError, match="Overlapping"):
            db.execute(
                """INSERT INTO team_source_ids (provider, external_id, team_id)
                   VALUES (?, ?, ?)""",
                ("sample", "reused", AWAY.team_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="Overlapping"):
            db.execute(
                "UPDATE team_source_ids SET valid_from = ? WHERE team_id = ?",
                (cutoff.isoformat(), AWAY.team_id),
            )
        assert (
            resolve_team_id(db, SEASON.season_id, "sample", "reused", cutoff)
            == HOME.team_id
        )


LATE_KICKOFF = datetime(2021, 6, 2, 18, tzinfo=UTC)
LATE_FIXTURE = replace(
    FIXTURE, kickoff_utc=LATE_KICKOFF, updated_at=LATE_KICKOFF + timedelta(hours=2)
)
APPROVAL = ReschedulingApproval(
    SEASON.season_id, FIXTURE.fixture_id, LATE_KICKOFF, "reviewed synthetic amendment"
)


@pytest.mark.parametrize("status", [FixtureStatus.SCHEDULED, FixtureStatus.FINISHED])
def test_exact_approval_allows_out_of_season_kickoff(status: FixtureStatus) -> None:
    fixture = LATE_FIXTURE
    if status is FixtureStatus.SCHEDULED:
        fixture = replace(fixture, status=status, home_goals=None, away_goals=None)
    with temporary_database() as db:
        store_season(
            db,
            SEASON,
            (HOME, AWAY),
            fixtures=(fixture,),
            rescheduling_approvals=(APPROVAL,),
        )
        assert db.execute(
            "SELECT kickoff_utc, status, rescheduling_source_ref FROM fixtures"
        ).fetchone() == (LATE_KICKOFF.isoformat(), status.value, APPROVAL.source_ref)


@pytest.mark.parametrize(
    ("approvals", "message"),
    [
        ((), "date range"),
        ((replace(APPROVAL, season_id="other-season"),), "different season"),
        ((replace(APPROVAL, fixture_id="other-fixture"),), "date range"),
        (
            (replace(APPROVAL, kickoff_utc=LATE_KICKOFF + timedelta(seconds=1)),),
            "date range",
        ),
        (
            (replace(APPROVAL, kickoff_utc=LATE_KICKOFF.replace(tzinfo=None)),),
            "UTC timestamp",
        ),
        ((replace(APPROVAL, source_ref=" "),), "nonempty text"),
        ((APPROVAL, APPROVAL), "Duplicate rescheduling approval"),
    ],
)
def test_postponed_status_cannot_substitute_for_an_exact_approval(
    approvals, message
) -> None:
    fixture = replace(
        LATE_FIXTURE, status=FixtureStatus.POSTPONED, home_goals=None, away_goals=None
    )
    with temporary_database() as db, pytest.raises(ValueError, match=message):
        store_season(
            db,
            SEASON,
            (HOME, AWAY),
            fixtures=(fixture,),
            rescheduling_approvals=approvals,
        )


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"home_goals": -1}, ValueError, "integer scores"),
        ({"status": "unknown"}, TypeError, "Unknown fixture status"),
        ({"away_team_id": HOME.team_id}, ValueError, "cannot play itself"),
        ({"away_team_id": "missing-club"}, sqlite3.IntegrityError, "FOREIGN KEY"),
        ({"provider_fixture_id": ""}, ValueError, "nonempty text"),
    ],
)
def test_approval_cannot_bypass_fixture_validation(changes, error, message) -> None:
    with temporary_database() as db, pytest.raises(error, match=message):
        store_season(
            db,
            SEASON,
            (HOME, AWAY),
            fixtures=(replace(LATE_FIXTURE, **changes),),
            rescheduling_approvals=(APPROVAL,),
        )


@pytest.mark.parametrize(
    "invalid", ["", "Upper", "café", "../escape", "club--1", "a" * 65]
)
def test_invalid_canonical_ids_are_rejected(invalid: str) -> None:
    with temporary_database() as db:
        with pytest.raises(ValueError, match="canonical identifier"):
            store_season(db, replace(SEASON, season_id=invalid), (HOME, AWAY))
        with pytest.raises(ValueError, match="canonical identifier"):
            store_season(db, SEASON, (replace(HOME, team_id=invalid), AWAY))
        with pytest.raises(ValueError, match="canonical identifier"):
            store_season(
                db,
                SEASON,
                (HOME, AWAY),
                fixtures=(replace(FIXTURE, fixture_id=invalid),),
            )


def test_late_failure_rolls_back_batch_and_preserves_caller_transaction() -> None:
    reversed_fixture = replace(
        LATE_FIXTURE,
        fixture_id="late-fixture",
        provider_fixture_id="99",
        home_team_id=AWAY.team_id,
        away_team_id=HOME.team_id,
    )
    alias = TeamSourceId("sample", "home", HOME.team_id)
    with temporary_database() as db:
        earlier = replace(SEASON, season_id="earlier")
        store_season(db, earlier, (AWAY,))
        db.execute("BEGIN")
        pending = replace(SEASON, season_id="pending")
        store_season(db, pending, (AWAY,))
        with pytest.raises(ValueError, match="date range"):
            store_season(
                db,
                SEASON,
                (HOME, AWAY),
                team_sources=(alias,),
                fixtures=(FIXTURE, reversed_fixture),
            )
        assert db.in_transaction
        assert db.execute(
            "SELECT season_id FROM seasons ORDER BY season_id"
        ).fetchall() == [("earlier",), ("pending",)]
        assert db.execute("SELECT team_id FROM teams").fetchall() == [(AWAY.team_id,)]
        assert db.execute("SELECT count(*) FROM team_source_ids").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM fixtures").fetchone() == (0,)
        db.rollback()
        assert db.execute("SELECT season_id FROM seasons").fetchall() == [("earlier",)]
