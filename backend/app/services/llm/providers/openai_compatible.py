"""OpenAI Chat Completions 兼容协议适配器。"""

from typing import cast

import httpx

from app.services.llm.configuration import LlmConfigurationService, ResolvedLlmModel
from app.services.llm.domain import ChatMessage, LlmProvider, ProviderCompletion
from app.services.llm.providers.mock import MockLlmProvider, estimate_tokens


class ConfiguredLlmProvider(LlmProvider):
    """按管理员配置动态路由到 OpenAI-compatible 上游，保留内置 Mock。"""

    def __init__(
        self,
        configuration: LlmConfigurationService | None,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._configuration = configuration
        self._http_client = http_client
        self._mock = MockLlmProvider()
        self._next_index: dict[str, int] = {}

    async def supports(self, model: str) -> bool:
        """确认模型是内置 Mock 或启用的供应商模型。"""
        if model == "mock-chat":
            return True
        if self._configuration is None:
            return False
        return model in await self._configuration.active_model_codes()

    async def complete(
        self, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ProviderCompletion:
        """调用指定兼容端点并归一化 completion 与 token 用量。"""
        if model == "mock-chat":
            return await self._mock.complete(model, messages, max_tokens)
        if self._configuration is None:
            raise RuntimeError("尚未配置 LLM_PROVIDER_SECRET_KEY")
        routes = await self._configuration.resolve_model_pool(model)
        if not routes:
            raise RuntimeError("模型配置已停用或不存在")
        start = self._next_index.get(model, 0) % len(routes)
        self._next_index[model] = start + 1
        candidates = [
            routes[(start + offset) % len(routes)] for offset in range(min(3, len(routes)))
        ]
        last_error: Exception | None = None
        for resolved in candidates:
            try:
                return await self._complete_with_route(resolved, messages, max_tokens)
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500 and error.response.status_code not in {
                    401,
                    403,
                    404,
                    408,
                    409,
                    429,
                }:
                    raise
                last_error = error
            except httpx.RequestError as error:
                last_error = error
        raise RuntimeError("LLM 上游连接池中的可用连接均调用失败") from last_error

    async def _complete_with_route(
        self,
        resolved: ResolvedLlmModel,
        messages: list[ChatMessage],
        max_tokens: int,
    ) -> ProviderCompletion:
        """调用池中的一个上游连接并归一化响应。"""
        response = await self._http_client.post(
            f"{resolved.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {resolved.api_key}"},
            json={
                "model": resolved.upstream_model,
                "messages": [{"role": item.role, "content": item.content} for item in messages],
                "max_tokens": max_tokens,
            },
        )
        response.raise_for_status()
        payload = cast(dict[str, object], response.json())
        choices = cast(list[dict[str, object]], payload["choices"])
        message = cast(dict[str, object], choices[0]["message"])
        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError("上游响应未包含文本内容")
        usage = cast(dict[str, object], payload.get("usage") or {})
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if not isinstance(prompt_tokens, int):
            prompt_tokens = estimate_tokens("\n".join(item.content for item in messages))
        if not isinstance(completion_tokens, int):
            completion_tokens = estimate_tokens(content)
        finish_reason = choices[0].get("finish_reason")
        return ProviderCompletion(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason if isinstance(finish_reason, str) else "stop",
        )
