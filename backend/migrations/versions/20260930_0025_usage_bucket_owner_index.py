"""Index monthly usage buckets for owner quota aggregation."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260930_0025"
down_revision: str | None = "20260929_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Support filtering monthly usage before joining projects by owner."""
    op.create_index(
        "ix_service_usage_buckets_service_period_project",
        "service_usage_buckets",
        ["service_code", "period_start", "project_id"],
    )


def downgrade() -> None:
    """Remove the owner quota aggregation index."""
    op.drop_index(
        "ix_service_usage_buckets_service_period_project",
        table_name="service_usage_buckets",
    )
