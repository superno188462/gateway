"""应用组合根。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.api_keys import router as api_keys_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.projects import router as projects_router
from app.config import Settings
from app.container import AppContainer
from app.domain.health import ReadinessProbe
from app.service_management.api import account_router as account_services_router
from app.service_management.api import router as llm_services_router
from app.services.llm.admin_api import public_router as llm_models_router
from app.services.llm.admin_api import router as llm_provider_admin_router
from app.services.llm.api import router as llm_gateway_router


def create_app(
    settings: Settings | None = None,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    """创建并装配 FastAPI 应用，允许测试替换外部依赖。"""
    resolved_settings = settings or Settings()
    container = AppContainer.build(resolved_settings, readiness_probe)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await container.startup()
        yield
        await container.close()

    app = FastAPI(
        title="Agent Gateway API",
        version="0.1.0",
        description="Agent 网关中台后端，提供账户、项目、服务管理和模型网关接口。",
        lifespan=lifespan,
    )
    app.state.container = container
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(api_keys_router)
    app.include_router(llm_services_router)
    app.include_router(account_services_router)
    app.include_router(llm_gateway_router)
    app.include_router(llm_models_router)
    app.include_router(llm_provider_admin_router)
    return app
