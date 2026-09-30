"""Add ordered project-scoped session messages."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260930_0029"
down_revision: str | None = "20260930_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the normalized short-term conversation message table."""
    op.create_table(
        "project_session_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_user_id", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.String(length=200), nullable=False),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_session_messages_role",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sequence"),
    )
    op.create_index(
        "ix_session_messages_scope_sequence",
        "project_session_messages",
        ["project_id", "external_user_id", "session_id", "sequence"],
    )
    op.create_index("ix_session_messages_expiry", "project_session_messages", ["expires_at"])


def downgrade() -> None:
    """Drop the normalized session messages table."""
    op.drop_index("ix_session_messages_expiry", table_name="project_session_messages")
    op.drop_index("ix_session_messages_scope_sequence", table_name="project_session_messages")
    op.drop_table("project_session_messages")
