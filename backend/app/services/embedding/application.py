"""Embedding 路由、项目额度、请求日志与用量结算。"""

import logging
import time
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.api_keys import VerifiedApiKey
from app.infrastructure.db.models import (
    EmbeddingRequestUsage,
    Project,
    ProjectServiceSubscription,
    ServiceUsageBucket,
    UserServiceQuota,
)
from app.request_logging.application import GatewayRequestRecorder
from app.services.embedding.provider import (
    ConfiguredEmbeddingProvider,
    EmbeddingProviderError,
)
from app.services.llm.providers.mock import estimate_tokens

logger = logging.getLogger("gateway.service.embedding")


class EmbeddingGatewayError(RuntimeError):
    """可安全返回给 OpenAI 风格客户端的错误。"""

    def __init__(self, code: str, message: str, status_code: int, request_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


class EmbeddingGatewayService:
    """处理文本向量请求并按上游 token usage 结算。"""

    service_code = "embedding-v1"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: ConfiguredEmbeddingProvider,
        recorder: GatewayRequestRecorder,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._recorder = recorder

    async def embed(
        self,
        key: VerifiedApiKey,
        model: str,
        inputs: list[str],
        parameters: dict[str, object] | None,
        request_id: str,
    ) -> dict[str, object]:
        started = time.perf_counter()
        reservation = sum(estimate_tokens(value) for value in inputs)
        if reservation < 1:
            reservation = len(inputs)
        period = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self._session_factory.begin() as session:
            await self._recorder.start_in_session(
                session,
                request_id=request_id,
                project_id=key.project_id,
                api_key_id=key.id,
                service_code=self.service_code,
                description=f"Embedding 请求处理中，模型 {model}，输入 {len(inputs)} 条",
                actor_user_id=key.user_id,
                actor_username=key.username,
            )
            # EmbeddingRequestUsage 通过 request_id 外键引用 GatewayRequest；
            # 先 flush 父记录，避免同一事务中的插入顺序触发外键错误。
            await session.flush()
            session.add(
                EmbeddingRequestUsage(
                    request_id=request_id,
                    model=model[:200],
                    modality="text",
                )
            )
        try:
            # 额度检查也必须经过统一错误转换，否则未开通服务时会冒泡成裸 500。
            await self._reserve(key, model, request_id, period, reservation, started)
            result = await self._provider.embed(model, inputs, parameters)
        except EmbeddingGatewayError:
            raise
        except EmbeddingProviderError as error:
            await self._release(
                key, request_id, period, reservation, started, error.code, str(error)
            )
            raise EmbeddingGatewayError(
                error.code, str(error), error.status_code, request_id
            ) from error
        except Exception as error:
            logger.exception("embedding_unhandled request_id=%s model=%s", request_id, model)
            await self._release(
                key,
                request_id,
                period,
                reservation,
                started,
                "embedding_internal_error",
                "Embedding 服务内部处理失败",
            )
            raise EmbeddingGatewayError(
                "embedding_internal_error", "Embedding 服务内部处理失败", 500, request_id
            ) from error
        used = result.input_tokens or reservation
        async with self._session_factory.begin() as session:
            bucket = await session.scalar(
                select(ServiceUsageBucket)
                .where(
                    ServiceUsageBucket.project_id == key.project_id,
                    ServiceUsageBucket.service_code == self.service_code,
                    ServiceUsageBucket.period_start == period,
                )
                .with_for_update()
            )
            assert bucket is not None
            bucket.tokens_reserved = max(0, bucket.tokens_reserved - reservation)
            bucket.tokens_used += used
            usage = await session.get(EmbeddingRequestUsage, request_id, with_for_update=True)
            assert usage is not None
            usage.input_tokens = used
            usage.vector_dimensions = result.dimensions
            await self._recorder.record_stage_in_session(
                session, request_id, "service_call", "succeeded"
            )
            await self._recorder.finish_in_session(
                session,
                request_id,
                "succeeded",
                latency_ms=self._latency(started),
                description=(
                    f"Embedding 成功，模型 {model}，输入 {len(inputs)} 条，"
                    f"消耗 {used} tokens，向量维度 {result.dimensions}"
                ),
            )
        body = result.response
        body["usage"] = {
            "prompt_tokens": used,
            "total_tokens": used,
        }
        body.setdefault("object", "list")
        body["model"] = model
        return body

    async def _reserve(
        self,
        key: VerifiedApiKey,
        model: str,
        request_id: str,
        period: datetime,
        reservation: int,
        started: float,
    ) -> None:
        failure: EmbeddingGatewayError | None = None
        async with self._session_factory.begin() as session:
            user_quota = await session.scalar(
                select(UserServiceQuota)
                .where(
                    UserServiceQuota.user_id == key.owner_id,
                    UserServiceQuota.service_code == self.service_code,
                )
                .with_for_update()
            )
            subscription = await session.scalar(
                select(ProjectServiceSubscription)
                .where(
                    ProjectServiceSubscription.project_id == key.project_id,
                    ProjectServiceSubscription.service_code == self.service_code,
                    ProjectServiceSubscription.status == "active",
                )
                .with_for_update()
            )
            if user_quota is None:
                failure = EmbeddingGatewayError(
                    "user_quota_missing", "项目 owner 尚未获得 Embedding 服务额度", 403, request_id
                )
            elif subscription is None:
                failure = EmbeddingGatewayError(
                    "service_not_enabled", "项目尚未申请 Embedding 服务", 403, request_id
                )
            else:
                await session.execute(
                    insert(ServiceUsageBucket)
                    .values(
                        project_id=key.project_id,
                        service_code=self.service_code,
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
                        ServiceUsageBucket.service_code == self.service_code,
                        ServiceUsageBucket.period_start == period,
                    )
                    .with_for_update()
                )
                assert bucket is not None
                aggregate = await session.execute(
                    select(
                        func.coalesce(func.sum(ServiceUsageBucket.tokens_used), 0),
                        func.coalesce(func.sum(ServiceUsageBucket.tokens_reserved), 0),
                    )
                    .join(Project, Project.id == ServiceUsageBucket.project_id)
                    .where(
                        Project.owner_id == key.owner_id,
                        ServiceUsageBucket.service_code == self.service_code,
                        ServiceUsageBucket.period_start == period,
                    )
                )
                used, reserved = aggregate.one()
                if used + reserved + reservation > user_quota.monthly_token_limit:
                    failure = EmbeddingGatewayError(
                        "user_quota_exceeded", "用户本月 Embedding token 额度不足", 429, request_id
                    )
                elif subscription.monthly_token_limit is None:
                    failure = EmbeddingGatewayError(
                        "service_quota_configuration_error",
                        "Embedding 服务项目额度配置异常",
                        500,
                        request_id,
                    )
                elif (
                    bucket.tokens_used + bucket.tokens_reserved + reservation
                    > subscription.monthly_token_limit
                ):
                    failure = EmbeddingGatewayError(
                        "project_quota_exceeded",
                        "项目本月 Embedding token 额度不足",
                        429,
                        request_id,
                    )
                else:
                    bucket.tokens_reserved += reservation
                    await self._recorder.record_stages_in_session(
                        session,
                        request_id,
                        {"quota": "allowed", "routing": "allowed", "service_call": "pending"},
                    )
            if failure is not None:
                await self._recorder.record_stage_in_session(
                    session, request_id, "quota", "denied", error_code=failure.code
                )
                await self._recorder.finish_in_session(
                    session,
                    request_id,
                    "denied",
                    latency_ms=self._latency(started),
                    error_code=failure.code,
                    error_message=str(failure),
                    description=f"模型 {model}：{failure}",
                )
        if failure is not None:
            raise failure

    async def _release(
        self,
        key: VerifiedApiKey,
        request_id: str,
        period: datetime,
        reservation: int,
        started: float,
        error_code: str,
        message: str,
    ) -> None:
        async with self._session_factory.begin() as session:
            bucket = await session.scalar(
                select(ServiceUsageBucket)
                .where(
                    ServiceUsageBucket.project_id == key.project_id,
                    ServiceUsageBucket.service_code == self.service_code,
                    ServiceUsageBucket.period_start == period,
                )
                .with_for_update()
            )
            if bucket is not None:
                bucket.tokens_reserved = max(0, bucket.tokens_reserved - reservation)
            await self._recorder.record_stage_in_session(
                session, request_id, "service_call", "failed", error_code=error_code
            )
            await self._recorder.finish_in_session(
                session,
                request_id,
                "failed",
                latency_ms=self._latency(started),
                error_code=error_code,
                error_message=message,
                description=f"Embedding 调用失败：{message}",
            )

    @staticmethod
    def _latency(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))
