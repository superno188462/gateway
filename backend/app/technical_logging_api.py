"""只对管理员开放的应用技术日志查询接口。"""

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.container import get_current_admin, get_technical_log_service
from app.infrastructure.db.models import User
from app.technical_logging import TechnicalLogEntry, TechnicalLogService

router = APIRouter(prefix="/api/admin/v1/system-logs", tags=["System Logs"])


class TechnicalLogEntryResponse(BaseModel):
    """一行标准技术日志。"""

    timestamp: str
    level: str
    source: str
    trace_id: str
    message: str

    @classmethod
    def from_entry(cls, entry: TechnicalLogEntry) -> "TechnicalLogEntryResponse":
        return cls(**asdict(entry))


class TechnicalLogPageResponse(BaseModel):
    """技术日志分页响应，返回页码、页容量、匹配总数和总页数。"""

    entries: list[TechnicalLogEntryResponse]
    log_file: str
    page: int
    page_size: int
    total_count: int
    total_pages: int


@router.get("", response_model=TechnicalLogPageResponse, summary="管理员查询应用技术日志")
async def list_technical_logs(
    _: Annotated[User, Depends(get_current_admin)],
    service: Annotated[TechnicalLogService, Depends(get_technical_log_service)],
    level: Annotated[
        Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None,
        Query(description="按日志等级精确筛选。"),
    ] = None,
    trace_id: Annotated[str | None, Query(max_length=64)] = None,
    query: Annotated[str | None, Query(max_length=200, description="事件文本模糊筛选。")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TechnicalLogPageResponse:
    """按页返回最近匹配的技术日志，仅管理员可调用。"""
    try:
        entries, total_count = service.list_page(
            page=page,
            page_size=page_size,
            level=level,
            trace_id=trace_id,
            query=query,
        )
    except OSError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "technical_log_unavailable", "message": "技术日志文件暂不可读"},
        ) from error
    return TechnicalLogPageResponse(
        entries=[TechnicalLogEntryResponse.from_entry(entry) for entry in entries],
        log_file=service.file_path.name,
        page=page,
        page_size=page_size,
        total_count=total_count,
        total_pages=(total_count + page_size - 1) // page_size,
    )
