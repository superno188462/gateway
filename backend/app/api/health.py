"""存活和就绪检查端点。"""

from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.container import get_readiness_probe
from app.domain.health import ReadinessProbe

router = APIRouter(prefix="/health", tags=["Health"])


class HealthResponse(BaseModel):
    """健康检查成功响应。"""

    status: Literal["ok"] = Field(description="当前探针状态。")


class ErrorResponse(BaseModel):
    """不暴露底层异常的公共错误响应。"""

    code: str = Field(description="供调用方稳定判断的错误码。")
    message: str = Field(description="可安全展示的错误消息。")
    request_id: str = Field(description="用于关联服务端日志的请求标识。")


@router.get(
    "/live",
    response_model=HealthResponse,
    summary="检查应用进程是否可响应",
)
async def liveness() -> HealthResponse:
    """返回进程存活状态，不访问外部依赖。"""
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
    summary="检查应用是否可接收依赖数据库的请求",
)
async def readiness(
    probe: Annotated[ReadinessProbe, Depends(get_readiness_probe)],
) -> HealthResponse | JSONResponse:
    """验证数据库可用性，并将底层异常转换为安全的稳定错误。"""
    try:
        await probe.check()
    except Exception:
        payload = ErrorResponse(
            code="database_unavailable",
            message="数据库当前不可用",
            request_id=str(uuid4()),
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload.model_dump(),
        )
    return HealthResponse(status="ok")
