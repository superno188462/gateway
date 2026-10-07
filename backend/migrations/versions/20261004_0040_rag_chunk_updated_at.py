"""Track when an individual vector chunk was last edited."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261004_0040"
down_revision: str | None = "20261004_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rag_chunks",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE rag_chunks SET updated_at = created_at")
    op.alter_column(
        "rag_chunks",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def downgrade() -> None:
    op.drop_column("rag_chunks", "updated_at")
