"""ATERag bundle import API (P2).

Two-phase by design:

1. ``POST /api/v1/imports/aterag?dry_run=true`` — plan only. Returns what would
   change plus any conflicts against manual edits. Nothing is written.
2. ``POST /api/v1/imports/aterag?dry_run=false&confirm_overwrite=true`` —
   apply. ``confirm_overwrite`` is required to overwrite manual edits; without it
   those fields are left alone and reported as conflicts.

Auth: JWT with the ``aterag:import`` scope (operator roles cannot ingest).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import get_current_user, require_scopes
from ate_cloud.db import get_db
from ate_cloud.schemas.aterag_bundle import (
    BundleModel,
    ContractMismatchError,
    assert_contract,
    contract_mismatch_report,
)
from ate_cloud.services.aterag_importer import ATERagImporter

router = APIRouter(prefix="/imports", tags=["imports"])


def get_importer() -> ATERagImporter:
    """Importer factory (overridable in tests via dependency_overrides)."""
    return ATERagImporter()


@router.post("/aterag")
async def import_aterag_bundle(
    bundle: BundleModel,
    dry_run: Annotated[bool, Query(description="只计算变更计划, 不写库")] = True,
    confirm_overwrite: Annotated[bool, Query(description="允许覆盖人工修改的字段; 否则只报冲突")] = False,
    db: Annotated[AsyncSession, Depends(get_db)] = None,  # type: ignore[assignment]
    _: Annotated[Any, Depends(require_scopes("aterag:import"))] = None,
    importer: Annotated[ATERagImporter, Depends(get_importer)] = None,  # type: ignore[assignment]
    _user: Annotated[Any, Depends(get_current_user)] = None,
) -> dict[str, Any]:
    """Import an ATERag bundle.

    Returns the same shape for both phases so the UI can render a preview and a
    result with one component. ``dry_run=true`` is the default precisely because
    a wrong import is expensive to unwind: nothing is written until the caller
    explicitly asks.
    """
    try:
        assert_contract(bundle, strict=True)
    except ContractMismatchError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=contract_mismatch_report(bundle.contract_hash),
        ) from e

    assert db is not None and importer is not None
    try:
        result = await importer.import_bundle(db, bundle, dry_run=dry_run, confirm_overwrite=confirm_overwrite)
    except Exception as e:  # noqa: BLE001
        # One transaction covers the import; a failure must not leave
        # requirements without their conditions.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导入失败, 已回滚: {e}",
        ) from e
    return result.to_dict()
