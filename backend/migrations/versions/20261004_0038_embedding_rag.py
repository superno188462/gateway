"""Add OpenAI compatible embeddings and project scoped pgvector RAG."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20261004_0038"
down_revision: str | None = "20261004_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "embedding_provider_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("supplier_name", sa.String(100), nullable=False),
        sa.Column("route_prefix", sa.String(64), nullable=True),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("api_fingerprint", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_success", sa.Boolean(), nullable=True),
        sa.Column("last_test_message", sa.String(250), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_embedding_provider_configs_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "api_fingerprint", name="uq_embedding_provider_configs_api_fingerprint"
        ),
    )
    op.create_index(
        "ix_embedding_provider_configs_status", "embedding_provider_configs", ["status"]
    )
    op.create_table(
        "embedding_request_usages",
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("modality", sa.String(20), server_default="text", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("vector_dimensions", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"], ["gateway_requests.request_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_table(
        "rag_knowledge_bases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column("chunk_size", sa.Integer(), server_default="1000", nullable=False),
        sa.Column("chunk_overlap", sa.Integer(), server_default="120", nullable=False),
        sa.Column("vector_dimensions", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_rag_knowledge_bases_project_name"),
    )
    op.create_index(
        "ix_rag_knowledge_bases_project_created",
        "rag_knowledge_bases",
        ["project_id", "created_at"],
    )
    op.create_table(
        "rag_documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("knowledge_base_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["rag_knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_base_id", "external_id", name="uq_rag_documents_external_id"
        ),
    )
    op.create_index(
        "ix_rag_documents_kb_created", "rag_documents", ["knowledge_base_id", "created_at"]
    )
    op.create_table(
        "rag_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("knowledge_base_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("modality", sa.String(20), server_default="text", nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["rag_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["rag_knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rag_chunks_kb_document",
        "rag_chunks",
        ["knowledge_base_id", "document_id", "sequence"],
    )
    op.create_index("ix_rag_chunks_kb_modality", "rag_chunks", ["knowledge_base_id", "modality"])


def downgrade() -> None:
    op.drop_index("ix_rag_chunks_kb_modality", table_name="rag_chunks")
    op.drop_index("ix_rag_chunks_kb_document", table_name="rag_chunks")
    op.drop_table("rag_chunks")
    op.drop_index("ix_rag_documents_kb_created", table_name="rag_documents")
    op.drop_table("rag_documents")
    op.drop_index("ix_rag_knowledge_bases_project_created", table_name="rag_knowledge_bases")
    op.drop_table("rag_knowledge_bases")
    op.drop_table("embedding_request_usages")
    op.drop_index("ix_embedding_provider_configs_status", table_name="embedding_provider_configs")
    op.drop_table("embedding_provider_configs")
