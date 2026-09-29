"""Chat Completions 网关输入保留兼容标准及扩展字段。"""

from pydantic import ValidationError

from app.services.llm.api import ChatCompletionRequest


def test_chat_request_accepts_standard_and_future_vendor_fields() -> None:
    request = ChatCompletionRequest.model_validate(
        {
            "model": "public-chat",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                }
            ],
            "temperature": 0.3,
            "top_p": 0.85,
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "response_format": {"type": "json_object"},
            "future_provider_option": {"enabled": True},
        }
    )

    assert request.temperature == 0.3
    assert request.top_p == 0.85
    assert request.model_extra == {"future_provider_option": {"enabled": True}}
    assert request.messages[0].model_extra is not None
    assert "tool_calls" in request.messages[0].model_extra


def test_stream_options_reject_unsupported_or_non_streaming_usage() -> None:
    base = {"model": "public-chat", "messages": [{"role": "user", "content": "hi"}]}

    try:
        ChatCompletionRequest.model_validate(
            {**base, "stream": True, "stream_options": {"include_obfuscation": True}}
        )
    except ValidationError as error:
        assert "Extra inputs are not permitted" in str(error)
    else:
        raise AssertionError("unsupported stream_options must not be silently ignored")

    try:
        ChatCompletionRequest.model_validate({**base, "stream_options": {"include_usage": True}})
    except ValidationError as error:
        assert "stream=true" in str(error)
    else:
        raise AssertionError("stream_options without stream=true must be rejected")
