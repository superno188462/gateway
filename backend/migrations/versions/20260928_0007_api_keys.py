"""Add project API Key digest and metadata table."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0007"
down_revision: str | None = "20260928_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create API Key metadata table; plaintext keys are never persisted."""
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=20), nullable=False),
        sa.Column("key_last_four", sa.String(length=4), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_api_keys_status"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        sa.UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),
    )
    op.create_index("ix_api_keys_project_created_at", "api_keys", ["project_id", "created_at"])


def downgrade() -> None:
    """Drop API Key records."""
    op.drop_index("ix_api_keys_project_created_at", table_name="api_keys")
    op.drop_table("api_keys")
