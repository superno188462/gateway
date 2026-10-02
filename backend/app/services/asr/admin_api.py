"""管理员配置 ASR 上游连接。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.container import get_asr_configuration_service, get_current_admin
from app.infrastructure.db.models import User
from app.services.asr.configuration import (
    AsrConfigurationError,
    AsrConfigurationService,
    AsrProviderInfo,
)

router = APIRouter(prefix="/api/admin/v1/asr", tags=["ASR Provider Management"])


class ProviderPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="管理员用于区分连接的名称。")
    supplier_name: Literal["火山引擎"] = Field(
        default="火山引擎", description="当前仅支持火山引擎。"
    )
    route_prefix: Literal["volc"] = Field(default="volc", description="客户端模型路由前缀。")
    model_name: str = Field(
        default="doubao-seed-asr-2.0",
        min_length=1,
        max_length=200,
        description="客户端 start.model 传入的公开模型名；不包含服务商前缀。",
    )
    resource_id: str = Field(min_length=1, max_length=200, description="火山 ASR Resource ID。")
    file_transcription_url: str = Field(
        max_length=500,
        description="整段录音文件转写使用的 WebSocket URL。",
    )
    realtime_url: str = Field(
        max_length=500,
        description="实时音频流识别使用的 WebSocket URL。",
    )
    api_key: str = Field(min_length=1, max_length=4000)


class ProviderUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    supplier_name: Literal["火山引擎"] | None = None
    route_prefix: Literal["volc"] | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    resource_id: str | None = Field(default=None, min_length=1, max_length=200)
    file_transcription_url: str | None = Field(default=None, max_length=500)
    realtime_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    status: Literal["active", "disabled"] | None = None


class ProviderResponse(BaseModel):
    id: UUID
    name: str
    supplier_name: str
    route_prefix: Literal["volc"]
    model_name: str
    resource_id: str
    file_transcription_url: str
    realtime_url: str
    status: Literal["active", "disabled"]
    api_key_configured: bool
    api_key: str | None = Field(description="明文仅在管理员 API 响应中返回。")
    created_at: str


def _response(info: AsrProviderInfo) -> ProviderResponse:
    return ProviderResponse(
        id=info.id,
        name=info.name,
        supplier_name=info.supplier_name,
        route_prefix=info.route_prefix,
        model_name=info.model_name,
        resource_id=info.resource_id,
        file_transcription_url=info.file_transcription_url,
        realtime_url=info.realtime_url,
        status=info.status,
        api_key_configured=info.api_key_configured,
        api_key=info.api_key,
        created_at=info.created_at.isoformat(),
    )


def _raise(error: AsrConfigurationError) -> HTTPException:
    text = str(error)
    status_code = 404 if "不存在" in text else 409 if "已存在" in text else 422
    code = (
        "not_found"
        if status_code == 404
        else "conflict"
        if status_code == 409
        else "invalid_config"
    )
    return HTTPException(status_code, detail={"code": code, "message": text})


@router.get("/providers", response_model=list[ProviderResponse], summary="列出 ASR 上游连接")
async def list_providers(
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[AsrConfigurationService, Depends(get_asr_configuration_service)],
) -> list[ProviderResponse]:
    return [_response(item) for item in await service.list_providers()]


@router.post(
    "/providers",
    status_code=status.HTTP_201_CREATED,
    response_model=ProviderResponse,
    summary="添加 ASR 上游连接",
)
async def create_provider(
    payload: ProviderPayload,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[AsrConfigurationService, Depends(get_asr_configuration_service)],
) -> ProviderResponse:
    try:
        return _response(await service.create_provider(**payload.model_dump()))
    except AsrConfigurationError as error:
        raise _raise(error) from error


@router.put(
    "/providers/{provider_id}", response_model=ProviderResponse, summary="修改 ASR 上游连接"
)
async def update_provider(
    provider_id: UUID,
    payload: ProviderUpdatePayload,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[AsrConfigurationService, Depends(get_asr_configuration_service)],
) -> ProviderResponse:
    try:
        return _response(
            await service.update_provider(
                provider_id,
                **payload.model_dump(exclude_unset=True),
            )
        )
    except AsrConfigurationError as error:
        raise _raise(error) from error
    except IntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": "仓库中已存在相同 Resource ID、端点和 API Key 的 ASR API",
            },
        ) from error


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: UUID,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[AsrConfigurationService, Depends(get_asr_configuration_service)],
) -> None:
    try:
        await service.delete_provider(provider_id)
    except AsrConfigurationError as error:
        raise _raise(error) from error
