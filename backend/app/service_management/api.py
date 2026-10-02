"""服务目录、项目申请、个人额度和管理额度 API。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.container import (
    get_current_admin,
    get_current_user,
    get_request_recorder,
    get_service_management,
)
from app.domain.service_catalog import ServiceCatalogItem
from app.infrastructure.db.models import User
from app.request_logging.application import GatewayRequestRecorder
from app.request_logging.operations import record_project_operation
from app.service_management.application import (
    ProjectServiceInfo,
    ProjectServiceManagement,
    ServiceAccessConflictError,
    ServiceAccessForbiddenError,
    ServiceAccessNotFoundError,
    ServiceProjectInfo,
    UserQuotaTarget,
    UserServiceQuotaExceededError,
    UserServiceQuotaInfo,
)

router = APIRouter(prefix="/api/admin/v1", tags=["Project Services"])


class ServiceCatalogResponse(BaseModel):
    """目前可申请的服务目录项。"""

    code: str = Field(description="稳定服务代码。")
    name: str = Field(description="服务展示名称。")
    models: list[str] = Field(description="此服务包含的模型标识。")
    quota_unit: Literal["tokens", "seconds"] | None = Field(
        description="月额度计量单位；ASR 使用音频秒数，null 表示不占用月度额度。"
    )

    @classmethod
    def from_item(cls, item: ServiceCatalogItem) -> "ServiceCatalogResponse":
        return cls(
            code=item.code,
            name=item.name,
            models=list(item.models),
            quota_unit=item.quota_unit,
        )


class ProjectServiceResponse(BaseModel):
    """项目的有效服务订阅及当前 UTC 月用量。"""

    project_id: UUID = Field(description="订阅所属项目；调用方无需把该 ID 传给模型网关。")
    service_code: str = Field(description="服务目录代码。")
    quota_unit: str = Field(description="本服务月额度的计量单位。")
    monthly_token_limit: int | None = Field(
        description="每月上限数值，单位由 quota_unit 指定；保留旧字段名以兼容现有客户端。"
    )
    status: Literal["active", "suspended"] = Field(description="服务订阅状态。")
    period_start: str = Field(description="当前用量周期起始日，UTC 月初。")
    tokens_used: int = Field(description="本周期已确认消耗，单位由 quota_unit 指定。")
    tokens_reserved: int = Field(description="并发请求预留中额度，单位由 quota_unit 指定。")

    @classmethod
    def from_info(cls, item: ProjectServiceInfo) -> "ProjectServiceResponse":
        return cls(
            project_id=item.project_id,
            service_code=item.service_code,
            quota_unit=item.quota_unit,
            monthly_token_limit=item.monthly_token_limit,
            status=item.status,
            period_start=item.period_start,
            tokens_used=item.tokens_used,
            tokens_reserved=item.tokens_reserved,
        )


class ServiceProjectResponse(BaseModel):
    """当前用户可访问且已开通指定服务的项目。"""

    id: UUID
    name: str
    description: str | None
    visibility: Literal["public", "private"]
    owner_id: UUID
    service_code: str
    quota_unit: str
    monthly_token_limit: int | None
    service_status: Literal["active", "suspended"]
    tokens_used: int
    tokens_reserved: int

    @classmethod
    def from_info(cls, item: ServiceProjectInfo) -> "ServiceProjectResponse":
        project = item.project
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            visibility=project.visibility,
            owner_id=project.owner_id,
            service_code=item.service_code,
            quota_unit=item.quota_unit,
            monthly_token_limit=item.monthly_token_limit,
            service_status=item.status,
            tokens_used=item.tokens_used,
            tokens_reserved=item.tokens_reserved,
        )


class ServiceProjectPageResponse(BaseModel):
    items: list[ServiceProjectResponse]
    total: int
    offset: int
    limit: int


class ServiceApplicationRequest(BaseModel):
    service_code: str = Field(description="要申请的服务目录代码。")
    monthly_token_limit: int | None = Field(
        default=None,
        ge=1,
        description="项目月度额度数值，单位由服务目录 quota_unit 指定；上下文字段不提供此字段。",
    )


class ProjectServiceAllocationRequest(BaseModel):
    monthly_token_limit: int = Field(
        ge=1, description="新的项目月度额度数值；单位由服务目录 quota_unit 指定。"
    )


class UserServiceQuotaResponse(BaseModel):
    """个人服务能力和跨项目额度分配概览。"""

    service_code: str = Field(description="服务目录代码。")
    name: str = Field(description="服务展示名称。")
    models: list[str] = Field(description="服务包含的模型。")
    quota_unit: str = Field(description="额度计量单位，如 tokens 或 seconds。")
    monthly_token_limit: int = Field(
        description="管理员授予的用户月度总上限；数值单位由 quota_unit 指定。"
    )
    allocated_tokens: int = Field(
        description="已分配到该用户所有项目的额度合计，单位由 quota_unit 指定。"
    )
    available_tokens: int = Field(description="还能分配给项目的额度，单位由 quota_unit 指定。")
    tokens_used: int = Field(description="该用户所有项目本月实际消耗合计，单位由 quota_unit 指定。")
    tokens_reserved: int = Field(
        description="该用户所有项目当前请求预留合计，单位由 quota_unit 指定。"
    )

    @classmethod
    def from_info(cls, item: UserServiceQuotaInfo) -> "UserServiceQuotaResponse":
        return cls(
            service_code=item.service_code,
            name=item.name,
            models=list(item.models),
            quota_unit=item.quota_unit,
            monthly_token_limit=item.monthly_token_limit,
            allocated_tokens=item.allocated_tokens,
            available_tokens=item.available_tokens,
            tokens_used=item.tokens_used,
            tokens_reserved=item.tokens_reserved,
        )


class SetUserServiceQuotaRequest(BaseModel):
    monthly_token_limit: int = Field(
        ge=0,
        description="用户每月服务总上限；允许低于项目分配总额，0 会暂停调用且保留项目分配记录。",
    )


class QuotaTargetResponse(BaseModel):
    """最少字段的用户识别结果，禁止返回账户凭据。"""

    id: UUID = Field(description="目标用户 ID。")
    username: str = Field(description="唯一用户名。")
    role: Literal["admin", "user"] = Field(description="当前平台角色。")

    @classmethod
    def from_target(cls, target: UserQuotaTarget) -> "QuotaTargetResponse":
        return cls(id=target.id, username=target.username, role=target.role)


def map_error(error: RuntimeError) -> HTTPException:
    if isinstance(error, ServiceAccessNotFoundError):
        return HTTPException(status_code=404, detail={"code": "not_found", "message": str(error)})
    if isinstance(error, ServiceAccessForbiddenError):
        return HTTPException(status_code=403, detail={"code": "forbidden", "message": str(error)})
    if isinstance(error, UserServiceQuotaExceededError):
        return HTTPException(
            status_code=409, detail={"code": "quota_conflict", "message": str(error)}
        )
    if isinstance(error, ServiceAccessConflictError):
        return HTTPException(status_code=409, detail={"code": "conflict", "message": str(error)})
    return HTTPException(status_code=400, detail={"code": "invalid_request", "message": str(error)})


@router.get("/services", response_model=list[ServiceCatalogResponse], summary="列出服务目录和模型")
async def list_service_catalog(
    _: Annotated[User, Depends(get_current_user)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
) -> list[ServiceCatalogResponse]:
    """提供当前可申请的服务目录及额度档位。"""
    return [ServiceCatalogResponse.from_item(item) for item in await service.catalog()]


@router.get(
    "/users/lookup",
    response_model=QuotaTargetResponse,
    summary="按用户名或用户 ID 查找额度目标",
    responses={403: {"description": "需要管理员权限"}, 404: {"description": "用户不存在"}},
)
async def lookup_quota_target(
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
    user_id: Annotated[
        UUID | None, Query(description="目标用户 UUID；与 username 二选一。")
    ] = None,
    username: Annotated[
        str | None,
        Query(min_length=1, max_length=100, description="目标用户的精确用户名。"),
    ] = None,
) -> QuotaTargetResponse:
    """管理员按唯一用户名或 UUID 精确查找用户。"""
    if (user_id is None) == (username is None):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_lookup", "message": "必须且只能提供 user_id 或 username"},
        )
    try:
        target = await service.find_quota_target(user_id=user_id, username=username)
    except ServiceAccessNotFoundError as error:
        raise map_error(error) from error
    return QuotaTargetResponse.from_target(target)


@router.get(
    "/users/{user_id}/services",
    response_model=list[UserServiceQuotaResponse],
    summary="查看指定用户服务月额度",
    responses={403: {"description": "需要管理员权限"}, 404: {"description": "用户不存在"}},
)
async def get_user_service_quotas(
    user_id: UUID,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
) -> list[UserServiceQuotaResponse]:
    """管理员查看指定用户的额度、项目分配及跨项目当月用量。"""
    try:
        items = await service.list_for_quota_target(user_id)
    except ServiceAccessNotFoundError as error:
        raise map_error(error) from error
    return [UserServiceQuotaResponse.from_info(item) for item in items]


@router.put(
    "/users/{user_id}/services/{service_code}",
    response_model=UserServiceQuotaResponse,
    summary="设置用户服务月额度",
)
async def set_user_service_quota(
    user_id: UUID,
    service_code: str,
    payload: SetUserServiceQuotaRequest,
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
) -> UserServiceQuotaResponse:
    """管理员配置指定用户的服务级月额度。"""
    try:
        item = await service.set_user_limit(user_id, service_code, payload.monthly_token_limit)
    except (
        ServiceAccessNotFoundError,
        ServiceAccessConflictError,
        UserServiceQuotaExceededError,
    ) as error:
        raise map_error(error) from error
    return UserServiceQuotaResponse.from_info(item)


account_router = APIRouter(prefix="/api/v1/me", tags=["My Services"])


@account_router.get(
    "/services", response_model=list[UserServiceQuotaResponse], summary="查看我的服务和额度"
)
async def list_my_services(
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
) -> list[UserServiceQuotaResponse]:
    """返回服务目录中当前用户获授的额度、项目分配和当月用量。"""
    return [
        UserServiceQuotaResponse.from_info(item)
        for item in await service.list_for_user(current_user)
    ]


@router.get(
    "/services/{service_code}/projects",
    response_model=ServiceProjectPageResponse,
    summary="列出已开通指定服务的可访问项目",
)
async def list_projects_for_service(
    service_code: str,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ServiceProjectPageResponse:
    """返回当前用户可查看且已开通指定服务的运行中项目，管理员可查看全部。"""
    try:
        items, total = await service.list_projects_for_service(
            service_code, current_user, offset, limit
        )
    except ServiceAccessNotFoundError as error:
        raise map_error(error) from error
    return ServiceProjectPageResponse(
        items=[ServiceProjectResponse.from_info(item) for item in items],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/projects/{project_id}/services",
    response_model=list[ProjectServiceResponse],
    summary="查询项目已开通服务和用量",
)
async def list_project_services(
    project_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
) -> list[ProjectServiceResponse]:
    """项目成员、公开项目 review 用户和管理员可以查看；管理员只读。"""
    try:
        items = await service.list_for_project(project_id, current_user)
    except (ServiceAccessNotFoundError, ServiceAccessForbiddenError) as error:
        raise map_error(error) from error
    return [ProjectServiceResponse.from_info(item) for item in items]


@router.post(
    "/projects/{project_id}/services",
    response_model=ProjectServiceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="申请项目服务",
)
async def apply_project_service(
    project_id: UUID,
    payload: ServiceApplicationRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ProjectServiceResponse:
    """项目 owner 提交申请后自动开通，无支付流程。"""
    try:
        await service.apply(
            project_id, current_user, payload.service_code, payload.monthly_token_limit
        )
        item = next(
            value
            for value in await service.list_for_project(project_id, current_user)
            if value.service_code == payload.service_code
        )
        await record_project_operation(
            recorder,
            request,
            current_user,
            project_id,
            "service.apply",
            f"申请项目服务 {payload.service_code}，月额度={payload.monthly_token_limit}",
        )
    except (
        ServiceAccessNotFoundError,
        ServiceAccessForbiddenError,
        ServiceAccessConflictError,
        UserServiceQuotaExceededError,
    ) as error:
        raise map_error(error) from error
    return ProjectServiceResponse.from_info(item)


@router.patch(
    "/projects/{project_id}/services/{service_code}",
    response_model=ProjectServiceResponse,
    summary="调整项目服务额度",
)
async def update_project_service_allocation(
    project_id: UUID,
    service_code: str,
    payload: ProjectServiceAllocationRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[ProjectServiceManagement, Depends(get_service_management)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ProjectServiceResponse:
    """项目 owner 可调整项目额度；执行时校验个人可分配余额和当月已用量。"""
    try:
        await service.set_allocation(
            project_id, current_user, service_code, payload.monthly_token_limit
        )
        item = next(
            value
            for value in await service.list_for_project(project_id, current_user)
            if value.service_code == service_code
        )
        await record_project_operation(
            recorder,
            request,
            current_user,
            project_id,
            "service.allocation.update",
            f"调整项目服务 {service_code} 月额度为 {payload.monthly_token_limit}",
        )
    except (
        ServiceAccessNotFoundError,
        ServiceAccessForbiddenError,
        ServiceAccessConflictError,
        UserServiceQuotaExceededError,
    ) as error:
        raise map_error(error) from error
    return ProjectServiceResponse.from_info(item)
