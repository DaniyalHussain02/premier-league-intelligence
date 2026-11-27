"""Weekly workflow's synthetic season and local atomic publication checks."""

import json
from datetime import timedelta
from pathlib import Path
from runpy import run_path

import pytest

from pl_intel.cli import _fixture_repository, main, prepare_snapshot
from pl_intel.season import ResolutionStatus, SeasonCandidate, resolve_active_season
from pl_intel.simulate import SimulationConfig


@pytest.fixture(scope="module")
def inputs():
    root = Path(__file__).parents[1]
    model = run_path(str(root / "unit" / "test_simulate.py"))
    publication = run_path(str(root / "integration" / "test_publish.py"))
    season, history = model["season_input"]()
    changed, corrected = publication["_change_score"](season, history)
    return model["CUTOFF"], (season, history), (changed, corrected)


def _prepare(prior, inputs, cutoff):
    snapshot, _ = inputs
    resolution = resolve_active_season(
        (
            SeasonCandidate(
                "PL",
                "synthetic-season",
                snapshot.season,
                snapshot.teams,
                tuple(item.fixture for item in snapshot.fixtures),
                snapshot.rescheduling_approvals,
            ),
        ),
        as_of=cutoff.date(),
    )
    assert resolution.status is ResolutionStatus.ACTIVE
    assert resolution.active_season_id == snapshot.season.season_id
    return prepare_snapshot(
        prior,
        lambda: inputs,
        cutoff=cutoff,
        generated_at=cutoff + timedelta(minutes=1),
        matchweek_label="Fixture check",
        resolution=resolution,
        simulation=SimulationConfig(trials=2, batch_size=1, seed=12),
    )


def test_changed_no_change_and_git_archive_reversion(inputs):
    cutoff, original, changed = inputs
    with _fixture_repository() as repo:
        base = repo.revision()
        with repo.prior() as prior, _prepare(prior, original, cutoff) as stage:
            first = repo.apply(stage)
        assert first != base
        assert repo._git("rev-parse", f"{first}^").decode().strip() == base
        old_tree = repo._files(first)
        assert len(old_tree) == 4
        assert all(path.endswith(".json") for path in old_tree)

        with repo.prior() as prior, _prepare(prior, original, cutoff) as stage:
            assert stage is None
        assert repo.revision() == first

        with (
            repo.prior() as prior,
            _prepare(prior, changed, cutoff + timedelta(days=1)) as stage,
        ):
            second = repo.apply(stage)
        assert repo._git("rev-parse", f"{second}^").decode().strip() == first
        second_tree = repo._files(second)
        assert len(second_tree) == 5

        with (
            repo.prior() as prior,
            _prepare(prior, original, cutoff + timedelta(days=2)) as stage,
        ):
            reverted = repo.apply(stage)
        latest = next(path for path in old_tree if path.endswith("/latest.json"))
        reverted_tree = repo._files(reverted)
        assert reverted_tree[latest] == old_tree[latest]
        assert all(
            reverted_tree[key] == oid
            for key, oid in second_tree.items()
            if key != latest
        )
        manifest = json.loads(repo._blob(reverted_tree[latest]))
        assert manifest["data_cutoff_at"] == cutoff.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("failure", ["commit-tree", "concurrent-ref"])
def test_failure_preserves_prior_publication(inputs, monkeypatch, failure):
    cutoff, original, changed = inputs
    with _fixture_repository() as repo:
        with repo.prior() as prior, _prepare(prior, original, cutoff) as stage:
            first = repo.apply(stage)
        before = repo._files(first)

        with (
            repo.prior() as prior,
            _prepare(prior, changed, cutoff + timedelta(days=1)) as stage,
        ):
            original_git = repo._git
            destination = "refs/heads/fixture-publication"
            concurrent = None

            def interrupted(*args, **kwargs):
                nonlocal concurrent
                if failure == "commit-tree" and args[0] == "commit-tree":
                    raise RuntimeError("injected commit failure")
                if failure == "concurrent-ref" and args[0] == "update-ref":
                    tree = (
                        original_git("rev-parse", f"{first}^{{tree}}").decode().strip()
                    )
                    concurrent = (
                        original_git(
                            "commit-tree",
                            tree,
                            "-p",
                            first,
                            "-m",
                            "Concurrent fixture writer",
                        )
                        .decode()
                        .strip()
                    )
                    original_git("update-ref", destination, concurrent, first)
                return original_git(*args, **kwargs)

            monkeypatch.setattr(repo, "_git", interrupted)
            with pytest.raises(RuntimeError, match="commit failure|update-ref"):
                repo.apply(stage)
            assert repo.revision() == (concurrent or first)
            assert repo._files(repo.revision()) == before


def test_production_entrypoint_remains_blocked(capsys):
    assert main() == 1
    assert "Production publication is BLOCKED" in capsys.readouterr().out


def test_rejects_partial_staged_patch_before_ref_update(inputs):
    cutoff, original, _ = inputs
    with _fixture_repository() as repo:
        base = repo.revision()
        with repo.prior() as prior, _prepare(prior, original, cutoff) as stage:
            next(stage.directory.glob("seasons/*/history.json")).unlink()
            with pytest.raises(ValueError, match="requires a staged history"):
                repo.apply(stage)
        assert repo.revision() == base
        assert repo._files(base) == {}
