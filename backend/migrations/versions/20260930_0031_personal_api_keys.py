"""Bind project API keys to their creating member for attribution and isolation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260930_0031"
down_revision: str | None = "20260930_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("gateway_requests", sa.Column("actor_username", sa.String(100), nullable=True))

    # Existing shared keys are attributed to the project owner as the safest legacy default.
    op.execute(
        """
        UPDATE api_keys AS key
        SET created_by_user_id = project.owner_id
        FROM projects AS project
        WHERE key.project_id = project.id
        """
    )
    op.execute(
        """
        UPDATE gateway_requests AS request
        SET actor_user_id = key.created_by_user_id
        FROM api_keys AS key
        WHERE request.api_key_id = key.id
          AND request.actor_user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE gateway_requests AS request
        SET actor_username = users.username
        FROM users
        WHERE request.actor_user_id = users.id
          AND request.actor_username IS NULL
        """
    )

    op.alter_column("api_keys", "created_by_user_id", nullable=False)
    op.create_foreign_key(
        "fk_api_keys_created_by_user_id_users",
        "api_keys",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_api_keys_project_creator_created_at",
        "api_keys",
        ["project_id", "created_by_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_keys_project_creator_created_at", table_name="api_keys")
    op.drop_constraint("fk_api_keys_created_by_user_id_users", "api_keys", type_="foreignkey")
    op.drop_column("gateway_requests", "actor_username")
    op.drop_column("api_keys", "created_by_user_id")
