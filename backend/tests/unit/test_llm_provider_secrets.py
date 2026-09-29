"""供应商 API Key 必须加密保存且不能依赖数据库密文直接回显。"""

import pytest

from app.services.llm.configuration import (
    LlmConfigurationService,
    LlmConfigurationValidationError,
)
from app.services.llm.secrets import ProviderSecretCipher, ProviderSecretError


def test_provider_secret_is_encrypted_and_round_trips() -> None:
    cipher = ProviderSecretCipher("provider-secret-key-at-least-32-characters")
    encrypted = cipher.encrypt("sk-vendor-secret")

    assert encrypted != "sk-vendor-secret"
    assert cipher.decrypt(encrypted) == "sk-vendor-secret"


def test_provider_secret_rejects_wrong_master_key() -> None:
    encrypted = ProviderSecretCipher("first-secret-key-at-least-32-characters").encrypt("secret")
    other_cipher = ProviderSecretCipher("other-secret-key-at-least-32-characters")

    with pytest.raises(ProviderSecretError):
        other_cipher.decrypt(encrypted)


def test_provider_base_url_requires_https_except_local_debug() -> None:
    assert LlmConfigurationService._normalize_base_url("https://api.example.test/v1/") == (
        "https://api.example.test/v1"
    )
    assert LlmConfigurationService._normalize_base_url("http://127.0.0.1:9000/v1") == (
        "http://127.0.0.1:9000/v1"
    )
    with pytest.raises(LlmConfigurationValidationError):
        LlmConfigurationService._normalize_base_url("http://api.example.test/v1")
