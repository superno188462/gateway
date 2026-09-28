"""Add user-level monthly service quotas and preserve current allocations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0010"
down_revision: str | None = "20260928_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create user quota rows and backfill existing project allocations without access loss."""
    op.create_table(
        "user_service_quotas",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_code", sa.String(length=50), nullable=False),
        sa.Column("monthly_token_limit", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("monthly_token_limit > 0", name="ck_user_service_quotas_limit"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "service_code"),
    )
    op.drop_column("project_service_subscriptions", "plan_code")
    op.execute(
        """
        INSERT INTO user_service_quotas (user_id, service_code, monthly_token_limit)
        SELECT projects.owner_id, subscriptions.service_code,
               GREATEST(SUM(subscriptions.monthly_token_limit)::integer, 100000)
        FROM project_service_subscriptions AS subscriptions
        JOIN projects ON projects.id = subscriptions.project_id
        GROUP BY projects.owner_id, subscriptions.service_code
        """
    )
    op.execute(
        """
        INSERT INTO user_service_quotas (user_id, service_code, monthly_token_limit)
        SELECT users.id, 'mock-llm-v1', 100000
        FROM users
        WHERE NOT EXISTS (
            SELECT 1 FROM user_service_quotas
            WHERE user_service_quotas.user_id = users.id
              AND user_service_quotas.service_code = 'mock-llm-v1'
        )
        """
    )


def downgrade() -> None:
    """Remove user service quotas."""
    op.drop_table("user_service_quotas")
    op.add_column(
        "project_service_subscriptions",
        sa.Column("plan_code", sa.String(length=50), server_default="custom", nullable=False),
    )
    op.alter_column("project_service_subscriptions", "plan_code", server_default=None)
