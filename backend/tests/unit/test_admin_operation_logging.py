"""Global administrator operations are represented in the shared operation log."""

from uuid import uuid4

import pytest

from app.infrastructure.db.models import GatewayRequest
from app.request_logging.application import GatewayRequestRecorder


class FakeSession:
    def __init__(self) -> None:
        self.rows: list[GatewayRequest] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def add(self, row: GatewayRequest) -> None:
        self.rows.append(row)


class FakeSessionFactory:
    def __init__(self) -> None:
        self.session = FakeSession()

    def begin(self) -> FakeSession:
        return self.session


@pytest.mark.asyncio
async def test_admin_operation_record_is_global_and_does_not_require_project_or_api_key() -> None:
    factory = FakeSessionFactory()
    recorder = GatewayRequestRecorder(factory)  # type: ignore[arg-type]
    actor_id = uuid4()

    await recorder.record_admin_operation(
        trace_id="trace_test_admin_op",
        actor_user_id=actor_id,
        actor_username="admin",
        operation="embedding.provider.test",
        description="Embedding 上游连接测试成功，模型 example/embed；连接成功，向量维度 2048",
    )

    assert len(factory.session.rows) == 1
    item = factory.session.rows[0]
    assert item.event_type == "project_operation"
    assert item.project_id is None
    assert item.api_key_id is None
    assert item.actor_user_id == actor_id
    assert item.actor_username == "admin"
    assert item.service_code == "embedding-admin"
    assert item.status == "succeeded"
    assert item.trace_id == "trace_test_admin_op"
    assert "API Key" not in (item.description or "")


@pytest.mark.asyncio
async def test_failed_project_operation_keeps_safe_error_metadata() -> None:
    factory = FakeSessionFactory()
    recorder = GatewayRequestRecorder(factory)  # type: ignore[arg-type]
    project_id = uuid4()
    actor_id = uuid4()
    key_id = uuid4()

    await recorder.record_project_operation(
        trace_id="trace_test_rag_failure",
        project_id=project_id,
        actor_user_id=actor_id,
        actor_username="member",
        api_key_id=key_id,
        operation="rag.document.create",
        description="新增 RAG 文档失败",
        status="failed",
        error_code="embedding_dimensions_conflict",
        error_message="该知识库已有不同向量维度的数据",
    )

    item = factory.session.rows[0]
    assert item.project_id == project_id
    assert item.actor_user_id == actor_id
    assert item.api_key_id == key_id
    assert item.status == "failed"
    assert item.error_code == "embedding_dimensions_conflict"
    assert item.error_message == "该知识库已有不同向量维度的数据"
    assert item.trace_id == "trace_test_rag_failure"
