"""Add an index for descending gateway request page queries."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260929_0023"
down_revision: str | None = "20260929_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Support global log ordering and page navigation by created time and ID."""
    op.create_index(
        "ix_gateway_requests_created_id",
        "gateway_requests",
        ["created_at", "id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Remove the page ordering index."""
    op.drop_index("ix_gateway_requests_created_id", table_name="gateway_requests")
