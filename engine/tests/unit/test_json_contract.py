import json
from dataclasses import replace
from functools import cache
from math import fsum

import pytest
from test_backtest import _CONFIG, synthetic_study
from test_simulate import CUTOFF, season_input

from pl_intel import publish
from pl_intel.assignment import assign_representative_table
from pl_intel.backtest import BacktestReport, StudyKind, run_backtest
from pl_intel.publish import (
    SnapshotInputs,
    backtest_path,
    history_path,
    latest_path,
    serialize_backtest_summary,
    serialize_document,
    snapshot_path,
    validate_manifest_snapshot,
)
from pl_intel.ranking import (
    AdjustmentEvidence,
    AdjustmentStatus,
    PointsAdjustment,
    calculate_standings,
)
from pl_intel.schemas import BacktestSummary, FixtureStatus
from pl_intel.simulate import SimulationConfig, SimulationResult, simulate_season

_SEASON = "2026-2027"
_SNAPSHOT = "forecast-01"
_GENERATED = "2027-05-01T00:00:00.000001Z"
_CUTOFF = "2027-05-01T00:00:00Z"


@cache
def _snapshot_inputs(*, shared_rank=False):
    season, history = season_input(
        shared_rank=shared_rank,
        unfinished=None if shared_rank else FixtureStatus.SCHEDULED,
    )
    season = replace(
        season,
        adjustments=AdjustmentEvidence(
            _SEASON,
            AdjustmentStatus.VERIFIED_ADJUSTMENTS,
            "synthetic-decision",
            (PointsAdjustment(_SEASON, "club-00", -1, "synthetic-decision"),),
        ),
    )
    simulation = simulate_season(
        season, history, cutoff=CUTOFF, config=SimulationConfig(7, 2, 12)
    )
    assert isinstance(simulation, SimulationResult)
    return SnapshotInputs(season, simulation)


def forecast_document(inputs=None):
    inputs = inputs or _snapshot_inputs()
    season = inputs.season
    standings = {
        row.team_id: row
        for row in calculate_standings(
            season.season,
            season.teams,
            (item.fixture for item in season.fixtures),
            adjustments=season.adjustments,
            rules=season.rules,
        ).rows
    }
    assignment = {
        row.team_id: row.predicted_position
        for row in assign_representative_table(inputs.simulation).rows
    }
    teams = []
    for i, club in enumerate(inputs.simulation.clubs):
        row = list(club.positions)
        official = standings[club.team_id]
        teams.append(
            {
                "team_id": f"club-{i:02}",
                "name": f"Club {i}",
                "actual_rank": official.rank,
                "played": official.played,
                "actual_points": club.actual_adjusted_points,
                "expected_points": club.expected_adjusted_points,
                "predicted_rank": assignment[club.team_id],
                "title_probability": club.title_probability,
                "relegation_probability": club.relegation_probability,
                "position_probabilities": row,
            }
        )
    return {
        "schema_version": 1,
        "season_id": _SEASON,
        "snapshot_id": _SNAPSHOT,
        "generated_at": _GENERATED,
        "data_cutoff_at": _CUTOFF,
        "model_version": inputs.simulation.provenance.model_version,
        "simulation_count": 7,
        "representative_table_method": "maximum-weight-position-assignment",
        "teams": teams,
    }


def latest_document():
    return {
        key: value
        for key, value in forecast_document().items()
        if key not in {"teams", "simulation_count", "representative_table_method"}
    } | {"input_hash": "a" * 64, "snapshot_path": snapshot_path(_SEASON, _SNAPSHOT)}


def history_document():
    return {
        "schema_version": 1,
        "season_id": _SEASON,
        "snapshots": [
            {
                "snapshot_id": _SNAPSHOT,
                "generated_at": _GENERATED,
                "data_cutoff_at": _CUTOFF,
                "matchweek_label": "Matchweek 10",
                "path": snapshot_path(_SEASON, _SNAPSHOT),
            }
        ],
    }


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        (
            "season_index",
            {
                "schema_version": 1,
                "active_season_id": None,
                "display_season_id": _SEASON,
                "season_status": "pending",
                "latest_manifest_path": latest_path(_SEASON),
            },
        ),
        ("latest", latest_document()),
        ("snapshot", forecast_document()),
        ("history", history_document()),
        (
            "backtest_index",
            {
                "schema_version": 1,
                "reports": [
                    {
                        "backtest_id": "reviewed-01",
                        "season_id": _SEASON,
                        "model_version": "poisson-elo-v1",
                        "path": backtest_path("reviewed-01"),
                    }
                ],
            },
        ),
    ],
)
def test_v1_documents_round_trip_with_stable_bytes(kind, document):
    options = {"snapshot_inputs": _snapshot_inputs()} if kind == "snapshot" else {}
    serialized = serialize_document(kind, document, **options)
    assert json.loads(serialized) == document
    assert (
        serialize_document(kind, dict(reversed(tuple(document.items()))), **options)
        == serialized
    )


def test_absent_display_season_and_empty_indexes():
    serialize_document(
        "season_index",
        {
            "schema_version": 1,
            "active_season_id": None,
            "display_season_id": None,
            "season_status": "blocked",
            "latest_manifest_path": None,
        },
    )
    serialize_document("backtest_index", {"schema_version": 1, "reports": []})
    serialize_document(
        "history", {"schema_version": 1, "season_id": _SEASON, "snapshots": []}
    )


@pytest.mark.parametrize(
    "bad", ["../secret", "https://elsewhere.test/a", "%2e%2e", "club/1", "one\\two"]
)
def test_paths_derive_only_from_canonical_ids(bad):
    with pytest.raises(ValueError):
        snapshot_path(_SEASON, bad)
    with pytest.raises(ValueError):
        latest_path(bad)
    with pytest.raises(ValueError):
        history_path(bad)
    with pytest.raises(ValueError):
        backtest_path(bad)


@pytest.mark.parametrize(
    "path",
    [
        "/data/seasons/other/snapshots/forecast-01.json",
        "//elsewhere.test/a",
        "/data/%2e%2e",
    ],
)
def test_latest_rejects_unsafe_or_mismatched_path(path):
    with pytest.raises(ValueError, match="snapshot path"):
        serialize_document("latest", latest_document() | {"snapshot_path": path})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("generated_at", "2026-02-30T01:00:00Z"),
        ("generated_at", "2026-11-24T12:00:00+01:00"),
        ("data_cutoff_at", "2027-05-01T00:00:00.000002Z"),
        ("simulation_count", 0),
        ("simulation_count", True),
        ("input_hash", "internal"),
    ],
)
def test_invalid_snapshot_header(key, value):
    with pytest.raises(ValueError):
        serialize_document("snapshot", forecast_document() | {key: value})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"team_id": "club-01"}, "Duplicate team ID"),
        ({"predicted_rank": 2}, "Duplicate predicted rank"),
        ({"actual_rank": 3}, "Invalid shared official ranks"),
        ({"expected_points": float("nan")}, "Invalid public number"),
        ({"expected_points": float("inf")}, "Invalid public number"),
        ({"expected_points": 9}, "Invalid public number"),
        ({"played": 39}, "Invalid public integer"),
        ({"name": " club "}, "Invalid public text"),
        ({"position_probabilities": [1.0]}, "Expected 20 position probabilities"),
        ({"position_probabilities": [0.5] + [0.0] * 19}, "Invalid probability total"),
        ({"title_probability": 0.5}, "Invalid probability total"),
        ({"provider_payload": {}}, "Invalid public document fields"),
    ],
)
def test_rejects_invalid_team_fields(changes, message):
    document = forecast_document()
    document["teams"][0].update(changes)
    with pytest.raises(ValueError, match=message):
        serialize_document("snapshot", document)


def test_rejects_incomplete_roster_and_invalid_matrix_columns():
    document = forecast_document()
    with pytest.raises(ValueError, match="20 forecast teams"):
        serialize_document("snapshot", document | {"teams": document["teams"][:-1]})

    # Leave each row, title total, and relegation total valid but break occupancy.
    document["teams"][1]["position_probabilities"] = document["teams"][2][
        "position_probabilities"
    ]
    with pytest.raises(ValueError, match="Invalid probability total"):
        serialize_document("snapshot", document)


def test_history_rejects_duplicate_or_unsorted_entries():
    document = history_document()
    entry = document["snapshots"][0]
    with pytest.raises(ValueError, match="Duplicate history snapshot"):
        serialize_document("history", document | {"snapshots": [entry, entry]})
    older = entry | {
        "snapshot_id": "older",
        "path": snapshot_path(_SEASON, "older"),
        "generated_at": _CUTOFF,
    }
    with pytest.raises(ValueError, match="chronological"):
        serialize_document("history", document | {"snapshots": [entry, older]})


@pytest.fixture(scope="module")
def synthetic_report():
    result = run_backtest(synthetic_study(), config=_CONFIG)
    assert isinstance(result, BacktestReport)
    return result


def test_synthetic_backtest_is_ineligible_even_with_review_callbacks():
    with pytest.raises(ValueError, match="verified historical evidence"):
        serialize_backtest_summary(
            synthetic_study(),
            config=_CONFIG,
            verify_evidence=lambda *_: True,
            review_redistribution=lambda *_: True,
        )


def test_review_rejects_relabelled_synthetic_report(synthetic_report):
    relabelled = replace(synthetic_report, kind=StudyKind.VERIFIED_HISTORICAL)
    with pytest.raises(ValueError, match="verified historical evidence"):
        serialize_backtest_summary(relabelled, config=_CONFIG)


@pytest.mark.parametrize("verify", [None, lambda *_: False])
def test_backtest_requires_t11_authentication(verify):
    dataset = replace(synthetic_study(), kind=StudyKind.VERIFIED_HISTORICAL)
    with pytest.raises(
        ValueError, match="verified (historical evidence|retrospective report)"
    ):
        serialize_backtest_summary(
            dataset,
            config=_CONFIG,
            verify_evidence=verify,
            review_redistribution=lambda *_: True,
        )


@pytest.mark.parametrize("review", [None, lambda *_: False])
def test_backtest_requires_separate_redistribution_review(review):
    dataset = replace(synthetic_study(), kind=StudyKind.VERIFIED_HISTORICAL)
    with pytest.raises(ValueError, match="redistribution review"):
        serialize_backtest_summary(
            dataset,
            config=_CONFIG,
            verify_evidence=lambda *_: True,
            review_redistribution=review,
        )


def test_backtest_projection_uses_only_controlled_public_fields(monkeypatch):
    dataset = replace(synthetic_study(), kind=StudyKind.VERIFIED_HISTORICAL)
    calls = []

    def verify(candidate, config):
        assert candidate is dataset and config is _CONFIG
        calls.append("verification")
        return True

    def review(candidate, config):
        assert candidate is dataset and config is _CONFIG
        calls.append("redistribution")
        return True

    def run_with_internal_note(*args, **kwargs):
        report = run_backtest(*args, **kwargs)
        assert isinstance(report, BacktestReport)
        return replace(report, limitations=("PRIVATE-EVIDENCE-TEST-MARKER",))

    monkeypatch.setattr(publish, "run_backtest", run_with_internal_note)
    payload = serialize_backtest_summary(
        dataset, config=_CONFIG, verify_evidence=verify, review_redistribution=review
    )
    public = json.loads(payload)
    assert calls == ["redistribution", "verification"]
    assert public.keys() == BacktestSummary.__required_keys__
    assert public["sample_size"] == 380
    assert b"synthetic-reviewed" not in payload
    assert b"PRIVATE-EVIDENCE-TEST-MARKER" not in payload


def test_review_rejects_odd_team_games():
    document = forecast_document()
    document["teams"][5]["played"] += 1
    with pytest.raises(ValueError, match="team games"):
        serialize_document("snapshot", document)


def test_review_rejects_bom_whitespace():
    document = forecast_document()
    document["teams"][0]["name"] = "\ufeffClub"
    with pytest.raises(ValueError, match="Invalid public text"):
        serialize_document("snapshot", document)


def test_snapshot_requires_complete_unique_canonical_inventory():
    inputs = _snapshot_inputs()
    fixtures = inputs.season.fixtures
    for candidate, reason in (
        (fixtures[:-1], "380 canonical fixtures"),
        ((*fixtures[:-1], fixtures[0]), "duplicate season fixture pairing"),
    ):
        broken = replace(inputs, season=replace(inputs.season, fixtures=candidate))
        with pytest.raises(ValueError, match=reason):
            serialize_document("snapshot", forecast_document(), snapshot_inputs=broken)
    with pytest.raises(ValueError, match="requires canonical inventory"):
        serialize_document("snapshot", forecast_document())


def test_snapshot_reconciles_even_played_counts_and_approved_names():
    inputs = _snapshot_inputs()
    document = forecast_document()
    serialize_document("snapshot", document, snapshot_inputs=inputs)
    document["teams"][5]["played"] = document["teams"][10]["played"] = 38
    with pytest.raises(ValueError, match="Played count"):
        serialize_document("snapshot", document, snapshot_inputs=inputs)
    document = forecast_document()
    document["teams"][0]["name"] = "Unapproved club label"
    with pytest.raises(ValueError, match="approved canonical club name"):
        serialize_document("snapshot", document, snapshot_inputs=inputs)


@pytest.mark.parametrize("field", ["actual_points", "expected_points"])
def test_snapshot_preserves_adjusted_points_with_a_deduction(field):
    inputs = _snapshot_inputs()
    doc = forecast_document()
    club = inputs.simulation.clubs[0]
    assert club.actual_earned_points == club.actual_adjusted_points + 1
    public = json.loads(serialize_document("snapshot", doc, snapshot_inputs=inputs))
    assert public["teams"][0]["actual_points"] == club.actual_adjusted_points
    assert public["teams"][0]["expected_points"] == club.expected_adjusted_points
    doc["teams"][0][field] = club.actual_earned_points
    with pytest.raises(ValueError):
        serialize_document("snapshot", doc, snapshot_inputs=inputs)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("season_id", "other-season"),
        ("snapshot_id", "other-snapshot"),
        ("model_version", "other-model"),
        ("generated_at", "2027-05-01T00:00:00.000002Z"),
        ("data_cutoff_at", "2027-04-30T23:59:59.999999Z"),
    ],
)
def test_manifest_snapshot_consistency(key, value):
    snapshot = forecast_document()
    manifest = latest_document()
    validate_manifest_snapshot(manifest, snapshot)
    with pytest.raises(ValueError, match="metadata differ"):
        validate_manifest_snapshot(manifest, snapshot | {key: value})


@pytest.mark.parametrize(("delta", "accepted"), [(5e-13, True), (2e-12, False)])
def test_shared_probability_boundary(delta, accepted):
    document = forecast_document()
    document["teams"][1]["position_probabilities"][1] -= delta
    if accepted:
        validate_manifest_snapshot(latest_document(), document)
    else:
        with pytest.raises(ValueError, match="probability total"):
            validate_manifest_snapshot(latest_document(), document)


def test_rejects_unicode_whitespace_in_public_label():
    for prefix in ("\ufeff", "\u0085", "\u00a0"):
        document = forecast_document()
        document["teams"][0]["name"] = prefix + "Club"
        with pytest.raises(ValueError, match="Invalid public text"):
            serialize_document("snapshot", document)


def test_python_emits_integer_tokens_for_integer_fields():
    document = forecast_document()
    document["simulation_count"] = 7.0
    with pytest.raises(ValueError, match="public integer"):
        serialize_document("snapshot", document, snapshot_inputs=_snapshot_inputs())


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        (
            "season_index",
            {
                "schema_version": 1,
                "active_season_id": None,
                "display_season_id": None,
                "season_status": "pending",
                "latest_manifest_path": None,
            },
        ),
        ("latest", latest_document()),
        ("snapshot", forecast_document()),
        ("history", history_document()),
        ("backtest_index", {"schema_version": 1, "reports": []}),
    ],
)
def test_unknown_top_level_fields_are_rejected(kind, document):
    with pytest.raises(ValueError, match="Invalid public document fields"):
        serialize_document(kind, document | {"evidence_refs": ["private-test-marker"]})


def _change_finished_score(inputs, home, away):
    fixtures = list(inputs.season.fixtures)
    index = next(
        i
        for i, row in enumerate(fixtures)
        if (row.fixture.home_team_id, row.fixture.away_team_id)
        == ("club-00", "club-01")
    )
    original = fixtures[index]
    # Change the score fact as well as the fixture; leave the original
    # simulation intact to exercise the serializer's reconciliation.
    fixtures[index] = replace(
        original,
        fixture=replace(original.fixture, home_goals=home, away_goals=away),
        finished_result=replace(
            original.finished_result, home_goals=home, away_goals=away
        ),
    )
    return replace(inputs, season=replace(inputs.season, fixtures=tuple(fixtures)))


def test_rejects_finished_score_that_changes_current_standings():
    changed = _change_finished_score(_snapshot_inputs(), 0, 3)
    with pytest.raises(ValueError, match="adjusted points|Actual rank"):
        serialize_document("snapshot", forecast_document(), snapshot_inputs=changed)


def test_score_change_preserving_standings_is_outside_provenance_boundary():
    inputs = _snapshot_inputs()
    changed = _change_finished_score(inputs, 3, 0)
    assert changed.season != inputs.season
    document = forecast_document()
    assert serialize_document(
        "snapshot", document, snapshot_inputs=changed
    ) == serialize_document("snapshot", document, snapshot_inputs=inputs)


def test_rejects_changed_deduction_and_blocked_standings():
    inputs = _snapshot_inputs()
    adjustments = inputs.season.adjustments
    for changed, message in (
        (
            replace(
                adjustments,
                adjustments=(replace(adjustments.adjustments[0], points_delta=-2),),
            ),
            "adjusted points",
        ),
        (
            AdjustmentEvidence(_SEASON, AdjustmentStatus.UNKNOWN),
            "Current standings are blocked",
        ),
    ):
        candidate = replace(inputs, season=replace(inputs.season, adjustments=changed))
        with pytest.raises(ValueError, match=message):
            serialize_document(
                "snapshot", forecast_document(), snapshot_inputs=candidate
            )


def test_rejects_unique_but_suboptimal_predicted_rank_swap():
    document = forecast_document()
    first, second = document["teams"][:2]
    first["predicted_rank"], second["predicted_rank"] = (
        second["predicted_rank"],
        first["predicted_rank"],
    )
    with pytest.raises(ValueError, match="representative assignment"):
        serialize_document("snapshot", document, snapshot_inputs=_snapshot_inputs())


def test_rejects_structurally_valid_substitute_matrix():
    document = forecast_document()
    first, second = document["teams"][:2]
    for key in (
        "position_probabilities",
        "title_probability",
        "relegation_probability",
    ):
        first[key], second[key] = second[key], first[key]
    validate_manifest_snapshot(latest_document(), document)
    with pytest.raises(ValueError, match="probabilities do not match"):
        serialize_document("snapshot", document, snapshot_inputs=_snapshot_inputs())


@pytest.mark.parametrize(
    ("field", "index"), [("title_probability", 0), ("relegation_probability", 19)]
)
def test_rejects_probability_that_passes_tolerance_but_differs_from_simulation(
    field, index
):
    document = forecast_document()
    team = document["teams"][index]
    assert team[field] == 1
    team[field] -= 5e-13
    validate_manifest_snapshot(latest_document(), document)
    with pytest.raises(ValueError, match="probabilities do not match"):
        serialize_document("snapshot", document, snapshot_inputs=_snapshot_inputs())


def test_shared_official_ranks_and_exact_predicted_tie_break():
    inputs = _snapshot_inputs(shared_rank=True)
    document = forecast_document(inputs)
    first, second = document["teams"][5:7]
    assert first["actual_rank"] == second["actual_rank"]
    assert first["position_probabilities"] == second["position_probabilities"]
    assert first["predicted_rank"] < second["predicted_rank"]
    serialize_document("snapshot", document, snapshot_inputs=inputs)
    first["predicted_rank"], second["predicted_rank"] = (
        second["predicted_rank"],
        first["predicted_rank"],
    )
    with pytest.raises(ValueError, match="representative assignment"):
        serialize_document("snapshot", document, snapshot_inputs=inputs)


def test_probability_precision_vector_matches_frontend():
    document = forecast_document()
    row = [0.05] * 19 + [0.05000000000099969]
    for i, team in enumerate(document["teams"]):
        values = row if i < 10 else [0.1 - p for p in row]
        team.update(
            position_probabilities=values,
            title_probability=values[0],
            relegation_probability=fsum(values[-3:]),
        )
    validate_manifest_snapshot(latest_document(), document)
