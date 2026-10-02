"""Make ASR API identity match LLM: normalized upstream URL plus API Key."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261001_0033"
down_revision: str | None = "20261001_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Old fingerprints also included Resource ID. Recompute them in the service
    # after decrypting each key with the configured application secret.
    op.drop_constraint(
        "uq_asr_provider_configs_api_fingerprint", "asr_provider_configs", type_="unique"
    )
    op.alter_column(
        "asr_provider_configs", "api_fingerprint", existing_type=sa.String(64), nullable=True
    )
    op.execute("UPDATE asr_provider_configs SET api_fingerprint = NULL")
    op.create_unique_constraint(
        "uq_asr_provider_configs_api_fingerprint",
        "asr_provider_configs",
        ["api_fingerprint"],
    )


def downgrade() -> None:
    # URL-only identity cannot be transformed back without decrypting keys.
    op.drop_constraint(
        "uq_asr_provider_configs_api_fingerprint", "asr_provider_configs", type_="unique"
    )
    op.alter_column(
        "asr_provider_configs", "api_fingerprint", existing_type=sa.String(64), nullable=False
    )
