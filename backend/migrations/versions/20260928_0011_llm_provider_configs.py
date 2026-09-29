"""Add configurable OpenAI-compatible LLM providers and models."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0011"
down_revision: str | None = "20260928_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create provider and public-to-upstream model configuration tables."""
    op.create_table(
        "llm_provider_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_llm_provider_configs_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(
        "ix_llm_provider_configs_status", "llm_provider_configs", ["status"], unique=False
    )
    op.create_table(
        "llm_model_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_code", sa.String(length=100), nullable=False),
        sa.Column("upstream_model", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_llm_model_configs_status"),
        sa.ForeignKeyConstraint(["provider_id"], ["llm_provider_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_code"),
    )
    op.create_index("ix_llm_model_configs_provider_id", "llm_model_configs", ["provider_id"])


def downgrade() -> None:
    """Drop model mappings and provider connections."""
    op.drop_index("ix_llm_model_configs_provider_id", table_name="llm_model_configs")
    op.drop_table("llm_model_configs")
    op.drop_index("ix_llm_provider_configs_status", table_name="llm_provider_configs")
    op.drop_table("llm_provider_configs")
