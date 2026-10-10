"""Add non-vector records, parent links, and atomic document-version publication."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261009_0041"
down_revision: str | None = "20261004_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rag_documents",
        sa.Column("record_kind", sa.String(20), server_default="vector", nullable=False),
    )
    op.add_column("rag_documents", sa.Column("parent_external_id", sa.String(200)))
    op.add_column("rag_documents", sa.Column("namespace", sa.String(200)))
    op.add_column("rag_documents", sa.Column("logical_document_id", sa.String(200)))
    op.add_column("rag_documents", sa.Column("version_id", sa.String(200)))
    op.add_column(
        "rag_documents",
        sa.Column("is_published", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "rag_documents",
        sa.Column("modality", sa.String(20), server_default="text", nullable=False),
    )
    op.execute(
        """
        UPDATE rag_documents AS documents
        SET modality = chunks.modality
        FROM (
            SELECT DISTINCT ON (document_id) document_id, modality
            FROM rag_chunks
            ORDER BY document_id, sequence
        ) AS chunks
        WHERE chunks.document_id = documents.id
        """
    )
    op.create_check_constraint(
        "ck_rag_documents_record_kind", "rag_documents", "record_kind IN ('vector', 'document')"
    )
    op.create_check_constraint(
        "ck_rag_documents_child_is_vector",
        "rag_documents",
        "parent_external_id IS NULL OR record_kind = 'vector'",
    )
    op.create_check_constraint(
        "ck_rag_documents_version_scope",
        "rag_documents",
        "(version_id IS NULL AND "
        "((namespace IS NULL AND logical_document_id IS NULL) OR "
        "(namespace IS NOT NULL AND logical_document_id IS NOT NULL))) OR "
        "(version_id IS NOT NULL AND namespace IS NOT NULL AND logical_document_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_rag_documents_parent_external_id",
        "rag_documents",
        "rag_documents",
        ["knowledge_base_id", "parent_external_id"],
        ["knowledge_base_id", "external_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_rag_documents_parent_reference",
        "rag_documents",
        ["knowledge_base_id", "parent_external_id"],
    )
    op.create_index(
        "ix_rag_documents_publication_scope",
        "rag_documents",
        [
            "knowledge_base_id",
            "namespace",
            "logical_document_id",
            "version_id",
            "is_published",
        ],
    )
    op.execute(
        """
        CREATE FUNCTION validate_rag_parent_record() RETURNS trigger AS $$
        DECLARE parent rag_documents%ROWTYPE;
        BEGIN
            IF NEW.parent_external_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT * INTO parent
            FROM rag_documents
            WHERE knowledge_base_id = NEW.knowledge_base_id
              AND external_id = NEW.parent_external_id
            FOR KEY SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'parent record does not exist in collection'
                    USING ERRCODE = '23503', CONSTRAINT = 'fk_rag_documents_parent_external_id';
            END IF;
            IF parent.record_kind <> 'document' THEN
                RAISE EXCEPTION 'parent record must have document kind'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_rag_documents_parent_kind';
            END IF;
            IF parent.namespace IS DISTINCT FROM NEW.namespace
               OR parent.logical_document_id IS DISTINCT FROM NEW.logical_document_id THEN
                RAISE EXCEPTION 'parent and child must share namespace and logical document id'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_rag_documents_parent_scope';
            END IF;
            IF parent.is_published IS DISTINCT FROM NEW.is_published
               OR parent.version_id IS DISTINCT FROM NEW.version_id THEN
                RAISE EXCEPTION 'parent and child publication state/version must match'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_rag_documents_parent_version';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_rag_parent_record
        BEFORE INSERT OR UPDATE OF knowledge_base_id, external_id, record_kind,
            parent_external_id, namespace, logical_document_id, version_id, is_published
        ON rag_documents
        FOR EACH ROW EXECUTE FUNCTION validate_rag_parent_record();
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_rag_parent_scope_drift() RETURNS trigger AS $$
        BEGIN
            IF NEW.record_kind <> 'vector' AND EXISTS (
                SELECT 1 FROM rag_chunks chunk WHERE chunk.document_id = OLD.id
            ) THEN
                RAISE EXCEPTION 'record with vector chunks cannot change to document kind'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_rag_document_kind_has_no_chunks';
            END IF;
            IF EXISTS (
                SELECT 1 FROM rag_documents child
                WHERE child.knowledge_base_id = OLD.knowledge_base_id
                  AND child.parent_external_id = OLD.external_id
                  AND (
                    NEW.record_kind <> 'document'
                    OR child.knowledge_base_id <> NEW.knowledge_base_id
                    OR child.namespace IS DISTINCT FROM NEW.namespace
                    OR child.logical_document_id IS DISTINCT FROM NEW.logical_document_id
                    OR child.version_id IS DISTINCT FROM NEW.version_id
                  )
            ) THEN
                RAISE EXCEPTION 'parent update would invalidate child record references'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_rag_documents_parent_scope';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_prevent_rag_parent_scope_drift
        BEFORE UPDATE OF knowledge_base_id, external_id, record_kind,
            namespace, logical_document_id, version_id
        ON rag_documents
        FOR EACH ROW EXECUTE FUNCTION prevent_rag_parent_scope_drift();
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_rag_chunk_record_kind() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM rag_documents
                WHERE id = NEW.document_id
                  AND knowledge_base_id = NEW.knowledge_base_id
                  AND record_kind = 'vector'
            ) THEN
                RAISE EXCEPTION 'vector chunk must belong to a vector record'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_rag_chunk_vector_record_kind';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_rag_chunk_record_kind
        BEFORE INSERT OR UPDATE OF document_id, knowledge_base_id
        ON rag_chunks
        FOR EACH ROW EXECUTE FUNCTION validate_rag_chunk_record_kind();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM rag_documents
                WHERE record_kind <> 'vector'
                   OR parent_external_id IS NOT NULL
                   OR namespace IS NOT NULL
                   OR logical_document_id IS NOT NULL
                   OR version_id IS NOT NULL
                OR is_published IS FALSE
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade vector records migration while new records exist. '
                    'Export and remove ordinary, linked, scoped, versioned, or hidden records.';
            END IF;
        END $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_validate_rag_chunk_record_kind ON rag_chunks")
    op.execute("DROP FUNCTION IF EXISTS validate_rag_chunk_record_kind()")
    op.execute("DROP TRIGGER IF EXISTS trg_validate_rag_parent_record ON rag_documents")
    op.execute("DROP FUNCTION IF EXISTS validate_rag_parent_record()")
    op.execute("DROP TRIGGER IF EXISTS trg_prevent_rag_parent_scope_drift ON rag_documents")
    op.execute("DROP FUNCTION IF EXISTS prevent_rag_parent_scope_drift()")
    op.drop_index("ix_rag_documents_publication_scope", table_name="rag_documents")
    op.drop_index("ix_rag_documents_parent_reference", table_name="rag_documents")
    op.drop_constraint(
        "fk_rag_documents_parent_external_id", "rag_documents", type_="foreignkey"
    )
    op.drop_constraint("ck_rag_documents_version_scope", "rag_documents", type_="check")
    op.drop_constraint("ck_rag_documents_child_is_vector", "rag_documents", type_="check")
    op.drop_constraint("ck_rag_documents_record_kind", "rag_documents", type_="check")
    op.drop_column("rag_documents", "is_published")
    op.drop_column("rag_documents", "modality")
    op.drop_column("rag_documents", "version_id")
    op.drop_column("rag_documents", "logical_document_id")
    op.drop_column("rag_documents", "namespace")
    op.drop_column("rag_documents", "parent_external_id")
    op.drop_column("rag_documents", "record_kind")
