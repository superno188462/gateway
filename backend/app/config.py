"""类型化应用配置及启动前校验。"""

from enum import StrEnum

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """应用运行环境。"""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """从环境变量读取的应用配置。"""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    database_url: str = Field(description="SQLAlchemy 异步 PostgreSQL 连接串。")
    log_level: str = "INFO"
    admin_username: str | None = None
    admin_password: str | None = None
    jwt_secret_key: str = Field(min_length=32)
    api_key_secret_key: SecretStr | None = Field(
        default=None,
        min_length=32,
        description="用于 API Key 摘要和密文加解密的主密钥；未配置时禁用 Key 功能。",
    )
    default_llm_monthly_token_limit: int = Field(
        default=100_000,
        ge=0,
        le=2_147_483_647,
        description="新注册用户默认 LLM 月 token 上限；0 表示不自动授予。",
    )
    jwt_access_token_expire_minutes: int = Field(default=30, ge=5, le=1440)

    @model_validator(mode="after")
    def validate_admin_pair(self) -> "Settings":
        """管理员用户名和密码必须同时配置或同时缺省。"""
        configured = (self.admin_username is not None, self.admin_password is not None)
        if configured == (True, False) or configured == (False, True):
            raise ValueError("ADMIN_USERNAME 和 ADMIN_PASSWORD 必须同时配置或同时缺省")
        if self.admin_username is not None and not self.admin_username.strip():
            raise ValueError("ADMIN_USERNAME 不能为空")
        if self.admin_password is not None and len(self.admin_password) < 8:
            raise ValueError("ADMIN_PASSWORD 长度不能少于 8 位")
        return self

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """拒绝同步驱动及非 PostgreSQL 数据库。"""
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL 必须使用 postgresql+asyncpg://")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """规范并校验日志级别。"""
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL 不是支持的日志级别")
        return normalized
