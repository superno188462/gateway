"""Add optional model prefixes to route requests to provider groups."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0014"
down_revision: str | None = "20260929_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add an optional routing prefix; existing connections remain in the default pool."""
    op.add_column("llm_provider_configs", sa.Column("route_prefix", sa.String(length=64)))
    op.create_index(
        "ix_llm_provider_configs_prefix_status_priority",
        "llm_provider_configs",
        ["route_prefix", "status", "priority"],
        unique=False,
    )


def downgrade() -> None:
    """Remove optional provider routing prefixes."""
    op.drop_index(
        "ix_llm_provider_configs_prefix_status_priority",
        table_name="llm_provider_configs",
    )
    op.drop_column("llm_provider_configs", "route_prefix")
