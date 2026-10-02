"""ASR 调用生命周期、项目 API Key 授权和独立音频时长额度。"""

import logging
import math
import time
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.api_keys import VerifiedApiKey
from app.infrastructure.db.models import (
    Project,
    ProjectServiceSubscription,
    ServiceUsageBucket,
    UserServiceQuota,
)
from app.request_logging.application import GatewayRequestRecorder
from app.services.asr.configuration import AsrConfigurationService, ResolvedAsrModel
from app.services.asr.volcengine import (
    AsrProviderError,
    VolcAsrProvider,
    normalize_audio_format,
    openai_transcription_response,
)

logger = logging.getLogger("gateway.service.asr")
ASR_SERVICE_CODE = "asr-v1"


class AsrGatewayError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(slots=True)
class AsrStreamSession:
    key: VerifiedApiKey
    request_id: str
    model: str
    started: float
    period_start: datetime
    routes: tuple[ResolvedAsrModel, ...]
    audio_bytes: int = 0
    forwarded_audio_bytes: int = 0
    reserved_seconds: int = 0
    settled_seconds: int = 0
    finished: bool = False


def estimate_audio_seconds(audio: bytes, filename: str) -> float:
    """从常见音频容器估算实际时长；预留及结算额度均按音频秒数。"""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension == "wav":
        try:
            with wave.open(BytesIO(audio)) as source:
                return float(source.getnframes() / source.getframerate())
        except (wave.Error, ZeroDivisionError):
            pass
    elif extension == "mp3":
        duration = _mp3_duration(audio)
        if duration is not None:
            return duration
    elif extension == "ogg":
        duration = _ogg_duration(audio)
        if duration is not None:
            return duration
    elif extension == "m4a":
        duration = _mp4_duration(audio)
        if duration is not None:
            return duration
    elif extension == "pcm":
        # Only the documented gateway profile is accepted: 16 kHz, s16le, mono.
        if audio and len(audio) % 2 == 0:
            return len(audio) / (16_000 * 2)
    raise AsrGatewayError(
        "audio_duration_unavailable",
        "无法读取音频时长；请提交带有效时长信息的 WAV、MP3、OGG 或 M4A 音频文件",
        422,
    )


def _mp3_duration(audio: bytes) -> float | None:
    """按 MPEG 音频帧样本数累计时长，支持 CBR/VBR MP3。"""
    offset = 0
    if audio.startswith(b"ID3") and len(audio) >= 10:
        size = audio[6:10]
        if all(byte < 128 for byte in size):
            offset = 10 + (size[0] << 21 | size[1] << 14 | size[2] << 7 | size[3])
    duration = 0.0
    while offset + 4 <= len(audio):
        header = int.from_bytes(audio[offset : offset + 4], "big")
        if header >> 21 != 0x7FF:
            offset += 1
            continue
        version = (header >> 19) & 0b11
        layer = (header >> 17) & 0b11
        sample_index = (header >> 10) & 0b11
        if version == 0b01 or layer != 0b01 or sample_index == 0b11:
            offset += 1
            continue
        rates = (44100, 48000, 32000)
        rate = rates[sample_index]
        if version == 0b10:
            rate //= 2
        elif version == 0b00:
            rate //= 4
        duration += (1152 if version == 0b11 else 576) / rate
        bitrate_index = (header >> 12) & 0b1111
        bitrates = (
            (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
            (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        )
        bitrate_table = bitrates[0 if version == 0b11 else 1]
        bitrate = bitrate_table[bitrate_index] if bitrate_index < len(bitrate_table) else 0
        if bitrate == 0:
            return None
        frame_length = (144 if version == 0b11 else 72) * bitrate * 1000 // rate
        frame_length += (header >> 9) & 1
        if frame_length < 4:
            return None
        offset += frame_length
    return duration if duration > 0 else None


def _ogg_duration(audio: bytes) -> float | None:
    """读取 Ogg 最后一个 granule position，兼容 Opus 与 Vorbis。"""
    if not audio.startswith(b"OggS"):
        return None
    sample_rate = 48000 if b"OpusHead" in audio[:256] else None
    if sample_rate is None:
        marker = audio.find(b"\x01vorbis")
        if marker >= 0 and marker + 16 <= len(audio):
            sample_rate = int.from_bytes(audio[marker + 12 : marker + 16], "little")
    if not sample_rate:
        return None
    offset = 0
    last_granule = 0
    while offset + 27 <= len(audio):
        if audio[offset : offset + 4] != b"OggS":
            return None
        segment_count = audio[offset + 26]
        table_end = offset + 27 + segment_count
        if table_end > len(audio):
            return None
        page_size = sum(audio[offset + 27 : table_end])
        if table_end + page_size > len(audio):
            return None
        granule = int.from_bytes(audio[offset + 6 : offset + 14], "little")
        if granule != 0xFFFFFFFFFFFFFFFF:
            last_granule = max(last_granule, granule)
        offset = table_end + page_size
    return last_granule / sample_rate if last_granule > 0 else None


def _mp4_duration(audio: bytes) -> float | None:
    """优先读取 movie 时长；movie 未填写时回退到媒体轨道的 mdhd 时长。"""
    # 一些录音器写入的 mvhd.duration 为 0 或 all-ones，尽管音频轨道 mdhd
    # 中仍有有效时长。搜索两个标准 atom，兼容普通及扩展尺寸 atom 的位置。
    for atom_type in (b"mvhd", b"mdhd"):
        marker = audio.find(atom_type)
        while marker >= 0:
            duration = _mp4_header_duration(audio, marker)
            if duration is not None:
                return duration
            marker = audio.find(atom_type, marker + len(atom_type))
    return None


def _mp4_header_duration(audio: bytes, marker: int) -> float | None:
    """解析 mvhd/mdhd atom 中 version 0 或 version 1 的时间字段。"""
    if marker + 24 > len(audio):
        return None
    version = audio[marker + 4]
    if version == 0:
        timescale = int.from_bytes(audio[marker + 16 : marker + 20], "big")
        duration = int.from_bytes(audio[marker + 20 : marker + 24], "big")
    elif version == 1 and marker + 36 <= len(audio):
        timescale = int.from_bytes(audio[marker + 24 : marker + 28], "big")
        duration = int.from_bytes(audio[marker + 28 : marker + 36], "big")
    else:
        return None
    # 0xFFFFFFFF / 0xFFFFFFFFFFFFFFFF 表示未知时长，继续尝试下一个 header。
    if timescale <= 0 or duration <= 0 or duration >= (1 << (32 if version == 0 else 64)) - 1:
        return None
    return duration / timescale


class AsrGatewayService:
    """将项目请求转到 ASR Provider 并扣除独立的 ASR 服务额度。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        configuration: AsrConfigurationService,
        recorder: GatewayRequestRecorder,
        provider: VolcAsrProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._configuration = configuration
        self._recorder = recorder
        self._provider = provider or VolcAsrProvider()

    @property
    def provider(self) -> VolcAsrProvider:
        """当前 ASR 协议适配器，供同一服务的流式路由复用。"""
        return self._provider

    async def begin_stream(
        self, key: VerifiedApiKey, *, model: str, request_id: str
    ) -> AsrStreamSession:
        started = time.perf_counter()
        period = self._period_start()
        await self._recorder.start(
            request_id=request_id,
            project_id=key.project_id,
            api_key_id=key.id,
            actor_user_id=key.user_id,
            actor_username=key.username,
            service_code=ASR_SERVICE_CODE,
            description=f"实时 ASR 请求处理中，模型 {model}",
        )
        session = AsrStreamSession(key, request_id, model, started, period, ())
        try:
            routes = await self._configuration.resolve_pool(model)
            if not routes:
                raise AsrGatewayError(
                    "model_not_configured", f"未配置可用的 ASR 模型：{model}", 404
                )
            session.routes = routes
            await self._reserve(key, request_id, period, 1, started)
            session.reserved_seconds = 1
            return session
        except AsrGatewayError as error:
            await self.finish_stream(session, error_code=error.code, error_message=str(error))
            raise
        except Exception:
            await self.finish_stream(
                session, error_code="asr_internal_error", error_message="ASR 服务内部处理失败"
            )
            raise

    async def reserve_stream_audio(self, session: AsrStreamSession, byte_count: int) -> None:
        """累计当前音频时长，并按需追加预留秒数。"""
        session.audio_bytes += byte_count
        target_seconds = max(1, math.ceil(session.audio_bytes / 32_000))
        additional = target_seconds - session.reserved_seconds - session.settled_seconds
        if additional > 0:
            await self._reserve(
                session.key,
                session.request_id,
                session.period_start,
                additional,
                session.started,
                finish_denied=False,
            )
            session.reserved_seconds += additional

    async def settle_stream_progress(self, session: AsrStreamSession) -> int:
        """在每个句级 final 到达时结算已接收音频，避免等待整条 WS 会话结束。"""
        if session.finished or session.forwarded_audio_bytes <= 0:
            return 0
        target_seconds = max(1, math.ceil(session.forwarded_audio_bytes / 32_000))
        additional = min(
            max(0, target_seconds - session.settled_seconds), session.reserved_seconds
        )
        if additional <= 0:
            return 0
        async with self._session_factory.begin() as db_session:
            bucket = await db_session.scalar(
                select(ServiceUsageBucket)
                .where(
                    ServiceUsageBucket.project_id == session.key.project_id,
                    ServiceUsageBucket.service_code == ASR_SERVICE_CODE,
                    ServiceUsageBucket.period_start == session.period_start,
                )
                .with_for_update()
            )
            assert bucket is not None
            bucket.tokens_reserved = max(0, bucket.tokens_reserved - additional)
            bucket.tokens_used += additional
        session.reserved_seconds -= additional
        session.settled_seconds += additional
        logger.info(
            "asr_stream_usage_checkpoint request_id=%s model=%s billed_seconds=%d "
            "total_billed_seconds=%d",
            session.request_id,
            session.model,
            additional,
            session.settled_seconds,
        )
        return additional

    @staticmethod
    def mark_stream_audio_forwarded(session: AsrStreamSession, byte_count: int) -> None:
        """仅统计已经成功写入 ASR 上游 WebSocket 的 PCM 字节。"""
        session.forwarded_audio_bytes += byte_count

    async def finish_stream(
        self,
        session: AsrStreamSession,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> int:
        """結束實時請求並釋放預留額度或按音頻秒數結算。"""
        if session.finished:
            return 0
        if error_code:
            total_used = (
                max(1, math.ceil(session.forwarded_audio_bytes / 32_000))
                if session.forwarded_audio_bytes
                else 0
            )
            unbilled_used = max(0, total_used - session.settled_seconds)
            await self._finish_failure(
                session.request_id,
                session.started,
                error_code,
                error_message or "ASR 请求失败",
                period_start=session.period_start,
                reserved=session.reserved_seconds,
                used=unbilled_used,
            )
            session.finished = True
            return total_used
        if session.forwarded_audio_bytes == 0:
            await self._finish_failure(
                session.request_id,
                session.started,
                "empty_audio",
                "音频流不能为空",
                period_start=session.period_start,
                reserved=session.reserved_seconds,
            )
            session.finished = True
            return 0
        total_used = max(1, math.ceil(session.forwarded_audio_bytes / 32_000))
        used = max(0, total_used - session.settled_seconds)
        await self._settle(
            session.key,
            session.request_id,
            session.period_start,
            session.reserved_seconds,
            used,
            self._latency(session.started),
            (
                f"实时 ASR 成功，模型 {session.model}，音频 "
                f"{session.forwarded_audio_bytes / 32_000:.2f} 秒，计费 {total_used} 秒"
            ),
        )
        session.finished = True
        return total_used

    async def transcribe(
        self,
        key: VerifiedApiKey,
        *,
        model: str,
        filename: str,
        audio: bytes,
        response_format: str = "json",
        language: str | None = None,
        prompt: str | None = None,
        request_id: str,
    ) -> tuple[str, object]:
        started = time.perf_counter()
        period_start: datetime | None = None
        reserved = 0
        reservation_created = False
        await self._recorder.start(
            request_id=request_id,
            project_id=key.project_id,
            api_key_id=key.id,
            actor_user_id=key.user_id,
            actor_username=key.username,
            service_code=ASR_SERVICE_CODE,
            description=f"ASR 请求处理中，模型 {model}",
        )
        try:
            audio_format = normalize_audio_format(filename)
            duration_seconds = estimate_audio_seconds(audio, filename)
            reserved = max(1, math.ceil(duration_seconds))
            period_start = self._period_start()
            pool = await self._configuration.resolve_pool(model)
            if not pool:
                has_prefix = "/" in model
                prefix = model.partition("/")[0].lower() if has_prefix else None
                if prefix == "volc":
                    message = "未配置可用的 volc ASR 连接"
                else:
                    message = "此前缀将使用通用 OpenAI 转录适配器，但当前尚未配置通用 ASR 上游连接"
                raise AsrGatewayError("provider_not_configured", message, 404)
            await self._reserve(key, request_id, period_start, reserved, started)
            reservation_created = True
            last_error: AsrProviderError | None = None
            result = None
            selected = None
            for route in pool:
                try:
                    result = await self._provider.transcribe(
                        route.provider,
                        audio,
                        audio_format,
                        language=language,
                        prompt=prompt,
                    )
                    selected = route
                    break
                except AsrProviderError as error:
                    last_error = error
                    logger.warning(
                        "asr_upstream_attempt_failed request_id=%s supplier=%s "
                        "connection=%s code=%s",
                        request_id,
                        route.supplier_name,
                        route.connection_name,
                        error.code,
                    )
            if result is None or selected is None:
                if last_error is not None:
                    raise last_error
                raise AsrGatewayError("upstream_unavailable", "没有可用的 ASR 上游连接", 502)
            payload_type, response = openai_transcription_response(result, response_format)
            payload = result.get("payload_msg", {})
            audio_info = payload.get("audio_info", {}) if isinstance(payload, dict) else {}
            reported_ms = audio_info.get("duration") if isinstance(audio_info, dict) else None
            used = (
                max(1, math.ceil(reported_ms / 1000))
                if isinstance(reported_ms, (int, float))
                else reserved
            )
            await self._settle(
                key,
                request_id,
                period_start,
                reserved,
                used,
                max(0, round((time.perf_counter() - started) * 1000)),
                f"ASR 成功，模型 {model}，音频 {duration_seconds:.2f} 秒，计费 {used} 秒",
            )
            return payload_type, response
        except AsrGatewayError as error:
            await self._finish_failure(
                request_id,
                started,
                error.code,
                str(error),
                period_start=period_start,
                reserved=reserved if reservation_created else 0,
            )
            raise
        except AsrProviderError as error:
            await self._finish_failure(
                request_id,
                started,
                error.code,
                str(error),
                period_start=period_start,
                reserved=reserved if reservation_created else 0,
            )
            raise AsrGatewayError(error.code, str(error), error.status_code) from error
        except Exception as error:
            logger.exception("asr_request_failed request_id=%s", request_id)
            await self._finish_failure(
                request_id,
                started,
                "asr_internal_error",
                "ASR 服务内部处理失败",
                period_start=period_start,
                reserved=reserved if reservation_created else 0,
            )
            raise AsrGatewayError("asr_internal_error", "ASR 服务内部处理失败", 500) from error

    async def _reserve(
        self,
        key: VerifiedApiKey,
        request_id: str,
        period: datetime,
        amount: int,
        started: float,
        *,
        finish_denied: bool = True,
    ) -> None:
        rejection: AsrGatewayError | None = None
        async with self._session_factory.begin() as session:
            quota = await session.scalar(
                select(UserServiceQuota)
                .where(
                    UserServiceQuota.user_id == key.owner_id,
                    UserServiceQuota.service_code == ASR_SERVICE_CODE,
                )
                .with_for_update()
            )
            subscription = await session.scalar(
                select(ProjectServiceSubscription)
                .where(
                    ProjectServiceSubscription.project_id == key.project_id,
                    ProjectServiceSubscription.service_code == ASR_SERVICE_CODE,
                    ProjectServiceSubscription.status == "active",
                )
                .with_for_update()
            )
            if quota is None:
                rejection = AsrGatewayError(
                    "service_quota_missing", "项目 owner 尚未获得 ASR 服务额度", 403
                )
            elif subscription is None:
                rejection = AsrGatewayError("service_not_enabled", "项目尚未申请 ASR 服务", 403)
            else:
                await session.execute(
                    insert(ServiceUsageBucket)
                    .values(
                        project_id=key.project_id,
                        service_code=ASR_SERVICE_CODE,
                        period_start=period,
                        tokens_used=0,
                        tokens_reserved=0,
                    )
                    .on_conflict_do_nothing()
                )
                bucket = await session.scalar(
                    select(ServiceUsageBucket)
                    .where(
                        ServiceUsageBucket.project_id == key.project_id,
                        ServiceUsageBucket.service_code == ASR_SERVICE_CODE,
                        ServiceUsageBucket.period_start == period,
                    )
                    .with_for_update()
                )
                assert bucket is not None
                user_usage = await session.execute(
                    select(
                        func.coalesce(func.sum(ServiceUsageBucket.tokens_used), 0),
                        func.coalesce(func.sum(ServiceUsageBucket.tokens_reserved), 0),
                    )
                    .join(Project, Project.id == ServiceUsageBucket.project_id)
                    .where(
                        Project.owner_id == key.owner_id,
                        ServiceUsageBucket.service_code == ASR_SERVICE_CODE,
                        ServiceUsageBucket.period_start == period,
                    )
                )
                used, reserved = user_usage.one()
                if used + reserved + amount > quota.monthly_token_limit:
                    rejection = AsrGatewayError(
                        "user_quota_exceeded", "ASR 服务月度时长额度不足", 429
                    )
                elif subscription.monthly_token_limit is None:
                    rejection = AsrGatewayError(
                        "service_quota_invalid", "项目 ASR 额度配置异常", 500
                    )
                elif (
                    bucket.tokens_used + bucket.tokens_reserved + amount
                    > subscription.monthly_token_limit
                ):
                    rejection = AsrGatewayError(
                        "project_quota_exceeded", "项目 ASR 月度时长分配不足", 429
                    )
                else:
                    bucket.tokens_reserved += amount
            if rejection:
                await self._recorder.record_stage_in_session(
                    session, request_id, "quota", "denied", error_code=rejection.code
                )
                if finish_denied:
                    await self._recorder.finish_in_session(
                        session,
                        request_id,
                        "denied",
                        latency_ms=self._latency(started),
                        error_code=rejection.code,
                        error_message=str(rejection),
                        description=f"ASR 额度检查失败：{rejection}",
                    )
            else:
                await self._recorder.record_stages_in_session(
                    session,
                    request_id,
                    {"quota": "allowed", "routing": "allowed", "service_call": "pending"},
                )
        if rejection:
            raise rejection

    async def _settle(
        self,
        key: VerifiedApiKey,
        request_id: str,
        period: datetime,
        reserved: int,
        used: int,
        latency_ms: int,
        description: str,
    ) -> None:
        async with self._session_factory.begin() as session:
            bucket = await session.scalar(
                select(ServiceUsageBucket)
                .where(
                    ServiceUsageBucket.project_id == key.project_id,
                    ServiceUsageBucket.service_code == ASR_SERVICE_CODE,
                    ServiceUsageBucket.period_start == period,
                )
                .with_for_update()
            )
            assert bucket is not None
            bucket.tokens_reserved = max(0, bucket.tokens_reserved - reserved)
            bucket.tokens_used += used
            await self._recorder.record_stage_in_session(
                session, request_id, "service_call", "succeeded"
            )
            await self._recorder.finish_in_session(
                session,
                request_id,
                "succeeded",
                latency_ms=latency_ms,
                description=description,
            )

    async def _finish_failure(
        self,
        request_id: str,
        started: float,
        code: str,
        message: str,
        *,
        period_start: datetime | None,
        reserved: int,
        used: int = 0,
    ) -> None:
        async with self._session_factory.begin() as session:
            record = await self._recorder._get_record(session, request_id)
            if record.status == "denied":
                return
            if (reserved or used) and period_start is not None:
                bucket = await session.scalar(
                    select(ServiceUsageBucket)
                    .where(
                        ServiceUsageBucket.project_id == record.project_id,
                        ServiceUsageBucket.service_code == ASR_SERVICE_CODE,
                        ServiceUsageBucket.period_start == period_start,
                    )
                    .with_for_update()
                )
                if bucket is not None:
                    bucket.tokens_reserved = max(0, bucket.tokens_reserved - reserved)
                    bucket.tokens_used += used
            await self._recorder.record_stage_in_session(
                session, request_id, "service_call", "failed", error_code=code
            )
            await self._recorder.finish_in_session(
                session,
                request_id,
                "failed",
                latency_ms=self._latency(started),
                error_code=code,
                error_message=message,
                description=f"ASR 调用失败：{message}",
            )

    @staticmethod
    def _period_start() -> datetime:
        now = datetime.now(UTC)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _latency(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))
