"""密码哈希和 JWT 访问令牌服务。"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
from pwdlib import PasswordHash


class PasswordService:
    """使用 Argon2 保存和验证密码哈希。"""

    def __init__(self) -> None:
        self._hasher = PasswordHash.recommended()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        return self._hasher.verify(password, password_hash)


class JwtService:
    """创建和解析管理员短期访问令牌。"""

    algorithm = "HS256"

    def __init__(self, secret_key: str, expires_minutes: int) -> None:
        self._secret_key = secret_key
        self.expires_minutes = expires_minutes

    def issue(self, user_id: str, role: str) -> tuple[str, str, datetime]:
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=self.expires_minutes)
        jti = str(uuid4())
        payload: dict[str, Any] = {
            "sub": user_id,
            "jti": jti,
            "role": role,
            "iat": now,
            "exp": expires_at,
        }
        return jwt.encode(payload, self._secret_key, algorithm=self.algorithm), jti, expires_at

    def decode(self, token: str) -> dict[str, Any]:
        return jwt.decode(token, self._secret_key, algorithms=[self.algorithm])
