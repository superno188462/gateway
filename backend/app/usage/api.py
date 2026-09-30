"""用户可见范围内的项目操作日志查询和管理员保留任务端点。"""

from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.container import get_current_admin, get_current_user, get_request_log_service
from app.infrastructure.db.models import User
from app.usage.application import (
    RequestLogItem,
    RequestLogNotFoundError,
    RequestLogPage,
    RequestLogQueryError,
    RequestLogService,
    RequestUsageSummary,
    RetentionRunInfo,
)

router = APIRouter(prefix="/api/v1", tags=["Request Logs and Usage"])
admin_router = APIRouter(prefix="/api/admin/v1/requests", tags=["Request Log Retention"])


class RequestLogResponse(BaseModel):
    """隐私安全的请求元数据，不包含 Authorization、提示词和回复正文。"""

    request_id: str
    trace_id: str
    event_type: Literal["service_call", "project_operation"]
    actor_user_id: UUID | None
    actor_username: str | None
    project_id: UUID | None
    api_key_id: UUID | None
    project_name: str | None
    service_code: str
    status: str
    latency_ms: int
    error_code: str | None
    error_message: str | None
    description: str | None
    created_at: datetime

    @classmethod
    def from_item(cls, item: RequestLogItem) -> "RequestLogResponse":
        return cls(
            request_id=item.request_id,
            trace_id=item.trace_id,
            event_type=item.event_type,
            actor_user_id=item.actor_user_id,
            actor_username=item.actor_username,
            project_id=item.project_id,
            api_key_id=item.api_key_id,
            project_name=item.project_name,
            service_code=item.service_code,
            status=item.status,
            latency_ms=item.latency_ms,
            error_code=item.error_code,
            error_message=item.error_message,
            description=item.description,
            created_at=item.created_at,
        )


class RequestLogPageResponse(BaseModel):
    """数据库分页响应，明确返回页码、页容量、匹配总数和总页数。"""

    items: list[RequestLogResponse]
    next_cursor: str | None
    page: int
    page_size: int
    total_count: int
    total_pages: int

    @classmethod
    def from_page(cls, page: RequestLogPage) -> "RequestLogPageResponse":
        return cls(
            items=[RequestLogResponse.from_item(item) for item in page.items],
            next_cursor=page.next_cursor,
            page=page.page,
            page_size=page.page_size,
            total_count=page.total_count,
            total_pages=page.total_pages,
        )


class RequestUsageSummaryResponse(BaseModel):
    """指定 UTC 时间和项目筛选范围内的调用汇总。"""

    start_at: datetime
    end_at: datetime
    request_count: int
    succeeded_count: int
    failed_count: int
    denied_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    average_latency_ms: int
    cost_cny: None = Field(description="暂无模型定价时固定为 null，客户端应显示 -。")
    by_model: list[dict[str, int | str]]
    by_day: list[dict[str, date | int]]

    @classmethod
    def from_summary(cls, summary: RequestUsageSummary) -> "RequestUsageSummaryResponse":
        data = asdict(summary)
        return cls(**data)


class RetentionRunResponse(BaseModel):
    """日志保留任务状态，不暴露异常详情或数据库错误。"""

    id: UUID
    status: Literal["running", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None
    deleted_count: int
    error_code: str | None

    @classmethod
    def from_info(cls, info: RetentionRunInfo) -> "RetentionRunResponse":
        return cls(**asdict(info))


def _raise_log_error(error: RuntimeError) -> HTTPException:
    request_id = f"req_{uuid4().hex}"
    if isinstance(error, RequestLogNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": str(error), "request_id": request_id},
        )
    if isinstance(error, RequestLogQueryError):
        return HTTPException(
            status_code=422,
            detail={"code": "invalid_query", "message": str(error), "request_id": request_id},
        )
    return HTTPException(
        status_code=500,
        detail={
            "code": "request_log_error",
            "message": "日志查询失败",
            "request_id": request_id,
        },
    )


def _resolve_window(
    start_at: datetime | None, end_at: datetime | None
) -> tuple[datetime, datetime]:
    end = end_at or datetime.now(UTC)
    start = start_at or end - timedelta(days=7)
    return start, end


@router.get(
    "/usage/summary",
    response_model=RequestUsageSummaryResponse,
    summary="查询调用用量汇总",
)
async def usage_summary(
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
    start_at: Annotated[
        datetime | None, Query(description="UTC 开始时间，包含。默认最近 7 天。")
    ] = None,
    end_at: Annotated[
        datetime | None, Query(description="UTC 结束时间，不包含。默认当前时间。")
    ] = None,
    project_id: Annotated[UUID | None, Query(description="可选项目筛选。")] = None,
) -> RequestUsageSummaryResponse:
    """管理员查看全局数据；普通用户仅查看可访问项目的数据。"""
    start, end = _resolve_window(start_at, end_at)
    try:
        summary = await service.summary(user, start_at=start, end_at=end, project_id=project_id)
        return RequestUsageSummaryResponse.from_summary(summary)
    except RuntimeError as error:
        raise _raise_log_error(error) from error


@router.get(
    "/requests",
    response_model=RequestLogPageResponse,
    summary="查询当前用户可访问项目的操作日志",
)
async def list_visible_requests(
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
    start_at: Annotated[
        datetime | None, Query(description="UTC 开始时间，包含。默认最近 7 天。")
    ] = None,
    end_at: Annotated[
        datetime | None, Query(description="UTC 结束时间，不包含。默认当前时间。")
    ] = None,
    project_id: Annotated[
        UUID | None, Query(description="可选项目筛选；只能查询当前用户有权限查看的项目。")
    ] = None,
    log_status: Annotated[
        Literal["received", "succeeded", "failed", "denied"] | None, Query(alias="status")
    ] = None,
    service_code: Annotated[str | None, Query(max_length=50)] = None,
    request_id: Annotated[str | None, Query(max_length=64)] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    page_number: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RequestLogPageResponse:
    """查询当前用户可见项目的日志；项目可见范围由应用服务强制执行。"""
    start, end = _resolve_window(start_at, end_at)
    try:
        page = await service.list_requests(
            user,
            start_at=start,
            end_at=end,
            project_id=project_id,
            status=log_status,
            service_code=service_code,
            request_id=request_id,
            cursor=cursor,
            limit=page_size,
            page=page_number,
        )
        return RequestLogPageResponse.from_page(page)
    except RuntimeError as error:
        raise _raise_log_error(error) from error


@router.get(
    "/requests/{request_id}",
    response_model=RequestLogResponse,
    summary="查询当前用户可访问项目中的单条操作日志",
)
async def get_visible_request(
    request_id: str,
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
) -> RequestLogResponse:
    """Return one log only if its project is visible to the current user."""
    try:
        return RequestLogResponse.from_item(await service.get_request(user, request_id))
    except RuntimeError as error:
        raise _raise_log_error(error) from error


@router.get(
    "/projects/{project_id}/requests",
    response_model=RequestLogPageResponse,
    summary="按页码查询项目操作日志",
)
async def list_requests(
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
    project_id: UUID,
    start_at: Annotated[
        datetime | None, Query(description="UTC 开始时间，包含。默认最近 7 天。")
    ] = None,
    end_at: Annotated[
        datetime | None, Query(description="UTC 结束时间，不包含。默认当前时间。")
    ] = None,
    log_status: Annotated[
        Literal["received", "succeeded", "failed", "denied"] | None,
        Query(alias="status", description="请求状态筛选。"),
    ] = None,
    service_code: Annotated[str | None, Query(max_length=50)] = None,
    request_id: Annotated[
        str | None, Query(max_length=64, description="请求 ID 精确筛选。")
    ] = None,
    cursor: Annotated[str | None, Query(max_length=512, description="上一页返回的游标。")] = None,
    page_number: Annotated[int, Query(alias="page", ge=1, description="从 1 开始的页码。")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，最大 100。")] = 50,
    limit: Annotated[int | None, Query(ge=1, le=100, deprecated=True)] = None,
) -> RequestLogPageResponse:
    """只有拥有项目查看权限的用户才能查询该项目操作日志。"""
    start, end = _resolve_window(start_at, end_at)
    try:
        request_page = await service.list_requests(
            user,
            start_at=start,
            end_at=end,
            project_id=project_id,
            status=log_status,
            service_code=service_code,
            request_id=request_id,
            cursor=cursor,
            limit=limit or page_size,
            page=page_number,
        )
        return RequestLogPageResponse.from_page(request_page)
    except RuntimeError as error:
        raise _raise_log_error(error) from error


@router.get(
    "/projects/{project_id}/requests/{request_id}",
    response_model=RequestLogResponse,
    summary="查询单条操作日志",
)
async def get_request(
    project_id: UUID,
    request_id: str,
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
) -> RequestLogResponse:
    """仅对可访问项目的成员或管理员返回该请求元数据。"""
    try:
        return RequestLogResponse.from_item(
            await service.get_request(user, request_id, project_id=project_id)
        )
    except RuntimeError as error:
        raise _raise_log_error(error) from error


@admin_router.get(
    "",
    response_model=RequestLogPageResponse,
    summary="管理员查询全局操作日志",
)
async def list_all_requests(
    admin: Annotated[User, Depends(get_current_admin)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
    start_at: Annotated[
        datetime | None, Query(description="UTC 开始时间，包含。默认最近 7 天。")
    ] = None,
    end_at: Annotated[
        datetime | None, Query(description="UTC 结束时间，不包含。默认当前时间。")
    ] = None,
    project_id: Annotated[UUID | None, Query(description="可选项目筛选。")] = None,
    log_status: Annotated[
        Literal["received", "succeeded", "failed", "denied"] | None,
        Query(alias="status", description="请求状态筛选。"),
    ] = None,
    service_code: Annotated[str | None, Query(max_length=50)] = None,
    request_id: Annotated[str | None, Query(max_length=64)] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    page_number: Annotated[int, Query(alias="page", ge=1, description="从 1 开始的页码。")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，最大 100。")] = 50,
    limit: Annotated[int | None, Query(ge=1, le=100, deprecated=True)] = None,
) -> RequestLogPageResponse:
    """仅管理员可查全局操作日志；所有筛选条件均由后端执行。"""
    start, end = _resolve_window(start_at, end_at)
    try:
        request_page = await service.list_requests(
            admin,
            start_at=start,
            end_at=end,
            project_id=project_id,
            status=log_status,
            request_id=request_id,
            cursor=cursor,
            limit=limit or page_size,
            page=page_number,
            service_code=service_code,
        )
        return RequestLogPageResponse.from_page(request_page)
    except RuntimeError as error:
        raise _raise_log_error(error) from error


@admin_router.get(
    "/logs/{request_id}",
    response_model=RequestLogResponse,
    summary="管理员查询单条操作日志",
)
async def get_any_request(
    request_id: str,
    admin: Annotated[User, Depends(get_current_admin)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
) -> RequestLogResponse:
    """管理员按 Request ID 查询脱敏详情，包括已删除项目的历史记录。"""
    try:
        return RequestLogResponse.from_item(await service.get_request(admin, request_id))
    except RuntimeError as error:
        raise _raise_log_error(error) from error


@admin_router.get(
    "/retention",
    response_model=RetentionRunResponse | None,
    summary="查询最近日志保留任务",
)
async def latest_retention_run(
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
) -> RetentionRunResponse | None:
    """管理员查看自动保留任务最近一次状态。"""
    run = await service.latest_retention_run()
    return RetentionRunResponse.from_info(run) if run is not None else None


@admin_router.post(
    "/retention/run",
    response_model=RetentionRunResponse,
    status_code=status.HTTP_200_OK,
    summary="立即执行日志保留任务",
)
async def run_retention(
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[RequestLogService, Depends(get_request_log_service)],
) -> RetentionRunResponse:
    """手动清理 30 天之外且不在全局最新 10,000 条内的日志。"""
    try:
        return RetentionRunResponse.from_info(await service.run_retention())
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "retention_failed",
                "message": "日志保留任务执行失败",
                "request_id": f"req_{uuid4().hex}",
            },
        ) from error
