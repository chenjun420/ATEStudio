"""Automatic failure→KG evolution trigger (task 16).

Bridges the :class:`~ate_cloud.services.failure_indexer.FailureIndexer` to the
existing task-7 :class:`~ate_cloud.services.kg_pipeline.KGPipeline`: once a
real edge failure (STEP_FAILED / EXECUTION_COMPLETED(FAILED) / SPC
ALARM_RAISED) has been persisted to the failure index, the trigger builds a
pipeline :class:`~ate_cloud.services.kg_pipeline.Document` from the failure
metadata and ingests it — extracting/merging new symptom/cause/solution/FMEA
entities and relationships into the FalkorDB ontology graph (via
:class:`GraphService`) and upserting entity vectors to Qdrant.

Hard contract — evolution is BEST-EFFORT and must NEVER break failure
indexing or the request path:

- the trigger is invoked after the failure point is upserted;
- every failure mode (no pipeline wired, Semantica/GraphBuilder unavailable,
  graph backend down, no LLM key) is caught here and logged, returning
  ``False`` instead of raising;
- with no LLM key the pipeline's key-free pattern extractors still run and
  graph writes still proceed;
- idempotency comes from a deterministic document id (uuid5 over the stable
  failure identity) plus the pipeline's ``MERGE`` on stable ids, so indexing
  the same failure repeatedly never duplicates graph state.

The manual ``POST /faults/evolve`` endpoint (KGEvolution feedback path) is
unaffected — this module only adds the automatic, index-triggered path.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ate_cloud.services.kg_pipeline import Document

logger = logging.getLogger(__name__)

# Source label stamped on auto-evolved documents (provenance / id namespacing).
SOURCE_LABEL = "auto_failure_evolution"

# Metadata fields that, together, uniquely identify an observed failure.
# Order matters only for a stable canonical string; values are lowercased.
_IDENTITY_FIELDS: tuple[str, ...] = (
    "event_type",
    "alarm_id",
    "run_id",
    "failed_step_id",
    "failed_step_name",
    "error_message",
    "message",
    "measurement_name",
    "rule",
    "plan_name",
)

# Metadata fields concatenated into the text the extractors read.
_TEXT_FIELDS: tuple[str, ...] = (
    "failed_step_name",
    "measurement_name",
    "product_type",
    "rule",
    "severity",
    "error_message",
    "message",
    "plan_name",
)

# Scalar fields carried as document provenance (vectors payload / tracing).
_PROVENANCE_FIELDS: tuple[str, ...] = (
    "run_id",
    "event_type",
    "failed_step_id",
    "failed_step_name",
    "plan_name",
    "product_type",
    "alarm_id",
    "measurement_name",
)


def _stable_doc_id(metadata: dict[str, Any]) -> str:
    """Deterministic document id for a failure (uuid5 over its identity)."""
    parts = [
        f"{key}={str(metadata[key]).strip().lower()}"
        for key in _IDENTITY_FIELDS
        if metadata.get(key) is not None and str(metadata[key]).strip()
    ]
    digest = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts))
    return f"auto-fail:{digest}"


def _evolution_text(metadata: dict[str, Any]) -> str:
    """Build the text fed to extraction from meaningful failure fields."""
    parts = [
        str(metadata[key])
        for key in _TEXT_FIELDS
        if metadata.get(key) is not None and str(metadata[key]).strip()
    ]
    snapshot = metadata.get("variable_snapshot")
    if snapshot:
        parts.append(str(snapshot))
    return " ".join(parts)


def build_failure_document(metadata: dict[str, Any]) -> Document:
    """Map indexed failure metadata onto a pipeline :class:`Document`.

    The document id is deterministic for identical failure content, so
    re-ingestion upserts/merges instead of duplicating (idempotent).
    """
    provenance = {
        key: metadata[key]
        for key in _PROVENANCE_FIELDS
        if metadata.get(key) is not None
    }
    return Document(
        doc_id=_stable_doc_id(metadata),
        text=_evolution_text(metadata),
        source=SOURCE_LABEL,
        metadata=provenance,
    )


# A resolver builds (or returns a cached) pipeline lazily; None means
# "evolution unavailable" (e.g. no graph / Semantica unusable).
PipelineResolver = Callable[[], Any | None]
EvolutionHook = Callable[[dict[str, Any]], Awaitable[bool]]


class FailureEvolutionTrigger:
    """Best-effort hook that evolves the KG after a failure is indexed.

    Args:
        pipeline: A ready :class:`~ate_cloud.services.kg_pipeline.KGPipeline`
            (or compatible). When provided it is used for every failure.
        resolve: Optional zero-arg callable returning a pipeline (or ``None``)
            when no fixed pipeline was injected — lets the app build the
            pipeline lazily and cache it. Any resolver exception degrades to
            a logged skip.
    """

    def __init__(
        self,
        pipeline: Any | None = None,
        resolve: PipelineResolver | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._resolve = resolve

    def _resolve_pipeline(self) -> Any | None:
        """Return the injected pipeline, or lazily resolve one (never raises)."""
        if self._pipeline is not None:
            return self._pipeline
        if self._resolve is None:
            return None
        try:
            return self._resolve()
        except Exception as e:  # noqa: BLE001 — degrade, never break indexing
            logger.warning("KG pipeline resolution failed; auto evolution skipped: %s", e)
            return None

    async def evolve_from_failure(self, metadata: dict[str, Any]) -> bool:
        """Ingest one indexed failure into the KG. Returns True on success.

        Never raises: any failure (no pipeline, construction/ingest error,
        graph or vector backend down) is logged and returns ``False`` so the
        failure-indexing / request path is unaffected.
        """
        try:
            pipeline = self._resolve_pipeline()
            if pipeline is None:
                logger.debug("No KG pipeline available; auto evolution skipped")
                return False

            document = build_failure_document(metadata)
            if not document.text.strip():
                logger.debug(
                    "Indexed failure %s has no evolvable text; skipping", document.doc_id
                )
                return False

            await pipeline.ingest(document)
            logger.info(
                "Auto-evolved KG from indexed failure %s (mode via task-7 pipeline)",
                document.doc_id,
            )
            return True
        except Exception as e:  # noqa: BLE001 — boundary: evolution is non-fatal
            logger.warning(
                "Auto KG evolution failed for indexed failure (indexing unaffected): %s",
                e,
            )
            return False


__all__ = [
    "EvolutionHook",
    "FailureEvolutionTrigger",
    "PipelineResolver",
    "build_failure_document",
]
