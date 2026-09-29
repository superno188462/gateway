"""Separate generic request log fields from LLM usage details."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0024"
down_revision: str | None = "20260929_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Preserve historical LLM usage in its own service table."""
    op.add_column("gateway_requests", sa.Column("description", sa.String(1000), nullable=True))
    op.create_table(
        "llm_request_usages",
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("finish_reason", sa.String(80), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"], ["gateway_requests.request_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.execute(
        """
        INSERT INTO llm_request_usages
            (request_id, model, prompt_tokens, completion_tokens, total_tokens, finish_reason)
        SELECT request_id, model, prompt_tokens, completion_tokens, total_tokens,
               result_summary ->> 'finish_reason'
        FROM gateway_requests
        WHERE service_code = 'mock-llm-v1'
        """
    )
    op.execute(
        """
        UPDATE gateway_requests AS request
        SET description = concat_ws(
            '；',
            '模型 ' || usage.model,
            CASE WHEN request.status = 'succeeded' THEN
                '调用成功，消耗 ' || usage.total_tokens || ' tokens（输入 '
                    || usage.prompt_tokens || '，输出 ' || usage.completion_tokens || '）'
            WHEN request.error_message IS NOT NULL THEN request.error_message
            ELSE NULL END,
            CASE WHEN usage.finish_reason IS NOT NULL
                THEN '结束原因：' || usage.finish_reason ELSE NULL END
        )
        FROM llm_request_usages AS usage
        WHERE usage.request_id = request.request_id
        """
    )
    op.drop_column("gateway_requests", "model")
    op.drop_column("gateway_requests", "result_summary")
    op.drop_column("gateway_requests", "usage_metrics")
    op.drop_column("gateway_requests", "prompt_tokens")
    op.drop_column("gateway_requests", "completion_tokens")
    op.drop_column("gateway_requests", "total_tokens")


def downgrade() -> None:
    """Restore legacy columns and backfill them from service usage details."""
    op.add_column(
        "gateway_requests",
        sa.Column("model", sa.String(100), server_default="unknown", nullable=False),
    )
    op.add_column("gateway_requests", sa.Column("result_summary", sa.JSON(), nullable=True))
    op.add_column(
        "gateway_requests",
        sa.Column("usage_metrics", sa.JSON(), server_default="{}", nullable=False),
    )
    op.add_column(
        "gateway_requests",
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "gateway_requests",
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "gateway_requests",
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.execute(
        """
        UPDATE gateway_requests AS request
        SET model = usage.model,
            prompt_tokens = usage.prompt_tokens,
            completion_tokens = usage.completion_tokens,
            total_tokens = usage.total_tokens,
            usage_metrics = json_build_object(
                'input_tokens', usage.prompt_tokens,
                'output_tokens', usage.completion_tokens,
                'total_tokens', usage.total_tokens,
                'unit', 'tokens'
            ),
            result_summary = CASE WHEN usage.finish_reason IS NULL THEN NULL
                ELSE json_build_object('finish_reason', usage.finish_reason) END
        FROM llm_request_usages AS usage
        WHERE usage.request_id = request.request_id
        """
    )
    op.drop_table("llm_request_usages")
    op.drop_column("gateway_requests", "description")
