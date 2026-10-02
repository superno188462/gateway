"""OpenAI Audio Transcriptions 兼容接口。"""

import logging
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.application.api_keys import ApiKeyInvalidError, ApiKeyService
from app.container import (
    bearer_scheme,
    get_api_key_service,
    get_asr_gateway_service,
    get_request_recorder,
)
from app.request_logging.application import GatewayRequestRecorder
from app.services.asr.application import AsrGatewayError, AsrGatewayService

router = APIRouter(tags=["ASR Gateway"])
logger = logging.getLogger("gateway.auth")
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class OpenAiAudioError(BaseModel):
    message: str
    type: str = "gateway_error"
    code: str
    request_id: str


def _error(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else {}
    headers["x-request-id"] = request_id
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": OpenAiAudioError(
                message=message,
                code=code,
                request_id=request_id,
            ).model_dump(),
        },
    )


@router.post(
    "/v1/audio/transcriptions",
    summary="OpenAI 兼容音频转写",
    description=(
        "接受 OpenAI Audio Transcriptions multipart/form-data 请求，支持 json、text、verbose_json；"
        "当前将完整录音桥接到火山豆包 ASR V3 单句识别 WebSocket。"
    ),
    response_model=None,
    responses={
        200: {"description": "识别文本。根据 response_format 返回 JSON、纯文本或详细 JSON。"},
        401: {"description": "项目 API Key 无效。"},
        422: {"description": "音频文件、模型或响应格式无效。"},
        429: {"description": "ASR 服务额度不足。"},
        502: {"description": "上游 ASR 调用失败。"},
    },
)
async def create_transcription(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    gateway: Annotated[AsrGatewayService, Depends(get_asr_gateway_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
    file: Annotated[
        UploadFile,
        File(
            description=(
                "必填。支持 WAV、MP3、OGG、M4A，以及固定为 16 kHz、16-bit 小端、单声道的裸 PCM。"
            )
        ),
    ],
    model: Annotated[str, Form(min_length=1, max_length=200)],
    language: Annotated[
        str | None, Form(max_length=20, examples=["zh-CN"], description="可省略，示例：zh-CN。")
    ] = None,
    prompt: Annotated[
        str | None,
        Form(max_length=4000, description="可选识别提示，例如领域词汇或专有名词。"),
    ] = None,
    response_format: Annotated[
        Literal["json", "text", "verbose_json"],
        Form(description="可选；json 为默认值。当前不支持 srt、vtt。"),
    ] = "json",
    temperature: Annotated[float, Form(ge=0, le=0, description="可省略；当前上游仅支持 0。")] = 0,
) -> Response:
    request_id = request.state.trace_id
    started = time.perf_counter()
    if credentials is None or credentials.scheme.lower() != "bearer":
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code="asr-v1",
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error_code="missing_api_key",
        )
        return _error(401, "invalid_api_key", "缺少 Bearer API Key", request_id)
    try:
        key = await key_service.verify(credentials.credentials)
    except ApiKeyInvalidError:
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code="asr-v1",
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error_code="invalid_api_key",
        )
        return _error(401, "invalid_api_key", "API Key 无效、已撤销或已过期", request_id)
    logger.info(
        "audit_stage stage=authentication result=allowed request_id=%s project_id=%s "
        "api_key_id=%s actor_user_id=%s actor_username=%s",
        request_id,
        key.project_id,
        key.id,
        key.user_id,
        key.username,
    )
    if temperature != 0:
        return _error(
            422,
            "unsupported_parameter",
            "豆包 ASR 不支持 temperature 参数；请省略或设置为 0",
            request_id,
        )
    if response_format in {"srt", "vtt"}:
        return _error(
            422,
            "unsupported_response_format",
            "当前 ASR 网关支持 json、text 和 verbose_json 格式",
            request_id,
        )
    audio = await file.read(MAX_AUDIO_BYTES + 1)
    if len(audio) > MAX_AUDIO_BYTES:
        return _error(413, "file_too_large", "音频文件不能超过 25 MiB", request_id)
    try:
        media_type, content = await gateway.transcribe(
            key,
            model=model,
            filename=file.filename or "audio.wav",
            audio=audio,
            response_format=response_format,
            language=language,
            prompt=prompt,
            request_id=request_id,
        )
    except AsrGatewayError as error:
        return _error(error.status_code, error.code, str(error), request_id)
    headers = {"x-request-id": request_id}
    if media_type == "text/plain":
        return PlainTextResponse(content=str(content), headers=headers)
    return JSONResponse(content=content, headers=headers)
