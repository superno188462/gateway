"""LLM Provider 边界和与厂商无关的数据类型。"""

from dataclasses import dataclass, field
from typing import Protocol


class ProviderParameterError(RuntimeError):
    """上游拒绝某项调用参数，常见原因是模型不支持该字段或取值。"""


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
