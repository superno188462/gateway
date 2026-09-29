"""Separate supplier display name from individual upstream connection name."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0015"
down_revision: str | None = "20260929_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill supplier names from existing connection names for compatibility."""
    op.add_column(
        "llm_provider_configs",
        sa.Column("supplier_name", sa.String(length=100), nullable=True),
    )
    op.execute("UPDATE llm_provider_configs SET supplier_name = name")
    op.alter_column("llm_provider_configs", "supplier_name", nullable=False)


def downgrade() -> None:
    """Remove the separate supplier display name."""
    op.drop_column("llm_provider_configs", "supplier_name")
