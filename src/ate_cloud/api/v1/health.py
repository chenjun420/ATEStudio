from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from ate_cloud.db import async_session_factory

router = APIRouter(tags=["health"])


@router.get("/health/db")
async def check_db_health():
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "healthy"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connection failed: {str(e)}"
        )