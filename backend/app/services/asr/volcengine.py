"""火山引擎豆包 ASR V3 单句识别 Provider。"""

import asyncio
import io
import logging
import os
import shutil
import wave
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK, InvalidStatus

from app.services.asr.protocol import (
    AsrProtocolError,
    RealtimeTranscriptTracker,
    decode_frame,
    encode_audio,
    encode_full_request,
    parse_result,
)


class AsrProviderError(RuntimeError):
    """可安全映射为 OpenAI 兼容错误的 ASR Provider 故障。"""

    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


logger = logging.getLogger("gateway.service.asr.provider.volc")


@dataclass(frozen=True, slots=True)
class VolcAsrConnection:
    """一条火山引擎 ASR 上游连接。"""

    api_key: str
    resource_id: str
    file_transcription_url: str = (
        "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream"
    )
    realtime_url: str = "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async"


@dataclass(slots=True)
class VolcAsrRealtimeSession:
    """把上游 socket 与按请求隔离的分句去重状态绑定。"""

    websocket: Any
    gateway_request_id: str
    transcript_tracker: RealtimeTranscriptTracker

    @property
    def response(self) -> Any:
        return getattr(self.websocket, "response", None)

    async def close(self) -> None:
        await self.websocket.close()


class VolcAsrProvider:
    """将一次性录音文件转成豆包语音识别 WebSocket V3 请求。"""

    async def transcribe(
        self,
        connection: VolcAsrConnection,
        audio: bytes,
        audio_format: str,
        *,
        language: str | None = None,
        prompt: str | None = None,
        enable_punc: bool = True,
        enable_itn: bool = True,
        enable_ddc: bool = False,
        show_utterances: bool = False,
    ) -> dict[str, Any]:
        if not audio:
            raise AsrProviderError("empty_audio", "音频文件不能为空", 422)
        try:
            audio_chunks = await self._prepare_wav_chunks(audio, audio_format)
        except AsrProviderError:
            raise
        payload: dict[str, Any] = {
            "audio": {
                "format": "wav",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_punc": enable_punc,
                "enable_itn": enable_itn,
                "result_type": "full",
            },
            "user": {"uid": "gateway"},
        }
        if language:
            payload["audio"]["language"] = language
        if prompt:
            payload["request"]["context"] = {
                "context_type": "dialog_ctx",
                "context_data": [{"text": prompt[:4000]}],
            }
        latest_result: dict[str, Any] | None = None
        headers = {
            "X-Api-Key": connection.api_key,
            "X-Api-Resource-Id": connection.resource_id,
            "X-Api-Request-Id": str(uuid4()),
            "X-Api-Connect-Id": str(uuid4()),
        }
        if "/api/v3/plan/" in connection.file_transcription_url:
            headers["X-Api-Sequence"] = "-1"
        upstream_logid = "unavailable"
        try:
            async with connect(
                connection.file_transcription_url,
                additional_headers=headers,
                open_timeout=10,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
                proxy=None,
            ) as websocket:
                response = getattr(websocket, "response", None)
                response_headers = getattr(response, "headers", None)
                if response_headers is not None:
                    upstream_logid = response_headers.get("X-Tt-Logid", "unavailable")
                logger.info("asr_upstream_connected logid=%s", upstream_logid)
                sequence = 1
                await websocket.send(encode_full_request(payload, sequence=sequence))
                sequence += 1
                for chunk in audio_chunks:
                    await websocket.send(
                        encode_audio(chunk, final=False, sequence=sequence, compress=False)
                    )
                    sequence += 1
                await websocket.send(
                    encode_audio(b"", final=True, sequence=sequence, compress=False)
                )
                while True:
                    result = await self._receive_response(websocket)
                    latest_result = result or latest_result
                    if latest_result and latest_result.get("is_last_package") is True:
                        return latest_result
        except AsrProviderError:
            raise
        except ConnectionClosedOK as error:
            if latest_result and isinstance(latest_result.get("payload_msg"), dict):
                logger.warning(
                    "asr_upstream_closed_after_result is_last_package=%s",
                    latest_result.get("is_last_package"),
                )
                return latest_result
            close_code = error.rcvd.code if error.rcvd is not None else "unknown"
            close_reason = error.rcvd.reason if error.rcvd is not None else "unknown"
            logger.warning(
                "asr_upstream_closed_without_result close_code=%s close_reason=%r logid=%s",
                close_code,
                close_reason,
                upstream_logid,
            )
            raise AsrProviderError(
                "upstream_closed_without_result",
                f"火山 ASR 已正常关闭连接，但未返回识别结果（上游日志 ID：{upstream_logid}）",
                502,
            ) from error
        except TimeoutError as error:
            raise AsrProviderError("upstream_timeout", "等待火山 ASR 识别超时", 504) from error
        except InvalidStatus as error:
            status_code = error.response.status_code
            if status_code in (401, 403):
                raise AsrProviderError(
                    "upstream_authentication_failed",
                    "火山 ASR 上游鉴权失败，请检查 ASR API Key 和资源权限",
                    502,
                ) from error
            raise AsrProviderError(
                "upstream_handshake_failed",
                f"火山 ASR WebSocket 建连失败（HTTP {status_code}）",
                502,
            ) from error
        except OSError as error:
            raise AsrProviderError("upstream_connection_error", "无法连接火山 ASR 上游") from error
        except AsrProtocolError as error:
            raise AsrProviderError("upstream_protocol_error", str(error)) from error

    async def open_realtime(
        self,
        connection: VolcAsrConnection,
        *,
        gateway_request_id: str,
        language: str | None = None,
        prompt: str | None = None,
    ) -> Any:
        """建立火山实时流连接并发送识别初始化帧。"""
        payload: dict[str, Any] = {
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_nonstream": True,
                "end_window_size": 800,
                "show_utterances": True,
                "enable_itn": True,
                "enable_punc": True,
                "result_type": "full",
            },
            "user": {"uid": "gateway"},
        }
        if language:
            payload["audio"]["language"] = language
        if prompt:
            payload["request"]["context"] = {
                "context_type": "dialog_ctx",
                "context_data": [{"text": prompt[:4000]}],
            }
        upstream_request_id = str(uuid4())
        headers = {
            "X-Api-Key": connection.api_key,
            "X-Api-Resource-Id": connection.resource_id,
            "X-Api-Request-Id": upstream_request_id,
            "X-Api-Connect-Id": str(uuid4()),
            "X-Api-Sequence": "-1",
        }
        websocket = None
        try:
            websocket = await connect(
                connection.realtime_url,
                additional_headers=headers,
                open_timeout=10,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
                proxy=None,
            )
            response = getattr(websocket, "response", None)
            response_headers = getattr(response, "headers", {})
            logger.info(
                "asr_upstream_handshake request_id=%s upstream_request_id=%s upstream_logid=%s",
                gateway_request_id,
                upstream_request_id,
                response_headers.get("X-Tt-Logid", "unavailable"),
            )
            await websocket.send(encode_full_request(payload, sequence=1))
            return VolcAsrRealtimeSession(
                websocket=websocket,
                gateway_request_id=gateway_request_id,
                transcript_tracker=RealtimeTranscriptTracker(gateway_request_id),
            )
        except InvalidStatus as error:
            if websocket is not None:
                await websocket.close()
            status_code = error.response.status_code
            code = (
                "upstream_authentication_failed"
                if status_code in (401, 403)
                else "upstream_handshake_failed"
            )
            message = (
                "火山 ASR 上游鉴权失败，请检查服务端 ASR API Key 和资源权限"
                if status_code in (401, 403)
                else f"火山 ASR WebSocket 建连失败（HTTP {status_code}）"
            )
            raise AsrProviderError(code, message, 502) from error
        except TimeoutError as error:
            if websocket is not None:
                await websocket.close()
            raise AsrProviderError("upstream_timeout", "连接火山 ASR 上游超时", 504) from error
        except OSError as error:
            if websocket is not None:
                await websocket.close()
            raise AsrProviderError("upstream_connection_error", "无法连接火山 ASR 上游") from error
        except Exception as error:
            if websocket is not None:
                try:
                    await websocket.close()
                except Exception:
                    pass
            raise AsrProviderError(
                "upstream_connection_error", "火山 ASR 上游初始化失败"
            ) from error

    @staticmethod
    async def send_realtime_audio(
        websocket: Any, audio: bytes, sequence: int, *, final: bool = False
    ) -> None:
        """发送一帧原始 PCM；结束帧允许为空。"""
        upstream = getattr(websocket, "websocket", websocket)
        await upstream.send(encode_audio(audio, final=final, sequence=sequence, compress=True))

    @staticmethod
    async def receive_realtime(
        websocket: Any,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        """读取一个火山结果帧并返回网关事件和是否识别结束。"""
        upstream = getattr(websocket, "websocket", websocket)
        try:
            frame_data = await asyncio.wait_for(upstream.recv(), timeout=60)
        except TimeoutError as error:
            raise AsrProviderError("upstream_timeout", "等待火山 ASR 实时结果超时", 504) from error
        if not isinstance(frame_data, bytes):
            raise AsrProviderError("upstream_protocol_error", "火山 ASR 返回了非二进制帧")
        try:
            frame = decode_frame(frame_data)
            result = parse_result(frame.payload)
        except AsrProtocolError as error:
            raise AsrProviderError("upstream_protocol_error", str(error)) from error
        completed = bool(
            frame.flags & 0x02
            or result.get("is_last_package") is True
            or (frame.sequence is not None and frame.sequence < 0)
        )
        final_frame = completed or result.get("is_final") is True
        tracker = getattr(websocket, "transcript_tracker", None)
        request_id = getattr(websocket, "gateway_request_id", "untracked")
        if tracker is None:
            tracker = RealtimeTranscriptTracker(request_id)
        events = tracker.extract(result, final_frame=final_frame)
        final = any(event.get("type") == "transcript.final" for event in events)
        return events, final, completed

    @staticmethod
    async def _prepare_wav_chunks(audio: bytes, audio_format: str) -> list[bytes]:
        """解码并封装为 16kHz 单声道 PCM WAV，按 100ms 大小切片（保留 WAV 文件头）。"""
        if audio_format == "pcm":
            if len(audio) % 2:
                raise AsrProviderError(
                    "invalid_audio",
                    "裸 PCM 文件必须是 16 kHz、16-bit 小端、单声道格式，数据长度必须为偶数",
                    422,
                )
            pcm = audio
        else:
            ffmpeg = os.environ.get("ASR_FFMPEG_BIN", "ffmpeg")
            executable = shutil.which(ffmpeg)
            if executable is None:
                raise AsrProviderError(
                    "audio_decoder_unavailable",
                    "ASR 音频解码器不可用，请安装 ffmpeg 并确保其位于 PATH 中",
                    503,
                )
            try:
                process = await asyncio.create_subprocess_exec(
                    executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    "pipe:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(audio), timeout=60)
            except TimeoutError as error:
                raise AsrProviderError("audio_decode_timeout", "音频解码超时", 422) from error
            except OSError as error:
                raise AsrProviderError(
                    "audio_decoder_unavailable", "无法启动 ffmpeg 音频解码器", 503
                ) from error
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                logger.info("asr_audio_decode_failed detail=%r", detail[:300])
                raise AsrProviderError(
                    "invalid_audio", "无法解码音频文件，请确认文件格式和内容有效", 422
                )
            try:
                with wave.open(io.BytesIO(stdout), "rb") as wav:
                    if (
                        wav.getframerate() != 16000
                        or wav.getnchannels() != 1
                        or wav.getsampwidth() != 2
                    ):
                        raise ValueError("ffmpeg 输出了非预期的 PCM 音频格式")
                    pcm = wav.readframes(wav.getnframes())
            except (wave.Error, EOFError, ValueError) as error:
                raise AsrProviderError("invalid_audio", "音频解码结果无效", 422) from error
        chunk_size = 3200  # 16 kHz × 16 bit × 1 声道 × 100 ms
        if not pcm:
            raise AsrProviderError("empty_audio", "音频文件不包含有效音频", 422)
        wav_audio = io.BytesIO()
        with wave.open(wav_audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        wav_bytes = wav_audio.getvalue()
        return [
            wav_bytes[offset : offset + chunk_size]
            for offset in range(0, len(wav_bytes), chunk_size)
        ]

    @staticmethod
    async def _receive_response(websocket: Any) -> dict[str, Any] | None:
        frame_data = await asyncio.wait_for(websocket.recv(), timeout=60)
        if not isinstance(frame_data, bytes):
            raise AsrProtocolError("ASR 上游返回了非二进制帧")
        frame = decode_frame(frame_data)
        result = parse_result(frame.payload)
        result["is_last_package"] = bool(
            result.get("is_last_package")
            or frame.flags & 0b0010
            or (frame.sequence is not None and frame.sequence < 0)
        )
        code = result.get("code")
        if isinstance(code, int) and code not in (0, 20000000):
            message = result.get("message") or result.get("payload_msg") or "未知上游错误"
            raise AsrProviderError("upstream_rejected", f"火山 ASR 上游拒绝请求：{message}", 502)
        payload = result.get("payload_msg")
        if not isinstance(payload, dict):
            payload = result.get("message")
        if isinstance(payload, dict) and not isinstance(payload.get("payload_msg"), dict):
            result["payload_msg"] = payload
        elif isinstance(payload, dict):
            result["payload_msg"] = payload["payload_msg"]
        if isinstance(result.get("result"), dict) and not isinstance(
            result.get("payload_msg"), dict
        ):
            result["payload_msg"] = {
                "result": result["result"],
                "audio_info": result.get("audio_info", {}),
            }
        if isinstance(result.get("payload_msg"), dict):
            return result
        return None


def normalize_audio_format(filename: str) -> str:
    """根据客户端文件名校验 ASR 支持的音频格式。"""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in {"mp3", "wav", "ogg", "m4a", "pcm"}:
        return extension
    raise AsrProviderError(
        "unsupported_audio_format",
        "当前网关支持 WAV、MP3、OGG、M4A，以及 16 kHz/16-bit/单声道小端裸 PCM 文件",
        422,
    )


def openai_transcription_response(result: dict[str, Any], response_format: str) -> tuple[str, Any]:
    """把豆包输出归一化为 OpenAI Transcription 响应。"""
    payload = result.get("payload_msg")
    asr_result = payload.get("result") if isinstance(payload, dict) else None
    text = asr_result.get("text", "") if isinstance(asr_result, dict) else ""
    audio_info = payload.get("audio_info", {}) if isinstance(payload, dict) else {}
    duration_ms = audio_info.get("duration") if isinstance(audio_info, dict) else None
    duration = round(duration_ms / 1000, 3) if isinstance(duration_ms, int) else None
    if response_format == "text":
        return "text/plain", text
    body: dict[str, Any] = {"text": text}
    if response_format == "verbose_json":
        body.update({"task": "transcribe", "language": "unknown"})
        if duration is not None:
            body["duration"] = duration
        if isinstance(asr_result, dict) and isinstance(asr_result.get("utterances"), list):
            body["segments"] = [
                {
                    "id": index,
                    "start": utterance.get("start_time", 0) / 1000,
                    "end": utterance.get("end_time", 0) / 1000,
                    "text": utterance.get("text", ""),
                }
                for index, utterance in enumerate(asr_result["utterances"])
                if isinstance(utterance, dict)
            ]
    return "application/json", body
