"""工位故障案例 → 向量库。

为什么这张表必须能进 Qdrant
--------------------------
故障诊断的检索腿读的是向量库, 不是这张关系表。所以如果只有 CRUD 而没有索引,
这张表就是个写不进的孤岛: 操作员认真录了案例, 而 ``/diagnose`` 永远返回
「无相似历史故障」—— 与「这条线从没出过故障」完全无法区分。第一期既然承诺
「数据采集 + 诊断建议」, 采集的数据就必须真的被建议用到。

被嵌入的是「故障描述」, 不是全部字段
------------------------------------
只取 ``symptom`` 与 ``cause``。``effect`` 是后果、``fix`` 是措施, 描述的是
「怎么处置」而非「发生了什么」; 混进来会让「哪些故障最常发生」这类检索被修复
动作的措辞带偏。其余字段进 payload, 供过滤与展示使用。

而且刻意**不把未验证的措施塞进向量**: 未验证的措施可以被检索出来当作候选, 但
它的向量不该让检索结果看起来更权威。

point id 用案例主键本身
----------------------
Qdrant 接受 UUID 字符串作为 point id, 而案例 id 本就是 UUID。于是重复索引是幂
等的, 删除也只需一个 id, 不必维护一张 id 映射表 —— 少一张需要同步的表就少一处
会漂移的地方。
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client.http import models as qmodels

from ate_cloud.config import settings
from ate_cloud.models.station import StationFaultCase

logger = logging.getLogger(__name__)


def case_embedding_text(case: StationFaultCase) -> str:
    """The text a diagnosis is matched against.

    Kept separate and pure so the indexing rules are testable without a vector
    database, and so what gets matched on is visible in one place rather than
    being assembled inline at three call sites.
    """
    parts = [case.symptom.strip()]
    if case.cause and case.cause.strip():
        parts.append(case.cause.strip())
    return "\n".join(parts)


def case_payload(case: StationFaultCase, *, plant_id: str | None) -> dict[str, Any]:
    """Filterable metadata carried alongside the vector.

    ``fix_verified`` is included as a first-class filter because "only quote
    remedies someone confirmed" has to be enforceable at query time, not left to
    whoever writes the prompt to remember.
    """
    return {
        "case_id": case.id,
        "station_id": case.station_id,
        "plant_id": plant_id,
        "product_code": case.product_code,
        "fix_verified": case.fix_verified,
        "rpn": case.rpn,
        "severity": case.severity,
        "occurrence": case.occurrence,
        "detection": case.detection,
        "source": "station_fault_case",
        # Distinguishable from failure events, which index into the same
        # collection — a retrieval result must be able to say which it is.
        "kind": "station_fault_case",
    }


class FaultCaseIndexer:
    """Keeps the vector collection in step with the relational table."""

    def __init__(
        self,
        qdrant_client: Any,
        embedding_service: Any,
        collection_name: str | None = None,
    ) -> None:
        self._client = qdrant_client
        self._embeddings = embedding_service
        self._collection = collection_name or settings.qdrant_collection_failures

    async def index_case(self, case: StationFaultCase, *, plant_id: str | None = None) -> bool:
        """Upsert one case. Returns False if the indexing path is unavailable.

        Returns rather than raises: a case that could not be indexed is still a
        real record in the table, and refusing to create it would turn a
        retrieval-side problem into data loss on the write side. The caller
        surfaces the shortfall instead.
        """
        if self._embeddings is None or self._client is None:
            logger.warning(
                "Skipping index of fault case %s: embedding service or Qdrant unavailable",
                case.id,
            )
            return False
        try:
            vector = await self._embeddings.embed(case_embedding_text(case))
            self._client.upsert(
                collection_name=self._collection,
                points=[
                    qmodels.PointStruct(
                        id=case.id,
                        vector=vector,
                        payload=case_payload(case, plant_id=plant_id),
                    )
                ],
            )
            return True
        except Exception as exc:  # noqa: BLE001 — never fail the write because of retrieval
            logger.error("Failed to index fault case %s: %s", case.id, exc, exc_info=True)
            return False

    async def reindex_all(self, cases: list[tuple[StationFaultCase, str | None]]) -> tuple[int, int]:
        """Backfill every case. Returns ``(indexed, skipped)``.

        Needed because the first cases were entered before this path existed, and
        because a width or model change invalidates everything already indexed.
        """
        indexed = 0
        for case, plant_id in cases:
            if await self.index_case(case, plant_id=plant_id):
                indexed += 1
        return indexed, len(cases) - indexed

    async def remove_case(self, case_id: str) -> bool:
        """Delete a case's vector. A missing vector is not an error.

        The relational row is the record of truth; the vector is derived data.
        Deriving it again is cheap, so an orphaned vector is a nuisance, not a
        reason to fail a delete.
        """
        if self._client is None:
            return False
        try:
            self._client.delete(
                collection_name=self._collection,
                points_selector=qmodels.PointIdsList(points=[case_id]),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to remove fault case vector %s: %s", case_id, exc)
            return False


__all__ = ["FaultCaseIndexer", "case_embedding_text", "case_payload"]
