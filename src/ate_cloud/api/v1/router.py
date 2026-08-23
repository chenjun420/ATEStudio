"""API v1 router aggregation.

Mounts all v1 sub-routers onto ``api_router`` and applies JWT bearer
authentication (``get_current_user``, RS256 — design doc §9) centrally:

- Routers listed in ``_PROTECTED_ROUTERS`` are mounted with
  ``dependencies=[Depends(get_current_user)]`` so every endpoint under them
  requires a valid bearer token (T17 v41-gap-analysis).
- Exemptions (each with an inline justification, per plan requirement):
  * ``health_router`` — infrastructure liveness probe; the Docker
    healthcheck (docker-compose.yml: ``curl /api/v1/health/db``) runs
    without credentials, so protecting it would fail container startup.
  * ``auth_router`` — login/register/refresh must stay anonymous: they are
    the token-acquisition endpoints themselves (design doc §9). The
    authenticated ``GET /auth/me`` carries its own endpoint-level
    ``get_current_user`` dependency inside the auth module.
- ``users_router`` / ``rbac_router`` / ``apps_router`` already enforce auth
  per-endpoint (``Security(get_current_user, scopes=...)``) in their modules
  and are mounted unchanged.

SSE note: the executions/recordings routers include EventSource streams
(``/{run_id}/events``, ``/{run_id}/topology-stream``,
``/{run_id}/replay/stream``). Native EventSource cannot send an
Authorization header, so browser clients must migrate to a token-carrying
mechanism (fetch-based stream or query-param token); tracked as T24 which
inherits this protection pattern.
"""

from fastapi import APIRouter, Depends

from ate_cloud.api.v1.apps import router as apps_router
from ate_cloud.api.v1.auth import router as auth_router
from ate_cloud.api.v1.calibrations import router as calibrations_router
from ate_cloud.api.v1.changeover import router as changeover_router
from ate_cloud.api.v1.dashboard import router as dashboard_router
from ate_cloud.api.v1.debug import router as debug_router
from ate_cloud.api.v1.diagnose import router as diagnose_router
from ate_cloud.api.v1.executions import router as executions_router
from ate_cloud.api.v1.faults import router as faults_router
from ate_cloud.api.v1.fixtures import router as fixtures_router
from ate_cloud.api.v1.health import router as health_router
from ate_cloud.api.v1.limits import router as limits_router
from ate_cloud.api.v1.node_flow_bindings import router as node_flow_bindings_router
from ate_cloud.api.v1.node_templates import router as node_templates_router
from ate_cloud.api.v1.offline import router as offline_router
from ate_cloud.api.v1.operator_checkpoints import router as operator_checkpoints_router
from ate_cloud.api.v1.products import router as products_router
from ate_cloud.api.v1.rbac import router as rbac_router
from ate_cloud.api.v1.recordings import router as recordings_router
from ate_cloud.api.v1.reports import router as reports_router
from ate_cloud.api.v1.resources import router as resources_router
from ate_cloud.api.v1.scripts import router as scripts_router
from ate_cloud.api.v1.scripts_generate import router as scripts_generate_router
from ate_cloud.api.v1.sequences import router as sequences_router
from ate_cloud.api.v1.spc import router as spc_router
from ate_cloud.api.v1.trace import router as trace_router
from ate_cloud.api.v1.users import router as users_router
from ate_cloud.api.v1.workers import router as workers_router
from ate_cloud.api.v1.workflows import router as workflows_router
from ate_cloud.auth.dependencies import get_current_user

# Routers requiring a valid RS256 JWT on every endpoint (T17).
_PROTECTED_ROUTERS = (
    node_templates_router,
    scripts_router,
    scripts_generate_router,
    sequences_router,
    executions_router,
    debug_router,
    workers_router,
    changeover_router,
    dashboard_router,
    resources_router,
    reports_router,
    node_flow_bindings_router,
    calibrations_router,
    diagnose_router,
    faults_router,
    fixtures_router,
    limits_router,
    offline_router,
    operator_checkpoints_router,
    products_router,
    recordings_router,
    spc_router,
    trace_router,
    workflows_router,
)

api_router = APIRouter()

# ── Anonymous mounts (exemptions justified above) ──────────────────────────
api_router.include_router(health_router)
api_router.include_router(auth_router)

# ── Already protected per-endpoint in their own modules ────────────────────
api_router.include_router(users_router)
api_router.include_router(rbac_router)
api_router.include_router(apps_router)

# ── Uniform JWT enforcement at mount level ────────────────────────────────
for _protected in _PROTECTED_ROUTERS:
    api_router.include_router(
        _protected,
        dependencies=[Depends(get_current_user)],
    )
