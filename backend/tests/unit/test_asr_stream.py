import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.api_keys import VerifiedApiKey
from app.services.asr.stream_api import router


class FakeUpstream:
    def __init__(self) -> None:
        self.responses: asyncio.Queue[tuple[list[dict[str, str]], bool, bool]] = asyncio.Queue()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeProvider:
    def __init__(self) -> None:
        self.upstream = FakeUpstream()
        self.final_before_end = False

    async def open_realtime(self, *_args: object, **_kwargs: object) -> FakeUpstream:
        return self.upstream

    async def send_realtime_audio(
        self, upstream: FakeUpstream, audio: bytes, _sequence: int, *, final: bool = False
    ) -> None:
        if final:
            await upstream.responses.put(
                ([{"type": "transcript.final", "text": "你好"}], True, True)
            )
        elif self.final_before_end and audio:
            await upstream.responses.put(
                ([{"type": "transcript.final", "text": "第一句"}], True, True)
            )
        elif audio:
            await upstream.responses.put(
                ([{"type": "transcript.partial", "text": "你"}], False, False)
            )

    async def receive_realtime(
        self, upstream: FakeUpstream
    ) -> tuple[list[dict[str, str]], bool, bool]:
        return await upstream.responses.get()


class FakeGateway:
    def __init__(self) -> None:
        self.provider = FakeProvider()
        self.session: SimpleNamespace | None = None
        self.reserved = 0
        self.used: int | None = None

    async def begin_stream(
        self, key: VerifiedApiKey, *, model: str, request_id: str
    ) -> SimpleNamespace:
        self.session = SimpleNamespace(
            key=key,
            request_id=request_id,
            model=model,
            started=0.0,
            period_start=None,
            routes=(
                SimpleNamespace(
                    provider=object(), supplier_name="测试供应商", connection_name="默认"
                ),
            ),
            audio_bytes=0,
            forwarded_audio_bytes=0,
            reserved_seconds=1,
            settled_seconds=0,
            finished=False,
        )
        self.reserved = 1
        return self.session

    async def reserve_stream_audio(self, session: SimpleNamespace, byte_count: int) -> None:
        session.audio_bytes += byte_count

    def mark_stream_audio_forwarded(self, session: SimpleNamespace, byte_count: int) -> None:
        session.forwarded_audio_bytes += byte_count

    async def settle_stream_progress(self, session: SimpleNamespace) -> int:
        import math

        target = max(1, math.ceil(session.forwarded_audio_bytes / 32_000))
        additional = max(0, target - session.settled_seconds)
        session.settled_seconds += additional
        self.used = session.settled_seconds
        return additional

    async def finish_stream(
        self,
        session: SimpleNamespace,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> int:
        if error_code and session.forwarded_audio_bytes:
            import math

            target = max(1, math.ceil(session.forwarded_audio_bytes / 32_000))
            self.used = max(self.used or 0, target)
        session.finished = True
        if error_code is None:
            self.used = max(self.used or 0, 1)
        return self.used


class FakeKeyService:
    async def verify(self, secret: str) -> VerifiedApiKey:
        assert secret == "project-key"
        return VerifiedApiKey(id=uuid4(), project_id=uuid4(), owner_id=uuid4(), name="test")


class FakeRecorder:
    async def record_auth_rejection(self, **_kwargs: Any) -> None:
        raise AssertionError("valid test key should not be rejected")


class FakeConfiguration:
    pass


def test_stream_route_forwards_pcm_and_partial_final_events() -> None:
    app = FastAPI()
    gateway = FakeGateway()
    app.state.container = SimpleNamespace(
        api_key_service=FakeKeyService(),
        asr_gateway_service=gateway,
        request_recorder=FakeRecorder(),
        asr_configuration_service=FakeConfiguration(),
    )
    app.include_router(router)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/audio/stream", headers={"Authorization": "Bearer project-key"}
        ) as websocket:
            websocket.send_json({"type": "start", "model": "doubao-seed-asr-2.0"})
            assert websocket.receive_json()["type"] == "session.started"
            websocket.send_bytes(b"\x00\x00" * 1600)
            assert websocket.receive_json()["type"] == "transcript.partial"
            websocket.send_json({"type": "end"})
            websocket.send_json({"type": "end"})
            assert websocket.receive_json()["type"] == "transcript.final"
            complete = websocket.receive_json()

    assert complete["type"] == "session.completed"
    assert complete["audio_seconds"] == 0.1
    assert complete["billed_seconds"] == 1
    assert gateway.used == 1
    assert gateway.provider.upstream.closed is True


def test_stream_route_keeps_session_open_after_utterance_final() -> None:
    app = FastAPI()
    gateway = FakeGateway()
    gateway.provider.final_before_end = True
    app.state.container = SimpleNamespace(
        api_key_service=FakeKeyService(),
        asr_gateway_service=gateway,
        request_recorder=FakeRecorder(),
        asr_configuration_service=FakeConfiguration(),
    )
    app.include_router(router)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/audio/stream", headers={"Authorization": "Bearer project-key"}
        ) as websocket:
            websocket.send_json({"type": "start", "model": "doubao-seed-asr-2.0"})
            assert websocket.receive_json()["type"] == "session.started"
            websocket.send_bytes(b"\x00\x00" * 1600)
            final = websocket.receive_json()
            assert final["type"] == "transcript.final"
            assert final["text"] == "第一句"
            assert gateway.used == 1

            # 收到句级 final 后继续保留同一条 WebSocket，等待后续音频和 end。
            websocket.send_bytes(b"\x01\x00" * 1600)
            assert websocket.receive_json()["type"] == "transcript.final"
            websocket.send_json({"type": "end"})
            assert websocket.receive_json()["type"] == "transcript.final"
            assert websocket.receive_json()["type"] == "session.completed"

    assert gateway.provider.upstream.closed is True


def test_stream_final_is_billed_before_client_closes_connection() -> None:
    app = FastAPI()
    gateway = FakeGateway()
    gateway.provider.final_before_end = True
    app.state.container = SimpleNamespace(
        api_key_service=FakeKeyService(),
        asr_gateway_service=gateway,
        request_recorder=FakeRecorder(),
        asr_configuration_service=FakeConfiguration(),
    )
    app.include_router(router)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/audio/stream", headers={"Authorization": "Bearer project-key"}
        ) as websocket:
            websocket.send_json({"type": "start", "model": "doubao-seed-asr-2.0"})
            assert websocket.receive_json()["type"] == "session.started"
            websocket.send_bytes(b"\x00\x00" * 1600)
            assert websocket.receive_json()["type"] == "transcript.final"
            assert gateway.used == 1

    assert gateway.session is not None and gateway.session.finished is True
    assert gateway.used == 1


def test_stream_disconnect_bills_audio_already_forwarded_without_final() -> None:
    app = FastAPI()
    gateway = FakeGateway()
    app.state.container = SimpleNamespace(
        api_key_service=FakeKeyService(),
        asr_gateway_service=gateway,
        request_recorder=FakeRecorder(),
        asr_configuration_service=FakeConfiguration(),
    )
    app.include_router(router)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/audio/stream", headers={"Authorization": "Bearer project-key"}
        ) as websocket:
            websocket.send_json({"type": "start", "model": "doubao-seed-asr-2.0"})
            assert websocket.receive_json()["type"] == "session.started"
            websocket.send_bytes(b"\x00\x00" * 1600)
            assert websocket.receive_json()["type"] == "transcript.partial"
            assert gateway.used is None

    assert gateway.session is not None and gateway.session.finished is True
    assert gateway.used == 1
