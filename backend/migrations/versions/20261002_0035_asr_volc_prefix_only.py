"""Restrict current ASR admin connections to the Volc adapter prefix."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0035"
down_revision: str | None = "20261001_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing ASR connections are the Volc integration introduced by this service.
    op.execute("UPDATE asr_provider_configs SET route_prefix = 'volc'")
    op.alter_column(
        "asr_provider_configs",
        "route_prefix",
        existing_type=sa.String(64),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_asr_provider_configs_route_prefix_volc",
        "asr_provider_configs",
        "route_prefix = 'volc'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_asr_provider_configs_route_prefix_volc", "asr_provider_configs", type_="check"
    )
    op.alter_column(
        "asr_provider_configs",
        "route_prefix",
        existing_type=sa.String(64),
        nullable=True,
    )
