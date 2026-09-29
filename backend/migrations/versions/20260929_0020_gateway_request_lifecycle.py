"""Add trace, audit and sanitized result metadata to gateway requests."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0020"
down_revision: str | None = "20260929_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend each request record to cover its full gateway lifecycle."""
    op.add_column("gateway_requests", sa.Column("trace_id", sa.String(length=64), nullable=True))
    op.add_column(
        "gateway_requests",
        sa.Column("audit_result", sa.String(length=20), server_default="pending", nullable=False),
    )
    op.add_column(
        "gateway_requests",
        sa.Column("audit_steps", sa.JSON(), server_default="{}", nullable=False),
    )
    op.add_column("gateway_requests", sa.Column("result_summary", sa.JSON(), nullable=True))
    op.add_column(
        "gateway_requests",
        sa.Column("usage_metrics", sa.JSON(), server_default="{}", nullable=False),
    )
    op.add_column(
        "gateway_requests", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE gateway_requests SET trace_id = request_id, "
        "audit_result = CASE WHEN status = 'succeeded' THEN 'allowed' "
        "WHEN status = 'denied' THEN 'denied' ELSE 'failed' END, "
        'audit_steps = \'{"legacy": {"result": "recorded_before_lifecycle_logging"}}\', '
        "usage_metrics = json_build_object('input_tokens', prompt_tokens, "
        "'output_tokens', completion_tokens, 'total_tokens', total_tokens, 'unit', 'tokens'), "
        "finished_at = created_at"
    )
    op.alter_column("gateway_requests", "trace_id", nullable=False)
    op.create_index("ix_gateway_requests_trace_id", "gateway_requests", ["trace_id"])


def downgrade() -> None:
    """Remove lifecycle fields while retaining original request and token data."""
    op.drop_index("ix_gateway_requests_trace_id", table_name="gateway_requests")
    op.drop_column("gateway_requests", "finished_at")
    op.drop_column("gateway_requests", "usage_metrics")
    op.drop_column("gateway_requests", "result_summary")
    op.drop_column("gateway_requests", "audit_steps")
    op.drop_column("gateway_requests", "audit_result")
    op.drop_column("gateway_requests", "trace_id")
