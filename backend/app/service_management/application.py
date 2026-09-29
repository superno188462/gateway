"""通用服务目录、项目订阅与额度管理用例。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.service_catalog import ServiceCatalogItem
from app.infrastructure.db.models import (
    Project,
    ProjectMember,
    ProjectServiceSubscription,
    ServiceUsageBucket,
    User,
    UserServiceQuota,
)


class ServiceAccessNotFoundError(RuntimeError):
    """项目或服务订阅不存在。"""


class ServiceAccessForbiddenError(RuntimeError):
    """当前用户无权管理项目服务。"""


class ServiceAccessConflictError(RuntimeError):
    """重复申请或服务配置冲突。"""


class UserServiceQuotaExceededError(RuntimeError):
    """请求的项目分配额度超出用户上限或低于当前用量。"""


@dataclass(frozen=True, slots=True)
class UserServiceQuotaInfo:
    """当前用户某项服务的月度上限、分配额和实际用量。"""

    service_code: str
    name: str
    models: tuple[str, ...]
    monthly_token_limit: int
    allocated_tokens: int
    available_tokens: int
    tokens_used: int
    tokens_reserved: int


@dataclass(frozen=True, slots=True)
class UserQuotaTarget:
    """管理员额度操作中可识别目标用户的非敏感信息。"""

    id: UUID
    username: str
    role: str


@dataclass(frozen=True, slots=True)
class ProjectServiceInfo:
    """项目服务订阅及当前月份用量。"""

    project_id: UUID
    service_code: str
    monthly_token_limit: int
    status: str
    period_start: str
    tokens_used: int
    tokens_reserved: int


class ProjectServiceManagement:
    """管理服务目录、项目申请、额度分配和用量查询。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        catalog: tuple[ServiceCatalogItem, ...],
        catalog_provider: Callable[[], Awaitable[tuple[ServiceCatalogItem, ...]]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._catalog = catalog
        self._catalog_provider = catalog_provider

    async def _current_catalog(self) -> tuple[ServiceCatalogItem, ...]:
        if self._catalog_provider is not None:
            return await self._catalog_provider()
        return self._catalog

    async def catalog(self) -> tuple[ServiceCatalogItem, ...]:
        """列出目前可申请的模型服务目录。"""
        return await self._current_catalog()

    async def list_for_project(self, project_id: UUID, user: User) -> list[ProjectServiceInfo]:
        """列出项目已开通服务；项目成员和管理员可 review。"""
        async with self._session_factory() as session:
            await self._require_review(session, project_id, user)
            now = datetime.now(UTC)
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            rows = await session.scalars(
                select(ProjectServiceSubscription)
                .where(ProjectServiceSubscription.project_id == project_id)
                .order_by(ProjectServiceSubscription.service_code)
            )
            items: list[ProjectServiceInfo] = []
            for row in rows:
                bucket = await session.get(
                    ServiceUsageBucket, (project_id, row.service_code, period_start)
                )
                items.append(
                    ProjectServiceInfo(
                        project_id=row.project_id,
                        service_code=row.service_code,
                        monthly_token_limit=row.monthly_token_limit,
                        status=row.status,
                        period_start=period_start.date().isoformat(),
                        tokens_used=bucket.tokens_used if bucket is not None else 0,
                        tokens_reserved=bucket.tokens_reserved if bucket is not None else 0,
                    )
                )
            return items

    async def apply(
        self, project_id: UUID, user: User, service_code: str, monthly_token_limit: int
    ) -> None:
        """为项目开通服务并分配指定月额度；额度来自项目 owner 的个人上限。"""
        await self._change_allocation(
            project_id, user, service_code, monthly_token_limit, applying=True
        )

    async def set_allocation(
        self, project_id: UUID, user: User, service_code: str, monthly_token_limit: int
    ) -> None:
        """调整项目分配额；允许升降，但不得超过用户上限或低于当月项目已用量。"""
        await self._change_allocation(
            project_id, user, service_code, monthly_token_limit, applying=False
        )

    async def _change_allocation(
        self,
        project_id: UUID,
        user: User,
        service_code: str,
        monthly_token_limit: int,
        applying: bool,
    ) -> None:
        item = next(
            (item for item in await self._current_catalog() if item.code == service_code), None
        )
        if item is None:
            raise ServiceAccessNotFoundError("服务不存在")
        if monthly_token_limit <= 0:
            raise ServiceAccessConflictError("项目月度分配额度必须大于 0")
        async with self._session_factory.begin() as session:
            project_owner_id = await session.scalar(
                select(Project.owner_id).where(Project.id == project_id)
            )
            if project_owner_id is None:
                raise ServiceAccessNotFoundError("项目不存在")
            if project_owner_id != user.id:
                raise ServiceAccessForbiddenError("只有项目 owner 可以分配服务额度")
            await session.scalar(select(User.id).where(User.id == user.id).with_for_update())
            quota = await session.get(UserServiceQuota, (user.id, service_code))
            if quota is None:
                raise ServiceAccessForbiddenError("当前用户尚未获得该服务额度")
            await session.scalar(
                select(Project.id).where(Project.id == project_id).with_for_update()
            )
            current = await session.scalar(
                select(ProjectServiceSubscription)
                .where(
                    ProjectServiceSubscription.project_id == project_id,
                    ProjectServiceSubscription.service_code == service_code,
                )
                .with_for_update()
            )
            if applying:
                if current is not None:
                    raise ServiceAccessConflictError("项目已申请该服务")
                session.add(
                    ProjectServiceSubscription(
                        project_id=project_id,
                        service_code=service_code,
                        monthly_token_limit=monthly_token_limit,
                        status="active",
                    )
                )
            else:
                if current is None:
                    raise ServiceAccessNotFoundError("项目尚未申请该服务")
                period_start = self._period_start()
                bucket = await session.get(
                    ServiceUsageBucket, (project_id, service_code, period_start)
                )
                if bucket is not None and monthly_token_limit < (
                    bucket.tokens_used + bucket.tokens_reserved
                ):
                    raise UserServiceQuotaExceededError("项目额度不能低于本月已使用和预留的 token")
                current.monthly_token_limit = monthly_token_limit

            allocated = (
                await session.scalar(
                    select(
                        func.coalesce(func.sum(ProjectServiceSubscription.monthly_token_limit), 0)
                    )
                    .join(Project, Project.id == ProjectServiceSubscription.project_id)
                    .where(
                        Project.owner_id == user.id,
                        ProjectServiceSubscription.service_code == service_code,
                    )
                )
                or 0
            )
            if allocated > quota.monthly_token_limit:
                raise UserServiceQuotaExceededError(
                    "所有项目的额度分配合计不能超过当前用户服务上限"
                )

    async def list_for_user(self, user: User) -> list[UserServiceQuotaInfo]:
        """返回个人可申请服务、总上限、跨项目分配、剩余额度和当月用量。"""
        return await self._list_for_user_id(user.id)

    async def find_quota_target(
        self, *, user_id: UUID | None = None, username: str | None = None
    ) -> UserQuotaTarget:
        """按 UUID 或精确用户名查找管理员要调整额度的用户。"""
        if (user_id is None) == (username is None):
            raise ServiceAccessConflictError("必须且只能提供用户 ID 或用户名")
        async with self._session_factory() as session:
            query = select(User)
            if user_id is not None:
                query = query.where(User.id == user_id)
            else:
                query = query.where(User.username == (username or "").strip())
            target = await session.scalar(query)
            if target is None:
                raise ServiceAccessNotFoundError("用户不存在")
            return UserQuotaTarget(id=target.id, username=target.username, role=target.role)

    async def list_for_quota_target(self, user_id: UUID) -> list[UserServiceQuotaInfo]:
        """管理员查看指定用户的服务额度；目标用户不存在时返回 not found。"""
        async with self._session_factory() as session:
            target = await session.get(User, user_id)
            if target is None:
                raise ServiceAccessNotFoundError("用户不存在")
        return await self._list_for_user_id(user_id)

    async def _list_for_user_id(self, user_id: UUID) -> list[UserServiceQuotaInfo]:
        async with self._session_factory() as session:
            now = datetime.now(UTC)
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            items: list[UserServiceQuotaInfo] = []
            for item in await self._current_catalog():
                quota = await session.get(UserServiceQuota, (user_id, item.code))
                allocated = (
                    await session.scalar(
                        select(
                            func.coalesce(
                                func.sum(ProjectServiceSubscription.monthly_token_limit), 0
                            )
                        )
                        .join(Project, Project.id == ProjectServiceSubscription.project_id)
                        .where(
                            Project.owner_id == user_id,
                            ProjectServiceSubscription.service_code == item.code,
                        )
                    )
                    or 0
                )
                usage = await session.execute(
                    select(
                        func.coalesce(func.sum(ServiceUsageBucket.tokens_used), 0),
                        func.coalesce(func.sum(ServiceUsageBucket.tokens_reserved), 0),
                    )
                    .join(Project, Project.id == ServiceUsageBucket.project_id)
                    .where(
                        Project.owner_id == user_id,
                        ServiceUsageBucket.service_code == item.code,
                        ServiceUsageBucket.period_start == period_start,
                    )
                )
                tokens_used, tokens_reserved = usage.one()
                monthly_limit = quota.monthly_token_limit if quota is not None else 0
                items.append(
                    UserServiceQuotaInfo(
                        service_code=item.code,
                        name=item.name,
                        models=item.models,
                        monthly_token_limit=monthly_limit,
                        allocated_tokens=allocated,
                        available_tokens=max(0, monthly_limit - allocated),
                        tokens_used=tokens_used,
                        tokens_reserved=tokens_reserved,
                    )
                )
            return items

    async def set_user_limit(
        self, user_id: UUID, service_code: str, monthly_token_limit: int
    ) -> UserServiceQuotaInfo:
        """管理员设定用户级服务月上限；低于项目分配时保留分配并由总额度限制调用。"""
        if not any(item.code == service_code for item in await self._current_catalog()):
            raise ServiceAccessNotFoundError("服务不存在")
        if monthly_token_limit < 0:
            raise ServiceAccessConflictError("月度额度不能小于 0")
        async with self._session_factory.begin() as session:
            target = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            if target is None:
                raise ServiceAccessNotFoundError("用户不存在")
            quota = await session.get(UserServiceQuota, (user_id, service_code))
            if monthly_token_limit == 0:
                if quota is not None:
                    await session.delete(quota)
            elif quota is None:
                session.add(
                    UserServiceQuota(
                        user_id=user_id,
                        service_code=service_code,
                        monthly_token_limit=monthly_token_limit,
                    )
                )
            else:
                quota.monthly_token_limit = monthly_token_limit
        return next(
            item
            for item in await self._list_for_user_id(user_id)
            if item.service_code == service_code
        )

    @staticmethod
    async def _require_review(session: AsyncSession, project_id: UUID, user: User) -> None:
        project_exists = await session.scalar(select(Project.id).where(Project.id == project_id))
        if project_exists is None:
            raise ServiceAccessNotFoundError("项目不存在")
        role = await session.scalar(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user.id
            )
        )
        if user.role != "admin" and role is None:
            project = await session.get(Project, project_id)
            if project is None or project.visibility != "public":
                raise ServiceAccessForbiddenError("无权查看该项目服务")

    @staticmethod
    def _period_start() -> datetime:
        now = datetime.now(UTC)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
