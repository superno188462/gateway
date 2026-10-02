"""Add public model aliases for ASR provider routing."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0037"
down_revision: str | None = "20261002_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "asr_provider_configs",
        sa.Column(
            "model_name",
            sa.String(200),
            nullable=False,
            server_default="doubao-seed-asr-2.0",
        ),
    )
    op.create_index(
        "ix_asr_provider_configs_model_route_status",
        "asr_provider_configs",
        ["route_prefix", "model_name", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_asr_provider_configs_model_route_status",
        table_name="asr_provider_configs",
    )
    op.drop_column("asr_provider_configs", "model_name")
