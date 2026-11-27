"""Prepare a staged forecast from one trusted input acquisition and simulation."""

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory

from pl_intel.elo import EloConfig, EloMatch
from pl_intel.poisson import PoissonConfig
from pl_intel.publish import (
    _MAX_DOCUMENT_BYTES,
    ArchivedSnapshot,
    PriorPublication,
    StagedPublication,
    _format_utc,
    _read_document,
    _stage_prepared,
    _validate_document,
    _validated_simulation,
    history_path,
    latest_path,
    snapshot_path,
    validate_manifest_snapshot,
)
from pl_intel.season import SeasonResolution
from pl_intel.simulate import (
    SeasonSnapshot,
    SimulationConfig,
    _prepare_simulation,
    _run_simulation,
)

_DEFAULT_SIMULATION = SimulationConfig()
_DEFAULT_ELO = EloConfig()
_DEFAULT_POISSON = PoissonConfig()


@contextmanager
def prepare_snapshot(
    prior: PriorPublication,
    load: Callable[[], tuple[SeasonSnapshot, Iterable[EloMatch]]],
    *,
    cutoff: datetime,
    generated_at: datetime,
    matchweek_label: str,
    resolution: SeasonResolution | None = None,
    simulation: SimulationConfig = _DEFAULT_SIMULATION,
    elo: EloConfig = _DEFAULT_ELO,
    poisson: PoissonConfig = _DEFAULT_POISSON,
) -> Iterator[StagedPublication | None]:
    """Prepare an offline candidate, with no production write path.

    A trusted caller independently authenticates fixture revisions, result
    facts, and any active-season resolution at the same cutoff. The prepared
    inputs are shared by validation, identity computation, and forecast trials.
    """
    prior.validate()
    _format_utc(cutoff)
    _format_utc(generated_at)
    if generated_at < cutoff:
        raise ValueError("Generation time precedes the data cutoff")
    season, records = load()
    prepared = _validated_simulation(
        _prepare_simulation(
            season,
            records,
            cutoff=cutoff,
            config=simulation,
            elo_config=elo,
            poisson_config=poisson,
        )
    )
    with _stage_prepared(
        prior,
        prepared,
        lambda: _run_simulation(prepared),
        generated_at=generated_at,
        matchweek_label=matchweek_label,
        resolution=resolution,
    ) as stage:
        yield stage


_FIXTURE_REF = "refs/heads/fixture-publication"
_PUBLIC_DATA = "frontend/public/data/"
_MAX_ARCHIVE_COMMITS = 4096
_MAX_TREE_BYTES = 64 * 1024 * 1024
_PUBLIC_PATH = re.compile(
    r"index\.json|seasons/[a-z0-9-]+/(?:latest\.json|history\.json|snapshots/[a-z0-9-]+\.json)"
)


class _FixtureRepository:
    """Isolated Git adapter for synthetic workflow verification only."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.repo = directory / "fixture.git"
        self._git(
            "init", "--bare", "--initial-branch=fixture-publication", str(self.repo)
        )
        tree = self._git("mktree", data=b"").decode().strip()
        revision = self._git("commit-tree", tree, "-m", "Initialize fixture repository")
        self._git("update-ref", _FIXTURE_REF, revision.decode().strip())

    def _git(
        self, *args: str, data: bytes | None = None, index: Path | None = None
    ) -> bytes:
        env = {
            "PATH": os.defpath,
            "HOME": str(self.directory),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Forecast fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Forecast fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        command = ["git"]
        if args[0] != "init":
            command.append(f"--git-dir={self.repo}")
        result = subprocess.run(
            [*command, *args],
            input=data,
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
        if result.returncode:
            # Keep subprocess diagnostics and input data out of workflow logs.
            raise RuntimeError(f"Fixture Git operation failed: {args[0]}")
        return result.stdout

    def revision(self) -> str:
        return self._git("rev-parse", "--verify", _FIXTURE_REF).decode().strip()

    def _files(self, revision: str) -> dict[str, str]:
        files = {}
        listing = self._git("ls-tree", "-r", "-z", revision, "--", _PUBLIC_DATA)
        for entry in listing.decode().split("\0"):
            if not entry:
                continue
            metadata, path = entry.split("\t", 1)
            mode, kind, oid = metadata.split()
            relative = path.removeprefix(_PUBLIC_DATA)
            if (
                mode != "100644"
                or kind != "blob"
                or not _PUBLIC_PATH.fullmatch(relative)
            ):
                raise ValueError("Invalid fixture publication tree entry")
            files[relative] = oid
        return files

    def _blob(self, oid: str, *, limit: int = _MAX_DOCUMENT_BYTES) -> bytes:
        if int(self._git("cat-file", "-s", oid)) > limit:
            raise ValueError("Fixture publication exceeds data budget")
        return self._git("cat-file", "blob", oid)

    def _archive(
        self, revision: str, season_id: str, snapshot_id: str
    ) -> ArchivedSnapshot | None:
        latest = latest_path(season_id).removeprefix("/data/")
        snapshot = snapshot_path(season_id, snapshot_id).removeprefix("/data/")
        commits = (
            self._git(
                "rev-list",
                "--first-parent",
                f"--max-count={_MAX_ARCHIVE_COMMITS + 1}",
                revision,
            )
            .decode()
            .splitlines()
        )
        if len(commits) > _MAX_ARCHIVE_COMMITS:
            raise ValueError("Fixture publication archive exceeds history budget")
        # Locate the original commit adding the immutable snapshot. The
        # manifest and bytes must come from that same commit.
        for commit, parent in pairwise(commits):
            files = self._files(commit)
            blob = files.get(snapshot)
            if blob is None or snapshot in self._files(parent):
                continue
            if latest not in files:
                return None
            manifest = json.loads(self._blob(files[latest]))
            contents = self._blob(blob)
            validate_manifest_snapshot(manifest, json.loads(contents))
            if (
                manifest["snapshot_id"] != snapshot_id
                or manifest["season_id"] != season_id
            ):
                return None
            return ArchivedSnapshot(manifest, sha256(contents).hexdigest())
        return None

    @contextmanager
    def prior(self) -> Iterator[PriorPublication]:
        """Materialize only protected Git blobs from one captured revision."""
        revision = self.revision()
        with TemporaryDirectory(dir=self.directory) as temp:
            root = Path(temp)
            budget = _MAX_TREE_BYTES
            for path, oid in self._files(revision).items():
                contents = self._blob(oid, limit=min(budget, _MAX_DOCUMENT_BYTES))
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
                target.chmod(0o400)
                budget -= len(contents)
            yield PriorPublication(
                root,
                revision,
                self.revision,
                lambda season, snapshot: self._archive(revision, season, snapshot),
            )

    def apply(self, stage: StagedPublication) -> str:
        """Build an entire commit before a conditional ref update."""
        revision = self.revision()
        if revision != stage.expected_revision:
            raise ValueError("Stale fixture publication")
        files = self._files(revision)
        paths = sorted(stage.directory.rglob("*"))
        staged = {}
        for path in paths:
            if path.is_symlink():
                raise ValueError("Invalid staged publication link")
            if path.is_dir():
                continue
            relative = path.relative_to(stage.directory).as_posix()
            if not _PUBLIC_PATH.fullmatch(relative):
                raise ValueError("Unexpected staged publication file")
            document = _read_document(stage.directory, f"/data/{relative}")
            if document is None:
                raise ValueError("Missing staged publication file")
            if "/snapshots/" in relative:
                if relative in files:
                    raise ValueError("Immutable snapshot already exists")
                _validate_document("snapshot", document[0])
            elif relative.endswith("/history.json"):
                _validate_document("history", document[0])
            elif relative.endswith("/latest.json"):
                _validate_document("latest", document[0])
            else:
                _validate_document("season_index", document[0])
            staged[relative] = document[1]
        manifests = [path for path in staged if path.endswith("/latest.json")]
        if len(manifests) != 1:
            raise ValueError("Expected one staged latest manifest")
        manifest = json.loads(staged[manifests[0]])
        relative = manifest["snapshot_path"].removeprefix("/data/")
        contents = staged.get(relative)
        if contents is None and relative in files:
            contents = self._blob(files[relative])
        if contents is None or manifests[0] != latest_path(
            manifest["season_id"]
        ).removeprefix("/data/"):
            raise ValueError("Invalid staged manifest identity")
        validate_manifest_snapshot(manifest, json.loads(contents))

        season = manifest["season_id"]
        history = history_path(season).removeprefix("/data/")
        permitted = {"index.json", manifests[0]}
        if relative in staged:
            permitted.update((relative, history))
            if history not in staged:
                raise ValueError("New snapshot requires a staged history index")
            index_document = json.loads(staged[history])
            entries = index_document["snapshots"]
            old_entries = (
                json.loads(self._blob(files[history]))["snapshots"]
                if history in files
                else []
            )
            if (
                index_document["season_id"] != season
                or entries[:-1] != old_entries
                or len(entries) != len(old_entries) + 1
                or any(
                    entries[-1][key] != manifest[key]
                    for key in ("snapshot_id", "generated_at", "data_cutoff_at")
                )
            ):
                raise ValueError("Staged history does not append the latest snapshot")
        else:
            archived = self._archive(revision, season, manifest["snapshot_id"])
            if (
                archived is None
                or archived.manifest != manifest
                or archived.snapshot_sha256 != sha256(contents).hexdigest()
            ):
                raise ValueError("Cannot verify staged reversion against archive")

        if staged.keys() - permitted:
            raise ValueError("Staged patch contains unrelated publication files")
        index_bytes = staged.get("index.json")
        if index_bytes is None and "index.json" in files:
            index_bytes = self._blob(files["index.json"])
        if index_bytes is None:
            raise ValueError("Missing publication index")
        season_index = json.loads(index_bytes)
        _validate_document("season_index", season_index)
        if season_index["active_season_id"] != season or season_index[
            "latest_manifest_path"
        ] != latest_path(season):
            raise ValueError("Publication index does not match staged manifest")

        with TemporaryDirectory(dir=self.directory) as temp:
            index = Path(temp) / "index"
            self._git("read-tree", revision, index=index)
            for path, contents in staged.items():
                oid = (
                    self._git("hash-object", "-w", "--stdin", data=contents)
                    .decode()
                    .strip()
                )
                self._git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{oid},{_PUBLIC_DATA}{path}",
                    index=index,
                )
            tree = self._git("write-tree", index=index).decode().strip()
            commit = (
                self._git(
                    "commit-tree",
                    tree,
                    "-p",
                    revision,
                    "-m",
                    "Stage validated forecast",
                )
                .decode()
                .strip()
            )
            self._git("update-ref", _FIXTURE_REF, commit, revision)
        return commit


@contextmanager
def _fixture_repository() -> Iterator[_FixtureRepository]:
    with TemporaryDirectory(prefix="pl-intel-git-fixture-") as temp:
        yield _FixtureRepository(Path(temp))


def main() -> int:
    """Fail closed while the reviewed feasibility gates prohibit publication."""
    print("Production publication is BLOCKED by T03 feasibility gates.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
