"""Prepare a staged forecast from one trusted input acquisition and simulation."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime

from pl_intel.elo import EloConfig, EloMatch
from pl_intel.poisson import PoissonConfig
from pl_intel.publish import (
    PriorPublication,
    StagedPublication,
    _format_utc,
    _stage_prepared,
    _validated_simulation,
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


def main() -> int:
    """Fail closed while the reviewed feasibility gates prohibit publication."""
    print("Production publication is BLOCKED by T03 feasibility gates.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
