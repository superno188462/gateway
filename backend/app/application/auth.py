"""管理员引导和通用认证用例。"""

from datetime import UTC, datetime

import jwt
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import AuthSession, User
from app.security import JwtService, PasswordService


class AdminBootstrapError(RuntimeError):
    """管理员引导状态不满足启动要求。"""


class AdminBootstrapService:
    """启动时同步唯一管理员，或验证数据库中已有管理员。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        password_service: PasswordService,
        admin_username: str | None,
        admin_password: str | None,
    ) -> None:
        self._session_factory = session_factory
        self._password_service = password_service
        self._admin_username = admin_username
        self._admin_password = admin_password

    async def ensure(self) -> User:
        async with self._session_factory.begin() as session:
            result = await session.execute(select(User).where(User.role == "admin"))
            admins = list(result.scalars().all())
            if len(admins) > 1:
                raise AdminBootstrapError("数据库中存在多个管理员，无法自动修复")

            if self._admin_username is not None and self._admin_password is not None:
                if admins:
                    admin = admins[0]
                    admin.username = self._admin_username.strip()
                    admin.password_hash = self._password_service.hash(self._admin_password)
                else:
                    admin = User(
                        username=self._admin_username.strip(),
                        password_hash=self._password_service.hash(self._admin_password),
                        role="admin",
                    )
                    session.add(admin)
                await session.flush()
                return admin

            if not admins:
                raise AdminBootstrapError(
                    "数据库中没有管理员，请配置 ADMIN_USERNAME 和 ADMIN_PASSWORD"
                )
            return admins[0]


class AuthService:
    """统一登录、令牌校验和主动退出。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        password_service: PasswordService,
        jwt_service: JwtService,
    ) -> None:
        self._session_factory = session_factory
        self._password_service = password_service
        self._jwt_service = jwt_service

    async def login(self, username: str, password: str) -> tuple[str, int]:
        async with self._session_factory.begin() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if user is None or not self._password_service.verify(password, user.password_hash):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"code": "invalid_credentials", "message": "用户名或密码错误"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token, jti, expires_at = self._jwt_service.issue(str(user.id), user.role)
            session.add(AuthSession(jti=jti, user_id=user.id, expires_at=expires_at))
        return token, self._jwt_service.expires_minutes * 60

    async def require_user(self, token: str) -> User:
        try:
            claims = self._jwt_service.decode(token)
            user_id = claims["sub"]
            jti = claims["jti"]
        except (KeyError, TypeError, ValueError, jwt.PyJWTError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "invalid_token", "message": "访问令牌无效或已过期"},
                headers={"WWW-Authenticate": "Bearer"},
            ) from None

        async with self._session_factory() as session:
            session_result = await session.execute(
                select(AuthSession).where(AuthSession.jti == jti)
            )
            auth_session = session_result.scalar_one_or_none()
            if (
                auth_session is None
                or auth_session.revoked_at is not None
                or auth_session.expires_at <= datetime.now(UTC)
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"code": "invalid_token", "message": "访问令牌无效或已过期"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            user_result = await session.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"code": "invalid_token", "message": "访问令牌无效或已过期"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return user

    async def require_admin(self, token: str) -> User:
        """校验令牌并要求管理员角色。"""
        user = await self.require_user(token)
        if user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "admin_required", "message": "需要管理员权限"},
            )
        return user

    async def logout(self, token: str) -> None:
        try:
            claims = self._jwt_service.decode(token)
            jti = claims["jti"]
        except (KeyError, TypeError, ValueError, jwt.PyJWTError):
            return
        async with self._session_factory.begin() as session:
            await session.execute(
                update(AuthSession)
                .where(AuthSession.jti == jti, AuthSession.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
