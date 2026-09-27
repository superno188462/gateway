"""SQLAlchemy 异步引擎与数据库就绪探针。"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_database_engine(database_url: str) -> AsyncEngine:
    """创建应用共享的异步数据库引擎。"""
    return create_async_engine(database_url, pool_pre_ping=True)


class SqlAlchemyReadinessProbe:
    """通过最小查询验证 PostgreSQL 连接。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        """执行不修改数据的 `SELECT 1`。"""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
