import gzip
import json
import struct
from unittest.mock import AsyncMock, patch

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from app.services.asr.protocol import RealtimeTranscriptTracker
from app.services.asr.volcengine import (
    AsrProviderError,
    VolcAsrConnection,
    VolcAsrProvider,
    VolcAsrRealtimeSession,
)


def server_response(payload: dict[str, object], *, flags: int = 0) -> bytes:
    compressed = gzip.compress(json.dumps(payload).encode("utf-8"))
    return bytes((0x11, 0x90 | flags, 0x11, 0x00)) + struct.pack(">I", len(compressed)) + compressed


@pytest.mark.asyncio
async def test_realtime_provider_deduplicates_cumulative_utterance_snapshots() -> None:
    first_snapshot = {
        "code": 0,
        "payload_msg": {
            "result": {
                "text": "很好，很好。",
                "utterances": [
                    {"text": "很好，", "start_time": 100, "end_time": 500, "definite": True},
                    {"text": "很好。", "start_time": 900, "end_time": 1400, "definite": False},
                ],
            }
        },
    }
    final_snapshot = {
        "code": 0,
        "payload_msg": {
            "result": {
                "text": "很好，很好。",
                "utterances": [
                    {"text": "很好，", "start_time": 100, "end_time": 500, "definite": True},
                    {"text": "很好。", "start_time": 900, "end_time": 1400, "definite": True},
                ],
            }
        },
    }

    class FakeWebSocket:
        def __init__(self) -> None:
            self.responses = [
                server_response(first_snapshot),
                server_response(final_snapshot),
                server_response(final_snapshot),
            ]

        async def recv(self) -> bytes:
            return self.responses.pop(0)

    session = VolcAsrRealtimeSession(
        FakeWebSocket(), "trace-test", RealtimeTranscriptTracker("trace-test")
    )
    provider = VolcAsrProvider()

    first_events, first_final, first_completed = await provider.receive_realtime(session)
    second_events, second_final, second_completed = await provider.receive_realtime(session)
    repeated_events, repeated_final, repeated_completed = await provider.receive_realtime(session)

    assert [event["type"] for event in first_events] == [
        "transcript.final",
        "transcript.partial",
    ]
    assert [event["text"] for event in first_events] == ["很好，", "很好。"]
    assert [event["type"] for event in second_events] == ["transcript.final"]
    assert second_events[0]["text"] == "很好。"
    assert second_events[0]["utterance_id"] == first_events[1]["utterance_id"]
    assert repeated_events == []
    assert first_final is second_final is True
    assert repeated_final is False
    assert first_completed is second_completed is repeated_completed is False


@pytest.mark.asyncio
async def test_websocket_401_is_reported_as_upstream_authentication_failure() -> None:
    response = Response(401, "Unauthorized", Headers(), b"")
    handshake_error = InvalidStatus(response)

    class FailedConnect:
        async def __aenter__(self) -> None:
            raise handshake_error

        async def __aexit__(self, *_args: object) -> None:
            return None

    with (
        patch.object(VolcAsrProvider, "_prepare_wav_chunks", new_callable=AsyncMock),
        patch("app.services.asr.volcengine.connect", return_value=FailedConnect()) as connect_mock,
    ):
        with pytest.raises(AsrProviderError) as error:
            await VolcAsrProvider().transcribe(
                VolcAsrConnection(
                    api_key="test-key",
                    resource_id="volc.seedasr.sauc.duration",
                    file_transcription_url="wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream",
                ),
                b"audio",
                "m4a",
            )

    assert error.value.code == "upstream_authentication_failed"
    assert "API Key" in str(error.value)
    headers = connect_mock.call_args.kwargs["additional_headers"]
    assert headers["X-Api-Key"] == "test-key"
    assert headers["X-Api-Resource-Id"] == "volc.seedasr.sauc.duration"
    assert headers["X-Api-Sequence"] == "-1"


@pytest.mark.asyncio
async def test_normal_close_after_result_returns_latest_result_without_last_flag() -> None:
    close_error = ConnectionClosedOK(
        Close(1000, "finish last sequence"),
        Close(1000, "finish last sequence"),
        True,
    )

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.responses = [
                server_response({"code": 0}),
                server_response(
                    {
                        "code": 0,
                        "payload_msg": {
                            "audio_info": {"duration": 1000},
                            "result": {"text": "识别结果"},
                        }
                    }
                ),
            ]
            self.response_index = 0

        async def send(self, _data: bytes) -> None:
            self.sent.append(_data)

        async def recv(self) -> bytes:
            if self.response_index >= len(self.responses):
                raise close_error
            response = self.responses[self.response_index]
            self.response_index += 1
            return response

    class FakeConnect:
        def __init__(self) -> None:
            self.websocket = FakeWebSocket()

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, *_args: object) -> None:
            return None

    fake_connect = FakeConnect()
    with (
        patch.object(
            VolcAsrProvider,
            "_prepare_wav_chunks",
            new_callable=AsyncMock,
            return_value=[b"pcm-data"],
        ),
        patch("app.services.asr.volcengine.connect", return_value=fake_connect),
    ):
        result = await VolcAsrProvider().transcribe(
            VolcAsrConnection(
                api_key="test-key",
                resource_id="volc.seedasr.sauc.duration",
                file_transcription_url="wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream",
            ),
            b"audio",
            "wav",
        )

    assert result["payload_msg"]["result"]["text"] == "识别结果"
    request_payload = json.loads(gzip.decompress(fake_connect.websocket.sent[0][12:]))
    assert "enable_nonstream" not in request_payload["request"]


@pytest.mark.asyncio
async def test_mp3_is_decoded_to_raw_pcm_chunks_for_plan_asr() -> None:
    frame_header = bytes.fromhex("fffb9000")
    mp3_frame = frame_header + bytes(417 - len(frame_header))
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x05title" + mp3_frame * 20

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.responses = [
                server_response({"code": 0}),
                server_response(
                    {
                        "code": 0,
                        "result": {"text": "识别结果"},
                    },
                    flags=0b0010,
                ),
            ]

        async def send(self, data: bytes) -> None:
            self.sent.append(data)

        async def recv(self) -> bytes:
            return self.responses.pop(0)

    class FakeConnect:
        def __init__(self) -> None:
            self.websocket = FakeWebSocket()

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, *_args: object) -> None:
            return None

    fake_connect = FakeConnect()
    with (
        patch.object(
            VolcAsrProvider,
            "_prepare_wav_chunks",
            new_callable=AsyncMock,
            return_value=[b"pcm-one", b"pcm-two"],
        ),
        patch("app.services.asr.volcengine.connect", return_value=fake_connect),
    ):
        result = await VolcAsrProvider().transcribe(
            VolcAsrConnection(
                api_key="test-key",
                resource_id="volc.seedasr.sauc.duration",
                file_transcription_url="wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream",
            ),
            audio,
            "mp3",
        )

    assert result["payload_msg"]["result"]["text"] == "识别结果"
    request_payload = json.loads(gzip.decompress(fake_connect.websocket.sent[0][12:]))
    assert request_payload["audio"]["format"] == "wav"
    packets = fake_connect.websocket.sent[1:]
    assert len(packets) == 3
    assert [packet[1] & 0x0F for packet in packets] == [1, 1, 3]
    assert [struct.unpack_from(">i", packet, 4)[0] for packet in packets] == [2, 3, -4]
    assert [packet[2] & 0x0F for packet in packets] == [0, 0, 0]
    assert packets[0][12:] == b"pcm-one"
    assert packets[1][12:] == b"pcm-two"
    assert struct.unpack_from(">I", packets[2], 8)[0] == 0
    assert packets[2][12:] == b""
