"""PostgreSQL 项目资源适配器。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import Project, ProjectMember, ProjectResource
from app.resources.domain import (
    ResourceConflictError,
    ResourceForbiddenError,
    ResourceNotFoundError,
    ResourceRecord,
    ResourceVersionConflictError,
)


class PostgresResourceRepository:
    """在每个数据库事务中验证项目权限并读写隔离的资源行。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_for_project(
        self, project_id: UUID, user_id: UUID, is_admin: bool, category: str | None
    ) -> list[ResourceRecord]:
        async with self._session_factory() as session:
            await self._authorize(session, project_id, user_id, is_admin, write=False)
            query = select(ProjectResource).where(
                ProjectResource.project_id == project_id,
                ProjectResource.deleted_at.is_(None),
            )
            if category is not None:
                query = query.where(ProjectResource.category == category)
            result = await session.scalars(
                query.order_by(
                    ProjectResource.resource_type,
                    ProjectResource.category,
                    ProjectResource.name,
                )
            )
            return [self._record(item) for item in result.all()]

    async def get_for_project(
        self, project_id: UUID, resource_id: UUID, user_id: UUID, is_admin: bool
    ) -> ResourceRecord:
        async with self._session_factory() as session:
            await self._authorize(session, project_id, user_id, is_admin, write=False)
            result = await session.scalar(
                select(ProjectResource).where(
                    ProjectResource.id == resource_id,
                    ProjectResource.project_id == project_id,
                    ProjectResource.deleted_at.is_(None),
                )
            )
            if result is None:
                raise ResourceNotFoundError("资源不存在")
            return self._record(result)

    async def create(
        self,
        project_id: UUID,
        user_id: UUID,
        resource_type: str,
        category: str,
        name: str,
        content: str,
    ) -> ResourceRecord:
        try:
            async with self._session_factory.begin() as session:
                await self._authorize(session, project_id, user_id, False, write=True)
                resource = ProjectResource(
                    id=uuid4(),
                    project_id=project_id,
                    resource_type=resource_type,
                    category=category,
                    name=name,
                    content=content,
                    created_by=user_id,
                    updated_by=user_id,
                )
                session.add(resource)
                await session.flush()
                await session.refresh(resource)
                return self._record(resource)
        except IntegrityError as error:
            raise ResourceConflictError("该目录已存在同名文件") from error

    async def update(
        self,
        project_id: UUID,
        resource_id: UUID,
        user_id: UUID,
        is_admin: bool,
        expected_version: int,
        name: str,
        content: str,
    ) -> ResourceRecord:
        try:
            async with self._session_factory.begin() as session:
                await self._authorize(session, project_id, user_id, is_admin, write=True)
                resource = await self._get_locked(session, project_id, resource_id)
                self._check_version(resource, expected_version)
                resource.name = name
                resource.content = content
                resource.version += 1
                resource.updated_by = user_id
                await session.flush()
                await session.refresh(resource)
                return self._record(resource)
        except IntegrityError as error:
            raise ResourceConflictError("该目录已存在同名文件") from error

    async def soft_delete(
        self,
        project_id: UUID,
        resource_id: UUID,
        user_id: UUID,
        is_admin: bool,
        expected_version: int,
    ) -> None:
        async with self._session_factory.begin() as session:
            await self._authorize(session, project_id, user_id, is_admin, write=True)
            resource = await self._get_locked(session, project_id, resource_id)
            self._check_version(resource, expected_version)
            resource.deleted_at = datetime.now(UTC)
            resource.version += 1
            resource.updated_by = user_id

    @staticmethod
    async def _authorize(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        is_admin: bool,
        write: bool,
    ) -> None:
        project = await session.get(Project, project_id)
        if project is None:
            raise ResourceNotFoundError("项目不存在")
        result = await session.execute(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        role = result.scalar_one_or_none()
        if write:
            if role not in {"owner", "editor"}:
                raise ResourceForbiddenError("需要项目 owner 或 editor 权限")
            return
        if not is_admin and project.visibility != "public" and role is None:
            raise ResourceForbiddenError("无权访问该项目")

    @staticmethod
    async def _get_locked(
        session: AsyncSession, project_id: UUID, resource_id: UUID
    ) -> ProjectResource:
        resource = await session.scalar(
            select(ProjectResource)
            .where(
                ProjectResource.id == resource_id,
                ProjectResource.project_id == project_id,
                ProjectResource.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if resource is None:
            raise ResourceNotFoundError("资源不存在")
        return resource

    @staticmethod
    def _check_version(resource: ProjectResource, expected_version: int) -> None:
        if resource.version != expected_version:
            raise ResourceVersionConflictError("资源已被其他人修改，请重新加载")

    @staticmethod
    def _record(resource: ProjectResource) -> ResourceRecord:
        return ResourceRecord(
            id=resource.id,
            project_id=resource.project_id,
            resource_type=resource.resource_type,
            category=resource.category,
            name=resource.name,
            content=resource.content,
            version=resource.version,
            created_by=resource.created_by,
            updated_by=resource.updated_by,
            created_at=resource.created_at,
            updated_at=resource.updated_at,
        )
