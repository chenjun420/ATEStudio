"""诊断能力的「到底能不能用」必须能被问出来,且能在不能用时说清楚。

为什么加这个端点
----------------
本项目出现过四个「建好、接线、部署、但从未真正运行过」的能力:

  1. 前端构建产物从未被服务,``/`` 长期 404(而进程 active、stamp 写着已构建)
  2. ``aterag:import`` 端点存在, 但没有任何角色被授予该 scope, admin 亦 403
  3. 诊断路径的 ``OPENAI_*`` 三个键存在但值为空, 端点稳定 503
  4. 知识图谱后端依赖齐备(依赖/compose/部署脚本/手册), 但从未部署, 端点 503

四者都通过了当时存在的全部检查, 因为那些检查都是**探测 API 是否应答**,
而不是**走一遍流程看结果**。菜单项存在、路由注册、构建成功, 都不构成可用证据。

``/diagnose/readiness`` 把「当前配置下这项能力能做什么」变成可查询的事实,
供界面据此置灰或提示, 而不是把一个必然失败的入口摆在操作员面前。

覆盖的三种状态
--------------
  - 全缺: 既无 embedding 也无 Qdrant -> 不可用, 并给出两条可执行的阻塞原因
  - 检索可用、无 LLM -> 建议可用但无归纳(这是第一期目标状态, 不是故障)
  - 全备 -> 全部能力可用
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ate_cloud.api.v1 import diagnose as diagnose_module


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A bare app with the diagnose router mounted.

    The endpoint reads only ``app.state`` and ``settings``, so a bare app keeps
    this test from depending on the whole application booting.

    No extra prefix here: the module's router already carries
    ``prefix="/diagnose"``, so adding one again puts every path at
    ``/diagnose/diagnose/...`` and the test 404s against a correct route.
    """
    application = FastAPI()
    application.include_router(diagnose_module.router)
    return application


class _FakeQdrant:
    """Reports a point count, the way the real ``QdrantClient`` does."""

    def __init__(self, points: int | None = 0) -> None:
        self._points = points
        self.collection = settings_qdrant_collection()

    def get_collection(self, name: str) -> object:
        if self._points is None:
            raise RuntimeError("collection not found")
        return type("Info", (), {"points_count": self._points})()


def settings_qdrant_collection() -> str:
    from ate_cloud.config import settings

    return settings.qdrant_collection_failures


def _set_state(app: FastAPI, *, embedding: bool, qdrant: bool, points: int | None = 0) -> None:
    app.state.embedding_service = object() if embedding else None
    app.state.qdrant_client = _FakeQdrant(points) if qdrant else None


def _readiness(app: FastAPI) -> dict:
    return TestClient(app).get("/diagnose/readiness").json()


class TestReadinessReportsHonestly:
    def test_nothing_configured_is_not_available(self, app: FastAPI) -> None:
        _set_state(app, embedding=False, qdrant=False)
        r = _readiness(app)

        assert r["available"] is False
        assert r["capabilities"]["retrieval"] is False
        assert r["capabilities"]["suggestion"] is False
        # The whole point: it must be able to say "off".
        assert "未启用" in r["detail"]

    def test_blockers_name_the_thing_to_configure(self, app: FastAPI) -> None:
        """A blocker the reader cannot act on is just an error message.

        Each blocker has to name the variable or service, so an operator can go
        fix it without reading the source.
        """
        _set_state(app, embedding=False, qdrant=False)
        blockers = " ".join(_readiness(app)["blockers"])

        assert "OPENAI_API_KEY" in blockers
        assert "Qdrant" in blockers

    def test_one_missing_thing_is_reported_as_one_blocker(self, app: FastAPI) -> None:
        """Counts matter: "something is wrong" is not debuggable."""
        _set_state(app, embedding=False, qdrant=True)
        assert len(_readiness(app)["blockers"]) == 1

        _set_state(app, embedding=True, qdrant=False)
        assert len(_readiness(app)["blockers"]) == 1

    def test_both_present_is_available(self, app: FastAPI) -> None:
        _set_state(app, embedding=True, qdrant=True)
        r = _readiness(app)

        assert r["available"] is True
        assert r["capabilities"]["retrieval"] is True
        assert r["blockers"] == []


class TestTheFirstDeliveryTargetIsNotAFault:
    """Retrieval without an LLM is the goal state for phase 1, not a defect.

    Phase 1 is "collect data and give diagnostic suggestions" — a suggestion
    assembled from retrieved prior cases. The LLM only writes it up. If the
    readiness check reported that as broken, it would push the design towards
    making a model key a prerequisite for the first useful release.
    """

    def test_retrieval_without_llm_is_still_a_suggestion(self, app: FastAPI, monkeypatch) -> None:
        monkeypatch.setattr(diagnose_module.settings, "openai_api_key", "", raising=False)
        # A populated index, so this exercises the LLM axis and not the
        # history axis — the two are reported independently.
        _set_state(app, embedding=True, qdrant=True, points=12)
        r = _readiness(app)

        assert r["available"] is True
        assert r["capabilities"]["suggestion"] is True
        # The LLM is what writes up the result, so only it stays off.
        assert r["capabilities"]["llm_writeup"] is False
        assert "无 LLM" in r["detail"]
        assert r["blockers"] == [], "缺 LLM 不是阻塞项"

    def test_llm_present_enables_writeup(self, app: FastAPI, monkeypatch) -> None:
        monkeypatch.setattr(diagnose_module.settings, "openai_api_key", "sk-test", raising=False)
        _set_state(app, embedding=True, qdrant=True)
        r = _readiness(app)

        assert r["capabilities"]["llm_writeup"] is True


class TestEndpointIsCheapAndSideEffectFree:
    """The UI is meant to call this on every page load.

    That only works if it does not dial out to a service per call — a readiness
    check that is itself slow or flaky is worse than no check, because it gets
    cached by callers and then lies.
    """

    def test_it_does_not_reach_the_network(self, app: FastAPI, monkeypatch) -> None:
        def explode(*_a, **_k):
            raise AssertionError("readiness must not construct services")

        monkeypatch.setattr(diagnose_module, "EmbeddingService", explode)
        _set_state(app, embedding=True, qdrant=True)
        assert TestClient(app).get("/diagnose/readiness").status_code == 200


class TestConfiguredIsNotTheSameAsUseful:
    """An empty index answers every query with "no similar past faults".

    That answer is indistinguishable from a line that has genuinely never
    failed, so an operator concludes either that the tool is broken or that
    nothing is wrong. Reporting such a line as fully available is the same
    mistake as the four capabilities that were built, deployed, and never run —
    the UI asserting something the data does not support.
    """

    def test_an_empty_index_is_reported_as_empty(self, app: FastAPI, monkeypatch) -> None:
        monkeypatch.setattr(diagnose_module.settings, "openai_api_key", "sk-test", raising=False)
        _set_state(app, embedding=True, qdrant=True, points=0)
        r = _readiness(app)

        assert r["available"] is True, "机制可用是事实, 空库不是故障"
        assert r["indexed_cases"] == 0
        assert r["has_history"] is False
        assert "为空" in r["detail"]

    def test_a_populated_index_says_how_much(self, app: FastAPI, monkeypatch) -> None:
        monkeypatch.setattr(diagnose_module.settings, "openai_api_key", "sk-test", raising=False)
        _set_state(app, embedding=True, qdrant=True, points=37)
        r = _readiness(app)

        assert r["has_history"] is True
        assert r["indexed_cases"] == 37
        assert "37" in r["detail"]

    def test_an_empty_index_is_not_a_blocker(self, app: FastAPI) -> None:
        """Nothing is broken, so nothing is listed as blocking.

        Reporting it as a blocker would push the design towards treating a model
        key as a prerequisite, and would train operators to ignore the list.
        """
        _set_state(app, embedding=True, qdrant=True, points=0)
        r = _readiness(app)

        assert r["available"] is True
        assert r["blockers"] == []

    def test_an_unreadable_count_is_none_not_zero(self, app: FastAPI) -> None:
        """"Count failed" and "there are none" lead to opposite advice.

        Reporting a failed read as 0 would tell an operator their history is
        gone. Reporting it as None says the question could not be answered.
        """
        _set_state(app, embedding=True, qdrant=True, points=None)
        r = _readiness(app)

        assert r["indexed_cases"] is None
        assert r["has_history"] is False
        # And it must not have taken the whole report down with it.
        assert r["available"] is True


class TestReadinessMatchesWhatDiagnoseActuallyDoes:
    """The two must not drift.

    If ``/diagnose`` starts working while readiness says unavailable, the UI
    greys out a working feature; the reverse leaves a broken one on offer. Both
    were the failure mode this endpoint was added to prevent, so the invariant is
    asserted rather than left to review.
    """

    def test_unavailable_readiness_matches_the_503(self, app: FastAPI, monkeypatch) -> None:
        """No embedding service -> readiness says off AND diagnose refuses.

        The refusal is also checked for being *actionable*: the old behaviour
        surfaced the OpenAI SDK's own "Missing credentials" text, which named
        neither the variable to set nor the consequence for the operator.
        """
        monkeypatch.setattr(diagnose_module.settings, "openai_api_key", "", raising=False)
        _set_state(app, embedding=False, qdrant=True)

        assert _readiness(app)["available"] is False

        with pytest.raises(diagnose_module.HTTPException) as ei:
            diagnose_module._get_embedding_service(Request({"app": app, "type": "http"}))

        assert ei.value.status_code == 503
        assert "OPENAI_API_KEY" in ei.value.detail
        # Must not leak library internals as the only signal an operator sees.
        assert "Missing credentials" not in ei.value.detail
