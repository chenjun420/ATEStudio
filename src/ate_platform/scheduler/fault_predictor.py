"""FaultPredictor — lightweight classifier for per-step fault probability.

Trains a numpy-based logistic regression classifier on historical fault data
extracted from the Qdrant failure index. Features include product type, test
step, time-of-day, instrument id, and recent failure count. The predictor
outputs a probability [0.0, 1.0] per step, which is consumed by
FaultPenaltyIntegrator to inject soft-constraint penalties into the CP-SAT
scheduler.

Design decisions:
- Pure numpy implementation (no sklearn/xgboost dependency required).
- Categorical features are hash-encoded into a fixed-size feature vector.
- Feature extraction is fully mockable: the ``FeatureExtractor`` protocol
  allows tests to inject synthetic features without a live Qdrant instance.
- The classifier is a simple logistic regression trained via gradient descent,
  which is sufficient for distinguishing fault-prone steps from safe ones.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature vector dimension — hash-encoded categorical features + numerical
# ---------------------------------------------------------------------------
_FEATURE_HASH_DIM = 32
_NUMERICAL_FEATURE_COUNT = 2  # time_of_day (0-23), recent_failure_count
_FEATURE_VECTOR_SIZE = _FEATURE_HASH_DIM + _NUMERICAL_FEATURE_COUNT


class FeatureExtractor(Protocol):
    """Protocol for extracting features from Qdrant historical fault data."""

    def extract_training_data(
        self,
        qdrant_client: Any,
        collection_name: str,
    ) -> list[dict[str, Any]]:
        """Extract training samples from Qdrant fault index.

        Args:
            qdrant_client: Qdrant client (or mock).
            collection_name: Qdrant collection name.

        Returns:
            List of sample dicts, each containing:
              - product_type (str)
              - test_step (str)
              - time_of_day (int 0-23)
              - instrument_id (str)
              - recent_failure_count (int)
              - label (int: 1 = fault, 0 = success)
        """
        ...

    def extract_step_features(
        self,
        step_id: str,
        product_type: str = "",
        time_of_day: int = 12,
        instrument_id: str = "",
        recent_failure_count: int = 0,
    ) -> dict[str, Any]:
        """Extract features for a single step to predict.

        Args:
            step_id: Step identifier.
            product_type: Product type string.
            time_of_day: Hour of day (0-23).
            instrument_id: Instrument identifier.
            recent_failure_count: Recent failures for this step/product.

        Returns:
            Feature dict with the same keys as training samples.
        """
        ...


# ---------------------------------------------------------------------------
# Default feature extractor — works with Qdrant payloads or mock data
# ---------------------------------------------------------------------------


class DefaultFeatureExtractor:
    """Default implementation of FeatureExtractor.

    Extracts training data by scrolling the Qdrant collection and building
    feature dicts from payload metadata. For step prediction, builds a feature
    dict from the provided parameters.
    """

    def extract_training_data(
        self,
        qdrant_client: Any,
        collection_name: str,
    ) -> list[dict[str, Any]]:
        """Scroll Qdrant collection to extract all stored fault records.

        Each Qdrant payload (from FailureIndexer) contains fields like
        failed_step_id, failed_step_name, error_message, timestamp, etc.
        We build training samples where each fault record is a positive
        example (label=1). Negative examples (label=0) are synthesized
        by pairing each fault with a different step_id from the same
        collection at a different time.
        """
        samples: list[dict[str, Any]] = []
        try:
            # Scroll all points from the collection
            points, _offset = qdrant_client.scroll(
                collection_name=collection_name,
                limit=10000,
                with_payload=True,
                with_vectors=False,
            )

            fault_step_ids: set[str] = set()
            for point in points:
                payload = point.payload or {}
                step_id = str(payload.get("failed_step_id", payload.get("failed_step_name", "")))
                if not step_id:
                    continue

                fault_step_ids.add(step_id)
                timestamp_str = str(payload.get("timestamp", ""))
                hour = _parse_hour_from_timestamp(timestamp_str)

                samples.append({
                    "product_type": str(payload.get("plan_name", "unknown")),
                    "test_step": step_id,
                    "time_of_day": hour,
                    "instrument_id": str(payload.get("instrument_id", "default")),
                    "recent_failure_count": 1,
                    "label": 1,
                })

            # Synthesize negative examples: for each fault step, create a
            # success sample with a different time and zero recent failures.
            for step_id in fault_step_ids:
                samples.append({
                    "product_type": "unknown",
                    "test_step": step_id,
                    "time_of_day": 6,  # early morning — typically fewer faults
                    "instrument_id": "default",
                    "recent_failure_count": 0,
                    "label": 0,
                })
        except Exception as e:
            logger.warning("Failed to extract training data from Qdrant: %s", e)
            return []

        return samples

    def extract_step_features(
        self,
        step_id: str,
        product_type: str = "",
        time_of_day: int = 12,
        instrument_id: str = "",
        recent_failure_count: int = 0,
    ) -> dict[str, Any]:
        """Build a feature dict for a single step prediction."""
        return {
            "product_type": product_type,
            "test_step": step_id,
            "time_of_day": time_of_day,
            "instrument_id": instrument_id,
            "recent_failure_count": recent_failure_count,
        }


# ---------------------------------------------------------------------------
# Feature vectorization — hash-based encoding for categorical features
# ---------------------------------------------------------------------------


def _hash_to_index(key: str, dim: int) -> int:
    """Hash a string key to an index in [0, dim)."""
    h = hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(h[:8], 16) % dim


def vectorize_features(sample: dict[str, Any]) -> np.ndarray:
    """Convert a feature dict into a fixed-size numpy vector.

    Categorical features (product_type, test_step, instrument_id) are
    hash-encoded into the first _FEATURE_HASH_DIM slots. Each categorical
    feature contributes one hash slot. Numerical features (time_of_day,
    recent_failure_count) occupy the last _NUMERICAL_FEATURE_COUNT slots.
    """
    vec = np.zeros(_FEATURE_VECTOR_SIZE, dtype=np.float64)

    # Categorical: hash-encode into shared hash space
    categorical_keys = [
        f"pt:{sample.get('product_type', '')}",
        f"ts:{sample.get('test_step', '')}",
        f"inst:{sample.get('instrument_id', '')}",
    ]
    for key in categorical_keys:
        idx = _hash_to_index(key, _FEATURE_HASH_DIM)
        vec[idx] = 1.0

    # Numerical features (normalized)
    time_of_day = float(sample.get("time_of_day", 12))
    recent_failures = float(sample.get("recent_failure_count", 0))
    vec[_FEATURE_HASH_DIM] = time_of_day / 23.0
    vec[_FEATURE_HASH_DIM + 1] = min(recent_failures / 10.0, 1.0)

    return vec


# ---------------------------------------------------------------------------
# FaultPredictor — numpy logistic regression
# ---------------------------------------------------------------------------


@dataclass
class FaultPredictor:
    """Lightweight fault probability predictor using numpy logistic regression.

    Trains on historical fault data from Qdrant and predicts per-step fault
    probability. Uses gradient descent for logistic regression — no sklearn
    or xgboost dependency required.

    Attributes:
        _weights: Trained weight vector (set after train()).
        _bias: Trained bias term (set after train()).
        _trained: Whether train() has been called successfully.
        _feature_extractor: FeatureExtractor instance for data extraction.
    """

    _feature_extractor: FeatureExtractor = field(default_factory=DefaultFeatureExtractor)
    _weights: np.ndarray | None = field(default=None, repr=False)
    _bias: float = field(default=0.0, repr=False)
    _trained: bool = field(default=False, repr=False)
    _learning_rate: float = field(default=0.01, repr=False)
    _epochs: int = field(default=200, repr=False)

    def train(
        self,
        qdrant_client: Any,
        collection_name: str = "ate_failures",
        extractor: FeatureExtractor | None = None,
    ) -> None:
        """Train the logistic regression classifier on Qdrant fault data.

        Extracts training samples from the Qdrant fault index via the
        FeatureExtractor, vectorizes them, and runs gradient descent.

        Args:
            qdrant_client: Qdrant client instance (or mock).
            collection_name: Qdrant collection name for fault records.
            extractor: Optional FeatureExtractor override (for testing).
        """
        ext = extractor or self._feature_extractor
        samples = ext.extract_training_data(qdrant_client, collection_name)

        if not samples:
            logger.warning("No training data extracted from Qdrant — predictor untrained")
            self._trained = False
            return

        # Build feature matrix and label vector
        x_matrix = np.array([vectorize_features(s) for s in samples])
        y_labels = np.array([float(s.get("label", 0)) for s in samples])

        # Check if we have both classes
        unique_labels = set(y_labels.tolist())
        if len(unique_labels) < 2:
            logger.warning(
                "Training data has only one class (%s) — predictor untrained",
                unique_labels,
            )
            self._trained = False
            return

        # Gradient descent logistic regression
        n_features = x_matrix.shape[1]
        self._weights = np.zeros(n_features, dtype=np.float64)
        self._bias = 0.0

        for _epoch in range(self._epochs):
            logits = x_matrix @ self._weights + self._bias
            predictions = _sigmoid(logits)
            errors = predictions - y_labels

            grad_w = (x_matrix.T @ errors) / len(samples)
            grad_b = float(np.mean(errors))

            self._weights -= self._learning_rate * grad_w
            self._bias -= self._learning_rate * grad_b

        self._trained = True
        logger.info(
            "FaultPredictor trained on %d samples (weights norm=%.4f)",
            len(samples),
            float(np.linalg.norm(self._weights)),
        )

    def train_from_samples(self, samples: list[dict[str, Any]]) -> None:
        """Train directly from pre-extracted sample dicts.

        This is the primary entry point for unit tests — no Qdrant required.

        Args:
            samples: List of feature dicts with 'label' key.
        """
        if not samples:
            self._trained = False
            return

        x_matrix = np.array([vectorize_features(s) for s in samples])
        y_labels = np.array([float(s.get("label", 0)) for s in samples])

        unique_labels = set(y_labels.tolist())
        if len(unique_labels) < 2:
            self._trained = False
            return

        n_features = x_matrix.shape[1]
        self._weights = np.zeros(n_features, dtype=np.float64)
        self._bias = 0.0

        for _epoch in range(self._epochs):
            logits = x_matrix @ self._weights + self._bias
            predictions = _sigmoid(logits)
            errors = predictions - y_labels

            grad_w = (x_matrix.T @ errors) / len(samples)
            grad_b = float(np.mean(errors))

            self._weights -= self._learning_rate * grad_w
            self._bias -= self._learning_rate * grad_b

        self._trained = True

    def predict(self, step_features: dict[str, Any]) -> float:
        """Predict fault probability for a single step.

        Args:
            step_features: Feature dict with keys matching training samples.

        Returns:
            Fault probability in [0.0, 1.0]. Returns 0.5 (uncertain) if
            the predictor has not been trained.
        """
        if not self._trained or self._weights is None:
            return 0.5

        vec = vectorize_features(step_features)
        logits = float(np.dot(vec, self._weights) + self._bias)
        return float(_sigmoid_scalar(logits))

    def get_step_fault_probability(
        self,
        step_id: str,
        product_type: str = "",
        time_of_day: int = 12,
        instrument_id: str = "",
        recent_failure_count: int = 0,
    ) -> float:
        """Convenience method: build features from params and predict.

        Args:
            step_id: Step identifier.
            product_type: Product type string.
            time_of_day: Hour of day (0-23).
            instrument_id: Instrument identifier.
            recent_failure_count: Recent failures for this step/product.

        Returns:
            Fault probability in [0.0, 1.0].
        """
        features = self._feature_extractor.extract_step_features(
            step_id=step_id,
            product_type=product_type,
            time_of_day=time_of_day,
            instrument_id=instrument_id,
            recent_failure_count=recent_failure_count,
        )
        return self.predict(features)

    @property
    def is_trained(self) -> bool:
        """Whether the predictor has been successfully trained."""
        return self._trained

    def get_step_probabilities(
        self,
        step_ids: list[str],
        product_type: str = "",
        time_of_day: int = 12,
        instrument_id: str = "",
        recent_failure_counts: dict[str, int] | None = None,
    ) -> dict[str, float]:
        """Batch predict fault probabilities for multiple steps.

        Args:
            step_ids: List of step IDs to predict for.
            product_type: Product type for all steps.
            time_of_day: Hour of day for all steps.
            instrument_id: Instrument for all steps.
            recent_failure_counts: Optional per-step recent failure counts.

        Returns:
            Dict mapping step_id → fault probability.
        """
        rfc = recent_failure_counts or {}
        return {
            sid: self.get_step_fault_probability(
                step_id=sid,
                product_type=product_type,
                time_of_day=time_of_day,
                instrument_id=instrument_id,
                recent_failure_count=rfc.get(sid, 0),
            )
            for sid in step_ids
        }


# ---------------------------------------------------------------------------
# Numerical helpers
# ---------------------------------------------------------------------------


def _sigmoid(x: np.ndarray) -> np.ndarray:
    """Numerically stable sigmoid for numpy arrays."""
    result = np.empty_like(x)
    positive = x >= 0
    result[positive] = 1.0 / (1.0 + np.exp(-x[positive]))
    exp_x = np.exp(x[~positive])
    result[~positive] = exp_x / (1.0 + exp_x)
    return result


def _sigmoid_scalar(x: float) -> float:
    """Numerically stable sigmoid for a single float."""
    if x >= 0:
        return 1.0 / (1.0 + float(np.exp(-x)))
    exp_x = float(np.exp(x))
    return exp_x / (1.0 + exp_x)


def _parse_hour_from_timestamp(timestamp_str: str) -> int:
    """Extract hour-of-day from an ISO timestamp string.

    Returns 12 (noon) as a neutral default if parsing fails.
    """
    if not timestamp_str:
        return 12
    # ISO format: 2026-01-15T14:30:00 or similar
    if "T" in timestamp_str:
        time_part = timestamp_str.split("T")[1]
        try:
            return int(time_part[:2])
        except (ValueError, IndexError):
            return 12
    return 12
