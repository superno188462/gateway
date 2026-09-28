"""Add normalized project tags."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0006"
down_revision: str | None = "20260928_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create project tag relation with per-project uniqueness."""
    op.create_table(
        "project_tags",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag", sa.String(length=20), nullable=False),
        sa.CheckConstraint("char_length(tag) BETWEEN 1 AND 20", name="ck_project_tags_length"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "tag"),
    )
    op.create_index("ix_project_tags_tag", "project_tags", ["tag"])


def downgrade() -> None:
    """Remove project tag relation."""
    op.drop_index("ix_project_tags_tag", table_name="project_tags")
    op.drop_table("project_tags")
