"""项目和项目成员管理端点。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from app.application.projects import (
    ProjectConflictError,
    ProjectForbiddenError,
    ProjectMemberInfo,
    ProjectNotFoundError,
    ProjectService,
    UserNotFoundError,
)
from app.container import get_current_user, get_project_service, get_request_recorder
from app.infrastructure.db.models import Project, User
from app.request_logging.application import GatewayRequestRecorder
from app.request_logging.operations import record_project_operation

router = APIRouter(prefix="/api/admin/v1/projects", tags=["Projects"])
ProjectTagName = Annotated[str, Field(min_length=1, max_length=20)]


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    visibility: Literal["public", "private"] = "private"
    tags: list[ProjectTagName] = Field(default_factory=list, max_length=5)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "inactive"] | None = None
    visibility: Literal["public", "private"] | None = None
    tags: list[ProjectTagName] | None = Field(default=None, max_length=5)


class ProjectResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    status: Literal["active", "inactive"]
    visibility: Literal["public", "private"]
    tags: list[str]
    owner_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectMemberRequest(BaseModel):
    user_id: UUID | None = Field(default=None, description="目标用户 UUID；与 username 二选一。")
    username: str | None = Field(
        default=None,
        min_length=3,
        max_length=100,
        description="目标用户的唯一用户名；与 user_id 二选一。",
    )
    role: Literal["editor", "viewer"] = Field(description="授予用户的项目成员角色。")

    @model_validator(mode="after")
    def validate_user_identifier(self) -> "ProjectMemberRequest":
        if (self.user_id is None) == (self.username is None):
            raise ValueError("user_id 和 username 必须且只能提供一个")
        return self


class ProjectMemberRoleUpdateRequest(BaseModel):
    role: Literal["editor", "viewer"] = Field(description="修改后的项目成员角色。")


class ProjectMemberResponse(BaseModel):
    project_id: UUID = Field(description="项目 UUID。")
    user_id: UUID = Field(description="成员用户 UUID，可用于移除成员等后续操作。")
    username: str = Field(description="成员的唯一用户名。")
    role: Literal["owner", "editor", "viewer"] = Field(description="成员在该项目中的角色。")
    created_at: datetime = Field(description="成员关系创建时间。")
    updated_at: datetime = Field(description="成员关系最近更新时间。")

    model_config = {"from_attributes": True}


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    total: int
    offset: int
    limit: int


def map_project_error(error: RuntimeError) -> HTTPException:
    if isinstance(error, ProjectNotFoundError) or isinstance(error, UserNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, ProjectForbiddenError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建项目",
)
async def create_project(
    payload: ProjectCreateRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Project:
    """任何已登录用户都可以创建项目，创建者自动成为 owner。"""
    project = await project_service.create(
        payload.name.strip(), payload.description, payload.visibility, payload.tags, current_user
    )
    await record_project_operation(
        recorder, request, current_user, project.id, "project.create", "创建项目"
    )
    return project


@router.get("", response_model=ProjectListResponse, summary="筛选和分页查询可访问项目")
async def list_projects(
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    query: Annotated[str | None, Query(max_length=100)] = None,
    tag: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    project_status: Annotated[Literal["active", "inactive"] | None, Query(alias="status")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ProjectListResponse:
    """管理员查询全部项目；普通用户查询可见项目，并支持关键词、标签、状态和分页。"""
    page = await project_service.list_for_user(
        current_user, query, tag, project_status, offset, limit
    )
    return ProjectListResponse(
        items=page.items, total=page.total, offset=page.offset, limit=page.limit
    )


@router.get("/tags", response_model=list[str], summary="查询可见项目的标签建议")
async def list_project_tags(
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> list[str]:
    """返回当前用户可查看项目中的标签，供项目列表筛选。"""
    return await project_service.list_tags_for_user(current_user)


@router.get("/{project_id}", response_model=ProjectResponse, summary="查看项目")
async def get_project(
    project_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> Project:
    try:
        return await project_service.get_for_user(project_id, current_user)
    except (ProjectNotFoundError, ProjectForbiddenError) as error:
        raise map_project_error(error) from error


@router.patch("/{project_id}", response_model=ProjectResponse, summary="更新项目")
async def update_project(
    project_id: UUID,
    payload: ProjectUpdateRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Project:
    try:
        project = await project_service.update(
            project_id,
            current_user,
            payload.name.strip() if payload.name is not None else None,
            payload.description,
            payload.status,
            payload.visibility,
            update_description="description" in payload.model_fields_set,
            tags=payload.tags,
            update_tags="tags" in payload.model_fields_set,
        )
        await record_project_operation(
            recorder, request, current_user, project_id, "project.update", "更新项目资料或状态"
        )
        return project
    except (ProjectNotFoundError, ProjectForbiddenError, ProjectConflictError) as error:
        raise map_project_error(error) from error


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除项目",
)
async def delete_project(
    project_id: UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Response:
    """永久删除项目及其标签、成员关系；仅项目 owner 可以删除。"""
    try:
        await project_service.delete(project_id, current_user)
        await record_project_operation(
            recorder, request, current_user, project_id, "project.delete", "删除项目"
        )
    except (ProjectNotFoundError, ProjectForbiddenError) as error:
        raise map_project_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{project_id}/members",
    response_model=list[ProjectMemberResponse],
    summary="查询项目成员",
)
async def list_members(
    project_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> list[ProjectMemberInfo]:
    try:
        return await project_service.list_members(project_id, current_user)
    except (ProjectNotFoundError, ProjectForbiddenError) as error:
        raise map_project_error(error) from error


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="添加项目成员",
)
async def add_member(
    project_id: UUID,
    payload: ProjectMemberRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ProjectMemberInfo:
    try:
        member = await project_service.add_member(
            project_id,
            current_user,
            payload.role,
            member_user_id=payload.user_id,
            member_username=payload.username,
        )
        await record_project_operation(
            recorder,
            request,
            current_user,
            project_id,
            "member.add",
            f"添加项目成员 user_id={member.user_id} role={member.role}",
        )
        return member
    except (
        ProjectNotFoundError,
        ProjectForbiddenError,
        ProjectConflictError,
        UserNotFoundError,
    ) as error:
        raise map_project_error(error) from error


@router.delete(
    "/{project_id}/members/{member_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="移除项目成员",
)
async def remove_member(
    project_id: UUID,
    member_user_id: UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Response:
    try:
        member = await project_service.remove_member(project_id, current_user, member_user_id)
        await record_project_operation(
            recorder,
            request,
            current_user,
            project_id,
            "member.remove",
            f"移除项目成员 username={member.username} user_id={member_user_id}；"
            "其个人 API Key 已撤销",
        )
    except (
        ProjectNotFoundError,
        ProjectForbiddenError,
        ProjectConflictError,
        UserNotFoundError,
    ) as error:
        raise map_project_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{project_id}/members/{member_user_id}",
    response_model=ProjectMemberResponse,
    summary="修改项目成员角色",
)
async def update_member_role(
    project_id: UUID,
    member_user_id: UUID,
    payload: ProjectMemberRoleUpdateRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> ProjectMemberInfo:
    try:
        member = await project_service.update_member_role(
            project_id, current_user, member_user_id, payload.role
        )
        await record_project_operation(
            recorder,
            request,
            current_user,
            project_id,
            "member.role.update",
            f"修改项目成员权限 username={member.username} "
            f"user_id={member_user_id} role={member.role}"
            + ("；其个人 API Key 已撤销" if member.role == "viewer" else ""),
        )
        return member
    except (
        ProjectNotFoundError,
        ProjectForbiddenError,
        ProjectConflictError,
        UserNotFoundError,
    ) as error:
        raise map_project_error(error) from error
