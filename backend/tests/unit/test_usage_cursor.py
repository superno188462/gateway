"""调用日志游标的稳定性与输入校验。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.usage.application import RequestLogQueryError, RequestLogService


def test_request_cursor_round_trips_timestamp_and_database_id() -> None:
    created_at = datetime(2026, 9, 29, 12, 30, tzinfo=UTC)
    record_id = uuid4()

    cursor = RequestLogService._encode_cursor(created_at, record_id)

    assert RequestLogService._decode_cursor(cursor) == (created_at, record_id)


@pytest.mark.parametrize("cursor", ["%%%", "e30", "W10"])
def test_request_cursor_rejects_malformed_values(cursor: str) -> None:
    with pytest.raises(RequestLogQueryError, match="游标无效"):
        RequestLogService._decode_cursor(cursor)
