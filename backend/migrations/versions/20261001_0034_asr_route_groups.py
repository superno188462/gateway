"""Route ASR connections by prefix and keep provider resource settings internal."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261001_0034"
down_revision: str | None = "20261001_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_asr_provider_configs_model_status", table_name="asr_provider_configs")
    op.drop_column("asr_provider_configs", "model_code")
    op.drop_column("asr_provider_configs", "resource_id")
    op.create_index(
        "ix_asr_provider_configs_route_status",
        "asr_provider_configs",
        ["route_prefix", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_asr_provider_configs_route_status", table_name="asr_provider_configs")
    op.add_column(
        "asr_provider_configs",
        sa.Column("resource_id", sa.String(200), server_default="volc.seedasr.sauc.duration", nullable=False),
    )
    op.add_column(
        "asr_provider_configs",
        sa.Column("model_code", sa.String(200), server_default="doubao-asr-2.0", nullable=False),
    )
    op.create_index(
        "ix_asr_provider_configs_model_status",
        "asr_provider_configs",
        ["model_code", "status"],
    )
