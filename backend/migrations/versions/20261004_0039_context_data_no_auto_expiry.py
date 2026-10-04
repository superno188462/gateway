"""Retain project context data until an explicit API deletion."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261004_0039"
down_revision: str | None = "20261002_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "project_session_memories",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.alter_column(
        "project_session_messages",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    # Keep all rows and remove legacy expiry timestamps so they cannot be
    # mistaken for an active retention policy by older clients.
    op.execute("UPDATE project_session_memories SET expires_at = NULL")
    op.execute("UPDATE project_session_messages SET expires_at = NULL")


def downgrade() -> None:
    # Legacy versions required a timestamp. Restore a future timestamp for rows
    # that were created under the permanent-retention behavior.
    op.execute(
        "UPDATE project_session_memories "
        "SET expires_at = COALESCE(expires_at, now() + interval '30 days')"
    )
    op.execute(
        "UPDATE project_session_messages "
        "SET expires_at = COALESCE(expires_at, now() + interval '30 days')"
    )
    op.alter_column(
        "project_session_messages",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "project_session_memories",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
