"""LLM Mock Provider 与 token 估算器单元测试。"""

import pytest

from app.services.llm.domain import ChatMessage
from app.services.llm.providers.mock import MockLlmProvider, estimate_tokens


def test_token_estimation_counts_cjk_and_latin_words() -> None:
    assert estimate_tokens("你好 world!") == 4


@pytest.mark.asyncio
async def test_mock_provider_returns_deterministic_chat_completion() -> None:
    provider = MockLlmProvider()
    result = await provider.complete("mock-chat", [ChatMessage("user", "你好")], 100)
    assert result.content == "[Mock LLM] 已收到你的消息：你好"
    assert result.prompt_tokens == estimate_tokens("user: 你好")
    assert result.completion_tokens == estimate_tokens(result.content)
    assert result.finish_reason == "stop"
