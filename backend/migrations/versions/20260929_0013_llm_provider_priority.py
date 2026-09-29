"""Add explicit priority order to OpenAI-compatible provider connections."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0013"
down_revision: str | None = "20260929_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Assign existing connections the same priority, preserving creation order as tie-breaker."""
    op.add_column(
        "llm_provider_configs",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
    )


def downgrade() -> None:
    """Remove explicit provider priority."""
    op.drop_column("llm_provider_configs", "priority")
