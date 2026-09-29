"""供应商凭据的服务端加密与解密。"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class ProviderSecretError(RuntimeError):
    """无法使用当前服务端主密钥解密供应商凭据。"""


class ProviderSecretCipher:
    """使用独立 Fernet 密钥加密供应商 API Key。"""

    def __init__(self, secret_key: str) -> None:
        derived = hashlib.sha256(b"llm-provider-secret-v1:" + secret_key.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, value: str) -> str:
        """加密上游凭据，返回适合写入数据库的 ASCII 密文。"""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        """解密上游凭据；密钥不匹配时返回受控错误。"""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as error:
            raise ProviderSecretError(
                "无法解密供应商 API Key，请检查 LLM_PROVIDER_SECRET_KEY"
            ) from error
