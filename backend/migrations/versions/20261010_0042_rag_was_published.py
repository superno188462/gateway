"""Track whether a versioned vector record has ever been published."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261010_0042"
down_revision: str | None = "20261009_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0041 may have been applied with or without this column during development.
    op.execute(
        "ALTER TABLE rag_documents "
        "ADD COLUMN IF NOT EXISTS was_published BOOLEAN"
    )
    # Older hidden rows are ambiguous: they may be staged or retired. Convert the old
    # NOT NULL DEFAULT TRUE representation to an explicit unknown instead of guessing.
    op.execute("ALTER TABLE rag_documents ALTER COLUMN was_published DROP NOT NULL")
    op.execute("ALTER TABLE rag_documents ALTER COLUMN was_published DROP DEFAULT")
    op.execute(
        "UPDATE rag_documents "
        "SET was_published = CASE "
        "WHEN is_published IS TRUE THEN TRUE "
        "WHEN was_published IS TRUE THEN NULL "
        "ELSE was_published END"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM rag_documents WHERE version_id IS NOT NULL) THEN
                RAISE EXCEPTION
                    'Cannot downgrade publication history while versioned records exist. '
                    'Export or remove them first.';
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE rag_documents DROP COLUMN IF EXISTS was_published")
