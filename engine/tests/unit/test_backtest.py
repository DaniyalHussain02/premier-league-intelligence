from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import nan
from types import SimpleNamespace

import pytest
from test_simulate import season_input

from pl_intel import backtest
from pl_intel.backtest import (
    BacktestBlocked,
    BacktestConfig,
    BacktestDataset,
    BacktestReport,
    FixtureObservation,
    FixtureRevision,
    Outcome,
    ResultEvidence,
    ReviewedEvidence,
    StudyKind,
    brier_score,
    run_backtest,
)
from pl_intel.elo import EloMatch
from pl_intel.normalize import canonical_fixture_id
from pl_intel.poisson import MODEL_VERSION
from pl_intel.schemas import FixtureStatus, SeasonStatus

_START = datetime(2026, 8, 15, 15, tzinfo=UTC)
_STUDY = datetime(2027, 6, 30, tzinfo=UTC)
_CONFIG = BacktestConfig(datetime(2017, 8, 1, tzinfo=UTC), _STUDY)
_REVIEW = ReviewedEvidence("synthetic-reviewed", _STUDY)
_SCHEDULED = FixtureStatus.SCHEDULED


def synthetic_study(prior_seasons=0):
    snapshot, history = season_input(all_draws=True)
    observations = []
    for index, match in enumerate(history):
        kickoff = _START + timedelta(days=index // 10 * 7)
        available = kickoff + timedelta(hours=3)
        fixture = replace(match.fixture, kickoff_utc=kickoff, updated_at=available)
        result = replace(match.result, available_at=available)
        observations.append(
            FixtureObservation(
                ResultEvidence(
                    EloMatch(result, fixture, match.identity_source_ref), _REVIEW
                ),
                (
                    FixtureRevision(
                        fixture.fixture_id,
                        kickoff - timedelta(hours=2),
                        _SCHEDULED,
                        kickoff,
                        _REVIEW,
                    ),
                ),
                _REVIEW,
                kickoff,
            )
        )
    prior = []
    for year in range(2017, 2017 + prior_seasons):
        season_id = f"{year}-{year + 1}"
        for index, match in enumerate(history):
            kickoff = _START.replace(year=year) + timedelta(days=index // 10 * 7)
            available = kickoff + timedelta(hours=3)
            fixture = replace(
                match.fixture,
                season_id=season_id,
                fixture_id=canonical_fixture_id(
                    season_id, match.fixture.home_team_id, match.fixture.away_team_id
                ),
                provider_fixture_id=f"{year}-{index}",
                kickoff_utc=kickoff,
                updated_at=available,
            )
            result = replace(
                match.result, fixture_id=fixture.fixture_id, available_at=available
            )
            prior.append(
                ResultEvidence(
                    EloMatch(result, fixture, match.identity_source_ref), _REVIEW
                )
            )
    return BacktestDataset(
        StudyKind.SYNTHETIC,
        replace(snapshot.season, status=SeasonStatus.COMPLETE),
        snapshot.teams,
        tuple(observations),
        tuple(prior),
        ("synthetic-source-v1",),
        _REVIEW,
        _REVIEW,
    )


def _first(dataset, **changes):
    return replace(
        dataset,
        observations=(
            replace(dataset.observations[0], **changes),
            *dataset.observations[1:],
        ),
    )


def _result_at(observation, available_at):
    evidence = observation.result
    match = evidence.match
    return replace(
        evidence,
        match=replace(match, result=replace(match.result, available_at=available_at)),
    )


def test_walk_forward_strict_cutoffs_and_identical_training(monkeypatch):
    dataset = synthetic_study()
    target = dataset.observations[0]
    delayed_at = _START + timedelta(days=7, hours=-1)
    dataset = _first(dataset, result=_result_at(target, delayed_at))
    received = []
    predict = backtest.predict_match

    def trace(history, **kwargs):
        history = tuple(history)
        received.append(tuple(match.result.fixture_id for match in history))
        assert all(match.result.available_at < kwargs["cutoff"] for match in history)
        return predict(history, **kwargs)

    monkeypatch.setattr(backtest, "predict_match", trace)
    report = run_backtest(dataset, config=_CONFIG)
    assert isinstance(report, BacktestReport)
    assert report.label == "RETROSPECTIVE"
    assert report.kind is StudyKind.SYNTHETIC
    assert "ineligible for publication" in report.limitations[-1]
    assert report.sample_count == 380
    assert len({row.fixture_id for row in report.observations}) == 380
    delayed_id = target.result.match.result.fixture_id
    assert all(not history for history in received[:10])
    assert all(
        len(history) == 9 and delayed_id not in history for history in received[10:20]
    )
    assert all(
        len(history) == 20 and delayed_id in history for history in received[20:30]
    )
    assert report.fallback_count == 20
    for row, ids in zip(report.observations, received, strict=True):
        assert ids == report.training_order[: row.eligible_count]
        assert row.fixture_id not in ids
        expected = (1 / 3,) * 3 if len(ids) < 20 else (0, 1, 0)
        assert row.baseline == expected
        assert row.outcome is Outcome.DRAW
    assert report.baseline_brier == pytest.approx(20 / 380 * 2 / 3)


def test_repeatability_and_input_permutation():
    dataset = synthetic_study()
    report = run_backtest(dataset, config=_CONFIG)
    assert isinstance(report, BacktestReport)
    assert report.source_revisions == ("synthetic-source-v1",)
    assert report.evidence_refs
    assert report.model_version == MODEL_VERSION
    assert report.config == _CONFIG
    assert run_backtest(dataset, config=_CONFIG) == report
    changed = replace(
        dataset, observations=dataset.observations[::-1], teams=dataset.teams[::-1]
    )
    assert run_backtest(changed, config=_CONFIG) == report


def test_future_outcome_change_does_not_change_earlier_predictions():
    dataset = synthetic_study()
    original = run_backtest(dataset, config=_CONFIG)
    assert isinstance(original, BacktestReport)
    first = dataset.observations[0].result
    match = first.match
    replacement = replace(
        first,
        match=replace(
            match,
            fixture=replace(match.fixture, home_goals=2),
            result=replace(match.result, home_goals=2),
        ),
    )
    updated = run_backtest(_first(dataset, result=replacement), config=_CONFIG)
    assert isinstance(updated, BacktestReport)
    assert [(r.model, r.baseline) for r in updated.observations[:10]] == [
        (r.model, r.baseline) for r in original.observations[:10]
    ]
    assert any(
        a.model != b.model
        for a, b in zip(
            updated.observations[10:], original.observations[10:], strict=True
        )
    )


def _revision(hours, status=_SCHEDULED, kickoff=_START):
    return FixtureRevision(
        canonical_fixture_id("2026-2027", "club-05", "club-10"),
        _START + timedelta(hours=hours),
        status,
        kickoff,
        _REVIEW,
    )


@pytest.mark.parametrize(
    ("revisions", "expected"),
    [
        ((_revision(-3, kickoff=_START + timedelta(hours=2)), _revision(-2)), -1),
        (
            (
                _revision(-3),
                _revision(-1, FixtureStatus.POSTPONED, None),
                _revision(1, kickoff=_START + timedelta(hours=4)),
            ),
            3,
        ),
        ((_revision(-3), _revision(0, FixtureStatus.POSTPONED, None)), -1),
        ((_revision(-0.5),), None),
        ((_revision(-3), _revision(-1, FixtureStatus.CANCELLED, None)), None),
        (
            (_revision(-3), _revision(-3, kickoff=_START + timedelta(hours=4))),
            "conflict",
        ),
    ],
)
def test_first_timer_uses_only_then_known_schedule(revisions, expected):
    if expected == "conflict":
        with pytest.raises(backtest._ConflictingFixtureRevisionsError):
            backtest._select_timer(revisions[::-1], _STUDY)
        return
    timer = backtest._select_timer(revisions[::-1], _STUDY)
    if expected is None:
        assert timer is None
    else:
        assert timer.cutoff == _START + timedelta(hours=expected)


def test_equal_equivalent_revision_claims_keep_both_refs():
    revision = _revision(-3)
    duplicate = replace(revision, review=ReviewedEvidence("other-proof", _STUDY))
    timer = backtest._select_timer((revision, duplicate), _STUDY)
    assert timer.refs == ("other-proof", "synthetic-reviewed")


@pytest.mark.parametrize(
    "change", [{"result": None}, {"revision_coverage": None}, {"revisions": ()}]
)
def test_missing_target_evidence_blocks_without_partial_scores(change):
    blocked = run_backtest(_first(synthetic_study(), **change), config=_CONFIG)
    assert isinstance(blocked, BacktestBlocked)
    assert not hasattr(blocked, "model_brier")


def test_historical_classification_is_not_independent_authentication():
    dataset = replace(synthetic_study(), kind=StudyKind.VERIFIED_HISTORICAL)
    expected = BacktestBlocked("independent_authentication_required")
    assert run_backtest(dataset, config=_CONFIG) == expected
    assert (
        run_backtest(dataset, config=_CONFIG, verify_evidence=lambda *_: False)
        == expected
    )


def test_future_evidence_rejected_even_after_timer_locked():
    dataset = synthetic_study()
    late = replace(
        _revision(1), review=ReviewedEvidence("future", _STUDY + timedelta(seconds=1))
    )
    with pytest.raises(ValueError, match="later than"):
        run_backtest(
            _first(dataset, revisions=(*dataset.observations[0].revisions, late)),
            config=_CONFIG,
        )


def test_actual_kickoff_cannot_be_used_to_retry_invalid_timer():
    dataset = synthetic_study()
    revisions = (
        _revision(-3, kickoff=_START + timedelta(hours=1)),
        _revision(-1, kickoff=_START + timedelta(hours=2)),
    )
    with pytest.raises(ValueError, match="pre-kickoff"):
        run_backtest(
            _first(
                dataset,
                revisions=revisions,
                coverage_through=_START + timedelta(hours=2),
            ),
            config=_CONFIG,
        )


def test_duplicate_identity_rejected_but_missing_cohort_blocks():
    dataset = synthetic_study()
    assert run_backtest(
        replace(dataset, observations=dataset.observations[:-1]), config=_CONFIG
    ) == BacktestBlocked("incomplete_evaluation_season")
    with pytest.raises(ValueError, match="Duplicate"):
        run_backtest(
            replace(
                dataset,
                observations=(*dataset.observations[:-1], dataset.observations[0]),
            ),
            config=_CONFIG,
        )


def test_combined_training_cap_does_not_truncate():
    dataset = synthetic_study(9)
    assert len(dataset.prior_results) + len(dataset.observations) == 3800
    assert run_backtest(
        replace(
            dataset, prior_results=(*dataset.prior_results, dataset.prior_results[0])
        ),
        config=_CONFIG,
    ) == BacktestBlocked("combined_result_cap_exceeded")


def test_empty_history_and_fallback_threshold(monkeypatch):
    dataset = synthetic_study(1)
    monkeypatch.setattr(
        backtest,
        "predict_match",
        lambda *a, **kw: SimpleNamespace(
            model_version=MODEL_VERSION, home_win=0, draw=1, away_win=0
        ),
    )
    for size in (0, 19, 20):
        result = run_backtest(
            replace(dataset, prior_results=dataset.prior_results[:size]), config=_CONFIG
        )
        assert isinstance(result, BacktestReport)
        assert result.observations[0].eligible_count == size
        assert result.observations[0].baseline == (
            (1 / 3,) * 3 if size < 20 else (0, 1, 0)
        )
        assert result.model_brier == 0


@pytest.mark.parametrize(
    ("values", "outcome", "expected"),
    [
        ((1, 0, 0), Outcome.HOME, 0),
        ((1, 0, 0), Outcome.AWAY, 2),
        ((0.5, 0.25, 0.25), Outcome.DRAW, 0.875),
    ],
)
def test_brier_hand_calculations(values, outcome, expected):
    assert brier_score(values, outcome) == expected


@pytest.mark.parametrize(
    "values", [(1, 0), (0.5, 0.5, 0.5), (nan, 0, 1), (-0.1, 0.1, 1), (True, 0, 0)]
)
def test_brier_rejects_invalid_probabilities(values):
    with pytest.raises(ValueError, match="normalized"):
        brier_score(values, Outcome.HOME)


def test_result_with_unknown_availability_is_invalid():
    dataset = synthetic_study()
    with pytest.raises(ValueError, match="UTC"):
        run_backtest(
            _first(dataset, result=_result_at(dataset.observations[0], None)),
            config=_CONFIG,
        )


def test_swapped_revision_binding_and_insufficient_timeline_coverage():
    dataset = synthetic_study()
    revision = dataset.observations[0].revisions[0]
    other_id = dataset.observations[1].result.match.result.fixture_id
    with pytest.raises(ValueError, match="different fixture"):
        run_backtest(
            _first(dataset, revisions=(replace(revision, fixture_id=other_id),)),
            config=_CONFIG,
        )
    blocked = run_backtest(
        _first(dataset, coverage_through=revision.known_at), config=_CONFIG
    )
    assert blocked == BacktestBlocked(
        "unverified_prediction_timer", revision.fixture_id
    )


def test_duplicate_provider_identity_across_prior_and_target():
    dataset = synthetic_study(1)
    first = dataset.prior_results[0]
    target = dataset.observations[0].result.match.fixture
    conflicting = replace(
        first,
        match=replace(
            first.match,
            fixture=replace(
                first.match.fixture, provider_fixture_id=target.provider_fixture_id
            ),
        ),
    )
    with pytest.raises(ValueError, match="Duplicate"):
        run_backtest(replace(dataset, prior_results=(conflicting,)), config=_CONFIG)


def test_invalid_model_probabilities_block_scoring(monkeypatch):
    monkeypatch.setattr(
        backtest,
        "predict_match",
        lambda *a, **kw: SimpleNamespace(
            model_version=MODEL_VERSION, home_win=nan, draw=0, away_win=1
        ),
    )
    with pytest.raises(ValueError, match="normalized"):
        run_backtest(synthetic_study(), config=_CONFIG)


def test_later_postponement_preserves_locked_prediction():
    dataset = synthetic_study()
    original = run_backtest(dataset, config=_CONFIG)
    assert isinstance(original, BacktestReport)
    observation = dataset.observations[0]
    evidence = observation.result
    match = evidence.match
    later_kickoff = _START + timedelta(hours=4)
    later_available = later_kickoff + timedelta(hours=3)
    postponed = _revision(0, FixtureStatus.POSTPONED, None)
    replacement = _revision(1, kickoff=later_kickoff)
    changed = _first(
        dataset,
        revisions=(*observation.revisions, postponed, replacement),
        result=replace(
            evidence,
            match=replace(
                match,
                fixture=replace(
                    match.fixture, kickoff_utc=later_kickoff, updated_at=later_available
                ),
                result=replace(match.result, available_at=later_available),
            ),
        ),
    )
    result = run_backtest(changed, config=_CONFIG)
    assert isinstance(result, BacktestReport)
    assert result.sample_count == 380
    assert result.observations[:10] == original.observations[:10]


@pytest.mark.parametrize("hours", [-2, -1, 0], ids=["before", "at", "after"])
def test_revision_conflict_is_distinct_from_missing_timer(hours):
    dataset = synthetic_study()
    initial = _revision(-3)
    conflicting = (
        initial,
        _revision(hours),
        _revision(hours, kickoff=_START + timedelta(hours=4)),
    )
    expected = BacktestBlocked("conflicting_fixture_revisions", initial.fixture_id)
    for revisions in (conflicting, conflicting[::-1]):
        assert (
            run_backtest(_first(dataset, revisions=revisions), config=_CONFIG)
            == expected
        )

    missing = _first(dataset, revisions=(_revision(-0.5),))
    assert run_backtest(missing, config=_CONFIG) == BacktestBlocked(
        "unverified_prediction_timer", initial.fixture_id
    )
