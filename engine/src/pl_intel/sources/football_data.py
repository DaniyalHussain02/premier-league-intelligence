"""Fetch a single football-data.org EPL season and validate before returning it."""

import json
import math
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from threading import Lock
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pl_intel.normalize import SourceFixture, normalize_season
from pl_intel.schemas import (
    Fixture,
    FixtureStatus,
    ReschedulingApproval,
    Season,
    Team,
    TeamSourceId,
)

PROVIDER = "football-data-org"
_ENDPOINT = "https://api.football-data.org/v4/competitions/PL/matches"
_TIMEOUT = 10
_MAX_BODY = 2 * 1024 * 1024
_MAX_ATTEMPTS = 3
_MIN_INTERVAL = 7
_MAX_RETRY_WAIT = 60

_STATUSES = {
    "SCHEDULED": FixtureStatus.SCHEDULED,
    "TIMED": FixtureStatus.SCHEDULED,
    "POSTPONED": FixtureStatus.POSTPONED,
    "CANCELLED": FixtureStatus.CANCELLED,
    "FINISHED": FixtureStatus.FINISHED,
}


class SourceError(ValueError):
    """An ingestion error safe to report without provider payloads or credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_delay(value: str | None, attempt: int) -> float:
    fallback = float(2**attempt)
    if value is None:
        return fallback
    try:
        delay = float(value)
        if not math.isfinite(delay) or delay < 0:
            return fallback
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.utcoffset() is None:
                return fallback
            delay = (retry_at - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    return max(fallback, delay)


def _read_body(response) -> bytes:
    deadline = monotonic() + _TIMEOUT
    body = bytearray()
    while len(body) <= _MAX_BODY:
        chunk = response.read1(min(64 * 1024, _MAX_BODY + 1 - len(body)))
        if monotonic() > deadline:
            raise TimeoutError
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    raise SourceError("football-data.org response exceeds the size limit")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return result


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise SourceError("Invalid football-data.org object")
    return value


def _id(value: object) -> str:
    if type(value) is not int or value < 1:
        raise SourceError("Invalid football-data.org ID")
    return str(value)


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise SourceError("Invalid football-data.org UTC timestamp")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise SourceError("Invalid football-data.org UTC timestamp") from None
    if stamp.utcoffset() != UTC.utcoffset(None):
        raise SourceError("Invalid football-data.org UTC timestamp")
    return stamp


def _rows(payload: object, season: Season) -> Iterable[SourceFixture]:
    data = _object(payload)
    if _object(data.get("competition")).get("code") != "PL":
        raise SourceError("Unexpected football-data.org competition")
    matches = data.get("matches")
    if not isinstance(matches, list) or len(matches) > 380:
        raise SourceError("Invalid football-data.org match inventory")
    count = _object(data.get("resultSet")).get("count")
    if type(count) is not int or count != len(matches):
        raise SourceError(
            "football-data.org result count disagrees with match inventory"
        )

    provider_season_id = None
    for item in matches:
        row = _object(item)
        source_season = _object(row.get("season"))
        source_id = _id(source_season.get("id"))
        if (
            source_season.get("startDate") != season.start_date.isoformat()
            or source_season.get("endDate") != season.end_date.isoformat()
            or (provider_season_id is not None and source_id != provider_season_id)
        ):
            raise SourceError("Unexpected football-data.org season")
        provider_season_id = source_id
        source_status = row.get("status")
        if not isinstance(source_status, str) or source_status not in _STATUSES:
            raise SourceError("Unsupported football-data.org match status")
        status = _STATUSES[source_status]
        score = _object(row.get("score") if row.get("score") is not None else {})
        full_time = _object(
            score.get("fullTime") if score.get("fullTime") is not None else {}
        )
        if status is FixtureStatus.FINISHED and score.get("duration") not in (
            None,
            "REGULAR",
        ):
            raise SourceError("Unsupported football-data.org score duration")
        yield SourceFixture(
            _id(row.get("id")),
            _id(_object(row.get("homeTeam")).get("id")),
            _id(_object(row.get("awayTeam")).get("id")),
            _utc(row.get("utcDate")),
            row.get("matchday"),
            status,
            _utc(row.get("lastUpdated")),
            full_time.get("home"),
            full_time.get("away"),
        )


class FootballDataClient:
    """Reuse a client for a batch so request pacing applies across calls."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirect())
        self._pacing_lock = Lock()
        self._next_request = 0.0

    def _pace(self) -> None:
        with self._pacing_lock:
            remaining = self._next_request - monotonic()
            if remaining > 0:
                sleep(remaining)
            self._next_request = monotonic() + _MIN_INTERVAL

    def _fetch(self, year: int) -> object:
        token = os.environ.get("FOOTBALL_DATA_API_TOKEN")
        if (
            not token
            or not token.isascii()
            or not token.isprintable()
            or any(c.isspace() for c in token)
        ):
            raise SourceError("Set FOOTBALL_DATA_API_TOKEN in the runtime environment")
        request = Request(f"{_ENDPOINT}?season={year}", headers={"X-Auth-Token": token})

        for attempt in range(_MAX_ATTEMPTS):
            self._pace()
            try:
                with self._opener.open(request, timeout=_TIMEOUT) as response:
                    body = _read_body(response)
                try:
                    return json.loads(body, object_pairs_hook=_unique_object)
                except (ValueError, RecursionError):
                    raise SourceError("Invalid football-data.org JSON") from None
            except HTTPError as error:
                code = error.code
                retry_after = (
                    error.headers.get("Retry-After")
                    if error.headers is not None
                    else None
                )
                error.close()
                if code in {401, 403}:
                    raise SourceError(
                        "football-data.org authentication or access denied"
                    ) from None
                if code not in {429, 500, 502, 503, 504}:
                    raise SourceError(
                        f"football-data.org returned HTTP {code}"
                    ) from None
                delay = _retry_delay(retry_after, attempt)
                if delay > _MAX_RETRY_WAIT:
                    raise SourceError(
                        "football-data.org retry delay exceeds the wait budget"
                    ) from None
            except (URLError, OSError, HTTPException):
                delay = _retry_delay(None, attempt)
            if attempt + 1 < _MAX_ATTEMPTS:
                sleep(delay)
        raise SourceError("football-data.org request failed after bounded retries")

    def fetch_season(
        self,
        season: Season,
        teams: Iterable[Team],
        team_sources: Iterable[TeamSourceId],
        *,
        rescheduling_approvals: Iterable[ReschedulingApproval] = (),
    ) -> tuple[Fixture, ...]:
        payload = self._fetch(season.start_date.year)
        return normalize_season(
            PROVIDER,
            season,
            teams,
            team_sources,
            _rows(payload, season),
            rescheduling_approvals=rescheduling_approvals,
        )
