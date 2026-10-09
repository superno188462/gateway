"""Embedding 接口错误的技术日志关联测试。"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials

from app.services.embedding.api import EmbeddingRequest, create_embeddings
from app.services.embedding.application import EmbeddingGatewayError


@pytest.mark.asyncio
async def test_embedding_gateway_failure_is_written_to_technical_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    trace_id = "emb-test-trace-123"
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/embeddings",
            "raw_path": b"/v1/embeddings",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
    )
    request.state.trace_id = trace_id
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="project-key")
    key = SimpleNamespace()
    key_service = SimpleNamespace(verify=AsyncMock(return_value=key))
    gateway = SimpleNamespace(
        embed=AsyncMock(
            side_effect=EmbeddingGatewayError(
                "upstream_embedding_failed",
                "Embedding 上游调用失败，请检查连接配置、模型名称和 API Key",
                502,
                trace_id,
            )
        )
    )
    recorder = SimpleNamespace(record_auth_rejection=AsyncMock())

    caplog.set_level(logging.ERROR, logger="gateway.embedding.auth")
    response = await create_embeddings(
        EmbeddingRequest(model="volc/example", input="test"),
        request,
        credentials,
        key_service,  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
        recorder,  # type: ignore[arg-type]
    )

    assert response.status_code == 502
    assert any(
        record.name == "gateway.embedding.auth"
        and f"request_id={trace_id}" in record.getMessage()
        and "error_code=upstream_embedding_failed" in record.getMessage()
        for record in caplog.records
    )
