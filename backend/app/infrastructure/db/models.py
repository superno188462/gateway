"""应用数据库模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy 声明式模型基类。"""


class User(Base):
    """系统用户；权限由 role 标签决定。"""

    __tablename__ = "users"
    __table_args__ = (
        Index(
            "uq_users_single_admin",
            "role",
            unique=True,
            postgresql_where=text("role = 'admin'"),
        ),
        CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AuthSession(Base):
    """访问令牌会话，用于支持主动退出和令牌撤销。"""

    __tablename__ = "auth_sessions"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserServiceQuota(Base):
    """管理员授予用户的服务级月度 token 总上限。"""

    __tablename__ = "user_service_quotas"
    __table_args__ = (
        CheckConstraint("monthly_token_limit > 0", name="ck_user_service_quotas_limit"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    service_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    monthly_token_limit: Mapped[int] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Project(Base):
    """Agent 项目。"""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_projects_status"),
        CheckConstraint("visibility IN ('public', 'private')", name="ck_projects_visibility"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default="private", server_default="private"
    )
    owner_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    project_tags: Mapped[list[ProjectTag]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def tags(self) -> list[str]:
        """返回规范化后的项目标签，用于 API 响应。"""
        return sorted(project_tag.tag for project_tag in self.project_tags)


class ProjectTag(Base):
    """项目自定义标签；同一项目内标签唯一。"""

    __tablename__ = "project_tags"
    __table_args__ = (
        CheckConstraint("char_length(tag) BETWEEN 1 AND 20", name="ck_project_tags_length"),
        Index("ix_project_tags_tag", "tag"),
    )

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag: Mapped[str] = mapped_column(String(20), primary_key=True)
    project: Mapped[Project] = relationship(back_populates="project_tags")


class ProjectMember(Base):
    """用户与项目的授权关系。"""

    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="ck_project_members_role"),
        Index("ix_project_members_user_id", "user_id"),
    )

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ApiKey(Base):
    """项目 API Key；校验摘要与可逆密文分别保存，密钥不以明文落库。"""

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'revoked')", name="ck_api_keys_status"),
        Index("ix_api_keys_project_created_at", "project_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    encrypted_secret: Mapped[str | None] = mapped_column(String(256))
    key_prefix: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    key_last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProjectServiceSubscription(Base):
    """项目已开通的服务及 owner 分配给该项目的月度上限。"""

    __tablename__ = "project_service_subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended')", name="ck_service_subscriptions_status"
        ),
        CheckConstraint("monthly_token_limit > 0", name="ck_service_subscriptions_token_limit"),
    )

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    service_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    monthly_token_limit: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ServiceUsageBucket(Base):
    """按项目、服务和 UTC 月份记录已消费及预留 token。"""

    __tablename__ = "service_usage_buckets"

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    service_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    tokens_used: Mapped[int] = mapped_column(nullable=False, server_default="0")
    tokens_reserved: Mapped[int] = mapped_column(nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GatewayRequest(Base):
    """不含提示词或回复正文的网关调用记录。"""

    __tablename__ = "gateway_requests"
    __table_args__ = (
        Index("ix_gateway_requests_project_created", "project_id", "created_at"),
        Index("ix_gateway_requests_key_created", "api_key_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    project_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    api_key_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    service_code: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    total_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LlmProviderConfig(Base):
    """管理员配置的 OpenAI 兼容供应商连接信息；API Key 仅保存密文。"""

    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_llm_provider_configs_status"),
        Index("ix_llm_provider_configs_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_success: Mapped[bool | None] = mapped_column()
    last_test_message: Mapped[str | None] = mapped_column(String(250))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LlmModelConfig(Base):
    """网关公开模型名到供应商上游模型名的映射。"""

    __tablename__ = "llm_model_configs"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_llm_model_configs_status"),
        Index("ix_llm_model_configs_provider_id", "provider_id"),
        Index("ix_llm_model_configs_model_code_status", "model_code", "status"),
        UniqueConstraint("provider_id", "model_code", name="uq_llm_model_configs_provider_model"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("llm_provider_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_code: Mapped[str] = mapped_column(String(100), nullable=False)
    upstream_model: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
