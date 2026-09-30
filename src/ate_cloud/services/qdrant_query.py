"""Qdrant 向量检索的唯一入口。

为什么需要这一层
----------------
``qdrant-client`` 在 1.12 里**移除了** ``QdrantClient.search()``, 改用
``query_points()``。本项目声明的依赖是 ``qdrant-client>=1.12.0``, 所以
``search()`` 从那一刻起就不存在了。

而这不是一次响亮的失败:

    Qdrant retrieval failed: 'QdrantClient' object has no attribute 'search'
    Graph retrieval failed: Error 111 connecting to localhost:6379

两行都是 ``except Exception`` 里的 ``logger.warning``, 之后照常 ``return []``。
于是 /diagnose 每次都返回 HTTP 200、给出看起来很专业的根因与修复步骤, 证据栏
写着 "No historical cases retrieved (top 0)" —— 而向量库里明明有数据, 手工
用同样的向量调 Qdrant 能拿到 score 0.71 的命中。

「接口 200 + 输出像模像样 + 一条历史都检索不到」, 与本项目此前那些「建好、接线、
部署、从未运行」的能力是同一种故障, 而且更难发现: 这次连报错都被降级成了警告。

所以检索调用集中在这里, 并且有一处测试断言「代码调用的每个 Qdrant 方法, 装的这个
版本确实有」—— 那正是当初缺的那道检查。

不选别的做法的原因
------------------
不直接改调 ``query_points``: 那样 ``search``/``query_points`` 的选择会散落在多个
调用点, 下一次客户端再改名时又得逐个找。这里接受两种方法名, 优先新的。
"""

from __future__ import annotations

from typing import Any


class QdrantQueryUnavailableError(RuntimeError):
    """The installed client exposes neither search API.

    Separate from the generic path so the log says "the client cannot do vector
    search at all" rather than "a search failed", which sends the reader
    looking at Qdrant instead of at their dependency.
    """


def query_points(
    client: Any,
    *,
    collection_name: str,
    query: list[float],
    limit: int,
    with_payload: bool = True,
) -> Any:
    """Run a vector query and return the Qdrant response object.

    Prefers ``query_points`` (1.12+) and falls back to ``search`` (older), so the
    caller gets one response shape either way: something with ``.points``.
    """
    if hasattr(client, "query_points"):
        return client.query_points(
            collection_name=collection_name,
            query=query,
            limit=limit,
            with_payload=with_payload,
        )
    if hasattr(client, "search"):
        return client.search(
            collection_name=collection_name,
            query_vector=query,
            limit=limit,
            with_payload=with_payload,
        )
    raise QdrantQueryUnavailableError(
        f"{type(client).__name__} 既没有 query_points 也没有 search —— "
        "装的 qdrant-client 版本与本代码期望的不一致"
    )


def qdrant_points(response: Any) -> list[Any]:
    """Normalise a response to a list of scored points.

    ``query_points`` returns an object with ``.points``; ``search`` returned the
    list directly. Callers should not have to know which one they got.
    """
    return getattr(response, "points", response) or []


__all__ = ["QdrantQueryUnavailableError", "qdrant_points", "query_points"]
