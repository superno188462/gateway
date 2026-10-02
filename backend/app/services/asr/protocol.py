"""豆包流式 ASR WebSocket V3 二进制帧编解码。"""

import gzip
import json
import struct
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, uuid5


class AsrProtocolError(RuntimeError):
    """上游返回了无法解析的 ASR 二进制帧。"""


class AudioSegmentationError(ValueError):
    """上传的 MP3 无法按 MPEG 音频帧边界拆包。"""


@dataclass(frozen=True, slots=True)
class AsrFrame:
    message_type: int
    flags: int
    serialization: int
    compression: int
    sequence: int | None
    error_code: int | None
    payload: bytes


def encode_full_request(payload: dict[str, Any], *, sequence: int | None = None) -> bytes:
    """编码 JSON+gzip 的 full client request，可选携带 Plan 协议序号。"""
    compressed = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    flags = 0b0001 if sequence is not None else 0
    frame = bytes((0x11, (0x01 << 4) | flags, 0x11, 0x00))
    if sequence is not None:
        frame += struct.pack(">i", sequence)
    return frame + struct.pack(">I", len(compressed)) + compressed


def encode_audio(
    audio: bytes,
    *,
    final: bool = True,
    sequence: int | None = None,
    compress: bool = True,
) -> bytes:
    """编码音频请求；Plan 模式按序号递增，末包序号使用负数。"""
    compressed = gzip.compress(audio) if compress and audio else audio
    if sequence is None:
        flags = 0b0010 if final else 0
    else:
        flags = 0b0011 if final else 0b0001
    compression = 0x01 if compress else 0x00
    header = bytes((0x11, (0x02 << 4) | flags, compression, 0x00))
    if sequence is not None:
        frame_sequence = -sequence if final else sequence
        header += struct.pack(">i", frame_sequence)
    return header + struct.pack(">I", len(compressed)) + compressed


def segment_mp3(audio: bytes, *, target_duration_ms: int = 200) -> list[bytes]:
    """按 MP3 帧边界切成约 200ms 的包，丢弃文件级 ID3 标签和尾部标签。"""
    if target_duration_ms <= 0:
        raise ValueError("target_duration_ms must be positive")

    offset = _skip_id3v2(audio)
    frames: list[tuple[bytes, float]] = []
    while offset + 4 <= len(audio):
        header = int.from_bytes(audio[offset : offset + 4], "big")
        frame_size, duration_ms = _mp3_frame_info(header)
        if frame_size is None or offset + frame_size > len(audio):
            # MP3 常在末尾带 ID3v1/APEv2 标签；遇到非音频尾部时停止扫描。
            if not frames:
                raise AudioSegmentationError("文件中未找到有效的 MP3 音频帧")
            break
        frame = audio[offset : offset + frame_size]
        frames.append((frame, duration_ms))
        offset += frame_size

    if not frames:
        raise AudioSegmentationError("文件中未找到有效的 MP3 音频帧")

    chunks: list[bytes] = []
    pending = bytearray()
    pending_duration = 0.0
    for frame, duration_ms in frames:
        pending.extend(frame)
        pending_duration += duration_ms
        if pending_duration >= target_duration_ms:
            chunks.append(bytes(pending))
            pending.clear()
            pending_duration = 0.0
    if pending:
        chunks.append(bytes(pending))
    return chunks


def _skip_id3v2(audio: bytes) -> int:
    if len(audio) < 10 or audio[:3] != b"ID3":
        return 0
    size_bytes = audio[6:10]
    if any(value & 0x80 for value in size_bytes):
        raise AudioSegmentationError("MP3 文件的 ID3v2 标签长度无效")
    tag_size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
    footer_size = 10 if audio[5] & 0x10 else 0
    offset = 10 + tag_size + footer_size
    if offset > len(audio):
        raise AudioSegmentationError("MP3 文件的 ID3v2 标签不完整")
    return offset


def _mp3_frame_info(header: int) -> tuple[int | None, float]:
    if header >> 21 != 0x7FF:
        return None, 0.0
    version = (header >> 19) & 0b11
    layer = (header >> 17) & 0b11
    bitrate_index = (header >> 12) & 0b1111
    sample_rate_index = (header >> 10) & 0b11
    padding = (header >> 9) & 1
    if version == 0b01 or layer == 0 or bitrate_index in {0, 15} or sample_rate_index == 3:
        return None, 0.0

    mpeg1_bitrates = {
        3: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
        2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
        1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    }
    mpeg2_bitrates = {
        3: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
        2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
        1: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    }
    sample_rates = {
        3: (44100, 48000, 32000),
        2: (22050, 24000, 16000),
        0: (11025, 12000, 8000),
    }
    bitrate_kbps = (mpeg1_bitrates if version == 3 else mpeg2_bitrates)[layer][bitrate_index]
    sample_rate = sample_rates[version][sample_rate_index]
    samples_per_frame = 384 if layer == 3 else (1152 if layer == 2 or version == 3 else 576)
    bitrate = bitrate_kbps * 1000
    if layer == 3:
        frame_size = ((12 * bitrate) // sample_rate + padding) * 4
    else:
        coefficient = 144 if layer == 2 or version == 3 else 72
        frame_size = (coefficient * bitrate) // sample_rate + padding
    return frame_size, samples_per_frame * 1000 / sample_rate


def decode_frame(data: bytes) -> AsrFrame:
    """解析服务端 full response 或 error response。"""
    if len(data) < 8:
        raise AsrProtocolError("ASR 上游响应帧长度不足")
    header_size = (data[0] & 0x0F) * 4
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    cursor = header_size
    sequence: int | None = None
    error_code: int | None = None
    if message_type == 0x0F:
        if len(data) < cursor + 8:
            raise AsrProtocolError("ASR 上游错误响应帧长度不足")
        error_code = struct.unpack_from(">I", data, cursor)[0]
        cursor += 4
    elif flags in {0x01, 0x03}:
        if len(data) < cursor + 4:
            raise AsrProtocolError("ASR 上游响应缺少序号")
        sequence = struct.unpack_from(">i", data, cursor)[0]
        cursor += 4
    if len(data) < cursor + 4:
        raise AsrProtocolError("ASR 上游响应缺少负载长度")
    payload_size = struct.unpack_from(">I", data, cursor)[0]
    cursor += 4
    payload = data[cursor : cursor + payload_size]
    if len(payload) != payload_size:
        raise AsrProtocolError("ASR 上游响应负载长度不匹配")
    if compression == 0x01:
        payload = gzip.decompress(payload)
    elif compression != 0x00:
        raise AsrProtocolError("ASR 上游使用了不支持的压缩方式")
    if message_type == 0x0F:
        message = payload.decode("utf-8", errors="replace")
        raise AsrProtocolError(f"ASR 上游错误 {error_code}: {message[:300]}")
    if message_type not in {0x09, 0x0B}:
        raise AsrProtocolError(f"ASR 上游返回未知消息类型 {message_type}")
    return AsrFrame(message_type, flags, serialization, compression, sequence, error_code, payload)


def parse_result(payload: bytes) -> dict[str, Any]:
    """反序列化标准 ASR full server response。"""
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AsrProtocolError("ASR 上游返回的 JSON 无效") from error
    if not isinstance(result, dict):
        raise AsrProtocolError("ASR 上游返回的数据结构无效")
    code = result.get("code")
    if isinstance(code, int) and code != 0 and code != 20000000:
        raise AsrProtocolError(f"ASR 上游处理失败，错误码 {code}")
    return result


def extract_transcript_events(
    result: dict[str, Any], *, final_frame: bool = False
) -> list[dict[str, str]]:
    """把豆包 ASR 结果转换为网关 transcript.partial/final 事件。"""
    payload = result.get("payload_msg", result.get("payload", result))
    if not isinstance(payload, dict):
        return []
    content = payload.get("result", result.get("result"))
    if not isinstance(content, dict):
        return []
    utterances = content.get("utterances")
    events: list[dict[str, str]] = []
    if isinstance(utterances, list):
        for utterance in utterances:
            if not isinstance(utterance, dict):
                continue
            text = utterance.get("text")
            if isinstance(text, str) and text:
                definite = bool(utterance.get("definite")) or final_frame
                events.append(
                    {"type": "transcript.final" if definite else "transcript.partial", "text": text}
                )
    if not events:
        text = content.get("text")
        if isinstance(text, str) and text:
            definite = bool(content.get("definite") or payload.get("is_final")) or final_frame
            events.append(
                {"type": "transcript.final" if definite else "transcript.partial", "text": text}
            )
    return events


@dataclass(slots=True)
class RealtimeTranscriptTracker:
    """将火山可能重复返回的 utterance 快照压成按句唯一的网关事件。"""

    request_id: str
    _finalized: set[str] = field(default_factory=set)
    _partial_text: dict[str, str] = field(default_factory=dict)
    _fallback_confirmed_text: str = ""
    _fallback_generation: int = 0

    def extract(
        self, result: dict[str, Any], *, final_frame: bool = False
    ) -> list[dict[str, Any]]:
        payload = result.get("payload_msg", result.get("payload", result))
        if not isinstance(payload, dict):
            return []
        content = payload.get("result", result.get("result"))
        if not isinstance(content, dict):
            return []

        utterances = content.get("utterances")
        if isinstance(utterances, list):
            return self._extract_utterances(utterances, final_frame=final_frame)
        return self._extract_cumulative_text(content, payload, final_frame=final_frame)

    def _extract_utterances(
        self, utterances: list[Any], *, final_frame: bool
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for index, utterance in enumerate(utterances):
            if not isinstance(utterance, dict):
                continue
            text = utterance.get("text")
            if not isinstance(text, str) or not text:
                continue

            start = _optional_milliseconds(utterance.get("start_time"))
            end = _optional_milliseconds(utterance.get("end_time"))
            additions = utterance.get("additions")
            speaker = additions.get("speaker_id") if isinstance(additions, dict) else None
            # 火山在 show_utterances=true 时提供语句起始时间。起始时间在 partial
            # 到 definite 期间保持稳定，也能区分用户后来再次说出的同一句文本。
            identity = (
                f"start:{start}:speaker:{speaker or ''}"
                if start is not None
                else f"index:{index}"
            )
            utterance_id = str(uuid5(NAMESPACE_URL, f"gateway-asr:{self.request_id}:{identity}"))
            definite = bool(utterance.get("definite")) or final_frame
            if definite:
                if utterance_id in self._finalized:
                    continue
                self._finalized.add(utterance_id)
                self._partial_text.pop(utterance_id, None)
                event: dict[str, Any] = {
                    "type": "transcript.final",
                    "utterance_id": utterance_id,
                    "text": text,
                }
                if start is not None:
                    event["start_ms"] = start
                if end is not None:
                    event["end_ms"] = end
                events.append(event)
                continue

            if self._partial_text.get(utterance_id) == text:
                continue
            self._partial_text[utterance_id] = text
            event = {
                "type": "transcript.partial",
                "utterance_id": utterance_id,
                "text": text,
            }
            if start is not None:
                event["start_ms"] = start
            if end is not None:
                event["end_ms"] = end
            events.append(event)
        return events

    def _extract_cumulative_text(
        self,
        content: dict[str, Any],
        payload: dict[str, Any],
        *,
        final_frame: bool,
    ) -> list[dict[str, Any]]:
        """兼容缺少 utterances 的响应，并剥离上游 result.text 的已确认前缀。"""
        text = content.get("text")
        if not isinstance(text, str) or not text:
            return []
        if self._fallback_confirmed_text and not text.startswith(self._fallback_confirmed_text):
            # 无分句时间戳时无法安全判断被改写的整段累计文本属于哪句话；宁可不
            # 生成可能重复触发 Agent 的 final，也不把整段历史冒充新语句。
            return []
        delta = text[len(self._fallback_confirmed_text) :]
        if not delta:
            return []

        identity = f"fallback:{self._fallback_generation}"
        utterance_id = str(uuid5(NAMESPACE_URL, f"gateway-asr:{self.request_id}:{identity}"))
        definite = bool(content.get("definite") or payload.get("is_final")) or final_frame
        if definite:
            self._fallback_confirmed_text = text
            self._fallback_generation += 1
            self._finalized.add(utterance_id)
            return [{"type": "transcript.final", "utterance_id": utterance_id, "text": delta}]
        if self._partial_text.get(utterance_id) == delta:
            return []
        self._partial_text[utterance_id] = delta
        return [{"type": "transcript.partial", "utterance_id": utterance_id, "text": delta}]


def _optional_milliseconds(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
