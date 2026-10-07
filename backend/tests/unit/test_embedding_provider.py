"""OpenAI 兼容 Embedding 传输与响应结构验证。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.services.embedding.api import EmbeddingRequest
from app.services.embedding.configuration import (
    EmbeddingConfigurationService,
    EmbeddingConfigurationValidationError,
    ResolvedEmbeddingModel,
)
from app.services.embedding.console_api import _actor
from app.services.embedding.provider import ConfiguredEmbeddingProvider
from app.services.embedding.rag import RagKnowledgeService


class FakeConfiguration:
    async def resolve_model_pool(self, model: str) -> tuple[ResolvedEmbeddingModel, ...]:
        return (
            ResolvedEmbeddingModel(
                model=model,
                upstream_model="provider-embed-v1",
                base_url="https://embedding.example/v1",
                api_key="secret-upstream-key",
                connection_name="primary",
                supplier_name="Example",
            ),
        )


@pytest.mark.asyncio
async def test_embedding_provider_sends_openai_request_and_orders_vectors() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "provider-embed-v1",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]},
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
                ],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredEmbeddingProvider(FakeConfiguration(), client)  # type: ignore[arg-type]
        result = await provider.embed("embed/test-model", ["first", "second"], {"dimensions": 2})

    assert len(sent) == 1
    assert str(sent[0].url) == "https://embedding.example/v1/embeddings"
    assert sent[0].headers["authorization"] == "Bearer secret-upstream-key"
    request_body = json.loads(sent[0].content)
    assert request_body == {
        "model": "provider-embed-v1",
        "input": ["first", "second"],
        "dimensions": 2,
    }
    assert result.input_tokens == 7
    assert result.dimensions == 2
    assert result.response["model"] == "embed/test-model"
    data = result.response["data"]
    assert isinstance(data, list)
    first, second = data
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["embedding"] == [0.1, 0.2]
    assert second["embedding"] == [0.3, 0.4]


def test_rag_chunker_preserves_text_and_overlaps_adjacent_segments() -> None:
    text = "x" * 2300
    chunks = RagKnowledgeService._chunk_text(text, 1000, 100)

    assert [len(chunk) for chunk in chunks] == [1000, 1000, 500]
    assert chunks[0][-100:] == chunks[1][:100]
    assert chunks[1][-100:] == chunks[2][:100]


def test_rag_chunker_prefers_paragraph_boundaries_when_reasonable() -> None:
    chunks = RagKnowledgeService._chunk_text("第一段。\n\n第二段。", 100, 10)

    assert chunks == ["第一段。\n\n第二段。"]


def test_embedding_request_accepts_text_and_rejects_empty_input() -> None:
    single = EmbeddingRequest(model="prefix/model", input="要向量化的文本")
    multiple = EmbeddingRequest(model="prefix/model", input=["第一段", "第二段"])

    assert single.input == "要向量化的文本"
    assert multiple.input == ["第一段", "第二段"]
    with pytest.raises(ValidationError, match="不能为空"):
        EmbeddingRequest(model="prefix/model", input=[" "])


def test_embedding_base_url_restricts_plain_http_to_loopback() -> None:
    assert EmbeddingConfigurationService._normalize_url("http://127.0.0.1:9000/v1") == (
        "http://127.0.0.1:9000/v1"
    )
    assert EmbeddingConfigurationService._normalize_url("https://api.example.com/v1/") == (
        "https://api.example.com/v1"
    )
    with pytest.raises(EmbeddingConfigurationValidationError, match="必须使用 HTTPS"):
        EmbeddingConfigurationService._normalize_url("http://api.example.com/v1")


@pytest.mark.asyncio
async def test_rag_console_allows_viewer_reads_but_rejects_writes() -> None:
    project_id = uuid4()
    user_id = uuid4()

    class Projects:
        async def get_for_user(self, requested_project_id: object, _user: object) -> object:
            assert requested_project_id == project_id
            return SimpleNamespace(owner_id=uuid4())

        async def list_members(self, requested_project_id: object, _user: object) -> list[object]:
            assert requested_project_id == project_id
            return [SimpleNamespace(user_id=user_id, role="viewer")]

    viewer = SimpleNamespace(id=user_id, username="viewer", role="user")
    actor = await _actor(project_id, viewer, Projects(), write=False)  # type: ignore[arg-type]
    assert actor.project_id == project_id
    assert actor.user_id == user_id
    assert actor.id is None
    with pytest.raises(HTTPException, match="项目成员无权执行此操作") as error:
        await _actor(project_id, viewer, Projects(), write=True)  # type: ignore[arg-type]
    assert error.value.status_code == 403
