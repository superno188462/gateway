"""Allow subscriptions that do not consume AI token quotas."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0027"
down_revision: str | None = "20260930_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make the token allocation optional for data-only project services."""
    op.drop_constraint(
        "ck_service_subscriptions_token_limit",
        "project_service_subscriptions",
        type_="check",
    )
    op.alter_column(
        "project_service_subscriptions",
        "monthly_token_limit",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_service_subscriptions_token_limit",
        "project_service_subscriptions",
        "monthly_token_limit IS NULL OR monthly_token_limit > 0",
    )


def downgrade() -> None:
    """Refuse downgrade while data-only subscriptions exist."""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM project_service_subscriptions
                WHERE monthly_token_limit IS NULL
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while data-only service subscriptions exist';
            END IF;
        END $$;
    """)
    op.drop_constraint(
        "ck_service_subscriptions_token_limit",
        "project_service_subscriptions",
        type_="check",
    )
    op.alter_column(
        "project_service_subscriptions",
        "monthly_token_limit",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_service_subscriptions_token_limit",
        "project_service_subscriptions",
        "monthly_token_limit > 0",
    )
