"""将认证从管理员专用调整为基于角色的通用认证。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0003"
down_revision: str | None = "20260927_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加用户角色并将会话表改为通用命名。"""
    op.add_column("users", sa.Column("role", sa.String(length=20), nullable=True))
    op.execute("UPDATE users SET role = CASE WHEN is_admin THEN 'admin' ELSE 'user' END")
    op.alter_column("users", "role", nullable=False)
    op.drop_index("uq_users_single_admin", table_name="users")
    op.create_check_constraint("ck_users_role", "users", "role IN ('admin', 'user')")
    op.create_index(
        "uq_users_single_admin",
        "users",
        ["role"],
        unique=True,
        postgresql_where=sa.text("role = 'admin'"),
    )
    op.drop_column("users", "is_admin")
    op.rename_table("admin_sessions", "auth_sessions")
    op.drop_index("ix_admin_sessions_user_id", table_name="auth_sessions")
    op.drop_index("ix_admin_sessions_expires_at", table_name="auth_sessions")
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])


def downgrade() -> None:
    """恢复管理员专用会话和布尔管理员标记。"""
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.create_index("ix_admin_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_admin_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.rename_table("auth_sessions", "admin_sessions")
    op.drop_index("uq_users_single_admin", table_name="users")
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), nullable=True))
    op.execute("UPDATE users SET is_admin = role = 'admin'")
    op.alter_column("users", "is_admin", nullable=False)
    op.drop_column("users", "role")
    op.create_index(
        "uq_users_single_admin",
        "users",
        ["is_admin"],
        unique=True,
        postgresql_where=sa.text("is_admin = true"),
    )
