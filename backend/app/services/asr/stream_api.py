"""统一的 PCM 实时 ASR WebSocket 接口。"""

import asyncio
import json
import logging
import re
import time
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from app.application.api_keys import ApiKeyInvalidError, VerifiedApiKey
from app.container import AppContainer
from app.request_logging.application import GatewayRequestRecorder
from app.services.asr.application import AsrGatewayError, AsrStreamSession
from app.services.asr.volcengine import AsrProviderError, VolcAsrProvider
from app.technical_logging import reset_trace_id, set_trace_id

router = APIRouter(tags=["ASR Gateway"])
logger = logging.getLogger("gateway.service.asr.stream")
TRACE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
PCM_BYTES_PER_SECOND = 32_000
MAX_AUDIO_CHUNK_BYTES = 1024 * 1024
MAX_PROMPT_LENGTH = 4000


def _error(code: str, message: str, request_id: str) -> dict[str, str]:
    return {
        "type": "error",
        "code": code.upper(),
        "message": message,
        "request_id": request_id,
    }


def _bearer_token(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    # Browser WebSocket APIs cannot set Authorization. Allow the standard
    # subprotocol offer: Sec-WebSocket-Protocol: bearer, <project-api-key>.
    offered = websocket.headers.get("sec-websocket-protocol", "").split(",")
    protocols = [part.strip() for part in offered if part.strip()]
    if len(protocols) >= 2 and protocols[0].lower() == "bearer":
        return protocols[1]
    return None


async def _send_error(websocket: WebSocket, code: str, message: str, request_id: str) -> None:
    await websocket.send_json(_error(code, message, request_id))


async def _record_invalid_session(
    recorder: GatewayRequestRecorder,
    key: VerifiedApiKey,
    request_id: str,
    started: float,
    code: str,
    message: str,
    model: str | None,
) -> None:
    description = "实时 ASR 协议校验失败"
    if model:
        description += f"，模型 {model}"
    await recorder.start(
        request_id=request_id,
        project_id=key.project_id,
        api_key_id=key.id,
        actor_user_id=key.user_id,
        actor_username=key.username,
        service_code="asr-v1",
        description=description,
    )
    await recorder.record_stage(request_id, "service_call", "failed", error_code=code)
    await recorder.finish(
        request_id,
        "failed",
        latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        error_code=code,
        error_message=message,
        description=description,
    )


@router.websocket("/v1/audio/stream")
async def stream_transcription(websocket: WebSocket) -> None:
    """接收 16 kHz mono s16le PCM，并将识别事件实时转发给调用方。"""
    container: AppContainer = websocket.app.state.container
    trace_candidate = websocket.headers.get("x-trace-id", "")
    request_id = (
        trace_candidate if TRACE_PATTERN.fullmatch(trace_candidate) else f"trace_{uuid4().hex}"
    )
    trace_token = set_trace_id(request_id)
    started = time.perf_counter()
    api_key_service = container.api_key_service
    gateway = container.asr_gateway_service
    recorder = container.request_recorder
    configuration = container.asr_configuration_service
    provider = gateway.provider if gateway is not None else VolcAsrProvider()
    if api_key_service is None or gateway is None or recorder is None or configuration is None:
        await websocket.accept()
        await _send_error(websocket, "SERVICE_UNAVAILABLE", "ASR 服务暂不可用", request_id)
        await websocket.close(code=1013)
        reset_trace_id(trace_token)
        return

    offered_protocols = [
        part.strip().lower()
        for part in websocket.headers.get("sec-websocket-protocol", "").split(",")
    ]
    await websocket.accept(subprotocol="bearer" if "bearer" in offered_protocols else None)
    raw_key = _bearer_token(websocket)
    if raw_key is None:
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code="asr-v1",
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_code="missing_api_key",
        )
        await _send_error(
            websocket,
            "INVALID_API_KEY",
            "请通过 Bearer 项目 API Key 完成鉴权",
            request_id,
        )
        await websocket.close(code=4401)
        reset_trace_id(trace_token)
        return
    try:
        key = await api_key_service.verify(raw_key)
    except ApiKeyInvalidError:
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code="asr-v1",
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_code="invalid_api_key",
        )
        await _send_error(
            websocket,
            "INVALID_API_KEY",
            "项目 API Key 无效、已撤销或已过期",
            request_id,
        )
        await websocket.close(code=4401)
        reset_trace_id(trace_token)
        return

    session: AsrStreamSession | None = None
    upstream = None
    sequence = 2
    ended = False
    request_record_started = False
    model: str | None = None
    try:
        first_message = await asyncio.wait_for(websocket.receive(), timeout=15)
        if first_message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(first_message.get("code", 1000))
        if not isinstance(first_message.get("text"), str):
            raise AsrGatewayError("invalid_start", "首条消息必须是 type=start 的 JSON", 1008)
        try:
            start_message = json.loads(first_message["text"])
        except json.JSONDecodeError as error:
            raise AsrGatewayError("invalid_start", "首条消息必须是有效 JSON", 1008) from error
        if not isinstance(start_message, dict) or start_message.get("type") != "start":
            raise AsrGatewayError("invalid_start", "首条消息必须是 type=start 的 JSON", 1008)
        model = start_message.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise AsrGatewayError("invalid_model", "start.model 必须是有效模型名", 1008)
        audio = start_message.get("audio", {})
        if audio is None:
            audio = {}
        if not isinstance(audio, dict) or any(
            type(audio.get(key_name, expected)) is not type(expected)
            or audio.get(key_name, expected) != expected
            for key_name, expected in (
                ("format", "pcm"),
                ("sample_rate", 16000),
                ("channels", 1),
                ("bits", 16),
                ("endianness", "little"),
            )
        ):
            raise AsrGatewayError(
                "unsupported_audio_format",
                "仅支持 16 kHz、单声道、16-bit little-endian PCM 音频",
                1008,
            )
        language = start_message.get("language")
        prompt = start_message.get("prompt")
        if language is not None and (not isinstance(language, str) or len(language) > 20):
            raise AsrGatewayError("invalid_language", "language 长度不能超过 20", 1008)
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_LENGTH):
            raise AsrGatewayError("invalid_prompt", "prompt 长度不能超过 4000", 1008)

        request_record_started = True
        session = await gateway.begin_stream(key, model=model.strip(), request_id=request_id)
        last_error: AsrProviderError | None = None
        for route in session.routes:
            connect_started = time.perf_counter()
            try:
                upstream = await provider.open_realtime(
                    route.provider,
                    gateway_request_id=request_id,
                    language=language,
                    prompt=prompt,
                )
                response_headers = getattr(getattr(upstream, "response", None), "headers", {})
                logger.info(
                    "asr_stream_upstream_connected request_id=%s model=%s "
                    "supplier=%s connection=%s stage_latency_ms=%d upstream_status=%s "
                    "upstream_logid=%s",
                    request_id,
                    model,
                    route.supplier_name,
                    route.connection_name,
                    round((time.perf_counter() - connect_started) * 1000),
                    getattr(getattr(upstream, "response", None), "status_code", 101),
                    response_headers.get("X-Tt-Logid", "unavailable"),
                )
                break
            except AsrProviderError as error:
                last_error = error
                logger.warning(
                    "asr_stream_upstream_attempt_failed request_id=%s model=%s code=%s "
                    "upstream_status=%d stage_latency_ms=%d message=%s",
                    request_id,
                    model,
                    error.code,
                    error.status_code,
                    round((time.perf_counter() - connect_started) * 1000),
                    str(error)[:300],
                )
        if upstream is None:
            raise last_error or AsrProviderError("upstream_unavailable", "没有可用的 ASR 上游连接")

        await websocket.send_json(
            {"type": "session.started", "request_id": request_id, "model": model.strip()}
        )
        while True:
            client_task = asyncio.create_task(websocket.receive())
            upstream_task = asyncio.create_task(provider.receive_realtime(upstream))
            done, pending = await asyncio.wait(
                {client_task, upstream_task},
                timeout=65,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                raise AsrProviderError("upstream_timeout", "等待 ASR 实时结果超时", 504)

            if client_task in done:
                incoming = client_task.result()
                message_type = incoming.get("type")
                if message_type == "websocket.disconnect":
                    raise WebSocketDisconnect(incoming.get("code", 1000))
                if message_type == "websocket.receive" and incoming.get("bytes") is not None:
                    chunk = incoming["bytes"]
                    if ended:
                        raise AsrGatewayError("audio_after_end", "end 之后不能继续发送音频", 1008)
                    if not chunk or len(chunk) > MAX_AUDIO_CHUNK_BYTES or len(chunk) % 2:
                        raise AsrGatewayError(
                            "invalid_audio_chunk",
                            "音频帧必须为 1 到 1 MiB 的偶数字节 PCM 块",
                            1008,
                        )
                    await gateway.reserve_stream_audio(session, len(chunk))
                    await provider.send_realtime_audio(upstream, chunk, sequence)
                    sequence += 1
                    if upstream_task not in done:
                        continue
                text = incoming.get("text") if message_type == "websocket.receive" else None
                if not isinstance(text, str):
                    raise AsrGatewayError(
                        "invalid_message", "仅接受 JSON 控制消息和二进制音频帧", 1008
                    )
                try:
                    control = json.loads(text)
                except json.JSONDecodeError as error:
                    raise AsrGatewayError(
                        "invalid_json", "控制消息必须是有效 JSON", 1008
                    ) from error
                if not isinstance(control, dict) or control.get("type") != "end":
                    raise AsrGatewayError(
                        "invalid_control_message", "仅支持 type=end 控制消息", 1008
                    )
                if not ended:
                    if session.audio_bytes == 0:
                        raise AsrGatewayError("empty_audio", "音频流不能为空", 422)
                    await provider.send_realtime_audio(upstream, b"", sequence, final=True)
                    sequence += 1
                    ended = True
                    logger.info(
                        "asr_stream_input_ended request_id=%s model=%s audio_seconds=%.3f",
                        request_id,
                        session.model,
                        session.audio_bytes / PCM_BYTES_PER_SECOND,
                    )

            if upstream_task in done:
                try:
                    events, _is_final, completed = upstream_task.result()
                except ConnectionClosed as error:
                    raise AsrProviderError(
                        "upstream_closed_without_result",
                        "火山 ASR 上游关闭连接时未完成识别",
                    ) from error
                for event in events:
                    await websocket.send_json({**event, "request_id": request_id})
                logger.info(
                    "asr_stream_upstream_response request_id=%s model=%s event_count=%d "
                    "final=%s completed=%s elapsed_ms=%d",
                    request_id,
                    session.model,
                    len(events),
                    _is_final,
                    completed,
                    round((time.perf_counter() - session.started) * 1000),
                )
                if completed and ended:
                    used = await gateway.finish_stream(session)
                    await websocket.send_json(
                        {
                            "type": "session.completed",
                            "request_id": request_id,
                            "audio_seconds": round(session.audio_bytes / PCM_BYTES_PER_SECOND, 3),
                            "billed_seconds": used,
                        }
                    )
                    break
                if completed:
                    # 上游包级 final 可能只是静音判停后的当前语句结果；只有调用方
                    # 发送 end 后，才将上游最后一包视为整条会话完成。
                    logger.info(
                        "asr_stream_utterance_final request_id=%s model=%s elapsed_ms=%d",
                        request_id,
                        session.model,
                        round((time.perf_counter() - session.started) * 1000),
                    )

    except TimeoutError:
        if session is not None and not session.finished:
            await gateway.finish_stream(
                session,
                error_code="upstream_timeout",
                error_message="等待 ASR 实时结果超时",
            )
        elif not request_record_started:
            await _record_invalid_session(
                recorder, key, request_id, started, "start_timeout", "等待 start 消息超时", model
            )
        code = "START_TIMEOUT" if session is None else "UPSTREAM_TIMEOUT"
        message = "等待 start 消息超时" if session is None else "等待 ASR 实时结果超时"
        await _send_error(websocket, code, message, request_id)
        await websocket.close(code=1008 if session is None else 1011)
    except WebSocketDisconnect:
        if session is not None and not session.finished:
            await gateway.finish_stream(
                session,
                error_code="client_disconnected",
                error_message="调用方在识别完成前断开连接",
            )
    except AsrGatewayError as error:
        if session is None and not request_record_started:
            await _record_invalid_session(
                recorder, key, request_id, started, error.code, str(error), model
            )
        if session is not None and not session.finished:
            await gateway.finish_stream(session, error_code=error.code, error_message=str(error))
        await _send_error(websocket, error.code, str(error), request_id)
        close_code = 1011 if error.status_code >= 500 else 1008
        if error.code in {"service_unavailable", "quota_temporarily_unavailable"}:
            close_code = 1013
        await websocket.close(code=close_code)
    except AsrProviderError as error:
        logger.error(
            "asr_stream_failed request_id=%s code=%s stage=upstream message=%s",
            request_id,
            error.code,
            str(error)[:500],
        )
        if session is not None and not session.finished:
            await gateway.finish_stream(session, error_code=error.code, error_message=str(error))
        await _send_error(websocket, error.code, str(error), request_id)
        await websocket.close(code=1011)
    except asyncio.CancelledError:
        if session is not None and not session.finished:
            await asyncio.shield(
                gateway.finish_stream(
                    session,
                    error_code="server_cancelled",
                    error_message="服务端取消了 ASR 请求",
                )
            )
        raise
    except Exception:
        logger.exception("asr_stream_internal_error request_id=%s", request_id)
        if session is not None and not session.finished:
            await gateway.finish_stream(
                session,
                error_code="asr_internal_error",
                error_message="ASR 服务内部处理失败",
            )
        try:
            await _send_error(websocket, "ASR_INTERNAL_ERROR", "ASR 服务内部处理失败", request_id)
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        try:
            if upstream is not None:
                await upstream.close()
        except Exception:
            logger.warning("asr_stream_upstream_close_failed request_id=%s", request_id)
        finally:
            reset_trace_id(trace_token)
