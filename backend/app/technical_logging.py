"""管理员可查看的标准运行日志文件和请求 Trace ID 上下文。"""

import logging
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

TRACE_ID: ContextVar[str] = ContextVar("gateway_trace_id", default="-")
_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_LOG_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"\[(?P<level>[A-Z]+)] \[(?P<source>[^\]]+)] "
    r"\[TraceID: (?P<trace_id>[^\]]+)] - (?P<message>.*)$"
)


class TraceIdFilter(logging.Filter):
    """将当前 HTTP 请求的追踪 ID 注入标准日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = TRACE_ID.get()
        return True


class GatewayLogFormatter(logging.Formatter):
    """按时间、等级、模块、Trace ID 和事件输出单行日志。"""

    def format(self, record: logging.LogRecord) -> str:
        local_time = datetime.fromtimestamp(record.created).astimezone()
        timestamp = local_time.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(record.msecs):03d}"
        message = record.getMessage().replace("\r", "\\r").replace("\n", "\\n")
        if record.exc_info:
            exception = (
                self.formatException(record.exc_info).replace("\r", "\\r").replace("\n", "\\n")
            )
            message = f"{message} | exception={exception}"
        trace_id = getattr(record, "trace_id", TRACE_ID.get())
        return f"{timestamp} [{record.levelname}] [{record.name}] [TraceID: {trace_id}] - {message}"


@dataclass(frozen=True, slots=True)
class TechnicalLogEntry:
    """已解析且可筛选的单条应用技术日志。"""

    timestamp: str
    level: str
    source: str
    trace_id: str
    message: str


class TechnicalLogService:
    """只读查询本机轮转日志文件，供管理员控制台展示。"""

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path

    def list_entries(
        self,
        *,
        limit: int = 200,
        level: str | None = None,
        trace_id: str | None = None,
        query: str | None = None,
    ) -> list[TechnicalLogEntry]:
        """读取日志尾部并按等级、Trace ID、文本筛选，最新记录排在前面。"""
        if not self.file_path.exists():
            return []
        tail_lines = self._read_tail_lines(5000)
        entries: list[TechnicalLogEntry] = []
        normalized_level = level.upper() if level else None
        normalized_trace = trace_id.casefold() if trace_id else None
        normalized_query = query.casefold() if query else None
        for line in reversed(tail_lines):
            match = _LOG_LINE_PATTERN.match(line)
            if match is None:
                continue
            entry = TechnicalLogEntry(**match.groupdict())
            if normalized_level and entry.level != normalized_level:
                continue
            if normalized_trace and normalized_trace not in entry.trace_id.casefold():
                continue
            if normalized_query and normalized_query not in entry.message.casefold():
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        level: str | None = None,
        trace_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[TechnicalLogEntry], int]:
        """从日志尾部筛选记录并返回指定页；为避免扫描整份文件，最多检查最近 5,000 行。"""
        if page < 1:
            raise ValueError("页码必须大于或等于 1")
        if page_size < 1 or page_size > 100:
            raise ValueError("每页条数必须在 1 到 100 之间")
        if not self.file_path.exists():
            return [], 0

        normalized_level = level.upper() if level else None
        normalized_trace = trace_id.casefold() if trace_id else None
        normalized_query = query.casefold() if query else None
        matching: list[TechnicalLogEntry] = []
        for line in reversed(self._read_tail_lines(5000)):
            match = _LOG_LINE_PATTERN.match(line)
            if match is None:
                continue
            entry = TechnicalLogEntry(**match.groupdict())
            if normalized_level and entry.level != normalized_level:
                continue
            if normalized_trace and normalized_trace not in entry.trace_id.casefold():
                continue
            if normalized_query and normalized_query not in entry.message.casefold():
                continue
            matching.append(entry)

        total_count = len(matching)
        start = (page - 1) * page_size
        return matching[start : start + page_size], total_count

    def _read_tail_lines(self, max_lines: int) -> list[str]:
        """从文件末尾倒读有限行，避免生产日志增长后每次查询扫描全文件。"""
        chunks: list[bytes] = []
        newline_count = 0
        with self.file_path.open("rb") as stream:
            stream.seek(0, 2)
            remaining = stream.tell()
            while remaining > 0 and newline_count <= max_lines:
                chunk_size = min(8192, remaining)
                remaining -= chunk_size
                stream.seek(remaining)
                chunk = stream.read(chunk_size)
                newline_count += chunk.count(b"\n")
                chunks.append(chunk)
        lines = b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()
        return lines[-max_lines:]


def configure_file_logging(file_path: Path, level: str, backup_count: int = 30) -> None:
    """配置 UTF-8 日志文件并按天轮转；同一路径重复启动不会重复挂载 Handler。"""
    resolved_path = file_path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        if getattr(handler, "_gateway_log_path", None) == str(resolved_path):
            handler.setLevel(level)
            return
    handler = TimedRotatingFileHandler(
        resolved_path,
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
        utc=False,
    )
    handler.setLevel(level)
    handler.setFormatter(GatewayLogFormatter())
    handler.addFilter(TraceIdFilter())
    handler._gateway_log_path = str(resolved_path)  # type: ignore[attr-defined]
    root_logger.addHandler(handler)


def set_trace_id(trace_id: str) -> Token[str]:
    """将经过校验的追踪标识设为当前异步上下文 ID。"""
    value = trace_id if _TRACE_ID_PATTERN.fullmatch(trace_id) else "-"
    return TRACE_ID.set(value)


def reset_trace_id(token: Token[str]) -> None:
    """恢复请求前的 Trace ID 上下文。"""
    TRACE_ID.reset(token)
