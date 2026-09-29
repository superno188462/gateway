"""Allow multiple API keys for the same supplier endpoint group."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260929_0016"
down_revision: str | None = "20260929_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow the shared connection name to repeat for each individual API key."""
    op.drop_constraint("llm_provider_configs_name_key", "llm_provider_configs", type_="unique")


def downgrade() -> None:
    """Restore unique connection names when no duplicates exist."""
    op.create_unique_constraint("llm_provider_configs_name_key", "llm_provider_configs", ["name"])
