import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ate_cloud.config import settings, DatabaseType

logger = logging.getLogger(__name__)


def create_engine() -> AsyncEngine:
    database_url = settings.get_database_url()

    # SQLite doesn't support connection pooling
    pool_config: dict[str, Any]
    if settings.database_type == DatabaseType.SQLITE:
        pool_config = {"poolclass": NullPool}
    else:
        pool_config = {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_pre_ping": True,
        }

    return create_async_engine(
        database_url,
        echo=settings.debug,
        **pool_config,
    )


engine = create_engine()