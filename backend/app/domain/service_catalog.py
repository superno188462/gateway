"""服务管理与具体服务模块共享的目录契约。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServiceCatalogItem:
    """可供项目申请的服务及其当前可用模型。"""

    code: str
    name: str
    models: tuple[str, ...]
