"""Validate and encode static v1 documents without writing or publishing files."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import fsum
from typing import Literal

from pl_intel.assignment import assign_representative_table
from pl_intel.backtest import (
    BacktestConfig,
    BacktestDataset,
    BacktestReport,
    EvidenceVerifier,
    StudyKind,
    run_backtest,
)
from pl_intel.normalize import (
    canonical_fixture_id,
    inventory_team_ids,
    validate_fixture_pairings,
)
from pl_intel.ranking import calculate_standings
from pl_intel.schemas import (
    BacktestEntry,
    BacktestIndex,
    BacktestSummary,
    FixtureStatus,
    ForecastSnapshot,
    ForecastTeam,
    HistoryEntry,
    HistoryIndex,
    LatestManifest,
    SeasonIndex,
)
from pl_intel.simulate import SeasonSnapshot, SimulationResult
from pl_intel.validate import require_canonical_id

DATA_INDEX_PATH = "/data/index.json"
BACKTEST_INDEX_PATH = "/data/backtests/index.json"
_TOLERANCE = 1e-12
_SAFE_INTEGER = 2**53 - 1
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_PUBLIC_LIMITATIONS = (
    "The first forecast can become stale after a subsequent postponement.",
    "A paired Brier comparison alone does not establish calibration or production performance.",
)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z"
)

type DocumentKind = Literal[
    "season_index", "latest", "snapshot", "history", "backtest_index"
]
type PublicDocument = (
    SeasonIndex | LatestManifest | ForecastSnapshot | HistoryIndex | BacktestIndex
)


@dataclass(frozen=True, slots=True)
class SnapshotInputs:
    """Canonical season and simulation supplied by a trusted caller.

    T12 reconciles derived standings and forecast values. The caller must
    supply the original simulation inventory. T09's input digest also covers
    eligible Elo history, which is unavailable at this boundary.
    """

    season: SeasonSnapshot
    simulation: SimulationResult


def _segment(value: str) -> str:
    require_canonical_id(value)
    return value


def latest_path(season_id: str) -> str:
    return f"/data/seasons/{_segment(season_id)}/latest.json"


def snapshot_path(season_id: str, snapshot_id: str) -> str:
    return f"/data/seasons/{_segment(season_id)}/snapshots/{_segment(snapshot_id)}.json"


def history_path(season_id: str) -> str:
    return f"/data/seasons/{_segment(season_id)}/history.json"


def backtest_path(backtest_id: str) -> str:
    if backtest_id == "index":
        raise ValueError("Backtest ID is reserved")
    return f"/data/backtests/{_segment(backtest_id)}.json"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _fields(document: dict, allowed: frozenset[str]) -> None:
    _require(
        type(document) is dict and document.keys() == allowed,
        "Invalid public document fields",
    )


def _text(value: str) -> None:
    _require(
        isinstance(value, str)
        and 0 < len(value) <= 256
        and not any(c.isspace() or c == "\ufeff" for c in (value[0], value[-1]))
        and all(ord(c) >= 32 for c in value),
        "Invalid public text",
    )


def _number(value: float, low: float, high: float) -> None:
    _require(
        type(value) in (int, float) and low <= value <= high, "Invalid public number"
    )


def _integer(value: int, low: int, high: int) -> None:
    # Require Python ints so the producer emits integral tokens. JavaScript
    # validates safe integer values because parsing erases token spelling.
    _require(type(value) is int and low <= value <= high, "Invalid public integer")


def _timestamp(value: str) -> datetime:
    _require(
        isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None,
        "Expected a UTC timestamp",
    )
    return datetime.fromisoformat(value)


def _times(document: dict) -> datetime:
    generated = _timestamp(document["generated_at"])
    _require(
        _timestamp(document["data_cutoff_at"]) <= generated,
        "Data cutoff follows generation",
    )
    return generated


def _total(values, expected: float) -> None:
    _require(abs(fsum(values) - expected) <= _TOLERANCE, "Invalid probability total")


def _season_index(document: SeasonIndex) -> None:
    active, display = document["active_season_id"], document["display_season_id"]
    status = document["season_status"]
    _require(status in ("active", "pending", "blocked"), "Invalid season status")
    if active is not None:
        _segment(active)
    _require(
        (status == "active" and active is not None and active == display)
        or (status != "active" and active is None),
        "Inconsistent season selection",
    )
    expected = None if display is None else latest_path(display)
    _require(
        document["latest_manifest_path"] == expected, "Invalid latest manifest path"
    )


def _snapshot_metadata(document: LatestManifest | ForecastSnapshot) -> None:
    _segment(document["season_id"])
    _segment(document["snapshot_id"])
    _text(document["model_version"])
    _times(document)


def _latest(document: LatestManifest) -> None:
    _snapshot_metadata(document)
    _require(
        document["snapshot_path"]
        == snapshot_path(document["season_id"], document["snapshot_id"]),
        "Invalid snapshot path",
    )
    digest = document["input_hash"]
    _require(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
        "Invalid input hash",
    )


def _snapshot(document: ForecastSnapshot) -> None:
    _snapshot_metadata(document)
    _integer(document["simulation_count"], 1, _SAFE_INTEGER)
    _text(document["representative_table_method"])
    teams = document["teams"]
    _require(type(teams) is list and len(teams) == 20, "Expected 20 forecast teams")
    for team in teams:
        _fields(team, ForecastTeam.__required_keys__)
        _segment(team["team_id"])
        _text(team["name"])
        _integer(team["actual_rank"], 1, 20)
        _integer(team["predicted_rank"], 1, 20)
        _integer(team["played"], 0, 38)
        _integer(team["actual_points"], -_SAFE_INTEGER, _SAFE_INTEGER)
        _number(team["expected_points"], team["actual_points"], _SAFE_INTEGER)
        row = team["position_probabilities"]
        _require(
            type(row) is list and len(row) == 20, "Expected 20 position probabilities"
        )
        for value in row:
            _number(value, 0, 1)
        _total(row, 1)
        _number(team["title_probability"], 0, 1)
        _number(team["relegation_probability"], 0, 1)
        _total((team["title_probability"], -row[0]), 0)
        _total((team["relegation_probability"], *(-p for p in row[-3:])), 0)
    _require(sum(t["played"] for t in teams) % 2 == 0, "Invalid aggregate team games")
    _require(len({t["team_id"] for t in teams}) == 20, "Duplicate team ID")
    _require(
        len({t["predicted_rank"] for t in teams}) == 20, "Duplicate predicted rank"
    )
    previous = 0
    for index, rank in enumerate(sorted(t["actual_rank"] for t in teams), 1):
        _require(rank == previous or rank == index, "Invalid shared official ranks")
        previous = rank
    _total((t["title_probability"] for t in teams), 1)
    _total((t["relegation_probability"] for t in teams), 3)
    for column in zip(*(t["position_probabilities"] for t in teams), strict=True):
        _total(column, 1)


def _history(document: HistoryIndex) -> None:
    season = _segment(document["season_id"])
    entries = document["snapshots"]
    _require(type(entries) is list and len(entries) <= 4096, "Invalid history length")
    seen = set()
    previous = None
    for entry in entries:
        _fields(entry, HistoryEntry.__required_keys__)
        snapshot = entry["snapshot_id"]
        _require(
            entry["path"] == snapshot_path(season, snapshot),
            "Invalid history snapshot path",
        )
        _require(snapshot not in seen, "Duplicate history snapshot")
        seen.add(snapshot)
        _text(entry["matchweek_label"])
        current = _times(entry)
        if previous is not None:
            _require(current >= previous, "History must be chronological")
        previous = current


def _backtest_index(document: BacktestIndex) -> None:
    entries = document["reports"]
    _require(
        type(entries) is list and len(entries) <= 4096, "Invalid backtest index length"
    )
    seen = set()
    for entry in entries:
        _fields(entry, BacktestEntry.__required_keys__)
        _segment(entry["season_id"])
        _text(entry["model_version"])
        key = entry["backtest_id"]
        _require(
            entry["path"] == backtest_path(key) and key not in seen,
            "Invalid backtest entry",
        )
        seen.add(key)


_VALIDATORS = {
    "season_index": (SeasonIndex, _season_index),
    "latest": (LatestManifest, _latest),
    "snapshot": (ForecastSnapshot, _snapshot),
    "history": (HistoryIndex, _history),
    "backtest_index": (BacktestIndex, _backtest_index),
}


def _encode(document: dict) -> bytes:
    payload = (
        json.dumps(document, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    _require(len(payload) <= _MAX_DOCUMENT_BYTES, "Static JSON exceeds size limit")
    return payload


def _validate_document(kind: DocumentKind, document: PublicDocument) -> None:
    schema, validate = _VALIDATORS[kind]
    _fields(document, schema.__required_keys__)
    _integer(document["schema_version"], 1, 1)
    validate(document)


def validate_manifest_snapshot(
    manifest: LatestManifest, snapshot: ForecastSnapshot
) -> None:
    """Check a validated document pair without accessing the filesystem."""
    _validate_document("latest", manifest)
    _validate_document("snapshot", snapshot)
    for key in (
        "season_id",
        "snapshot_id",
        "model_version",
        "generated_at",
        "data_cutoff_at",
    ):
        _require(
            manifest[key] == snapshot[key], "Manifest and snapshot metadata differ"
        )


def _validate_snapshot_inputs(
    document: ForecastSnapshot, inputs: SnapshotInputs
) -> None:
    season = inputs.season
    simulation = inputs.simulation
    _require(isinstance(simulation, SimulationResult), "Expected successful simulation")
    provenance = simulation.provenance
    _require(
        document["season_id"] == season.season.season_id == provenance.season_id
        and _timestamp(document["data_cutoff_at"]) == provenance.cutoff
        and document["model_version"] == provenance.model_version
        and document["simulation_count"] == provenance.simulation.trials,
        "Snapshot metadata does not match the simulation",
    )
    team_ids = inventory_team_ids(season.teams)
    _require(len(season.fixtures) == 380, "Expected 380 canonical fixtures")
    fixtures = tuple(item.fixture for item in season.fixtures)
    validate_fixture_pairings(
        team_ids, ((f.home_team_id, f.away_team_id) for f in fixtures)
    )
    for fixture in fixtures:
        _require(
            fixture.fixture_id
            == canonical_fixture_id(
                fixture.season_id, fixture.home_team_id, fixture.away_team_id
            ),
            "Invalid canonical fixture identity",
        )
        if fixture.status is FixtureStatus.FINISHED:
            _require(
                fixture.updated_at <= provenance.cutoff,
                "Finished fixture postdates cutoff",
            )
    current = calculate_standings(
        season.season,
        season.teams,
        fixtures,
        adjustments=season.adjustments,
        rules=season.rules,
        qualification=season.qualification,
        rescheduling_approvals=season.rescheduling_approvals,
        exceptional_procedure=season.exceptional_procedure,
    )
    _require(not current.blockers, "Current standings are blocked")
    standings = {row.team_id: row for row in current.rows}

    names = {team.team_id: team.canonical_name for team in season.teams}
    clubs = {club.team_id: club for club in simulation.clubs}
    _require(
        len(simulation.clubs) == 20 and clubs.keys() == team_ids,
        "Invalid simulation roster",
    )
    _require(
        {team["team_id"] for team in document["teams"]} == team_ids,
        "Invalid snapshot roster",
    )
    predicted = {
        row.team_id: row.predicted_position
        for row in assign_representative_table(simulation).rows
    }
    for team in document["teams"]:
        team_id = team["team_id"]
        _require(
            team["name"] == names[team_id], "Expected approved canonical club name"
        )
        _require(
            team["played"] == standings[team_id].played,
            "Played count does not match finished fixtures",
        )
        club = clubs[team_id]
        _require(
            team["actual_points"]
            == standings[team_id].adjusted_points
            == club.actual_adjusted_points
            and team["expected_points"] == club.expected_adjusted_points,
            "Snapshot adjusted points do not match standings and simulation",
        )
        _require(
            team["actual_rank"] == standings[team_id].rank,
            "Actual rank does not match current standings",
        )
        _require(
            tuple(team["position_probabilities"]) == club.positions
            and team["title_probability"] == club.title_probability
            and team["relegation_probability"] == club.relegation_probability,
            "Snapshot probabilities do not match the simulation",
        )
        _require(
            team["predicted_rank"] == predicted[team_id],
            "Predicted rank does not match the representative assignment",
        )


def serialize_document(
    kind: DocumentKind,
    document: PublicDocument,
    *,
    snapshot_inputs: SnapshotInputs | None = None,
) -> bytes:
    """Encode allowlisted JSON. The caller owns source-rights and metadata review."""
    _validate_document(kind, document)
    if kind == "snapshot":
        if snapshot_inputs is None:
            raise ValueError("Snapshot requires canonical inventory and simulation")
        _validate_snapshot_inputs(document, snapshot_inputs)
    return _encode(document)


def _format_utc(value: datetime) -> str:
    _require(
        isinstance(value, datetime) and value.utcoffset() == UTC.utcoffset(value),
        "Expected a UTC timestamp",
    )
    return value.isoformat().replace("+00:00", "Z")


def serialize_backtest_summary(
    dataset: BacktestDataset,
    *,
    config: BacktestConfig | None = None,
    verify_evidence: EvidenceVerifier | None = None,
    review_redistribution: Callable[[BacktestDataset, BacktestConfig], bool]
    | None = None,
) -> bytes:
    """Obtain an authenticated T11 report and project controlled public text.

    Trusted upstream callbacks own evidence authentication and rights review.
    This serializer writes no files and confers no production authorization.
    """
    _require(
        isinstance(dataset, BacktestDataset)
        and dataset.kind is StudyKind.VERIFIED_HISTORICAL
        and config is not None
        and callable(verify_evidence),
        "Backtest requires verified historical evidence",
    )
    _require(
        callable(review_redistribution)
        and review_redistribution(dataset, config) is True,
        "Backtest requires independent redistribution review",
    )
    report = run_backtest(dataset, config=config, verify_evidence=verify_evidence)
    _require(
        isinstance(report, BacktestReport)
        and report.kind is StudyKind.VERIFIED_HISTORICAL
        and report.label == "RETROSPECTIVE",
        "Backtest requires a verified retrospective report",
    )
    _segment(report.season_id)
    _integer(report.sample_count, 380, 380)
    _require(
        len(report.observations) == report.sample_count
        and len({row.fixture_id for row in report.observations}) == report.sample_count,
        "Invalid historical evaluation cohort",
    )
    for value in (report.model_version, report.cutoff_policy, report.baseline_policy):
        _text(value)
    for score in (report.model_brier, report.baseline_brier):
        _number(score, 0, 2)
    period = report.training_kickoff_span
    training_period = None
    if period is not None:
        start, end = map(_format_utc, period)
        _require(_timestamp(start) <= _timestamp(end), "Invalid training period")
        training_period = [start, end]
    document: BacktestSummary = {
        "schema_version": 1,
        "season_id": report.season_id,
        "model_version": report.model_version,
        "training_period": training_period,
        "cutoff_policy": report.cutoff_policy,
        "sample_size": report.sample_count,
        "baseline": report.baseline_policy,
        "multiclass_brier_model": report.model_brier,
        "multiclass_brier_baseline": report.baseline_brier,
        "limitations": list(_PUBLIC_LIMITATIONS),
    }
    return _encode(document)
