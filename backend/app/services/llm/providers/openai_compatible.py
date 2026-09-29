"""OpenAI Chat Completions 兼容协议适配器。"""

import logging
from typing import cast

import httpx

from app.services.llm.configuration import LlmConfigurationService, ResolvedLlmModel
from app.services.llm.domain import (
    ChatMessage,
    LlmProvider,
    ProviderCallError,
    ProviderCompletion,
)
from app.services.llm.providers.mock import MockLlmProvider, estimate_tokens

logger = logging.getLogger("gateway.llm.upstream")


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
            raise ProviderCallError(
                "provider_configuration_missing",
                "LLM 上游配置不可用，请检查服务端密钥配置",
                {"attempts": []},
            )
        routes = await self._configuration.resolve_model_pool(model)
        if not routes:
            raise ProviderCallError(
                "model_route_not_found",
                "没有可用的上游连接，请检查模型前缀和连接状态",
                {"attempts": []},
            )
        attempts: list[dict[str, object]] = []
        for resolved in routes:
            try:
                return await self._complete_with_route(
                    resolved, messages, max_tokens, parameters or {}
                )
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                attempt: dict[str, object] = {
                    "connection_name": resolved.connection_name,
                    "supplier_name": resolved.supplier_name,
                    "failure_type": "http_status",
                    "upstream_status_code": status_code,
                    "upstream_request_id": self._upstream_request_id(error.response),
                }
                provider_error = self._provider_error_code(error.response)
                if provider_error:
                    attempt["provider_error"] = provider_error
                attempts.append(attempt)
                logger.warning(
                    "upstream_attempt_failed supplier=%s connection=%s "
                    "failure_type=http_status status=%d upstream_request_id=%s "
                    "provider_error=%s",
                    resolved.supplier_name or "unknown",
                    resolved.connection_name or "unnamed",
                    status_code,
                    attempt.get("upstream_request_id") or "-",
                    provider_error or "-",
                )
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
                    raise self._call_error(status_code, attempts) from error
            except httpx.RequestError as error:
                failure_type = (
                    "timeout"
                    if isinstance(error, httpx.TimeoutException)
                    else "connection_error"
                    if isinstance(error, httpx.ConnectError)
                    else "protocol_error"
                    if isinstance(error, httpx.ProtocolError)
                    else "request_error"
                )
                attempts.append(
                    {
                        "connection_name": resolved.connection_name,
                        "supplier_name": resolved.supplier_name,
                        "failure_type": failure_type,
                    }
                )
                logger.warning(
                    "upstream_attempt_failed supplier=%s connection=%s failure_type=%s",
                    resolved.supplier_name or "unknown",
                    resolved.connection_name or "unnamed",
                    failure_type,
                )
        raise self._call_error(None, attempts)

    @staticmethod
    def _upstream_request_id(response: httpx.Response) -> str | None:
        for header in ("x-request-id", "request-id", "x-client-request-id"):
            value: str | None = response.headers.get(header)
            if value:
                return value[:200]
        return None

    @staticmethod
    def _provider_error_code(response: httpx.Response) -> str | None:
        try:
            payload = cast(object, response.json())
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        payload_dict = cast(dict[str, object], payload)
        error = payload_dict.get("error")
        if isinstance(error, dict):
            error_dict = cast(dict[str, object], error)
            value = error_dict.get("code") or error_dict.get("type")
        else:
            value = payload_dict.get("code")
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 100:
            return None
        if not all(character.isalnum() or character in "._-" for character in normalized):
            return None
        return normalized

    @staticmethod
    def _call_error(
        status_code: int | None, attempts: list[dict[str, object]]
    ) -> ProviderCallError:
        details: dict[str, object] = {"attempts": attempts}
        if status_code is None and attempts:
            last_status = attempts[-1].get("upstream_status_code")
            if isinstance(last_status, int):
                status_code = last_status
        if status_code in {400, 422}:
            return ProviderCallError(
                f"upstream_http_{status_code}",
                f"上游拒绝请求参数（HTTP {status_code}），请检查参数和模型支持情况",
                details,
                parameter_error=True,
            )
        if status_code is not None:
            return ProviderCallError(
                f"upstream_http_{status_code}",
                f"所有可用上游连接均失败，最后一个上游返回 HTTP {status_code}",
                details,
            )
        last_attempt = attempts[-1] if attempts else {}
        failure_type = last_attempt.get("failure_type")
        code = {
            "timeout": "upstream_timeout",
            "connection_error": "upstream_connection_error",
            "protocol_error": "upstream_protocol_error",
        }.get(str(failure_type), "upstream_request_error")
        message = {
            "timeout": "连接上游超时，请检查上游响应时间和网关超时设置",
            "connection_error": "无法连接上游，请检查 Base URL、DNS 和网络策略",
            "protocol_error": "上游连接协议异常，请检查 HTTPS/TLS 配置",
        }.get(str(failure_type), "上游请求失败，请查看各连接的诊断信息")
        return ProviderCallError(code, message, details)

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
