"""确定性 LLM Mock Provider，供开发和接口联调使用。"""

import re

from app.domain.llm import ChatMessage, ProviderCompletion


class MockLlmProvider:
    """不访问第三方模型，以稳定规则返回结果并估算 token。"""

    async def complete(
        self, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ProviderCompletion:
        """回复最后一条用户消息的摘要式 Mock 文本。"""
        del model
        prompt_text = "\n".join(f"{message.role}: {message.content}" for message in messages)
        latest_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        content = f"[Mock LLM] 已收到你的消息：{latest_user[:500]}" or "[Mock LLM] 请求已收到。"
        completion_tokens = min(estimate_tokens(content), max_tokens)
        content = truncate_to_tokens(content, completion_tokens)
        return ProviderCompletion(
            content=content,
            prompt_tokens=estimate_tokens(prompt_text),
            completion_tokens=estimate_tokens(content),
            finish_reason="length" if estimate_tokens(content) >= max_tokens else "stop",
        )


def estimate_tokens(text: str) -> int:
    """仅供 Mock 用量演示的粗略 tokenizer：汉字逐字，其余按单词/标点计数。"""
    return max(1, len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]", text)))


def truncate_to_tokens(text: str, limit: int) -> str:
    """按 Mock 估算单位截断，避免回复量超过请求上限。"""
    matches = list(re.finditer(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]", text))
    if len(matches) <= limit:
        return text
    return text[: matches[limit - 1].end()]
