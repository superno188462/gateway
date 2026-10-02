"""Add ASR provider connections."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261001_0032"
down_revision: str | None = "20260930_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asr_provider_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("supplier_name", sa.String(100), nullable=False),
        sa.Column("route_prefix", sa.String(64), nullable=True),
        sa.Column("model_code", sa.String(200), nullable=False),
        sa.Column("websocket_url", sa.String(500), nullable=False),
        sa.Column("resource_id", sa.String(200), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("api_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_asr_provider_configs_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_fingerprint", name="uq_asr_provider_configs_api_fingerprint"),
    )
    op.create_index(
        "ix_asr_provider_configs_model_status",
        "asr_provider_configs",
        ["model_code", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_asr_provider_configs_model_status", table_name="asr_provider_configs")
    op.drop_table("asr_provider_configs")
