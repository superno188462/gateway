"""Allow recording authentication failures without an assumed project."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0021"
down_revision: str | None = "20260929_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Permit global audit records for requests with an invalid project key."""
    op.alter_column("gateway_requests", "project_id", nullable=True)
    op.alter_column("gateway_requests", "api_key_id", nullable=True)
    op.alter_column("gateway_requests", "model", type_=sa.String(200))


def downgrade() -> None:
    """Remove unattributed audits before restoring required project references."""
    op.execute("DELETE FROM gateway_requests WHERE project_id IS NULL OR api_key_id IS NULL")
    op.alter_column("gateway_requests", "api_key_id", nullable=False)
    op.alter_column("gateway_requests", "project_id", nullable=False)
    op.alter_column("gateway_requests", "model", type_=sa.String(100))
