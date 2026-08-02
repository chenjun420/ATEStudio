"""Unit tests for FaultPredictor — numpy-based fault probability classifier.

Tests cover:
- predict() returns valid probabilities [0.0, 1.0]
- Untrained predictor returns 0.5 (uncertain)
- Trained predictor distinguishes high-risk from low-risk steps
- train_from_samples() with both classes present
- train_from_samples() with single class (edge case)
- train_from_samples() with empty data (edge case)
- get_step_fault_probability() convenience method
- get_step_probabilities() batch method
- Feature vectorization produces correct shapes
- Sigmoid numerical stability
- DefaultFeatureExtractor.extract_step_features()
- DefaultFeatureExtractor.extract_training_data() with mock Qdrant
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from ate_platform.scheduler.fault_predictor import (
    DefaultFeatureExtractor,
    FaultPredictor,
    _sigmoid,
    _sigmoid_scalar,
    vectorize_features,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_training_samples() -> list[dict[str, Any]]:
    """Create synthetic training data with clear fault/success separation.

    High-risk step: "risky_step" with recent_failure_count=5, afternoon time
    Low-risk step: "safe_step" with recent_failure_count=0, morning time
    """
    samples: list[dict[str, Any]] = []

    # Positive examples (faults) — high recent failures, afternoon
    for _ in range(20):
        samples.append({
            "product_type": "product_a",
            "test_step": "risky_step",
            "time_of_day": 15,
            "instrument_id": "oscilloscope_1",
            "recent_failure_count": 5,
            "label": 1,
        })

    # Negative examples (successes) — low recent failures, morning
    for _ in range(20):
        samples.append({
            "product_type": "product_a",
            "test_step": "safe_step",
            "time_of_day": 8,
            "instrument_id": "multimeter_1",
            "recent_failure_count": 0,
            "label": 0,
        })

    return samples


def _make_mock_qdrant_with_faults() -> MagicMock:
    """Create a mock Qdrant client with fault records in scroll results."""
    mock_client = MagicMock()

    # Simulate scroll returning fault records
    fault_points = []
    for i in range(10):
        point = MagicMock()
        point.payload = {
            "failed_step_id": f"step_{i % 3}",
            "failed_step_name": f"Test Step {i % 3}",
            "plan_name": "product_x",
            "timestamp": f"2026-01-15T1{i}:30:00",
            "instrument_id": "scope_1",
            "error_message": "Voltage out of range",
        }
        fault_points.append(point)

    mock_client.scroll.return_value = (fault_points, None)
    return mock_client


# ---------------------------------------------------------------------------
# Tests: Untrained predictor
# ---------------------------------------------------------------------------


class TestUntrainedPredictor:
    """Tests for the untrained state."""

    def test_untrained_predict_returns_default(self) -> None:
        """Given: untrained predictor. When: predict(). Then: returns 0.5."""
        predictor = FaultPredictor()
        result = predictor.predict({"test_step": "step_a"})
        assert result == pytest.approx(0.5)

    def test_untrained_is_trained_false(self) -> None:
        """Given: untrained predictor. When: check is_trained. Then: False."""
        predictor = FaultPredictor()
        assert predictor.is_trained is False

    def test_untrained_get_step_fault_probability_returns_default(self) -> None:
        """Given: untrained predictor. When: get_step_fault_probability(). Then: 0.5."""
        predictor = FaultPredictor()
        result = predictor.get_step_fault_probability("step_a")
        assert result == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Tests: Trained predictor — probability validity
# ---------------------------------------------------------------------------


class TestPredictProbabilityRange:
    """predict() must always return a value in [0.0, 1.0]."""

    def test_trained_predict_in_range(self) -> None:
        """Given: trained predictor. When: predict(). Then: 0.0 <= p <= 1.0."""
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        features = {
            "product_type": "product_a",
            "test_step": "risky_step",
            "time_of_day": 15,
            "instrument_id": "oscilloscope_1",
            "recent_failure_count": 5,
        }
        prob = predictor.predict(features)
        assert 0.0 <= prob <= 1.0

    def test_trained_predict_multiple_features_in_range(self) -> None:
        """Given: trained predictor. When: predict various features. Then: all in [0,1]."""
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        test_cases = [
            {"test_step": "risky_step", "recent_failure_count": 10, "time_of_day": 22},
            {"test_step": "safe_step", "recent_failure_count": 0, "time_of_day": 6},
            {"test_step": "unknown_step", "recent_failure_count": 3, "time_of_day": 12},
        ]

        for features in test_cases:
            prob = predictor.predict(features)
            assert 0.0 <= prob <= 1.0, f"Probability {prob} out of range for {features}"


# ---------------------------------------------------------------------------
# Tests: Fault probability ranking — the core verification
# ---------------------------------------------------------------------------


class TestFaultProbabilityRanking:
    """The predictor must distinguish high-risk from low-risk steps."""

    def test_high_risk_higher_than_low_risk(self) -> None:
        """Given: trained predictor with clear fault pattern.
        When: predict high-risk vs low-risk step.
        Then: high-risk probability > low-risk probability.
        """
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        high_risk_prob = predictor.get_step_fault_probability(
            step_id="risky_step",
            product_type="product_a",
            time_of_day=15,
            instrument_id="oscilloscope_1",
            recent_failure_count=5,
        )
        low_risk_prob = predictor.get_step_fault_probability(
            step_id="safe_step",
            product_type="product_a",
            time_of_day=8,
            instrument_id="multimeter_1",
            recent_failure_count=0,
        )

        assert high_risk_prob > low_risk_prob, (
            f"High-risk ({high_risk_prob}) should be > low-risk ({low_risk_prob})"
        )

    def test_recent_failure_count_affects_probability(self) -> None:
        """Given: trained predictor. When: vary recent_failure_count
        for a known risky step.
        Then: higher recent failures → higher or equal probability.
        """
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        prob_low_rfc = predictor.get_step_fault_probability(
            step_id="risky_step",
            product_type="product_a",
            time_of_day=15,
            instrument_id="oscilloscope_1",
            recent_failure_count=0,
        )
        prob_high_rfc = predictor.get_step_fault_probability(
            step_id="risky_step",
            product_type="product_a",
            time_of_day=15,
            instrument_id="oscilloscope_1",
            recent_failure_count=8,
        )

        assert prob_high_rfc >= prob_low_rfc, (
            f"High RFC ({prob_high_rfc}) should be >= low RFC ({prob_low_rfc})"
        )


# ---------------------------------------------------------------------------
# Tests: Batch prediction
# ---------------------------------------------------------------------------


class TestBatchPrediction:
    """get_step_probabilities() batch method."""

    def test_batch_returns_all_steps(self) -> None:
        """Given: trained predictor. When: batch predict 3 steps.
        Then: result has 3 entries.
        """
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        step_ids = ["step_a", "step_b", "step_c"]
        result = predictor.get_step_probabilities(step_ids)

        assert len(result) == 3
        assert all(sid in result for sid in step_ids)

    def test_batch_all_probabilities_in_range(self) -> None:
        """Given: trained predictor. When: batch predict.
        Then: all probabilities in [0.0, 1.0].
        """
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        step_ids = ["step_a", "step_b", "step_c"]
        result = predictor.get_step_probabilities(step_ids)

        for prob in result.values():
            assert 0.0 <= prob <= 1.0

    def test_batch_with_recent_failure_counts(self) -> None:
        """Given: trained predictor with RFC data. When: batch predict
        with known step IDs and different RFC.
        Then: steps with higher RFC have higher probability.
        """
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        # With higher RFC, the probability should be higher than with 0
        prob_high_rfc = predictor.get_step_fault_probability(
            step_id="risky_step",
            recent_failure_count=8,
        )
        prob_low_rfc = predictor.get_step_fault_probability(
            step_id="risky_step",
            recent_failure_count=0,
        )
        assert prob_high_rfc >= prob_low_rfc


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case handling."""

    def test_empty_training_data_stays_untrained(self) -> None:
        """Given: empty training data. When: train_from_samples().
        Then: predictor stays untrained.
        """
        predictor = FaultPredictor()
        predictor.train_from_samples([])
        assert predictor.is_trained is False

    def test_single_class_training_stays_untrained(self) -> None:
        """Given: only positive labels. When: train_from_samples().
        Then: predictor stays untrained (can't learn without both classes).
        """
        predictor = FaultPredictor()
        samples = [
            {"test_step": "a", "label": 1, "time_of_day": 12, "recent_failure_count": 1},
            {"test_step": "b", "label": 1, "time_of_day": 14, "recent_failure_count": 2},
        ]
        predictor.train_from_samples(samples)
        assert predictor.is_trained is False

    def test_predict_with_missing_features(self) -> None:
        """Given: trained predictor. When: predict with minimal features.
        Then: returns valid probability (no crash).
        """
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        prob = predictor.predict({"test_step": "unknown"})
        assert 0.0 <= prob <= 1.0


# ---------------------------------------------------------------------------
# Tests: Feature vectorization
# ---------------------------------------------------------------------------


class TestFeatureVectorization:
    """vectorize_features() produces correct output."""

    def test_vector_has_correct_size(self) -> None:
        """Given: feature dict. When: vectorize. Then: correct vector size."""
        features = {
            "product_type": "pt",
            "test_step": "ts",
            "instrument_id": "inst",
            "time_of_day": 12,
            "recent_failure_count": 3,
        }
        vec = vectorize_features(features)
        assert vec.shape == (34,)  # _FEATURE_HASH_DIM + _NUMERICAL_FEATURE_COUNT

    def test_numerical_features_normalized(self) -> None:
        """Given: time_of_day=23, recent_failure_count=5. When: vectorize.
        Then: numerical slots are normalized to [0,1].
        """
        vec = vectorize_features({
            "time_of_day": 23,
            "recent_failure_count": 5,
        })
        # time_of_day normalized: 23/23 = 1.0
        assert vec[32] == pytest.approx(1.0)
        # recent_failure_count normalized: 5/10 = 0.5
        assert vec[33] == pytest.approx(0.5)

    def test_high_failure_count_clamped(self) -> None:
        """Given: recent_failure_count=20 (> 10). When: vectorize.
        Then: clamped to 1.0.
        """
        vec = vectorize_features({"recent_failure_count": 20})
        assert vec[33] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tests: Sigmoid stability
# ---------------------------------------------------------------------------


class TestSigmoidStability:
    """Numerical stability of sigmoid functions."""

    def test_scalar_sigmoid_extremes(self) -> None:
        """Given: very large positive/negative inputs. When: sigmoid.
        Then: no overflow, returns valid values.
        """
        assert _sigmoid_scalar(1000.0) == pytest.approx(1.0)
        assert _sigmoid_scalar(-1000.0) == pytest.approx(0.0)
        assert _sigmoid_scalar(0.0) == pytest.approx(0.5)

    def test_array_sigmoid_extremes(self) -> None:
        """Given: array with extreme values. When: sigmoid.
        Then: no NaN, no inf.
        """
        x = np.array([-1000.0, -1.0, 0.0, 1.0, 1000.0])
        result = _sigmoid(x)
        assert not np.any(np.isnan(result))
        assert not np.any(np.isinf(result))
        assert result[0] == pytest.approx(0.0)
        assert result[4] == pytest.approx(1.0)
        assert result[2] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Tests: DefaultFeatureExtractor
# ---------------------------------------------------------------------------


class TestDefaultFeatureExtractor:
    """DefaultFeatureExtractor behavior."""

    def test_extract_step_features_returns_dict(self) -> None:
        """Given: DefaultFeatureExtractor. When: extract_step_features().
        Then: returns dict with expected keys.
        """
        extractor = DefaultFeatureExtractor()
        features = extractor.extract_step_features(
            step_id="test_step",
            product_type="product_a",
            time_of_day=14,
            instrument_id="scope_1",
            recent_failure_count=3,
        )
        assert features["test_step"] == "test_step"
        assert features["product_type"] == "product_a"
        assert features["time_of_day"] == 14
        assert features["instrument_id"] == "scope_1"
        assert features["recent_failure_count"] == 3

    def test_extract_training_data_from_mock_qdrant(self) -> None:
        """Given: mock Qdrant with fault records. When: extract_training_data().
        Then: returns samples with both positive and negative labels.
        """
        mock_client = _make_mock_qdrant_with_faults()
        extractor = DefaultFeatureExtractor()
        samples = extractor.extract_training_data(mock_client, "ate_failures")

        assert len(samples) > 0
        labels = {s["label"] for s in samples}
        assert 1 in labels  # At least one positive example
        assert 0 in labels  # At least one negative example

    def test_extract_training_data_qdrant_error_returns_empty(self) -> None:
        """Given: Qdrant client that raises. When: extract_training_data().
        Then: returns empty list (no crash).
        """
        mock_client = MagicMock()
        mock_client.scroll.side_effect = RuntimeError("connection failed")
        extractor = DefaultFeatureExtractor()
        samples = extractor.extract_training_data(mock_client, "ate_failures")
        assert samples == []


# ---------------------------------------------------------------------------
# Tests: Train from Qdrant (mocked)
# ---------------------------------------------------------------------------


class TestTrainFromQdrant:
    """Training via the train() method with mocked Qdrant."""

    def test_train_with_mock_qdrant_succeeds(self) -> None:
        """Given: mock Qdrant with faults. When: train(). Then: is_trained=True."""
        mock_client = _make_mock_qdrant_with_faults()
        predictor = FaultPredictor()
        predictor.train(mock_client, "ate_failures")
        assert predictor.is_trained is True

    def test_train_with_empty_qdrant_stays_untrained(self) -> None:
        """Given: Qdrant with no records. When: train(). Then: untrained."""
        mock_client = MagicMock()
        mock_client.scroll.return_value = ([], None)
        predictor = FaultPredictor()
        predictor.train(mock_client, "ate_failures")
        assert predictor.is_trained is False

    def test_predict_after_qdrant_training(self) -> None:
        """Given: predictor trained from mock Qdrant. When: predict().
        Then: returns valid probability.
        """
        mock_client = _make_mock_qdrant_with_faults()
        predictor = FaultPredictor()
        predictor.train(mock_client, "ate_failures")

        prob = predictor.get_step_fault_probability("step_0")
        assert 0.0 <= prob <= 1.0
