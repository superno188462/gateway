"""全局管理员配置操作日志辅助函数。"""

import logging
from time import perf_counter
from typing import Literal

from fastapi import Request

from app.infrastructure.db.models import User
from app.request_logging.application import GatewayRequestRecorder

logger = logging.getLogger("gateway.admin_operations")


async def record_admin_operation(
    recorder: GatewayRequestRecorder,
    request: Request,
    user: User,
    operation: str,
    description: str,
    *,
    status: Literal["succeeded", "failed"] = "succeeded",
    error_code: str | None = None,
    error_message: str | None = None,
    started_at: float | None = None,
) -> None:
    """保存脱敏的全局操作记录，不让日志存储故障影响主操作结果。"""
    try:
        await recorder.record_admin_operation(
            trace_id=getattr(request.state, "trace_id", ""),
            actor_user_id=user.id,
            actor_username=user.username,
            operation=operation,
            description=description,
            status=status,
            error_code=error_code,
            error_message=error_message,
            latency_ms=round((perf_counter() - started_at) * 1000) if started_at else 0,
        )
    except Exception:
        logger.exception(
            "admin_operation_log_write_failed actor_user_id=%s operation=%s",
            user.id,
            operation,
        )
