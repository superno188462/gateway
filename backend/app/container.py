"""应用依赖容器和 FastAPI 依赖适配器。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.auth import AdminBootstrapService, AuthService
from app.config import Settings
from app.domain.health import ReadinessProbe
from app.infrastructure.db.engine import SqlAlchemyReadinessProbe, create_database_engine
from app.infrastructure.db.models import User
from app.security import JwtService, PasswordService


@dataclass(slots=True)
class AppContainer:
    """应用生命周期内唯一的依赖注册表。

    配置、数据库引擎和基础设施服务在这里创建一次，并由 FastAPI 的 Depends
    适配器按请求注入。业务服务在后续阶段继续注册到同一个容器。
    """

    settings: Settings
    database_engine: AsyncEngine | None
    readiness_probe: ReadinessProbe
    session_factory: async_sessionmaker[AsyncSession] | None
    password_service: PasswordService
    jwt_service: JwtService
    admin_bootstrap: AdminBootstrapService | None
    auth_service: AuthService | None

    @classmethod
    def build(
        cls,
        settings: Settings,
        readiness_probe: ReadinessProbe | None = None,
    ) -> "AppContainer":
        """创建容器并注册 B0 所需的单例服务。"""
        if readiness_probe is not None:
            return cls(
                settings=settings,
                database_engine=None,
                readiness_probe=readiness_probe,
                session_factory=None,
                password_service=PasswordService(),
                jwt_service=JwtService(
                    settings.jwt_secret_key,
                    settings.jwt_access_token_expire_minutes,
                ),
                admin_bootstrap=None,
                auth_service=None,
            )

        database_engine = create_database_engine(settings.database_url)
        session_factory = async_sessionmaker(database_engine, expire_on_commit=False)
        password_service = PasswordService()
        jwt_service = JwtService(settings.jwt_secret_key, settings.jwt_access_token_expire_minutes)
        return cls(
            settings=settings,
            database_engine=database_engine,
            readiness_probe=SqlAlchemyReadinessProbe(database_engine),
            session_factory=session_factory,
            password_service=password_service,
            jwt_service=jwt_service,
            admin_bootstrap=AdminBootstrapService(
                session_factory,
                password_service,
                settings.admin_username,
                settings.admin_password,
            ),
            auth_service=AuthService(session_factory, password_service, jwt_service),
        )

    async def startup(self) -> None:
        """执行启动前管理员状态校验和环境变量引导。"""
        if self.admin_bootstrap is not None:
            await self.admin_bootstrap.ensure()

    async def close(self) -> None:
        """释放容器拥有的异步资源。"""
        if self.database_engine is not None:
            await self.database_engine.dispose()


def get_container(request: Request) -> AppContainer:
    """从应用状态取得当前应用的容器单例。"""
    return cast(AppContainer, request.app.state.container)


def get_settings(request: Request) -> Settings:
    """注入配置单例。"""
    return get_container(request).settings


def get_readiness_probe(request: Request) -> ReadinessProbe:
    """注入数据库就绪探针单例。"""
    return get_container(request).readiness_probe


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为业务请求提供短生命周期数据库会话。"""
    session_factory = get_container(request).session_factory
    if session_factory is None:
        raise RuntimeError("数据库会话未注册")
    async with session_factory() as session:
        yield session


def get_auth_service(request: Request) -> AuthService:
    """注入通用认证服务单例。"""
    service = get_container(request).auth_service
    if service is None:
        raise RuntimeError("认证服务未注册")
    return service


bearer_scheme = HTTPBearer(auto_error=False)
_bearer_dependency = Depends(bearer_scheme)
_auth_service_dependency = Depends(get_auth_service)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = _bearer_dependency,
    auth_service: AuthService = _auth_service_dependency,
) -> User:
    """校验 Bearer 令牌并返回当前用户。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_token", "message": "需要访问令牌"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await auth_service.require_user(credentials.credentials)


_current_user_dependency = Depends(get_current_user)


async def get_current_admin(
    user: User = _current_user_dependency,
) -> User:
    """校验 Bearer 令牌并要求管理员角色。"""
    if user.role != "admin":
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "admin_required", "message": "需要管理员权限"},
        )
    return user
