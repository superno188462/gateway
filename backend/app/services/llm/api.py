"""对外提供 OpenAI 风格的 LLM Mock Chat Completions API。"""

import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.api_keys import ApiKeyInvalidError, ApiKeyService, VerifiedApiKey
from app.container import bearer_scheme, get_api_key_service, get_llm_gateway_service
from app.services.llm.application import GatewayCompletion, GatewayRequestError, LlmGatewayService
from app.services.llm.domain import ChatMessage

router = APIRouter(tags=["LLM Gateway"])


class ChatMessageRequest(BaseModel):
    """兼容 Chat Completions 的消息，允许 tool 和多模态等扩展字段。"""

    model_config = ConfigDict(extra="allow")

    role: str = Field(min_length=1, max_length=32, description="消息角色。")
    content: Any = Field(description="消息内容；文本、多模态数组或供应商兼容格式。")


class StreamOptionsRequest(BaseModel):
    """网关支持的流式选项；供应商标准中尚未实现的选项会被拒绝。"""

    model_config = ConfigDict(extra="forbid")

    include_usage: bool | None = Field(
        default=None,
        description="为 true 时在 SSE 结束前发送 token 用量 chunk。",
    )


class ChatCompletionRequest(BaseModel):
    """网关 Chat Completions 请求；额外标准和供应商兼容参数会透传。"""

    model_config = ConfigDict(extra="allow")

    model: str = Field(
        min_length=1,
        max_length=200,
        description="模型标识；供应商连接原样接收此名称，必须填写供应商支持的模型名。",
    )
    messages: list[ChatMessageRequest] = Field(min_length=1, max_length=100)
    max_tokens: int | None = Field(
        default=None, ge=1, le=4096, description="兼容旧参数并用于额度预留。"
    )
    max_completion_tokens: int | None = Field(
        default=None, ge=1, le=4096, description="生成 token 上限，也用于额度预留。"
    )
    temperature: float | None = Field(
        default=None, ge=0, le=2, description="采样温度；由上游模型决定是否支持。"
    )
    top_p: float | None = Field(
        default=None, gt=0, le=1, description="核采样参数；由上游模型决定是否支持。"
    )
    presence_penalty: float | None = Field(
        default=None, ge=-2, le=2, description="Presence penalty；具体支持范围以供应商为准。"
    )
    frequency_penalty: float | None = Field(
        default=None, ge=-2, le=2, description="Frequency penalty；具体支持范围以供应商为准。"
    )
    stop: str | list[str] | None = Field(default=None, description="停止生成序列；按原样转发。")
    response_format: dict[str, Any] | None = Field(
        default=None, description="如 json_object 或 json_schema；按原样转发给上游。"
    )
    tools: list[dict[str, Any]] | None = Field(
        default=None, description="OpenAI function tools 定义；按原样转发给上游。"
    )
    tool_choice: str | dict[str, Any] | None = Field(
        default=None, description="工具选择策略；按原样转发。"
    )
    parallel_tool_calls: bool | None = Field(default=None, description="是否允许并行工具调用。")
    logit_bias: dict[str, float] | None = Field(default=None, description="token 概率偏置映射。")
    logprobs: bool | None = Field(default=None, description="是否返回 token 对数概率。")
    top_logprobs: int | None = Field(
        default=None, ge=0, le=20, description="返回的候选 token 概率数量。"
    )
    seed: int | None = Field(default=None, description="尽可能确定性采样的随机种子。")
    user: str | None = Field(default=None, description="可选的调用方终端用户标识。")
    stream: bool = Field(default=False, description="是否以 Server-Sent Events 返回。")
    stream_options: StreamOptionsRequest | None = Field(
        default=None, description="流式响应选项；支持 include_usage。"
    )
    n: int | None = Field(default=None, ge=1, le=8, description="生成选择数量；最多 8 个。")

    @model_validator(mode="after")
    def validate_total_content_length(self) -> "ChatCompletionRequest":
        """限制完整请求的提示词规模，避免单个 Key 提交超大消息集合。"""
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options 只能与 stream=true 一起使用")
        content_size = sum(
            len(json.dumps(message.model_dump(exclude_unset=True), ensure_ascii=False))
            for message in self.messages
        )
        if content_size > 200_000:
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
            [
                ChatMessage(
                    role=item.role,
                    content=item.content,
                    fields=item.model_dump(exclude={"role", "content"}, exclude_unset=True),
                )
                for item in payload.messages
            ],
            payload.max_completion_tokens or payload.max_tokens or 256,
            cast(
                dict[str, object],
                payload.model_dump(
                    exclude={"model", "messages", "stream", "stream_options"},
                    exclude_unset=True,
                ),
            ),
        )
    except GatewayRequestError as error:
        return openai_error(error.status_code, error.code, str(error), error.request_id)


@router.post(
    "/v1/chat/completions",
    summary="调用 OpenAI 兼容 Chat Completions 网关",
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
        stream_options = (
            cast(dict[str, bool], payload.stream_options.model_dump(exclude_unset=True))
            if payload.stream_options is not None
            else {}
        )
        return StreamingResponse(
            _stream_response(result, stream_options),
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
    if completion.raw_response is not None:
        body = dict(completion.raw_response)
        body["model"] = result.model
        return JSONResponse(headers={"x-request-id": result.request_id}, content=body)
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
    """将完整 Provider 结果编码为兼容 SSE chunks，并以 [DONE] 结束。"""
    completion = result.completion
    common = {
        "id": result.request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": result.model,
    }
    raw_choices = completion.raw_response.get("choices") if completion.raw_response else None
    if isinstance(raw_choices, list):
        for raw_choice in raw_choices:
            if not isinstance(raw_choice, dict):
                continue
            first = {
                **common,
                "choices": [
                    {
                        "index": raw_choice.get("index", 0),
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
            raw_message = raw_choice.get("message")
            if isinstance(raw_message, dict):
                delta = {key: value for key, value in raw_message.items() if key != "role"}
                choice_index = raw_choice.get("index", 0)
                delta_chunk = {
                    **common,
                    "choices": [{"index": choice_index, "delta": delta, "finish_reason": None}],
                }
                yield f"data: {json.dumps(delta_chunk, ensure_ascii=False)}\n\n"
            finish_chunk = {
                **common,
                "choices": [
                    {
                        "index": raw_choice.get("index", 0),
                        "delta": {},
                        "finish_reason": raw_choice.get("finish_reason", "stop"),
                    }
                ],
            }
            yield f"data: {json.dumps(finish_chunk, ensure_ascii=False)}\n\n"
        if stream_options.get("include_usage"):
            raw_usage = completion.raw_response.get("usage") if completion.raw_response else None
            usage_chunk: dict[str, object] = {**common, "choices": []}
            usage_chunk["usage"] = raw_usage or {
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "total_tokens": completion.prompt_tokens + completion.completion_tokens,
            }
            yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
    else:
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
                    {
                        "index": 0,
                        "delta": {"content": text[start : start + 24]},
                        "finish_reason": None,
                    }
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
