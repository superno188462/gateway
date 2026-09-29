"""LLM Provider 边界和与厂商无关的数据类型。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一次聊天请求中的文本消息。"""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    """Provider 生成的文本和 token 用量。"""

    content: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str


class LlmProvider(Protocol):
    """可替换的聊天模型 Provider 端口。"""

    async def supports(self, model: str) -> bool:
        """判断公开模型代码是否已配置且可调用。"""
        ...

    async def complete(
        self, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ProviderCompletion:
        """生成聊天结果；实现应返回本次调用的 token 用量。"""
