"""厂区 / 工位 / 工位故障案例: 三个第一期必须成立的不变式。

这些不是 CRUD 覆盖率测试。每个断言挡住一类**会在产线上出事**的错误, 而这些
错误在接口返回 200 时全都看不出来。

不变式
------
1. **工位编码在厂区内唯一。** 上下文选择器按 (plant, code) 定位工位; 若同厂
   两个工位同名, 故障会被归到其中一个, 另一个静默为空 —— 而「某工位没记录」
   与「某工位没出过故障」在界面上完全一样。

2. **厂区被删不带走工位与故障历史。** 外键是 SET NULL 而不是 CASCADE:
   删一个分组不该静默删掉一条产线的工位和它的故障史。工位失去厂区是可恢复的
   (重新指派), 历史被删不是。

3. **未验证的措施不能冒充已验证。** ``fix_verified`` 默认 false。诊断建议只能
   引用已验证的措施; 未验证的可作候选, 但不能呈现为「这么修就好了」。这与本
   项目对产测判据的既有纪律同源 —— 未批准的条件不得成为判据。把一个刚写下、
   谁也没验过的措施当成结论, 等于把未签字的判断当成已签字的。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.models import Base, Plant, Station, StationFaultCase


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _plant(db: AsyncSession, code: str = "PLANT-A") -> Plant:
    p = Plant(id=str(uuid.uuid4()), code=code, name=f"厂区 {code}")
    db.add(p)
    await db.commit()
    return p


async def _station(db: AsyncSession, plant: Plant, code: str) -> Station:
    s = Station(
        id=str(uuid.uuid4()),
        plant_id=plant.id,
        code=code,
        name=f"工位 {code}",
        created_at=datetime.now(UTC),
    )
    db.add(s)
    await db.commit()
    return s


class TestStationCodesAreUniquePerPlant:
    async def test_same_code_in_a_different_plant_is_fine(self, db: AsyncSession) -> None:
        """Codes are per-plant, not global.

        Two lines routinely label their first station the same thing, and forcing
        global uniqueness would make the second line un-modellable.
        """
        a = await _plant(db, "PLANT-A")
        b = await _plant(db, "PLANT-B")
        s1 = await _station(db, a, "W1")
        s2 = await _station(db, b, "W1")
        assert s1.code == s2.code

    async def test_same_code_in_the_same_plant_is_rejected(self, db: AsyncSession) -> None:
        """The case that actually matters.

        A duplicate would split one station's fault history across two rows, and
        "this station has no history" is indistinguishable from "this station
        never failed".
        """
        plant = await _plant(db)
        await _station(db, plant, "W1")
        with pytest.raises(IntegrityError):
            await _station(db, plant, "W1")


class TestDeletingAPlantCannotTakeTheLineWithIt:
    async def test_plant_with_stations_cannot_be_deleted(self, db: AsyncSession) -> None:
        """A grouping change must never take fault history with it.

        ``stations.plant_id`` is NOT NULL, so deleting a plant that still has
        stations is refused rather than cascading. That is the safer of the two
        possible designs: the alternative — orphaning the stations — produces
        rows the context selector can never show, which is a worse failure than
        an operation that asks you to empty the line first.
        """
        plant = await _plant(db)
        station = await _station(db, plant, "W1")
        db.add(
            StationFaultCase(
                id=str(uuid.uuid4()),
                station_id=station.id,
                symptom="输出电压偏低",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

        await db.delete(plant)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        assert (await db.execute(select(func.count()).select_from(Station))).scalar() == 1
        assert (await db.execute(select(func.count()).select_from(StationFaultCase))).scalar() == 1

    async def test_an_empty_plant_can_be_deleted(self, db: AsyncSession) -> None:
        """The guard must not make plants undeletable in general."""
        plant = await _plant(db)
        await db.delete(plant)
        await db.commit()
        assert (await db.execute(select(func.count()).select_from(Plant))).scalar() == 0

    async def test_deleting_a_station_does_take_its_cases(self, db: AsyncSession) -> None:
        """The opposite call, and it is the right one.

        A station's cases have no meaning without the station, unlike a plant's
        stations which are still real hardware somewhere.
        """
        plant = await _plant(db)
        station = await _station(db, plant, "W1")
        db.add(
            StationFaultCase(
                id=str(uuid.uuid4()),
                station_id=station.id,
                symptom="上电失败",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

        await db.delete(station)
        await db.commit()

        assert (await db.execute(select(func.count()).select_from(StationFaultCase))).scalar() == 0


class TestUnverifiedFixesCannotPoseAsRemedies:
    async def test_a_new_case_is_unverified_by_default(self, db: AsyncSession) -> None:
        """Defaulting to verified would make the safe state the unsafe one.

        Anyone who forgets to set the flag would be publishing a remedy that
        nobody ever confirmed — and the suggestion layer would quote it.
        """
        plant = await _plant(db)
        station = await _station(db, plant, "W1")
        case = StationFaultCase(
            id=str(uuid.uuid4()),
            station_id=station.id,
            symptom="输出电压偏低",
            fix="重新压紧 JIG 螺丝",
            created_at=datetime.now(UTC),
        )
        db.add(case)
        await db.commit()

        assert case.fix_verified is False
        assert case.fix == "重新压紧 JIG 螺丝"

    async def test_rpn_is_stored_not_derived(self, db: AsyncSession) -> None:
        """Recorded, not recomputed from S*O*D.

        A person assessed and signed a number. If the product later recomputes
        it and disagrees, the recomputation must not silently overwrite what was
        signed — the disagreement is information, not a bug to hide.
        """
        plant = await _plant(db)
        station = await _station(db, plant, "W1")
        case = StationFaultCase(
            id=str(uuid.uuid4()),
            station_id=station.id,
            symptom="间歇性接触不良",
            severity=8,
            occurrence=6,
            detection=9,
            rpn=100,  # deliberately not 8*6*9
            created_at=datetime.now(UTC),
        )
        db.add(case)
        await db.commit()

        assert case.rpn == 100
        assert case.severity * case.occurrence * case.detection == 432

    async def test_symptom_is_required(self, db: AsyncSession) -> None:
        """The database guarantees NOT NULL, and only that.

        An *empty* symptom is a different problem and is deliberately not
        constrained here: rejecting ``""`` is a validation-layer decision that
        belongs to the API, not a schema one, and pretending otherwise would
        have this test asserting behaviour nobody implemented. The API has to
        refuse it when it is written; what matters here is that a case with no
        observation at all cannot be stored.
        """
        plant = await _plant(db)
        station = await _station(db, plant, "W1")
        db.add(
            StationFaultCase(
                id=str(uuid.uuid4()),
                station_id=station.id,
                symptom=None,  # type: ignore[arg-type]
                created_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


class TestTheTwoQueriesPhaseOneExistsToAnswer:
    """The table's purpose, asserted as behaviour rather than as schema."""

    async def test_top_rpn_faults_for_one_station(self, db: AsyncSession) -> None:
        plant = await _plant(db)
        station = await _station(db, plant, "W1")
        other = await _station(db, plant, "W2")
        for st, symptom, rpn in (
            (station, "输出电压偏低", 240),
            (station, "上电失败", 480),
            (other, "通信超时", 999),  # highest overall, wrong station
        ):
            db.add(
                StationFaultCase(
                    id=str(uuid.uuid4()),
                    station_id=st.id,
                    symptom=symptom,
                    rpn=rpn,
                    created_at=datetime.now(UTC),
                )
            )
        await db.commit()

        rows = (
            await db.execute(
                select(StationFaultCase)
                .where(StationFaultCase.station_id == station.id)
                .order_by(StationFaultCase.rpn.desc())
            )
        ).scalars().all()

        # Station-scoped, and the other station's higher RPN does not leak in.
        assert [r.rpn for r in rows] == [480, 240]

    async def test_verified_remedies_for_one_station(self, db: AsyncSession) -> None:
        plant = await _plant(db)
        station = await _station(db, plant, "W1")
        for symptom, fix, verified in (
            ("上电失败", "更换保险丝", True),
            ("接触不良", "重新压紧 JIG 螺丝", False),
        ):
            db.add(
                StationFaultCase(
                    id=str(uuid.uuid4()),
                    station_id=station.id,
                    symptom=symptom,
                    fix=fix,
                    fix_verified=verified,
                    created_at=datetime.now(UTC),
                )
            )
        await db.commit()

        quotable = (
            await db.execute(
                select(StationFaultCase).where(
                    StationFaultCase.station_id == station.id,
                    StationFaultCase.fix_verified.is_(True),
                )
            )
        ).scalars().all()

        assert [c.fix for c in quotable] == ["更换保险丝"]

    async def test_product_scoping_is_optional(self, db: AsyncSession) -> None:
        """Not every fault is product-specific.

        A fixture or tooling fault belongs to the station whatever is on the
        line, so requiring a product_code would force operators to invent one.
        """
        plant = await _plant(db)
        station = await _station(db, plant, "W1")
        case = StationFaultCase(
            id=str(uuid.uuid4()),
            station_id=station.id,
            symptom="真空吸嘴漏气",
            created_at=datetime.now(UTC),
        )
        db.add(case)
        await db.commit()
        assert case.product_code is None
