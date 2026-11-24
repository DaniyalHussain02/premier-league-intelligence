"""Normalize a provider's complete season against an explicitly reviewed roster."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from itertools import permutations

from pl_intel.db import resolve_team_id, store_season, temporary_database
from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    ReschedulingApproval,
    Season,
    Team,
    TeamSourceId,
)


@dataclass(frozen=True, slots=True)
class SourceFixture:
    """Source record whose update time carries no result-availability guarantee."""

    provider_fixture_id: str | None
    home_ref: str
    away_ref: str
    kickoff_utc: datetime
    matchweek: int
    status: FixtureStatus
    updated_at: datetime
    home_goals: int | None = None
    away_goals: int | None = None


def canonical_fixture_id(season_id: str, home_id: str, away_id: str) -> str:
    """Identify a directed fixture slot independently of provider or kickoff."""
    return sha256(f"{season_id}:{home_id}:{away_id}".encode("ascii")).hexdigest()


def normalize_season(
    provider: str,
    season: Season,
    teams: Iterable[Team],
    team_sources: Iterable[TeamSourceId],
    fixtures: Iterable[SourceFixture],
    *,
    rescheduling_approvals: Iterable[ReschedulingApproval] = (),
) -> tuple[Fixture, ...]:
    """Return canonical records only after validating the complete inventory."""
    if provider not in {"openfootball", "football-data-org"}:
        raise ValueError("Unsupported fixture provider")
    roster = tuple(teams)
    team_ids = {team.team_id for team in roster}
    if len(roster) != 20 or len(team_ids) != 20:
        raise ValueError("Season inventory must contain exactly 20 distinct clubs")

    normalized: list[Fixture] = []
    seen_source_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    seen_team_refs: set[str] = set()
    aliases = tuple(team_sources)
    with temporary_database() as db:
        store_season(db, season, roster, team_sources=aliases)
        for row in fixtures:
            day = row.kickoff_utc.date()
            seen_team_refs.update((row.home_ref, row.away_ref))
            home = resolve_team_id(db, season.season_id, provider, row.home_ref, day)
            away = resolve_team_id(db, season.season_id, provider, row.away_ref, day)
            if home == away:
                raise ValueError("A club cannot play itself")
            pair = (home, away)
            source_id = row.provider_fixture_id
            if provider == "openfootball":
                # Football.TXT has no fixture ID. The directed slot survives date changes.
                source_id = f"{season.season_id}:{home}:{away}"
            if source_id is None:
                raise ValueError("Missing provider fixture ID")
            if source_id in seen_source_ids:
                raise ValueError("Duplicate provider fixture ID")
            if pair in seen_pairs:
                raise ValueError("Duplicate season fixture pairing")
            seen_source_ids.add(source_id)
            seen_pairs.add(pair)
            fixture_id = canonical_fixture_id(season.season_id, home, away)
            normalized.append(
                Fixture(
                    fixture_id,
                    season.season_id,
                    provider,
                    source_id,
                    home,
                    away,
                    row.kickoff_utc,
                    row.matchweek,
                    row.status,
                    row.updated_at,
                    row.home_goals,
                    row.away_goals,
                )
            )
        if seen_pairs != set(permutations(team_ids, 2)):
            raise ValueError(
                "Season inventory must contain all 380 directed club pairings"
            )

    if provider == "football-data-org" and len(seen_team_refs) != 20:
        raise ValueError("Season inventory must contain exactly 20 provider team IDs")

    with temporary_database() as db:
        store_season(
            db,
            season,
            roster,
            fixtures=normalized,
            rescheduling_approvals=rescheduling_approvals,
        )
    return tuple(sorted(normalized, key=lambda fixture: fixture.fixture_id))
