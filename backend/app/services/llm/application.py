"""LLM 网关用例：授权、额度预留、Provider 调用、结算和无正文用量记录。"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.api_keys import VerifiedApiKey
from app.infrastructure.db.models import (
    GatewayRequest,
    Project,
    ProjectServiceSubscription,
    ServiceUsageBucket,
    User,
    UserServiceQuota,
)
from app.services.llm.domain import (
    ChatMessage,
    LlmProvider,
    ProviderCompletion,
    ProviderParameterError,
)
from app.services.llm.providers.mock import estimate_tokens


class GatewayRequestError(RuntimeError):
    """可以安全返回给 OpenAI 风格客户端的网关错误。"""

    def __init__(
        self, code: str, message: str, status_code: int, request_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class GatewayCompletion:
    """完成的模型结果和网关追踪标识。"""

    request_id: str
    model: str
    completion: ProviderCompletion


class LlmGatewayService:
    """将已验证项目 Key 的请求路由到 Provider 并实施月额度。"""

    service_code = "mock-llm-v1"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: LlmProvider,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    async def complete(
        self,
        api_key: VerifiedApiKey,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        parameters: dict[str, object] | None = None,
    ) -> GatewayCompletion:
        """预留最大用量后调用 Provider，成功记实际用量，失败释放预留。"""
        request_id = f"req_{uuid4().hex}"
        started = time.perf_counter()
        if not await self._provider.supports(model):
            await self._write_request(
                request_id,
                api_key,
                model,
                "failed",
                0,
                0,
                0,
                self._latency(started),
                "model_not_found",
            )
            raise GatewayRequestError("model_not_found", "请求的模型不存在", 404, request_id)
        prompt_tokens = estimate_tokens(
            json.dumps([message.as_payload() for message in messages], ensure_ascii=False)
        )
        count = (parameters or {}).get("n", 1)
        choice_count = count if isinstance(count, int) and not isinstance(count, bool) else 1
        reservation = prompt_tokens + max_tokens * choice_count
        period_start = self._period_start()
        await self._reserve(api_key, request_id, model, period_start, reservation, started)
        try:
            completion = await self._provider.complete(model, messages, max_tokens, parameters)
        except ProviderParameterError as error:
            await self._release_and_log(
                api_key,
                request_id,
                model,
                period_start,
                reservation,
                self._latency(started),
                error_code="upstream_invalid_parameters",
            )
            raise GatewayRequestError(
                "upstream_invalid_parameters",
                "上游拒绝了请求参数；请检查参数格式及当前模型的支持情况",
                400,
                request_id,
            ) from error
        except Exception as error:
            await self._release_and_log(
                api_key, request_id, model, period_start, reservation, self._latency(started)
            )
            raise GatewayRequestError(
                "provider_error", "LLM 上游服务暂时不可用", 502, request_id
            ) from error
        await self._settle_and_log(
            api_key,
            request_id,
            model,
            period_start,
            reservation,
            completion,
            self._latency(started),
        )
        return GatewayCompletion(request_id, model, completion)

    async def _reserve(
        self,
        api_key: VerifiedApiKey,
        request_id: str,
        model: str,
        period_start: datetime,
        reservation: int,
        started: float,
    ) -> None:
        rejection: GatewayRequestError | None = None
        async with self._session_factory.begin() as session:
            owner_id = await session.scalar(
                select(Project.owner_id).where(Project.id == api_key.project_id)
            )
            if owner_id is None:
                await self._add_log(
                    session,
                    request_id,
                    api_key,
                    model,
                    "denied",
                    0,
                    0,
                    0,
                    self._latency(started),
                    "project_not_found",
                )
                rejection = GatewayRequestError(
                    "project_not_found", "API Key 所属项目不存在", 403, request_id
                )
            else:
                # 锁用户行，在不同项目之间串行核验个人总额度，防止并发绕过上限。
                await session.scalar(select(User.id).where(User.id == owner_id).with_for_update())
                user_quota = await session.scalar(
                    select(UserServiceQuota)
                    .where(
                        UserServiceQuota.user_id == owner_id,
                        UserServiceQuota.service_code == self.service_code,
                    )
                    .with_for_update()
                )
                subscription = await session.scalar(
                    select(ProjectServiceSubscription)
                    .where(
                        ProjectServiceSubscription.project_id == api_key.project_id,
                        ProjectServiceSubscription.service_code == self.service_code,
                        ProjectServiceSubscription.status == "active",
                    )
                    .with_for_update()
                )
                if user_quota is None:
                    await self._add_log(
                        session,
                        request_id,
                        api_key,
                        model,
                        "denied",
                        0,
                        0,
                        0,
                        self._latency(started),
                        "user_quota_missing",
                    )
                    rejection = GatewayRequestError(
                        "user_quota_missing", "项目 owner 尚未获得该服务额度", 403, request_id
                    )
                elif subscription is None:
                    await self._add_log(
                        session,
                        request_id,
                        api_key,
                        model,
                        "denied",
                        0,
                        0,
                        0,
                        self._latency(started),
                        "service_not_enabled",
                    )
                    rejection = GatewayRequestError(
                        "service_not_enabled", "项目尚未申请 LLM Mock 服务", 403, request_id
                    )
                else:
                    statement = (
                        insert(ServiceUsageBucket)
                        .values(
                            project_id=api_key.project_id,
                            service_code=self.service_code,
                            period_start=period_start,
                            tokens_used=0,
                            tokens_reserved=0,
                        )
                        .on_conflict_do_nothing()
                    )
                    await session.execute(statement)
                    bucket = await session.scalar(
                        select(ServiceUsageBucket)
                        .where(
                            ServiceUsageBucket.project_id == api_key.project_id,
                            ServiceUsageBucket.service_code == self.service_code,
                            ServiceUsageBucket.period_start == period_start,
                        )
                        .with_for_update()
                    )
                    assert bucket is not None
                    usage = await session.execute(
                        select(
                            func.coalesce(func.sum(ServiceUsageBucket.tokens_used), 0),
                            func.coalesce(func.sum(ServiceUsageBucket.tokens_reserved), 0),
                        )
                        .join(Project, Project.id == ServiceUsageBucket.project_id)
                        .where(
                            Project.owner_id == owner_id,
                            ServiceUsageBucket.service_code == self.service_code,
                            ServiceUsageBucket.period_start == period_start,
                        )
                    )
                    user_tokens_used, user_tokens_reserved = usage.one()
                    if (
                        user_tokens_used + user_tokens_reserved + reservation
                        > user_quota.monthly_token_limit
                    ):
                        await self._add_log(
                            session,
                            request_id,
                            api_key,
                            model,
                            "denied",
                            0,
                            0,
                            0,
                            self._latency(started),
                            "user_quota_exceeded",
                        )
                        rejection = GatewayRequestError(
                            "user_quota_exceeded", "用户本月 LLM token 总额度不足", 429, request_id
                        )
                    elif (
                        bucket.tokens_used + bucket.tokens_reserved + reservation
                        > subscription.monthly_token_limit
                    ):
                        await self._add_log(
                            session,
                            request_id,
                            api_key,
                            model,
                            "denied",
                            0,
                            0,
                            0,
                            self._latency(started),
                            "project_quota_exceeded",
                        )
                        rejection = GatewayRequestError(
                            "project_quota_exceeded", "项目本月 token 分配额度不足", 429, request_id
                        )
                    else:
                        bucket.tokens_reserved += reservation
        if rejection is not None:
            raise rejection

    async def _settle_and_log(
        self,
        api_key: VerifiedApiKey,
        request_id: str,
        model: str,
        period_start: datetime,
        reservation: int,
        completion: ProviderCompletion,
        latency_ms: int,
    ) -> None:
        async with self._session_factory.begin() as session:
            bucket = await session.scalar(
                select(ServiceUsageBucket)
                .where(
                    ServiceUsageBucket.project_id == api_key.project_id,
                    ServiceUsageBucket.service_code == self.service_code,
                    ServiceUsageBucket.period_start == period_start,
                )
                .with_for_update()
            )
            assert bucket is not None
            used = completion.prompt_tokens + completion.completion_tokens
            bucket.tokens_reserved = max(0, bucket.tokens_reserved - reservation)
            bucket.tokens_used += used
            await self._add_log(
                session,
                request_id,
                api_key,
                model,
                "succeeded",
                completion.prompt_tokens,
                completion.completion_tokens,
                used,
                latency_ms,
                None,
            )

    async def _release_and_log(
        self,
        api_key: VerifiedApiKey,
        request_id: str,
        model: str,
        period_start: datetime,
        reservation: int,
        latency_ms: int,
        error_code: str = "provider_error",
    ) -> None:
        async with self._session_factory.begin() as session:
            bucket = await session.scalar(
                select(ServiceUsageBucket)
                .where(
                    ServiceUsageBucket.project_id == api_key.project_id,
                    ServiceUsageBucket.service_code == self.service_code,
                    ServiceUsageBucket.period_start == period_start,
                )
                .with_for_update()
            )
            if bucket is not None:
                bucket.tokens_reserved = max(0, bucket.tokens_reserved - reservation)
            await self._add_log(
                session, request_id, api_key, model, "failed", 0, 0, 0, latency_ms, error_code
            )

    async def _write_request(
        self,
        request_id: str,
        api_key: VerifiedApiKey,
        model: str,
        status: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: int,
        error_code: str | None,
    ) -> None:
        async with self._session_factory.begin() as session:
            await self._add_log(
                session,
                request_id,
                api_key,
                model,
                status,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                latency_ms,
                error_code,
            )

    async def _add_log(
        self,
        session: AsyncSession,
        request_id: str,
        api_key: VerifiedApiKey,
        model: str,
        status: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: int,
        error_code: str | None,
    ) -> None:
        session.add(
            GatewayRequest(
                request_id=request_id,
                project_id=api_key.project_id,
                api_key_id=api_key.id,
                service_code=self.service_code,
                model=model,
                status=status,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                error_code=error_code,
            )
        )

    @staticmethod
    def _period_start() -> datetime:
        now = datetime.now(UTC)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _latency(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))
