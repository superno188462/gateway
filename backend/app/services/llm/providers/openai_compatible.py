"""OpenAI Chat Completions 兼容协议适配器。"""

from typing import cast

import httpx

from app.services.llm.configuration import LlmConfigurationService, ResolvedLlmModel
from app.services.llm.domain import (
    ChatMessage,
    LlmProvider,
    ProviderCompletion,
    ProviderParameterError,
)
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

    async def supports(self, model: str) -> bool:
        """确认模型是内置 Mock，或存在可尝试的启用上游连接。"""
        if model == "mock-chat":
            return True
        if self._configuration is None:
            return False
        return bool(model.strip()) and await self._configuration.supports_model(model)

    async def complete(
        self,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        parameters: dict[str, object] | None = None,
    ) -> ProviderCompletion:
        """透传兼容参数并保留供应商响应字段，同时抽取额度结算用量。"""
        if model == "mock-chat":
            return await self._mock.complete(model, messages, max_tokens, parameters)
        if self._configuration is None:
            raise RuntimeError("尚未配置 LLM_PROVIDER_SECRET_KEY")
        routes = await self._configuration.resolve_model_pool(model)
        if not routes:
            raise RuntimeError("模型配置已停用或不存在")
        last_error: Exception | None = None
        for resolved in routes:
            try:
                return await self._complete_with_route(
                    resolved, messages, max_tokens, parameters or {}
                )
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                retryable_status = status_code >= 500 or status_code in {
                    400,
                    401,
                    403,
                    404,
                    408,
                    409,
                    429,
                    422,
                }
                if not retryable_status:
                    raise
                last_error = error
            except httpx.RequestError as error:
                last_error = error
        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response.status_code in {
            400,
            422,
        }:
            raise ProviderParameterError(
                "上游拒绝了请求参数或当前模型不支持这些参数"
            ) from last_error
        raise RuntimeError("LLM 上游连接池中的可用连接均调用失败") from last_error

    async def _complete_with_route(
        self,
        resolved: ResolvedLlmModel,
        messages: list[ChatMessage],
        max_tokens: int,
        parameters: dict[str, object],
    ) -> ProviderCompletion:
        """调用上游；仅覆盖网关公开模型名，其余请求参数保持不变。"""
        payload = dict(parameters)
        payload.pop("model", None)
        payload.pop("messages", None)
        payload.pop("stream", None)
        payload.pop("stream_options", None)
        if not payload.get("max_tokens") and not payload.get("max_completion_tokens"):
            payload["max_tokens"] = max_tokens
        payload["model"] = resolved.upstream_model
        payload["messages"] = [item.as_payload() for item in messages]
        response = await self._http_client.post(
            f"{resolved.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {resolved.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        payload = cast(dict[str, object], response.json())
        choices = cast(list[dict[str, object]], payload["choices"])
        message = cast(dict[str, object], choices[0]["message"])
        content = message.get("content")
        if not isinstance(content, str):
            content = ""
        usage = cast(dict[str, object], payload.get("usage") or {})
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if not isinstance(prompt_tokens, int):
            prompt_tokens = estimate_tokens("\n".join(str(item.content) for item in messages))
        if not isinstance(completion_tokens, int):
            completion_tokens = estimate_tokens(content)
        finish_reason = choices[0].get("finish_reason")
        return ProviderCompletion(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason if isinstance(finish_reason, str) else "stop",
            raw_response=payload,
        )
