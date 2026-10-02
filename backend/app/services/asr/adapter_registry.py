"""ASR 路由前缀与供应商协议配置。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AsrAdapterDefinition:
    adapter: str


# 模型前缀由调用方传入。Resource ID 属于供应商协议参数，集中在适配器注册表，
# 不要求管理员或调用方维护模型名到 Resource ID 的映射。
ASR_ADAPTERS: dict[str, AsrAdapterDefinition] = {
    "volc": AsrAdapterDefinition(
        adapter="volc_websocket_v3",
    ),
}

GENERIC_ADAPTER = AsrAdapterDefinition(adapter="openai_transcriptions")
