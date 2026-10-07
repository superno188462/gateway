"""Embedding 上游连接的管理员 API。"""

from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.container import (
    get_current_admin,
    get_embedding_configuration_service,
    get_http_client,
    get_request_recorder,
)
from app.infrastructure.db.models import User
from app.request_logging.admin_operations import record_admin_operation
from app.request_logging.application import GatewayRequestRecorder
from app.services.embedding.configuration import (
    EmbeddingConfigurationConflictError,
    EmbeddingConfigurationError,
    EmbeddingConfigurationNotFoundError,
    EmbeddingConfigurationService,
    EmbeddingConfigurationValidationError,
    EmbeddingProviderInfo,
)

router = APIRouter(prefix="/api/admin/v1/embedding", tags=["Embedding Provider Management"])
public_router = APIRouter(prefix="/api/v1/embedding", tags=["Embedding"])


class ProviderResponse(BaseModel):
    id: UUID
    name: str
    supplier_name: str
    route_prefix: str | None
    base_url: str
    status: Literal["active", "disabled"]
    priority: int
    api_key_configured: bool
    api_key: str | None
    last_tested_at: str | None
    last_test_success: bool | None
    last_test_message: str | None

    @classmethod
    def from_info(cls, info: EmbeddingProviderInfo) -> "ProviderResponse":
        return cls(
            id=info.id,
            name=info.name,
            supplier_name=info.supplier_name,
            route_prefix=info.route_prefix,
            base_url=info.base_url,
            status=info.status,
            priority=info.priority,
            api_key_configured=info.api_key_configured,
            api_key=info.api_key,
            last_tested_at=info.last_tested_at.isoformat() if info.last_tested_at else None,
            last_test_success=info.last_test_success,
            last_test_message=info.last_test_message,
        )


class CreateProviderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    supplier_name: str = Field(min_length=1, max_length=100)
    route_prefix: str | None = Field(default=None, max_length=64)
    base_url: str = Field(max_length=500)
    api_key: str = Field(min_length=1, max_length=4000)


class UpdateProviderRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    supplier_name: str | None = Field(default=None, min_length=1, max_length=100)
    route_prefix: str | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    status: Literal["active", "disabled"] | None = None
    priority: int | None = Field(default=None, ge=0, le=1_000_000)


class ProviderTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class PublicRouteGroupResponse(BaseModel):
    prefix: str | None
    suppliers: list[str]
    connection_count: int


class ProviderTestResponse(BaseModel):
    success: bool
    message: str


def map_error(error: EmbeddingConfigurationError) -> HTTPException:
    if isinstance(error, EmbeddingConfigurationNotFoundError):
        return HTTPException(status_code=404, detail={"code": "not_found", "message": str(error)})
    if isinstance(error, EmbeddingConfigurationConflictError):
        return HTTPException(status_code=409, detail={"code": "conflict", "message": str(error)})
    if isinstance(error, EmbeddingConfigurationValidationError):
        return HTTPException(
            status_code=422, detail={"code": "invalid_config", "message": str(error)}
        )
    return HTTPException(status_code=400, detail={"code": "invalid_request", "message": str(error)})


@router.get("/providers", response_model=list[ProviderResponse])
async def list_providers(
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[EmbeddingConfigurationService, Depends(get_embedding_configuration_service)],
) -> list[ProviderResponse]:
    return [ProviderResponse.from_info(item) for item in await service.list_providers()]


@router.post("/providers", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: CreateProviderRequest,
    request: Request,
    admin: Annotated[User, Depends(get_current_admin)],
    service: Annotated[EmbeddingConfigurationService, Depends(get_embedding_configuration_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ProviderResponse:
    started = perf_counter()
    try:
        info = await service.create_provider(
            payload.name,
            payload.supplier_name,
            payload.route_prefix,
            payload.base_url,
            payload.api_key,
        )
    except EmbeddingConfigurationError as error:
        await record_admin_operation(
            recorder,
            request,
            admin,
            "embedding.provider.create",
            "新增 Embedding 上游连接失败",
            status="failed",
            error_code="embedding_config_error",
            error_message="配置校验失败或连接重复",
            started_at=started,
        )
        raise map_error(error) from error
    await record_admin_operation(
        recorder,
        request,
        admin,
        "embedding.provider.create",
        (
            f"新增 Embedding 上游连接：{info.supplier_name} · {info.name}"
            f"（前缀 {info.route_prefix or '默认'}）"
        ),
        started_at=started,
    )
    return ProviderResponse.from_info(info)


@router.patch("/providers/{provider_id}", response_model=ProviderResponse)
async def update_provider(
    provider_id: UUID,
    payload: UpdateProviderRequest,
    request: Request,
    admin: Annotated[User, Depends(get_current_admin)],
    service: Annotated[EmbeddingConfigurationService, Depends(get_embedding_configuration_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ProviderResponse:
    started = perf_counter()
    try:
        info = await service.update_provider(provider_id, **payload.model_dump(exclude_unset=True))
    except EmbeddingConfigurationError as error:
        await record_admin_operation(
            recorder,
            request,
            admin,
            "embedding.provider.update",
            "修改 Embedding 上游连接失败",
            status="failed",
            error_code="embedding_config_error",
            error_message="配置校验失败、连接不存在或连接重复",
            started_at=started,
        )
        raise map_error(error) from error
    field_labels = {
        "name": "连接名称",
        "supplier_name": "供应商名称",
        "route_prefix": "模型前缀",
        "base_url": "Base URL",
        "api_key": "API Key 已轮换",
        "status": "启停状态",
        "priority": "优先级",
    }
    changed = [field_labels[field] for field in payload.model_fields_set if field in field_labels]
    await record_admin_operation(
        recorder,
        request,
        admin,
        "embedding.provider.update",
        f"修改 Embedding 上游连接：{info.supplier_name} · {info.name}（{', '.join(changed)}）",
        started_at=started,
    )
    return ProviderResponse.from_info(info)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: UUID,
    request: Request,
    admin: Annotated[User, Depends(get_current_admin)],
    service: Annotated[EmbeddingConfigurationService, Depends(get_embedding_configuration_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> None:
    started = perf_counter()
    try:
        await service.delete_provider(provider_id)
    except EmbeddingConfigurationError as error:
        await record_admin_operation(
            recorder,
            request,
            admin,
            "embedding.provider.delete",
            f"删除 Embedding 上游连接失败：{provider_id}",
            status="failed",
            error_code="embedding_config_error",
            error_message="连接不存在或删除失败",
            started_at=started,
        )
        raise map_error(error) from error
    await record_admin_operation(
        recorder,
        request,
        admin,
        "embedding.provider.delete",
        f"删除 Embedding 上游连接：{provider_id}",
        started_at=started,
    )


@router.post("/providers/{provider_id}/test", response_model=ProviderTestResponse)
async def test_provider(
    provider_id: UUID,
    payload: ProviderTestRequest,
    request: Request,
    admin: Annotated[User, Depends(get_current_admin)],
    service: Annotated[EmbeddingConfigurationService, Depends(get_embedding_configuration_service)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ProviderTestResponse:
    started = perf_counter()
    try:
        message = await service.test_provider(provider_id, payload.model, client)
        await service.set_test_result(provider_id, True, message)
        await record_admin_operation(
            recorder,
            request,
            admin,
            "embedding.provider.test",
            f"Embedding 上游连接测试成功，模型 {payload.model}；{message}",
            started_at=started,
        )
    except EmbeddingConfigurationError as error:
        if not isinstance(error, EmbeddingConfigurationNotFoundError):
            await service.set_test_result(provider_id, False, str(error))
            await record_admin_operation(
                recorder,
                request,
                admin,
                "embedding.provider.test",
                f"Embedding 上游连接测试失败，模型 {payload.model}",
                status="failed",
                error_code="embedding_provider_test_failed",
                error_message="上游未返回有效的向量结果",
                started_at=started,
            )
        raise map_error(error) from error
    except httpx.HTTPError as error:
        message = f"上游连接失败：{type(error).__name__}"
        await service.set_test_result(provider_id, False, message)
        await record_admin_operation(
            recorder,
            request,
            admin,
            "embedding.provider.test",
            f"Embedding 上游连接测试失败，模型 {payload.model}",
            status="failed",
            error_code="embedding_upstream_unavailable",
            error_message="Embedding 上游连接失败",
            started_at=started,
        )
        raise HTTPException(
            status_code=502, detail={"code": "upstream_test_failed", "message": message}
        ) from error
    except Exception as error:
        await service.set_test_result(provider_id, False, "上游返回无法识别的响应")
        await record_admin_operation(
            recorder,
            request,
            admin,
            "embedding.provider.test",
            f"Embedding 上游连接测试失败，模型 {payload.model}",
            status="failed",
            error_code="embedding_upstream_invalid_response",
            error_message="Embedding 上游返回无法识别的响应",
            started_at=started,
        )
        raise HTTPException(
            status_code=502,
            detail={"code": "upstream_test_failed", "message": "Embedding 上游返回无效响应"},
        ) from error
    return ProviderTestResponse(success=True, message=message)


@public_router.get("/provider-catalog", response_model=list[PublicRouteGroupResponse])
async def provider_catalog(
    service: Annotated[EmbeddingConfigurationService, Depends(get_embedding_configuration_service)],
) -> list[PublicRouteGroupResponse]:
    return [
        PublicRouteGroupResponse(
            prefix=item.prefix,
            suppliers=list(item.supplier_names),
            connection_count=item.connection_count,
        )
        for item in await service.list_public_route_groups()
    ]
