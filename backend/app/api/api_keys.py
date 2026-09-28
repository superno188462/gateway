"""项目 API Key 管理端点。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from app.application.api_keys import (
    ApiKeyConfigurationError,
    ApiKeyForbiddenError,
    ApiKeyInfo,
    ApiKeyNotFoundError,
    ApiKeyService,
    CreatedApiKey,
)
from app.container import get_api_key_service, get_current_user
from app.infrastructure.db.models import User

router = APIRouter(prefix="/api/admin/v1/projects/{project_id}/keys", tags=["API Keys"])


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_at: datetime | None = Field(default=None, description="省略表示永不过期。")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Key 名称不能为空")
        return normalized


class ApiKeyResponse(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    key_prefix: str
    key_last_four: str
    secret: str | None = Field(
        description="完整 Key；仅 owner/editor 可查看，旧版无法恢复的 Key 为 null。"
    )
    status: Literal["active", "revoked", "expired"]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    @classmethod
    def from_info(cls, info: ApiKeyInfo) -> "ApiKeyResponse":
        return cls(
            id=info.id,
            project_id=info.project_id,
            name=info.name,
            key_prefix=info.key_prefix,
            key_last_four=info.key_last_four,
            secret=info.secret,
            status=info.status,
            expires_at=info.expires_at,
            last_used_at=info.last_used_at,
            revoked_at=info.revoked_at,
            created_at=info.created_at,
        )


class ApiKeyCreatedResponse(ApiKeyResponse):
    secret: str = Field(description="完整 Key；项目 owner/editor 之后仍可查看。")


def map_api_key_error(error: RuntimeError) -> HTTPException:
    if isinstance(error, ApiKeyConfigurationError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "api_key_configuration_error", "message": str(error)},
        )
    if isinstance(error, ApiKeyNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, ApiKeyForbiddenError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@router.get("", response_model=list[ApiKeyResponse], summary="查询项目 API Key")
async def list_api_keys(
    project_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[ApiKeyService, Depends(get_api_key_service)],
) -> list[ApiKeyResponse]:
    try:
        items = await service.list_for_project(project_id, current_user)
    except (ApiKeyNotFoundError, ApiKeyForbiddenError, ApiKeyConfigurationError) as error:
        raise map_api_key_error(error) from error
    return [ApiKeyResponse.from_info(item) for item in items]


@router.post(
    "",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建项目 API Key",
)
async def create_api_key(
    project_id: UUID,
    payload: ApiKeyCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[ApiKeyService, Depends(get_api_key_service)],
) -> ApiKeyCreatedResponse:
    try:
        created: CreatedApiKey = await service.create(
            project_id, current_user, payload.name, payload.expires_at
        )
    except (ApiKeyNotFoundError, ApiKeyForbiddenError) as error:
        raise map_api_key_error(error) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    return ApiKeyCreatedResponse(
        **ApiKeyResponse.from_info(created.info).model_dump(exclude={"secret"}),
        secret=created.secret,
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT, summary="撤销项目 API Key")
async def revoke_api_key(
    project_id: UUID,
    key_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[ApiKeyService, Depends(get_api_key_service)],
) -> Response:
    try:
        await service.revoke(project_id, key_id, current_user)
    except (ApiKeyNotFoundError, ApiKeyForbiddenError) as error:
        raise map_api_key_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
