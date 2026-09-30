"""LLM 网关用例：授权、额度预留、Provider 调用、结算和无正文用量记录。"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import anyio
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.api_keys import VerifiedApiKey
from app.infrastructure.db.models import (
    LlmRequestUsage,
    Project,
    ProjectServiceSubscription,
    ServiceUsageBucket,
    UserServiceQuota,
)
from app.request_logging.application import GatewayRequestRecorder
from app.services.llm.domain import (
    ChatMessage,
    LlmProvider,
    ProviderCallError,
    ProviderCompletion,
    ProviderParameterError,
    ProviderStreamEvent,
)
from app.services.llm.providers.mock import estimate_tokens

logger = logging.getLogger("gateway.service.llm")


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
        request_recorder: GatewayRequestRecorder,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._request_recorder = request_recorder

    async def complete(
        self,
        api_key: VerifiedApiKey,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        parameters: dict[str, object] | None = None,
        request_id: str | None = None,
    ) -> GatewayCompletion:
        """预留最大用量后调用 Provider，成功记实际用量，失败释放预留。"""
        request_id = request_id or f"req_{uuid4().hex}"
        started = time.perf_counter()
        async with self._session_factory.begin() as session:
            await self._request_recorder.start_in_session(
                session,
                request_id=request_id,
                project_id=api_key.project_id,
                api_key_id=api_key.id,
                service_code=self.service_code,
                description=f"LLM 请求处理中，模型 {model}",
                actor_user_id=api_key.user_id,
                actor_username=api_key.username,
            )
            session.add(LlmRequestUsage(request_id=request_id, model=model))
        try:
            model_supported = await self._provider.supports(model)
        except Exception as error:
            logger.exception(
                "audit_stage stage=routing result=failed request_id=%s model=%s "
                "error_code=routing_error",
                request_id,
                model,
            )
            await self._request_recorder.record_stage(
                request_id, "routing", "failed", error_code="routing_error"
            )
            await self._request_recorder.finish(
                request_id,
                "failed",
                latency_ms=self._latency(started),
                error_code="routing_error",
                error_message="LLM 服务路由暂时不可用",
                description=f"LLM 路由失败：{model}",
            )
            raise GatewayRequestError(
                "routing_error", "LLM 服务路由暂时不可用", 502, request_id
            ) from error
        if not model_supported:
            logger.warning(
                "audit_stage stage=routing result=failed request_id=%s model=%s "
                "error_code=model_not_found",
                request_id,
                model,
            )
            await self._request_recorder.record_stage(
                request_id,
                "routing",
                "failed",
                error_code="model_not_found",
            )
            await self._request_recorder.finish(
                request_id,
                "failed",
                latency_ms=self._latency(started),
                error_code="model_not_found",
                error_message="请求的模型不存在，或当前没有可用的上游路由",
                description=f"模型 {model} 不存在或没有可用路由",
            )
            raise GatewayRequestError("model_not_found", "请求的模型不存在", 404, request_id)
        logger.info(
            "audit_stage stage=routing result=allowed request_id=%s model=%s",
            request_id,
            model,
        )
        prompt_tokens = estimate_tokens(
            json.dumps([message.as_payload() for message in messages], ensure_ascii=False)
        )
        count = (parameters or {}).get("n", 1)
        choice_count = count if isinstance(count, int) and not isinstance(count, bool) else 1
        reservation = prompt_tokens + max_tokens * choice_count
        period_start = self._period_start()
        await self._reserve(api_key, request_id, model, period_start, reservation, started)
        logger.info(
            "audit_stage stage=service_call result=started request_id=%s model=%s",
            request_id,
            model,
        )
        try:
            completion = await self._provider.complete(model, messages, max_tokens, parameters)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await self._release_usage(
                    api_key,
                    period_start,
                    reservation,
                    request_id=request_id,
                    latency_ms=self._latency(started),
                    error_code="request_cancelled",
                )
            raise
        except ProviderCallError as error:
            await self._release_usage(
                api_key,
                period_start,
                reservation,
                request_id=request_id,
                latency_ms=self._latency(started),
                error_code=error.code,
                error_message=str(error),
                error_details=error.diagnostics,
            )
            status_code = 400 if error.parameter_error else 502
            raise GatewayRequestError(error.code, str(error), status_code, request_id) from error
        except ProviderParameterError as error:
            await self._release_usage(
                api_key,
                period_start,
                reservation,
                request_id=request_id,
                latency_ms=self._latency(started),
                error_code="upstream_invalid_parameters",
                error_message=str(error),
            )
            raise GatewayRequestError(
                "upstream_invalid_parameters",
                "上游拒绝了请求参数；请检查参数格式及当前模型的支持情况",
                400,
                request_id,
            ) from error
        except Exception as error:
            logger.exception(
                "audit_stage stage=service_call result=failed request_id=%s model=%s "
                "error_code=provider_error exception_type=%s",
                request_id,
                model,
                type(error).__name__,
            )
            await self._release_usage(
                api_key,
                period_start,
                reservation,
                request_id=request_id,
                latency_ms=self._latency(started),
                error_code="provider_error",
                error_message="LLM 上游服务暂时不可用，请检查供应商连接和网关日志",
            )
            raise GatewayRequestError(
                "provider_error", "LLM 上游服务暂时不可用", 502, request_id
            ) from error
        await self._settle_usage(
            api_key,
            period_start,
            reservation,
            completion,
            request_id=request_id,
            latency_ms=self._latency(started),
        )
        logger.info(
            "audit_stage stage=service_call result=succeeded request_id=%s model=%s "
            "total_tokens=%d",
            request_id,
            model,
            completion.prompt_tokens + completion.completion_tokens,
        )
        return GatewayCompletion(request_id, model, completion)

    async def stream(
        self,
        api_key: VerifiedApiKey,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        parameters: dict[str, object] | None = None,
        request_id: str | None = None,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        """流式调用 Provider 并逐事件转发；结束时结算用量，取消或失败时释放预留。"""
        request_id = request_id or f"req_{uuid4().hex}"
        started = time.perf_counter()
        async with self._session_factory.begin() as session:
            await self._request_recorder.start_in_session(
                session,
                request_id=request_id,
                project_id=api_key.project_id,
                api_key_id=api_key.id,
                service_code=self.service_code,
                description=f"LLM 流式请求处理中，模型 {model}",
                actor_user_id=api_key.user_id,
                actor_username=api_key.username,
            )
            session.add(LlmRequestUsage(request_id=request_id, model=model))
        try:
            model_supported = await self._provider.supports(model)
        except Exception as error:
            await self._request_recorder.record_stage(
                request_id, "routing", "failed", error_code="routing_error"
            )
            await self._request_recorder.finish(
                request_id,
                "failed",
                latency_ms=self._latency(started),
                error_code="routing_error",
                error_message="LLM 服务路由暂时不可用",
                description=f"LLM 路由失败：{model}",
            )
            raise GatewayRequestError(
                "routing_error", "LLM 服务路由暂时不可用", 502, request_id
            ) from error
        if not model_supported:
            await self._request_recorder.record_stage(
                request_id, "routing", "failed", error_code="model_not_found"
            )
            await self._request_recorder.finish(
                request_id,
                "failed",
                latency_ms=self._latency(started),
                error_code="model_not_found",
                error_message="请求的模型不存在，或当前没有可用的上游路由",
                description=f"模型 {model} 不存在或没有可用路由",
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
        logger.info(
            "audit_stage stage=service_call result=started request_id=%s model=%s stream=true",
            request_id,
            model,
        )

        output_fragments: list[str] = []
        prompt_usage: int | None = None
        completion_usage: int | None = None
        saw_done = False
        provider_stream = self._provider.stream(
            model, messages, max_tokens, parameters, request_id=request_id
        )
        try:
            async for event in provider_stream:
                if event.headers_received:
                    yield event
                    continue
                if event.done:
                    saw_done = True
                    break
                if event.data is None:
                    continue
                data = dict(event.data)
                data["model"] = model
                usage = data.get("usage")
                if isinstance(usage, dict):
                    raw_prompt = usage.get("prompt_tokens")
                    raw_completion = usage.get("completion_tokens")
                    if isinstance(raw_prompt, int) and raw_prompt >= 0:
                        prompt_usage = raw_prompt
                    if isinstance(raw_completion, int) and raw_completion >= 0:
                        completion_usage = raw_completion
                choices = data.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta")
                        if isinstance(delta, dict):
                            content = delta.get("content")
                            if isinstance(content, str):
                                output_fragments.append(content)
                yield ProviderStreamEvent(data)
            if not saw_done:
                raise ProviderCallError(
                    "upstream_stream_incomplete",
                    "上游流式响应未正常结束",
                    {"attempts": []},
                )
            output_text = "".join(output_fragments)
            prompt_tokens = prompt_usage if prompt_usage is not None else prompt_tokens
            completion_tokens = (
                completion_usage if completion_usage is not None else estimate_tokens(output_text)
            )
            completion = ProviderCompletion(
                content=output_text,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                finish_reason="stop",
            )
            await self._settle_usage(
                api_key,
                period_start,
                reservation,
                completion,
                request_id=request_id,
                latency_ms=self._latency(started),
            )
            logger.info(
                "audit_stage stage=service_call result=succeeded request_id=%s model=%s "
                "total_tokens=%d stream=true",
                request_id,
                model,
                prompt_tokens + completion_tokens,
            )
        except asyncio.CancelledError:
            await self._cleanup_cancelled_stream(
                provider_stream,
                api_key,
                period_start,
                reservation,
                request_id=request_id,
                started=started,
                partial_usage=self._partial_stream_usage(
                    prompt_tokens, prompt_usage, completion_usage, output_fragments
                ),
            )
            raise
        except GeneratorExit:
            await self._cleanup_cancelled_stream(
                provider_stream,
                api_key,
                period_start,
                reservation,
                request_id=request_id,
                started=started,
                partial_usage=self._partial_stream_usage(
                    prompt_tokens, prompt_usage, completion_usage, output_fragments
                ),
            )
            raise
        except ProviderCallError as error:
            await self._release_usage(
                api_key,
                period_start,
                reservation,
                request_id=request_id,
                latency_ms=self._latency(started),
                error_code=error.code,
                error_message=str(error),
                error_details=error.diagnostics,
                partial_usage=self._partial_stream_usage(
                    prompt_tokens, prompt_usage, completion_usage, output_fragments
                ),
            )
            status_code = 400 if error.parameter_error else 502
            raise GatewayRequestError(error.code, str(error), status_code, request_id) from error
        except ProviderParameterError as error:
            await self._release_usage(
                api_key,
                period_start,
                reservation,
                request_id=request_id,
                latency_ms=self._latency(started),
                error_code="upstream_invalid_parameters",
                error_message=str(error),
                partial_usage=self._partial_stream_usage(
                    prompt_tokens, prompt_usage, completion_usage, output_fragments
                ),
            )
            raise GatewayRequestError(
                "upstream_invalid_parameters",
                "上游拒绝了请求参数；请检查参数格式及当前模型的支持情况",
                400,
                request_id,
            ) from error
        except Exception as error:
            logger.exception(
                "audit_stage stage=service_call result=failed request_id=%s model=%s "
                "error_code=provider_error exception_type=%s stream=true",
                request_id,
                model,
                type(error).__name__,
            )
            await self._release_usage(
                api_key,
                period_start,
                reservation,
                request_id=request_id,
                latency_ms=self._latency(started),
                error_code="provider_error",
                error_message="LLM 上游服务暂时不可用，请检查供应商连接和网关日志",
                partial_usage=self._partial_stream_usage(
                    prompt_tokens, prompt_usage, completion_usage, output_fragments
                ),
            )
            raise GatewayRequestError(
                "provider_error", "LLM 上游服务暂时不可用", 502, request_id
            ) from error
        yield ProviderStreamEvent(None, done=True)

    async def _cleanup_cancelled_stream(
        self,
        provider_stream: AsyncGenerator[ProviderStreamEvent, None],
        api_key: VerifiedApiKey,
        period_start: datetime,
        reservation: int,
        *,
        request_id: str,
        started: float,
        partial_usage: tuple[int, int] | None,
    ) -> None:
        """Shield disconnect cleanup so cancellation cannot interrupt quota release."""
        with anyio.CancelScope(shield=True):
            try:
                await provider_stream.aclose()
            finally:
                await self._release_usage(
                    api_key,
                    period_start,
                    reservation,
                    request_id=request_id,
                    latency_ms=self._latency(started),
                    error_code="request_cancelled",
                    partial_usage=partial_usage,
                )

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
        quota_started = time.perf_counter()
        quota_timings: dict[str, float] = {}
        audit_context: dict[str, object] = {
            "requested_tokens": reservation,
            "user_monthly_limit": None,
            "user_tokens_used": None,
            "user_tokens_reserved": None,
            "project_monthly_limit": None,
            "project_tokens_used": None,
            "project_tokens_reserved": None,
        }
        async with self._session_factory.begin() as session:
            owner_id = api_key.owner_id
            # 用户额度行在该用户所有项目间共享；锁它即可串行化总额度检查，
            # 无需再额外锁 User 行。owner_id 已在 API Key 认证查询中一并读取。
            query_started = time.perf_counter()
            user_quota = await session.scalar(
                select(UserServiceQuota)
                .where(
                    UserServiceQuota.user_id == owner_id,
                    UserServiceQuota.service_code == self.service_code,
                )
                .with_for_update()
            )
            quota_timings["user_quota_lock_ms"] = (time.perf_counter() - query_started) * 1000
            query_started = time.perf_counter()
            subscription = await session.scalar(
                select(ProjectServiceSubscription)
                .where(
                    ProjectServiceSubscription.project_id == api_key.project_id,
                    ProjectServiceSubscription.service_code == self.service_code,
                    ProjectServiceSubscription.status == "active",
                )
                .with_for_update()
            )
            quota_timings["subscription_lock_ms"] = (time.perf_counter() - query_started) * 1000
            if user_quota is None:
                audit_context["decision"] = "user_quota_missing"
                rejection = GatewayRequestError(
                    "user_quota_missing", "项目 owner 尚未获得该服务额度", 403, request_id
                )
            elif subscription is None:
                audit_context["decision"] = "service_not_enabled"
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
                query_started = time.perf_counter()
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
                quota_timings["project_bucket_lock_ms"] = (
                    time.perf_counter() - query_started
                ) * 1000
                assert bucket is not None
                audit_context.update(
                    {
                        "user_monthly_limit": user_quota.monthly_token_limit,
                        "project_monthly_limit": subscription.monthly_token_limit,
                        "project_tokens_used": bucket.tokens_used,
                        "project_tokens_reserved": bucket.tokens_reserved,
                    }
                )
                query_started = time.perf_counter()
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
                quota_timings["user_usage_aggregate_ms"] = (
                    time.perf_counter() - query_started
                ) * 1000
                user_tokens_used, user_tokens_reserved = usage.one()
                audit_context.update(
                    {
                        "user_tokens_used": user_tokens_used,
                        "user_tokens_reserved": user_tokens_reserved,
                    }
                )
                if (
                    user_tokens_used + user_tokens_reserved + reservation
                    > user_quota.monthly_token_limit
                ):
                    audit_context["decision"] = "user_quota_exceeded"
                    rejection = GatewayRequestError(
                        "user_quota_exceeded", "用户本月 LLM token 总额度不足", 429, request_id
                    )
                elif subscription.monthly_token_limit is None:
                    audit_context["decision"] = "service_quota_configuration_error"
                    rejection = GatewayRequestError(
                        "service_quota_configuration_error",
                        "LLM 服务项目额度配置异常",
                        500,
                        request_id,
                    )
                elif (
                    bucket.tokens_used + bucket.tokens_reserved + reservation
                    > subscription.monthly_token_limit
                ):
                    audit_context["decision"] = "project_quota_exceeded"
                    rejection = GatewayRequestError(
                        "project_quota_exceeded", "项目本月 token 分配额度不足", 429, request_id
                    )
                else:
                    audit_context["decision"] = "allowed"
                    bucket.tokens_reserved += reservation
            if rejection is not None:
                await self._request_recorder.record_stage_in_session(
                    session, request_id, "quota", "denied", error_code=rejection.code
                )
                await self._request_recorder.finish_in_session(
                    session,
                    request_id,
                    "denied",
                    latency_ms=self._latency(started),
                    error_code=rejection.code,
                    error_message=str(rejection),
                    description=f"模型 {model}：{rejection}",
                )
            else:
                await self._request_recorder.record_stages_in_session(
                    session,
                    request_id,
                    {
                        "quota": "allowed",
                        "routing": "allowed",
                        "service_call": "pending",
                    },
                )
        if rejection is not None:
            quota_timings["transaction_total_ms"] = (time.perf_counter() - quota_started) * 1000
            logger.info(
                "quota_timing request_id=%s outcome=denied timings_ms=%s",
                request_id,
                json.dumps(quota_timings, sort_keys=True),
            )
            logger.warning(
                "audit_stage stage=quota result=denied request_id=%s model=%s "
                "error_code=%s details=%s",
                request_id,
                model,
                rejection.code,
                json.dumps(audit_context, ensure_ascii=False, sort_keys=True),
            )
            raise rejection
        logger.info(
            "audit_stage stage=quota result=allowed request_id=%s model=%s details=%s",
            request_id,
            model,
            json.dumps(audit_context, ensure_ascii=False, sort_keys=True),
        )
        quota_timings["transaction_total_ms"] = (time.perf_counter() - quota_started) * 1000
        logger.info(
            "quota_timing request_id=%s outcome=allowed timings_ms=%s",
            request_id,
            json.dumps(quota_timings, sort_keys=True),
        )

    async def _settle_usage(
        self,
        api_key: VerifiedApiKey,
        period_start: datetime,
        reservation: int,
        completion: ProviderCompletion,
        *,
        request_id: str,
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
            await self._request_recorder.record_stage_in_session(
                session, request_id, "service_call", "succeeded"
            )
            llm_usage = await session.get(LlmRequestUsage, request_id, with_for_update=True)
            if llm_usage is None:
                raise LookupError(f"LLM 用量记录不存在：{request_id}")
            llm_usage.prompt_tokens = completion.prompt_tokens
            llm_usage.completion_tokens = completion.completion_tokens
            llm_usage.total_tokens = used
            llm_usage.finish_reason = completion.finish_reason
            await self._request_recorder.finish_in_session(
                session,
                request_id,
                "succeeded",
                latency_ms=latency_ms,
                description=(
                    f"LLM 调用成功，模型 {llm_usage.model}，消耗 {used} tokens "
                    f"（输入 {completion.prompt_tokens}，输出 {completion.completion_tokens}）"
                ),
            )

    async def _release_usage(
        self,
        api_key: VerifiedApiKey,
        period_start: datetime,
        reservation: int,
        *,
        request_id: str,
        latency_ms: int,
        error_code: str,
        error_message: str | None = None,
        error_details: dict[str, object] | None = None,
        partial_usage: tuple[int, int] | None = None,
    ) -> None:
        logger.error(
            "audit_stage stage=service_call result=failed request_id=%s error_code=%s message=%s",
            request_id,
            error_code,
            error_message or "LLM service call failed",
        )
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
                if partial_usage is not None:
                    bucket.tokens_used += sum(partial_usage)
            if partial_usage is not None:
                llm_usage = await session.get(LlmRequestUsage, request_id, with_for_update=True)
                if llm_usage is not None:
                    llm_usage.prompt_tokens = partial_usage[0]
                    llm_usage.completion_tokens = partial_usage[1]
                    llm_usage.total_tokens = sum(partial_usage)
                    llm_usage.finish_reason = "interrupted"
            await self._request_recorder.record_stage_in_session(
                session, request_id, "service_call", "failed", error_code=error_code
            )
            await self._request_recorder.finish_in_session(
                session,
                request_id,
                "failed",
                latency_ms=latency_ms,
                error_code=error_code,
                error_message=error_message,
                error_details=error_details,
                description=(
                    f"LLM 调用失败：{error_message or error_code}"
                    + (
                        f"；中断前计入 {sum(partial_usage)} tokens"
                        if partial_usage is not None
                        else ""
                    )
                ),
            )

    @staticmethod
    def _partial_stream_usage(
        estimated_prompt: int,
        prompt_usage: int | None,
        completion_usage: int | None,
        output_fragments: list[str],
    ) -> tuple[int, int] | None:
        """仅在上游已提供用量或已产生内容时，记录可确认的中断前用量。"""
        if prompt_usage is None and completion_usage is None and not output_fragments:
            return None
        return (
            prompt_usage if prompt_usage is not None else estimated_prompt,
            completion_usage
            if completion_usage is not None
            else estimate_tokens("".join(output_fragments)),
        )

    @staticmethod
    def _period_start() -> datetime:
        now = datetime.now(UTC)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _latency(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))
