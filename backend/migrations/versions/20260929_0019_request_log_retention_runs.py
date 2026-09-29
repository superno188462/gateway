"""Persist request log retention task outcomes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0019"
down_revision: str | None = "20260929_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create retention task history and improve request filter indexes."""
    op.create_table(
        "log_retention_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_log_retention_runs_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_log_retention_runs_started_at", "log_retention_runs", ["started_at"])
    op.create_index("ix_gateway_requests_created_id", "gateway_requests", ["created_at", "id"])
    op.create_index(
        "ix_gateway_requests_status_created", "gateway_requests", ["status", "created_at"]
    )
    op.create_index(
        "ix_gateway_requests_model_created", "gateway_requests", ["model", "created_at"]
    )


def downgrade() -> None:
    """Remove retention history and A5 indexes."""
    op.drop_index("ix_gateway_requests_model_created", table_name="gateway_requests")
    op.drop_index("ix_gateway_requests_status_created", table_name="gateway_requests")
    op.drop_index("ix_gateway_requests_created_id", table_name="gateway_requests")
    op.drop_index("ix_log_retention_runs_started_at", table_name="log_retention_runs")
    op.drop_table("log_retention_runs")
