"""OpenAI 兼容文本向量接口。"""

import logging
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.application.api_keys import ApiKeyInvalidError, ApiKeyService, VerifiedApiKey
from app.container import (
    bearer_scheme,
    get_api_key_service,
    get_embedding_gateway_service,
    get_request_recorder,
)
from app.request_logging.application import GatewayRequestRecorder
from app.services.embedding.application import EmbeddingGatewayError, EmbeddingGatewayService
from app.services.embedding.multimodal import EmbeddingInput

router = APIRouter(tags=["Embedding Gateway"])
logger = logging.getLogger("gateway.embedding.auth")


class EmbeddingRequest(BaseModel):
    """OpenAI 文本输入及网关约定的多模态输入。"""

    model_config = ConfigDict(extra="allow")
    model: str = Field(min_length=1, max_length=200)
    input: str | list[str | EmbeddingInput] = Field(
        description=(
            "文本可传字符串或字符串数组；火山多模态模型传结构化对象数组，"
            "每个请求生成一个向量。"
        )
    )
    encoding_format: str = Field(default="float", description="目前仅支持 float。")
    dimensions: int | None = Field(default=None, ge=1, le=65536)
    user: str | None = Field(default=None)

    @field_validator("input")
    @classmethod
    def validate_input(
        cls, value: str | list[str | EmbeddingInput]
    ) -> str | list[str | EmbeddingInput]:
        values = [value] if isinstance(value, str) else value
        if not values or len(values) > 64:
            raise ValueError("input 必须包含 1 到 64 个内容项")
        if all(isinstance(item, str) for item in values):
            if any(not item.strip() for item in values if isinstance(item, str)):
                raise ValueError("input 文本不能为空")
            if sum(len(item) for item in values if isinstance(item, str)) > 200_000:
                raise ValueError("input 文本总长度不能超过 200,000 个字符")
        elif any(isinstance(item, str) for item in values):
            raise ValueError("文本批量 input 不能与多模态对象混用")
        return value


def openai_error(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else {}
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


@router.post("/v1/embeddings", summary="创建文本或多模态嵌入向量", response_model=None)
async def create_embeddings(
    payload: EmbeddingRequest,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    gateway: Annotated[EmbeddingGatewayService, Depends(get_embedding_gateway_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> JSONResponse:
    """以项目 API Key 鉴权，遵循 OpenAI Embeddings 响应结构并记录 token 用量。"""
    request_id = request.state.trace_id or f"trace_{uuid4().hex}"
    if credentials is None or credentials.scheme.lower() != "bearer":
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code=EmbeddingGatewayService.service_code,
            latency_ms=0,
            error_code="missing_api_key",
        )
        return openai_error(401, "invalid_api_key", "缺少 Bearer API Key", request_id)
    try:
        key: VerifiedApiKey = await key_service.verify(credentials.credentials)
    except ApiKeyInvalidError:
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code=EmbeddingGatewayService.service_code,
            latency_ms=0,
            error_code="invalid_api_key",
        )
        return openai_error(401, "invalid_api_key", "API Key 无效、已撤销或已过期", request_id)
    raw_inputs = [payload.input] if isinstance(payload.input, str) else payload.input
    inputs: list[str | dict[str, object]] = [
        item if isinstance(item, str) else item.model_dump(mode="json", exclude_none=True)
        for item in raw_inputs
    ]
    if payload.encoding_format != "float":
        return openai_error(
            422, "unsupported_encoding_format", "目前仅支持 encoding_format=float", request_id
        )
    try:
        response = await gateway.embed(
            key,
            payload.model,
            inputs,
            payload.model_dump(
                exclude={"model", "input", "user", "encoding_format"}, exclude_unset=True
            ),
            request_id,
        )
    except EmbeddingGatewayError as error:
        logger.error(
            "embedding_request_failed request_id=%s error_code=%s status_code=%d message=%s",
            request_id,
            error.code,
            error.status_code,
            str(error),
        )
        return openai_error(error.status_code, error.code, str(error), error.request_id)
    except Exception:
        # 兜底收敛数据库、额度或其他未预期异常，避免 Swagger 只看到裸 500。
        logger.exception("embedding_request_unhandled request_id=%s", request_id)
        try:
            await recorder.finish(
                request_id,
                "failed",
                latency_ms=0,
                error_code="embedding_internal_error",
                error_message="Embedding 服务内部处理失败",
                description="Embedding 请求失败：服务内部处理失败",
            )
        except Exception:
            logger.exception("embedding_failure_log_write_failed request_id=%s", request_id)
        return openai_error(
            500,
            "embedding_internal_error",
            "Embedding 服务内部处理失败",
            request_id,
        )
    return JSONResponse(headers={"x-request-id": request_id}, content=response)
