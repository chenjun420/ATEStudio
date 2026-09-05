"""ATML API endpoints (task 11).

Provides the INGEST side of ATML — the existing ``reports`` router only
*exports* IEEE 1636.1 TestResults:

- ``POST /api/v1/atml/import-test-description`` — parse an IEEE 1671
  TestDescription XML document (raw request body, ``application/xml`` /
  ``text/xml``), persist its test requirements and test cases, map test cases
  to DSL steps where a valid mapping exists, and return an import summary.

Malformed or non-TestDescription XML is a controlled ``400`` (never a raw
``500`` from an XML parser traceback).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.schemas.knowledge import (
    ATMLImportCounts,
    ATMLImportSummary,
    ATMLUnmappedCase,
)
from ate_cloud.services.atml_importer import ATMLImporter
from ate_cloud.services.atml_td_parser import ATMLParseError

router = APIRouter(
    prefix="/atml",
    tags=["atml"],
)

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def get_importer() -> ATMLImporter:
    """Factory for the ATML importer (overridable in tests via dependency_overrides)."""
    return ATMLImporter()


@router.post(
    "/import-test-description",
    response_model=ATMLImportSummary,
    responses={400: {"description": "Malformed or non-TestDescription XML"}},
)
async def import_test_description(
    request: Request,
    db: DBSession,
    importer: Annotated[ATMLImporter, Depends(get_importer)],
) -> ATMLImportSummary:
    """POST /api/v1/atml/import-test-description — import a 1671 TestDescription.

    The request body is the raw IEEE 1671 TestDescription XML document.
    Idempotent: re-importing the same document updates requirements/cases in
    place (keyed on requirement_code / case_code) rather than duplicating.

    Raises:
        HTTPException: 400 if the body is empty or not a valid
            TestDescription document.
    """
    body = await request.body()
    if not body or not body.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty request body: expected an IEEE 1671 TestDescription XML document",
        )
    try:
        result = await importer.import_test_description(db, body)
    except ATMLParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return ATMLImportSummary(
        product_code=result.product_code,
        requirements=ATMLImportCounts(
            created=result.requirements.created,
            updated=result.requirements.updated,
        ),
        cases=ATMLImportCounts(
            created=result.cases.created,
            updated=result.cases.updated,
        ),
        unmapped=[
            ATMLUnmappedCase(case_code=u.case_code, title=u.title, reason=u.reason)
            for u in result.unmapped
        ],
    )


__all__ = ["router", "get_importer"]
