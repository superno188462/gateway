"""项目模板和记忆资源 HTTP API。"""

import logging
from dataclasses import asdict
from datetime import datetime
from time import perf_counter
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.container import get_current_user, get_request_recorder, get_resource_service
from app.infrastructure.db.models import User
from app.request_logging.application import GatewayRequestRecorder
from app.resources.application import ResourceService
from app.resources.domain import (
    DIRECTORIES,
    ResourceConflictError,
    ResourceForbiddenError,
    ResourceNotFoundError,
    ResourceRecord,
    ResourceValidationError,
    ResourceVersionConflictError,
)

router = APIRouter(
    prefix="/api/admin/v1/projects/{project_id}/resources", tags=["Project Resources"]
)
logger = logging.getLogger("gateway.project_operations")
ResourceType = Literal["memory", "template"]
ResourceCategory = Literal["sessions", "profiles", "longterm", "system", "user", "assistant"]


class ResourceDirectoryResponse(BaseModel):
    """固定目录中一项不可变分类。"""

    resource_type: str = Field(description="资源类型：memory 或 template。")
    category: str = Field(description="固定目录代码，不是文件系统路径。")
    label: str = Field(description="用于界面展示的目录名称。")


class ResourceCreateRequest(BaseModel):
    """新建固定目录内的项目资源。"""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "resource_type": "template",
                    "category": "system",
                    "name": "support-agent",
                    "content": "你是一个客服助手。请用{{language}}回答以下问题：{{question}}",
                }
            ]
        }
    }

    resource_type: ResourceType = Field(description="固定资源类型。", examples=["template"])
    category: ResourceCategory = Field(description="固定目录分类。", examples=["system"])
    name: str = Field(
        min_length=1,
        max_length=200,
        description="目录内唯一文件名，不含路径。",
        examples=["support-agent"],
    )
    content: str = Field(
        max_length=100_000,
        description="用户主动保存的资源正文，最多 100,000 字符。",
        examples=["你是一个客服助手。请用{{language}}回答以下问题：{{question}}"],
    )


class ResourceUpdateRequest(BaseModel):
    """使用乐观锁更新资源名称与正文。"""

    expected_version: int = Field(
        ge=1, description="最近读取的资源版本；过期时返回 409。", examples=[1]
    )
    name: str = Field(
        min_length=1, max_length=200, description="更新后的文件名。", examples=["support-agent"]
    )
    content: str = Field(
        max_length=100_000,
        description="更新后的正文，最多 100,000 字符。",
        examples=["你是一个专业的客服助手。回答时保持简洁。"],
    )


class ResourceResponse(BaseModel):
    """项目资源及其版本和审计字段。"""

    id: UUID = Field(description="稳定资源 ID。")
    project_id: UUID = Field(description="所属项目 ID。")
    resource_type: ResourceType = Field(description="固定资源类型。")
    category: ResourceCategory = Field(description="固定目录分类。")
    name: str = Field(description="文件名。")
    content: str = Field(description="资源正文；与网关调用日志分离。")
    version: int = Field(description="每次成功修改或删除后递增的版本号。")
    created_by: UUID = Field(description="创建者用户 ID。")
    updated_by: UUID = Field(description="最近修改者用户 ID。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最近更新时间。")


class ResourceErrorDetail(BaseModel):
    """资源 API 稳定错误码和用户可读原因。"""

    code: str = Field(description="机器可读错误码，例如 version_conflict。")
    message: str = Field(description="可展示给用户的错误原因。")


class ResourceErrorResponse(BaseModel):
    """资源 API 错误响应结构。"""

    detail: ResourceErrorDetail = Field(description="结构化错误详情。")


READ_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ResourceErrorResponse, "description": "需要有效登录令牌。"},
    403: {"model": ResourceErrorResponse, "description": "无权访问该项目。"},
    404: {"model": ResourceErrorResponse, "description": "项目或资源不存在。"},
}
WRITE_ERRORS: dict[int | str, dict[str, Any]] = {
    **READ_ERRORS,
    409: {"model": ResourceErrorResponse, "description": "名称冲突或资源版本已过期。"},
    422: {"model": ResourceErrorResponse, "description": "请求字段或固定目录无效。"},
}


def map_resource_error(error: RuntimeError) -> HTTPException:
    """将资源用例错误转换为稳定、可读的 HTTP 错误。"""
    if isinstance(error, ResourceNotFoundError):
        return HTTPException(
            status_code=404, detail={"code": "resource_not_found", "message": str(error)}
        )
    if isinstance(error, ResourceForbiddenError):
        return HTTPException(
            status_code=403, detail={"code": "resource_forbidden", "message": str(error)}
        )
    if isinstance(error, ResourceConflictError):
        code = (
            "version_conflict"
            if isinstance(error, ResourceVersionConflictError)
            else "resource_name_conflict"
        )
        return HTTPException(status_code=409, detail={"code": code, "message": str(error)})
    if isinstance(error, ResourceValidationError):
        return HTTPException(
            status_code=422, detail={"code": "invalid_resource", "message": str(error)}
        )
    raise error


def _as_response(resource: ResourceRecord) -> ResourceResponse:
    return ResourceResponse(**asdict(resource))


async def _record_resource_operation(
    recorder: GatewayRequestRecorder,
    request: Request,
    project_id: UUID,
    current_user: User,
    operation: str,
    description: str,
    started_at: float,
) -> None:
    """Write sanitized project operation metadata without risking the completed mutation."""
    try:
        await recorder.record_project_operation(
            trace_id=getattr(request.state, "trace_id", ""),
            project_id=project_id,
            actor_user_id=current_user.id,
            actor_username=current_user.username,
            operation=operation,
            description=description,
            latency_ms=round((perf_counter() - started_at) * 1000),
        )
    except Exception:
        logger.exception(
            "project_operation_log_write_failed project_id=%s actor_user_id=%s operation=%s",
            project_id,
            current_user.id,
            operation,
        )


@router.get(
    "/directories",
    response_model=list[ResourceDirectoryResponse],
    summary="查询项目固定资源目录",
    responses=READ_ERRORS,
)
async def list_resource_directories(
    project_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
) -> list[ResourceDirectoryResponse]:
    """返回固定目录分类；目录不可新增、重命名或删除。"""
    try:
        await resource_service.list_for_project(
            project_id, current_user.id, current_user.role == "admin", None, None
        )
    except RuntimeError as error:
        raise map_resource_error(error) from error
    return [
        ResourceDirectoryResponse(resource_type=resource_type, category=category, label=label)
        for resource_type, category, label in DIRECTORIES
    ]


@router.get(
    "", response_model=list[ResourceResponse], summary="列出项目资源", responses=READ_ERRORS
)
async def list_resources(
    project_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
    resource_type: Annotated[
        ResourceType | None, Query(description="固定资源类型。若提供则必须同时提供 category。")
    ] = None,
    category: Annotated[ResourceCategory | None, Query(description="固定目录分类。")] = None,
) -> list[ResourceResponse]:
    """按项目访问权限列出有效资源；默认返回六个固定目录的全部资源。"""
    try:
        resources = await resource_service.list_for_project(
            project_id,
            current_user.id,
            current_user.role == "admin",
            resource_type,
            category,
        )
    except RuntimeError as error:
        raise map_resource_error(error) from error
    return [_as_response(resource) for resource in resources]


@router.post(
    "",
    response_model=ResourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建项目资源",
    responses=WRITE_ERRORS,
)
async def create_resource(
    project_id: UUID,
    payload: ResourceCreateRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ResourceResponse:
    """创建目录内同名唯一的资源；需有项目 owner/editor 权限。"""
    try:
        started_at = perf_counter()
        resource = await resource_service.create(
            project_id,
            current_user.id,
            payload.resource_type,
            payload.category,
            payload.name,
            payload.content,
        )
    except RuntimeError as error:
        raise map_resource_error(error) from error
    await _record_resource_operation(
        recorder,
        request,
        project_id,
        current_user,
        f"{resource.resource_type}.create",
        (
            f"创建项目资源：{resource.resource_type}/{resource.category}/{resource.name}"
            f"（ID: {resource.id}）"
        ),
        started_at,
    )
    return _as_response(resource)


@router.get(
    "/{resource_id}",
    response_model=ResourceResponse,
    summary="读取项目资源",
    responses=READ_ERRORS,
)
async def get_resource(
    project_id: UUID,
    resource_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
) -> ResourceResponse:
    """同时按项目 ID 和资源 ID 查找，防止跨项目读取。"""
    try:
        resource = await resource_service.get(
            project_id, resource_id, current_user.id, current_user.role == "admin"
        )
    except RuntimeError as error:
        raise map_resource_error(error) from error
    return _as_response(resource)


@router.patch(
    "/{resource_id}",
    response_model=ResourceResponse,
    summary="更新项目资源",
    responses=WRITE_ERRORS,
)
async def update_resource(
    project_id: UUID,
    resource_id: UUID,
    payload: ResourceUpdateRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ResourceResponse:
    """版本匹配时更新并递增版本；陈旧版本返回 409 version_conflict。"""
    try:
        started_at = perf_counter()
        resource = await resource_service.update(
            project_id,
            resource_id,
            current_user.id,
            current_user.role == "admin",
            payload.expected_version,
            payload.name,
            payload.content,
        )
    except RuntimeError as error:
        raise map_resource_error(error) from error
    await _record_resource_operation(
        recorder,
        request,
        project_id,
        current_user,
        f"{resource.resource_type}.update",
        (
            f"更新项目资源：{resource.resource_type}/{resource.category}/{resource.name}"
            f"（ID: {resource.id}，版本: v{resource.version}）"
        ),
        started_at,
    )
    return _as_response(resource)


@router.delete(
    "/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="软删除项目资源",
    responses=WRITE_ERRORS,
)
async def delete_resource(
    project_id: UUID,
    resource_id: UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
    expected_version: Annotated[int, Query(ge=1, description="最近读取的资源版本。")],
) -> Response:
    """版本匹配时设置软删除时间；删除项从默认目录列表中隐藏。"""
    try:
        started_at = perf_counter()
        await resource_service.soft_delete(
            project_id,
            resource_id,
            current_user.id,
            current_user.role == "admin",
            expected_version,
        )
    except RuntimeError as error:
        raise map_resource_error(error) from error
    await _record_resource_operation(
        recorder,
        request,
        project_id,
        current_user,
        "resource.delete",
        f"删除项目资源（ID: {resource_id}）",
        started_at,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
