"""Prevent storing the same API key more than once in one provider group."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0017"
down_revision: str | None = "20260929_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add keyed fingerprints for enforcing API uniqueness without storing plaintext."""
    op.add_column(
        "llm_provider_configs", sa.Column("group_fingerprint", sa.String(64), nullable=True)
    )
    op.add_column(
        "llm_provider_configs", sa.Column("api_key_fingerprint", sa.String(64), nullable=True)
    )
    op.create_unique_constraint(
        "uq_llm_provider_configs_group_api_key",
        "llm_provider_configs",
        ["group_fingerprint", "api_key_fingerprint"],
    )


def downgrade() -> None:
    """Remove API uniqueness fingerprints."""
    op.drop_constraint(
        "uq_llm_provider_configs_group_api_key", "llm_provider_configs", type_="unique"
    )
    op.drop_column("llm_provider_configs", "api_key_fingerprint")
    op.drop_column("llm_provider_configs", "group_fingerprint")
