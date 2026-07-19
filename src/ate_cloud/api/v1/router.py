from fastapi import APIRouter

from ate_cloud.api.v1.scripts import router as scripts_router

api_router = APIRouter()
api_router.include_router(scripts_router)