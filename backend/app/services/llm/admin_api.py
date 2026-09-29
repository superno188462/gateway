"""管理员维护 LLM 上游供应商和模型映射的 API。"""

from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.container import get_current_admin, get_http_client, get_llm_configuration_service
from app.infrastructure.db.models import User
from app.services.llm.configuration import (
    LlmConfigurationConflictError,
    LlmConfigurationNotFoundError,
    LlmConfigurationService,
    LlmConfigurationValidationError,
    LlmModelInfo,
    LlmProviderInfo,
    ProviderTestResult,
)

router = APIRouter(prefix="/api/admin/v1/llm", tags=["LLM Provider Management"])
public_router = APIRouter(prefix="/api/v1/llm", tags=["LLM"])


class ModelResponse(BaseModel):
    """网关公开模型及上游模型映射。"""

    id: UUID
    model_code: str = Field(description="调用方传给网关的公开模型名。")
    upstream_model: str = Field(description="发送给第三方供应商的模型名。")
    status: Literal["active", "disabled"]

    @classmethod
    def from_info(cls, info: LlmModelInfo) -> "ModelResponse":
        return cls(
            id=info.id,
            model_code=info.model_code,
            upstream_model=info.upstream_model,
            status=info.status,
        )


class ProviderResponse(BaseModel):
    """供应商可公开配置；永不包含其 API Key 或密文。"""

    id: UUID
    name: str
    base_url: str
    status: Literal["active", "disabled"]
    api_key_configured: bool
    last_tested_at: str | None
    last_test_success: bool | None
    last_test_message: str | None
    models: list[ModelResponse]

    @classmethod
    def from_info(cls, info: LlmProviderInfo) -> "ProviderResponse":
        return cls(
            id=info.id,
            name=info.name,
            base_url=info.base_url,
            status=info.status,
            api_key_configured=info.api_key_configured,
            last_tested_at=(info.last_tested_at.isoformat() if info.last_tested_at else None),
            last_test_success=info.last_test_success,
            last_test_message=info.last_test_message,
            models=[ModelResponse.from_info(model) for model in info.models],
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
    base_url: str = Field(max_length=500, description="OpenAI 兼容 API Base URL，通常以 /v1 结尾。")
    api_key: str = Field(min_length=1, max_length=4000, description="上游供应商 API Key，仅写入。")


class UpdateProviderRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000, description="留空表示不轮换密钥。")
    status: Literal["active", "disabled"] | None = None


class CreateModelRequest(BaseModel):
    model_code: str = Field(min_length=1, max_length=100, description="网关调用方使用的模型标识。")
    upstream_model: str = Field(min_length=1, max_length=200, description="供应商定义的模型标识。")


class UpdateModelRequest(BaseModel):
    model_code: str | None = Field(default=None, min_length=1, max_length=100)
    upstream_model: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "disabled"] | None = None


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
    """管理员查看供应商 URL、密钥配置状态和模型映射。"""
    return [ProviderResponse.from_info(item) for item in await service.list_providers()]


@router.post(
    "/providers",
    response_model=ProviderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建 LLM 供应商",
)
async def create_provider(
    payload: CreateProviderRequest,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> ProviderResponse:
    """创建 OpenAI 兼容供应商，并在服务端加密保存 API Key。"""
    try:
        result = await service.create_provider(payload.name, payload.base_url, payload.api_key)
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error
    return ProviderResponse.from_info(result)


@router.patch(
    "/providers/{provider_id}", response_model=ProviderResponse, summary="修改 LLM 供应商"
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
            base_url=payload.base_url,
            api_key=payload.api_key,
            status=payload.status,
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
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> ProviderTestResponse:
    """使用一个已启用模型发送最小聊天请求测试连接，不返回上游原始响应或密钥。"""
    try:
        result = await service.test_provider(provider_id, http_client)
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
    """删除供应商及其模型映射。"""
    try:
        await service.delete_provider(provider_id)
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error


@router.post(
    "/providers/{provider_id}/models",
    response_model=ModelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="为供应商添加模型",
)
async def create_model(
    provider_id: UUID,
    payload: CreateModelRequest,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> ModelResponse:
    """添加网关公开名称与供应商上游模型名的映射。"""
    try:
        result = await service.create_model(provider_id, payload.model_code, payload.upstream_model)
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error
    return ModelResponse.from_info(result)


@router.patch("/models/{model_id}", response_model=ModelResponse, summary="修改 LLM 模型映射")
async def update_model(
    model_id: UUID,
    payload: UpdateModelRequest,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> ModelResponse:
    """修改对外名称、上游模型名或启用状态。"""
    try:
        result = await service.update_model(
            model_id,
            model_code=payload.model_code,
            upstream_model=payload.upstream_model,
            status=payload.status,
        )
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error
    return ModelResponse.from_info(result)


@router.delete(
    "/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除 LLM 模型映射"
)
async def delete_model(
    model_id: UUID,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> None:
    """删除模型映射。"""
    try:
        await service.delete_model(model_id)
    except RuntimeError as error:
        raise _raise_configuration_error(error) from error


@public_router.get("/models", response_model=list[str], summary="列出可调用的 LLM 模型")
async def list_available_models(
    service: Annotated[LlmConfigurationService, Depends(get_llm_configuration_service)],
) -> list[str]:
    """返回可传入 Chat Completions 的公开模型名，不含供应商信息。"""
    return list(await service.active_model_codes())
