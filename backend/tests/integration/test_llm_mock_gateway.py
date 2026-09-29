"""A4 LLM Mock 闭环集成测试；仅清理本测试创建的独立数据。"""

import os
import secrets
from uuid import UUID, uuid4

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
    LlmRequestUsage,
    Project,
    ProjectMember,
    ServiceUsageBucket,
    User,
)
from app.security import JwtService

pytestmark = pytest.mark.integration


def database_url() -> str:
    """优先使用隔离库；开发配置数据库只做随机数据的增量操作。"""
    return os.getenv("TEST_DATABASE_URL") or Settings().database_url


async def test_llm_mock_access_chat_stream_quota_and_upgrade(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, project_id, second_project_id = uuid4(), uuid4(), uuid4()
    suffix = secrets.token_hex(8)
    owner = User(
        id=owner_id, username=f"test_a4_owner_{suffix}", password_hash="unused", role="user"
    )
    app = None
    default_user_id: UUID | None = None
    admin_session_jti = None
    invalid_request_id: str | None = None
    invalid_route_request_id: str | None = None
    try:
        async with factory.begin() as session:
            session.add(owner)
            await session.flush()
            session.add(
                Project(
                    id=project_id,
                    name=f"A4 test {suffix}",
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
                    Project(
                        id=second_project_id,
                        name=f"A4 second project {suffix}",
                        description=None,
                        status="active",
                        visibility="private",
                        owner_id=owner_id,
                    ),
                ]
            )
            await session.flush()
            session.add(ProjectMember(project_id=second_project_id, user_id=owner_id, role="owner"))

        jwt_service = JwtService("a4-integration-jwt-secret-key-32-characters-long", 30)
        token, jti, expires_at = jwt_service.issue(str(owner_id), "user")
        async with factory.begin() as session:
            session.add(AuthSession(jti=jti, user_id=owner_id, expires_at=expires_at))

        api_secret = "a4-integration-api-key-encryption-secret-32"
        key_service = ApiKeyService(factory, api_secret)
        created = await key_service.create(project_id, owner, "A4 integration", None)
        app = create_app(
            settings=Settings(
                database_url=database_url(),
                jwt_secret_key="a4-integration-jwt-secret-key-32-characters-long",
                api_key_secret_key=api_secret,
                _env_file=None,
            )
        )
        default_registration = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        async with default_registration as register_client:
            registered = await register_client.post(
                "/api/v1/auth/register",
                json={"username": f"test_a4_default_{suffix}", "password": "test-password-123"},
            )
            assert registered.status_code == 201
            default_user_id = UUID(registered.json()["id"])
        assert default_user_id is not None
        default_token, default_jti, default_expires = jwt_service.issue(
            str(default_user_id), "user"
        )
        async with factory.begin() as session:
            session.add(
                AuthSession(jti=default_jti, user_id=default_user_id, expires_at=default_expires)
            )
            admin_id = await session.scalar(select(User.id).where(User.role == "admin"))
            admin_token = None
            if admin_id is not None:
                admin_token, admin_session_jti, admin_expires = jwt_service.issue(
                    str(admin_id), "admin"
                )
                session.add(
                    AuthSession(
                        jti=admin_session_jti,
                        user_id=admin_id,
                        expires_at=admin_expires,
                    )
                )
        assert app.state.container.service_management is not None
        if admin_token is None:
            await app.state.container.service_management.set_user_limit(
                owner_id, "mock-llm-v1", 120_000
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            jwt_headers = {"Authorization": f"Bearer {token}"}
            default_headers = {"Authorization": f"Bearer {default_token}"}
            if admin_token is not None:
                admin_headers = {"Authorization": f"Bearer {admin_token}"}
                admin_grant = await client.put(
                    f"/api/admin/v1/users/{owner_id}/services/mock-llm-v1",
                    headers=admin_headers,
                    json={"monthly_token_limit": 120_000},
                )
                assert admin_grant.status_code == 200
                by_username = await client.get(
                    "/api/admin/v1/users/lookup",
                    headers=admin_headers,
                    params={"username": owner.username},
                )
                assert by_username.status_code == 200
                assert by_username.json()["id"] == str(owner_id)
                by_id = await client.get(
                    "/api/admin/v1/users/lookup",
                    headers=admin_headers,
                    params={"user_id": str(owner_id)},
                )
                assert by_id.status_code == 200
                assert by_id.json()["username"] == owner.username
                target_quotas = await client.get(
                    f"/api/admin/v1/users/{owner_id}/services", headers=admin_headers
                )
                assert target_quotas.status_code == 200
                assert target_quotas.json()[0]["monthly_token_limit"] == 120_000
                invalid_lookup = await client.get(
                    "/api/admin/v1/users/lookup",
                    headers=admin_headers,
                    params={"username": owner.username, "user_id": str(owner_id)},
                )
                assert invalid_lookup.status_code == 422
                forbidden_lookup = await client.get(
                    "/api/admin/v1/users/lookup",
                    headers=jwt_headers,
                    params={"username": owner.username},
                )
                assert forbidden_lookup.status_code == 403
                unknown_user = await client.get(
                    "/api/admin/v1/users/lookup",
                    headers=admin_headers,
                    params={"username": f"missing_{suffix}"},
                )
                assert unknown_user.status_code == 404
                forbidden_quota_read = await client.get(
                    f"/api/admin/v1/users/{owner_id}/services", headers=jwt_headers
                )
                assert forbidden_quota_read.status_code == 403
            default_capabilities = await client.get("/api/v1/me/services", headers=default_headers)
            assert default_capabilities.status_code == 200
            assert default_capabilities.json()[0]["monthly_token_limit"] == 100_000
            api_headers = {"Authorization": f"Bearer {created.secret}"}
            body = {
                "model": "mock-chat",
                "messages": [{"role": "user", "content": "请介绍一下网关"}],
                "max_tokens": 80,
            }

            denied = await client.post("/v1/chat/completions", headers=api_headers, json=body)
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "service_not_enabled"

            catalog = await client.get("/api/admin/v1/services", headers=jwt_headers)
            assert catalog.status_code == 200
            assert "mock-chat" in catalog.json()[0]["models"]
            personal = await client.get("/api/v1/me/services", headers=jwt_headers)
            assert personal.status_code == 200
            assert personal.json()[0]["monthly_token_limit"] == 120_000
            assert personal.json()[0]["available_tokens"] == 120_000
            applied = await client.post(
                f"/api/admin/v1/projects/{project_id}/services",
                headers=jwt_headers,
                json={"service_code": "mock-llm-v1", "monthly_token_limit": 100_000},
            )
            assert applied.status_code == 201
            assert applied.json()["monthly_token_limit"] == 100_000
            invalid_route = await client.post(
                "/v1/chat/completions",
                headers=api_headers,
                json={**body, "model": f"missing-{suffix}/no-such-model"},
            )
            assert invalid_route.status_code == 404
            invalid_route_request_id = invalid_route.headers["x-request-id"]
            assert invalid_route.headers["x-trace-id"] == invalid_route_request_id
            assert any(
                f"request_id={invalid_route_request_id}" in record.getMessage()
                and "stage=routing result=failed" in record.getMessage()
                for record in caplog.records
            )
            second_applied = await client.post(
                f"/api/admin/v1/projects/{second_project_id}/services",
                headers=jwt_headers,
                json={"service_code": "mock-llm-v1", "monthly_token_limit": 20_000},
            )
            assert second_applied.status_code == 201
            allocated = await client.get("/api/v1/me/services", headers=jwt_headers)
            assert allocated.json()[0]["allocated_tokens"] == 120_000
            assert allocated.json()[0]["available_tokens"] == 0
            if admin_token is not None:
                reduced_user_limit = await client.put(
                    f"/api/admin/v1/users/{owner_id}/services/mock-llm-v1",
                    headers=admin_headers,
                    json={"monthly_token_limit": 100_000},
                )
                assert reduced_user_limit.status_code == 200
                assert reduced_user_limit.json()["monthly_token_limit"] == 100_000
                assert reduced_user_limit.json()["allocated_tokens"] == 120_000
                assert reduced_user_limit.json()["available_tokens"] == 0
                revoked_user_limit = await client.put(
                    f"/api/admin/v1/users/{owner_id}/services/mock-llm-v1",
                    headers=admin_headers,
                    json={"monthly_token_limit": 0},
                )
                assert revoked_user_limit.status_code == 200
                assert revoked_user_limit.json()["monthly_token_limit"] == 0
                assert revoked_user_limit.json()["allocated_tokens"] == 120_000
                owner_capabilities = await client.get("/api/v1/me/services", headers=jwt_headers)
                assert owner_capabilities.status_code == 200
                assert owner_capabilities.json()[0]["monthly_token_limit"] == 0
                assert owner_capabilities.json()[0]["allocated_tokens"] == 120_000
                paused_call = await client.post(
                    "/v1/chat/completions", headers=api_headers, json=body
                )
                assert paused_call.status_code == 403
                assert paused_call.json()["error"]["code"] == "user_quota_missing"
                restored_user_limit = await client.put(
                    f"/api/admin/v1/users/{owner_id}/services/mock-llm-v1",
                    headers=admin_headers,
                    json={"monthly_token_limit": 120_000},
                )
                assert restored_user_limit.status_code == 200
            over_allocation = await client.patch(
                f"/api/admin/v1/projects/{project_id}/services/mock-llm-v1",
                headers=jwt_headers,
                json={"monthly_token_limit": 100_001},
            )
            assert over_allocation.status_code == 409
            second_reduced = await client.patch(
                f"/api/admin/v1/projects/{second_project_id}/services/mock-llm-v1",
                headers=jwt_headers,
                json={"monthly_token_limit": 10_000},
            )
            assert second_reduced.status_code == 200
            admin_only = await client.put(
                f"/api/admin/v1/users/{owner_id}/services/mock-llm-v1",
                headers=jwt_headers,
                json={"monthly_token_limit": 150_000},
            )
            assert admin_only.status_code == 403

            response = await client.post("/v1/chat/completions", headers=api_headers, json=body)
            assert response.status_code == 200
            result = response.json()
            assert result["model"] == "mock-chat"
            assert result["choices"][0]["message"]["content"].startswith("[Mock LLM]")
            assert result["usage"]["total_tokens"] > 0
            assert response.headers["x-request-id"] == result["id"]
            assert response.headers["x-trace-id"] == response.headers["x-request-id"]
            successful_request_id = response.headers["x-request-id"]

            streaming = await client.post(
                "/v1/chat/completions",
                headers=api_headers,
                json={**body, "stream": True, "stream_options": {"include_usage": True}},
            )
            assert streaming.status_code == 200
            assert "text/event-stream" in streaming.headers["content-type"]
            assert "data: [DONE]" in streaming.text
            assert '"usage"' in streaming.text

            async with factory.begin() as session:
                bucket = await session.scalar(
                    select(ServiceUsageBucket).where(
                        ServiceUsageBucket.project_id == project_id,
                        ServiceUsageBucket.service_code == "mock-llm-v1",
                    )
                )
                assert bucket is not None
                bucket.tokens_used = 100_000
            over_quota = await client.post("/v1/chat/completions", headers=api_headers, json=body)
            assert over_quota.status_code == 429
            assert over_quota.json()["error"]["code"] == "project_quota_exceeded"
            assert any(
                "stage=quota result=denied" in record.getMessage()
                and '"decision": "project_quota_exceeded"' in record.getMessage()
                and '"project_monthly_limit": 100000' in record.getMessage()
                for record in caplog.records
            )

            upgraded = await client.patch(
                f"/api/admin/v1/projects/{project_id}/services/mock-llm-v1",
                headers=jwt_headers,
                json={"monthly_token_limit": 110_000},
            )
            assert upgraded.status_code == 200
            assert upgraded.json()["monthly_token_limit"] == 110_000
            after_upgrade = await client.post(
                "/v1/chat/completions", headers=api_headers, json=body
            )
            assert after_upgrade.status_code == 200

            invalid = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer invalid-key"},
                json=body,
            )
            assert invalid.status_code == 401
            invalid_request_id = invalid.headers["x-request-id"]

            service_list = await client.get(
                f"/api/admin/v1/projects/{project_id}/services", headers=jwt_headers
            )
            assert service_list.json()[0]["tokens_used"] > 100_000

        async with factory() as session:
            logs = list(
                (
                    await session.scalars(
                        select(GatewayRequest).where(GatewayRequest.project_id == project_id)
                    )
                ).all()
            )
            assert any(log.error_code == "service_not_enabled" for log in logs)
            assert any(log.error_code == "project_quota_exceeded" for log in logs)
            assert all(log.api_key_id == created.info.id for log in logs)
            successful_log = next(log for log in logs if log.request_id == successful_request_id)
            denied_log = next(log for log in logs if log.error_code == "service_not_enabled")
            assert denied_log.audit_result == "denied"
            assert denied_log.error_message == "项目尚未申请 LLM Mock 服务"
            assert denied_log.audit_steps["quota"] == {
                "result": "denied",
                "error_code": "service_not_enabled",
            }
            assert denied_log.status == "denied"
            assert successful_log.trace_id == successful_request_id
            assert successful_log.audit_result == "allowed"
            assert successful_log.audit_steps == {
                "authentication": {"result": "allowed"},
                "quota": {"result": "allowed"},
                "routing": {"result": "allowed"},
                "service_call": {"result": "succeeded"},
            }
            successful_usage = await session.get(LlmRequestUsage, successful_request_id)
            assert successful_usage is not None
            assert successful_usage.model == "mock-chat"
            assert successful_usage.total_tokens > 0
            assert successful_usage.finish_reason == "stop"
            assert "消耗" in (successful_log.description or "")
            assert successful_log.status == "succeeded"
            assert not {"prompt", "messages", "response", "content"} & set(
                GatewayRequest.__table__.columns.keys()
            )
            invalid_log = await session.scalar(
                select(GatewayRequest).where(GatewayRequest.request_id == invalid_request_id)
            )
            assert invalid_log is not None
            assert invalid_log.project_id is None
            assert invalid_log.api_key_id is None
            assert invalid_log.audit_steps["authentication"] == {
                "result": "denied",
                "error_code": "invalid_api_key",
            }
            routing_log = await session.scalar(
                select(GatewayRequest).where(GatewayRequest.request_id == invalid_route_request_id)
            )
            assert routing_log is not None
            authentication_stage = routing_log.audit_steps["authentication"]
            routing_stage = routing_log.audit_steps["routing"]
            assert isinstance(authentication_stage, dict)
            assert authentication_stage.get("result") == "allowed"
            assert isinstance(routing_stage, dict)
            assert routing_stage.get("error_code") == "model_not_found"
            assert "quota" not in routing_log.audit_steps
            assert "service_call" not in routing_log.audit_steps
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            project_logs = await client.get(
                f"/api/v1/projects/{project_id}/requests", headers=jwt_headers
            )
            assert project_logs.status_code == 200
            assert project_logs.json()["page"] == 1
            assert project_logs.json()["page_size"] == 50
            assert project_logs.json()["total_count"] >= 2
            assert project_logs.json()["total_pages"] >= 1
            assert successful_request_id in {
                item["request_id"] for item in project_logs.json()["items"]
            }
            first_numbered_page = await client.get(
                f"/api/v1/projects/{project_id}/requests",
                headers=jwt_headers,
                params={"page": 1, "page_size": 1},
            )
            second_numbered_page = await client.get(
                f"/api/v1/projects/{project_id}/requests",
                headers=jwt_headers,
                params={"page": 2, "page_size": 1},
            )
            assert first_numbered_page.status_code == second_numbered_page.status_code == 200
            assert (
                first_numbered_page.json()["items"][0]["request_id"]
                != (second_numbered_page.json()["items"][0]["request_id"])
            )
            assert second_numbered_page.json()["page"] == 2
            successful_detail = await client.get(
                f"/api/v1/projects/{project_id}/requests/{successful_request_id}",
                headers=jwt_headers,
            )
            assert successful_detail.status_code == 200
            assert successful_detail.json()["trace_id"] == successful_request_id
            assert not {
                "audit_result",
                "audit_steps",
                "error_details",
            } & set(successful_detail.json())
            assert successful_detail.json()["description"]
            assert "model" not in successful_detail.json()
            assert "total_tokens" not in successful_detail.json()
            route_detail = await client.get(
                f"/api/v1/projects/{project_id}/requests/{invalid_route_request_id}",
                headers=jwt_headers,
            )
            assert route_detail.status_code == 200
            assert route_detail.json()["error_code"] == "model_not_found"
            assert route_detail.json()["error_message"] == (
                "请求的模型不存在，或当前没有可用的上游路由"
            )
            assert not {
                "audit_result",
                "audit_steps",
                "error_details",
            } & set(route_detail.json())
            forbidden_global_logs = await client.get("/api/admin/v1/requests", headers=jwt_headers)
            assert forbidden_global_logs.status_code == 403
            forbidden_system_logs = await client.get(
                "/api/admin/v1/system-logs", headers=jwt_headers
            )
            assert forbidden_system_logs.status_code == 403
            if admin_token is not None:
                system_logs = await client.get("/api/admin/v1/system-logs", headers=admin_headers)
                assert system_logs.status_code == 200
                assert "entries" in system_logs.json()
                all_logs = await client.get("/api/admin/v1/requests", headers=admin_headers)
                assert all_logs.status_code == 200
                assert invalid_request_id in {
                    item["request_id"] for item in all_logs.json()["items"]
                }
                global_logs = await client.get(
                    "/api/admin/v1/requests",
                    headers=admin_headers,
                    params={"project_id": str(project_id)},
                )
                assert global_logs.status_code == 200
                assert all(
                    item["project_id"] == str(project_id) for item in global_logs.json()["items"]
                )
    finally:
        if app is not None:
            await app.state.container.close()
        async with factory.begin() as session:
            await session.execute(
                delete(GatewayRequest).where(GatewayRequest.project_id == project_id)
            )
            await session.execute(
                delete(GatewayRequest).where(GatewayRequest.project_id == second_project_id)
            )
            if invalid_request_id is not None:
                await session.execute(
                    delete(GatewayRequest).where(GatewayRequest.request_id == invalid_request_id)
                )
            if admin_session_jti is not None:
                await session.execute(
                    delete(AuthSession).where(AuthSession.jti == admin_session_jti)
                )
            await session.execute(
                delete(Project).where(Project.id.in_([project_id, second_project_id]))
            )
            await session.execute(delete(User).where(User.id == owner_id))
            if default_user_id is not None:
                await session.execute(delete(User).where(User.id == default_user_id))
        await engine.dispose()
