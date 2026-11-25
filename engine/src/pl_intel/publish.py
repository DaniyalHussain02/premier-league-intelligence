"""Validate static v1 documents and stage complete snapshot updates."""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from math import fsum
from pathlib import Path
from tempfile import TemporaryDirectory
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
from pl_intel.elo import EloMatch
from pl_intel.normalize import (
    canonical_fixture_id,
    inventory_team_ids,
    validate_fixture_pairings,
)
from pl_intel.poisson import MODEL_VERSION
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
    SeasonStatus,
)
from pl_intel.season import ResolutionStatus, SeasonResolution
from pl_intel.simulate import (
    _CUTOFF_POLICY,
    _PYTHON,
    _RNG,
    _SIMULATOR_VERSION,
    SeasonSnapshot,
    SimulationBlocked,
    SimulationResult,
    _fingerprint,
    _prepare_simulation,
    _PreparedSimulation,
    _source_fingerprint,
)
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


def _publication_hash(prepared: _PreparedSimulation) -> str:
    """Hash semantic inputs, excluding poll timestamps and batch size."""
    season, history = prepared.snapshot, prepared.history
    return _fingerprint(
        "t13-publication-v1",
        {
            "season": replace(season.season, source_ref=""),
            "teams": sorted(season.teams, key=lambda team: team.team_id),
            "fixtures": [
                replace(item.fixture, updated_at=item.fixture.kickoff_utc)
                for item in sorted(season.fixtures, key=lambda f: f.fixture.fixture_id)
            ],
            "history": [
                (
                    match.fixture.fixture_id,
                    match.fixture.kickoff_utc,
                    match.result.home_goals,
                    match.result.away_goals,
                    match.result.available_at,
                )
                for match in sorted(history, key=lambda m: m.result.fixture_id)
            ],
            "rules": replace(season.rules, evidence_ref=""),
            "exceptional_procedure": season.exceptional_procedure,
            "adjustment_status": season.adjustments.status,
            "adjustments": sorted(
                (item.team_id, item.points_delta)
                for item in season.adjustments.adjustments
            ),
            "qualification": (
                sorted(season.qualification.after_positions)
                if season.qualification is not None
                else None
            ),
            "rescheduling": sorted(
                (item.fixture_id, item.kickoff_utc)
                for item in season.rescheduling_approvals
            ),
            "model": MODEL_VERSION,
            "simulator": _SIMULATOR_VERSION,
            "runtime": (_PYTHON, _RNG, _CUTOFF_POLICY),
            "simulation": (prepared.config.seed, prepared.config.trials),
            "elo": prepared.elo_config,
            "poisson": prepared.poisson_config,
        },
    )


def _snapshot_document(
    inputs: SnapshotInputs, snapshot_id: str, generated_at: datetime
) -> ForecastSnapshot:
    season, simulation = inputs.season, inputs.simulation
    run = simulation.provenance
    current = calculate_standings(
        season.season,
        season.teams,
        tuple(item.fixture for item in season.fixtures),
        adjustments=season.adjustments,
        rules=season.rules,
        qualification=season.qualification,
        rescheduling_approvals=season.rescheduling_approvals,
        exceptional_procedure=season.exceptional_procedure,
    )
    _require(not current.blockers, "Current standings are blocked")
    assignment = assign_representative_table(simulation)
    predicted = {row.team_id: row.predicted_position for row in assignment.rows}
    clubs = {club.team_id: club for club in simulation.clubs}
    names = {team.team_id: team.canonical_name for team in season.teams}
    teams: list[ForecastTeam] = []
    for row in current.rows:
        rank = row.rank
        if rank is None:
            raise ValueError("Missing official current rank")
        club = clubs[row.team_id]
        teams.append(
            ForecastTeam(
                team_id=row.team_id,
                name=names[row.team_id],
                actual_rank=rank,
                predicted_rank=predicted[row.team_id],
                played=row.played,
                actual_points=row.adjusted_points,
                expected_points=club.expected_adjusted_points,
                position_probabilities=list(club.positions),
                title_probability=club.title_probability,
                relegation_probability=club.relegation_probability,
            )
        )
    return ForecastSnapshot(
        schema_version=1,
        season_id=run.season_id,
        snapshot_id=snapshot_id,
        data_cutoff_at=_format_utc(run.cutoff),
        generated_at=_format_utc(generated_at),
        simulation_count=run.simulation.trials,
        model_version=run.model_version,
        representative_table_method="maximum-weight-position-assignment",
        teams=teams,
    )


@dataclass(frozen=True, slots=True)
class ArchivedSnapshot:
    """Full original manifest and byte digest from a trusted publication revision."""

    manifest: LatestManifest
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class PriorPublication:
    """Protected checkout of revision, plus the current destination revision reader.

    The caller owns checkout isolation and the archive trust boundary. An
    archive adapter must retrieve the original committed manifest and snapshot
    digest from trusted Git history, never infer them from a shortened ID.
    """

    root: Path
    revision: str
    current_revision: Callable[[], str]
    archived_snapshot: Callable[[str, str], ArchivedSnapshot | None] | None = None

    def validate(self) -> None:
        _require(
            re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", self.revision) is not None,
            "Invalid prior revision",
        )
        _require(self.current_revision() == self.revision, "Stale prior revision")


@dataclass(frozen=True, slots=True)
class StagedPublication:
    """Temporary patch relative to data/ and the required atomic commit base.

    T16 must compare-and-swap the destination ref against expected_revision,
    apply the entire patch in one commit, and test commit-failure preservation.
    A successful preparation does not authorize a production write.
    """

    directory: Path
    expected_revision: str


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    _require(len(result) == len(pairs), "Duplicate JSON key")
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("Nonstandard JSON constant")


def _read_document(
    root: Path, path: str, *, limit: int = _MAX_DOCUMENT_BYTES
) -> tuple[dict, bytes] | None:
    # The prior tree must remain protected against concurrent filesystem edits.
    _require(
        re.fullmatch(r"/data/(?:[a-z0-9-]+/)*[a-z0-9-]+\.json", path) is not None,
        "Invalid prior document path",
    )
    relative = Path(path.removeprefix("/data/"))
    target = root / relative
    _require(
        not root.is_symlink() and target.resolve() == root.resolve() / relative,
        "Unexpected link in prior publication",
    )
    if not target.is_file():
        _require(not target.exists(), "Prior document is not a regular file")
        return None
    with target.open("rb") as stream:
        contents = stream.read(limit + 1)
    _require(len(contents) <= limit, "Existing document exceeds validation budget")
    try:
        document = json.loads(
            contents, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except RecursionError as error:
        raise ValueError("Existing document is too deeply nested") from error
    _require(type(document) is dict, "Invalid existing document")
    return document, contents


def _read_season(
    root: Path, season_id: str, *, candidate_id: str
) -> tuple[LatestManifest | None, list[HistoryEntry], tuple[dict, bytes] | None]:
    latest = _read_document(root, latest_path(season_id))
    history = _read_document(root, history_path(season_id))
    _require((latest is None) == (history is None), "Incomplete prior publication")
    if latest is None or history is None:
        return None, [], None
    manifest, index = latest[0], history[0]
    _validate_document("latest", manifest)
    _validate_document("history", index)
    _require(
        manifest["season_id"] == index["season_id"] == season_id,
        "Prior history and manifest seasons differ",
    )
    entries = index["snapshots"]
    matched_latest = False
    candidate = None
    budget = 64 * 1024 * 1024
    for entry in entries:
        saved = _read_document(
            root, entry["path"], limit=min(budget, _MAX_DOCUMENT_BYTES)
        )
        if saved is None:
            raise ValueError("Prior history references a missing snapshot")
        document, contents = saved
        budget -= len(contents)
        _validate_document("snapshot", document)
        _require(
            document["season_id"] == season_id
            and all(
                entry[key] == document[key]
                for key in ("snapshot_id", "generated_at", "data_cutoff_at")
            ),
            "Prior history and snapshot metadata differ",
        )
        if entry["snapshot_id"] == manifest["snapshot_id"]:
            validate_manifest_snapshot(manifest, document)
            _require(
                manifest["snapshot_id"] == manifest["input_hash"][:24],
                "Prior manifest identity is inconsistent",
            )
            matched_latest = True
        if entry["snapshot_id"] == candidate_id:
            candidate = saved
    _require(matched_latest, "Prior history does not contain the latest snapshot")
    return manifest, entries, candidate


def _verify_simulation(prepared: _PreparedSimulation, result: SimulationResult) -> None:
    run = result.provenance
    _require(
        run.input_fingerprint == prepared.input_fingerprint
        and run.cutoff == prepared.cutoff
        and run.season_id == prepared.snapshot.season.season_id
        and run.simulation == prepared.config
        and run.elo == prepared.elo_config
        and run.poisson == prepared.poisson_config
        and (
            run.model_version,
            run.simulator_version,
            run.rng,
            run.cutoff_policy,
            run.python,
        )
        == (MODEL_VERSION, _SIMULATOR_VERSION, _RNG, _CUTOFF_POLICY, _PYTHON),
        "Simulation provenance does not match prepared inputs",
    )


def _validated_simulation(
    prepared: _PreparedSimulation | SimulationBlocked,
) -> _PreparedSimulation:
    if isinstance(prepared, SimulationBlocked):
        # A blocked simulation is a valid result type.
        raise RuntimeError("Simulation is blocked")  # noqa: TRY004
    return prepared


@contextmanager
def _stage_snapshot(
    prior: PriorPublication,
    inputs: SnapshotInputs,
    history: tuple[EloMatch, ...],
    *,
    generated_at: datetime,
    matchweek_label: str,
    resolution: SeasonResolution | None = None,
) -> Iterator[StagedPublication | None]:
    """Verify separately supplied simulation inputs before any no-op or handoff."""
    run = inputs.simulation.provenance
    prepared = _validated_simulation(
        _prepare_simulation(
            inputs.season,
            history,
            cutoff=run.cutoff,
            config=run.simulation,
            elo_config=run.elo,
            poisson_config=run.poisson,
        )
    )
    _verify_simulation(prepared, inputs.simulation)
    with _stage_prepared(
        prior,
        prepared,
        lambda: inputs.simulation,
        generated_at=generated_at,
        matchweek_label=matchweek_label,
        resolution=resolution,
    ) as stage:
        yield stage


@contextmanager
def _stage_prepared(
    prior: PriorPublication,
    prepared: _PreparedSimulation,
    run_trials: Callable[[], SimulationResult | SimulationBlocked],
    *,
    generated_at: datetime,
    matchweek_label: str,
    resolution: SeasonResolution | None,
) -> Iterator[StagedPublication | None]:
    prior.validate()
    _format_utc(generated_at)
    _require(generated_at >= prepared.cutoff, "Generation time precedes data cutoff")
    season = prepared.snapshot.season
    _require(
        season.status is SeasonStatus.ACTIVE, "Publication requires an active season"
    )
    _require(
        prepared.input_fingerprint
        == _source_fingerprint(
            prepared.snapshot,
            prepared.teams,
            prepared.snapshot.fixtures,
            prepared.history,
        ),
        "Prepared simulation source fingerprint is inconsistent",
    )
    digest = _publication_hash(prepared)
    snapshot_id = digest[:24]
    season_id = season.season_id
    latest_file = latest_path(season_id)
    new_file = snapshot_path(season_id, snapshot_id)

    prior_index = _read_document(prior.root, DATA_INDEX_PATH)
    display = None
    if prior_index is not None:
        _validate_document("season_index", prior_index[0])
        display = prior_index[0]["display_season_id"]
    else:
        seasons = prior.root / "seasons"
        _require(
            not seasons.exists() and not seasons.is_symlink(),
            "Prior global index is missing",
        )
    latest, entries, archived = _read_season(
        prior.root, season_id, candidate_id=snapshot_id
    )
    index_changed = (
        prior_index is None
        or display != season_id
        or prior_index[0]["season_status"] != "active"
    )
    active_evidence = SeasonResolution(
        season_id, season_id, ResolutionStatus.ACTIVE, None
    )
    if display == season_id:
        _require(latest is not None, "Prior index points to missing season")
    else:
        _require(latest is None, "Cannot reactivate an archived season")
        if display is not None:
            display_latest, display_entries, _ = _read_season(
                prior.root, display, candidate_id=""
            )
            _require(display_latest is not None, "Prior index points to missing season")
            _require(
                season.start_date
                > max(
                    _timestamp(entry["data_cutoff_at"]).date()
                    for entry in display_entries
                ),
                "Season transition would regress the reviewed calendar",
            )
    if prior_index is not None and index_changed:
        _require(
            resolution == active_evidence,
            "Season transition lacks current reviewed resolution",
        )

    if entries:
        _require(
            prepared.cutoff
            >= max(_timestamp(entry["data_cutoff_at"]) for entry in entries),
            "New forecast predates prior data cutoff",
        )
    if latest is not None and latest["input_hash"] == digest:
        if not index_changed:
            prior.validate()
            yield None
            return
        manifest = latest
        artifacts = []
    elif archived is None:
        _require(
            _read_document(prior.root, new_file) is None,
            "Immutable snapshot already exists",
        )
        result = run_trials()
        if isinstance(result, SimulationBlocked):
            raise RuntimeError("Simulation is blocked")
        _verify_simulation(prepared, result)
        inputs = SnapshotInputs(prepared.snapshot, result)
        document = _snapshot_document(inputs, snapshot_id, generated_at)
        manifest = LatestManifest(
            schema_version=1,
            season_id=season_id,
            snapshot_id=snapshot_id,
            snapshot_path=new_file,
            input_hash=digest,
            model_version=document["model_version"],
            generated_at=document["generated_at"],
            data_cutoff_at=document["data_cutoff_at"],
        )
        validate_manifest_snapshot(manifest, document)
        record = HistoryEntry(
            snapshot_id=snapshot_id,
            generated_at=document["generated_at"],
            data_cutoff_at=document["data_cutoff_at"],
            path=new_file,
            matchweek_label=matchweek_label,
        )
        artifacts = [
            (
                new_file,
                serialize_document("snapshot", document, snapshot_inputs=inputs),
            ),
            (
                history_path(season_id),
                serialize_document(
                    "history",
                    HistoryIndex(
                        schema_version=1,
                        season_id=season_id,
                        snapshots=[*entries, record],
                    ),
                ),
            ),
        ]
    else:
        archive = prior.archived_snapshot
        receipt = None if archive is None else archive(season_id, snapshot_id)
        document, contents = archived
        _require(
            receipt is not None
            and receipt.manifest["input_hash"] == digest
            and receipt.snapshot_sha256 == sha256(contents).hexdigest(),
            "Cannot verify archived publication identity and bytes",
        )
        assert receipt is not None
        manifest = receipt.manifest
        validate_manifest_snapshot(manifest, document)
        artifacts = []

    if index_changed:
        index = SeasonIndex(
            schema_version=1,
            active_season_id=season_id,
            display_season_id=season_id,
            season_status="active",
            latest_manifest_path=latest_file,
        )
        artifacts.append((DATA_INDEX_PATH, serialize_document("season_index", index)))
    artifacts.append((latest_file, serialize_document("latest", manifest)))
    with TemporaryDirectory(prefix="pl-intel-publication-") as directory:
        stage = Path(directory)
        for path, contents in artifacts:
            target = stage / path.removeprefix("/data/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        prior.validate()
        yield StagedPublication(stage, prior.revision)
