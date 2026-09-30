"""项目上下文服务：模板读取、短期/长期记忆和用户画像。"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import (
    Project,
    ProjectLongTermMemory,
    ProjectMember,
    ProjectResource,
    ProjectServiceSubscription,
    ProjectSessionMemory,
    ProjectSessionMessage,
    ProjectUserProfile,
    User,
)
from app.services.project_context.catalog import PROJECT_CONTEXT_SERVICE

DEFAULT_SESSION_TTL_SECONDS = 86_400
MAX_SESSION_TTL_SECONDS = 2_592_000
MAX_MEMORY_VALUE_BYTES = 64_000
logger = logging.getLogger("gateway.context.cleanup")


class ContextNotFoundError(RuntimeError):
    """项目上下文服务或指定记录不存在。"""


class ContextForbiddenError(RuntimeError):
    """项目未开通上下文服务或当前调用无权访问。"""


class ContextConflictError(RuntimeError):
    """项目订阅状态或乐观锁版本冲突。"""


class ProjectContextService:
    """从注入的 PostgreSQL 会话工厂访问项目隔离上下文数据。"""

    service_code = PROJECT_CONTEXT_SERVICE.code

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ensure_enabled(self, project_id: UUID) -> None:
        """运行时入口检查项目上下文服务订阅必须处于 active。"""
        async with self._session_factory() as session:
            project_status = await session.scalar(
                select(Project.status).where(Project.id == project_id)
            )
            if project_status is None:
                raise ContextNotFoundError("项目不存在")
            if project_status != "active":
                raise ContextForbiddenError("项目已停用")
            subscription_status = await session.scalar(
                select(ProjectServiceSubscription.status).where(
                    ProjectServiceSubscription.project_id == project_id,
                    ProjectServiceSubscription.service_code == self.service_code,
                )
            )
            if subscription_status != "active":
                raise ContextForbiddenError("项目尚未开通或已暂停上下文服务")

    async def list_projects_for_user(
        self, user: User, offset: int, limit: int
    ) -> tuple[list[Project], int]:
        """分页返回当前用户可查看且已开通上下文服务的运行中项目。"""
        conditions = [
            Project.status == "active",
            ProjectServiceSubscription.service_code == self.service_code,
            ProjectServiceSubscription.status == "active",
        ]
        if user.role != "admin":
            conditions.append(
                or_(
                    Project.visibility == "public",
                    Project.id.in_(
                        select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
                    ),
                )
            )
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count(Project.id))
                .join(
                    ProjectServiceSubscription,
                    ProjectServiceSubscription.project_id == Project.id,
                )
                .where(*conditions)
            )
            rows = await session.scalars(
                select(Project)
                .join(
                    ProjectServiceSubscription,
                    ProjectServiceSubscription.project_id == Project.id,
                )
                .where(*conditions)
                .order_by(Project.created_at.desc(), Project.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return list(rows.all()), int(total or 0)

    async def cleanup_expired_session_memories(self) -> int:
        """物理清除已过期短期记忆，避免 TTL 记录无限累积。"""
        async with self._session_factory.begin() as session:
            expired_ids = await session.scalars(
                delete(ProjectSessionMemory)
                .where(ProjectSessionMemory.expires_at <= datetime.now(UTC))
                .returning(ProjectSessionMemory.id)
            )
            return len(expired_ids.all())

    async def cleanup_expired_session_messages(self) -> int:
        """物理清除已过期的标准会话消息。"""
        async with self._session_factory.begin() as session:
            expired_ids = await session.scalars(
                delete(ProjectSessionMessage)
                .where(ProjectSessionMessage.expires_at <= datetime.now(UTC))
                .returning(ProjectSessionMessage.id)
            )
            return len(expired_ids.all())

    async def run_cleanup_periodically(self) -> None:
        """每小时清除过期会话数据；单轮清理失败时记录错误并继续运行。"""
        while True:
            try:
                deleted_count = await self.cleanup_expired_session_memories()
                deleted_count += await self.cleanup_expired_session_messages()
                if deleted_count:
                    logger.info("expired_context_memories_deleted count=%d", deleted_count)
            except Exception:
                logger.exception("expired_context_memory_cleanup_failed")
            await asyncio.sleep(3600)

    async def list_session_messages(
        self,
        project_id: UUID,
        external_user_id: str,
        session_id: str,
        limit: int,
    ) -> list[ProjectSessionMessage]:
        """读取会话最近的有效消息，并按会话顺序返回。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory() as session:
            newest_first = await session.scalars(
                select(ProjectSessionMessage)
                .where(
                    ProjectSessionMessage.project_id == project_id,
                    ProjectSessionMessage.external_user_id == external_user_id,
                    ProjectSessionMessage.session_id == session_id,
                    ProjectSessionMessage.expires_at > datetime.now(UTC),
                )
                .order_by(ProjectSessionMessage.sequence.desc())
                .limit(limit)
            )
            return list(reversed(newest_first.all()))

    async def append_session_messages(
        self,
        project_id: UUID,
        external_user_id: str,
        session_id: str,
        messages: list[dict[str, object]],
        ttl_seconds: int,
    ) -> list[ProjectSessionMessage]:
        """向会话追加一批有序消息；每条消息共享本次写入的 TTL。"""
        await self.ensure_enabled(project_id)
        if not 60 <= ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise ValueError("ttl_seconds 必须在 60 秒到 30 天之间")
        if not messages or len(messages) > 100:
            raise ValueError("messages 必须包含 1 到 100 条消息")
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            metadata = message.get("metadata", {})
            if role not in {"system", "user", "assistant", "tool"}:
                raise ValueError("role 必须是 system、user、assistant 或 tool")
            if not isinstance(content, str) or not content.strip() or len(content) > 20_000:
                raise ValueError("content 必须是 1 到 20,000 个字符的文本")
            if not isinstance(metadata, dict):
                raise ValueError("metadata 必须是 JSON 对象")
            self._validate_json_value(metadata)
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        async with self._session_factory.begin() as session:
            rows = [
                ProjectSessionMessage(
                    project_id=project_id,
                    external_user_id=external_user_id,
                    session_id=session_id,
                    role=str(message["role"]),
                    content=str(message["content"]),
                    metadata_json=message.get("metadata", {}),
                    expires_at=expires_at,
                )
                for message in messages
            ]
            session.add_all(rows)
            await session.flush()
            for row in rows:
                await session.refresh(row)
            return rows

    async def delete_session_messages(
        self, project_id: UUID, external_user_id: str, session_id: str
    ) -> int:
        """清除指定项目用户会话的全部短期消息，供会话结束或用户删除数据。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory.begin() as session:
            deleted_ids = await session.scalars(
                delete(ProjectSessionMessage)
                .where(
                    ProjectSessionMessage.project_id == project_id,
                    ProjectSessionMessage.external_user_id == external_user_id,
                    ProjectSessionMessage.session_id == session_id,
                )
                .returning(ProjectSessionMessage.id)
            )
            return len(deleted_ids.all())

    async def list_templates(self, project_id: UUID) -> list[ProjectResource]:
        """列出项目当前可用的提示词模板，不返回其他项目数据。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory() as session:
            result = await session.scalars(
                select(ProjectResource)
                .where(
                    ProjectResource.project_id == project_id,
                    ProjectResource.resource_type == "template",
                    ProjectResource.deleted_at.is_(None),
                )
                .order_by(ProjectResource.category, ProjectResource.name)
            )
            return list(result.all())

    async def get_template(self, project_id: UUID, category: str, name: str) -> ProjectResource:
        """按项目、模板分类和名称精确读取单个模板。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory() as session:
            item = await session.scalar(
                select(ProjectResource).where(
                    ProjectResource.project_id == project_id,
                    ProjectResource.resource_type == "template",
                    ProjectResource.category == category,
                    ProjectResource.name == name,
                    ProjectResource.deleted_at.is_(None),
                )
            )
            if item is None:
                raise ContextNotFoundError("提示词模板不存在")
            return item

    async def get_template_by_id(self, project_id: UUID, template_id: UUID) -> ProjectResource:
        """按项目和稳定模板 ID 精确读取模板。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory() as session:
            item = await session.scalar(
                select(ProjectResource).where(
                    ProjectResource.id == template_id,
                    ProjectResource.project_id == project_id,
                    ProjectResource.resource_type == "template",
                    ProjectResource.deleted_at.is_(None),
                )
            )
            if item is None:
                raise ContextNotFoundError("提示词模板不存在")
            return item

    async def get_session_memory(
        self, project_id: UUID, external_user_id: str, session_id: str, memory_key: str
    ) -> ProjectSessionMemory:
        """读取未过期的项目用户会话记忆。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory() as session:
            item = await session.scalar(
                select(ProjectSessionMemory).where(
                    ProjectSessionMemory.project_id == project_id,
                    ProjectSessionMemory.external_user_id == external_user_id,
                    ProjectSessionMemory.session_id == session_id,
                    ProjectSessionMemory.memory_key == memory_key,
                    ProjectSessionMemory.expires_at > datetime.now(UTC),
                )
            )
            if item is None:
                raise ContextNotFoundError("短期记忆不存在或已过期")
            return item

    async def list_session_memories(
        self, project_id: UUID, external_user_id: str, session_id: str
    ) -> list[ProjectSessionMemory]:
        """列出某项目用户会话中所有未过期的键值记忆。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory() as session:
            result = await session.scalars(
                select(ProjectSessionMemory)
                .where(
                    ProjectSessionMemory.project_id == project_id,
                    ProjectSessionMemory.external_user_id == external_user_id,
                    ProjectSessionMemory.session_id == session_id,
                    ProjectSessionMemory.expires_at > datetime.now(UTC),
                )
                .order_by(ProjectSessionMemory.memory_key)
            )
            return list(result.all())

    async def put_session_memory(
        self,
        project_id: UUID,
        external_user_id: str,
        session_id: str,
        memory_key: str,
        value: object,
        ttl_seconds: int,
    ) -> ProjectSessionMemory:
        """创建或原子更新短期键值记忆，并刷新过期时间。"""
        await self.ensure_enabled(project_id)
        self._validate_json_value(value)
        if not 60 <= ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise ValueError("ttl_seconds 必须在 60 秒到 30 天之间")
        now = datetime.now(UTC)
        statement = insert(ProjectSessionMemory).values(
            id=uuid4(),
            project_id=project_id,
            external_user_id=external_user_id,
            session_id=session_id,
            memory_key=memory_key,
            value_json=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            version=1,
            expires_at=now + timedelta(seconds=ttl_seconds),
            created_at=now,
            updated_at=now,
        )
        returning_statement = statement.on_conflict_do_update(
            constraint="uq_session_memory_key",
            set_={
                "value_json": statement.excluded.value_json,
                "version": ProjectSessionMemory.version + 1,
                "expires_at": statement.excluded.expires_at,
                "updated_at": now,
            },
        ).returning(ProjectSessionMemory)
        async with self._session_factory.begin() as session:
            result = await session.execute(returning_statement)
            return result.scalar_one()

    async def delete_session_memory(
        self, project_id: UUID, external_user_id: str, session_id: str, memory_key: str
    ) -> None:
        """删除指定项目用户会话中的一个短期记忆键。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory.begin() as session:
            result = await session.execute(
                delete(ProjectSessionMemory)
                .where(
                    ProjectSessionMemory.project_id == project_id,
                    ProjectSessionMemory.external_user_id == external_user_id,
                    ProjectSessionMemory.session_id == session_id,
                    ProjectSessionMemory.memory_key == memory_key,
                )
                .returning(ProjectSessionMemory.id)
            )
            if result.scalar_one_or_none() is None:
                raise ContextNotFoundError("短期记忆不存在")

    async def list_long_term_memories(
        self, project_id: UUID, external_user_id: str, offset: int, limit: int
    ) -> tuple[list[ProjectLongTermMemory], int]:
        """按外部用户和项目隔离分页读取长期记忆。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory() as session:
            conditions = (
                ProjectLongTermMemory.project_id == project_id,
                ProjectLongTermMemory.external_user_id == external_user_id,
                ProjectLongTermMemory.deleted_at.is_(None),
            )
            items = await session.scalars(
                select(ProjectLongTermMemory)
                .where(*conditions)
                .order_by(ProjectLongTermMemory.created_at.desc(), ProjectLongTermMemory.id.desc())
                .offset(offset)
                .limit(limit)
            )
            total = await session.scalar(
                select(func.count()).select_from(ProjectLongTermMemory).where(*conditions)
            )
            return list(items.all()), int(total or 0)

    async def create_long_term_memory(
        self,
        project_id: UUID,
        external_user_id: str,
        content: str,
        tags: list[str],
        metadata: dict[str, object],
    ) -> ProjectLongTermMemory:
        """保存一条用户长期记忆；只保留调用方显式提交的内容。"""
        await self.ensure_enabled(project_id)
        self._validate_long_term_memory(content, tags, metadata)
        async with self._session_factory.begin() as session:
            item = ProjectLongTermMemory(
                project_id=project_id,
                external_user_id=external_user_id,
                content=content,
                tags=tags,
                metadata_json=metadata,
            )
            session.add(item)
            await session.flush()
            await session.refresh(item)
            return item

    async def update_long_term_memory(
        self,
        project_id: UUID,
        external_user_id: str,
        memory_id: UUID,
        expected_version: int,
        content: str,
        tags: list[str],
        metadata: dict[str, object],
    ) -> ProjectLongTermMemory:
        """以版本号检查并更新长期记忆。"""
        await self.ensure_enabled(project_id)
        self._validate_long_term_memory(content, tags, metadata)
        async with self._session_factory.begin() as session:
            item = await session.scalar(
                select(ProjectLongTermMemory)
                .where(
                    ProjectLongTermMemory.id == memory_id,
                    ProjectLongTermMemory.project_id == project_id,
                    ProjectLongTermMemory.external_user_id == external_user_id,
                    ProjectLongTermMemory.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if item is None:
                raise ContextNotFoundError("长期记忆不存在")
            if item.version != expected_version:
                raise ContextConflictError("长期记忆已被其他请求修改，请重新读取")
            item.content = content
            item.tags = tags
            item.metadata_json = metadata
            item.version += 1
            await session.flush()
            await session.refresh(item)
            return item

    async def delete_long_term_memory(
        self,
        project_id: UUID,
        external_user_id: str,
        memory_id: UUID,
        expected_version: int,
    ) -> None:
        """按版本软删除长期记忆。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory.begin() as session:
            item = await session.scalar(
                select(ProjectLongTermMemory)
                .where(
                    ProjectLongTermMemory.id == memory_id,
                    ProjectLongTermMemory.project_id == project_id,
                    ProjectLongTermMemory.external_user_id == external_user_id,
                    ProjectLongTermMemory.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if item is None:
                raise ContextNotFoundError("长期记忆不存在")
            if item.version != expected_version:
                raise ContextConflictError("长期记忆已被其他请求修改，请重新读取")
            item.deleted_at = datetime.now(UTC)
            item.version += 1

    async def get_profile(
        self, project_id: UUID, external_user_id: str
    ) -> ProjectUserProfile | None:
        """读取项目内指定外部用户的画像，不存在时返回空值。"""
        await self.ensure_enabled(project_id)
        async with self._session_factory() as session:
            return await session.get(ProjectUserProfile, (project_id, external_user_id))

    async def put_profile(
        self,
        project_id: UUID,
        external_user_id: str,
        profile: dict[str, object],
        expected_version: int | None,
    ) -> ProjectUserProfile:
        """创建画像或按版本整体替换画像，防止字段合并歧义和静默覆盖。"""
        await self.ensure_enabled(project_id)
        self._validate_json_value(profile)
        async with self._session_factory.begin() as session:
            item = await session.scalar(
                select(ProjectUserProfile)
                .where(
                    ProjectUserProfile.project_id == project_id,
                    ProjectUserProfile.external_user_id == external_user_id,
                )
                .with_for_update()
            )
            if item is None:
                if expected_version is not None:
                    raise ContextConflictError("画像不存在，expected_version 应省略")
                item = ProjectUserProfile(
                    project_id=project_id,
                    external_user_id=external_user_id,
                    profile=profile,
                )
                session.add(item)
            else:
                if expected_version != item.version:
                    raise ContextConflictError("用户画像已被其他请求修改，请重新读取")
                item.profile = profile
                item.version += 1
            await session.flush()
            await session.refresh(item)
            return item

    @staticmethod
    def _validate_json_value(value: object) -> None:
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("value 必须是 JSON 可序列化数据") from error
        if len(encoded.encode("utf-8")) > MAX_MEMORY_VALUE_BYTES:
            raise ValueError("上下文 JSON 数据不能超过 64 KB")

    @staticmethod
    def _validate_long_term_memory(
        content: str, tags: list[str], metadata: dict[str, object]
    ) -> None:
        if not content.strip() or len(content) > 20_000:
            raise ValueError("长期记忆正文长度必须在 1 到 20,000 个字符之间")
        if len(tags) > 20 or any(not tag.strip() or len(tag) > 50 for tag in tags):
            raise ValueError("长期记忆最多有 20 个标签，每个标签最多 50 个字符")
        ProjectContextService._validate_json_value(metadata)
