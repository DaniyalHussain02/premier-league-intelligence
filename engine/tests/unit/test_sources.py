import io
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from itertools import permutations
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from pl_intel.normalize import canonical_fixture_id
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
    api = football_data.FootballDataClient()
    monkeypatch.setattr(football_data, "_NEXT_REQUEST", 0.0)

    def attempt(year, token, deadline):
        assert 0 < deadline - clock[0] <= football_data._TIMEOUT
        try:
            return football_data._request_once(year, token, api._opener)
        except football_data.SourceError:
            return -1, None, b""

    monkeypatch.setattr(football_data, "_run_attempt", attempt)
    return api, clock


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
    assert err.value.__context__ is None


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
            FIXTURE.read_text(),
            season=SEASON,
            observed_at=OBSERVED_AT,
            reviewed_aliases=frozenset({"Synthetic Home", "Synthetic Away"}),
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
                text,
                season=SEASON,
                observed_at=OBSERVED_AT,
                reviewed_aliases=frozenset({"Home", "Away"}),
            )
        )
    kickoff = datetime(2026, 8, 21, 19, tzinfo=UTC)
    rows = list(
        openfootball.parse_openfootball(
            text,
            season=SEASON,
            observed_at=OBSERVED_AT,
            reviewed_aliases=frozenset({"Home", "Away"}),
            reviewed_kickoffs={(kickoff.date(), "Home", "Away"): kickoff},
        )
    )
    assert rows[0].kickoff_utc == kickoff


@pytest.fixture
def historical_path(tmp_path):
    path = tmp_path / "synthetic.txt"
    pairs = permutations(TEAMS, 2)
    path.write_text(
        "= English Premier League\n▪ Matchday 1\nFri Aug 21\n"
        + "\n".join(
            f"20:00 {home.canonical_name} v {away.canonical_name}  2-1"
            for home, away in pairs
        )
    )
    return path


def test_local_complete_season_matches_same_canonical_ids(historical_path, client):
    history = openfootball.load_openfootball(
        historical_path,
        SEASON,
        TEAMS,
        mappings(openfootball.PROVIDER),
        observed_at=OBSERVED_AT,
    )
    api, _ = client
    api._opener = MockOpener(payload())
    assert len(history.archive_fixtures) == 380
    assert [f.fixture_id for f in history.archive_fixtures] == [
        f.fixture_id for f in fetch(api)
    ]
    assert history.results_known_before(OBSERVED_AT + timedelta(days=1)) == ()


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
                text,
                season=SEASON,
                observed_at=OBSERVED_AT,
                reviewed_aliases=frozenset({"Home", "Away"}),
            )
        )


@pytest.mark.parametrize(
    "suffix",
    [
        "  2:x",
        "  awarded",
        "  2-1 extra",
        "  2-1 (bad-score)",
        " [postponed]",
        " abandoned",
        "  [replayed]",
    ],
)
def test_malformed_openfootball_suffix_is_rejected_in_parser(suffix):
    text = (
        "= English Premier League\n▪ Matchday 1\nFri Aug 21\n20:00 Home v Away" + suffix
    )
    with pytest.raises(ValueError, match=r"OpenFootball line 4: .*result suffix"):
        list(
            openfootball.parse_openfootball(
                text,
                season=SEASON,
                observed_at=OBSERVED_AT,
                reviewed_aliases=frozenset({"Home", "Away"}),
            )
        )


@pytest.mark.parametrize(
    "row", ["[Fri Aug 21", "Fri Aug 32", "Fri Xxx 21", "Sat Aug 21"]
)
def test_malformed_date_header_fails_safely(row):
    text = f"= English Premier League\n▪ Matchday 1\n{row}\n20:00 Home v Away  2-1"
    with pytest.raises(ValueError, match=r"^OpenFootball line 3:"):
        list(
            openfootball.parse_openfootball(
                text,
                season=SEASON,
                observed_at=OBSERVED_AT,
                reviewed_aliases=frozenset({"Home", "Away"}),
            )
        )


def test_rejects_spring_dst_gap():
    text = "= English Premier League\n▪ Matchday 32\nSun Mar 28\n01:30 Home v Away  2-1"
    with pytest.raises(ValueError, match="Ambiguous or nonexistent"):
        list(
            openfootball.parse_openfootball(
                text,
                season=SEASON,
                observed_at=OBSERVED_AT,
                reviewed_aliases=frozenset({"Home", "Away"}),
            )
        )


def test_verified_availability_is_separate_and_version_specific(historical_path):
    fixture_id = canonical_fixture_id(
        SEASON.season_id, TEAMS[0].team_id, TEAMS[1].team_id
    )
    when = datetime(2026, 8, 21, 22, tzinfo=UTC)
    evidence = openfootball.VerifiedResultAvailability(when, 2, 1, "synthetic evidence")

    def load(verification):
        return openfootball.load_openfootball(
            historical_path,
            SEASON,
            TEAMS,
            mappings(openfootball.PROVIDER),
            observed_at=OBSERVED_AT,
            verified_availability={fixture_id: verification},
        )

    history = load(evidence)
    assert history.archive_observed_at == OBSERVED_AT
    assert history.results_known_before(when) == ()
    results = history.results_known_before(when + timedelta(seconds=1))
    assert len(results) == 1 and results[0].fixture_id == fixture_id
    assert results[0].updated_at == OBSERVED_AT
    with pytest.raises(ValueError, match="Invalid verified result availability"):
        load(
            openfootball.VerifiedResultAvailability(when, 0, 1, "another score version")
        )
    with pytest.raises(ValueError, match="Invalid verified result availability"):
        load(
            openfootball.VerifiedResultAvailability(
                OBSERVED_AT + timedelta(days=1), 2, 1, "late evidence"
            )
        )


def test_overall_budget_stops_before_server_cooldown_expires(client, monkeypatch):
    api, clock = client
    monkeypatch.setattr(football_data, "_REQUEST_BUDGET", 12)
    api._opener = opener = MockOpener(http_error(429, "13"))
    with pytest.raises(football_data.SourceError, match="overall request budget"):
        fetch(api)
    assert len(opener.calls) == 1
    assert clock[0] == 100
    assert football_data._NEXT_REQUEST == 113


@pytest.mark.parametrize(
    "partial_body", [False, True], ids=["before-headers", "during-body"]
)
def test_worker_deadline_kills_and_reaps_stalled_child(monkeypatch, partial_body):
    script = "import sys,time; sys.stdin.read(); "
    if partial_body:
        script += "sys.stdout.buffer.write(b'[200, null]\\n{'); sys.stdout.flush(); "
    script += "time.sleep(30)"
    monkeypatch.setattr(
        football_data, "_worker_command", lambda year: [sys.executable, "-c", script]
    )
    started = []
    original = subprocess.Popen

    def capture(*args, **kwargs):
        assert "synthetic-test-token" not in args[0]
        assert "FOOTBALL_DATA_API_TOKEN" not in kwargs["env"]
        child = original(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(football_data.subprocess, "Popen", capture)
    start = football_data.monotonic()
    result = football_data._run_attempt(2026, "synthetic-test-token", start + 0.25)
    assert result == (0, None, b"")
    assert football_data.monotonic() - start < 5
    assert len(started) == 1 and started[0].returncode is not None
    assert started[0].stdin.closed and started[0].stdout.closed


def test_worker_is_reaped_on_interruption(monkeypatch):
    monkeypatch.setattr(
        football_data,
        "_worker_command",
        lambda year: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    started = []

    class InterruptedWorker(subprocess.Popen):
        def communicate(self, *args, **kwargs):
            started.append(self)
            raise KeyboardInterrupt

    monkeypatch.setattr(football_data.subprocess, "Popen", InterruptedWorker)
    with pytest.raises(KeyboardInterrupt):
        football_data._run_attempt(
            2026, "synthetic-test-token", football_data.monotonic() + 1
        )
    assert len(started) == 1 and started[0].returncode is not None
    assert started[0].stdin.closed and started[0].stdout.closed


def test_worker_response_protocol_and_cleanup_on_failure(monkeypatch):
    argv = [
        sys.executable,
        "-c",
        (
            "import sys; token = sys.stdin.read(); "
            "assert token == 'synthetic-test-token'; "
            "sys.stdout.buffer.write(b'[200, null]\\n{}')"
        ),
    ]
    monkeypatch.setattr(football_data, "_worker_command", lambda year: argv)
    assert football_data._run_attempt(
        2026, "synthetic-test-token", football_data.monotonic() + 2
    ) == (200, None, b"{}")
    argv[2] = "import sys; sys.stderr.write(sys.stdin.read()); sys.exit(9)"
    assert football_data._run_attempt(
        2026, "synthetic-test-token", football_data.monotonic() + 2
    ) == (0, None, b"")


def test_occupied_worker_slot_counts_against_request_budget(client, monkeypatch):
    api, _ = client
    api._opener = opener = MockOpener()
    monkeypatch.setattr(football_data, "_REQUEST_BUDGET", 0.02)
    with (
        football_data._WORKER_LOCK,
        pytest.raises(football_data.SourceError, match="overall request budget"),
    ):
        fetch(api)
    assert opener.calls == []
