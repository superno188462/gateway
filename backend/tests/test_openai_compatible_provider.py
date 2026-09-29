"""OpenAI 兼容 Provider 把模型请求映射并归一化响应。"""

import json

import httpx
import pytest

from app.services.llm.configuration import ResolvedLlmModel
from app.services.llm.domain import ChatMessage
from app.services.llm.providers.openai_compatible import ConfiguredLlmProvider


class FakeConfiguration:
    """固定返回一组公开模型的测试上游连接。"""

    async def active_model_codes(self) -> tuple[str, ...]:
        return ("public-chat",)

    async def resolve_model_pool(self, model_code: str) -> tuple[ResolvedLlmModel, ...]:
        if model_code != "public-chat":
            return ()
        return (
            ResolvedLlmModel(
                model_code=model_code,
                upstream_model="vendor-chat-v2",
                base_url="https://llm.example.test/v1",
                api_key="upstream-secret",
            ),
        )


@pytest.mark.asyncio
async def test_openai_compatible_provider_maps_model_and_normalizes_usage() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["payload"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredLlmProvider(FakeConfiguration(), client)  # type: ignore[arg-type]
        assert await provider.supports("public-chat")
        result = await provider.complete("public-chat", [ChatMessage("user", "hi")], 30)

    assert seen["url"] == "https://llm.example.test/v1/chat/completions"
    assert seen["authorization"] == "Bearer upstream-secret"
    assert json.loads(str(seen["payload"]))["model"] == "vendor-chat-v2"
    assert result.content == "hello"
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 2
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_provider_pool_fails_over_and_rotates_starting_connection() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        requests.append(host)
        if host == "first.example.test":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    class PoolConfiguration(FakeConfiguration):
        async def resolve_model_pool(self, model_code: str) -> tuple[ResolvedLlmModel, ...]:
            return (
                ResolvedLlmModel(
                    model_code=model_code,
                    upstream_model="first-model",
                    base_url="https://first.example.test/v1",
                    api_key="first-key",
                ),
                ResolvedLlmModel(
                    model_code=model_code,
                    upstream_model="second-model",
                    base_url="https://second.example.test/v1",
                    api_key="second-key",
                ),
            )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredLlmProvider(PoolConfiguration(), client)  # type: ignore[arg-type]
        await provider.complete("public-chat", [ChatMessage("user", "hi")], 30)
        await provider.complete("public-chat", [ChatMessage("user", "hi")], 30)

    assert requests == ["first.example.test", "second.example.test", "second.example.test"]


@pytest.mark.asyncio
async def test_openai_compatible_provider_keeps_mock_available() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ConfiguredLlmProvider(None, client)
        assert await provider.supports("mock-chat")
        result = await provider.complete("mock-chat", [ChatMessage("user", "hi")], 30)

    assert result.content.startswith("[Mock LLM]")
