"""标准技术日志格式和日志文件查询。"""

import logging
from pathlib import Path

from app.technical_logging import (
    GatewayLogFormatter,
    TechnicalLogService,
    configure_file_logging,
    reset_trace_id,
    set_trace_id,
)


def test_formatter_emits_standard_timestamp_level_source_trace_and_message() -> None:
    record = logging.LogRecord(
        "gateway.auth",
        logging.ERROR,
        "auth.py",
        12,
        "login_failed code=%s",
        ("invalid_password",),
        None,
    )
    record.created = 1790676000.123
    record.msecs = 123
    record.trace_id = "trace-test-123"

    line = GatewayLogFormatter().format(record)

    assert "[ERROR] [gateway.auth] [TraceID: trace-test-123]" in line
    assert line.endswith("login_failed code=invalid_password")
    assert line[4:5] == "-"  # timestamp is rendered to millisecond precision


def test_technical_log_service_filters_recent_lines(tmp_path: Path) -> None:
    path = tmp_path / "gateway.log"
    path.write_text(
        "2026-09-29 10:00:00.123 [INFO] [gateway.http] [TraceID: trace-a] - request ok\n"
        "2026-09-29 10:00:01.456 [ERROR] [gateway.llm] [TraceID: trace-b] - upstream_http_404\n",
        encoding="utf-8",
    )

    entries = TechnicalLogService(path).list_entries(level="ERROR", trace_id="trace-b")

    assert len(entries) == 1
    assert entries[0].source == "gateway.llm"
    assert entries[0].message == "upstream_http_404"


def test_technical_log_service_returns_requested_page(tmp_path: Path) -> None:
    path = tmp_path / "gateway.log"
    path.write_text(
        "".join(
            f"2026-09-29 10:00:0{index}.123 [INFO] [gateway.test] "
            f"[TraceID: trace-{index}] - event_{index}\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )

    entries, total_count = TechnicalLogService(path).list_page(page=2, page_size=2)

    assert total_count == 5
    assert [entry.message for entry in entries] == ["event_2", "event_1"]


def test_file_logging_writes_trace_id_and_can_be_read_back(tmp_path: Path) -> None:
    path = tmp_path / "gateway.log"
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    configure_file_logging(path, "INFO")
    handler = next(
        item
        for item in root_logger.handlers
        if getattr(item, "_gateway_log_path", None) == str(path.resolve())
    )
    context_token = set_trace_id("trace-file-test")
    try:
        logging.getLogger("gateway.test").info("test_event code=ok")
        handler.flush()
        entries = TechnicalLogService(path).list_entries()
    finally:
        reset_trace_id(context_token)
        root_logger.removeHandler(handler)
        handler.close()
        root_logger.setLevel(previous_level)

    assert entries[0].trace_id == "trace-file-test"
    assert entries[0].message == "test_event code=ok"
