"""项目和项目成员管理端点。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.application.projects import (
    ProjectConflictError,
    ProjectForbiddenError,
    ProjectNotFoundError,
    ProjectService,
    UserNotFoundError,
)
from app.container import get_current_admin, get_current_user, get_project_service
from app.infrastructure.db.models import Project, ProjectMember, User

router = APIRouter(prefix="/api/admin/v1/projects", tags=["Projects"])


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "inactive"] | None = None


class ProjectResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    status: Literal["active", "inactive"]
    owner_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectMemberRequest(BaseModel):
    user_id: UUID
    role: Literal["editor", "viewer"]


class ProjectMemberResponse(BaseModel):
    project_id: UUID
    user_id: UUID
    role: Literal["owner", "editor", "viewer"]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


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
    current_admin: Annotated[User, Depends(get_current_admin)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> Project:
    """只有全局管理员可以创建项目。"""
    return await project_service.create(payload.name.strip(), payload.description, current_admin)


@router.get("", response_model=list[ProjectResponse], summary="查询可访问项目")
async def list_projects(
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> list[Project]:
    """管理员查询全部项目，普通用户只查询项目成员关系中的项目。"""
    return await project_service.list_for_user(current_user)


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
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> Project:
    try:
        return await project_service.update(
            project_id,
            current_user,
            payload.name.strip() if payload.name is not None else None,
            payload.description,
            payload.status,
        )
    except (ProjectNotFoundError, ProjectForbiddenError, ProjectConflictError) as error:
        raise map_project_error(error) from error


@router.get(
    "/{project_id}/members",
    response_model=list[ProjectMemberResponse],
    summary="查询项目成员",
)
async def list_members(
    project_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> list[ProjectMember]:
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
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> ProjectMember:
    try:
        return await project_service.add_member(
            project_id, current_user, payload.user_id, payload.role
        )
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
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> Response:
    try:
        await project_service.remove_member(project_id, current_user, member_user_id)
    except (
        ProjectNotFoundError,
        ProjectForbiddenError,
        ProjectConflictError,
    ) as error:
        raise map_project_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
