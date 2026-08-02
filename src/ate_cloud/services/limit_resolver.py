"""LimitResolver -- resolves the effective test limit for a given date.

Given a (product_type, test_name) pair and an optional date, finds the most
recent limit version that is effective at that date. This enables duckDuckGo
resolution: multiple limit versions coexist with different effective_from
dates, and the current (or queried) date determines which one is in effect.

Resolution logic:
    1. Filter by product_type + test_name.
    2. effective_from <= query_date (the limit has started).
    3. effective_until IS NULL OR effective_until >= query_date (not yet expired).
    4. Order by effective_from DESC, take the first (most recent start).
    5. If no match, return None.

If date is None, today's date is used.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.test_limits import TestLimit as TestLimitModel


class LimitResolver:
    """Resolves the effective test limit for a (product_type, test_name, date).

    限值解析器 -- 根据产品类型、测试名称和日期解析当前生效的测试限值。

    The resolver is stateless: each call performs a fresh database query.
    Construct one resolver per request (or reuse across requests -- there is
    no internal state).
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the resolver with a database session.

        Args:
            db: Async SQLAlchemy session used for all queries.
        """
        self._db = db

    async def resolve(
        self,
        product_type: str,
        test_name: str,
        date: datetime.date | None = None,
    ) -> TestLimitModel | None:
        """Resolve the effective test limit for the given parameters.

        Finds all limits matching product_type + test_name where the query
        date falls within [effective_from, effective_until], then returns the
        one with the most recent effective_from (the latest version that is
        active at the query date).

        Args:
            product_type: Product type identifier to resolve for.
            test_name: Test measurement name to resolve for.
            date: Resolution date. If None, uses today's date.

        Returns:
            The effective TestLimit model instance, or None if no limit is
            effective at the query date.
        """
        query_date = date if date is not None else datetime.date.today()

        stmt = (
            select(TestLimitModel)
            .where(TestLimitModel.product_type == product_type)
            .where(TestLimitModel.test_name == test_name)
            .where(TestLimitModel.effective_from <= query_date)
            .where(
                (TestLimitModel.effective_until.is_(None))
                | (TestLimitModel.effective_until >= query_date)
            )
            .order_by(TestLimitModel.effective_from.desc())
            .limit(1)
        )

        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()
