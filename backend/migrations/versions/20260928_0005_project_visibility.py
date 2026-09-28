"""Add public and private visibility to projects."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0005"
down_revision: str | None = "20260928_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Existing projects remain private unless their owner changes visibility."""
    op.add_column(
        "projects",
        sa.Column("visibility", sa.String(length=20), nullable=False, server_default="private"),
    )
    op.create_check_constraint(
        "ck_projects_visibility", "projects", "visibility IN ('public', 'private')"
    )


def downgrade() -> None:
    """Remove visibility; downgrade restores the previous project behavior."""
    op.drop_constraint("ck_projects_visibility", "projects", type_="check")
    op.drop_column("projects", "visibility")
