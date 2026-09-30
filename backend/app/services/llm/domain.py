"""LLM Provider 边界和与厂商无关的数据类型。"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Protocol


class ProviderParameterError(RuntimeError):
    """上游拒绝某项调用参数，常见原因是模型不支持该字段或取值。"""


class ProviderCallError(ProviderParameterError):
    """上游连接池调用失败，携带可供排障的脱敏诊断信息。"""

    def __init__(
        self,
        code: str,
        message: str,
        diagnostics: dict[str, object],
        *,
        parameter_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics
        self.parameter_error = parameter_error


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一次聊天消息及供应商定义的标准扩展字段。"""

    role: str
    content: object
    fields: dict[str, object] = field(default_factory=dict)

    def as_payload(self) -> dict[str, object]:
        """序列化消息而不丢弃 tool_call_id、name 等兼容字段。"""
        return {"role": self.role, "content": self.content, **self.fields}


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    """Provider 的归一化用量及可选的原始兼容响应。"""

    content: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    raw_response: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ProviderStreamEvent:
    """上游流状态或数据帧；头就绪、普通 SSE 帧和 [DONE] 通过标志区分。"""

    data: dict[str, object] | None
    headers_received: bool = False
    done: bool = False


class LlmProvider(Protocol):
    """可替换的聊天模型 Provider 端口。"""

    async def supports(self, model: str) -> bool:
        """判断公开模型代码是否已配置且可调用。"""
        ...

    async def complete(
        self,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        parameters: dict[str, object] | None = None,
    ) -> ProviderCompletion:
        """生成聊天结果；保留兼容参数，并返回本次调用的 token 用量。"""
        ...

    def stream(
        self,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        parameters: dict[str, object] | None = None,
        request_id: str | None = None,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        """逐帧读取 OpenAI SSE；首帧前的连接或状态错误允许切换上游。"""
        ...
