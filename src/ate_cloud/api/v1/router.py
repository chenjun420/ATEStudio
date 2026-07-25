from fastapi import APIRouter

from ate_cloud.api.v1.executions import router as executions_router
from ate_cloud.api.v1.health import router as health_router
from ate_cloud.api.v1.node_templates import router as node_templates_router
from ate_cloud.api.v1.scripts import router as scripts_router
from ate_cloud.api.v1.sequences import router as sequences_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(node_templates_router)
api_router.include_router(scripts_router)
api_router.include_router(sequences_router)
api_router.include_router(executions_router)