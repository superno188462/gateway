"""Real PostgreSQL tests for preserving ambiguous publication history during migration."""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


def _load_migration():
    path = Path(__file__).parents[2] / "migrations/versions/20261010_0042_rag_was_published.py"
    spec = importlib.util.spec_from_file_location("rag_was_published_migration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load publication migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_column_exists", [False, True])
async def test_migration_keeps_hidden_legacy_publication_state_unknown(
    legacy_column_exists: bool,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要隔离 PostgreSQL 并设置 TEST_DATABASE_URL")

    schema = f"rag_pub_migration_{uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    engine = None
    migration = _load_migration()
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE rag_documents (id integer PRIMARY KEY, "
                    "is_published boolean NOT NULL, version_id text)"
                )
            )
            if legacy_column_exists:
                await connection.execute(
                    text(
                        "ALTER TABLE rag_documents ADD COLUMN was_published "
                        "boolean NOT NULL DEFAULT TRUE"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO rag_documents (id, is_published, version_id, was_published) "
                        "VALUES (1, TRUE, 'visible', TRUE), "
                        "(2, FALSE, 'staged-or-retired', TRUE)"
                    )
                )
            else:
                await connection.execute(
                    text(
                        "INSERT INTO rag_documents (id, is_published, version_id) "
                        "VALUES (1, TRUE, 'visible'), (2, FALSE, 'staged-or-retired')"
                    )
                )

            def upgrade(sync_connection) -> None:
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration.upgrade()

            await connection.run_sync(upgrade)
            rows = (
                await connection.execute(
                    text("SELECT id, was_published FROM rag_documents ORDER BY id")
                )
            ).all()
            assert rows == [(1, True), (2, None)]

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:

                def downgrade(sync_connection) -> None:
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        migration.downgrade()

                await connection.run_sync(downgrade)

        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM rag_documents"))

        async with engine.begin() as connection:

            def downgrade_after_cleanup(sync_connection) -> None:
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration.downgrade()

            await connection.run_sync(downgrade_after_cleanup)
            columns = (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = 'rag_documents'"
                    ),
                    {"schema": schema},
                )
            ).scalars().all()
            assert "was_published" not in columns
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
