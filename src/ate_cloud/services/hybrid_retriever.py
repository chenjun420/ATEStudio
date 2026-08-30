"""Hybrid Retriever — Qdrant vector + Neo4j graph fusion with RRF and query rewriting.

Combines two complementary retrieval strategies for electronics test fault
diagnosis:

1. **Qdrant semantic similarity** — finds past failures with similar embedding
   vectors (text-level semantic match).
2. **Neo4j relationship reasoning** — traverses the FMEA knowledge graph to
   find symptom → cause → solution paths (structural/causal match).

Results are fused using **Reciprocal Rank Fusion (RRF)** with ``k=60``, the
standard parameter that balances rank position vs. score magnitude. An
optional re-ranking step re-sorts fused results by semantic similarity to
the original query.

**Golden-Retriever query rewriting** disambiguates domain jargon (I2C, SPI,
BGA, ESD, etc.) before retrieval. The LLM augmentation step is protected by
a CircuitBreaker; if the LLM is unavailable, the dictionary-expanded query
is used directly (the dictionary step is local and deterministic).

Per AGENTS.md section 7: Qdrant and Neo4j calls are protected by
CircuitBreakers. If either service is configured but unreachable, the
CircuitBreaker opens and ``CircuitBreakerOpenError`` propagates from that
retrieval branch — ``search()`` catches the error and returns results from
the surviving branch (a single-source result is still a valid answer; an
empty result list signals total failure).
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from ate_cloud.config import settings
from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.neo4j_graph_service import Neo4jGraphService
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

#: RRF constant — standard value from the original paper (Cormack et al. 2009).
#: Balances rank position influence vs. raw score; k=60 is the widely used default.
_RRF_K: int = 60

#: System prompt for LLM query rewriting — instructs it to output only the rewritten query.
_REWRITE_SYSTEM_PROMPT = (
    "You are an electronics test engineering expert. "
    "Rewrite the following fault query to include disambiguated domain terms. "
    "Expand abbreviations and add relevant technical context. "
    "Output ONLY the rewritten query text, nothing else."
)

#: Domain dictionary for electronics testing jargon (Golden-Retriever pattern).
#: Maps abbreviations to full expansions (English + Chinese gloss).
_DOMAIN_DICTIONARY: dict[str, str] = {
    "I2C": "Inter-Integrated Circuit (two-wire serial communication protocol)",
    "SPI": "Serial Peripheral Interface (synchronous serial communication protocol)",
    "UART": "Universal Asynchronous Receiver-Transmitter (serial communication)",
    "USART": "Universal Synchronous/Asynchronous Receiver-Transmitter",
    "Vreg": "Voltage Regulator (voltage stabilization component)",
    "BGA": "Ball Grid Array (integrated circuit package type)",
    "PCB": "Printed Circuit Board",
    "SMT": "Surface Mount Technology (component assembly method)",
    "ESD": "Electrostatic Discharge (sudden electrical transfer between objects)",
    "THD": "Total Harmonic Distortion (signal quality metric)",
    "SNR": "Signal-to-Noise Ratio (signal quality metric)",
    "BER": "Bit Error Rate (digital communication quality metric)",
    "JTAG": "Joint Test Action Group (boundary-scan test interface)",
    "CAN": "Controller Area Network (vehicle communication bus)",
    "USB": "Universal Serial Bus",
    "PCIe": "PCI Express (high-speed serial computer expansion bus)",
    "DDR": "Double Data Rate (synchronous dynamic RAM)",
    "GPIO": "General-Purpose Input/Output",
    "ADC": "Analog-to-Digital Converter",
    "DAC": "Digital-to-Analog Converter",
    "PWM": "Pulse Width Modulation",
    "RF": "Radio Frequency",
    "EMI": "Electromagnetic Interference",
    "EMC": "Electromagnetic Compatibility",
    "DMM": "Digital Multimeter",
    "OSC": "Oscilloscope",
    "PSU": "Power Supply Unit",
    "DUT": "Device Under Test",
    "FMEA": "Failure Mode and Effects Analysis",
    "HALT": "Highly Accelerated Life Test",
    "HASS": "Highly Accelerated Stress Screening",
    "ICT": "In-Circuit Test",
    "FCT": "Functional Circuit Test",
    "BODE": "Bode plot (frequency response analysis)",
    "LDO": "Low Dropout (linear voltage regulator)",
    "SOC": "System on Chip",
    "FPGA": "Field-Programmable Gate Array",
    "ASIC": "Application-Specific Integrated Circuit",
    "MCU": "Microcontroller Unit",
    "EEPROM": "Electrically Erasable Programmable Read-Only Memory",
}


class HybridRetriever:
    """Hybrid retrieval combining Qdrant semantic search with Neo4j causal reasoning.

    Pipeline: query rewriting (dictionary + LLM) -> parallel retrieval
    (Qdrant + Neo4j) -> Reciprocal Rank Fusion (k=60) -> optional re-ranking.

    All external calls (Qdrant, Neo4j via Neo4jGraphService, OpenAI LLM)
    are protected by CircuitBreakers (failure_threshold=5, timeout=30s).
    The EmbeddingService and Neo4jGraphService have their own internal
    breakers; this class adds breakers for direct Qdrant calls and the
    LLM query-rewriting call.

    Args:
        embedding_service: EmbeddingService for computing query/result vectors.
        neo4j_service: Neo4jGraphService for Cypher relationship queries.
        qdrant_client: Qdrant client instance (or compatible mock).
        collection_name: Qdrant collection name (defaults to settings).
        api_key: OpenAI API key for LLM query rewriting (defaults to settings).
        model: Chat model name for query rewriting.
        embedding_dim: Expected embedding vector dimensionality.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        neo4j_service: Neo4jGraphService,
        qdrant_client: Any,
        collection_name: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        self._embedding_service = embedding_service
        self._neo4j_service = neo4j_service
        self._qdrant_client = qdrant_client
        self._collection_name = collection_name or settings.qdrant_collection_failures
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.openai_model
        self._embedding_dim = embedding_dim or settings.embedding_dimensions

        self._qdrant_breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="qdrant-hybrid-retriever",
        )
        self._llm_breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="llm-query-rewriter",
        )

        self._llm: Any = None
        self._prompt: Any = None
        self._initialized = False

    @property
    def qdrant_circuit_breaker(self) -> CircuitBreaker:
        """CircuitBreaker protecting direct Qdrant calls."""
        return self._qdrant_breaker

    @property
    def llm_circuit_breaker(self) -> CircuitBreaker:
        """CircuitBreaker protecting LLM query-rewriting calls."""
        return self._llm_breaker

    # ── Public API ──────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        top_k: int = 10,
        *,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        """Hybrid search combining Qdrant + Neo4j with RRF fusion.

        Pipeline:
        1. Rewrite query (dictionary expansion + LLM augmentation).
        2. Embed the rewritten query.
        3. Parallel retrieval: Qdrant semantic + Neo4j relationship.
        4. Reciprocal Rank Fusion (k=60).
        5. Optional re-ranking by semantic similarity.

        Args:
            query: Natural-language fault description or error text.
            top_k: Maximum number of results to return.
            rerank: If True, re-rank fused results by semantic similarity
                to the original query.

        Returns:
            List of result dicts, each containing ``rrf_score``, ``source``
            (``"qdrant"``, ``"neo4j"``, or ``"fused"``), and payload fields.
            Results from both sources may appear; fused entries merge
            Qdrant semantic scores with Neo4j relationship paths.
        """
        # 1. Query rewriting
        rewritten = await self._rewrite_query(query)

        # 2. Embed rewritten query
        query_vector = await self._embedding_service.embed(rewritten)

        # 3. Parallel retrieval — gather with return_exceptions so one
        #    branch failing doesn't kill the other.
        qdrant_raw: list[dict[str, Any]] | Exception
        neo4j_raw: list[dict[str, Any]] | Exception
        qdrant_raw, neo4j_raw = await asyncio.gather(
            self._search_qdrant(query_vector, top_k),
            self._search_neo4j(rewritten, top_k),
            return_exceptions=True,
        )

        qdrant_results: list[dict[str, Any]] = []
        if isinstance(qdrant_raw, Exception):
            logger.warning("Qdrant retrieval failed: %s", qdrant_raw)
        else:
            qdrant_results = qdrant_raw

        neo4j_results: list[dict[str, Any]] = []
        if isinstance(neo4j_raw, Exception):
            logger.warning("Neo4j retrieval failed: %s", neo4j_raw)
        else:
            neo4j_results = neo4j_raw

        # 4. RRF fusion
        fused = self._reciprocal_rank_fusion(qdrant_results, neo4j_results, k=_RRF_K)

        # 5. Optional re-ranking
        if rerank and fused:
            fused = await self._rerank(fused, query)

        return fused[:top_k]

    # ── Query Rewriting (Golden-Retriever pattern) ──────────────────

    async def _rewrite_query(self, query: str) -> str:
        """Rewrite a query using the Golden-Retriever pattern.

        Steps:
        1. Identify domain jargon in the query (case-insensitive matching).
        2. Disambiguate using the local domain dictionary.
        3. Augment the query with dictionary expansions.
        4. Call LLM (via CircuitBreaker) for natural-language augmentation.

        If the LLM is unavailable (breaker open or API error), the
        dictionary-expanded query is returned. This is not silent
        degradation — the dictionary step is a deterministic local
        component of the Golden-Retriever pattern, and the failure is
        logged.

        Args:
            query: Original user query.

        Returns:
            Rewritten/augmented query string.
        """
        # Step 1-2: Identify and disambiguate domain jargon
        expansions = self._lookup_domain_terms(query)

        # Step 3: Augment query with dictionary expansions
        augmented = query
        if expansions:
            expansion_text = "; ".join(f"{abbr} = {full}" for abbr, full in expansions)
            augmented = f"{query} ({expansion_text})"

        # Step 4: LLM augmentation via CircuitBreaker
        if not self._api_key:
            logger.debug("No OpenAI API key; using dictionary-expanded query")
            return augmented

        try:
            self._ensure_initialized()
            llm_result = await self._call_llm_rewrite(augmented)
            if llm_result.strip():
                return llm_result.strip()
            return augmented
        except CircuitBreakerOpenError:
            logger.warning(
                "LLM circuit breaker open; using dictionary-expanded query"
            )
            return augmented
        except Exception as e:
            logger.warning("LLM query rewriting failed: %s; using dictionary-expanded query", e)
            return augmented

    def _lookup_domain_terms(self, query: str) -> list[tuple[str, str]]:
        """Find domain jargon in the query and return (abbreviation, expansion) pairs.

        Uses case-insensitive word-boundary matching to find known abbreviations.
        Returns results in order of first appearance.

        Args:
            query: The query text to scan.

        Returns:
            List of (abbreviation, full_expansion) tuples for matched terms.
        """
        query_upper = query.upper()
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for abbr, full in _DOMAIN_DICTIONARY.items():
            # Word-boundary match: check the abbreviation appears as a
            # standalone token (not a substring of a longer word).
            abbr_upper = abbr.upper()
            if abbr_upper in query_upper and abbr_upper not in seen:
                # Verify word boundary — the char before/after should be
                # non-alphanumeric or string start/end.
                idx = query_upper.find(abbr_upper)
                before_ok = idx == 0 or not query_upper[idx - 1].isalnum()
                after_idx = idx + len(abbr_upper)
                after_ok = after_idx >= len(query_upper) or not query_upper[after_idx].isalnum()
                if before_ok and after_ok:
                    found.append((abbr, full))
                    seen.add(abbr_upper)
        return found

    def _ensure_initialized(self) -> None:
        """Lazily initialize LangChain LLM and prompt template (deferred import)."""
        if self._initialized:
            return
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        kwargs: dict[str, Any] = {
            "model": self._model,
            "api_key": SecretStr(self._api_key),
            "temperature": 0,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self._llm = ChatOpenAI(**kwargs)
        self._prompt = ChatPromptTemplate.from_messages([
            ("system", _REWRITE_SYSTEM_PROMPT),
            ("human", "{query}"),
        ])
        self._initialized = True

    async def _call_llm_rewrite(self, query: str) -> str:
        """Call the LLM to rewrite the query, protected by CircuitBreaker.

        Args:
            query: Dictionary-expanded query to feed to the LLM.

        Returns:
            LLM-rewritten query string.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
            Exception: Any LLM API error not suppressed by the breaker.
        """
        async def _do_rewrite() -> str:
            messages = self._prompt.format_messages(query=query)
            response = await self._llm.ainvoke(messages)
            return str(response.content)

        return await self._llm_breaker.call(_do_rewrite)

    # ── Qdrant Semantic Search ──────────────────────────────────────

    async def _search_qdrant(
        self,
        query_vector: list[float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Search Qdrant for semantically similar fault cases.

        Args:
            query_vector: Embedding vector of the (rewritten) query.
            top_k: Maximum number of results.

        Returns:
            List of result dicts with ``id``, ``score``, ``source="qdrant"``,
            and payload fields from the stored fault case.

        Raises:
            CircuitBreakerOpenError: If the Qdrant circuit is OPEN.
        """
        async def _do_search() -> list[dict[str, Any]]:
            results = self._qdrant_client.search(
                collection_name=self._collection_name,
                query_vector=query_vector,
                limit=top_k,
                with_payload=True,
            )
            return [
                {
                    "id": str(r.id),
                    "score": float(r.score),
                    "source": "qdrant",
                    **({} if r.payload is None else r.payload),
                }
                for r in results
            ]

        return await self._qdrant_breaker.call(_do_search)

    # ── Neo4j Relationship Reasoning ────────────────────────────────

    async def _search_neo4j(
        self,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Search Neo4j for fault symptom -> cause -> solution relationship paths.

        Extracts keywords from the query and searches the FMEA knowledge graph
        for matching symptoms, their causes, solutions, and affected components.

        Args:
            query: Rewritten query text.
            top_k: Maximum number of results.

        Returns:
            List of result dicts with ``id``, ``score=0.0`` (Neo4j has no
            similarity score; rank is determined by graph traversal order),
            ``source="neo4j"``, and relationship path fields (``symptom``,
            ``cause``, ``solution``, ``component``).

        Raises:
            CircuitBreakerOpenError: If the Neo4j circuit is OPEN (propagated
                from Neo4jGraphService.query()).
        """
        keyword = self._extract_keyword(query)
        cypher = (
            "MATCH (s:FaultSymptom)-[:HAS_CAUSE]->(c:Cause) "
            "WHERE s.name CONTAINS $keyword OR c.name CONTAINS $keyword "
            "OPTIONAL MATCH (c)-[:HAS_SOLUTION]->(sol:Solution) "
            "OPTIONAL MATCH (s)-[:AFFECTS_COMPONENT]->(comp:Component) "
            "RETURN s.name AS symptom, c.name AS cause, "
            "sol.name AS solution, comp.name AS component "
            "LIMIT $limit"
        )
        results = await self._neo4j_service.query(
            cypher,
            {"keyword": keyword, "limit": top_k},
        )
        return [
            {
                "id": f"neo4j-{i}",
                "score": 0.0,
                "source": "neo4j",
                "symptom": r.get("symptom", ""),
                "cause": r.get("cause", ""),
                "solution": r.get("solution", ""),
                "component": r.get("component", ""),
            }
            for i, r in enumerate(results)
        ]

    @staticmethod
    def _extract_keyword(query: str) -> str:
        """Extract the most relevant keyword from a query for Neo4j CONTAINS search.

        Picks the longest non-stopword token from the query, as longer tokens
        are more specific and produce fewer false positives in Cypher CONTAINS.

        Args:
            query: Query text.

        Returns:
            The best keyword string (lowercased), or empty string if query is empty.
        """
        if not query.strip():
            return ""
        # Common English stop words to skip
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "has", "have", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "can", "shall", "to", "of",
            "in", "on", "at", "by", "for", "with", "from", "into", "and",
            "or", "not", "no", "but", "if", "then", "else", "when", "where",
            "what", "which", "who", "how", "why", "that", "this", "these",
            "those", "it", "its", "as", "so", "than", "too", "very",
            "error", "failure", "fault", "fail", "failed", "test", "issue",
        }
        # Tokenize — split on non-alphanumeric
        tokens: list[str] = []
        current: list[str] = []
        for ch in query:
            if ch.isalnum():
                current.append(ch)
            else:
                if current:
                    tokens.append("".join(current))
                    current = []
        if current:
            tokens.append("".join(current))

        # Filter stop words and pick longest remaining token
        candidates = [t for t in tokens if t.lower() not in stop_words and len(t) >= 2]
        if not candidates:
            return query.strip().split()[0].lower() if query.strip() else ""
        # Pick the longest token (most specific)
        best = max(candidates, key=len)
        return best.lower()

    # ── Reciprocal Rank Fusion ──────────────────────────────────────

    def _reciprocal_rank_fusion(
        self,
        results_a: list[dict[str, Any]],
        results_b: list[dict[str, Any]],
        k: int = _RRF_K,
    ) -> list[dict[str, Any]]:
        """Fuse two ranked result lists using Reciprocal Rank Fusion.

        RRF formula: ``score(d) = sum(1 / (k + rank_i(d)))``

        Documents are deduplicated by a text match key derived from their
        most relevant text field. When a Qdrant result and a Neo4j result
        describe the same fault (matched by symptom/error text), they are
        merged — the fused entry keeps fields from both sources and its
        ``source`` is set to ``"fused"``.

        Args:
            results_a: First ranked list (Qdrant), best first.
            results_b: Second ranked list (Neo4j), best first.
            k: RRF constant (default 60).

        Returns:
            Fused list sorted by RRF score (descending). Each dict has
            ``rrf_score``, ``source``, and merged payload fields.
        """
        # Build rank maps: match_key -> (rank, result_dict)
        # Rank starts at 1 (position 0 = rank 1).
        merged: dict[str, dict[str, Any]] = {}

        for rank, result in enumerate(results_a, start=1):
            key = self._match_key(result)
            rrf_score = 1.0 / (k + rank)
            if key in merged:
                existing = merged[key]
                existing["rrf_score"] = existing.get("rrf_score", 0.0) + rrf_score
                # Merge: keep Qdrant score and source becomes "fused"
                existing.setdefault("qdrant_score", result.get("score"))
                existing["source"] = "fused"
                # Keep Neo4j fields if present
            else:
                entry = dict(result)
                entry["rrf_score"] = rrf_score
                entry["qdrant_score"] = result.get("score")
                entry.pop("score", None)
                merged[key] = entry

        for rank, result in enumerate(results_b, start=1):
            key = self._match_key(result)
            rrf_score = 1.0 / (k + rank)
            if key in merged:
                existing = merged[key]
                existing["rrf_score"] = existing.get("rrf_score", 0.0) + rrf_score
                # Merge: add Neo4j relationship fields
                for field in ("symptom", "cause", "solution", "component"):
                    if result.get(field):
                        existing.setdefault(field, result[field])
                existing["neo4j_rank"] = rank
                existing["source"] = "fused" if existing.get("qdrant_score") is not None else "neo4j"
            else:
                entry = dict(result)
                entry["rrf_score"] = rrf_score
                entry["neo4j_rank"] = rank
                entry.pop("score", None)
                merged[key] = entry

        # Sort by RRF score descending
        fused = sorted(merged.values(), key=lambda x: x.get("rrf_score", 0.0), reverse=True)
        return fused

    @staticmethod
    def _match_key(result: dict[str, Any]) -> str:
        """Derive a deduplication match key from a result dict.

        Uses the most relevant text field available, normalized to
        lowercase first-50-chars. Falls back to ``id`` if no text field
        is present (ensures unique entries for results without text).

        Args:
            result: A result dict from Qdrant or Neo4j.

        Returns:
            Normalized match key string.
        """
        # Try text fields in priority order
        for field in ("symptom", "error_message", "failed_step_name", "cause"):
            val = result.get(field)
            if val and isinstance(val, str) and val.strip():
                return val.strip().lower()[:50]
        # Fall back to id (unique, no merge)
        return str(result.get("id", id(result)))

    # ── Re-ranking ──────────────────────────────────────────────────

    async def _rerank(
        self,
        results: list[dict[str, Any]],
        query: str,
    ) -> list[dict[str, Any]]:
        """Re-rank fused results by semantic similarity to the original query.

        Computes cosine similarity between the query embedding and each
        result's text embedding, then re-sorts by similarity score. This
        is a lightweight re-ranking fallback (no GPU cross-encoder required).

        If the embedding service is unavailable (breaker open or error),
        results are returned unchanged with a warning.

        Args:
            results: Fused result list from RRF.
            query: Original (un-rewritten) query for relevance comparison.

        Returns:
            Re-ranked result list with ``rerank_score`` field added.
        """
        if not results:
            return results

        # Build text representations for each result
        texts = [self._result_text(r) for r in results]

        try:
            query_vec = await self._embedding_service.embed(query)
            result_vecs = await self._embedding_service.embed_batch(texts)
        except CircuitBreakerOpenError:
            logger.warning("Embedding service breaker open; skipping re-ranking")
            return results
        except Exception as e:
            logger.warning("Re-ranking embedding failed: %s; skipping re-ranking", e)
            return results

        # Compute cosine similarity for each result
        for result, vec in zip(results, result_vecs, strict=True):
            sim = self._cosine_similarity(query_vec, vec)
            result["rerank_score"] = sim

        # Sort by rerank_score descending, then by rrf_score as tiebreaker
        return sorted(
            results,
            key=lambda x: (x.get("rerank_score", 0.0), x.get("rrf_score", 0.0)),
            reverse=True,
        )

    @staticmethod
    def _result_text(result: dict[str, Any]) -> str:
        """Build a text representation of a result for embedding.

        Concatenates available text fields (symptom, cause, solution,
        error_message, failed_step_name) into a single string.

        Args:
            result: A result dict.

        Returns:
            Concatenated text string.
        """
        parts: list[str] = []
        for field in ("symptom", "cause", "solution", "error_message", "failed_step_name"):
            val = result.get(field)
            if val and isinstance(val, str) and val.strip():
                parts.append(val.strip())
        return " ".join(parts) if parts else str(result.get("id", ""))

    @staticmethod
    def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        """Compute cosine similarity between two vectors.

        Args:
            vec_a: First vector.
            vec_b: Second vector.

        Returns:
            Cosine similarity in [-1, 1]. Returns 0.0 if either vector
            has zero magnitude.
        """
        if not vec_a or not vec_b:
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


__all__ = ["HybridRetriever", "CircuitBreakerOpenError"]
