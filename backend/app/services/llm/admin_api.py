"""管理员维护 LLM 上游供应商连接、优先级和连通性测试的 API。"""

from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.container import (
    get_current_admin,
    get_current_user,
    get_http_client,
    get_llm_configuration_service,
)
from app.infrastructure.db.models import User
from app.services.llm.configuration import (
    LlmConfigurationConflictError,
    LlmConfigurationNotFoundError,
    LlmConfigurationService,
    LlmConfigurationValidationError,
    LlmProviderInfo,
    LlmProviderRouteGroup,
    ProviderTestResult,
)

router = APIRouter(prefix="/api/admin/v1/llm", tags=["LLM Provider Management"])
public_router = APIRouter(prefix="/api/v1/llm", tags=["LLM"])


class PublicRouteGroupResponse(BaseModel):
    """调用方可用的供应商名称和模型路由前缀摘要。"""

    prefix: str | None = Field(description="null 表示默认池；非空值可作为 model 前缀。")
    suppliers: list[str] = Field(description="去重后的供应商显示名称。")
    connection_count: int = Field(description="当前启用的上游连接数量。")


class PublicProviderCatalogResponse(BaseModel):
    groups: list[PublicRouteGroupResponse]


class ProviderResponse(BaseModel):
    """管理员连接响应；包含仅管理员可见的解密 API Key，不返回数据库密文。"""

    id: UUID
    name: str = Field(description="管理员内部识别该 API 连接的名称。")
    supplier_name: str = Field(description="面向用户展示的供应商名称；同供应商连接可重复。")
    route_prefix: str | None = Field(
        description="可选模型路由前缀；相同前缀的连接组成独立故障切换组。"
    )
    base_url: str
    status: Literal["active", "disabled"]
    priority: int = Field(description="越小越先尝试；上游失败时按此顺序切换。")
    api_key_configured: bool
    api_key: str | None = Field(
        description="供应商 API Key 明文，仅管理员接口返回；未配置解密密钥时为 null。"
    )
    last_tested_at: str | None
    last_test_success: bool | None
    last_test_message: str | None

    @classmethod
    def from_info(cls, info: LlmProviderInfo) -> "ProviderResponse":
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
            last_tested_at=(info.last_tested_at.isoformat() if info.last_tested_at else None),
            last_test_success=info.last_test_success,
            last_test_message=info.last_test_message,
        )


class ProviderTestResponse(BaseModel):
    """供应商连通性测试结果。"""

    success: bool
    message: str
    tested_at: str

    @classmethod
    def from_result(cls, result: ProviderTestResult) -> "ProviderTestResponse":
        return cls(
            success=result.success,
            message=result.message,
            tested_at=result.tested_at.isoformat(),
        )


class CreateProviderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="管理员自定义的供应商名称。")
    supplier_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="面向用户展示的供应商名称；省略时沿用连接名称。",
    )
    route_prefix: str | None = Field(
        default=None,
        max_length=64,
        description="可选模型路由前缀，例如 volc；留空表示只参与默认池。",
    )
    base_url: str = Field(max_length=500, description="OpenAI 兼容 API Base URL，通常以 /v1 结尾。")
    api_key: str = Field(min_length=1, max_length=4000, description="上游供应商 API Key，仅写入。")


class UpdateProviderRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    supplier_name: str | None = Field(default=None, min_length=1, max_length=100)
    route_prefix: str | None = Field(default=None, max_length=64, description="空字符串清除前缀。")
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000, description="留空表示不轮换密钥。")
    status: Literal["active", "disabled"] | None = None
    priority: int | None = Field(default=None, ge=0, le=1_000_000, description="越小越优先。")


class ProviderTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200, description="用于该连接测试的上游模型名。")


def _raise_configuration_error(error: RuntimeError) -> HTTPException:
    if isinstance(error, LlmConfigurationNotFoundError):
        return HTTPException(status_code=404, detail={"code": "not_found", "message": str(error)})
    if isinstance(error, LlmConfigurationConflictError):
        return HTTPException(status_code=409, detail={"code": "conflict", "message": str(error)})
    if isinstance(error, LlmConfigurationValidationError):
        return HTTPException(
            status_code=422, detail={"code": "invalid_config", "message": str(error)}
        )
    return HTTPException(status_code=400, detail={"code": "invalid_request", "message": str(error)})


@router.get("/providers", response_model=list[ProviderResponse], summary="列出 LLM 供应商配置")
async def list_providers(
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> list[ProviderResponse]:
    """管理员查看供应商 URL、连接优先级和密钥配置状态。"""
    return [ProviderResponse.from_info(item) for item in await service.list_providers()]


@router.post(
    "/providers",
    response_model=ProviderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建 LLM 供应商",
    responses={409: {"description": "相同 Base URL 和 API Key 的 API 已存在。"}},
)
async def create_provider(
    payload: CreateProviderRequest,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> ProviderResponse:
    """创建 OpenAI 兼容供应商，并在服务端加密保存 API Key。"""
    try:
        result = await service.create_provider(
            payload.name,
            payload.base_url,
            payload.api_key,
            payload.route_prefix,
            payload.supplier_name,
        )
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error
    return ProviderResponse.from_info(result)


@router.patch(
    "/providers/{provider_id}",
    response_model=ProviderResponse,
    summary="修改 LLM 供应商",
    responses={409: {"description": "修改后会与仓库中已有 API 重复。"}},
)
async def update_provider(
    provider_id: UUID,
    payload: UpdateProviderRequest,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> ProviderResponse:
    """更新连接参数；提交新 API Key 时轮换，留空保持原密钥。"""
    try:
        result = await service.update_provider(
            provider_id,
            name=payload.name,
            supplier_name=payload.supplier_name,
            base_url=payload.base_url,
            api_key=payload.api_key,
            status=payload.status,
            priority=payload.priority,
            route_prefix=payload.route_prefix,
        )
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error
    return ProviderResponse.from_info(result)


@router.post(
    "/providers/{provider_id}/test",
    response_model=ProviderTestResponse,
    summary="测试 LLM 供应商连通性",
)
async def test_provider(
    provider_id: UUID,
    payload: ProviderTestRequest,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> ProviderTestResponse:
    """使用管理员提供的模型名发送最小请求，不返回上游原始响应或密钥。"""
    try:
        result = await service.test_provider(provider_id, payload.model, http_client)
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error
    return ProviderTestResponse.from_result(result)


@router.delete(
    "/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除 LLM 供应商"
)
async def delete_provider(
    provider_id: UUID,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> None:
    """删除供应商连接。"""
    try:
        await service.delete_provider(provider_id)
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error


@public_router.get("/models", response_model=list[str], summary="列出网关内置的 LLM 模型")
async def list_available_models(
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> list[str]:
    """返回网关内置模型；上游模型由调用方提供，无法从静态目录枚举。"""
    return list(await service.active_model_codes())


@public_router.get(
    "/provider-catalog",
    response_model=PublicProviderCatalogResponse,
    summary="查看可用的 LLM 路由前缀",
)
async def get_public_provider_catalog(
    _: Annotated[User, Depends(get_current_user)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> PublicProviderCatalogResponse:
    """认证用户查看启用的路由组及供应商显示名，不返回敏感配置。"""
    groups: tuple[LlmProviderRouteGroup, ...] = await service.list_public_route_groups()
    return PublicProviderCatalogResponse(
        groups=[
            PublicRouteGroupResponse(
                prefix=group.prefix,
                suppliers=list(group.supplier_names),
                connection_count=group.connection_count,
            )
            for group in groups
        ]
    )
