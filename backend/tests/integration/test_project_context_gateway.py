"""A6 项目上下文网关订阅、隔离和请求日志集成测试。"""

import os
import secrets
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.api_keys import ApiKeyService
from app.bootstrap import create_app
from app.config import Settings
from app.infrastructure.db.models import (
    AuthSession,
    GatewayRequest,
    Project,
    ProjectMember,
    ProjectServiceSubscription,
    User,
)
from app.security import JwtService

pytestmark = pytest.mark.integration


def database_url() -> str:
    return os.getenv("TEST_DATABASE_URL") or Settings().database_url


async def test_context_runtime_is_project_scoped_and_logged() -> None:
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = secrets.token_hex(8)
    owner_id, project_id, unsubscribed_project_id = uuid4(), uuid4(), uuid4()
    user = User(id=owner_id, username=f"context_{suffix}", password_hash="unused", role="user")
    jwt = JwtService("a6-context-integration-jwt-secret-32-characters", 30)
    user_token, jti, expires_at = jwt.issue(str(owner_id), "user")
    app = None
    trace_ids: set[str] = set()
    project_ids = (project_id, unsubscribed_project_id)
    try:
        async with factory.begin() as session:
            session.add(user)
            await session.flush()
            session.add_all(
                [
                    Project(
                        id=project_id,
                        name=f"context {suffix}",
                        status="active",
                        visibility="private",
                        owner_id=owner_id,
                    ),
                    Project(
                        id=unsubscribed_project_id,
                        name=f"context2 {suffix}",
                        status="active",
                        visibility="private",
                        owner_id=owner_id,
                    ),
                    AuthSession(jti=jti, user_id=owner_id, expires_at=expires_at),
                    ProjectMember(project_id=project_id, user_id=owner_id, role="owner"),
                    ProjectMember(
                        project_id=unsubscribed_project_id, user_id=owner_id, role="owner"
                    ),
                    ProjectServiceSubscription(
                        project_id=project_id,
                        service_code="project-context-v1",
                        monthly_token_limit=None,
                    ),
                ]
            )
        key_service = ApiKeyService(factory, "a6-context-api-key-secret-32-characters")
        active_key = await key_service.create(project_id, user, "active context", None)
        inactive_key = await key_service.create(
            unsubscribed_project_id, user, "not subscribed", None
        )
        app = create_app(
            settings=Settings(
                database_url=database_url(),
                jwt_secret_key="a6-context-integration-jwt-secret-32-characters",
                api_key_secret_key="a6-context-api-key-secret-32-characters",
                _env_file=None,
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            user_headers = {"Authorization": f"Bearer {user_token}"}
            key_headers = {"Authorization": f"Bearer {active_key.secret}"}
            created = await client.post(
                "/v1/context/templates",
                headers=key_headers,
                json={
                    "category": "system",
                    "name": "system",
                    "content": "Be helpful",
                },
            )
            assert created.status_code == 201
            created_template = created.json()
            assert created_template["project_id"] == str(project_id)
            trace_ids.add(created.headers["x-trace-id"])
            duplicate_template = await client.post(
                "/v1/context/templates",
                headers=key_headers,
                json={
                    "category": "system",
                    "name": "system",
                    "content": "Duplicate name",
                },
            )
            assert duplicate_template.status_code == 409

            templates = await client.get("/v1/context/templates", headers=key_headers)
            assert templates.status_code == 200
            assert templates.json()[0]["content"] == "Be helpful"
            trace_ids.add(templates.headers["x-trace-id"])
            template_id = created_template["id"]
            template_by_id = await client.get(
                f"/v1/context/templates/by-id/{template_id}", headers=key_headers
            )
            assert template_by_id.status_code == 200
            assert template_by_id.json()["name"] == "system"
            trace_ids.add(template_by_id.headers["x-trace-id"])
            updated_template = await client.patch(
                f"/v1/context/templates/{template_id}",
                headers=key_headers,
                json={"expected_version": 1, "name": "system", "content": "Updated"},
            )
            assert updated_template.status_code == 200
            assert updated_template.json()["version"] == 2
            trace_ids.add(updated_template.headers["x-trace-id"])
            template_by_name = await client.get(
                "/v1/context/templates/system/system", headers=key_headers
            )
            assert template_by_name.status_code == 200
            assert template_by_name.json()["id"] == template_id
            assert template_by_name.json()["content"] == "Updated"
            trace_ids.add(template_by_name.headers["x-trace-id"])
            deleted_template = await client.delete(
                f"/v1/context/templates/{template_id}?expected_version=2",
                headers=key_headers,
            )
            assert deleted_template.status_code == 204
            trace_ids.add(deleted_template.headers["x-trace-id"])

            context_projects = await client.get(
                "/api/admin/v1/context/projects", headers=user_headers
            )
            assert context_projects.status_code == 200
            assert [item["id"] for item in context_projects.json()["items"]] == [str(project_id)]
            context_permissions = await client.get(
                f"/api/admin/v1/context/projects/{project_id}/permissions",
                headers=user_headers,
            )
            assert context_permissions.status_code == 200
            assert context_permissions.json() == {"can_read_user_data": True, "can_edit": True}

            saved = await client.put(
                "/v1/context/users/external-42/sessions/session-9/memories/state",
                headers=key_headers,
                json={"value": {"step": 3}, "ttl_seconds": 300},
            )
            assert saved.status_code == 200
            trace_ids.add(saved.headers["x-trace-id"])
            listed_session = await client.get(
                "/v1/context/users/external-42/sessions/session-9/memories",
                headers=key_headers,
            )
            assert listed_session.json()[0]["value"] == {"step": 3}
            trace_ids.add(listed_session.headers["x-trace-id"])

            appended_messages = await client.post(
                "/v1/context/users/external-42/sessions/session-9/messages",
                headers=key_headers,
                json={
                    "messages": [
                        {"role": "user", "content": "我喜欢简洁回答。"},
                        {"role": "assistant", "content": "好的，我会简洁回答。"},
                    ],
                    "ttl_seconds": 300,
                },
            )
            assert appended_messages.status_code == 201
            assert [item["sequence"] for item in appended_messages.json()["messages"]] == sorted(
                item["sequence"] for item in appended_messages.json()["messages"]
            )
            trace_ids.add(appended_messages.headers["x-trace-id"])
            listed_messages = await client.get(
                "/v1/context/users/external-42/sessions/session-9/messages?limit=10",
                headers=key_headers,
            )
            assert listed_messages.status_code == 200
            assert [item["role"] for item in listed_messages.json()["messages"]] == [
                "user",
                "assistant",
            ]
            trace_ids.add(listed_messages.headers["x-trace-id"])
            console_messages = await client.get(
                f"/api/admin/v1/context/projects/{project_id}/users/external-42/sessions/session-9/messages",
                headers=user_headers,
            )
            assert console_messages.status_code == 200
            assert len(console_messages.json()["messages"]) == 2
            cleared_messages = await client.delete(
                "/v1/context/users/external-42/sessions/session-9/messages",
                headers=key_headers,
            )
            assert cleared_messages.status_code == 204
            trace_ids.add(cleared_messages.headers["x-trace-id"])

            profile = await client.put(
                "/v1/context/users/external-42/profile",
                headers=key_headers,
                json={"profile": {"language": "zh-CN"}},
            )
            assert profile.status_code == 200
            trace_ids.add(profile.headers["x-trace-id"])
            console_profile = await client.get(
                f"/api/admin/v1/context/projects/{project_id}/users/external-42/profile",
                headers=user_headers,
            )
            assert console_profile.status_code == 200
            assert console_profile.json()["profile"] == {"language": "zh-CN"}

            long_memory = await client.post(
                "/v1/context/users/external-42/memories",
                headers=key_headers,
                json={"content": "喜欢简洁回答", "tags": ["preference"]},
            )
            assert long_memory.status_code == 201
            trace_ids.add(long_memory.headers["x-trace-id"])
            memory_id = long_memory.json()["id"]
            updated_memory = await client.patch(
                f"/v1/context/users/external-42/memories/{memory_id}",
                headers=key_headers,
                json={"content": "偏好简短中文回答", "tags": ["preference"], "expected_version": 1},
            )
            assert updated_memory.status_code == 200
            assert updated_memory.json()["version"] == 2
            trace_ids.add(updated_memory.headers["x-trace-id"])
            stale_memory = await client.delete(
                f"/v1/context/users/external-42/memories/{memory_id}?expected_version=1",
                headers=key_headers,
            )
            assert stale_memory.status_code == 409
            trace_ids.add(stale_memory.headers["x-trace-id"])
            removed_memory = await client.delete(
                f"/v1/context/users/external-42/memories/{memory_id}?expected_version=2",
                headers=key_headers,
            )
            assert removed_memory.status_code == 204
            trace_ids.add(removed_memory.headers["x-trace-id"])

            blocked = await client.get(
                "/v1/context/users/external-42/profile",
                headers={"Authorization": f"Bearer {inactive_key.secret}"},
            )
            assert blocked.status_code == 403
            trace_ids.add(blocked.headers["x-trace-id"])

            async with factory() as session:
                records = list(
                    (
                        await session.scalars(
                            select(GatewayRequest).where(GatewayRequest.trace_id.in_(trace_ids))
                        )
                    ).all()
                )
                assert len(records) == 17
                assert all(item.service_code == "project-context-v1" for item in records)
                assert all("zh-CN" not in str(item.description) for item in records)
        await key_service.revoke(project_id, active_key.info.id, user)
    finally:
        if app is not None:
            await app.state.container.close()
        async with factory.begin() as session:
            if trace_ids:
                await session.execute(
                    delete(GatewayRequest).where(GatewayRequest.trace_id.in_(trace_ids))
                )
            await session.execute(delete(Project).where(Project.id.in_(project_ids)))
            await session.execute(delete(User).where(User.id == owner_id))
        await engine.dispose()
