"""项目和项目成员授权用例。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.infrastructure.db.models import Project, ProjectMember, ProjectTag, User


class ProjectNotFoundError(RuntimeError):
    """项目不存在。"""


class ProjectForbiddenError(RuntimeError):
    """用户无权访问项目。"""


class ProjectConflictError(RuntimeError):
    """项目成员关系冲突。"""


class UserNotFoundError(RuntimeError):
    """被授权的用户不存在。"""


@dataclass(frozen=True, slots=True)
class ProjectMemberInfo:
    """包含用户标识的项目成员信息，供 API 展示和调用方后续操作。"""

    project_id: UUID
    user_id: UUID
    username: str
    role: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectListPage:
    """当前访问范围内的一页项目及匹配总数。"""

    items: list[Project]
    total: int
    offset: int
    limit: int


class ProjectService:
    """项目 CRUD 和成员权限服务。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        name: str,
        description: str | None,
        visibility: str,
        tags: list[str],
        owner: User,
    ) -> Project:
        async with self._session_factory.begin() as session:
            project = Project(
                name=name,
                description=description,
                status="active",
                visibility=visibility,
                owner_id=owner.id,
                project_tags=[ProjectTag(tag=tag) for tag in self._normalize_tags(tags)],
            )
            session.add(project)
            await session.flush()
            session.add(ProjectMember(project_id=project.id, user_id=owner.id, role="owner"))
            await session.flush()
            return project

    async def list_for_user(
        self,
        user: User,
        query: str | None,
        tag: str | None,
        status: str | None,
        offset: int,
        limit: int,
    ) -> ProjectListPage:
        async with self._session_factory() as session:
            conditions = self._visible_project_conditions(user)
            if query and query.strip():
                pattern = f"%{query.strip()}%"
                conditions.append(
                    or_(Project.name.ilike(pattern), Project.description.ilike(pattern))
                )
            if tag and tag.strip():
                normalized_tag = tag.strip().lower()
                conditions.append(
                    Project.id.in_(
                        select(ProjectTag.project_id).where(ProjectTag.tag == normalized_tag)
                    )
                )
            if status is not None:
                conditions.append(Project.status == status)

            count_result = await session.execute(
                select(func.count()).select_from(Project).where(*conditions)
            )
            total = count_result.scalar_one()
            result = await session.execute(
                select(Project)
                .options(selectinload(Project.project_tags))
                .where(*conditions)
                .order_by(Project.created_at.desc(), Project.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return ProjectListPage(
                items=list(result.scalars().all()),
                total=total or 0,
                offset=offset,
                limit=limit,
            )

    async def list_tags_for_user(self, user: User) -> list[str]:
        """返回当前用户可查看项目中的标签，用作筛选建议。"""
        async with self._session_factory() as session:
            statement = select(ProjectTag.tag).join(Project, Project.id == ProjectTag.project_id)
            conditions = self._visible_project_conditions(user)
            if conditions:
                statement = statement.where(*conditions)
            result = await session.scalars(statement.distinct().order_by(ProjectTag.tag))
            return list(result.all())

    async def get_for_user(self, project_id: UUID, user: User) -> Project:
        async with self._session_factory() as session:
            return await self._require_access(session, project_id, user)

    async def update(
        self,
        project_id: UUID,
        user: User,
        name: str | None,
        description: str | None,
        status: str | None,
        visibility: str | None = None,
        update_description: bool = False,
        tags: list[str] | None = None,
        update_tags: bool = False,
    ) -> Project:
        async with self._session_factory.begin() as session:
            project = await self._get_project(session, project_id)
            await self._require_manager(session, project_id, user)
            if name is not None:
                project.name = name
            if update_description:
                project.description = description
            if status is not None:
                project.status = status
            if visibility is not None:
                project.visibility = visibility
            if update_tags:
                project.project_tags = [
                    ProjectTag(tag=tag) for tag in self._normalize_tags(tags or [])
                ]
            await session.flush()
            await session.refresh(project)
            return project

    async def delete(self, project_id: UUID, user: User) -> None:
        """永久删除项目；只有项目 owner 可以执行。"""
        async with self._session_factory.begin() as session:
            project = await self._require_manager(session, project_id, user)
            await session.delete(project)

    async def list_members(self, project_id: UUID, user: User) -> list[ProjectMemberInfo]:
        async with self._session_factory() as session:
            await self._require_access(session, project_id, user)
            result = await session.execute(
                select(ProjectMember, User.username)
                .join(User, User.id == ProjectMember.user_id)
                .where(ProjectMember.project_id == project_id)
                .order_by(ProjectMember.created_at, ProjectMember.user_id)
            )
            return [
                ProjectMemberInfo(
                    project_id=member.project_id,
                    user_id=member.user_id,
                    username=username,
                    role=member.role,
                    created_at=member.created_at,
                    updated_at=member.updated_at,
                )
                for member, username in result.all()
            ]

    async def add_member(
        self,
        project_id: UUID,
        user: User,
        role: str,
        member_user_id: UUID | None = None,
        member_username: str | None = None,
    ) -> ProjectMemberInfo:
        async with self._session_factory.begin() as session:
            await self._require_manager(session, project_id, user)
            if member_user_id is not None:
                target = await session.get(User, member_user_id)
            elif member_username is not None:
                result = await session.execute(
                    select(User).where(User.username == member_username.strip())
                )
                target = result.scalar_one_or_none()
            else:
                raise ProjectConflictError("必须提供用户 ID 或用户名")
            if target is None:
                raise UserNotFoundError("用户不存在")
            if role == "owner":
                raise ProjectConflictError("不能通过成员接口新增 owner")
            existing = await session.get(ProjectMember, (project_id, target.id))
            if existing is not None:
                raise ProjectConflictError("用户已经是项目成员")
            member = ProjectMember(project_id=project_id, user_id=target.id, role=role)
            session.add(member)
            await session.flush()
            await session.refresh(member)
            return ProjectMemberInfo(
                project_id=member.project_id,
                user_id=member.user_id,
                username=target.username,
                role=member.role,
                created_at=member.created_at,
                updated_at=member.updated_at,
            )

    async def remove_member(self, project_id: UUID, user: User, member_user_id: UUID) -> None:
        async with self._session_factory.begin() as session:
            await self._require_manager(session, project_id, user)
            member = await session.get(ProjectMember, (project_id, member_user_id))
            if member is None:
                raise ProjectNotFoundError("项目成员不存在")
            if member.role == "owner":
                raise ProjectConflictError("不能移除项目所有者")
            await session.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == member_user_id,
                )
            )

    async def update_member_role(
        self,
        project_id: UUID,
        user: User,
        member_user_id: UUID,
        role: str,
    ) -> ProjectMemberInfo:
        """修改项目成员角色；只有管理员或 owner 可以修改非 owner 成员。"""
        async with self._session_factory.begin() as session:
            await self._require_manager(session, project_id, user)
            member = await session.get(ProjectMember, (project_id, member_user_id))
            if member is None:
                raise ProjectNotFoundError("项目成员不存在")
            if member.role == "owner":
                raise ProjectConflictError("不能修改项目所有者角色")
            target = await session.get(User, member_user_id)
            if target is None:
                raise UserNotFoundError("用户不存在")
            member.role = role
            await session.flush()
            await session.refresh(member)
            return ProjectMemberInfo(
                project_id=member.project_id,
                user_id=member.user_id,
                username=target.username,
                role=member.role,
                created_at=member.created_at,
                updated_at=member.updated_at,
            )

    async def _get_project(self, session: AsyncSession, project_id: UUID) -> Project:
        result = await session.execute(
            select(Project)
            .options(selectinload(Project.project_tags))
            .where(Project.id == project_id)
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise ProjectNotFoundError("项目不存在")
        return project

    @staticmethod
    def _normalize_tags(tags: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in tags:
            tag = value.strip().lower()
            if not tag:
                continue
            if len(tag) > 20:
                raise ProjectConflictError("每个项目标签不能超过 20 个字符")
            if tag not in normalized:
                normalized.append(tag)
        if len(normalized) > 5:
            raise ProjectConflictError("每个项目最多设置 5 个标签")
        return normalized

    @staticmethod
    def _visible_project_conditions(user: User) -> list[ColumnElement[bool]]:
        if user.role == "admin":
            return []
        return [
            or_(
                Project.visibility == "public",
                Project.id.in_(
                    select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
                ),
            )
        ]

    async def _has_membership(self, session: AsyncSession, project_id: UUID, user_id: UUID) -> bool:
        return await self._has_role(session, project_id, user_id, {"owner", "editor", "viewer"})

    async def _has_role(
        self, session: AsyncSession, project_id: UUID, user_id: UUID, roles: set[str]
    ) -> bool:
        result = await session.execute(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.role.in_(roles),
            )
        )
        return result.scalar_one_or_none() is not None

    async def _require_access(self, session: AsyncSession, project_id: UUID, user: User) -> Project:
        project = await self._get_project(session, project_id)
        if (
            user.role != "admin"
            and project.visibility != "public"
            and not await self._has_membership(session, project_id, user.id)
        ):
            raise ProjectForbiddenError("无权访问该项目")
        return project

    async def _require_manager(
        self, session: AsyncSession, project_id: UUID, user: User
    ) -> Project:
        project = await self._get_project(session, project_id)
        if not await self._has_role(session, project_id, user.id, {"owner"}):
            raise ProjectForbiddenError("只有项目所有者可以管理项目和成员")
        return project
