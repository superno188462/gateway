"""A2 项目和项目成员权限集成测试。"""

import os
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bootstrap import create_app
from app.config import Settings
from app.infrastructure.db.models import Project, ProjectMember, User

pytestmark = pytest.mark.integration


def get_test_database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("未设置 TEST_DATABASE_URL")
    return value


async def test_project_membership_filters_and_authorizes() -> None:
    engine = create_async_engine(get_test_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory.begin() as session:
            await session.execute(delete(ProjectMember))
            await session.execute(delete(Project))
            await session.execute(delete(User))

        settings = Settings(
            database_url=get_test_database_url(),
            jwt_secret_key="a" * 32,
            admin_username="admin",
            admin_password="correct-password",
            _env_file=None,
        )
        app = create_app(settings=settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                admin_login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "correct-password"},
                )
                admin_token = admin_login.json()["access_token"]
                admin_headers = {"Authorization": f"Bearer {admin_token}"}

                created = await client.post(
                    "/api/admin/v1/projects",
                    headers=admin_headers,
                    json={"name": "Demo Project", "description": "A2"},
                )
                assert created.status_code == 201
                project_id = created.json()["id"]

                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"username": "project-user", "password": "user-password"},
                )
                assert registered.status_code == 201
                user_id = registered.json()["id"]

                member = await client.post(
                    f"/api/admin/v1/projects/{project_id}/members",
                    headers=admin_headers,
                    json={"user_id": user_id, "role": "viewer"},
                )
                assert member.status_code == 201

                user_login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "project-user", "password": "user-password"},
                )
                user_headers = {"Authorization": f"Bearer {user_login.json()['access_token']}"}
                visible = await client.get("/api/admin/v1/projects", headers=user_headers)
                assert visible.status_code == 200
                assert [item["id"] for item in visible.json()] == [project_id]

                forbidden_create = await client.post(
                    "/api/admin/v1/projects",
                    headers=user_headers,
                    json={"name": "Forbidden"},
                )
                assert forbidden_create.status_code == 403

                forbidden_update = await client.patch(
                    f"/api/admin/v1/projects/{project_id}",
                    headers=user_headers,
                    json={"name": "Changed"},
                )
                assert forbidden_update.status_code == 403

                admin_visible = await client.get("/api/admin/v1/projects", headers=admin_headers)
                assert admin_visible.status_code == 200
                assert len(admin_visible.json()) == 1
                assert UUID(project_id)
    finally:
        await engine.dispose()
