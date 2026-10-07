"""查询隐私安全的网关请求日志与用量，并执行保留策略。"""

import asyncio
import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import Date, delete, exists, func, or_, select, tuple_
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.infrastructure.db.models import (
    GatewayRequest,
    LlmRequestUsage,
    LogRetentionRun,
    Project,
    ProjectMember,
    User,
)


class RequestLogNotFoundError(RuntimeError):
    """项目或请求日志不存在，或当前用户无权查看。"""


class RequestLogQueryError(RuntimeError):
    """筛选条件或游标无效。"""


@dataclass(frozen=True, slots=True)
class RequestLogItem:
    """不包含凭据和请求正文的项目操作日志行。"""

    request_id: str
    trace_id: str
    event_type: str
    actor_user_id: UUID | None
    actor_username: str | None
    project_id: UUID | None
    api_key_id: UUID | None
    project_name: str | None
    service_code: str
    status: str
    audit_result: str
    audit_steps: dict[str, object]
    latency_ms: int
    error_code: str | None
    error_message: str | None
    error_details: dict[str, object] | None
    description: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RequestLogPage:
    """一页数据库查询结果，包含页码元数据并兼容旧游标。"""

    items: tuple[RequestLogItem, ...]
    next_cursor: str | None
    page: int
    page_size: int
    total_count: int
    total_pages: int


@dataclass(frozen=True, slots=True)
class ModelUsageSummary:
    """一个模型在筛选窗口内的请求和 token 汇总。"""

    model: str
    request_count: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class DailyUsageSummary:
    """某 UTC 日期的请求数和 token 总量。"""

    day: date
    request_count: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class RequestUsageSummary:
    """从 gateway_requests 实时汇总的可核对指标。"""

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
    cost_cny: None
    by_model: tuple[ModelUsageSummary, ...]
    by_day: tuple[DailyUsageSummary, ...]


@dataclass(frozen=True, slots=True)
class RetentionRunInfo:
    """一次操作日志保留任务的可审计状态。"""

    id: UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    deleted_count: int
    error_code: str | None


class RequestLogService:
    """按项目权限查询操作日志，并保留最近 30 天与最新 10,000 条记录。"""

    RETENTION_DAYS = 30
    MINIMUM_RECORDS = 10_000
    MAX_PAGE_SIZE = 100

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def summary(
        self,
        user: User,
        *,
        start_at: datetime,
        end_at: datetime,
        project_id: UUID | None = None,
    ) -> RequestUsageSummary:
        """Aggregate only the filtered source request rows for exact reconciliation."""
        self._validate_window(start_at, end_at)
        async with self._session_factory() as session:
            await self._ensure_project_access(session, user, project_id)
            filters = self._filters(user, start_at, end_at, project_id)
            filters.append(GatewayRequest.event_type == "service_call")
            aggregate = await session.execute(
                select(
                    func.count(GatewayRequest.id),
                    func.count(GatewayRequest.id).filter(GatewayRequest.status == "succeeded"),
                    func.count(GatewayRequest.id).filter(GatewayRequest.status == "failed"),
                    func.count(GatewayRequest.id).filter(GatewayRequest.status == "denied"),
                    func.coalesce(func.sum(LlmRequestUsage.prompt_tokens), 0),
                    func.coalesce(func.sum(LlmRequestUsage.completion_tokens), 0),
                    func.coalesce(func.sum(LlmRequestUsage.total_tokens), 0),
                    func.coalesce(func.avg(GatewayRequest.latency_ms), 0),
                )
                .select_from(GatewayRequest)
                .outerjoin(LlmRequestUsage, LlmRequestUsage.request_id == GatewayRequest.request_id)
                .outerjoin(Project, Project.id == GatewayRequest.project_id)
                .where(*filters)
            )
            (
                request_count,
                succeeded_count,
                failed_count,
                denied_count,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                average_latency_ms,
            ) = aggregate.one()
            model_rows = await session.execute(
                select(
                    LlmRequestUsage.model,
                    func.count(GatewayRequest.id),
                    func.coalesce(func.sum(LlmRequestUsage.total_tokens), 0),
                )
                .select_from(GatewayRequest)
                .join(LlmRequestUsage, LlmRequestUsage.request_id == GatewayRequest.request_id)
                .outerjoin(Project, Project.id == GatewayRequest.project_id)
                .where(*filters)
                .group_by(LlmRequestUsage.model)
                .order_by(func.sum(LlmRequestUsage.total_tokens).desc(), LlmRequestUsage.model)
                .limit(50)
            )
            daily_day = func.timezone("UTC", GatewayRequest.created_at).cast(Date).label("day")
            daily_rows = await session.execute(
                select(
                    daily_day,
                    func.count(GatewayRequest.id),
                    func.coalesce(func.sum(LlmRequestUsage.total_tokens), 0),
                )
                .select_from(GatewayRequest)
                .outerjoin(LlmRequestUsage, LlmRequestUsage.request_id == GatewayRequest.request_id)
                .outerjoin(Project, Project.id == GatewayRequest.project_id)
                .where(*filters)
                .group_by("day")
                .order_by("day")
            )
            return RequestUsageSummary(
                start_at=start_at,
                end_at=end_at,
                request_count=request_count,
                succeeded_count=succeeded_count,
                failed_count=failed_count,
                denied_count=denied_count,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                average_latency_ms=round(average_latency_ms),
                cost_cny=None,
                by_model=tuple(ModelUsageSummary(*row) for row in model_rows.all()),
                by_day=tuple(
                    DailyUsageSummary(
                        row[0],
                        row[1],
                        row[2],
                    )
                    for row in daily_rows.all()
                ),
            )

    async def list_requests(
        self,
        user: User,
        *,
        start_at: datetime,
        end_at: datetime,
        project_id: UUID | None = None,
        status: str | None = None,
        service_code: str | None = None,
        request_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        page: int = 1,
    ) -> RequestLogPage:
        """从数据库读取一页脱敏日志，返回总数和页数；兼容旧游标请求。"""
        self._validate_window(start_at, end_at)
        if limit < 1 or limit > self.MAX_PAGE_SIZE:
            raise RequestLogQueryError(f"每页条数必须在 1 到 {self.MAX_PAGE_SIZE} 之间")
        if page < 1:
            raise RequestLogQueryError("页码必须大于或等于 1")
        cursor_position = self._decode_cursor(cursor) if cursor else None
        async with self._session_factory() as session:
            await self._ensure_project_access(session, user, project_id)
            filters = self._filters(user, start_at, end_at, project_id, request_id=request_id)
            if status is not None:
                filters.append(GatewayRequest.status == status)
            if service_code:
                filters.append(GatewayRequest.service_code == service_code.strip())
            if request_id:
                request_value = request_id.strip()
                filters.append(
                    or_(
                        GatewayRequest.request_id == request_value,
                        GatewayRequest.trace_id == request_value,
                    )
                )
            total_count = await session.scalar(
                select(func.count())
                .select_from(GatewayRequest)
                .outerjoin(Project, Project.id == GatewayRequest.project_id)
                .where(*filters)
            )
            total_count = total_count or 0
            if cursor_position is not None:
                filters.append(
                    tuple_(GatewayRequest.created_at, GatewayRequest.id) < cursor_position
                )
            statement = (
                select(GatewayRequest, Project.name, User.username)
                .outerjoin(Project, Project.id == GatewayRequest.project_id)
                .outerjoin(User, User.id == GatewayRequest.actor_user_id)
                .where(*filters)
                .order_by(GatewayRequest.created_at.desc(), GatewayRequest.id.desc())
                .limit(limit + 1)
            )
            if cursor_position is None:
                statement = statement.offset((page - 1) * limit)
            rows = await session.execute(statement)
            result_rows = rows.all()
            has_more = len(result_rows) > limit
            result_rows = result_rows[:limit]
            items = tuple(
                RequestLogItem(
                    request_id=row.request_id,
                    trace_id=row.trace_id,
                    event_type=row.event_type,
                    actor_user_id=row.actor_user_id,
                    actor_username=row.actor_username or actor_username,
                    project_id=row.project_id,
                    api_key_id=row.api_key_id,
                    project_name=project_name,
                    service_code=row.service_code,
                    status=row.status,
                    audit_result=row.audit_result,
                    audit_steps=row.audit_steps,
                    latency_ms=row.latency_ms,
                    error_code=row.error_code,
                    error_message=row.error_message,
                    error_details=row.error_details,
                    description=row.description,
                    created_at=row.created_at,
                )
                for row, project_name, actor_username in result_rows
            )
            total_pages = (total_count + limit - 1) // limit
            has_next_page = page * limit < total_count
            next_cursor = (
                self._encode_cursor(result_rows[-1][0].created_at, result_rows[-1][0].id)
                if (has_more if cursor_position is not None else has_next_page) and result_rows
                else None
            )
            return RequestLogPage(items, next_cursor, page, limit, total_count, total_pages)

    async def get_request(
        self, user: User, request_id: str, project_id: UUID | None = None
    ) -> RequestLogItem:
        """Return a single sanitized request record after applying project visibility."""
        async with self._session_factory() as session:
            query = (
                select(GatewayRequest, Project.name, User.username)
                .outerjoin(Project, Project.id == GatewayRequest.project_id)
                .outerjoin(User, User.id == GatewayRequest.actor_user_id)
                .where(GatewayRequest.request_id == request_id)
            )
            if user.role != "admin":
                query = query.where(self._visibility_filter(user))
                if project_id is not None:
                    query = query.where(GatewayRequest.project_id == project_id)
            result = await session.execute(query)
            row = result.first()
            if row is None:
                raise RequestLogNotFoundError("请求日志不存在或无权查看")
            item, project_name, actor_username = row
            return RequestLogItem(
                request_id=item.request_id,
                trace_id=item.trace_id,
                event_type=item.event_type,
                actor_user_id=item.actor_user_id,
                actor_username=item.actor_username or actor_username,
                project_id=item.project_id,
                api_key_id=item.api_key_id,
                project_name=project_name,
                service_code=item.service_code,
                status=item.status,
                audit_result=item.audit_result,
                audit_steps=item.audit_steps,
                latency_ms=item.latency_ms,
                error_code=item.error_code,
                error_message=item.error_message,
                error_details=item.error_details,
                description=item.description,
                created_at=item.created_at,
            )

    async def run_retention(self) -> RetentionRunInfo:
        """Prune rows outside both retention guarantees and persist task outcome."""
        run_id = uuid4()
        started_at = datetime.now(UTC)
        async with self._session_factory.begin() as session:
            session.add(LogRetentionRun(id=run_id, status="running", started_at=started_at))
        cutoff = started_at - timedelta(days=self.RETENTION_DAYS)
        protected_ids = (
            select(GatewayRequest.id)
            .order_by(GatewayRequest.created_at.desc(), GatewayRequest.id.desc())
            .limit(self.MINIMUM_RECORDS)
        )
        try:
            async with self._session_factory.begin() as session:
                result = await session.execute(
                    delete(GatewayRequest)
                    .where(
                        GatewayRequest.created_at < cutoff,
                        GatewayRequest.id.not_in(protected_ids),
                    )
                    .execution_options(synchronize_session=False)
                )
                deleted_count = cast(CursorResult[tuple[object, ...]], result).rowcount or 0
            async with self._session_factory.begin() as session:
                run = await session.get(LogRetentionRun, run_id)
                assert run is not None
                run.status = "succeeded"
                run.finished_at = datetime.now(UTC)
                run.deleted_count = deleted_count
                return self._retention_info(run)
        except Exception:
            async with self._session_factory.begin() as session:
                run = await session.get(LogRetentionRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.finished_at = datetime.now(UTC)
                    run.error_code = "retention_failed"
            raise

    async def latest_retention_run(self) -> RetentionRunInfo | None:
        """Return the latest retention job state for administrator diagnostics."""
        async with self._session_factory() as session:
            run = await session.scalar(
                select(LogRetentionRun)
                .order_by(LogRetentionRun.started_at.desc(), LogRetentionRun.id.desc())
                .limit(1)
            )
            return self._retention_info(run) if run is not None else None

    async def run_periodically(self, interval_seconds: int = 86_400) -> None:
        """Run retention once per day until application shutdown cancels the task."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.run_retention()
            except Exception:
                # The failed state is persisted by run_retention; the next cycle retries.
                continue

    async def _ensure_project_access(
        self, session: AsyncSession, user: User, project_id: UUID | None
    ) -> None:
        if project_id is None or user.role == "admin":
            return
        exists_query = select(Project.id).where(
            Project.id == project_id,
            self._visibility_filter(user),
        )
        if await session.scalar(exists_query) is None:
            raise RequestLogNotFoundError("项目不存在或无权查看")

    @staticmethod
    def _visibility_filter(user: User) -> ColumnElement[bool]:
        return or_(
            Project.visibility == "public",
            exists().where(
                ProjectMember.project_id == Project.id,
                ProjectMember.user_id == user.id,
            ),
        )

    @classmethod
    def _filters(
        cls,
        user: User,
        start_at: datetime,
        end_at: datetime,
        project_id: UUID | None,
        *,
        request_id: str | None = None,
    ) -> list[ColumnElement[bool]]:
        # 精确请求 ID/Trace ID 查询不应被页面遗留的日期范围挡住。
        filters: list[ColumnElement[bool]] = []
        if request_id is None:
            filters.extend(
                [
                    GatewayRequest.created_at >= start_at,
                    GatewayRequest.created_at < end_at,
                ]
            )
        if project_id is not None:
            filters.append(GatewayRequest.project_id == project_id)
        if user.role != "admin":
            filters.append(cls._visibility_filter(user))
        return filters

    @staticmethod
    def _validate_window(start_at: datetime, end_at: datetime) -> None:
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise RequestLogQueryError("时间范围必须包含时区")
        if start_at >= end_at:
            raise RequestLogQueryError("开始时间必须早于结束时间")
        if end_at - start_at > timedelta(days=366):
            raise RequestLogQueryError("单次查询时间范围不能超过 366 天")

    @staticmethod
    def _encode_cursor(created_at: datetime, record_id: UUID) -> str:
        payload = json.dumps(
            [created_at.isoformat(), str(record_id)],
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[datetime, UUID]:
        try:
            payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
            created_at = datetime.fromisoformat(payload[0])
            if created_at.tzinfo is None:
                raise ValueError
            return created_at, UUID(payload[1])
        except (
            ValueError,
            TypeError,
            IndexError,
            KeyError,
            json.JSONDecodeError,
            binascii.Error,
        ) as error:
            raise RequestLogQueryError("分页游标无效") from error

    @staticmethod
    def _retention_info(run: LogRetentionRun) -> RetentionRunInfo:
        return RetentionRunInfo(
            id=run.id,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            deleted_count=run.deleted_count,
            error_code=run.error_code,
        )
