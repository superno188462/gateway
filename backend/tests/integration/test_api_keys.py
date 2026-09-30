"""A3 API Key 集成测试；只清理本测试创建的项目和用户。"""

import os
import secrets
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.api_keys import (
    ApiKeyConfigurationError,
    ApiKeyForbiddenError,
    ApiKeyInvalidError,
    ApiKeyService,
    VerifiedApiKey,
)
from app.bootstrap import create_app
from app.config import Settings
from app.infrastructure.db.models import ApiKey, AuthSession, Project, ProjectMember, User
from app.security import JwtService

pytestmark = pytest.mark.integration


def database_url() -> str:
    """优先使用显式隔离库；开发者确认的 .env 数据库仅用于非破坏性本测试。"""
    return os.getenv("TEST_DATABASE_URL") or Settings().database_url


async def test_personal_project_key_lifecycle_and_membership_revocation() -> None:
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, editor_id, other_id, project_id = uuid4(), uuid4(), uuid4(), uuid4()
    suffix = secrets.token_hex(8)
    owner = User(
        id=owner_id, username=f"test_a3_owner_{suffix}", password_hash="unused", role="user"
    )
    other = User(
        id=other_id, username=f"test_a3_other_{suffix}", password_hash="unused", role="user"
    )
    editor = User(
        id=editor_id, username=f"test_a3_editor_{suffix}", password_hash="unused", role="user"
    )
    app: FastAPI | None = None
    try:
        async with factory.begin() as session:
            session.add_all([owner, editor, other])
            await session.flush()
            session.add(
                Project(
                    id=project_id,
                    name=f"A3 test {suffix}",
                    description=None,
                    status="active",
                    visibility="private",
                    owner_id=owner_id,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ProjectMember(project_id=project_id, user_id=owner_id, role="owner"),
                    ProjectMember(project_id=project_id, user_id=editor_id, role="editor"),
                ]
            )

        jwt_service = JwtService("api-key-integration-jwt-secret-32-characters", 30)
        owner_token, owner_jti, owner_expires = jwt_service.issue(str(owner_id), "user")
        editor_token, editor_jti, editor_expires = jwt_service.issue(str(editor_id), "user")
        async with factory.begin() as session:
            session.add_all(
                [
                    AuthSession(jti=owner_jti, user_id=owner_id, expires_at=owner_expires),
                    AuthSession(jti=editor_jti, user_id=editor_id, expires_at=editor_expires),
                ]
            )

        service = ApiKeyService(factory, "test-api-key-secret-that-is-long-enough-32")
        created = await service.create(project_id, owner, "integration test", None)
        listed = await service.list_for_project(project_id, owner)
        assert len(listed) == 1
        assert listed[0].key_prefix == created.secret[:20]
        assert listed[0].key_last_four == created.secret[-4:]
        assert listed[0].secret == created.secret
        assert listed[0].created_by_user_id == owner_id
        assert listed[0].created_by_username == owner.username
        assert await service.verify(created.secret) == VerifiedApiKey(
            id=created.info.id,
            project_id=created.info.project_id,
            owner_id=owner_id,
            name=created.info.name,
            user_id=owner_id,
            username=owner.username,
        )
        editor_created = await service.create(project_id, editor, "editor personal key", None)
        assert [item.id for item in await service.list_for_project(project_id, owner)] == [
            created.info.id
        ]
        assert [item.id for item in await service.list_for_project(project_id, editor)] == [
            editor_created.info.id
        ]

        app = create_app(
            settings=Settings(
                database_url=database_url(),
                jwt_secret_key="api-key-integration-jwt-secret-32-characters",
                api_key_secret_key="test-api-key-secret-that-is-long-enough-32",
                _env_file=None,
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            owner_headers = {"Authorization": f"Bearer {owner_token}"}
            editor_headers = {"Authorization": f"Bearer {editor_token}"}
            created_response = await client.post(
                f"/api/admin/v1/projects/{project_id}/keys",
                headers=owner_headers,
                json={"name": "HTTP created key", "expires_at": None},
            )
            assert created_response.status_code == 201
            http_secret = created_response.json()["secret"]
            owner_list = await client.get(
                f"/api/admin/v1/projects/{project_id}/keys", headers=owner_headers
            )
            editor_list = await client.get(
                f"/api/admin/v1/projects/{project_id}/keys", headers=editor_headers
            )
            assert owner_list.status_code == editor_list.status_code == 200
            assert {item["secret"] for item in owner_list.json()} == {
                created.secret,
                http_secret,
            }
            assert [item["created_by_user_id"] for item in editor_list.json()] == [
                str(editor_id)
            ]
            editor_created_response = await client.post(
                f"/api/admin/v1/projects/{project_id}/keys",
                headers=editor_headers,
                json={"name": "editor http key"},
            )
            assert editor_created_response.status_code == 201
            editor_secret = editor_created_response.json()["secret"]
            owner_keys_after_editor = await client.get(
                f"/api/admin/v1/projects/{project_id}/keys", headers=owner_headers
            )
            assert editor_secret not in [item["secret"] for item in owner_keys_after_editor.json()]
            editor_keys_after_create = await client.get(
                f"/api/admin/v1/projects/{project_id}/keys", headers=editor_headers
            )
            assert editor_secret in [item["secret"] for item in editor_keys_after_create.json()]
            other_key_revoke = await client.delete(
                f"/api/admin/v1/projects/{project_id}/keys/{editor_created.info.id}",
                headers=owner_headers,
            )
            assert other_key_revoke.status_code == 404
            demoted = await client.patch(
                f"/api/admin/v1/projects/{project_id}/members/{editor_id}",
                headers=owner_headers,
                json={"role": "viewer"},
            )
            assert demoted.status_code == 200
            assert demoted.json()["role"] == "viewer"
            with pytest.raises(ApiKeyInvalidError):
                await service.verify(editor_secret)
            viewer_list = await client.get(
                f"/api/admin/v1/projects/{project_id}/keys", headers=editor_headers
            )
            assert viewer_list.status_code == 403
            promoted = await client.patch(
                f"/api/admin/v1/projects/{project_id}/members/{editor_id}",
                headers=owner_headers,
                json={"role": "editor"},
            )
            assert promoted.status_code == 200
            editor_key_after_promotion = await service.create(
                project_id, editor, "removed member key", None
            )
            removed = await client.delete(
                f"/api/admin/v1/projects/{project_id}/members/{editor_id}",
                headers=owner_headers,
            )
            assert removed.status_code == 204
            with pytest.raises(ApiKeyInvalidError):
                await service.verify(editor_key_after_promotion.secret)
        async with factory() as session:
            stored = await session.get(ApiKey, created.info.id)
            assert stored is not None
            assert stored.created_by_user_id == owner_id
            assert stored.key_hash != created.secret
            assert created.secret not in stored.key_hash
            assert stored.encrypted_secret is not None
            assert stored.encrypted_secret != created.secret
            assert created.secret not in stored.encrypted_secret

        with pytest.raises(ApiKeyForbiddenError):
            await service.list_for_project(project_id, other)
        with pytest.raises(ApiKeyForbiddenError):
            await service.list_for_project(project_id, editor)
        wrong_key_service = ApiKeyService(factory, "different-api-key-secret-is-also-32-chars")
        with pytest.raises(ApiKeyConfigurationError):
            await wrong_key_service.list_for_project(project_id, owner)

        await service.revoke(project_id, created.info.id, owner)
        await service.revoke(project_id, created.info.id, owner)
        with pytest.raises(ApiKeyInvalidError):
            await service.verify(created.secret)
        revoked = await service.list_for_project(project_id, owner)
        assert next(key for key in revoked if key.id == created.info.id).status == "revoked"
        with pytest.raises(ApiKeyInvalidError):
            await service.verify(editor_created.secret)
    finally:
        if app is not None:
            await app.state.container.close()
        async with factory.begin() as session:
            await session.execute(delete(Project).where(Project.id == project_id))
            await session.execute(delete(User).where(User.id.in_([owner_id, editor_id, other_id])))
        await engine.dispose()
