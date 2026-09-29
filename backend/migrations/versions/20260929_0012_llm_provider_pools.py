"""Allow multiple upstream routes per public model and record connectivity tests."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0012"
down_revision: str | None = "20260928_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make model codes shareable across upstreams and track connection test results."""
    op.drop_constraint("llm_model_configs_model_code_key", "llm_model_configs", type_="unique")
    op.create_unique_constraint(
        "uq_llm_model_configs_provider_model",
        "llm_model_configs",
        ["provider_id", "model_code"],
    )
    op.create_index(
        "ix_llm_model_configs_model_code_status",
        "llm_model_configs",
        ["model_code", "status"],
    )
    op.add_column(
        "llm_provider_configs", sa.Column("last_tested_at", sa.DateTime(timezone=True))
    )
    op.add_column("llm_provider_configs", sa.Column("last_test_success", sa.Boolean()))
    op.add_column("llm_provider_configs", sa.Column("last_test_message", sa.String(length=250)))


def downgrade() -> None:
    """Restore unique public model codes and remove connectivity test state."""
    op.drop_column("llm_provider_configs", "last_test_message")
    op.drop_column("llm_provider_configs", "last_test_success")
    op.drop_column("llm_provider_configs", "last_tested_at")
    op.drop_index("ix_llm_model_configs_model_code_status", table_name="llm_model_configs")
    op.drop_constraint(
        "uq_llm_model_configs_provider_model", "llm_model_configs", type_="unique"
    )
    op.create_unique_constraint(
        "llm_model_configs_model_code_key", "llm_model_configs", ["model_code"]
    )
