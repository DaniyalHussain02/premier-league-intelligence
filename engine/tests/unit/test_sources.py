import io
import json
from datetime import UTC, date, datetime
from email.message import Message
from itertools import permutations
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from pl_intel.schemas import FixtureStatus, Season, SeasonStatus, Team, TeamSourceId
from pl_intel.sources import football_data, openfootball

SEASON = Season(
    "2026-2027",
    date(2026, 8, 21),
    date(2027, 5, 30),
    SeasonStatus.ACTIVE,
    "demo",
    "synthetic",
)
TEAMS = tuple(Team(f"club-{n}", f"Synthetic club {n}", f"S{n}") for n in range(1, 21))
OBSERVED_AT = datetime(2027, 6, 1, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures/openfootball-synthetic.txt"


def mappings(provider):
    return tuple(
        TeamSourceId(
            provider,
            str(n) if provider == football_data.PROVIDER else team.canonical_name,
            team.team_id,
        )
        for n, team in enumerate(TEAMS, 1)
    )


def payload():
    return {
        "competition": {"code": "PL"},
        "resultSet": {"count": 380},
        "matches": [
            {
                "id": n,
                "season": {"id": 1, "startDate": "2026-08-21", "endDate": "2027-05-30"},
                "homeTeam": {"id": home},
                "awayTeam": {"id": away},
                "utcDate": "2026-08-21T19:00:00Z",
                "lastUpdated": "2026-08-21T22:00:00Z",
                "matchday": 1,
                "status": "FINISHED",
                "score": {"duration": "REGULAR", "fullTime": {"home": 2, "away": 1}},
            }
            for n, (home, away) in enumerate(permutations(range(1, 21), 2), 1)
        ],
    }


class MockOpener:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return io.BytesIO(response)
        return io.BytesIO(json.dumps(response).encode())


def http_error(status, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        "https://example.invalid/",
        status,
        "private response",
        headers,
        io.BytesIO(b"private body"),
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_TOKEN", "synthetic-test-token")
    clock = [100.0]
    monkeypatch.setattr(football_data, "monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(football_data, "sleep", advance)
    return football_data.FootballDataClient(), clock


def fetch(client):
    return client.fetch_season(SEASON, TEAMS, mappings(football_data.PROVIDER))


def test_complete_inventory_uses_fixed_endpoint_and_runtime_token(client):
    api, _ = client
    api._opener = opener = MockOpener(payload())
    records = fetch(api)
    assert len(records) == len({f.fixture_id for f in records}) == 380
    assert all((f.home_goals, f.away_goals) == (2, 1) for f in records)
    request, timeout = opener.calls[0]
    assert (
        request.full_url
        == "https://api.football-data.org/v4/competitions/PL/matches?season=2026"
    )
    assert request.get_header("X-auth-token") == "synthetic-test-token"
    assert 0 < timeout <= 10


def test_retry_after_and_server_error_are_rate_limited(client):
    api, clock = client
    api._opener = opener = MockOpener(http_error(429, "13"), http_error(503), payload())
    assert len(fetch(api)) == 380
    assert len(opener.calls) == 3
    assert clock[0] == 120


@pytest.mark.parametrize("status", [302, 401, 403, 404])
def test_nonretryable_http_errors_are_sanitized(client, status):
    api, _ = client
    api._opener = opener = MockOpener(http_error(status))
    with pytest.raises(football_data.SourceError) as err:
        fetch(api)
    assert len(opener.calls) == 1
    assert not any(
        x in str(err.value)
        for x in ("synthetic-test-token", "private", "example.invalid")
    )
    assert err.value.__suppress_context__


def test_redirect_handler_declines_cross_origin_redirect():
    handler = football_data._NoRedirect()
    assert (
        handler.redirect_request(None, None, 302, None, {}, "https://example.invalid/")
        is None
    )


def test_no_request_without_runtime_token(client, monkeypatch):
    api, _ = client
    api._opener = opener = MockOpener()
    monkeypatch.delenv("FOOTBALL_DATA_API_TOKEN")
    with pytest.raises(football_data.SourceError, match="runtime environment"):
        fetch(api)
    assert opener.calls == []


def test_rate_limit_wait_budget(client):
    api, _ = client
    api._opener = opener = MockOpener(http_error(429, "120"))
    with pytest.raises(football_data.SourceError, match="wait budget"):
        fetch(api)
    assert len(opener.calls) == 1


def test_transport_failures_stop_after_three_attempts(client):
    api, clock = client
    api._opener = opener = MockOpener(
        TimeoutError(), URLError("private"), TimeoutError()
    )
    with pytest.raises(football_data.SourceError, match="bounded retries"):
        fetch(api)
    assert len(opener.calls) == 3
    assert clock[0] == 114


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (b"{broken", "Invalid football-data.org JSON"),
        (b"x" * (2 * 1024 * 1024 + 1), "size limit"),
    ],
)
def test_malformed_or_oversized_response(client, response, message):
    api, _ = client
    api._opener = MockOpener(response)
    with pytest.raises(football_data.SourceError, match=message):
        fetch(api)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"homeTeam": {"id": 99}}, "Unmapped"),
        ({"id": 2}, "Duplicate provider fixture ID"),
        ({"status": "AWARDED"}, "Unsupported"),
        ({"utcDate": "2026-08-21T19:00:00"}, "UTC timestamp"),
        ({"score": {"fullTime": {"home": "2", "away": 1}}}, "integer scores"),
        (
            {"season": {"id": 1, "startDate": "2025-08-21", "endDate": "2026-05-30"}},
            "Unexpected.*season",
        ),
    ],
)
def test_invalid_provider_records_are_rejected(client, changes, message):
    api, _ = client
    response = payload()
    response["matches"][0].update(changes)
    api._opener = MockOpener(response)
    with pytest.raises(ValueError, match=message):
        fetch(api)


def test_partial_inventory_is_rejected_even_if_provider_count_matches(client):
    api, _ = client
    response = payload()
    response["matches"].pop()
    response["resultSet"]["count"] -= 1
    api._opener = MockOpener(response)
    with pytest.raises(ValueError, match="380 directed club pairings"):
        fetch(api)


def test_duplicate_pairing_rejected_even_with_unique_ids(client):
    api, _ = client
    response = payload()
    response["matches"][0]["awayTeam"] = response["matches"][1]["awayTeam"]
    api._opener = MockOpener(response)
    with pytest.raises(ValueError, match="Duplicate season fixture pairing"):
        fetch(api)


def test_parse_frozen_football_txt_without_network_or_credentials(monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_API_TOKEN", raising=False)
    records = list(
        openfootball.parse_openfootball(
            FIXTURE.read_text(), season=SEASON, observed_at=OBSERVED_AT
        )
    )
    assert records[0].kickoff_utc == datetime(2026, 8, 21, 19, tzinfo=UTC)
    assert (records[0].home_goals, records[0].away_goals) == (2, 1)
    assert records[1].status is FixtureStatus.POSTPONED
    assert records[1].home_goals is records[1].away_goals is None


def test_date_only_result_requires_reviewed_kickoff():
    text = "= English Premier League\n▪ Matchday 1\nFri Aug 21\nHome v Away   2-1"
    with pytest.raises(ValueError, match="reviewed kickoff"):
        list(
            openfootball.parse_openfootball(
                text, season=SEASON, observed_at=OBSERVED_AT
            )
        )
    kickoff = datetime(2026, 8, 21, 19, tzinfo=UTC)
    rows = list(
        openfootball.parse_openfootball(
            text,
            season=SEASON,
            observed_at=OBSERVED_AT,
            reviewed_kickoffs={(kickoff.date(), "Home", "Away"): kickoff},
        )
    )
    assert rows[0].kickoff_utc == kickoff


def test_local_complete_season_matches_same_canonical_ids(tmp_path, client):
    path = tmp_path / "synthetic.txt"
    pairs = permutations(TEAMS, 2)
    path.write_text(
        "= English Premier League\n▪ Matchday 1\nFri Aug 21\n"
        + "\n".join(
            f"20:00 {home.canonical_name} v {away.canonical_name}  2-1"
            for home, away in pairs
        )
    )
    records = openfootball.load_openfootball(
        path,
        SEASON,
        TEAMS,
        mappings(openfootball.PROVIDER),
        observed_at=OBSERVED_AT,
    )
    api, _ = client
    api._opener = MockOpener(payload())
    assert len(records) == 380
    assert [f.fixture_id for f in records] == [f.fixture_id for f in fetch(api)]


def test_current_inventory_rejects_extra_provider_team_id(client):
    api, _ = client
    response = payload()
    response["matches"][0]["homeTeam"] = {"id": 99}
    api._opener = MockOpener(response)
    extra = TeamSourceId(football_data.PROVIDER, "99", TEAMS[0].team_id)
    with pytest.raises(ValueError, match="20 provider team IDs"):
        api.fetch_season(SEASON, TEAMS, (*mappings(football_data.PROVIDER), extra))


def test_duplicate_json_keys_are_rejected(client):
    api, _ = client
    api._opener = MockOpener(b'{"competition": {}, "competition": {"code": "PL"}}')
    with pytest.raises(
        football_data.SourceError, match="Invalid football-data.org JSON"
    ):
        fetch(api)


def test_rejects_ambiguous_local_kickoff():
    text = "= English Premier League\n▪ Matchday 1\nSun Oct 25\n01:30 Home v Away   2-1"
    with pytest.raises(ValueError, match="Ambiguous or nonexistent"):
        list(
            openfootball.parse_openfootball(
                text, season=SEASON, observed_at=OBSERVED_AT
            )
        )
