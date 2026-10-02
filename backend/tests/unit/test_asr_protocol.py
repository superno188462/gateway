import gzip
import json
import struct
import wave
from io import BytesIO

import pytest

from app.services.asr.application import AsrGatewayError, estimate_audio_seconds
from app.services.asr.protocol import (
    AsrProtocolError,
    AudioSegmentationError,
    RealtimeTranscriptTracker,
    decode_frame,
    encode_audio,
    encode_full_request,
    extract_transcript_events,
    parse_result,
    segment_mp3,
)


def test_encode_full_request_uses_volcengine_json_gzip_frame() -> None:
    frame = encode_full_request({"request": {"model_name": "bigmodel"}})

    assert frame[:4] == bytes((0x11, 0x10, 0x11, 0x00))
    size = struct.unpack_from(">I", frame, 4)[0]
    assert json.loads(gzip.decompress(frame[8 : 8 + size])) == {
        "request": {"model_name": "bigmodel"}
    }


def test_encode_plan_full_request_includes_positive_sequence() -> None:
    frame = encode_full_request({"request": {"model_name": "bigmodel"}}, sequence=1)

    assert frame[:4] == bytes((0x11, 0x11, 0x11, 0x00))
    assert struct.unpack_from(">i", frame, 4)[0] == 1
    size = struct.unpack_from(">I", frame, 8)[0]
    assert json.loads(gzip.decompress(frame[12 : 12 + size])) == {
        "request": {"model_name": "bigmodel"}
    }


def test_encode_audio_marks_single_recording_packet_as_final() -> None:
    frame = encode_audio(b"audio-data")

    assert frame[:4] == bytes((0x11, 0x22, 0x01, 0x00))
    size = struct.unpack_from(">I", frame, 4)[0]
    assert gzip.decompress(frame[8 : 8 + size]) == b"audio-data"


def test_encode_plan_audio_uses_negative_sequence_for_final_audio_chunk() -> None:
    frame = encode_audio(b"last-audio", final=True, sequence=7)

    assert frame[:4] == bytes((0x11, 0x23, 0x01, 0x00))
    assert struct.unpack_from(">i", frame, 4)[0] == -7
    size = struct.unpack_from(">I", frame, 8)[0]
    assert size > 0
    assert gzip.decompress(frame[12 : 12 + size]) == b"last-audio"


def test_encode_plan_audio_can_send_raw_pcm_without_compression() -> None:
    frame = encode_audio(b"pcm-data", final=False, sequence=2, compress=False)

    assert frame[:4] == bytes((0x11, 0x21, 0x00, 0x00))
    assert struct.unpack_from(">i", frame, 4)[0] == 2
    size = struct.unpack_from(">I", frame, 8)[0]
    assert frame[12 : 12 + size] == b"pcm-data"


def test_segment_mp3_splits_on_frame_boundaries_around_200ms() -> None:
    frame_header = bytes.fromhex("fffb9000")  # MPEG-1 Layer III, 128kbps, 44.1kHz
    frame = frame_header + bytes(417 - len(frame_header))
    content = b"ID3\x04\x00\x00\x00\x00\x00\x05title" + frame * 20

    chunks = segment_mp3(content)

    assert [len(chunk) // len(frame) for chunk in chunks] == [8, 8, 4]
    assert b"".join(chunks) == frame * 20


def test_segment_mp3_rejects_content_without_mpeg_frames() -> None:
    with pytest.raises(AudioSegmentationError, match="未找到有效的 MP3 音频帧"):
        segment_mp3(b"not an mp3")


def test_decode_full_server_response_with_sequence_and_gzip() -> None:
    payload = gzip.compress(
        json.dumps({"code": 0, "payload_msg": {"result": {"text": "你好"}}}).encode()
    )
    frame = bytes((0x11, 0x91, 0x11, 0x00)) + struct.pack(">iI", 3, len(payload)) + payload

    decoded = decode_frame(frame)
    assert decoded.sequence == 3
    assert parse_result(decoded.payload)["payload_msg"]["result"]["text"] == "你好"


def test_decode_rejects_truncated_frame() -> None:
    with pytest.raises(AsrProtocolError, match="长度不足"):
        decode_frame(b"\x11\x91\x11")


def test_realtime_result_becomes_partial_and_final_gateway_events() -> None:
    payload = {
        "payload_msg": {
            "result": {
                "utterances": [
                    {"text": "你好", "definite": False},
                    {"text": "世界", "definite": True},
                ]
            }
        }
    }

    assert extract_transcript_events(payload) == [
        {"type": "transcript.partial", "text": "你好"},
        {"type": "transcript.final", "text": "世界"},
    ]
    assert extract_transcript_events({"result": {"text": "你好"}}, final_frame=True) == [
        {"type": "transcript.final", "text": "你好"}
    ]


def test_realtime_tracker_deduplicates_cumulative_final_utterances() -> None:
    tracker = RealtimeTranscriptTracker("trace-test")
    first = {
        "payload_msg": {
            "result": {
                "text": "你好，很好。",
                "utterances": [
                    {"text": "你好，", "start_time": 100, "end_time": 700, "definite": True},
                    {"text": "很好。", "start_time": 900, "end_time": 1400, "definite": False},
                ],
            }
        }
    }
    second = {
        "payload_msg": {
            "result": {
                "text": "你好，很好。",
                "utterances": [
                    {"text": "你好，", "start_time": 100, "end_time": 700, "definite": True},
                    {"text": "很好。", "start_time": 900, "end_time": 1400, "definite": True},
                ],
            }
        }
    }

    first_events = tracker.extract(first)
    second_events = tracker.extract(second)
    repeated_events = tracker.extract(second)

    assert [event["text"] for event in first_events] == ["你好，", "很好。"]
    assert first_events[0]["type"] == "transcript.final"
    assert first_events[1]["type"] == "transcript.partial"
    assert second_events == [
        {
            "type": "transcript.final",
            "utterance_id": first_events[1]["utterance_id"],
            "text": "很好。",
            "start_ms": 900,
            "end_ms": 1400,
        }
    ]
    assert repeated_events == []


def test_realtime_tracker_distinguishes_later_repetition_of_same_text() -> None:
    tracker = RealtimeTranscriptTracker("trace-test")
    first = tracker.extract(
        {
            "result": {
                "utterances": [
                    {"text": "很好", "start_time": 100, "end_time": 500, "definite": True}
                ]
            }
        }
    )
    repeated_sentence = tracker.extract(
        {
            "result": {
                "utterances": [
                    {"text": "很好", "start_time": 1200, "end_time": 1600, "definite": True}
                ]
            }
        }
    )

    assert first[0]["type"] == repeated_sentence[0]["type"] == "transcript.final"
    assert first[0]["text"] == repeated_sentence[0]["text"] == "很好"
    assert first[0]["utterance_id"] != repeated_sentence[0]["utterance_id"]


def test_realtime_tracker_only_emits_cumulative_text_delta_as_final() -> None:
    tracker = RealtimeTranscriptTracker("trace-test")
    first = tracker.extract({"result": {"text": "你好。", "definite": True}})
    second = tracker.extract({"result": {"text": "你好。很好。", "definite": True}})
    duplicate = tracker.extract({"result": {"text": "你好。很好。", "definite": True}})

    assert first[0]["text"] == "你好。"
    assert second[0]["text"] == "很好。"
    assert first[0]["utterance_id"] != second[0]["utterance_id"]
    assert duplicate == []


def test_wav_duration_is_measured_in_seconds() -> None:
    audio = BytesIO()
    with wave.open(audio, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 16000)
    assert estimate_audio_seconds(audio.getvalue(), "recording.wav") == 1


def test_pcm_without_duration_metadata_is_rejected() -> None:
    with pytest.raises(AsrGatewayError, match="时长") as error:
        estimate_audio_seconds(b"raw pcm", "recording.pcm")
    assert error.value.code == "audio_duration_unavailable"


def test_m4a_duration_is_measured_from_movie_header() -> None:
    # A minimal MP4 mvhd atom with 1,000 ticks/second and a 2-second duration.
    atom = (
        (28).to_bytes(4, "big")
        + b"mvhd"
        + bytes(4)
        + bytes(8)
        + (1000).to_bytes(4, "big")
        + (2000).to_bytes(4, "big")
    )
    assert estimate_audio_seconds(atom, "recording.m4a") == 2


def test_m4a_duration_falls_back_to_media_header_when_movie_duration_is_unknown() -> None:
    movie_header = (
        (28).to_bytes(4, "big")
        + b"mvhd"
        + bytes(4)
        + bytes(8)
        + (1000).to_bytes(4, "big")
        + bytes(4)
    )
    media_header = (
        (28).to_bytes(4, "big")
        + b"mdhd"
        + bytes(4)
        + bytes(8)
        + (48000).to_bytes(4, "big")
        + (96000).to_bytes(4, "big")
    )

    assert estimate_audio_seconds(movie_header + media_header, "recording.m4a") == 2
