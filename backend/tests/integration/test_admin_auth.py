"""A1 管理员引导和认证集成测试。"""

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.auth import AdminBootstrapError, AdminBootstrapService
from app.bootstrap import create_app
from app.config import Settings
from app.infrastructure.db.models import AuthSession, Project, ProjectMember, User
from app.security import PasswordService

pytestmark = pytest.mark.integration


def database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("未设置 TEST_DATABASE_URL")
    return value


async def clear_auth_data(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(delete(ProjectMember))
        await session.execute(delete(Project))
        await session.execute(delete(AuthSession))
        await session.execute(delete(User))


async def test_admin_bootstrap_and_login_logout_flow() -> None:
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await clear_auth_data(factory)
        password_service = PasswordService()
        bootstrap = AdminBootstrapService(factory, password_service, "admin", "correct-password")
        admin = await bootstrap.ensure()
        assert admin.username == "admin"
        assert password_service.verify("correct-password", admin.password_hash)
        async with factory.begin() as session:
            session.add(
                User(
                    username="normal-user",
                    password_hash=password_service.hash("user-password"),
                    role="user",
                )
            )

        settings = Settings(
            database_url=database_url(),
            jwt_secret_key="a" * 32,
            _env_file=None,
        )
        app = create_app(settings=settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wrong_password = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "wrong-password"},
                )
                assert wrong_password.status_code == 401

                response = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "correct-password"},
                )
                assert response.status_code == 200
                token = response.json()["access_token"]

                me = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert me.status_code == 200
                assert me.json()["username"] == "admin"
                user_login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "normal-user", "password": "user-password"},
                )
                assert user_login.status_code == 200
                user_me = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {user_login.json()['access_token']}"},
                )
                assert user_me.json()["role"] == "user"
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"username": "registered-user", "password": "register-password"},
                )
                assert registered.status_code == 201
                assert registered.json()["role"] == "user"
                duplicate = await client.post(
                    "/api/v1/auth/register",
                    json={"username": "registered-user", "password": "register-password"},
                )
                assert duplicate.status_code == 409

                logout = await client.post(
                    "/api/v1/auth/logout",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert logout.status_code == 204
                revoked = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert revoked.status_code == 401

        await clear_auth_data(factory)
    finally:
        await engine.dispose()


async def test_missing_admin_without_environment_fails() -> None:
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await clear_auth_data(factory)
        service = AdminBootstrapService(factory, PasswordService(), None, None)
        with pytest.raises(AdminBootstrapError, match="没有管理员"):
            await service.ensure()
    finally:
        await clear_auth_data(factory)
        await engine.dispose()
