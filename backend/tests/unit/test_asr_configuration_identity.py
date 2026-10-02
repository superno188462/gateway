from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.asr.configuration import AsrConfigurationError, AsrConfigurationService
from app.services.llm.secrets import ProviderSecretCipher


class FakeSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    async def scalars(self, _statement: object) -> list[SimpleNamespace]:
        return self.rows


@pytest.mark.asyncio
async def test_asr_api_key_cannot_be_reused_with_same_endpoints_in_another_group() -> None:
    cipher = ProviderSecretCipher("test-secret-key")
    api_key = "same-asr-key"
    row = SimpleNamespace(
        id=uuid4(),
        resource_id="volc.seedasr.sauc.duration",
        file_transcription_url="wss://asr.example.test/nostream",
        realtime_url="wss://asr.example.test/async",
        api_fingerprint=cipher.fingerprint(
            "volc.seedasr.sauc.duration\0wss://asr.example.test/nostream"
            "\0wss://asr.example.test/async",
            api_key,
        ),
        encrypted_api_key=cipher.encrypt(api_key),
    )
    service = AsrConfigurationService(None, cipher)  # type: ignore[arg-type]

    with pytest.raises(AsrConfigurationError, match="相同 Resource ID、端点和 API Key"):
        await service._ensure_api_unique(
            FakeSession([row]),
            "volc.seedasr.sauc.duration",
            "wss://asr.example.test/nostream/",
            "wss://asr.example.test/async",
            api_key,
        )


@pytest.mark.asyncio
async def test_same_asr_api_key_can_be_used_with_different_endpoints() -> None:
    cipher = ProviderSecretCipher("test-secret-key")
    api_key = "same-asr-key"
    row = SimpleNamespace(
        id=uuid4(),
        resource_id="volc.seedasr.sauc.duration",
        file_transcription_url="wss://asr-one.example.test/nostream",
        realtime_url="wss://asr-one.example.test/async",
        api_fingerprint=cipher.fingerprint(
            "volc.seedasr.sauc.duration\0wss://asr-one.example.test/nostream"
            "\0wss://asr-one.example.test/async",
            api_key,
        ),
        encrypted_api_key=cipher.encrypt(api_key),
    )
    service = AsrConfigurationService(None, cipher)  # type: ignore[arg-type]

    await service._ensure_api_unique(
        FakeSession([row]),
        "volc.seedasr.sauc.duration",
        "wss://asr-two.example.test/nostream",
        "wss://asr-two.example.test/async",
        api_key,
    )
