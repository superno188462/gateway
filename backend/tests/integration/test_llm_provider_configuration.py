"""供应商配置写库加密、模型路由解析和清理。"""

import os
import secrets
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.infrastructure.db.models import LlmProviderConfig
from app.services.llm.configuration import LlmConfigurationService
from app.services.llm.secrets import ProviderSecretCipher

pytestmark = pytest.mark.integration


async def test_provider_credentials_are_encrypted_and_model_is_forwarded_directly() -> None:
    database_url = os.getenv("TEST_DATABASE_URL") or Settings().database_url
    settings = Settings()
    assert settings.llm_provider_secret_key is not None
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = LlmConfigurationService(
        factory,
        ProviderSecretCipher(settings.llm_provider_secret_key.get_secret_value()),
    )
    provider_ids: list[UUID] = []
    raw_api_key = f"test-key-{secrets.token_urlsafe(16)}"
    model_code = f"test-model-{secrets.token_hex(6)}"
    try:
        provider = await service.create_provider(
            f"test-provider-{secrets.token_hex(6)}",
            "https://llm.example.test/v1",
            raw_api_key,
            "test-volc",
        )
        provider_ids.append(provider.id)
        second_provider = await service.create_provider(
            f"test-provider-{secrets.token_hex(6)}",
            "https://llm-backup.example.test/v1",
            f"backup-key-{secrets.token_urlsafe(16)}",
        )
        provider_ids.append(second_provider.id)
        resolved_pool = await service.resolve_model_pool(model_code)
        routed_pool = await service.resolve_model_pool(f"test-volc/{model_code}")
        assert await service.supports_model(f"test-volc/{model_code}") is True
        assert await service.supports_model(f"unknown-prefix/{model_code}") is False
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []}))
        ) as http_client:
            test_result = await service.test_provider(provider.id, model_code, http_client)

        async with factory() as session:
            row = await session.get(LlmProviderConfig, provider.id)
            assert row is not None
            assert raw_api_key not in row.encrypted_api_key

        listed = await service.list_providers()
        configured = next(item for item in listed if item.id == provider.id)
        assert configured.api_key_configured is True
        assert configured.last_test_success is True
        assert configured.last_test_message == "连接成功"
        assert test_result.success is True
        assert configured.api_key == raw_api_key
        assert raw_api_key not in repr(configured)
        assert configured.route_prefix == "test-volc"
        assert len(resolved_pool) >= 2
        assert {item.upstream_model for item in resolved_pool} == {model_code}
        assert [item.base_url for item in routed_pool] == ["https://llm.example.test/v1"]
        assert routed_pool[0].upstream_model == model_code
        assert [item.base_url for item in resolved_pool] == [
            f"{item.base_url}" for item in listed if item.status == "active"
        ]
        assert configured.priority == 100
        assert raw_api_key in {item.api_key for item in resolved_pool}
        assert model_code not in await service.active_model_codes()
    finally:
        for provider_id in provider_ids:
            await service.delete_provider(provider_id)
        await engine.dispose()
