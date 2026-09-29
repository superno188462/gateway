"""Add safe, structured failure diagnostics to gateway request records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0022"
down_revision: str | None = "20260929_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store a readable failure reason and structured upstream attempt metadata."""
    op.add_column("gateway_requests", sa.Column("error_message", sa.String(length=500)))
    op.add_column("gateway_requests", sa.Column("error_details", sa.JSON()))


def downgrade() -> None:
    """Remove additional failure diagnostics."""
    op.drop_column("gateway_requests", "error_details")
    op.drop_column("gateway_requests", "error_message")
