"""通过 HTTP 验证 LLM 供应商管理员 API 和连通性测试操作。"""

import json
import os
import secrets
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bootstrap import create_app
from app.config import Settings
from app.infrastructure.db.models import AuthSession, User
from app.security import JwtService

pytestmark = pytest.mark.integration


async def test_provider_admin_http_endpoints_and_permissions() -> None:
    database_url = os.getenv("TEST_DATABASE_URL") or Settings().database_url
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    jwt_secret = "llm-provider-admin-api-test-jwt-secret-32chars"
    provider_secret = "llm-provider-admin-api-test-encryption-secret"
    settings = Settings(
        database_url=database_url,
        jwt_secret_key=jwt_secret,
        llm_provider_secret_key=provider_secret,
        _env_file=None,
    )
    async with factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
    assert admin is not None

    jwt_service = JwtService(jwt_secret, 30)
    admin_token, admin_jti, admin_expiry = jwt_service.issue(str(admin.id), "admin")
    ordinary_user = User(
        id=uuid4(),
        username=f"test_provider_api_{secrets.token_hex(6)}",
        password_hash="unused",
        role="user",
    )
    user_token, user_jti, user_expiry = jwt_service.issue(str(ordinary_user.id), "user")
    async with factory.begin() as session:
        session.add(ordinary_user)
        await session.flush()
        session.add_all(
            [
                AuthSession(jti=admin_jti, user_id=admin.id, expires_at=admin_expiry),
                AuthSession(jti=user_jti, user_id=ordinary_user.id, expires_at=user_expiry),
            ]
        )

    app = create_app(settings=settings)
    container = app.state.container
    original_http_client = container.http_client
    requests: list[str] = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.url.path.endswith("/chat/completions")
        assert json.loads(request.read())["model"] == "vendor-model-a"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    container.http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    if (
        container.llm_gateway_service is not None
        and container.llm_configuration_service is not None
    ):
        from app.services.llm.application import LlmGatewayService
        from app.services.llm.providers.openai_compatible import ConfiguredLlmProvider

        assert container.session_factory is not None
        container.llm_gateway_service = LlmGatewayService(
            container.session_factory,
            ConfiguredLlmProvider(container.llm_configuration_service, container.http_client),
        )

    created_provider_ids: list[UUID] = []
    try:
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/admin/v1/llm/providers")).status_code == 401
            forbidden = await client.get(
                "/api/admin/v1/llm/providers",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            assert forbidden.status_code == 403

            first = await client.post(
                "/api/admin/v1/llm/providers",
                headers=admin_headers,
                json={
                    "name": f"api-test-a-{secrets.token_hex(5)}",
                    "base_url": "https://provider-a.example.test/v1",
                    "api_key": "secret-provider-a",
                },
            )
            assert first.status_code == 201
            assert first.json()["api_key_configured"] is True
            assert "secret-provider-a" not in first.text
            assert "api_key" not in first.json()
            first_id = UUID(first.json()["id"])
            created_provider_ids.append(first_id)

            public_model = f"pool-api-test-{secrets.token_hex(5)}"
            first_model = await client.post(
                f"/api/admin/v1/llm/providers/{first_id}/models",
                headers=admin_headers,
                json={"model_code": public_model, "upstream_model": "vendor-model-a"},
            )
            assert first_model.status_code == 201

            tested = await client.post(
                f"/api/admin/v1/llm/providers/{first_id}/test", headers=admin_headers
            )
            assert tested.status_code == 200
            assert tested.json()["success"] is True
            assert tested.json()["message"] == "连接成功"

            second = await client.post(
                "/api/admin/v1/llm/providers",
                headers=admin_headers,
                json={
                    "name": f"api-test-b-{secrets.token_hex(5)}",
                    "base_url": "https://provider-b.example.test/v1",
                    "api_key": "secret-provider-b",
                },
            )
            assert second.status_code == 201
            second_id = UUID(second.json()["id"])
            created_provider_ids.append(second_id)
            second_model = await client.post(
                f"/api/admin/v1/llm/providers/{second_id}/models",
                headers=admin_headers,
                json={"model_code": public_model, "upstream_model": "vendor-model-b"},
            )
            assert second_model.status_code == 201
            models = await client.get("/api/v1/llm/models")
            assert models.status_code == 200
            assert models.json().count(public_model) == 1

            providers = await client.get("/api/admin/v1/llm/providers", headers=admin_headers)
            assert providers.status_code == 200
            first_info = next(item for item in providers.json() if item["id"] == str(first_id))
            assert first_info["last_test_success"] is True
            assert "secret-provider-a" not in providers.text
            assert requests == ["/v1/chat/completions"]
    finally:
        if container.llm_configuration_service is not None:
            for provider_id in created_provider_ids:
                await container.llm_configuration_service.delete_provider(provider_id)
        async with factory.begin() as session:
            await session.execute(
                delete(AuthSession).where(AuthSession.jti.in_([admin_jti, user_jti]))
            )
            await session.execute(delete(User).where(User.id == ordinary_user.id))
        await container.close()
        if original_http_client is not None:
            await original_http_client.aclose()
        await engine.dispose()
