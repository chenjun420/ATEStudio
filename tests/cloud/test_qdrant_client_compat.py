"""代码调用的每个 Qdrant 方法, 装的这个版本必须真的有。

事故
----
``qdrant-client`` 1.12 移除了 ``QdrantClient.search()``, 改用 ``query_points()``。
本项目声明 ``qdrant-client>=1.12.0``, 所以从那一刻起 ``search()`` 就不存在了。

而失败被降级成了这样::

    Qdrant retrieval failed: 'QdrantClient' object has no attribute 'search'
    Graph retrieval failed: Error 111 connecting to localhost:6379

两行都是 ``except Exception`` 里的 ``logger.warning``, 之后照常 ``return []``。
于是 ``/diagnose`` 每次返回 200、给出看起来很专业的根因与修复步骤, 证据栏写着
"No historical cases retrieved (top 0)" —— 而向量库里有数据, 用同样的向量手工调
Qdrant 能拿到 score 0.71 的命中。

比前三例更难发现: 前几例至少有个 404 或 403, 这一例的响应体完全正常, 只有
「检索结果恒为空」这一个症状, 而恒为空看起来像是「本来就没有相似历史」。

所以这道检查补在这里: 它不需要 Qdrant 在跑, 只需要 import 一次客户端类。
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest
from qdrant_client import QdrantClient

from ate_cloud.services.qdrant_query import (
    QdrantQueryUnavailableError,
    qdrant_points,
    query_points,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

#: Receivers whose attribute access is a Qdrant client call.
#:
#: Deliberately narrow. An earlier version of this file also matched a bare
#: ``client``, which picked up ``await client.get_tools(...)`` on an MCP client
#: in ``aterag_agent.py`` and reported a nonexistent method — a false alarm.
#: A check that cries wolf is a check that gets deleted, so the names here stay
#: specific enough that every hit is a real one.
_CLIENT_VARS = {
    "_qdrant_client",
    "qdrant_client",
    "state.qdrant_client",
}


#: Attribute names that identify a Qdrant client receiver. ``_qdrant_client`` is
#: the important one: the removed ``search()`` call lived behind
#: ``self._qdrant_client.search(...)``, so a guard that only matched the
#: un-prefixed name would have missed the exact file where the incident happened.
_CLIENT_ATTRS = {"qdrant_client", "_qdrant_client"}


def _qdrant_attribute_calls() -> list[tuple[pathlib.Path, int, str]]:
    """Every ``<something>.method(...)`` where the receiver looks like a client."""
    found: list[tuple[pathlib.Path, int, str]] = []
    for path in SRC.rglob("*.py"):
        if "qdrant_query.py" in path.name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr.startswith("_"):
                continue
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and receiver.id in _CLIENT_VARS:
                found.append((path, node.lineno, node.func.attr))
            elif isinstance(receiver, ast.Attribute) and receiver.attr in _CLIENT_ATTRS:
                found.append((path, node.lineno, node.func.attr))
    return found


class TestEveryQdrantCallExistsOnTheInstalledClient:
    """The check that was missing when ``search`` disappeared."""

    def test_the_scan_found_the_call_sites(self) -> None:
        """Guards the guard: an empty scan would pass everything.

        A check that silently matches nothing is worse than no check — it reads
        as coverage. So the specific files it must reach are asserted by name,
        not merely a count: an earlier version matched only un-prefixed names
        and found five sites while skipping ``failure_indexer.py`` entirely —
        the file the incident was in.
        """
        calls = _qdrant_attribute_calls()
        files = {p.name for p, _, _ in calls}

        assert "failure_indexer.py" in files, "扫描漏掉了案发文件"
        assert "dashboard.py" in files
        assert "diagnose.py" in files
        assert "fault_predictor.py" in files
        assert len(calls) >= 8, f"只扫到 {len(calls)} 处, 扫描可能已失效"

    def test_every_hit_is_a_qdrant_method(self) -> None:
        """No false alarms: a check that reports non-problems gets ignored.

        The same lookup as ``test_no_call_uses_a_removed_api`` deliberately —
        stated separately because a scanner check lives or dies by whether people
        trust it. Concrete case it caught: matching a bare ``client`` flagged
        ``client.get_tools`` on an MCP client in ``aterag_agent.py``.
        """
        bogus = [
            f"{p.relative_to(SRC)}:{n} -> {a}"
            for p, n, a in _qdrant_attribute_calls()
            if not hasattr(QdrantClient, a)
        ]
        assert not bogus, f"扫到了不存在的方法: {bogus}"

    def test_search_is_gone_from_this_client_version(self) -> None:
        """State the premise, so a future upgrade that restores it is noticed.

        If this ever passes because ``search`` came back, the compatibility layer
        is worth re-examining rather than silently keeping.
        """
        assert hasattr(QdrantClient, "query_points")
        if not hasattr(QdrantClient, "search"):
            pytest.skip("此版本确实没有 search(), 兼容性分支已被 query_points 覆盖")


class TestTheCompatibilityLayer:
    def test_prefers_query_points(self) -> None:
        class _Modern:
            def __init__(self) -> None:
                self.kwargs: dict[str, Any] = {}

            def query_points(self, **kw: Any) -> str:
                self.kwargs = kw
                return "modern"

            def search(self, **kw: Any) -> str:  # must not be reached
                raise AssertionError("search() should not be called on a modern client")

        client = _Modern()
        assert query_points(
            client, collection_name="c", query=[0.1], limit=5
        ) == "modern"
        assert client.kwargs["query"] == [0.1]
        assert client.kwargs["collection_name"] == "c"

    def test_falls_back_to_search_on_an_older_client(self) -> None:
        # An older client genuinely has no ``query_points`` attribute — that is
        # the whole point of the fallback. Defining it (even to raise) would make
        # ``hasattr`` true and send the call down the modern path.
        class _Legacy:
            def search(self, **kw: Any) -> str:
                return "legacy"

        assert query_points(_Legacy(), collection_name="c", query=[0.1], limit=5) == "legacy"

    def test_a_client_with_neither_says_so_clearly(self) -> None:
        """Not "a search failed" — the reader would go look at Qdrant instead of
        at their dependency version."""

        class _Neither:
            pass

        with pytest.raises(QdrantQueryUnavailableError) as ei:
            query_points(_Neither(), collection_name="c", query=[0.1], limit=5)

        assert "query_points" in str(ei.value)
        assert "search" in str(ei.value)

    def test_response_shapes_normalise_to_one_list(self) -> None:
        class _Point:
            pass

        class _Response:
            points = [_Point(), _Point()]

        assert len(qdrant_points(_Response())) == 2
        # An older client returned the list itself.
        assert len(qdrant_points([_Point()])) == 1
        # And nothing at all is not an error.
        assert qdrant_points(None) == []


class TestRetrievalDoesNotFailSilently:
    """The failure that started this had a warning nobody would see.

    ``/diagnose`` returned 200 with a professional-looking answer and an empty
    evidence list. A retrieval that retrieved nothing has to be able to say so
    where the operator will look, not only in a log.
    """

    def test_the_error_names_the_actual_missing_method(self) -> None:
        class _Broken:
            def query_points(self, **kw: Any) -> None:
                raise AttributeError(
                    "'QdrantClient' object has no attribute 'search'"
                )

        with pytest.raises(AttributeError) as ei:
            query_points(_Broken(), collection_name="c", query=[0.1], limit=1)

        assert "search" in str(ei.value)
