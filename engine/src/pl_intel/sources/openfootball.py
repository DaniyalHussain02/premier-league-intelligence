"""Read a reviewed, local OpenFootball Football.TXT season file."""

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
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
_ROW = re.compile(
    r"(?:(\d{1,2}:\d{2})\s+)?(.+?)\s+v\s+(.+?)"
    r"(?:\s{2,}([0-9]{1,3}-[0-9]{1,3}(?:\s+\([0-9]{1,3}-[0-9]{1,3}\))?|"
    r"\[postponed\]|\[cancelled\]|-))?"
)
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


def parse_openfootball(
    text: str,
    *,
    season: Season,
    observed_at: datetime,
    reviewed_kickoffs: Mapping[tuple[date, str, str], datetime] | None = None,
) -> Iterable[SourceFixture]:
    """Parse the EPL section. Observation time is archive provenance, not result availability."""
    matchweek = 0
    day = None
    header_seen = False
    reviewed_kickoffs = reviewed_kickoffs or {}
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if not header_seen:
                if line != "= English Premier League":
                    raise ValueError("Expected an English Premier League header")
                header_seen = True
            elif match := _MATCHDAY.fullmatch(line):
                matchweek = int(match[1])
                day = None
            elif match := _DAY.fullmatch(line.strip("[]")):
                day = _fixture_date(match, season)
            elif (match := _ROW.fullmatch(line)) and day is not None and matchweek:
                clock, home, away, result = match.groups()
                home, away = home.strip(), away.strip()
                status = FixtureStatus.SCHEDULED
                home_goals = away_goals = None
                if result == "[postponed]":
                    status = FixtureStatus.POSTPONED
                elif result == "[cancelled]":
                    status = FixtureStatus.CANCELLED
                elif result not in (None, "-"):
                    status = FixtureStatus.FINISHED
                    home_goals, away_goals = map(int, result.split()[0].split("-"))
                elif re.search(r"\s+(?:\d|\[|-)", away):
                    raise ValueError("Unsupported OpenFootball result")
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
) -> tuple[Fixture, ...]:
    with path.open("rb") as stream:
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("OpenFootball file exceeds the size limit")
    rows = parse_openfootball(
        data.decode("utf-8-sig"),
        season=season,
        observed_at=observed_at,
        reviewed_kickoffs=reviewed_kickoffs,
    )
    return normalize_season(
        PROVIDER,
        season,
        teams,
        team_sources,
        rows,
        rescheduling_approvals=rescheduling_approvals,
    )
