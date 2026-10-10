"""在真实 PostgreSQL 上验证 RAG 写路径使用 document -> chunk 锁顺序。"""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_document_then_chunk_lock_order_completes_without_deadlock() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要隔离 PostgreSQL 并设置 TEST_DATABASE_URL")

    schema = f"rag_lock_{uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE rag_documents (id integer PRIMARY KEY, body text NOT NULL)")
            )
            await connection.execute(
                text(
                    "CREATE TABLE rag_chunks (id integer PRIMARY KEY, "
                    "document_id integer NOT NULL REFERENCES rag_documents(id), body text NOT NULL)"
                )
            )
            await connection.execute(
                text("INSERT INTO rag_documents VALUES (1, 'before')")
            )
            await connection.execute(text("INSERT INTO rag_chunks VALUES (1, 1, 'before')"))

        patch_locked_document = asyncio.Event()
        upsert_started_waiting_for_document = asyncio.Event()

        async def patch_record() -> None:
            async with engine.connect() as connection:
                async with connection.begin():
                    await connection.execute(
                        text("SELECT id FROM rag_documents WHERE id = 1 FOR UPDATE")
                    )
                    patch_locked_document.set()
                    await upsert_started_waiting_for_document.wait()
                    await connection.execute(
                        text("SELECT id FROM rag_chunks WHERE document_id = 1 FOR UPDATE")
                    )
                    await connection.execute(
                        text("UPDATE rag_chunks SET body = 'patched' WHERE id = 1")
                    )

        async def upsert_record() -> None:
            await patch_locked_document.wait()
            async with engine.connect() as connection:
                async with connection.begin():
                    upsert_started_waiting_for_document.set()
                    await connection.execute(
                        text("SELECT id FROM rag_documents WHERE id = 1 FOR UPDATE")
                    )
                    await connection.execute(
                        text("DELETE FROM rag_chunks WHERE document_id = 1")
                    )

        await asyncio.wait_for(
            asyncio.gather(patch_record(), upsert_record()),
            timeout=10,
        )
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
