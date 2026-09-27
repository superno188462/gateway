"""真实 PostgreSQL 就绪检查。"""

import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.db.engine import SqlAlchemyReadinessProbe


@pytest.mark.integration
async def test_real_postgresql_is_ready() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("未设置 TEST_DATABASE_URL")

    engine = create_async_engine(database_url)
    try:
        await SqlAlchemyReadinessProbe(engine).check()
    finally:
        await engine.dispose()
