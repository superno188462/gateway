"""服务管理与具体服务模块共享的目录契约。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServiceCatalogItem:
    """可供项目申请的服务及其计量方式；None 表示不属于 AI 推理额度。"""

    code: str
    name: str
    models: tuple[str, ...]
    quota_unit: str | None = "tokens"
