from fastapi import APIRouter

from ate_cloud.api.v1.apps import router as apps_router
from ate_cloud.api.v1.changeover import router as changeover_router
from ate_cloud.api.v1.dashboard import router as dashboard_router
from ate_cloud.api.v1.debug import router as debug_router
from ate_cloud.api.v1.executions import router as executions_router
from ate_cloud.api.v1.health import router as health_router
from ate_cloud.api.v1.node_templates import router as node_templates_router
from ate_cloud.api.v1.reports import router as reports_router
from ate_cloud.api.v1.resources import router as resources_router
from ate_cloud.api.v1.scripts import router as scripts_router
from ate_cloud.api.v1.scripts_generate import router as scripts_generate_router
from ate_cloud.api.v1.sequences import router as sequences_router
from ate_cloud.api.v1.workers import router as workers_router

api_router = APIRouter()
api_router.include_router(health_router)
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
