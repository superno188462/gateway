"""A6 项目资源的隔离、角色权限、软删除和版本冲突集成测试。"""

import os
import secrets
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bootstrap import create_app
from app.config import Settings
from app.infrastructure.db.models import AuthSession, Project, ProjectMember, User
from app.resources.application import ResourceService
from app.resources.domain import ResourceForbiddenError
from app.resources.infrastructure import PostgresResourceRepository
from app.security import JwtService

pytestmark = pytest.mark.integration


def database_url() -> str:
    """Use an isolated test database when supplied, otherwise the configured dev database."""
    return os.getenv("TEST_DATABASE_URL") or Settings().database_url


async def test_project_resource_lifecycle_and_isolation() -> None:
    """Verify A6 CRUD while enforcing project, role, and optimistic version boundaries."""
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = secrets.token_hex(8)
    owner_id, editor_id, viewer_id, outsider_id = (uuid4() for _ in range(4))
    private_project_id, public_project_id = uuid4(), uuid4()
    user_ids = (owner_id, editor_id, viewer_id, outsider_id)
    project_ids = (private_project_id, public_project_id)
    users = [
        User(id=user_id, username=f"a6_{suffix}_{index}", password_hash="unused", role="user")
        for index, user_id in enumerate(user_ids)
    ]
    jwt_service = JwtService("a6-resource-integration-secret-long-enough", 30)
    auth_tokens: dict[UUID, str] = {}
    app = None
    try:
        async with factory.begin() as session:
            session.add_all(users)
            await session.flush()
            session.add_all(
                [
                    Project(
                        id=private_project_id,
                        name=f"A6 private {suffix}",
                        status="active",
                        visibility="private",
                        owner_id=owner_id,
                    ),
                    Project(
                        id=public_project_id,
                        name=f"A6 public {suffix}",
                        status="active",
                        visibility="public",
                        owner_id=owner_id,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ProjectMember(project_id=private_project_id, user_id=owner_id, role="owner"),
                    ProjectMember(project_id=private_project_id, user_id=editor_id, role="editor"),
                    ProjectMember(project_id=private_project_id, user_id=viewer_id, role="viewer"),
                    ProjectMember(project_id=public_project_id, user_id=owner_id, role="owner"),
                ]
            )
            for user_id in user_ids:
                token, jti, expires_at = jwt_service.issue(str(user_id), "user")
                auth_tokens[user_id] = token
                session.add(AuthSession(jti=jti, user_id=user_id, expires_at=expires_at))

        settings = Settings(
            database_url=database_url(),
            jwt_secret_key="a6-resource-integration-secret-long-enough",
            _env_file=None,
        )
        app = create_app(settings=settings)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            owner_headers = {"Authorization": f"Bearer {auth_tokens[owner_id]}"}
            editor_headers = {"Authorization": f"Bearer {auth_tokens[editor_id]}"}
            viewer_headers = {"Authorization": f"Bearer {auth_tokens[viewer_id]}"}
            outsider_headers = {"Authorization": f"Bearer {auth_tokens[outsider_id]}"}
            prefix = f"/api/admin/v1/projects/{private_project_id}/resources"
            public_prefix = f"/api/admin/v1/projects/{public_project_id}/resources"

            directories = await client.get(f"{prefix}/directories", headers=owner_headers)
            assert directories.status_code == 200
            assert len(directories.json()) == 6

            created = await client.post(
                prefix,
                headers=owner_headers,
                json={
                    "resource_type": "template",
                    "category": "system",
                    "name": "  base prompt.md  ",
                    "content": "Be helpful.",
                },
            )
            assert created.status_code == 201
            resource_id = created.json()["id"]
            assert created.json()["name"] == "base prompt.md"
            assert created.json()["version"] == 1

            duplicate = await client.post(
                prefix,
                headers=owner_headers,
                json={
                    "resource_type": "template",
                    "category": "system",
                    "name": "base prompt.md",
                    "content": "duplicate",
                },
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["detail"]["code"] == "resource_name_conflict"

            editor_read = await client.get(prefix, headers=editor_headers)
            assert editor_read.status_code == 200
            assert len(editor_read.json()) == 1
            viewer_read = await client.get(prefix, headers=viewer_headers)
            assert viewer_read.status_code == 200
            assert len(viewer_read.json()) == 1

            editor_update = await client.patch(
                f"{prefix}/{resource_id}",
                headers=editor_headers,
                json={"expected_version": 1, "name": "base prompt.md", "content": "Updated."},
            )
            assert editor_update.status_code == 200
            assert editor_update.json()["version"] == 2

            stale_update = await client.patch(
                f"{prefix}/{resource_id}",
                headers=owner_headers,
                json={"expected_version": 1, "name": "base prompt.md", "content": "Stale."},
            )
            assert stale_update.status_code == 409
            assert stale_update.json()["detail"]["code"] == "version_conflict"

            viewer_write = await client.patch(
                f"{prefix}/{resource_id}",
                headers=viewer_headers,
                json={"expected_version": 2, "name": "base prompt.md", "content": "Denied."},
            )
            assert viewer_write.status_code == 403
            private_denied = await client.get(prefix, headers=outsider_headers)
            assert private_denied.status_code == 403

            public_resource = await client.post(
                public_prefix,
                headers=owner_headers,
                json={
                    "resource_type": "memory",
                    "category": "longterm",
                    "name": "shared.md",
                    "content": "Shared review content.",
                },
            )
            public_read = await client.get(public_prefix, headers=outsider_headers)
            assert public_read.status_code == 200
            assert public_read.json()[0]["id"] == public_resource.json()["id"]
            cross_project_read = await client.get(
                f"{prefix}/{public_resource.json()['id']}", headers=owner_headers
            )
            assert cross_project_read.status_code == 404

            removed = await client.delete(
                f"{prefix}/{resource_id}?expected_version=2", headers=owner_headers
            )
            assert removed.status_code == 204
            assert (await client.get(prefix, headers=owner_headers)).json() == []
            missing = await client.get(f"{prefix}/{resource_id}", headers=owner_headers)
            assert missing.status_code == 404

            recreated = await client.post(
                prefix,
                headers=owner_headers,
                json={
                    "resource_type": "template",
                    "category": "system",
                    "name": "base prompt.md",
                    "content": "A new active version.",
                },
            )
            assert recreated.status_code == 201

            viewer_operation_log = await client.get(
                f"/api/v1/projects/{private_project_id}/requests", headers=viewer_headers
            )
            assert viewer_operation_log.status_code == 200
            operation_items = [
                item
                for item in viewer_operation_log.json()["items"]
                if item["event_type"] == "project_operation"
            ]
            assert {item["actor_username"] for item in operation_items} == {
                users[0].username,
                users[1].username,
            }
            assert any("创建项目资源" in item["description"] for item in operation_items)
            assert all("Be helpful." not in item["description"] for item in operation_items)
            outsider_operation_log = await client.get(
                f"/api/v1/projects/{private_project_id}/requests", headers=outsider_headers
            )
            assert outsider_operation_log.status_code == 404

            invalid_directory = await client.post(
                prefix,
                headers=owner_headers,
                json={
                    "resource_type": "template",
                    "category": "sessions",
                    "name": "wrong category",
                    "content": "invalid",
                },
            )
            assert invalid_directory.status_code == 422

        repository = PostgresResourceRepository(factory)
        reviewer = ResourceService(repository)
        assert (
            len(await reviewer.list_for_project(private_project_id, outsider_id, True, None, None))
            == 1
        )
        with pytest.raises(ResourceForbiddenError):
            await reviewer.create(
                private_project_id, outsider_id, "template", "system", "admin-write", "denied"
            )
    finally:
        if app is not None:
            await app.state.container.close()
        async with factory.begin() as session:
            await session.execute(delete(Project).where(Project.id.in_(project_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await engine.dispose()
