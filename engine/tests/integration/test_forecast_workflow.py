"""Weekly workflow's synthetic season and local atomic publication checks."""

import json
import re
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


@pytest.fixture
def published_repository(inputs):
    cutoff, original, _ = inputs
    with _fixture_repository() as repo:
        with repo.prior() as prior, _prepare(prior, original, cutoff) as stage:
            repo.apply(stage)
        yield repo


def _replace_tree_entry(repo, path, mode, oid):
    """Inject a Git object directly to exercise rejection of invalid trees."""
    revision = repo.revision()
    index = repo.directory / "injected-index"
    repo._git("read-tree", revision, index=index)
    repo._git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{oid},frontend/public/data/{path}",
        index=index,
    )
    tree = repo._git("write-tree", index=index).decode().strip()
    commit = repo._git(
        "commit-tree", tree, "-p", revision, "-m", "Inject invalid fixture entry"
    )
    repo._git(
        "update-ref",
        "refs/heads/fixture-publication",
        commit.decode().strip(),
        revision,
    )


@pytest.mark.parametrize("evidence", ["tampered-snapshot", "invalid-original-manifest"])
def test_rejects_invalid_archived_evidence(
    inputs, published_repository, monkeypatch, evidence
):
    repo = published_repository
    cutoff, original, changed = inputs
    first_files = repo._files(repo.revision())
    latest = next(path for path in first_files if path.endswith("/latest.json"))
    archived_path = next(path for path in first_files if "/snapshots/" in path)
    with (
        repo.prior() as prior,
        _prepare(prior, changed, cutoff + timedelta(days=1)) as stage,
    ):
        repo.apply(stage)

    if evidence == "tampered-snapshot":
        snapshot = json.loads(repo._blob(first_files[archived_path]))
        snapshot["teams"][0]["name"] = "Tampered fixture club"
        blob = repo._git(
            "hash-object", "-w", "--stdin", data=json.dumps(snapshot).encode()
        )
        _replace_tree_entry(repo, archived_path, "100644", blob.decode().strip())
        reason = "Cannot verify archived publication identity and bytes"
    else:
        read_blob = repo._blob
        original_manifest = first_files[latest]

        def invalid_archive_manifest(oid, **kwargs):
            contents = read_blob(oid, **kwargs)
            if oid == original_manifest:
                manifest = json.loads(contents)
                manifest["generated_at"] = "2027-05-02T00:00:00Z"
                return json.dumps(manifest).encode()
            return contents

        monkeypatch.setattr(repo, "_blob", invalid_archive_manifest)
        reason = "Manifest and snapshot metadata differ"

    before = repo.revision()
    tree_before = repo._git("ls-tree", "-r", "-z", before)
    with (
        repo.prior() as prior,
        pytest.raises(ValueError, match=reason),
        _prepare(prior, original, cutoff + timedelta(days=2)),
    ):
        pytest.fail(
            "Invalid archived evidence must block reversion before a patch is staged"
        )
    assert repo.revision() == before
    assert repo._git("ls-tree", "-r", "-z", before) == tree_before


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("unexpected-file", "Unexpected staged publication file"),
        ("traversal", "Invalid snapshot path"),
        ("symlink", "Invalid staged publication link"),
    ],
)
def test_rejects_unsafe_staged_patch(
    inputs, published_repository, tmp_path, mutation, reason
):
    repo = published_repository
    cutoff, _, changed = inputs
    before = repo.revision()
    before_files = repo._files(before)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}")
    with (
        repo.prior() as prior,
        _prepare(prior, changed, cutoff + timedelta(days=1)) as stage,
    ):
        if mutation == "unexpected-file":
            (stage.directory / "unexpected.json").write_bytes(b"{}")
        elif mutation == "symlink":
            (stage.directory / "linked.json").symlink_to(outside)
        else:
            path = next(stage.directory.glob("seasons/*/latest.json"))
            manifest = json.loads(path.read_bytes())
            manifest["snapshot_path"] = "/data/../outside.json"
            path.write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match=reason):
            repo.apply(stage)
    assert repo.revision() == before
    assert repo._files(before) == before_files
    assert outside.read_bytes() == b"{}"


@pytest.mark.parametrize("mode", ["100755", "120000", "160000"])
def test_rejects_executable_link_and_gitlink_tree_entries(published_repository, mode):
    repo = published_repository
    revision = repo.revision()
    normal_files = repo._files(revision)
    path = "index.json"
    oid = revision if mode == "160000" else normal_files[path]
    _replace_tree_entry(repo, path, mode, oid)
    before = repo.revision()
    tree_before = repo._git("ls-tree", "-r", "-z", before)
    assert f"{mode} ".encode() in tree_before
    with (
        pytest.raises(ValueError, match="Invalid fixture publication tree entry"),
        repo.prior(),
    ):
        pytest.fail("Non-regular publication Git objects must not be materialized")
    assert repo.revision() == before
    assert repo._git("ls-tree", "-r", "-z", before) == tree_before


def test_forecast_workflow_remains_read_only_and_fixture_only():
    """Target the current workflow's policy with line checks, rather than parse YAML."""
    workflow = (
        Path(__file__).parents[3] / ".github/workflows/forecast.yml"
    ).read_text()
    assert re.search(
        r"(?m)^on:\n  schedule:\n    - cron: '[0-5]?\d (?:[01]?\d|2[0-3]) \* \* [0-6]'\n  workflow_dispatch:\s*$",
        workflow,
    )
    assert re.findall(r"(?m)^( *)permissions:(.*)$", workflow) == [("", "")]
    assert re.search(r"(?m)^permissions:\n  contents: read\n(?=\n|\S)", workflow)
    assert re.search(
        r"(?m)^concurrency:\n  group: forecast-publish\n  cancel-in-progress: false\s*$",
        workflow,
    )
    assert re.findall(r"(?m)^    runs-on: (.+)$", workflow) == ["ubuntu-24.04"]
    timeouts = re.findall(r"(?m)^    timeout-minutes: (\d+)$", workflow)
    assert len(timeouts) == 1 and 1 <= int(timeouts[0]) <= 30
    actions = re.findall(r"(?m)^\s*(?:- )?uses:\s*(\S+)", workflow)
    assert len(actions) == 2
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", actions[0])
    assert re.fullmatch(r"actions/setup-python@[0-9a-f]{40}", actions[1])
    assert re.findall(r"(?m)^\s+persist-credentials: (\S+)$", workflow) == ["false"]
    assert not re.search(
        r"(?im)\b(?:secrets|env|token|credentials|password)\s*[:.]",
        workflow.replace("persist-credentials: false", ""),
    )
    commands = re.findall(r"(?m)^\s*(?:- )?run:\s*(.*)$", workflow)
    assert [command.strip("'\"") for command in commands] == [
        "python -m pip install --require-hashes --only-binary=:all: -r engine/requirements.lock",
        "python -m pip install --no-build-isolation --no-deps -e ./engine",
        "python -m pytest engine/tests/integration/test_forecast_workflow.py -q",
    ]
