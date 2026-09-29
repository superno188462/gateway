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


async def test_provider_credentials_are_encrypted_and_model_can_be_resolved() -> None:
    database_url = os.getenv("TEST_DATABASE_URL") or Settings().database_url
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = LlmConfigurationService(
        factory,
        ProviderSecretCipher("test-llm-provider-secret-key-long-enough"),
    )
    provider_ids: list[UUID] = []
    raw_api_key = f"test-key-{secrets.token_urlsafe(16)}"
    model_code = f"test-model-{secrets.token_hex(6)}"
    try:
        provider = await service.create_provider(
            f"test-provider-{secrets.token_hex(6)}",
            "https://llm.example.test/v1",
            raw_api_key,
        )
        provider_ids.append(provider.id)
        model = await service.create_model(provider.id, model_code, "upstream-test-model")
        second_provider = await service.create_provider(
            f"test-provider-{secrets.token_hex(6)}",
            "https://llm-backup.example.test/v1",
            f"backup-key-{secrets.token_urlsafe(16)}",
        )
        provider_ids.append(second_provider.id)
        await service.create_model(second_provider.id, model_code, "upstream-backup-model")
        resolved_pool = await service.resolve_model_pool(model.model_code)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []}))
        ) as http_client:
            test_result = await service.test_provider(provider.id, http_client)

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
        assert raw_api_key not in repr(configured)
        assert len(resolved_pool) == 2
        assert {item.upstream_model for item in resolved_pool} == {
            "upstream-test-model",
            "upstream-backup-model",
        }
        assert raw_api_key in {item.api_key for item in resolved_pool}
        assert model.model_code in await service.active_model_codes()
    finally:
        for provider_id in provider_ids:
            await service.delete_provider(provider_id)
        await engine.dispose()
