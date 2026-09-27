"""建立 B0 迁移基线。

Revision ID: 20260927_0001
Revises: None
Create Date: 2026-09-27
"""

from collections.abc import Sequence

revision: str = "20260927_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """B0 不提前创建业务表。"""


def downgrade() -> None:
    """移除基线版本标记由 Alembic 管理。"""
