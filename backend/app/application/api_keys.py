"""项目级 API Key 的签发、查询、撤销和校验用例。"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import ApiKey, Project, ProjectMember, User


class ApiKeyNotFoundError(RuntimeError):
    """项目或 API Key 不存在。"""


class ApiKeyForbiddenError(RuntimeError):
    """当前用户不是项目成员，或试图管理其他成员的 Key。"""


class ApiKeyInvalidError(RuntimeError):
    """API Key 无效、已撤销或已过期。"""


class ApiKeyConfigurationError(RuntimeError):
    """服务端配置的加密主密钥与已保存的 Key 不匹配。"""


@dataclass(frozen=True, slots=True)
class ApiKeyInfo:
    """仅向 Key 创建者展示的 API Key 信息。"""

    id: UUID
    project_id: UUID
    created_by_user_id: UUID
    created_by_username: str
    name: str
    key_prefix: str
    key_last_four: str
    secret: str | None
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedApiKey:
    """创建结果及 Key 明文。"""

    info: ApiKeyInfo
    secret: str


@dataclass(frozen=True, slots=True)
class VerifiedApiKey:
    """验证成功后供后续网关用例使用的项目和 Key 标识。"""

    id: UUID | None
    project_id: UUID
    owner_id: UUID
    name: str
    user_id: UUID | None = None
    username: str | None = None


class ApiKeyService:
    """使用 HMAC 摘要校验 Key，并使用 Fernet 加密保存可授权查看的密钥。"""

    prefix_length = 20

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], secret_key: str) -> None:
        self._session_factory = session_factory
        root_key = secret_key.encode("utf-8")
        self._digest_key = hmac.new(root_key, b"api-key-digest-v1", hashlib.sha256).digest()
        encryption_key = hmac.new(root_key, b"api-key-encryption-v1", hashlib.sha256).digest()
        self._cipher = Fernet(base64.urlsafe_b64encode(encryption_key))

    async def list_for_project(self, project_id: UUID, user: User) -> list[ApiKeyInfo]:
        async with self._session_factory() as session:
            await self._require_project_role(session, project_id, user, {"owner", "editor"})
            result = await session.execute(
                select(ApiKey, User.username)
                .join(User, User.id == ApiKey.created_by_user_id)
                .where(
                    ApiKey.project_id == project_id,
                    ApiKey.created_by_user_id == user.id,
                )
                .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
            )
            return [self._to_info(key, username) for key, username in result.all()]

    async def create(
        self, project_id: UUID, user: User, name: str, expires_at: datetime | None
    ) -> CreatedApiKey:
        name = name.strip()
        if not name:
            raise ValueError("Key 名称不能为空")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                raise ValueError("expires_at 必须包含时区")
            if expires_at <= datetime.now(UTC):
                raise ValueError("expires_at 必须晚于当前时间")
        secret = f"agw_{secrets.token_urlsafe(32)}"
        key = ApiKey(
            project_id=project_id,
            created_by_user_id=user.id,
            name=name,
            key_hash=self._digest(secret),
            encrypted_secret=self._cipher.encrypt(secret.encode("utf-8")).decode("ascii"),
            key_prefix=secret[: self.prefix_length],
            key_last_four=secret[-4:],
            status="active",
            expires_at=expires_at,
        )
        async with self._session_factory.begin() as session:
            await self._require_project_role(session, project_id, user, {"owner", "editor"})
            session.add(key)
            await session.flush()
            await session.refresh(key)
            info = self._to_info(key, user.username)
        return CreatedApiKey(info=info, secret=secret)

    async def revoke(self, project_id: UUID, key_id: UUID, user: User) -> None:
        async with self._session_factory.begin() as session:
            await self._require_project_role(session, project_id, user, {"owner", "editor"})
            key = await session.scalar(
                select(ApiKey).where(
                    ApiKey.project_id == project_id,
                    ApiKey.id == key_id,
                    ApiKey.created_by_user_id == user.id,
                )
            )
            if key is None:
                raise ApiKeyNotFoundError("API Key 不存在")
            if key.status != "revoked":
                key.status = "revoked"
                key.revoked_at = datetime.now(UTC)

    async def verify(self, secret: str) -> VerifiedApiKey:
        """供网关调用认证使用；不泄露 Key 是否存在，并记录最近使用时间。"""
        prefix = secret[: self.prefix_length]
        async with self._session_factory.begin() as session:
            row = await session.execute(
                select(ApiKey, Project.owner_id, User.username)
                .join(Project, Project.id == ApiKey.project_id)
                .join(
                    ProjectMember,
                    (ProjectMember.project_id == ApiKey.project_id)
                    & (ProjectMember.user_id == ApiKey.created_by_user_id),
                )
                .join(User, User.id == ApiKey.created_by_user_id)
                .where(
                    ApiKey.key_prefix == prefix,
                    ProjectMember.role.in_({"owner", "editor"}),
                )
            )
            result = row.one_or_none()
            key = result[0] if result is not None else None
            if (
                key is None
                or not hmac.compare_digest(key.key_hash, self._digest(secret))
                or key.status != "active"
                or (key.expires_at is not None and key.expires_at <= datetime.now(UTC))
            ):
                raise ApiKeyInvalidError("API Key 无效、已撤销或已过期")
            now = datetime.now(UTC)
            # 每次请求都写同一 API Key 行会制造不必要的写放大和并发锁竞争。
            # 最近使用时间按分钟粒度更新，首次使用仍会立即记录。
            await session.execute(
                update(ApiKey)
                .where(
                    ApiKey.id == key.id,
                    or_(
                        ApiKey.last_used_at.is_(None),
                        ApiKey.last_used_at < now - timedelta(minutes=1),
                    ),
                )
                .values(last_used_at=now)
            )
            assert result is not None
            return VerifiedApiKey(
                id=key.id,
                project_id=key.project_id,
                owner_id=result[1],
                name=key.name,
                user_id=key.created_by_user_id,
                username=result[2],
            )

    def _digest(self, secret: str) -> str:
        return hmac.new(self._digest_key, secret.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    async def _require_project_role(
        session: AsyncSession, project_id: UUID, user: User, allowed_roles: set[str]
    ) -> None:
        project_exists = await session.scalar(select(Project.id).where(Project.id == project_id))
        if project_exists is None:
            raise ApiKeyNotFoundError("项目不存在")
        role = await session.scalar(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user.id,
            )
        )
        if role not in allowed_roles:
            message = (
                "需要项目 owner 或 editor 权限才能管理 API Key"
                if allowed_roles == {"owner"}
                else "需要项目 owner 或 editor 权限才能查看 API Key"
            )
            raise ApiKeyForbiddenError(message)

    def _to_info(self, key: ApiKey, username: str) -> ApiKeyInfo:
        state = key.status
        if state == "active" and key.expires_at is not None and key.expires_at <= datetime.now(UTC):
            state = "expired"
        try:
            secret = (
                self._cipher.decrypt(key.encrypted_secret.encode("ascii")).decode("utf-8")
                if key.encrypted_secret is not None
                else None
            )
        except InvalidToken as error:
            raise ApiKeyConfigurationError(
                "无法解密 API Key，请确认 API_KEY_SECRET_KEY 与创建密钥时一致"
            ) from error
        return ApiKeyInfo(
            id=key.id,
            project_id=key.project_id,
            created_by_user_id=key.created_by_user_id,
            created_by_username=username,
            name=key.name,
            key_prefix=key.key_prefix,
            key_last_four=key.key_last_four,
            secret=secret,
            status=state,
            expires_at=key.expires_at,
            last_used_at=key.last_used_at,
            revoked_at=key.revoked_at,
            created_at=key.created_at,
        )
