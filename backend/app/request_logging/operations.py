"""Shared project operation audit logging for control-plane mutations."""

import logging
from time import perf_counter
from uuid import UUID

from fastapi import Request

from app.infrastructure.db.models import User
from app.request_logging.application import GatewayRequestRecorder

logger = logging.getLogger("gateway.project_operations")


async def record_project_operation(
    recorder: GatewayRequestRecorder,
    request: Request,
    user: User,
    project_id: UUID,
    operation: str,
    description: str,
    api_key_id: UUID | None = None,
    *,
    started_at: float | None = None,
) -> None:
    """Record sanitized mutation metadata; never include request content or credentials."""
    try:
        await recorder.record_project_operation(
            trace_id=getattr(request.state, "trace_id", ""),
            project_id=project_id,
            actor_user_id=user.id,
            actor_username=user.username,
            operation=operation,
            description=description,
            api_key_id=api_key_id,
            latency_ms=round((perf_counter() - started_at) * 1000) if started_at else 0,
        )
    except Exception:
        logger.exception(
            "project_operation_log_write_failed project_id=%s actor_user_id=%s operation=%s",
            project_id,
            user.id,
            operation,
        )
