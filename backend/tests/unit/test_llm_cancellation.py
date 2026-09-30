"""LLM cancellation cleanup regression tests."""

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest

from app.application.api_keys import VerifiedApiKey
from app.services.llm.application import LlmGatewayService
from app.services.llm.domain import ProviderStreamEvent


@pytest.mark.asyncio
async def test_cancelled_stream_cleanup_is_shielded_and_releases_reservation() -> None:
    """A cancelled request must still close upstream and persist quota release."""
    upstream_closed = False

    async def provider_stream():
        nonlocal upstream_closed
        try:
            yield ProviderStreamEvent(None)
        finally:
            upstream_closed = True

    stream = provider_stream()
    await anext(stream)
    service = LlmGatewayService(cast(object, None), cast(object, None), cast(object, None))
    release_usage = AsyncMock()
    service._release_usage = release_usage  # type: ignore[method-assign]
    api_key = VerifiedApiKey(uuid4(), uuid4(), uuid4(), "test")
    period_start = datetime(2026, 9, 1, tzinfo=UTC)
    started = 0.0

    with anyio.CancelScope() as cancellation_scope:
        cancellation_scope.cancel()
        await service._cleanup_cancelled_stream(
            stream,
            api_key,
            period_start,
            446,
            request_id="trace-cancel-test",
            started=started,
            partial_usage=None,
        )

    assert upstream_closed is True
    release_usage.assert_awaited_once()
    assert release_usage.await_args is not None
    assert release_usage.await_args.args == (api_key, period_start, 446)
    assert release_usage.await_args.kwargs["error_code"] == "request_cancelled"
