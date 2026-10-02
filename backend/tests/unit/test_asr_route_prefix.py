from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.asr.admin_api import ProviderPayload, ProviderUpdatePayload
from app.services.asr.configuration import AsrConfigurationService
from app.services.llm.secrets import ProviderSecretCipher


class FakeFactory:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.statement = None

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def scalars(self, statement):
        self.statement = statement
        return self.rows


def test_asr_admin_configuration_only_allows_volc_prefix() -> None:
    payload = ProviderPayload(
        name="primary",
        resource_id="volc.seedasr.sauc.duration",
        file_transcription_url="wss://asr.example.test/nostream",
        realtime_url="wss://asr.example.test/async",
        api_key="secret",
    )
    assert payload.route_prefix == "volc"
    assert payload.supplier_name == "火山引擎"

    with pytest.raises(ValidationError):
        ProviderPayload(
            name="unsupported",
            supplier_name="Other",
            route_prefix="other",
            resource_id="volc.seedasr.sauc.duration",
            file_transcription_url="https://asr.example.test/v1",
            realtime_url="wss://asr.example.test/async",
            api_key="secret",
        )

    with pytest.raises(ValidationError):
        ProviderUpdatePayload(route_prefix="other")


@pytest.mark.asyncio
async def test_asr_model_routes_by_prefix_without_configured_model_name() -> None:
    cipher = ProviderSecretCipher("test-secret-key")
    api_key = "asr-test-key"
    row = SimpleNamespace(
        id=uuid4(),
        name="豆包语音",
        supplier_name="火山引擎",
        route_prefix="volc",
        resource_id="volc.seedasr.sauc.duration",
        file_transcription_url="wss://asr.example.test/nostream",
        realtime_url="wss://asr.example.test/async",
        status="active",
        priority=100,
        encrypted_api_key=cipher.encrypt(api_key),
        created_at=datetime.now(UTC),
    )
    factory = FakeFactory([row])
    service = AsrConfigurationService(factory, cipher)  # type: ignore[arg-type]

    routes = await service.resolve_pool("VOLC/doubao-seed-asr")

    assert len(routes) == 1
    assert routes[0].requested_model == "VOLC/doubao-seed-asr"
    assert routes[0].provider.api_key == api_key
    assert routes[0].provider.file_transcription_url == row.file_transcription_url
    assert routes[0].provider.realtime_url == row.realtime_url
    assert routes[0].provider.resource_id == row.resource_id
    assert "model_code" not in str(factory.statement)


@pytest.mark.asyncio
async def test_unknown_prefix_uses_generic_extension_without_volc_fallback() -> None:
    cipher = ProviderSecretCipher("test-secret-key")
    row = SimpleNamespace(
        id=uuid4(),
        name="默认语音",
        supplier_name="火山引擎",
        route_prefix=None,
        resource_id="volc.seedasr.sauc.duration",
        file_transcription_url="wss://asr.example.test/nostream",
        realtime_url="wss://asr.example.test/async",
        status="active",
        priority=100,
        encrypted_api_key=cipher.encrypt("asr-test-key"),
        created_at=datetime.now(UTC),
    )
    prefixed_row = SimpleNamespace(
        id=uuid4(),
        name="带前缀语音",
        supplier_name="火山引擎",
        route_prefix="other",
        resource_id="volc.seedasr.sauc.duration",
        file_transcription_url="wss://asr-prefixed.example.test/nostream",
        realtime_url="wss://asr-prefixed.example.test/async",
        status="active",
        priority=100,
        encrypted_api_key=cipher.encrypt("another-key"),
        created_at=datetime.now(UTC),
    )
    service = AsrConfigurationService(FakeFactory([row, prefixed_row]), cipher)  # type: ignore[arg-type]

    routes = await service.resolve_pool("other/any-model-name")

    assert routes == ()


@pytest.mark.asyncio
async def test_unprefixed_public_model_routes_to_configured_volc_adapter() -> None:
    cipher = ProviderSecretCipher("test-secret-key")
    row = SimpleNamespace(
        id=uuid4(),
        name="豆包语音",
        supplier_name="火山引擎",
        route_prefix="volc",
        model_name="doubao-seed-asr-2.0",
        resource_id="volc.seedasr.sauc.duration",
        file_transcription_url="wss://asr.example.test/nostream",
        realtime_url="wss://asr.example.test/async",
        status="active",
        priority=100,
        encrypted_api_key=cipher.encrypt("asr-test-key"),
        created_at=datetime.now(UTC),
    )
    factory = FakeFactory([row])
    service = AsrConfigurationService(factory, cipher)  # type: ignore[arg-type]

    routes = await service.resolve_pool("doubao-seed-asr-2.0")

    assert len(routes) == 1
    assert routes[0].provider.api_key == "asr-test-key"
    assert factory.statement.compile().params["model_name_1"] == "doubao-seed-asr-2.0"
