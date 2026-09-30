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
        description: str | None = None,
    ) -> None:
        """认证项目 Key 后、检查额度和调用服务前，先持久化请求与 Trace ID。"""
        async with self._session_factory.begin() as session:
            await self.start_in_session(
                session,
                request_id=request_id,
                project_id=project_id,
                api_key_id=api_key_id,
                service_code=service_code,
                description=description,
            )

    async def start_in_session(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        project_id: UUID,
        api_key_id: UUID,
        service_code: str,
        description: str | None = None,
    ) -> None:
        """在调用方事务中创建请求记录，以便与服务专属元数据一起提交。"""
        session.add(
            GatewayRequest(
                request_id=request_id,
                trace_id=request_id,
                project_id=project_id,
                api_key_id=api_key_id,
                service_code=service_code,
                status="received",
                audit_result="allowed",
                audit_steps={"authentication": {"result": "allowed"}},
                description=description,
            )
        )

    async def record_auth_rejection(
        self,
        *,
        request_id: str,
        service_code: str,
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
                    status="denied",
                    audit_result="denied",
                    audit_steps={"authentication": {"result": "denied", "error_code": error_code}},
                    description="请求未通过 API Key 认证",
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

    async def record_stages_in_session(
        self,
        session: AsyncSession,
        request_id: str,
        stages: dict[AuditStage, AuditOutcome],
    ) -> None:
        """在同一事务中一次性追加多个阶段，避免重复查询和锁定请求记录。"""
        record = await self._get_record(session, request_id)
        record.audit_steps = {
            **record.audit_steps,
            **{stage: {"result": outcome} for stage, outcome in stages.items()},
        }
        if record.audit_result != "denied":
            record.audit_result = "allowed"

    async def finish(
        self,
        request_id: str,
        outcome: RequestOutcome,
        *,
        latency_ms: int,
        description: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict[str, object] | None = None,
    ) -> None:
        """结束一次请求并保存通用结果描述，不保存请求或返回正文。"""
        async with self._session_factory.begin() as session:
            await self.finish_in_session(
                session,
                request_id,
                outcome,
                latency_ms=latency_ms,
                description=description,
                error_code=error_code,
                error_message=error_message,
                error_details=error_details,
            )

    async def finish_in_session(
        self,
        session: AsyncSession,
        request_id: str,
        outcome: RequestOutcome,
        *,
        latency_ms: int,
        description: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict[str, object] | None = None,
    ) -> None:
        """在调用方事务内完成请求记录，不保存提示词或返回正文。"""
        record = await self._get_record(session, request_id)
        record.status = outcome
        record.latency_ms = latency_ms
        if description is not None:
            record.description = description[:1000]
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
