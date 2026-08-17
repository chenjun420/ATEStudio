from fastapi import APIRouter

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

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(rbac_router)
api_router.include_router(node_templates_router)
api_router.include_router(scripts_router)
api_router.include_router(scripts_generate_router)
api_router.include_router(sequences_router)
api_router.include_router(executions_router)
api_router.include_router(debug_router)
api_router.include_router(workers_router)
api_router.include_router(changeover_router)
api_router.include_router(dashboard_router)
api_router.include_router(resources_router)
api_router.include_router(reports_router)
api_router.include_router(apps_router)
api_router.include_router(node_flow_bindings_router)
api_router.include_router(calibrations_router)
api_router.include_router(diagnose_router)
api_router.include_router(faults_router)
api_router.include_router(fixtures_router)
api_router.include_router(limits_router)
api_router.include_router(operator_checkpoints_router)
api_router.include_router(products_router)
api_router.include_router(recordings_router)
api_router.include_router(spc_router)
api_router.include_router(trace_router)
api_router.include_router(workflows_router)
