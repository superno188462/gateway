"""项目和项目成员授权用例。"""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import Project, ProjectMember, User


class ProjectNotFoundError(RuntimeError):
    """项目不存在。"""


class ProjectForbiddenError(RuntimeError):
    """用户无权访问项目。"""


class ProjectConflictError(RuntimeError):
    """项目成员关系冲突。"""


class UserNotFoundError(RuntimeError):
    """被授权的用户不存在。"""


class ProjectService:
    """项目 CRUD 和成员权限服务。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, name: str, description: str | None, owner: User) -> Project:
        async with self._session_factory.begin() as session:
            project = Project(
                name=name,
                description=description,
                status="active",
                owner_id=owner.id,
            )
            session.add(project)
            await session.flush()
            session.add(ProjectMember(project_id=project.id, user_id=owner.id, role="owner"))
            await session.flush()
            return project

    async def list_for_user(self, user: User) -> list[Project]:
        async with self._session_factory() as session:
            if user.role == "admin":
                result = await session.execute(
                    select(Project).order_by(Project.created_at.desc(), Project.id.desc())
                )
            else:
                result = await session.execute(
                    select(Project)
                    .join(ProjectMember, ProjectMember.project_id == Project.id)
                    .where(ProjectMember.user_id == user.id)
                    .order_by(Project.created_at.desc(), Project.id.desc())
                )
            return list(result.scalars().all())

    async def get_for_user(self, project_id: UUID, user: User) -> Project:
        async with self._session_factory() as session:
            project = await self._get_project(session, project_id)
            if user.role != "admin" and not await self._has_membership(
                session, project_id, user.id
            ):
                raise ProjectForbiddenError("无权访问该项目")
            return project

    async def update(
        self,
        project_id: UUID,
        user: User,
        name: str | None,
        description: str | None,
        status: str | None,
    ) -> Project:
        async with self._session_factory.begin() as session:
            project = await self._get_project(session, project_id)
            if user.role != "admin" and not await self._has_role(
                session, project_id, user.id, {"owner"}
            ):
                raise ProjectForbiddenError("只有管理员或项目所有者可以修改项目")
            if name is not None:
                project.name = name
            if description is not None:
                project.description = description
            if status is not None:
                project.status = status
            await session.flush()
            return project

    async def list_members(self, project_id: UUID, user: User) -> list[ProjectMember]:
        async with self._session_factory() as session:
            await self._require_access(session, project_id, user)
            result = await session.execute(
                select(ProjectMember)
                .where(ProjectMember.project_id == project_id)
                .order_by(ProjectMember.created_at, ProjectMember.user_id)
            )
            return list(result.scalars().all())

    async def add_member(
        self, project_id: UUID, user: User, member_user_id: UUID, role: str
    ) -> ProjectMember:
        async with self._session_factory.begin() as session:
            await self._require_manager(session, project_id, user)
            target = await session.get(User, member_user_id)
            if target is None:
                raise UserNotFoundError("用户不存在")
            if role == "owner":
                raise ProjectConflictError("不能通过成员接口新增 owner")
            existing = await session.get(ProjectMember, (project_id, member_user_id))
            if existing is not None:
                raise ProjectConflictError("用户已经是项目成员")
            member = ProjectMember(project_id=project_id, user_id=member_user_id, role=role)
            session.add(member)
            await session.flush()
            return member

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

    async def _get_project(self, session: AsyncSession, project_id: UUID) -> Project:
        project = await session.get(Project, project_id)
        if project is None:
            raise ProjectNotFoundError("项目不存在")
        return project

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
        if user.role != "admin" and not await self._has_membership(session, project_id, user.id):
            raise ProjectForbiddenError("无权访问该项目")
        return project

    async def _require_manager(
        self, session: AsyncSession, project_id: UUID, user: User
    ) -> Project:
        project = await self._get_project(session, project_id)
        if user.role != "admin" and not await self._has_role(
            session, project_id, user.id, {"owner"}
        ):
            raise ProjectForbiddenError("只有管理员或项目所有者可以管理项目成员")
        return project
