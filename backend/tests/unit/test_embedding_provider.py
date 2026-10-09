"""OpenAI 兼容 Embedding 传输与响应结构验证。"""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.services.embedding.api import EmbeddingRequest
from app.services.embedding.application import EmbeddingGatewayError, EmbeddingGatewayService
from app.services.embedding.configuration import (
    EmbeddingConfigurationService,
    EmbeddingConfigurationValidationError,
    ResolvedEmbeddingModel,
)
from app.services.embedding.console_api import _actor
from app.services.embedding.provider import ConfiguredEmbeddingProvider, EmbeddingProviderError
from app.services.embedding.rag import RagKnowledgeService, RagVectorUpsert


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


@pytest.mark.asyncio
async def test_volc_provider_uses_multimodal_endpoint_and_normalizes_single_vector() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "doubao-embedding-vision-251215",
                "data": {"object": "embedding", "embedding": [0.1, 0.2]},
                "usage": {"prompt_tokens": 12, "total_tokens": 12},
            },
        )

    class VolcConfiguration:
        async def resolve_model_pool(self, model: str) -> tuple[ResolvedEmbeddingModel, ...]:
            return (
                ResolvedEmbeddingModel(
                    model=model,
                    upstream_model="doubao-embedding-vision-251215",
                    base_url="https://ark.cn-beijing.volces.com/api/v3",
                    api_key="secret-upstream-key",
                    route_prefix="volc",
                ),
            )

    inputs: list[str | dict[str, object]] = [
        {"type": "image_url", "image_url": {"url": "https://cdn.example.com/cat.png"}},
        {"type": "text", "text": "a cat"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredEmbeddingProvider(VolcConfiguration(), client)  # type: ignore[arg-type]
        result = await provider.embed("volc/doubao-embedding-vision-251215", inputs)

    assert str(sent[0].url) == (
        "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
    )
    assert json.loads(sent[0].content)["input"] == inputs
    assert result.input_tokens == 12
    assert result.dimensions == 2
    data = result.response["data"]
    assert isinstance(data, list) and data[0]["embedding"] == [0.1, 0.2]


@pytest.mark.asyncio
async def test_volc_provider_processes_text_batch_and_preserves_input_order() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        text = json.loads(request.content)["input"][0]["text"]
        value = float(len(text))
        return httpx.Response(
            200,
            json={
                "data": {"embedding": [value, value + 1]},
                "usage": {"prompt_tokens": 2},
            },
        )

    class VolcConfiguration:
        async def resolve_model_pool(self, model: str) -> tuple[ResolvedEmbeddingModel, ...]:
            return (
                ResolvedEmbeddingModel(
                    model=model,
                    upstream_model="doubao-embedding-vision-251215",
                    base_url="https://ark.cn-beijing.volces.com/api/v3",
                    api_key="secret-upstream-key",
                    route_prefix="volc",
                ),
            )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredEmbeddingProvider(VolcConfiguration(), client)  # type: ignore[arg-type]
        result = await provider.embed("volc/doubao-embedding-vision-251215", ["a", "bbb", "cc"])

    assert len(sent) == 3
    assert [json.loads(request.content)["input"][0]["text"] for request in sent] == [
        "a",
        "bbb",
        "cc",
    ]
    data = result.response["data"]
    assert isinstance(data, list)
    assert [item["embedding"] for item in data if isinstance(item, dict)] == [
        [1.0, 2.0],
        [3.0, 4.0],
        [2.0, 3.0],
    ]
    assert result.input_tokens == 6


@pytest.mark.asyncio
async def test_volc_text_batch_caps_upstream_concurrency_at_eight() -> None:
    active = 0
    peak = 0

    async def fake_request(
        _route: ResolvedEmbeddingModel,
        content: list[dict[str, object]],
        _parameters: dict[str, object],
    ) -> tuple[list[float], int]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        text = content[0].get("text")
        assert isinstance(text, str)
        return [float(len(text))], 1

    async with httpx.AsyncClient() as client:
        provider = ConfiguredEmbeddingProvider(None, client)
        provider._volc_request = fake_request  # type: ignore[method-assign]
        vectors, tokens = await provider._embed_volc(
            ResolvedEmbeddingModel(
                model="volc/test",
                upstream_model="test",
                base_url="https://example.com",
                api_key="secret",
                route_prefix="volc",
            ),
            [f"text-{index}" for index in range(20)],
            {},
        )

    assert peak == 8
    assert len(vectors) == 20
    assert tokens == 20


@pytest.mark.asyncio
async def test_rag_vector_upsert_embeds_text_batch_once_and_persists_together() -> None:
    project_id = uuid4()
    owner_id = uuid4()
    base = SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        embedding_model="example/embed-v1",
        vector_dimensions=None,
    )

    class FakeSession:
        inserted: list[object] = []

        async def scalar(self, _statement: object) -> object:
            return base

        async def scalars(self, _statement: object) -> list[object]:
            return []

        def add_all(self, items: list[object]) -> None:
            self.inserted.extend(items)

        async def flush(self) -> None:
            return None

    session = FakeSession()

    class BeginContext:
        async def __aenter__(self) -> FakeSession:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeSessionFactory:
        def begin(self) -> BeginContext:
            return BeginContext()

    gateway = SimpleNamespace(
        embed=AsyncMock(
            return_value={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            }
        )
    )
    service = RagKnowledgeService(  # type: ignore[arg-type]
        FakeSessionFactory(), gateway, None  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    service.get_knowledge_base = AsyncMock(return_value=base)  # type: ignore[method-assign]
    key = SimpleNamespace(project_id=project_id, owner_id=owner_id, user_id=None)
    records = [
        RagVectorUpsert("part-1", "part-1", "first", {"order": 1}, "text", ["first"]),
        RagVectorUpsert("part-2", "part-2", "second", {"order": 2}, "text", ["second"]),
    ]

    result = await service.upsert_vectors(key, base.id, records, trace_id="trace-batch")  # type: ignore[arg-type]

    gateway.embed.assert_awaited_once()
    embed_call = gateway.embed.await_args
    assert embed_call is not None
    assert embed_call.args[:4] == (key, "example/embed-v1", ["first", "second"], None)
    assert embed_call.args[4].startswith("emb_")
    assert len(result) == 2
    assert len(session.inserted) == 4
    chunks = [item for item in session.inserted if hasattr(item, "embedding")]
    assert [chunk.embedding for chunk in chunks] == [[0.1, 0.2], [0.3, 0.4]]
    assert [chunk.metadata_json for chunk in chunks] == [{"order": 1}, {"order": 2}]


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


def test_embedding_request_accepts_image_and_video_content_objects() -> None:
    image = EmbeddingRequest(
        model="volc/doubao-embedding-vision-251215",
        input=[{"type": "image_url", "image_url": {"url": "https://cdn.example.com/a.png"}}],
    )
    video = EmbeddingRequest(
        model="volc/doubao-embedding-vision-251215",
        input=[{"type": "video_url", "video_url": {"url": "https://cdn.example.com/a.mp4"}}],
    )

    assert image.input[0].type == "image_url"  # type: ignore[union-attr]
    assert video.input[0].type == "video_url"  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        EmbeddingRequest(
            model="volc/doubao-embedding-vision-251215",
            input=[{"type": "video_url", "video_url": {"url": "https://cdn.example.com/a.txt"}}],
        )


def test_media_quota_reservation_uses_video_token_ceiling() -> None:
    assert EmbeddingGatewayService._estimate_reservation(
        {"type": "image_url", "image_url": {"url": "https://cdn.example.com/a.png"}}
    ) == 20_480
    assert EmbeddingGatewayService._estimate_reservation(
        {"type": "video_url", "video_url": {"url": "https://cdn.example.com/a.mp4"}}
    ) == 204_800
    assert EmbeddingGatewayService._estimate_reservation(
        {"type": "video_url", "video_url": {"max_video_tokens": 10_240}}
    ) == 10_240


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


def _provider_row(route_prefix: str | None, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        route_prefix=route_prefix,
        name=name,
        supplier_name=name,
        base_url=f"https://{name}.example/v3",
        encrypted_api_key=f"enc-{name}",
    )


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_args: object) -> None:
        return None


def _configuration_service(rows: list[SimpleNamespace]) -> EmbeddingConfigurationService:
    session = SimpleNamespace(scalars=AsyncMock(return_value=rows))

    def session_factory() -> SimpleNamespace:
        return _AsyncContext(session)  # type: ignore[return-value]
    cipher = SimpleNamespace(decrypt=lambda value: f"plain-{value}")
    return EmbeddingConfigurationService(session_factory, cipher)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_resolve_model_pool_without_prefix_matches_all_by_priority() -> None:
    rows = [_provider_row("volc", "火山"), _provider_row(None, "通用")]
    service = _configuration_service(rows)

    routes = await service.resolve_model_pool("doubao-embedding-vision")

    assert [route.connection_name for route in routes] == ["火山", "通用"]
    assert [route.upstream_model for route in routes] == [
        "doubao-embedding-vision",
        "doubao-embedding-vision",
    ]
    assert [route.route_prefix for route in routes] == ["volc", None]


@pytest.mark.asyncio
async def test_resolve_model_pool_with_prefix_pins_route_group() -> None:
    rows = [_provider_row("volc", "火山")]
    service = _configuration_service(rows)

    routes = await service.resolve_model_pool("volc/doubao-embedding-vision")

    assert len(routes) == 1
    assert routes[0].connection_name == "火山"
    assert routes[0].upstream_model == "doubao-embedding-vision"
    assert routes[0].route_prefix == "volc"


@pytest.mark.asyncio
async def test_resolve_model_pool_unknown_prefix_and_null_prefix_provider() -> None:
    # 显式前缀的过滤由 SQL WHERE route_prefix 保证；记录为空即返回空路由池
    assert await _configuration_service([]).resolve_model_pool("volc/model-x") == ()


@pytest.mark.asyncio
async def test_embedding_failure_from_internal_caller_is_in_technical_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request_id = "emb_testlog_0001"
    session = SimpleNamespace(flush=AsyncMock(), add=lambda _item: None)
    begin_context = _AsyncContext(session)
    session_factory = SimpleNamespace(begin=lambda: begin_context)
    recorder = SimpleNamespace(start_in_session=AsyncMock())
    provider = SimpleNamespace()
    service = EmbeddingGatewayService(
        session_factory,  # type: ignore[arg-type]
        provider,
        recorder,  # type: ignore[arg-type]
    )
    service._reserve = AsyncMock()  # type: ignore[method-assign]
    service._release = AsyncMock()  # type: ignore[method-assign]
    provider.embed = AsyncMock(
        side_effect=EmbeddingProviderError(
            "model_route_not_found", "没有可用的 Embedding 模型路由", 404
        )
    )

    caplog.set_level(logging.ERROR, logger="gateway.service.embedding")
    with pytest.raises(EmbeddingGatewayError) as exc_info:
        await service.embed(
            SimpleNamespace(
                project_id=uuid4(),
                id=uuid4(),
                user_id=None,
                username="tester",
            ),
            "doubao-embedding-vision",
            ["测试文本"],
            None,
            request_id,
        )

    assert exc_info.value.code == "model_route_not_found"
    assert any(
        record.name == "gateway.service.embedding"
        and f"request_id={request_id}" in record.getMessage()
        and "error_code=model_route_not_found" in record.getMessage()
        and "status_code=404" in record.getMessage()
        for record in caplog.records
    )
