"""共用的网关请求生命周期记录器。"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import GatewayRequest

AuditStage = Literal["authentication", "quota", "routing", "service_call"]
AuditOutcome = Literal["pending", "allowed", "denied", "succeeded", "failed"]
RequestOutcome = Literal["received", "denied", "succeeded", "failed"]


class GatewayRequestRecorder:
    """写入所有网关服务共用的请求、审计、结果和用量记录。

    记录器只接收脱敏元数据；调用方不得传递 API Key、请求正文或响应正文。
    ``request_id`` 同时作为贯穿网关内部流程的 trace ID。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def start(
        self,
        *,
        request_id: str,
        project_id: UUID,
        api_key_id: UUID,
        service_code: str,
        model: str,
    ) -> None:
        """认证项目 Key 后、检查额度和调用服务前，先持久化请求与 Trace ID。"""
        async with self._session_factory.begin() as session:
            session.add(
                GatewayRequest(
                    request_id=request_id,
                    trace_id=request_id,
                    project_id=project_id,
                    api_key_id=api_key_id,
                    service_code=service_code,
                    model=model,
                    status="received",
                    audit_result="allowed",
                    audit_steps={"authentication": {"result": "allowed"}},
                    usage_metrics={},
                )
            )

    async def record_auth_rejection(
        self,
        *,
        request_id: str,
        service_code: str,
        model: str,
        latency_ms: int,
        error_code: str,
    ) -> None:
        """记录未能认证的请求；不猜测项目归属，也不保存提交的凭据。"""
        async with self._session_factory.begin() as session:
            session.add(
                GatewayRequest(
                    request_id=request_id,
                    trace_id=request_id,
                    project_id=None,
                    api_key_id=None,
                    service_code=service_code,
                    model=model,
                    status="denied",
                    audit_result="denied",
                    audit_steps={"authentication": {"result": "denied", "error_code": error_code}},
                    usage_metrics={},
                    result_summary={"response_type": "error"},
                    error_code=error_code,
                    error_message={
                        "missing_api_key": "请求缺少项目 API Key",
                        "invalid_api_key": "项目 API Key 无效、已撤销或已过期",
                    }.get(error_code, "请求认证失败"),
                    latency_ms=latency_ms,
                    finished_at=datetime.now(UTC),
                )
            )

    async def record_stage(
        self,
        request_id: str,
        stage: AuditStage,
        outcome: AuditOutcome,
        *,
        error_code: str | None = None,
    ) -> None:
        """记录认证、额度、路由或服务调用阶段的审计结果。"""
        async with self._session_factory.begin() as session:
            await self.record_stage_in_session(
                session, request_id, stage, outcome, error_code=error_code
            )

    async def record_stage_in_session(
        self,
        session: AsyncSession,
        request_id: str,
        stage: AuditStage,
        outcome: AuditOutcome,
        *,
        error_code: str | None = None,
    ) -> None:
        """在调用方事务内更新阶段结果，使额度、审计与结算保持一致。"""
        record = await self._get_record(session, request_id)
        record.audit_steps = {
            **record.audit_steps,
            stage: {"result": outcome, **({"error_code": error_code} if error_code else {})},
        }
        if outcome == "denied":
            record.audit_result = "denied"
        elif outcome in {"allowed", "succeeded"} and record.audit_result != "denied":
            record.audit_result = "allowed"

    async def finish(
        self,
        request_id: str,
        outcome: RequestOutcome,
        *,
        latency_ms: int,
        usage_metrics: dict[str, object] | None = None,
        result_summary: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict[str, object] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """结束一次请求并保存安全结果摘要和通用用量指标，不保存返回正文。"""
        async with self._session_factory.begin() as session:
            await self.finish_in_session(
                session,
                request_id,
                outcome,
                latency_ms=latency_ms,
                usage_metrics=usage_metrics,
                result_summary=result_summary,
                error_code=error_code,
                error_message=error_message,
                error_details=error_details,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

    async def finish_in_session(
        self,
        session: AsyncSession,
        request_id: str,
        outcome: RequestOutcome,
        *,
        latency_ms: int,
        usage_metrics: dict[str, object] | None = None,
        result_summary: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict[str, object] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """在调用方事务内完成请求记录，不保存提示词或返回正文。"""
        record = await self._get_record(session, request_id)
        record.status = outcome
        record.latency_ms = latency_ms
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        record.total_tokens = total_tokens
        record.usage_metrics = usage_metrics or {}
        record.result_summary = result_summary
        record.error_code = error_code
        record.error_message = error_message[:500] if error_message else None
        record.error_details = error_details
        record.finished_at = datetime.now(UTC)
        if outcome == "denied":
            record.audit_result = "denied"

    async def _get_record(self, session: AsyncSession, request_id: str) -> GatewayRequest:
        record = await session.scalar(
            select(GatewayRequest).where(GatewayRequest.request_id == request_id).with_for_update()
        )
        if record is None:
            raise LookupError(f"网关请求日志不存在：{request_id}")
        return record
