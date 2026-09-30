"""OpenAI 兼容 Provider 把模型请求映射并归一化响应。"""

import asyncio
import json

import httpx
import pytest

from app.services.llm.configuration import ResolvedLlmModel
from app.services.llm.domain import ChatMessage, ProviderCallError, ProviderParameterError
from app.services.llm.providers.openai_compatible import ConfiguredLlmProvider


class FakeConfiguration:
    """固定返回优先级调用池的测试上游连接。"""

    async def has_active_providers(self) -> bool:
        return True

    async def supports_model(self, model: str) -> bool:
        return bool(model)

    async def resolve_model_pool(self, model_code: str) -> tuple[ResolvedLlmModel, ...]:
        return (
            ResolvedLlmModel(
                model_code=model_code,
                upstream_model=model_code,
                base_url="https://llm.example.test/v1",
                api_key="upstream-secret",
            ),
        )


@pytest.mark.asyncio
async def test_openai_compatible_provider_forwards_model_and_normalizes_usage() -> None:
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
        assert await provider.supports("any-vendor-model-name")
        result = await provider.complete("public-chat", [ChatMessage("user", "hi")], 30)

    assert seen["url"] == "https://llm.example.test/v1/chat/completions"
    assert seen["authorization"] == "Bearer upstream-secret"
    assert json.loads(str(seen["payload"]))["model"] == "public-chat"
    assert result.content == "hello"
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 2
    assert result.finish_reason == "stop"
    assert result.raw_response is not None
    assert result.raw_response["usage"] == {"prompt_tokens": 3, "completion_tokens": 2}


@pytest.mark.asyncio
async def test_provider_uses_resolved_model_after_prefix_route_selection() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["model"] = json.loads(request.read())["model"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    class PrefixConfiguration(FakeConfiguration):
        async def resolve_model_pool(self, model_code: str) -> tuple[ResolvedLlmModel, ...]:
            prefix, separator, upstream_model = model_code.partition("/")
            if prefix != "volc" or not separator:
                return ()
            return (
                ResolvedLlmModel(
                    model_code=model_code,
                    upstream_model=upstream_model,
                    base_url="https://volc.example.test/v1",
                    api_key="volc-key",
                ),
            )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredLlmProvider(PrefixConfiguration(), client)  # type: ignore[arg-type]
        await provider.complete("volc/doubao-seed", [ChatMessage("user", "hi")], 8)

    assert seen["model"] == "doubao-seed"


@pytest.mark.asyncio
async def test_provider_forwards_standard_and_vendor_fields_and_preserves_tool_response() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "id": "upstream-id",
                "object": "chat.completion",
                "model": "vendor-chat-v2",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
                "vendor_metadata": {"region": "test"},
            },
        )

    options = {
        "temperature": 0.2,
        "top_p": 0.8,
        "response_format": {"type": "json_object"},
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        "tool_choice": "auto",
        "vendor_extension": {"trace": True},
    }
    messages = [ChatMessage("user", "find it", {"name": "caller"})]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredLlmProvider(FakeConfiguration(), client)  # type: ignore[arg-type]
        result = await provider.complete("public-chat", messages, 30, options)

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.8
    assert payload["response_format"] == options["response_format"]
    assert payload["tools"] == options["tools"]
    assert payload["tool_choice"] == "auto"
    assert payload["vendor_extension"] == {"trace": True}
    assert payload["messages"] == [{"role": "user", "content": "find it", "name": "caller"}]
    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    assert result.raw_response is not None
    assert result.raw_response["vendor_metadata"] == {"region": "test"}


@pytest.mark.asyncio
async def test_provider_reports_unsupported_parameter_status() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json={"error": "unsupported"}))
    ) as client:
        provider = ConfiguredLlmProvider(FakeConfiguration(), client)  # type: ignore[arg-type]
        with pytest.raises(ProviderParameterError):
            await provider.complete("public-chat", [ChatMessage("user", "hi")], 30)


@pytest.mark.asyncio
async def test_provider_failure_exposes_safe_attempt_diagnostics() -> None:
    class DiagnosticPoolConfiguration(FakeConfiguration):
        async def resolve_model_pool(self, model_code: str) -> tuple[ResolvedLlmModel, ...]:
            return (
                ResolvedLlmModel(
                    model_code=model_code,
                    upstream_model=model_code,
                    base_url="https://first.example.test/v1",
                    api_key="secret-one",
                    connection_name="Primary",
                    supplier_name="Vendor A",
                ),
                ResolvedLlmModel(
                    model_code=model_code,
                    upstream_model=model_code,
                    base_url="https://second.example.test/v1",
                    api_key="secret-two",
                    connection_name="Backup",
                    supplier_name="Vendor B",
                ),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "first.example.test":
            return httpx.Response(
                503,
                json={"error": {"type": "server_error", "message": "private body"}},
                headers={"x-request-id": "upstream-first"},
            )
        return httpx.Response(
            404,
            json={"error": {"code": "model_not_found", "message": "private body"}},
            headers={"x-request-id": "upstream-second"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredLlmProvider(DiagnosticPoolConfiguration(), client)  # type: ignore[arg-type]
        with pytest.raises(ProviderCallError) as captured:
            await provider.complete("public-chat", [ChatMessage("user", "do not log this")], 30)

    error = captured.value
    attempts = error.diagnostics["attempts"]
    assert error.code == "upstream_http_404"
    assert isinstance(attempts, list)
    assert attempts[0]["connection_name"] == "Primary"
    assert attempts[0]["upstream_status_code"] == 503
    assert attempts[1]["supplier_name"] == "Vendor B"
    assert attempts[1]["provider_error"] == "model_not_found"
    assert "private body" not in str(error.diagnostics)
    assert "secret-one" not in str(error.diagnostics)


@pytest.mark.asyncio
async def test_provider_pool_tries_connections_in_priority_order_on_each_request() -> None:
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
                    upstream_model=model_code,
                    base_url="https://first.example.test/v1",
                    api_key="first-key",
                ),
                ResolvedLlmModel(
                    model_code=model_code,
                    upstream_model=model_code,
                    base_url="https://second.example.test/v1",
                    api_key="second-key",
                ),
            )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredLlmProvider(PoolConfiguration(), client)  # type: ignore[arg-type]
        await provider.complete("public-chat", [ChatMessage("user", "hi")], 30)
        await provider.complete("public-chat", [ChatMessage("user", "hi")], 30)

    assert requests == [
        "first.example.test",
        "second.example.test",
        "first.example.test",
        "second.example.test",
    ]


@pytest.mark.asyncio
async def test_openai_compatible_provider_keeps_mock_available() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ConfiguredLlmProvider(None, client)
        assert await provider.supports("mock-chat")
        result = await provider.complete("mock-chat", [ChatMessage("user", "hi")], 30)

    assert result.content.startswith("[Mock LLM]")


@pytest.mark.asyncio
async def test_provider_streams_first_sse_frame_before_upstream_finishes() -> None:
    release_stream = asyncio.Event()
    seen: dict[str, object] = {}

    class DelayedSseBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b'data: {"id":"upstream-id","model":"vendor-model",'
                b'"choices":[{"index":0,"delta":{"content":"first"},'
                b'"finish_reason":null}]}\n\n'
            )
            await release_stream.wait()
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=DelayedSseBody(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredLlmProvider(FakeConfiguration(), client)  # type: ignore[arg-type]
        stream = provider.stream(
            "public-chat",
            [ChatMessage("user", "hello")],
            20,
            {"temperature": 0.3},
            request_id="trace-stream-test",
        )
        headers = await asyncio.wait_for(anext(stream), timeout=1)
        first = await asyncio.wait_for(anext(stream), timeout=1)
        assert headers.headers_received is True
        assert first.data is not None
        payload = seen["payload"]
        assert isinstance(payload, dict)
        assert payload["stream"] is True
        first_choices = first.data["choices"]
        assert isinstance(first_choices, list)
        assert first_choices[0]["delta"]["content"] == "first"  # type: ignore[index]

        release_stream.set()
        done = await anext(stream)
        assert done.done is True
        await stream.aclose()


@pytest.mark.asyncio
async def test_provider_logs_transport_phases_and_connection_reuse(caplog) -> None:
    class TracedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            trace = request.extensions["trace"]
            for phase in (
                "http11.connect_tcp",
                "http11.start_tls",
                "http11.send_request_headers",
                "http11.send_request_body",
                "http11.receive_response_headers",
            ):
                await trace(f"{phase}.started", {})
                await trace(f"{phase}.complete", {})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                    b"data: [DONE]\n\n"
                ),
                request=request,
            )

    caplog.set_level("INFO", logger="gateway.llm.upstream")
    async with httpx.AsyncClient(transport=TracedTransport()) as client:
        provider = ConfiguredLlmProvider(FakeConfiguration(), client)  # type: ignore[arg-type]
        stream = provider.stream("public-chat", [ChatMessage("user", "hi")], 20)
        await anext(stream)  # upstream headers
        await anext(stream)  # first SSE content frame
        await anext(stream)  # upstream [DONE]
        await stream.aclose()

    timing = next(
        record.message for record in caplog.records if "upstream_transport_timing" in record.message
    )
    assert "connection_reused=False" in timing
    assert "dns_ms=not_separately_exposed" in timing
    assert "dns_tcp_ms=" in timing
    assert "tls_ms=" in timing
    assert "send_headers_ms=" in timing
    assert "send_body_ms=" in timing
    assert "response_headers_ms=" in timing
    assert "pool_wait_ms=unavailable" in timing
