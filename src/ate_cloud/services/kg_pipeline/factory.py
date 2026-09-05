"""Factory wiring application settings into the KGPipeline facade."""

from __future__ import annotations

from typing import Any

from ate_cloud.services.kg_pipeline.models import PipelineConfig
from ate_cloud.services.kg_pipeline.pipeline import KGPipeline


def build_pipeline(
    graph_service: Any,
    embedding_service: Any | None = None,
    qdrant_client: Any | None = None,
    config: PipelineConfig | None = None,
) -> KGPipeline:
    """Construct a :class:`KGPipeline` from app settings + injected clients.

    Reads LLM/vector settings from :mod:`ate_cloud.config` when ``config`` is
    omitted. Raises :class:`~ate_cloud.services.kg_pipeline.errors.KGPipelineUnavailable`
    if Semantica is unusable (callers map to 503; the app still boots).
    """
    if config is None:
        from ate_cloud.config import settings

        config = PipelineConfig(
            llm_api_key=settings.openai_api_key or None,
            llm_model=settings.openai_model,
            llm_base_url=settings.openai_base_url or None,
            embedding_dim=settings.embedding_dimensions,
        )
    return KGPipeline(
        config=config,
        graph_service=graph_service,
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
    )


__all__ = ["build_pipeline"]
