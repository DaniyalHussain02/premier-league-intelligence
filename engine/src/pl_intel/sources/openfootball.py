"""Read a reviewed, local OpenFootball Football.TXT season file."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pl_intel.normalize import SourceFixture, normalize_season
from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    ReschedulingApproval,
    Season,
    Team,
    TeamSourceId,
)

PROVIDER = "openfootball"
_MAX_FILE_BYTES = 512 * 1024
_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MATCHDAY = re.compile(r"(?:▪\s*)?Matchday ([0-9]{1,2})")
_DAY = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Z][a-z]{2}) ([0-9]{1,2})(?: ([0-9]{4}))?"
)
_ROW = re.compile(r"(?:(\d{1,2}:\d{2})\s+)?(.+?)\s+v\s+(.+)")
_SCORE = re.compile(r"([0-9]{1,3})-([0-9]{1,3})(?:\s+\([0-9]{1,3}-[0-9]{1,3}\))?")
_LONDON = ZoneInfo("Europe/London")


def _fixture_date(match: re.Match[str], season: Season) -> date:
    weekday, month, day, explicit_year = match.groups()
    if month not in _MONTHS:
        raise ValueError("Invalid OpenFootball month")
    year = int(explicit_year) if explicit_year else season.start_date.year
    month_day = (_MONTHS.index(month) + 1, int(day))
    if explicit_year is None and month_day < (
        season.start_date.month,
        season.start_date.day,
    ):
        year += 1
    result = date(year, *month_day)
    if result.weekday() != _WEEKDAYS.index(weekday):
        raise ValueError("OpenFootball weekday disagrees with fixture date")
    return result


def _kickoff(
    day: date,
    clock: str | None,
    supplied: datetime | None,
) -> datetime:
    if clock is None:
        if supplied is None or supplied.utcoffset() is None:
            raise ValueError("OpenFootball fixture needs a reviewed kickoff time")
        if supplied.astimezone(_LONDON).date() != day:
            raise ValueError("Reviewed kickoff disagrees with OpenFootball match date")
        return supplied.astimezone(UTC)
    hours, minutes = map(int, clock.split(":"))
    local = datetime.combine(day, time(hours, minutes), _LONDON)
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError("Ambiguous or nonexistent OpenFootball local kickoff")
    return local.astimezone(UTC)


def _parse_result(
    away_field: str,
    aliases: frozenset[str],
) -> tuple[str, FixtureStatus, int | None, int | None]:
    away, *suffixes = re.split(r"[ \t]{2,}|\t", away_field, maxsplit=1)
    if away not in aliases:
        raise ValueError("Unmapped club or unsupported OpenFootball result suffix")
    suffix = suffixes[0] if suffixes else ""
    if suffix in {"", "-"}:
        return away, FixtureStatus.SCHEDULED, None, None
    if suffix == "[postponed]":
        return away, FixtureStatus.POSTPONED, None, None
    if suffix == "[cancelled]":
        return away, FixtureStatus.CANCELLED, None, None
    if score := _SCORE.fullmatch(suffix):
        return away, FixtureStatus.FINISHED, int(score[1]), int(score[2])
    raise ValueError("Malformed or unsupported OpenFootball result suffix")


@dataclass(frozen=True, slots=True)
class VerifiedResultAvailability:
    """Evidence for when this exact score was independently known."""

    available_at: datetime
    home_goals: int
    away_goals: int
    source_ref: str


@dataclass(frozen=True, slots=True)
class VerifiedHistoricalResult:
    """A result fact with no metadata from the later archive snapshot."""

    fixture_id: str
    home_goals: int
    away_goals: int
    available_at: datetime
    source_ref: str


@dataclass(frozen=True, slots=True)
class HistoricalSeason:
    """An archive snapshot and separately verified availability by fixture ID."""

    archive_observed_at: datetime
    archive_fixtures: tuple[Fixture, ...]
    verified_results: tuple[VerifiedHistoricalResult, ...]

    def results_known_before(
        self, cutoff: datetime
    ) -> tuple[VerifiedHistoricalResult, ...]:
        """Return only independently verified result versions strictly before cutoff."""
        _require_utc(cutoff)
        return tuple(
            result for result in self.verified_results if result.available_at < cutoff
        )


def _require_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Expected a timezone-aware UTC timestamp")


def parse_openfootball(
    text: str,
    *,
    season: Season,
    observed_at: datetime,
    reviewed_aliases: frozenset[str],
    reviewed_kickoffs: Mapping[tuple[date, str, str], datetime] | None = None,
) -> Iterable[SourceFixture]:
    """Parse match rows against exact reviewed aliases."""
    _require_utc(observed_at)
    matchweek = 0
    day = None
    header_seen = False
    reviewed_kickoffs = reviewed_kickoffs or {}
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        date_field = line[1:-1] if line.startswith("[") and line.endswith("]") else line
        try:
            if not header_seen:
                if line != "= English Premier League":
                    raise ValueError("Expected an English Premier League header")
                header_seen = True
            elif match := _MATCHDAY.fullmatch(line):
                matchweek = int(match[1])
                day = None
            elif match := _DAY.fullmatch(date_field):
                day = _fixture_date(match, season)
            elif (match := _ROW.fullmatch(line)) and day is not None and matchweek:
                clock, home, away_field = match.groups()
                if home not in reviewed_aliases:
                    raise ValueError("Unmapped home club")
                away, status, home_goals, away_goals = _parse_result(
                    away_field, reviewed_aliases
                )
                yield SourceFixture(
                    None,
                    home,
                    away,
                    _kickoff(day, clock, reviewed_kickoffs.get((day, home, away))),
                    matchweek,
                    status,
                    observed_at,
                    home_goals,
                    away_goals,
                )
            else:
                raise ValueError("Unexpected OpenFootball row")
        except ValueError as error:
            raise ValueError(f"OpenFootball line {line_no}: {error}") from None
    if not header_seen:
        raise ValueError("Missing English Premier League header")


def load_openfootball(
    path: Path,
    season: Season,
    teams: Iterable[Team],
    team_sources: Iterable[TeamSourceId],
    *,
    observed_at: datetime,
    reviewed_kickoffs: Mapping[tuple[date, str, str], datetime] | None = None,
    rescheduling_approvals: Iterable[ReschedulingApproval] = (),
    verified_availability: Mapping[str, VerifiedResultAvailability] | None = None,
) -> HistoricalSeason:
    with path.open("rb") as stream:
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("OpenFootball file exceeds the size limit")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("OpenFootball file must be UTF-8") from None
    mappings = tuple(team_sources)
    rows = parse_openfootball(
        text,
        season=season,
        observed_at=observed_at,
        reviewed_aliases=frozenset(
            m.external_id for m in mappings if m.provider == PROVIDER
        ),
        reviewed_kickoffs=reviewed_kickoffs,
    )
    fixtures = normalize_season(
        PROVIDER,
        season,
        teams,
        mappings,
        rows,
        rescheduling_approvals=rescheduling_approvals,
    )
    by_id = {fixture.fixture_id: fixture for fixture in fixtures}
    verified = []
    for fixture_id, evidence in (verified_availability or {}).items():
        fixture = by_id.get(fixture_id)
        _require_utc(evidence.available_at)
        if (
            fixture is None
            or fixture.status is not FixtureStatus.FINISHED
            or not fixture.kickoff_utc < evidence.available_at <= observed_at
            or type(evidence.home_goals) is not int
            or type(evidence.away_goals) is not int
            or (fixture.home_goals, fixture.away_goals)
            != (evidence.home_goals, evidence.away_goals)
            or not isinstance(evidence.source_ref, str)
            or not evidence.source_ref.strip()
        ):
            raise ValueError(
                "Invalid verified result availability for OpenFootball fixture"
            )
        verified.append(
            VerifiedHistoricalResult(
                fixture_id,
                evidence.home_goals,
                evidence.away_goals,
                evidence.available_at,
                evidence.source_ref,
            )
        )
    return HistoricalSeason(
        observed_at,
        fixtures,
        tuple(sorted(verified, key=lambda result: result.fixture_id)),
    )
