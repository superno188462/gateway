"""配置校验测试。"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_accepts_async_postgresql_url() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/db",
        jwt_secret_key="x" * 32,
        _env_file=None,
    )
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_rejects_sync_or_non_postgresql_url() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite:///gateway.db", jwt_secret_key="x" * 32, _env_file=None)


def test_normalizes_log_level() -> None:
    assert (
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost/db",
            log_level="warning",
            jwt_secret_key="x" * 32,
            _env_file=None,
        ).log_level
        == "WARNING"
    )


def test_admin_credentials_must_be_configured_together() -> None:
    with pytest.raises(ValidationError, match="必须同时配置"):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost/db",
            jwt_secret_key="x" * 32,
            admin_username="admin",
            _env_file=None,
        )


def test_admin_password_requires_minimum_length() -> None:
    with pytest.raises(ValidationError, match="长度不能少于"):
        Settings(
            database_url="postgresql+asyncpg://user:pass@localhost/db",
            jwt_secret_key="x" * 32,
            admin_username="admin",
            admin_password="short",
            _env_file=None,
        )
