"""Extend gateway request records to include project operations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260930_0030"
down_revision: str | None = "20260930_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Distinguish calls from project operations and retain their actor."""
    op.add_column(
        "gateway_requests",
        sa.Column(
            "event_type",
            sa.String(length=30),
            server_default="service_call",
            nullable=False,
        ),
    )
    op.add_column(
        "gateway_requests",
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_gateway_requests_actor_user_id_users",
        "gateway_requests",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_gateway_requests_project_event_created",
        "gateway_requests",
        ["project_id", "event_type", "created_at"],
    )


def downgrade() -> None:
    """Remove project operation metadata."""
    op.drop_index("ix_gateway_requests_project_event_created", table_name="gateway_requests")
    op.drop_constraint(
        "fk_gateway_requests_actor_user_id_users", "gateway_requests", type_="foreignkey"
    )
    op.drop_column("gateway_requests", "actor_user_id")
    op.drop_column("gateway_requests", "event_type")
