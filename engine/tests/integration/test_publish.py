"""Offline publication staging against a previously materialized data tree."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from runpy import run_path
from shutil import copytree
from unittest.mock import Mock

import pytest

from pl_intel import cli, ranking
from pl_intel.cli import main, prepare_snapshot
from pl_intel.elo import EloConfig
from pl_intel.normalize import canonical_fixture_id
from pl_intel.poisson import PoissonConfig
from pl_intel.publish import (
    ArchivedSnapshot,
    PriorPublication,
    SnapshotInputs,
    _read_document,
    _stage_snapshot,
)
from pl_intel.season import (
    ResolutionStatus,
    SeasonCandidate,
    SeasonResolution,
    resolve_active_season,
)
from pl_intel.simulate import SimulationConfig, SimulationResult, simulate_season

CUTOFF = datetime(2027, 5, 1, tzinfo=UTC)


REVISION = "a" * 40


def _prior(root, **kwargs):
    return PriorPublication(
        root, REVISION, kwargs.pop("current_revision", lambda: REVISION), **kwargs
    )


def _prepare(
    root, season, history, *, cutoff=CUTOFF, batch_size=1, seed=12, trials=2, **options
):
    return prepare_snapshot(
        _prior(root) if isinstance(root, Path) else root,
        lambda: (season, iter(history)),
        cutoff=cutoff,
        generated_at=cutoff + timedelta(minutes=1),
        matchweek_label="Matchweek 38",
        simulation=SimulationConfig(trials=trials, batch_size=batch_size, seed=seed),
        **options,
    )


def _state(root):
    return {
        p.relative_to(root): (sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in root.rglob("*.json")
    }


@pytest.fixture
def published(tmp_path):
    prior = tmp_path / "prior"
    # Reuse the synthetic cohort even when pytest only collects this file.
    fixture_module = Path(__file__).parents[1] / "unit" / "test_simulate.py"
    season, history = run_path(str(fixture_module))["season_input"]()
    with _prepare(prior, season, history) as stage:
        assert stage is not None
        copytree(stage.directory, prior)
    assert not stage.directory.exists()
    return prior, season, history


def _change_score(season, history):
    reviewed = season.fixtures[0]
    fixture = replace(reviewed.fixture, away_goals=reviewed.fixture.away_goals + 1)
    fact = replace(reviewed.finished_result, away_goals=fixture.away_goals)
    changed = replace(reviewed, fixture=fixture, finished_result=fact)
    return (
        replace(season, fixtures=(changed, *season.fixtures[1:])),
        (replace(history[0], fixture=fixture, result=fact), *history[1:]),
    )


def test_no_change_ignores_order_batch_and_later_check(published, monkeypatch):
    prior, season, history = published
    before = _state(prior)
    run = Mock(wraps=cli._run_simulation)
    monkeypatch.setattr(cli, "_run_simulation", run)
    later = CUTOFF + timedelta(days=1)
    season = replace(
        season,
        known_at=later,
        source_revision="poll-two",
        teams=season.teams[::-1],
        fixtures=season.fixtures[::-1],
    )
    with _prepare(prior, season, history[::-1], cutoff=later, batch_size=2) as stage:
        assert stage is None
    run.assert_not_called()
    assert _state(prior) == before


def test_changed_valid_input_stages_append_only_patch(published, monkeypatch):
    prior, season, history = published
    before = _state(prior)
    season, history = _change_score(season, history)
    run = Mock(wraps=cli._run_simulation)
    monkeypatch.setattr(cli, "_run_simulation", run)
    with _prepare(prior, season, history) as stage:
        assert stage is not None
        assert stage.expected_revision == REVISION
        stage = stage.directory
        assert len(_state(stage)) == 3
        path = stage / "seasons" / season.season.season_id
        manifest = json.loads((path / "latest.json").read_bytes())
        latest = stage / manifest["snapshot_path"].removeprefix("/data/")
        assert latest.relative_to(stage) not in before
        snapshot = json.loads(latest.read_bytes())
        original = next((prior / latest.relative_to(stage).parent).glob("*.json"))
        old = json.loads(original.read_bytes())
        assert snapshot["teams"] == old["teams"]
        assert len(json.loads((path / "history.json").read_bytes())["snapshots"]) == 2
        for public in stage.rglob("*.json"):
            assert b"synthetic-score" not in public.read_bytes()
            assert b"provider_fixture_id" not in public.read_bytes()
    run.assert_called_once()
    assert _state(prior) == before


@pytest.mark.parametrize("failure", ["fetch", "validation", "simulation", "write"])
def test_failure_preserves_prior_files(published, monkeypatch, failure):
    prior, season, history = published
    before = _state(prior)
    changed, corrected_history = _change_score(season, history)
    failed_targets = []
    if failure == "fetch":

        def load():
            raise OSError("fetch unavailable")

        context = prepare_snapshot(
            _prior(prior),
            load,
            cutoff=CUTOFF,
            generated_at=CUTOFF,
            matchweek_label="Test",
        )
        error = OSError
    elif failure == "validation":
        context = _prepare(
            prior, replace(season, fixtures=season.fixtures[:-1]), history
        )
        error = ValueError
    elif failure == "simulation":
        from pl_intel.simulate import SimulationBlocked

        monkeypatch.setattr(
            cli, "_run_simulation", lambda _: SimulationBlocked(("test",))
        )
        context = _prepare(prior, changed, corrected_history)
        error = RuntimeError
    else:
        original_write = Path.write_bytes

        def fail_manifest(self, contents):
            failed_targets.append(self)
            if self.name == "latest.json":
                raise OSError("injected final staging write failure")
            return original_write(self, contents)

        monkeypatch.setattr(Path, "write_bytes", fail_manifest)
        context = _prepare(prior, changed, corrected_history)
        error = OSError
    with pytest.raises(error), context:
        pytest.fail("A failed publication must never hand off a patch")
    assert all(not path.exists() for path in failed_targets)
    assert _state(prior) == before


def test_immutable_snapshot_conflict_preserves_prior_files(published):
    prior, season, history = published
    season, history = _change_score(season, history)
    with _prepare(prior, season, history) as stage:
        stage = stage.directory
        new = next(stage.glob("seasons/*/snapshots/*.json"))
        collision = prior / new.relative_to(stage)
        collision.write_bytes(b"{}")
    before = _state(prior)
    with (
        pytest.raises(ValueError, match="Immutable snapshot"),
        _prepare(prior, season, history),
    ):
        pytest.fail("An immutable snapshot must not be overwritten")
    assert _state(prior) == before


def test_production_command_fails_closed(capsys):
    assert main() == 1
    assert "BLOCKED" in capsys.readouterr().out


def _manifest(root):
    return next(root.glob("seasons/*/latest.json"))


def test_direct_staging_verifies_exact_inputs_before_noop(published, tmp_path):
    prior, season, history = published
    result = simulate_season(
        season, history, cutoff=CUTOFF, config=SimulationConfig(2, 1, 12)
    )
    assert isinstance(result, SimulationResult)

    def stage(snapshot, records, root=prior):
        return _stage_snapshot(
            _prior(root),
            SnapshotInputs(snapshot, result),
            records,
            generated_at=CUTOFF,
            matchweek_label="Test",
        )

    with stage(season, history, tmp_path / "unpublished") as candidate:
        assert candidate is not None
        assert _manifest(candidate.directory).is_file()
    with stage(season, history) as candidate:
        assert candidate is None
    changed, changed_history = _change_score(season, history)
    with pytest.raises(ValueError, match="provenance"), stage(changed, changed_history):
        pytest.fail("A matching points table is insufficient")
    with pytest.raises(RuntimeError, match="blocked"), stage(changed, history):
        pytest.fail("An inconsistent score and history cannot be staged")
    # Evidence substitutions change the exact source digest without changing
    # the semantic publication hash. This must fail before the no-op check.
    substituted = (
        replace(history[0], identity_source_ref="different-review"),
        *history[1:],
    )
    revision = replace(season.fixtures[0], result_identity_ref="different-review")
    revised_season = replace(season, fixtures=(revision, *season.fixtures[1:]))
    with (
        pytest.raises(ValueError, match="provenance"),
        stage(revised_season, substituted),
    ):
        pytest.fail("The original run did not consume this exact history")


@pytest.mark.parametrize("revision", ["b" * 40, REVISION])
def test_stale_revision_early_or_at_handoff(published, monkeypatch, revision):
    prior, season, history = published
    season, history = _change_score(season, history)
    before = _state(prior)
    head = [revision]
    original = Path.write_bytes
    staged = []

    def advance_on_write(path, contents):
        staged.append(path)
        head[0] = "b" * 40
        return original(path, contents)

    monkeypatch.setattr(Path, "write_bytes", advance_on_write)
    with (
        pytest.raises(ValueError, match="Stale prior revision"),
        _prepare(_prior(prior, current_revision=lambda: head[0]), season, history),
    ):
        pytest.fail("Stale staging cannot be handed off")
    assert all(not path.exists() for path in staged)
    assert _state(prior) == before


@pytest.mark.parametrize(
    "invalid", ["malformed", "different-season", "missing", "duplicate", "nan"]
)
def test_invalid_global_index_blocks_noop(published, invalid):
    prior, season, history = published
    path = prior / "index.json"
    index = json.loads(path.read_bytes())
    if invalid == "malformed":
        path.write_text("{")
    elif invalid == "different-season":
        index.update(
            active_season_id="other",
            display_season_id="other",
            latest_manifest_path="/data/seasons/other/latest.json",
        )
        path.write_text(json.dumps(index))
    elif invalid == "missing":
        path.unlink()
    elif invalid == "duplicate":
        path.write_text('{"schema_version":1,' + path.read_text()[1:])
    else:
        path.write_text(
            path.read_text().replace('"schema_version":1', '"schema_version":NaN')
        )
    with pytest.raises(ValueError), _prepare(prior, season, history):
        pytest.fail("Invalid global index cannot yield a no-op")


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/outside.json",
        "/data/../outside.json",
        "index.json",
        "/data//index.json",
        "/data/%2e%2e/index.json",
    ],
)
def test_invalid_prior_document_paths(tmp_path, path):
    with pytest.raises(ValueError, match="Invalid prior document path"):
        _read_document(tmp_path, path)


@pytest.mark.parametrize("component", ["file", "directory"])
def test_prior_document_rejects_links(tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.json").write_text("{}")
    root = tmp_path / "root"
    root.mkdir()
    if component == "file":
        (root / "index.json").symlink_to(outside / "index.json")
        path = "/data/index.json"
    else:
        (root / "seasons").symlink_to(outside)
        path = "/data/seasons/index.json"
    with pytest.raises(ValueError, match="Unexpected link"):
        _read_document(root, path)


def _publish_second(prior, season, history):
    path = _manifest(prior)
    manifest = json.loads(path.read_bytes())
    target = prior / manifest["snapshot_path"].removeprefix("/data/")
    receipt = ArchivedSnapshot(manifest, sha256(target.read_bytes()).hexdigest())
    changed, changed_history = _change_score(season, history)
    with _prepare(
        prior, changed, changed_history, cutoff=CUTOFF + timedelta(days=1)
    ) as stage:
        copytree(stage.directory, prior, dirs_exist_ok=True)
    return receipt, target


@pytest.mark.parametrize(
    "evidence", ["valid", "missing", "wrong-full-hash", "wrong-bytes"]
)
def test_archived_reversion(published, evidence):
    prior, season, history = published
    receipt, _ = _publish_second(prior, season, history)
    if evidence == "missing":
        receipt = None
    elif evidence == "wrong-full-hash":
        manifest = receipt.manifest.copy()
        digest = manifest["input_hash"]
        manifest["input_hash"] = (
            digest[:24] + ("1" if digest[24] == "0" else "0") + digest[25:]
        )
        receipt = replace(receipt, manifest=manifest)
    elif evidence == "wrong-bytes":
        receipt = replace(receipt, snapshot_sha256="0" * 64)
    before = _state(prior)
    context = _prepare(
        _prior(prior, archived_snapshot=lambda *_: receipt),
        season,
        history,
        cutoff=CUTOFF + timedelta(days=2),
    )
    if evidence == "valid":
        with context as stage:
            files = list(stage.directory.rglob("*.json"))
            assert len(files) == 1
            assert json.loads(files[0].read_bytes()) == receipt.manifest
            copytree(stage.directory, prior, dirs_exist_ok=True)
        before.pop(_manifest(prior).relative_to(prior))
        assert {p: state for p, state in _state(prior).items() if p in before} == before
        with _prepare(
            prior, season, history, cutoff=CUTOFF + timedelta(days=2)
        ) as stage:
            assert stage is None
    else:
        with pytest.raises(ValueError, match="archived publication identity"), context:
            pytest.fail("Unverified archived snapshot cannot become latest")
        assert _state(prior) == before


@pytest.mark.parametrize("corruption", ["missing", "inconsistent"])
def test_validates_nonlatest_history_reference(published, corruption):
    prior, season, history = published
    _, older = _publish_second(prior, season, history)
    if corruption == "missing":
        older.unlink()
    else:
        document = json.loads(older.read_bytes())
        document["generated_at"] = "2027-05-15T00:00:00Z"
        older.write_text(json.dumps(document))
    changed, changed_history = _change_score(season, history)
    with (
        pytest.raises(ValueError, match="Prior history"),
        _prepare(prior, changed, changed_history, cutoff=CUTOFF + timedelta(days=1)),
    ):
        pytest.fail("A no-op must still verify older history references")


def test_reviewed_season_rollover_and_stale_reactivation(published, monkeypatch):
    prior, older, old_history = published
    season_id = "2027-2028"
    year = timedelta(days=365)
    cutoff = CUTOFF + year
    season = replace(
        older.season,
        season_id=season_id,
        start_date=older.season.start_date + year,
        end_date=older.season.end_date + year,
    )
    rules = replace(older.rules, season_id=season_id)
    existing_rules = ranking.verified_ruleset
    monkeypatch.setattr(
        ranking,
        "verified_ruleset",
        lambda sid, version: (
            rules if sid == season_id else existing_rules(sid, version)
        ),
    )
    fixtures = []
    history = []
    for row, match in zip(older.fixtures, old_history, strict=True):
        fixture = replace(
            row.fixture,
            season_id=season_id,
            fixture_id=canonical_fixture_id(
                season_id, row.fixture.home_team_id, row.fixture.away_team_id
            ),
            kickoff_utc=row.fixture.kickoff_utc + year,
            updated_at=row.fixture.updated_at + year,
        )
        fact = replace(
            match.result,
            fixture_id=fixture.fixture_id,
            available_at=match.result.available_at + year,
        )
        fixtures.append(
            replace(
                row, fixture=fixture, finished_result=fact, known_at=row.known_at + year
            )
        )
        history.append(replace(match, fixture=fixture, result=fact))
    new = replace(
        older,
        season=season,
        known_at=cutoff,
        fixtures=tuple(fixtures),
        rules=rules,
        adjustments=replace(older.adjustments, season_id=season_id),
        qualification=replace(older.qualification, season_id=season_id),
    )
    reviewed = resolve_active_season(
        [
            SeasonCandidate(
                "PL",
                "synthetic-rollover",
                season,
                new.teams,
                tuple(row.fixture for row in fixtures),
            )
        ],
        as_of=cutoff.date(),
    )
    assert reviewed == SeasonResolution(
        season_id, season_id, ResolutionStatus.ACTIVE, None
    )
    with (
        pytest.raises(ValueError, match="reviewed resolution"),
        _prepare(prior, new, history, cutoff=cutoff),
    ):
        pytest.fail("Rollover requires reviewed active-season resolution")
    with _prepare(prior, new, history, cutoff=cutoff, resolution=reviewed) as stage:
        copytree(stage.directory, prior, dirs_exist_ok=True)
    assert (
        json.loads((prior / "index.json").read_bytes())["active_season_id"] == season_id
    )
    with (
        pytest.raises(ValueError, match="archived season"),
        _prepare(prior, older, old_history),
    ):
        pytest.fail("Prior seasons may not displace a newer season")


def test_direct_staging_rejects_substituted_prior_season_result(published):
    prior, season, history = published
    original = history[0]
    older_id = "2025-2026"
    delta = timedelta(days=365)
    fixture = replace(
        original.fixture,
        season_id=older_id,
        fixture_id=canonical_fixture_id(
            older_id, original.fixture.home_team_id, original.fixture.away_team_id
        ),
        kickoff_utc=original.fixture.kickoff_utc - delta,
        updated_at=original.fixture.updated_at - delta,
    )
    past_result = replace(
        original.result,
        fixture_id=fixture.fixture_id,
        available_at=original.result.available_at - delta,
    )
    older = replace(original, fixture=fixture, result=past_result)
    run = simulate_season(
        season, (*history, older), cutoff=CUTOFF, config=SimulationConfig(2, 1, 12)
    )
    assert isinstance(run, SimulationResult)
    edited = replace(
        older,
        fixture=replace(fixture, away_goals=fixture.away_goals + 1),
        result=replace(past_result, away_goals=past_result.away_goals + 1),
    )
    with (
        pytest.raises(ValueError, match="provenance"),
        _stage_snapshot(
            _prior(prior),
            SnapshotInputs(season, run),
            (*history, edited),
            generated_at=CUTOFF,
            matchweek_label="Test",
        ),
    ):
        pytest.fail("Staging must bind the eligible earlier-season scores")


@pytest.mark.parametrize(
    "options",
    [
        {"seed": 13},
        {"trials": 3},
        {"elo": EloConfig(k_factor=21)},
        {"poisson": PoissonConfig(baseline_goals=1.4)},
    ],
    ids=["seed", "trials", "elo", "poisson"],
)
def test_model_configuration_changes_publication_identity(published, options):
    prior, season, history = published
    original = json.loads(_manifest(prior).read_bytes())["input_hash"]
    with _prepare(prior, season, history, **options) as stage:
        assert stage is not None
        changed = json.loads(_manifest(stage.directory).read_bytes())
        assert changed["input_hash"] != original


def test_cutoff_equal_history_is_excluded_from_publication_identity(published):
    prior, season, history = published
    extra = replace(history[0], result=replace(history[0].result, available_at=CUTOFF))
    with _prepare(prior, season, (*history, extra)) as stage:
        assert stage is None
