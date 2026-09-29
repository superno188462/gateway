"""A5 请求日志查询、项目隔离与汇总一致性。"""

import os
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.infrastructure.db.models import GatewayRequest, Project, ProjectMember, User
from app.usage.application import RequestLogNotFoundError, RequestLogService

pytestmark = pytest.mark.integration


async def test_request_logs_are_project_scoped_and_summary_matches_rows() -> None:
    database_url = os.getenv("TEST_DATABASE_URL") or Settings().database_url
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = RequestLogService(factory)
    owner = User(
        id=uuid4(),
        username=f"log-user-{secrets.token_hex(6)}",
        password_hash="test-only",
        role="user",
    )
    hidden_owner = User(
        id=uuid4(),
        username=f"log-owner-{secrets.token_hex(6)}",
        password_hash="test-only",
        role="user",
    )
    project = Project(
        id=uuid4(),
        name=f"log-visible-{secrets.token_hex(4)}",
        description=None,
        status="active",
        visibility="private",
        owner_id=owner.id,
    )
    hidden_project = Project(
        id=uuid4(),
        name=f"log-hidden-{secrets.token_hex(4)}",
        description=None,
        status="active",
        visibility="private",
        owner_id=hidden_owner.id,
    )
    now = datetime.now(UTC)
    request_ids = [f"req_{uuid4().hex}" for _ in range(3)]
    try:
        async with factory.begin() as session:
            session.add_all([owner, hidden_owner])
            await session.flush()
            session.add_all([project, hidden_project])
            await session.flush()
            session.add_all(
                [
                    ProjectMember(project_id=project.id, user_id=owner.id, role="owner"),
                    GatewayRequest(
                        request_id=request_ids[0],
                        project_id=project.id,
                        api_key_id=uuid4(),
                        service_code="llm",
                        model="test-model",
                        status="succeeded",
                        prompt_tokens=12,
                        completion_tokens=8,
                        total_tokens=20,
                        latency_ms=100,
                        created_at=now - timedelta(minutes=2),
                    ),
                    GatewayRequest(
                        request_id=request_ids[1],
                        project_id=project.id,
                        api_key_id=uuid4(),
                        service_code="llm",
                        model="test-model",
                        status="failed",
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        latency_ms=250,
                        error_code="provider_error",
                        created_at=now - timedelta(minutes=1),
                    ),
                    GatewayRequest(
                        request_id=request_ids[2],
                        project_id=hidden_project.id,
                        api_key_id=uuid4(),
                        service_code="llm",
                        model="hidden-model",
                        status="succeeded",
                        prompt_tokens=900,
                        completion_tokens=900,
                        total_tokens=1800,
                        latency_ms=1,
                        created_at=now,
                    ),
                ]
            )

        start_at = now - timedelta(days=1)
        end_at = now + timedelta(minutes=1)
        summary = await service.summary(owner, start_at=start_at, end_at=end_at)
        assert summary.request_count == 2
        assert summary.succeeded_count == 1
        assert summary.failed_count == 1
        assert summary.prompt_tokens == 12
        assert summary.completion_tokens == 8
        assert summary.total_tokens == 20
        assert summary.cost_cny is None

        first_page = await service.list_requests(owner, start_at=start_at, end_at=end_at, limit=1)
        assert len(first_page.items) == 1
        assert first_page.items[0].request_id == request_ids[1]
        assert first_page.page == 1
        assert first_page.page_size == 1
        assert first_page.total_count == 2
        assert first_page.total_pages == 2
        assert first_page.next_cursor is not None
        numbered_second_page = await service.list_requests(
            owner, start_at=start_at, end_at=end_at, limit=1, page=2
        )
        assert [item.request_id for item in numbered_second_page.items] == [request_ids[0]]
        assert numbered_second_page.total_pages == 2
        second_page = await service.list_requests(
            owner,
            start_at=start_at,
            end_at=end_at,
            cursor=first_page.next_cursor,
            limit=1,
        )
        assert [item.request_id for item in second_page.items] == [request_ids[0]]
        assert second_page.next_cursor is None
        assert second_page.items[0].project_name == project.name

        with pytest.raises(RequestLogNotFoundError, match="不存在或无权查看"):
            await service.get_request(owner, request_ids[2])
    finally:
        async with factory.begin() as session:
            await session.execute(
                delete(GatewayRequest).where(GatewayRequest.request_id.in_(request_ids))
            )
            await session.execute(
                delete(Project).where(Project.id.in_([project.id, hidden_project.id]))
            )
            await session.execute(delete(User).where(User.id.in_([owner.id, hidden_owner.id])))
        await engine.dispose()


async def test_retention_keeps_latest_minimum_without_touching_shared_logs() -> None:
    """Use connection-local shadow tables so the shared test log table is never pruned."""
    database_url = os.getenv("TEST_DATABASE_URL") or Settings().database_url
    engine = create_async_engine(database_url)
    now = datetime.now(UTC)
    request_ids = [f"req_{uuid4().hex}" for _ in range(4)]
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE gateway_requests "
                        "(LIKE public.gateway_requests INCLUDING ALL) ON COMMIT PRESERVE ROWS"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE log_retention_runs "
                        "(LIKE public.log_retention_runs INCLUDING ALL) ON COMMIT PRESERVE ROWS"
                    )
                )
                factory = async_sessionmaker(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                async with factory.begin() as session:
                    session.add_all(
                        [
                            GatewayRequest(
                                request_id=request_ids[index],
                                project_id=uuid4(),
                                api_key_id=uuid4(),
                                service_code="llm",
                                model="retention-test",
                                status="succeeded",
                                prompt_tokens=1,
                                completion_tokens=1,
                                total_tokens=2,
                                latency_ms=1,
                                created_at=created_at,
                            )
                            for index, created_at in enumerate(
                                (
                                    now,
                                    now - timedelta(days=31),
                                    now - timedelta(days=32),
                                    now - timedelta(days=33),
                                )
                            )
                        ]
                    )

                service = RequestLogService(factory)
                service.MINIMUM_RECORDS = 2
                run = await service.run_retention()
                async with factory() as session:
                    retained_ids = set(
                        (
                            await session.scalars(
                                select(GatewayRequest.request_id).where(
                                    GatewayRequest.request_id.in_(request_ids)
                                )
                            )
                        ).all()
                    )
                    row_count = await session.scalar(
                        select(func.count()).select_from(GatewayRequest)
                    )

                assert run.status == "succeeded"
                assert run.deleted_count == 2
                assert retained_ids == {request_ids[0], request_ids[1]}
                assert row_count == 2
            # Roll back removes both connection-local tables and their test data.
    finally:
        await engine.dispose()
