"""应用组合根。"""

import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from app.api.api_keys import router as api_keys_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.projects import router as projects_router
from app.config import Settings
from app.container import AppContainer
from app.domain.health import ReadinessProbe
from app.resources.api import router as project_resources_router
from app.service_management.api import account_router as account_services_router
from app.service_management.api import router as llm_services_router
from app.services.asr.admin_api import router as asr_provider_admin_router
from app.services.asr.api import router as asr_gateway_router
from app.services.asr.stream_api import router as asr_stream_router
from app.services.embedding.admin_api import public_router as embedding_models_router
from app.services.embedding.admin_api import router as embedding_provider_admin_router
from app.services.embedding.api import router as embedding_gateway_router
from app.services.embedding.console_api import router as rag_console_router
from app.services.embedding.rag_api import router as rag_knowledge_router
from app.services.embedding.vector_api import router as vector_store_router
from app.services.llm.admin_api import public_router as llm_models_router
from app.services.llm.admin_api import router as llm_provider_admin_router
from app.services.llm.api import router as llm_gateway_router
from app.services.project_context.api import console_router as project_context_console_router
from app.services.project_context.api import router as project_context_router
from app.technical_logging import configure_file_logging, reset_trace_id, set_trace_id
from app.technical_logging_api import router as technical_logging_router
from app.usage.api import admin_router as request_log_admin_router
from app.usage.api import router as request_log_router

logger = logging.getLogger("gateway.http")
_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def create_app(
    settings: Settings | None = None,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    """创建并装配 FastAPI 应用，允许测试替换外部依赖。"""
    resolved_settings = settings or Settings()
    container = AppContainer.build(resolved_settings, readiness_probe)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_file_logging(
            resolved_settings.log_file_path,
            resolved_settings.log_level,
            resolved_settings.log_backup_count,
        )
        await container.startup()
        yield
        await container.close()

    app = FastAPI(
        title="Agent Gateway API",
        version="0.1.0",
        description="Agent 网关中台后端，提供账户、项目、服务管理和模型网关接口。",
        root_path=resolved_settings.root_path,
        lifespan=lifespan,
    )
    app.state.container = container

    @app.middleware("http")
    async def trace_and_log_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-trace-id") or request.headers.get("x-request-id")
        trace_id = (
            incoming
            if incoming and _TRACE_ID_PATTERN.fullmatch(incoming)
            else f"trace_{uuid4().hex}"
        )
        request.state.trace_id = trace_id
        context_token = set_trace_id(trace_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "http_request_unhandled method=%s path=%s",
                request.method,
                request.url.path,
            )
            raise
        else:
            response.headers["X-Trace-ID"] = trace_id
            duration_ms = round((time.perf_counter() - started) * 1000)
            status_level = (
                logging.ERROR
                if response.status_code >= 500
                else logging.WARNING
                if response.status_code >= 400
                else logging.INFO
            )
            logger.log(
                status_level,
                "http_request_completed method=%s path=%s status=%d duration_ms=%d",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
            )
            return response
        finally:
            reset_trace_id(context_token)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(project_resources_router)
    app.include_router(api_keys_router)
    app.include_router(llm_services_router)
    app.include_router(account_services_router)
    app.include_router(llm_gateway_router)
    app.include_router(embedding_gateway_router)
    app.include_router(rag_knowledge_router)
    app.include_router(vector_store_router)
    app.include_router(rag_console_router)
    app.include_router(asr_gateway_router)
    app.include_router(asr_stream_router)
    app.include_router(asr_provider_admin_router)
    app.include_router(project_context_router)
    app.include_router(project_context_console_router)
    app.include_router(llm_models_router)
    app.include_router(llm_provider_admin_router)
    app.include_router(embedding_models_router)
    app.include_router(embedding_provider_admin_router)
    app.include_router(request_log_router)
    app.include_router(request_log_admin_router)
    app.include_router(technical_logging_router)
    return app
