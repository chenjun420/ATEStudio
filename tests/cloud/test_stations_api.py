"""工位 / 故障案例 的 API:三处不肯将就的行为, 以及索引腿。

这里测的不是「端点返回 200」, 而是四种在 200 之下依然可能出错的情形:

  1. 有工位的厂区被删 —— 一条产线的故障史随之消失
  2. 同厂区工位编码重复 —— 一个工位的故障史被劈成两半
  3. 案例写进关系表却进不了向量库 —— 操作员录了数据, 检索永远看不见
  4. 未验证的措施被当成结论引用 —— 把未签字的判断当成已签字的

第 3 条尤其值得单独成文件: 它在接口全 200 的情况下发生, 而且「关系表有数据」
与「检索有数据」是两件事, 只有后者能让诊断建议真的引用本线历史。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.api.v1 import stations as api
from ate_cloud.models import Base
from ate_cloud.models.station import Plant, Station, StationFaultCase
from ate_cloud.schemas.station import FaultCaseCreate, PlantCreate, StationCreate
from ate_cloud.services.fault_case_index import case_embedding_text

# ── doubles ───────────────────────────────────────────────────────────────


class _FakeEmbeddings:
    """Records what it was asked to embed and returns a distinct vector."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(text) % 7), 0.1, 0.2]


class _FakeQdrant:
    def __init__(self) -> None:
        self.upserts: dict[str, dict[str, Any]] = {}
        self.deletes: list[str] = []

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        for p in points:
            self.upserts[str(p.id)] = p.payload

    def delete(self, collection_name: str, points_selector: Any) -> None:
        self.deletes.extend(str(i) for i in points_selector.points)


class _State:
    def __init__(self) -> None:
        self.qdrant_client: Any = None
        self.embedding_service: Any = None


class _Req:
    """Stand-in for a Starlette Request carrying only ``app.state``."""

    def __init__(self, state: _State) -> None:
        self.app = type("App", (), {"state": state})()


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def state() -> _State:
    return _State()


@pytest.fixture
def live(state: _State) -> _State:
    """Both retrieval dependencies present — the normal deployment shape."""
    state.qdrant_client = _FakeQdrant()
    state.embedding_service = _FakeEmbeddings()
    return state


def _plant(db: AsyncSession, code: str = "PLANT-A") -> Plant:
    p = Plant(id=str(uuid.uuid4()), code=code, name=f"厂区 {code}",
              created_at=datetime.now(UTC))
    db.add(p)
    return p


def _station(db: AsyncSession, plant: Plant, code: str = "W1") -> Station:
    s = Station(id=str(uuid.uuid4()), plant_id=plant.id, code=code, name=f"工位 {code}",
                created_at=datetime.now(UTC))
    db.add(s)
    return s


async def _flush(db: AsyncSession) -> None:
    await db.commit()


# ── 1. 有工位的厂区删不掉 ─────────────────────────────────────────────────


class TestAPlantWithStationsCannotBeDeleted:
    async def test_the_delete_is_refused(self, db: AsyncSession) -> None:
        plant = _plant(db)
        await _flush(db)
        _station(db, plant)
        await _flush(db)

        with pytest.raises(HTTPException) as ei:
            await api.delete_plant(plant.id, db, _user=None)

        assert ei.value.status_code == 409

    async def test_the_refusal_says_what_to_do_about_it(self, db: AsyncSession) -> None:
        """A 409 without a next step is a dead end, not an explanation."""
        plant = _plant(db)
        await _flush(db)
        _station(db, plant)
        await _flush(db)

        with pytest.raises(HTTPException) as ei:
            await api.delete_plant(plant.id, db, _user=None)

        assert "工位" in ei.value.detail

    async def test_nothing_is_lost_when_it_is_refused(self, db: AsyncSession) -> None:
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        db.add(StationFaultCase(id=str(uuid.uuid4()), station_id=station.id,
                                symptom="输出电压偏低", created_at=datetime.now(UTC)))
        await _flush(db)

        with pytest.raises(HTTPException):
            await api.delete_plant(plant.id, db, _user=None)
        await db.rollback()

        assert (await db.execute(select(func.count()).select_from(Station))).scalar() == 1
        assert (
            await db.execute(select(func.count()).select_from(StationFaultCase))
        ).scalar() == 1

    async def test_an_empty_plant_can_still_be_deleted(self, db: AsyncSession) -> None:
        """The guard must not make plants undeletable in general."""
        plant = _plant(db)
        await _flush(db)

        await api.delete_plant(plant.id, db, _user=None)

        assert (await db.execute(select(func.count()).select_from(Plant))).scalar() == 0


# ── 2. 同厂区工位编码不可重复 ─────────────────────────────────────────────


class TestStationCodesAreUniquePerPlant:
    async def test_a_duplicate_in_the_same_plant_is_a_conflict(
        self, db: AsyncSession, state: _State
    ) -> None:
        plant = _plant(db)
        await _flush(db)
        await api.create_station(
            StationCreate(plant_id=plant.id, code="W1", name="工位 1"), db, _user=None
        )

        with pytest.raises(HTTPException) as ei:
            await api.create_station(
                StationCreate(plant_id=plant.id, code="W1", name="工位 1"), db, _user=None
            )

        assert ei.value.status_code == 409

    async def test_the_conflict_explains_the_consequence(
        self, db: AsyncSession, state: _State
    ) -> None:
        """The duplicate silently splits one station's history in two, so the
        message has to say that rather than just "already exists"."""
        plant = _plant(db)
        await _flush(db)
        await api.create_station(
            StationCreate(plant_id=plant.id, code="W1", name="工位 1"), db, _user=None
        )

        with pytest.raises(HTTPException) as ei:
            await api.create_station(
                StationCreate(plant_id=plant.id, code="W1", name="工位 1"), db, _user=None
            )

        assert "故障历史" in ei.value.detail

    async def test_the_same_code_in_another_plant_is_fine(
        self, db: AsyncSession, state: _State
    ) -> None:
        """Two lines both calling their first station W1 is routine."""
        a = PlantCreate(code="PLANT-A", name="A")
        b = PlantCreate(code="PLANT-B", name="B")
        pa = await api.create_plant(a, db, _user=None)
        pb = await api.create_plant(b, db, _user=None)

        s1 = await api.create_station(
            StationCreate(plant_id=pa.id, code="W1", name="1"), db, _user=None
        )
        s2 = await api.create_station(
            StationCreate(plant_id=pb.id, code="W1", name="1"), db, _user=None
        )

        assert s1.code == s2.code == "W1"


# ── 3. 校验层该拦的 ───────────────────────────────────────────────────────


class TestTheApiLayerOwnsTheChecksTheSchemaCannotMake:
    async def test_an_empty_symptom_is_refused(self, db: AsyncSession, live: _State) -> None:
        """NOT NULL does not reject "" — and an unmatchable case occupies a slot
        while making this station look as though it has no history."""
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)

        with pytest.raises(ValidationError):
            FaultCaseCreate(station_id=station.id, symptom="   ")

    async def test_an_unknown_field_is_refused(self, db: AsyncSession, live: _State) -> None:
        """A silently dropped ``plant_id`` would attach a row to nothing, and a
        silently dropped ``fix_verified`` would read as unverified — one is data
        loss, the other is a safety default."""
        with pytest.raises(ValidationError):
            FaultCaseCreate(station_id="x", symptom="s", fix_verified__typo=True)

    async def test_severity_is_bounded_like_fmea(self) -> None:
        with pytest.raises(ValidationError):
            FaultCaseCreate(station_id="x", symptom="s", severity=11)

    async def test_rpn_is_stored_not_recomputed(self, db: AsyncSession, live: _State) -> None:
        """A signed assessment must survive. If S*O*D disagrees with rpn, that
        disagreement is information, not something to overwrite."""
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)

        case = await api.create_fault_case(
            FaultCaseCreate(station_id=station.id, symptom="间歇性接触不良",
                            severity=8, occurrence=6, detection=9, rpn=100),
            db, _Req(live), _user=None,
        )

        assert case.rpn == 100
        assert case.severity * case.occurrence * case.detection == 432


# ── 4. 未验证的措施不得冒充结论 ───────────────────────────────────────────


class TestUnverifiedRemediesStayUnverified:
    async def test_a_new_case_is_unverified(self, db: AsyncSession, live: _State) -> None:
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)

        case = await api.create_fault_case(
            FaultCaseCreate(station_id=station.id, symptom="输出电压偏低",
                            fix="重新压紧 JIG 螺丝"),
            db, _Req(live), _user=None,
        )

        assert case.fix == "重新压紧 JIG 螺丝"
        assert case.fix_verified is False

    async def test_verified_only_returns_only_confirmed_ones(
        self, db: AsyncSession, live: _State
    ) -> None:
        """The suggestion layer needs this to be a query, not a convention —
        a future caller can forget a convention."""
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)

        await api.create_fault_case(
            FaultCaseCreate(station_id=station.id, symptom="上电失败", fix="更换保险丝",
                            fix_verified=True),
            db, _Req(live), _user=None,
        )
        await api.create_fault_case(
            FaultCaseCreate(station_id=station.id, symptom="接触不良",
                            fix="重新压紧 JIG 螺丝"),
            db, _Req(live), _user=None,
        )

        # Every parameter is passed explicitly. These endpoints are called
        # directly rather than through TestClient, so FastAPI's ``Query(...)``
        # defaults arrive as ``Query`` objects instead of values — leaving one
        # out fails deep inside SQLAlchemy with a confusing type error rather
        # than at the call site.
        listed = await api.list_fault_cases(
            db,
            _user=None,
            station_id=None,
            plant_id=None,
            product_code=None,
            verified_only=True,
            order_by="occurred_at",
            skip=0,
            limit=200,
        )
        assert [c.fix for c in listed.items] == ["更换保险丝"]

    async def test_the_count_agrees_with_the_rows(
        self, db: AsyncSession, live: _State
    ) -> None:
        """``total`` drives pagination, so a count that disagrees with the rows
        yields pages that appear to exist and then do not."""
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)
        for symptom in ("a", "b", "c", "d"):
            await api.create_fault_case(
                FaultCaseCreate(station_id=station.id, symptom=symptom),
                db, _Req(live), _user=None,
            )

        page = await api.list_fault_cases(
            db, _user=None, station_id=None, plant_id=None, product_code=None,
            verified_only=False, order_by="occurred_at", skip=0, limit=2,
        )

        assert page.total == 4
        assert len(page.items) == 2


# ── 5. 索引腿: 关系表有数据 ≠ 检索有数据 ─────────────────────────────────


class TestACaseThatIsStoredButNotIndexedIsNotALostCase:
    async def test_the_row_survives_a_broken_retrieval_side(
        self, db: AsyncSession, state: _State
    ) -> None:
        """No embedding service — the deployment shape before credentials.

        Refusing the write would turn a retrieval-side problem into data loss on
        the write side. The case must be created and the shortfall reported.
        """
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)

        case = await api.create_fault_case(
            FaultCaseCreate(station_id=station.id, symptom="输出电压偏低"),
            db, _Req(state), _user=None,
        )

        assert (await db.execute(
            select(func.count()).select_from(StationFaultCase).where(
                StationFaultCase.id == case.id)
        )).scalar() == 1

    async def test_reindex_backfills_what_was_missed(
        self, db: AsyncSession, state: _State, live: _State
    ) -> None:
        """The exact sequence that produced an empty index: cases entered while
        indexing was unavailable, then indexing fixed. Nothing else would notice.
        """
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)
        for symptom in ("输出电压偏低", "上电失败", "通信超时"):
            await api.create_fault_case(
                FaultCaseCreate(station_id=station.id, symptom=symptom),
                db, _Req(state), _user=None,
            )

        result = await api.reindex_fault_cases(db, _Req(live), _user=None)

        assert result.indexed == 3
        assert result.skipped == 0
        assert len(live.qdrant_client.upserts) == 3

    async def test_reindex_reports_what_it_could_not_do(
        self, db: AsyncSession, state: _State
    ) -> None:
        """A count that silently under-reports sends the operator away reassured."""
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)
        await api.create_fault_case(
            FaultCaseCreate(station_id=station.id, symptom="输出电压偏低"),
            db, _Req(state), _user=None,
        )

        result = await api.reindex_fault_cases(db, _Req(state), _user=None)

        assert result.indexed == 0
        assert result.skipped == 1
        assert "embedding" in result.detail


class TestDeletionRemovesTheVectorToo:
    async def test_deleting_a_case_drops_its_vector(
        self, db: AsyncSession, live: _State
    ) -> None:
        """A row deleted but a vector left behind means retrieval can quote a
        case that no longer exists — a confident answer citing deleted history."""
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)
        case = await api.create_fault_case(
            FaultCaseCreate(station_id=station.id, symptom="输出电压偏低"),
            db, _Req(live), _user=None,
        )
        assert case.id in live.qdrant_client.upserts

        await api.delete_fault_case(case.id, db, _Req(live), _user=None)

        assert case.id in live.qdrant_client.deletes
        assert (await db.execute(
            select(func.count()).select_from(StationFaultCase)
        )).scalar() == 0

    async def test_deleting_a_station_drops_every_case_vector(
        self, db: AsyncSession, live: _State
    ) -> None:
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)
        ids = []
        for symptom in ("输出电压偏低", "上电失败"):
            c = await api.create_fault_case(
                FaultCaseCreate(station_id=station.id, symptom=symptom),
                db, _Req(live), _user=None,
            )
            ids.append(c.id)

        await api.delete_station(station.id, db, _Req(live), _user=None)

        assert set(live.qdrant_client.deletes) == set(ids)
        assert (await db.execute(
            select(func.count()).select_from(StationFaultCase)
        )).scalar() == 0


# ── 6. 被嵌入的到底是什么 ────────────────────────────────────────────────


class TestWhatGetsEmbedded:
    """Only the fault description, not the remedy.

    Mixing ``fix`` into the embedding would let the wording of a repair action
    steer which failures get retrieved, which is backwards: a remedy is what you
    are looking for *after* you know what broke.
    """

    def _case(self, **kw: Any) -> StationFaultCase:
        return StationFaultCase(id="x", station_id="s", **kw)

    def test_symptom_alone_is_enough(self) -> None:
        assert case_embedding_text(self._case(symptom="  输出电压偏低  ")) == "输出电压偏低"

    def test_cause_is_included_when_present(self) -> None:
        text = case_embedding_text(self._case(symptom="电压低", cause="反馈电阻漂移"))
        assert text == "电压低\n反馈电阻漂移"

    def test_a_blank_cause_adds_no_blank_line(self) -> None:
        assert case_embedding_text(self._case(symptom="电压低", cause="   ")) == "电压低"

    def test_the_fix_is_not_embedded(self) -> None:
        text = case_embedding_text(
            self._case(symptom="电压低", cause="电阻漂移", effect="超差", fix="更换电阻")
        )
        assert "更换电阻" not in text
        assert "超差" not in text

    def test_the_payload_still_carries_everything_filterable(self) -> None:
        """``fix_verified`` has to be filterable at query time — that is the whole
        mechanism that stops an unconfirmed remedy being quoted."""
        from ate_cloud.services.fault_case_index import case_payload

        case = StationFaultCase(
            id="c1", station_id="s1", symptom="电压低", fix_verified=True, rpn=240
        )
        p = case_payload(case, plant_id="p1")

        assert p["fix_verified"] is True
        assert p["rpn"] == 240
        assert p["kind"] == "station_fault_case"
        assert p["plant_id"] == "p1"


class TestThePayloadCarriesTheCaseText:
    """A hit carrying only an id cannot be quoted correctly — so it gets quoted
    wrongly.

    First end-to-end run: two station fault cases were retrieved, and the
    second citation described "output capacitor ESR degradation and regulator
    trim offsets" for a case whose actual text was about fixture contact
    resistance causing intermittent communication timeouts. The downstream
    writer had an id, a station, and some risk numbers — and no words — so it
    supplied its own. Confident, fluent, and wrong, in exactly the shape of the
    "similar but the wrong component" failure the phase-1 safety
    rules single out.
    """

    def test_symptom_and_cause_travel_with_the_vector(self) -> None:
        from ate_cloud.services.fault_case_index import case_payload

        case = StationFaultCase(
            id="c1",
            station_id="s1",
            symptom="工装夹具接触不良导致间歇性通信超时",
            cause="JIG 螺丝松动, 触点氧化",
        )
        p = case_payload(case, plant_id="p1")

        assert p["symptom"] == "工装夹具接触不良导致间歇性通信超时"
        assert p["cause"] == "JIG 螺丝松动, 触点氧化"

    def test_an_unconfirmed_remedy_travels_with_its_flag(self) -> None:
        """The remedy may be shown, but it must not look confirmed."""
        from ate_cloud.services.fault_case_index import case_payload

        case = StationFaultCase(
            id="c1", station_id="s1", symptom="接触不良",
            fix="重新压紧 JIG 螺丝", fix_verified=False,
        )
        p = case_payload(case, plant_id="p1")

        assert p["fix"] == "重新压紧 JIG 螺丝"
        assert p["fix_verified"] is False

    async def test_the_vector_and_the_text_are_stored_together(
        self, db: AsyncSession, live: _State
    ) -> None:
        """End to end through the API, not just the payload function."""
        plant = _plant(db)
        await _flush(db)
        station = _station(db, plant)
        await _flush(db)
        await api.create_fault_case(
            FaultCaseCreate(station_id=station.id, symptom="真空吸嘴漏气导致吸附失败"),
            db, _Req(live), _user=None,
        )

        stored = list(live.qdrant_client.upserts.values())
        assert len(stored) == 1
        assert stored[0]["symptom"] == "真空吸嘴漏气导致吸附失败"
