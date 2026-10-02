"""最小实时 ASR 客户端：发送裸 16 kHz mono s16le PCM 文件。"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from websockets.asyncio.client import connect


async def run(url: str, model: str, api_key: str, file_path: Path) -> None:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with connect(url, additional_headers=headers, max_size=8 * 1024 * 1024) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "start",
                    "model": model,
                    "audio": {
                        "format": "pcm",
                        "sample_rate": 16000,
                        "channels": 1,
                        "bits": 16,
                        "endianness": "little",
                    },
                }
            )
        )

        async def receive_events() -> None:
            async for message in ws:
                if not isinstance(message, str):
                    continue
                event = json.loads(message)
                print(json.dumps(event, ensure_ascii=False))
                if event.get("type") in {"error", "session.completed"}:
                    return

        receiver = asyncio.create_task(receive_events())
        try:
            with file_path.open("rb") as audio:
                while chunk := audio.read(3200):  # 100 ms，32000 bytes/s
                    await ws.send(chunk)
                    await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "end"}))
            await receiver
        finally:
            if not receiver.done():
                receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="网关 WebSocket 地址")
    parser.add_argument("--model", required=True, help="网关公开 ASR 模型名")
    parser.add_argument("--file", required=True, type=Path, help="裸 s16le PCM 文件")
    args = parser.parse_args()
    api_key = os.environ.get("GATEWAY_API_KEY")
    if not api_key:
        parser.error("请先设置 GATEWAY_API_KEY 环境变量")
    asyncio.run(run(args.url, args.model, api_key, args.file))


if __name__ == "__main__":
    main()
