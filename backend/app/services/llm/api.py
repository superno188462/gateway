"""对外提供 OpenAI 风格的 LLM Mock Chat Completions API。"""

import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, model_validator

from app.application.api_keys import ApiKeyInvalidError, ApiKeyService, VerifiedApiKey
from app.container import bearer_scheme, get_api_key_service, get_llm_gateway_service
from app.services.llm.application import GatewayCompletion, GatewayRequestError, LlmGatewayService
from app.services.llm.domain import ChatMessage

router = APIRouter(tags=["LLM Gateway"])


class ChatMessageRequest(BaseModel):
    """兼容 OpenAI chat completions 的单条文本消息。"""

    role: Literal["system", "user", "assistant"] = Field(description="消息角色。")
    content: str = Field(
        min_length=1, max_length=100_000, description="文本内容；服务端不持久化正文。"
    )


class ChatCompletionRequest(BaseModel):
    """LLM Mock 聊天请求。"""

    model: str = Field(description="服务模型标识，例如 mock-chat。")
    messages: list[ChatMessageRequest] = Field(min_length=1, max_length=100)
    max_tokens: int = Field(
        default=256, ge=1, le=4096, description="本请求生成上限，也用于额度预留。"
    )
    stream: bool = Field(default=False, description="是否以 Server-Sent Events 返回。")
    stream_options: dict[str, bool] | None = Field(
        default=None, description="兼容 OpenAI 的 stream_options；支持 include_usage。"
    )

    @model_validator(mode="after")
    def validate_total_content_length(self) -> "ChatCompletionRequest":
        """限制完整请求的提示词规模，避免单个 Key 提交超大消息集合。"""
        if sum(len(message.content) for message in self.messages) > 200_000:
            raise ValueError("messages 内容总长度不能超过 200,000 个字符")
        return self


def openai_error(
    status_code: int, code: str, message: str, request_id: str | None = None
) -> JSONResponse:
    """返回 OpenAI 风格错误体，不暴露数据库或内部异常。"""
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else {}
    if request_id:
        headers["x-request-id"] = request_id
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "message": message,
                "type": "gateway_error",
                "code": code,
                "request_id": request_id,
            }
        },
    )


async def _invoke(
    payload: ChatCompletionRequest,
    key: VerifiedApiKey,
    service: LlmGatewayService,
) -> GatewayCompletion | JSONResponse:
    try:
        return await service.complete(
            key,
            payload.model,
            [ChatMessage(role=item.role, content=item.content) for item in payload.messages],
            payload.max_tokens,
        )
    except GatewayRequestError as error:
        return openai_error(error.status_code, error.code, str(error), error.request_id)


@router.post(
    "/v1/chat/completions",
    summary="调用 LLM Mock Chat Completions",
    response_model=None,
)
async def chat_completions(
    payload: ChatCompletionRequest,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    gateway: Annotated[LlmGatewayService, Depends(get_llm_gateway_service)],
) -> JSONResponse | StreamingResponse:
    """通过项目 API Key 自动解析项目并执行额度校验；请求正文不被保存。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return openai_error(401, "invalid_api_key", "缺少 Bearer API Key")
    try:
        key = await key_service.verify(credentials.credentials)
    except ApiKeyInvalidError:
        return openai_error(401, "invalid_api_key", "API Key 无效、已撤销或已过期")

    result = await _invoke(payload, key, gateway)
    if isinstance(result, JSONResponse):
        return result
    if payload.stream:
        return StreamingResponse(
            _stream_response(result, payload.stream_options or {}),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "x-request-id": result.request_id,
            },
        )
    return _json_response(result)


def _json_response(result: GatewayCompletion) -> JSONResponse:
    completion = result.completion
    return JSONResponse(
        headers={"x-request-id": result.request_id},
        content={
            "id": result.request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": completion.content},
                    "finish_reason": completion.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "total_tokens": completion.prompt_tokens + completion.completion_tokens,
            },
        },
    )


async def _stream_response(
    result: GatewayCompletion, stream_options: dict[str, bool]
) -> AsyncIterator[str]:
    """返回兼容 Chat Completions 的 SSE chunks，并以 [DONE] 结束。"""
    completion = result.completion
    common = {
        "id": result.request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": result.model,
    }
    first = {
        **common,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
    text = completion.content
    for start in range(0, len(text), 24):
        chunk = {
            **common,
            "choices": [
                {"index": 0, "delta": {"content": text[start : start + 24]}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    last: dict[str, object] = {
        **common,
        "choices": [{"index": 0, "delta": {}, "finish_reason": completion.finish_reason}],
    }
    if stream_options.get("include_usage"):
        last["usage"] = {
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "total_tokens": completion.prompt_tokens + completion.completion_tokens,
        }
    yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
