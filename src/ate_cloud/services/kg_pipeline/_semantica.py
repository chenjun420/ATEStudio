"""Semantica adapter — the ONLY module in the application that imports semantica.

Every Semantica import lives here so the rest of the codebase depends solely on
the :mod:`ate_cloud.services.kg_pipeline` facade. Importing this module (or
constructing a stage) is wrapped by the facade: a Semantica import/construction
failure is converted to :class:`~ate_cloud.services.kg_pipeline.errors.KGPipelineUnavailable`
so callers can degrade to a 503 while the app keeps booting.

Verified against semantica 0.6.7:
- ``semantica.kg.GraphBuilder(merge_entities=..., resolve_conflicts=...)``
  ``.build({"entities": [...], "relationships": [...]})`` → merged graph dict.
- ``semantica.semantic_extract.NERExtractor / RelationExtractor / TripletExtractor``
  with ``method="pattern"|"llm"``; ``.extract(...)`` returns Entity/Relation/Triplet.
- ``semantica.semantic_extract.types`` exposes ``Entity`` / ``Relation`` / ``Triplet``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _load_graph_builder() -> Any:
    """Construct a merge/dedup-enabled Semantica GraphBuilder.

    Isolated as a function (rather than imported at module top of the facade)
    so the pipeline can wrap construction failure and tests can force a raise.
    """
    from semantica.kg import GraphBuilder

    return GraphBuilder(
        merge_entities=True,
        resolve_conflicts=True,
        entity_resolution_strategy="fuzzy",
    )


def build_merged_graph(
    builder: Any,
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run Semantica GraphBuilder conflict/dedup over extracted elements.

    Returns the merged graph dict ``{"entities": [...], "relationships": [...],
    "metadata": {...}}``.
    """
    payload = {"entities": entities, "relationships": relationships}
    result = builder.build(payload)
    if not isinstance(result, dict):  # defensive: Semantica contract is a dict
        msg = "GraphBuilder.build returned a non-dict result"
        raise TypeError(msg)
    return result


def relations_to_triplets(
    text: str,
    entities: list[Any],
    relations: list[Any],
) -> list[Any]:
    """Derive canonical Semantica Triplets from entities + relations.

    Uses ``TripletExtractor(method="pattern")``; triplets feed provenance and
    the future ontology-enrichment stage.
    """
    from semantica.semantic_extract import TripletExtractor

    extractor = TripletExtractor(method="pattern")
    return list(extractor.extract(text, entities, relations))


class SemanticaLLMExtractor:
    """Production LLM extraction stage backed by Semantica LLM extractors.

    Constructed only when an API key is configured. Uses Semantica's
    OpenAI provider (``method="llm"``) for both entities and relations.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        from semantica.semantic_extract import NERExtractor, RelationExtractor

        common: dict[str, Any] = {
            "provider": "openai",
            "api_key": api_key,
            "llm_model": model,
        }
        if base_url:
            common["base_url"] = base_url
        self._ner = NERExtractor(method="llm", **common)
        self._rel = RelationExtractor(method="llm", **common)

    def extract(self, text: str) -> dict[str, Any]:
        """Run LLM entity + relation extraction and map to plain dicts."""
        entities_raw = self._ner.extract(text)
        entities = [
            {
                "id": f"llm-{i}",
                "name": getattr(e, "text", str(e)),
                "type": getattr(e, "label", "Entity"),
                "confidence": getattr(e, "confidence", 1.0),
            }
            for i, e in enumerate(entities_raw)
        ]
        relations_raw = self._rel.extract(text, entities_raw)
        relationships = [
            {
                "source": getattr(r.subject, "text", str(r.subject)),
                "target": getattr(r.object, "text", str(r.object)),
                "type": str(r.predicate).upper().replace(" ", "_"),
                "confidence": getattr(r, "confidence", 1.0),
            }
            for r in relations_raw
        ]
        return {"entities": entities, "relationships": relationships}


__all__ = [
    "SemanticaLLMExtractor",
    "build_merged_graph",
    "relations_to_triplets",
]
