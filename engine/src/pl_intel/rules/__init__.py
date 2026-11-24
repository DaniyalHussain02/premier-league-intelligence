"""Season-specific ranking rules and their verification provenance."""

from dataclasses import dataclass
from enum import StrEnum


class RuleVerification(StrEnum):
    VERIFIED = "verified"
    SYNTHETIC = "synthetic"
    UNVERIFIED = "unverified"


class RankingProcedure(StrEnum):
    EPL_SECTION_C = "epl_section_c"


@dataclass(frozen=True, slots=True)
class Ruleset:
    season_id: str
    version: str
    verification: RuleVerification
    evidence_ref: str
    procedure: RankingProcedure
    relegation_places: int = 3


# G3a in docs/FEASIBILITY.md supports these standard rules only.
_RULES_2026_2027 = Ruleset(
    "2026-2027",
    "pl-2026-2027-section-c",
    RuleVerification.VERIFIED,
    "docs/FEASIBILITY.md#official-season-specific-standings-rules",
    RankingProcedure.EPL_SECTION_C,
)
_VERIFIED_RULESETS = {
    (_RULES_2026_2027.season_id, _RULES_2026_2027.version): _RULES_2026_2027,
}


def verified_ruleset(season_id: str, version: str) -> Ruleset | None:
    """Return the exact reviewed version for this season, if available."""
    return _VERIFIED_RULESETS.get((season_id, version))
