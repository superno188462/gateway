"""应用数据库模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
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


class ProjectResource(Base):
    """项目内显式保存的提示词模板或记忆文档。"""

    __tablename__ = "resources"
    __table_args__ = (
        CheckConstraint(
            "(resource_type = 'memory' AND category IN ('sessions', 'profiles', 'longterm')) "
            "OR (resource_type = 'template' AND category IN ('system', 'user', 'assistant'))",
            name="ck_resources_fixed_directory",
        ),
        CheckConstraint("version >= 1", name="ck_resources_version"),
        Index(
            "uq_resources_active_name",
            "project_id",
            "resource_type",
            "category",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_resources_project_directory", "project_id", "resource_type", "category"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    updated_by: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProjectSessionMemory(Base):
    """按项目、外部用户、会话和键隔离的短期上下文值。"""

    __tablename__ = "project_session_memories"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "external_user_id",
            "session_id",
            "memory_key",
            name="uq_session_memory_key",
        ),
        Index("ix_session_memory_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    external_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    session_id: Mapped[str] = mapped_column(String(200), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(100), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProjectSessionMessage(Base):
    """按项目、外部用户和会话顺序保存的短期对话消息。"""

    __tablename__ = "project_session_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')", name="ck_session_messages_role"
        ),
        Index(
            "ix_session_messages_scope_sequence",
            "project_id",
            "external_user_id",
            "session_id",
            "sequence",
        ),
        Index("ix_session_messages_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    external_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    session_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProjectLongTermMemory(Base):
    """项目内以外部用户为边界的长期记忆条目。"""

    __tablename__ = "project_long_term_memories"
    __table_args__ = (
        Index("ix_long_term_memory_project_user", "project_id", "external_user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    external_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProjectUserProfile(Base):
    """项目内每个外部用户一份可版本化的结构化画像。"""

    __tablename__ = "project_user_profiles"

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    external_user_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    profile: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
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
    created_by_user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
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
        CheckConstraint(
            "monthly_token_limit IS NULL OR monthly_token_limit > 0",
            name="ck_service_subscriptions_token_limit",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    service_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    monthly_token_limit: Mapped[int | None] = mapped_column(nullable=True)
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
    __table_args__ = (
        Index(
            "ix_service_usage_buckets_service_period_project",
            "service_code",
            "period_start",
            "project_id",
        ),
    )

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
    """不含敏感正文的服务调用与项目操作记录。"""

    __tablename__ = "gateway_requests"
    __table_args__ = (
        Index("ix_gateway_requests_created_id", "created_at", "id"),
        Index("ix_gateway_requests_project_created", "project_id", "created_at"),
        Index("ix_gateway_requests_key_created", "api_key_id", "created_at"),
        Index("ix_gateway_requests_trace_id", "trace_id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    trace_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=lambda: f"trace_{uuid4().hex}"
    )
    event_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="service_call", server_default="service_call"
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_username: Mapped[str | None] = mapped_column(String(100))
    project_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    api_key_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    service_code: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    audit_result: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    audit_steps: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, server_default="{}"
    )
    description: Mapped[str | None] = mapped_column(String(1000))
    latency_ms: Mapped[int] = mapped_column(nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(500))
    error_details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LlmRequestUsage(Base):
    """LLM 请求专属的模型与 Token 计量，不属于通用请求日志字段。"""

    __tablename__ = "llm_request_usages"

    request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("gateway_requests.request_id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    total_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    finish_reason: Mapped[str | None] = mapped_column(String(80))


class LogRetentionRun(Base):
    """持久记录每次调用日志保留任务的执行结果。"""

    __tablename__ = "log_retention_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_log_retention_runs_status",
        ),
        Index("ix_log_retention_runs_started_at", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80))


class LlmProviderConfig(Base):
    """管理员配置的 OpenAI 兼容供应商连接信息；API Key 仅保存密文。"""

    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_llm_provider_configs_status"),
        Index("ix_llm_provider_configs_status", "status"),
        UniqueConstraint(
            "api_fingerprint",
            name="uq_llm_provider_configs_api_fingerprint",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(100), nullable=False)
    route_prefix: Mapped[str | None] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    api_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
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
    """已废弃的旧模型映射记录；保留表结构以避免升级时删除历史数据。"""

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


class AsrProviderConfig(Base):
    """管理员配置的 ASR 上游连接；供应商 API Key 仅保存密文。"""

    __tablename__ = "asr_provider_configs"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_asr_provider_configs_status"),
        CheckConstraint("route_prefix = 'volc'", name="ck_asr_provider_configs_route_prefix_volc"),
        Index("ix_asr_provider_configs_route_status", "route_prefix", "status"),
        Index(
            "ix_asr_provider_configs_model_route_status",
            "route_prefix",
            "model_name",
            "status",
        ),
        UniqueConstraint("api_fingerprint", name="uq_asr_provider_configs_api_fingerprint"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(100), nullable=False)
    route_prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(
        String(200), nullable=False, server_default="doubao-seed-asr-2.0"
    )
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    file_transcription_url: Mapped[str] = mapped_column(String(500), nullable=False)
    realtime_url: Mapped[str] = mapped_column(String(500), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    api_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EmbeddingProviderConfig(Base):
    """管理员维护的 OpenAI 兼容 Embedding 连接；API Key 只保存密文。"""

    __tablename__ = "embedding_provider_configs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_embedding_provider_configs_status"
        ),
        Index("ix_embedding_provider_configs_status", "status"),
        UniqueConstraint("api_fingerprint", name="uq_embedding_provider_configs_api_fingerprint"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(100), nullable=False)
    route_prefix: Mapped[str | None] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    api_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_success: Mapped[bool | None] = mapped_column()
    last_test_message: Mapped[str | None] = mapped_column(String(250))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EmbeddingRequestUsage(Base):
    """Embedding 请求使用的模型、模态和 token 用量。"""

    __tablename__ = "embedding_request_usages"

    request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("gateway_requests.request_id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    modality: Mapped[str] = mapped_column(String(20), nullable=False, server_default="text")
    input_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    vector_dimensions: Mapped[int | None] = mapped_column(Integer)


class RagKnowledgeBase(Base):
    """按项目隔离的向量知识库；创建后固定模型，避免混用向量空间。"""

    __tablename__ = "rag_knowledge_bases"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_rag_knowledge_bases_project_name"),
        Index("ix_rag_knowledge_bases_project_created", "project_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1000")
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, server_default="120")
    vector_dimensions: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RagDocument(Base):
    """知识库中的一个逻辑文档；当前首发支持文本内容。"""

    __tablename__ = "rag_documents"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "external_id", name="uq_rag_documents_external_id"),
        Index("ix_rag_documents_kb_created", "knowledge_base_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("rag_knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_id: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RagChunk(Base):
    """pgvector 中保存的文本切片；media_type/model/dimensions 为多模态扩展预留。"""

    __tablename__ = "rag_chunks"
    __table_args__ = (
        Index("ix_rag_chunks_kb_document", "knowledge_base_id", "document_id", "sequence"),
        Index("ix_rag_chunks_kb_modality", "knowledge_base_id", "modality"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("rag_knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("rag_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    modality: Mapped[str] = mapped_column(String(20), nullable=False, server_default="text")
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
