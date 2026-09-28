"""Encrypt API Key values for authorized owner/editor retrieval."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0008"
down_revision: str | None = "20260928_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ciphertext storage and revoke legacy keys that cannot be recovered."""
    op.add_column("api_keys", sa.Column("encrypted_secret", sa.String(length=256), nullable=True))
    op.execute(
        "UPDATE api_keys SET status = 'revoked', revoked_at = now() "
        "WHERE encrypted_secret IS NULL AND status = 'active'"
    )


def downgrade() -> None:
    """Remove encrypted values; keys will again be unrecoverable after downgrade."""
    op.drop_column("api_keys", "encrypted_secret")
