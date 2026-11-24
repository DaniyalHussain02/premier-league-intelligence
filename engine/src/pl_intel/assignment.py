"""Maximum-weight representative positions from T09 marginal probabilities."""

from dataclasses import dataclass
from math import fsum, isfinite

from scipy.optimize import linear_sum_assignment

from pl_intel.simulate import ClubForecast, SimulationResult
from pl_intel.validate import require_canonical_id

_CLUB_COUNT = 20
_NORMALIZATION_TOLERANCE = 1e-12
_EXPLANATION = (
    "This representative table gives every club a unique position and maximizes "
    "the sum of their assigned finishing-position probabilities. Clubs can "
    "receive a position other than their individual most likely finish. "
    "Projected points are shown separately. The combined score is a sum of "
    "individual probabilities, not the probability of the entire table."
)


@dataclass(frozen=True, slots=True)
class RepresentativeRow:
    team_id: str
    predicted_position: int
    assigned_probability: float
    expected_earned_points: float
    expected_adjusted_points: float


@dataclass(frozen=True, slots=True)
class RepresentativeTable:
    rows: tuple[RepresentativeRow, ...]
    total_assigned_probability: float
    explanation: str = _EXPLANATION


def _normalized(values: tuple[float, ...]) -> bool:
    return abs(fsum(values) - 1.0) <= _NORMALIZATION_TOLERANCE


def _finite_number(value: object) -> bool:
    return type(value) is int or (type(value) is float and isfinite(value))


def _validated_clubs(forecast: SimulationResult) -> tuple[ClubForecast, ...]:
    if not isinstance(forecast, SimulationResult):
        raise TypeError("Assignment requires a successful simulation result")
    if not isinstance(forecast.clubs, tuple) or len(forecast.clubs) != _CLUB_COUNT:
        raise ValueError("Assignment requires exactly 20 clubs")

    seen = set()
    for club in forecast.clubs:
        if not isinstance(club, ClubForecast):
            raise TypeError("Assignment requires club forecast rows")
        require_canonical_id(club.team_id)
        if club.team_id in seen:
            raise ValueError("Duplicate club ID")
        seen.add(club.team_id)
        if not all(
            _finite_number(points)
            for points in (club.expected_earned_points, club.expected_adjusted_points)
        ):
            raise ValueError("Expected points must be finite real numbers")
        probabilities = club.positions
        if not isinstance(probabilities, tuple) or len(probabilities) != _CLUB_COUNT:
            raise ValueError(
                "Assignment requires exactly 20 position probabilities per club"
            )
        if not all(_finite_number(p) and 0 <= p <= 1 for p in probabilities):
            raise ValueError("Position probabilities must be finite and in [0, 1]")
        if not _normalized(probabilities):
            raise ValueError("Position probability rows must sum to 1")
    if not all(
        _normalized(column)
        for column in zip(*(c.positions for c in forecast.clubs), strict=True)
    ):
        raise ValueError("Position probability columns must sum to 1")
    return tuple(sorted(forecast.clubs, key=lambda club: club.team_id))


def _exact_weights(probabilities: tuple[tuple[float, ...], ...]) -> list[list[int]]:
    ratios = [[p.as_integer_ratio() for p in row] for row in probabilities]
    # Every denominator is a power of two, so the largest is a common scale.
    scale = max(denominator for row in ratios for _, denominator in row)
    return [
        [numerator * (scale // denominator) for numerator, denominator in row]
        for row in ratios
    ]


def _tight_edges(
    weights: list[list[int]], column_by_row: list[int]
) -> list[list[bool]] | None:
    """Certify a candidate by ruling out positive-gain reassignment cycles.

    An edge from column a to b moves the row currently assigned to a.
    Every alternative permutation decomposes into such cycles. Bellman–Ford
    with an implicit zero-cost source finds potentials that bound every edge
    gain. An improvement on pass n proves a positive cycle. For a certified
    candidate the potentials telescope around every cycle, so an alternative
    has the same score exactly when all its edges are tight.
    """
    n = len(weights)
    potential = [0] * n
    for _ in range(n):
        updated = potential.copy()
        for row, source in zip(weights, column_by_row, strict=True):
            base = potential[source] - row[source]
            for target, weight in enumerate(row):
                updated[target] = max(updated[target], base + weight)
        if updated == potential:
            return [
                [
                    potential[source] - row[source] + weight == potential[target]
                    for target, weight in enumerate(row)
                ]
                for row, source in zip(weights, column_by_row, strict=True)
            ]
        potential = updated
    return None


def _exact_assignment(weights: list[list[int]]) -> list[int]:
    """Find the optimal matching with the O(n³) integer Hungarian algorithm.

    Maintain row and column dual potentials for the negated costs. For each
    new row, grow an alternating tree by the least reduced-cost slack until
    a free column is reached, then augment along the recorded predecessors.
    Column zero is the temporary tree root.
    """
    n = len(weights)
    row_dual = [0] * (n + 1)
    col_dual = [0] * (n + 1)
    row_at_col = [0] * (n + 1)
    for new_row in range(1, n + 1):
        row_at_col[0] = new_row
        used = [False] * (n + 1)
        slack: list[int | None] = [None] * (n + 1)
        predecessor = [0] * (n + 1)
        column = 0
        while True:
            used[column] = True
            row = row_at_col[column]
            next_column = 0
            delta = None
            for target in range(1, n + 1):
                if used[target]:
                    continue
                reduced = (
                    -weights[row - 1][target - 1] - row_dual[row] - col_dual[target]
                )
                best = slack[target]
                if best is None or reduced < best:
                    slack[target] = reduced
                    predecessor[target] = column
                    best = reduced
                if delta is None or best < delta:
                    delta, next_column = best, target
            assert delta is not None
            for target in range(n + 1):
                if used[target]:
                    row_dual[row_at_col[target]] += delta
                    col_dual[target] -= delta
                elif slack[target] is not None:
                    slack[target] -= delta
            column = next_column
            if row_at_col[column] == 0:
                break
        while column:
            previous = predecessor[column]
            row_at_col[column] = row_at_col[previous]
            column = previous

    column_by_row = [0] * n
    for column, row in enumerate(row_at_col[1:]):
        column_by_row[row - 1] = column
    return column_by_row


def _has_perfect_matching(tight: list[list[bool]], rows: list[int], start: int) -> bool:
    """Test whether the remaining tight edges can fill every remaining rank."""
    if not rows:
        return True
    # A zero-cost assignment exists exactly when all selected edges are tight.
    costs = [[int(not edge) for edge in tight[row][start:]] for row in rows]
    indices, columns = linear_sum_assignment(costs)
    return all(
        costs[row][column] == 0 for row, column in zip(indices, columns, strict=True)
    )


def _lexicographic_rows(tight: list[list[bool]]) -> list[int]:
    remaining = list(range(len(tight)))
    ordered = []
    for column in range(len(tight)):
        for row in remaining:
            if not tight[row][column]:
                continue
            rest = [candidate for candidate in remaining if candidate != row]
            if _has_perfect_matching(tight, rest, column + 1):
                ordered.append(row)
                remaining = rest
                break
        else:
            raise ArithmeticError("No optimal completion for representative positions")
    return ordered


def assign_representative_table(forecast: SimulationResult) -> RepresentativeTable:
    """Assign unique ranks using exact binary weights and stable ID tie breaks."""
    clubs = _validated_clubs(forecast)
    probabilities = tuple(club.positions for club in clubs)
    weights = _exact_weights(probabilities)
    indices, columns = linear_sum_assignment(
        [[-p for p in row] for row in probabilities]
    )
    candidate = [0] * _CLUB_COUNT
    for row, column in zip(indices, columns, strict=True):
        candidate[row] = int(column)

    tight = _tight_edges(weights, candidate)
    if tight is None:
        candidate = _exact_assignment(weights)
        tight = _tight_edges(weights, candidate)
        if tight is None:
            raise ArithmeticError("Unable to certify an optimal representative table")

    optimal_score = sum(weights[row][column] for row, column in enumerate(candidate))
    ordered = _lexicographic_rows(tight)
    if sum(weights[row][column] for column, row in enumerate(ordered)) != optimal_score:
        raise ArithmeticError("Lexicographic assignment changed the optimal score")

    rows = tuple(
        RepresentativeRow(
            clubs[row].team_id,
            column + 1,
            probabilities[row][column],
            clubs[row].expected_earned_points,
            clubs[row].expected_adjusted_points,
        )
        for column, row in enumerate(ordered)
    )
    return RepresentativeTable(rows, fsum(row.assigned_probability for row in rows))
