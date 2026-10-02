"""Store Volc resource ID and mode-specific ASR endpoints."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0036"
down_revision: str | None = "20261002_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "asr_provider_configs",
        "websocket_url",
        new_column_name="realtime_url",
        existing_type=sa.String(500),
        existing_nullable=False,
    )
    op.add_column(
        "asr_provider_configs",
        sa.Column(
            "resource_id",
            sa.String(200),
            nullable=False,
            server_default="volc.seedasr.sauc.duration",
        ),
    )
    op.add_column(
        "asr_provider_configs",
        sa.Column("file_transcription_url", sa.String(500), nullable=True),
    )
    op.execute(
        "UPDATE asr_provider_configs "
        "SET file_transcription_url = regexp_replace("
        "realtime_url, '/bigmodel_async$', '/bigmodel_nostream')"
    )
    op.alter_column(
        "asr_provider_configs",
        "file_transcription_url",
        existing_type=sa.String(500),
        nullable=False,
    )
    op.alter_column(
        "asr_provider_configs",
        "resource_id",
        existing_type=sa.String(200),
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("asr_provider_configs", "file_transcription_url")
    op.drop_column("asr_provider_configs", "resource_id")
    op.alter_column(
        "asr_provider_configs",
        "realtime_url",
        new_column_name="websocket_url",
        existing_type=sa.String(500),
        existing_nullable=False,
    )
