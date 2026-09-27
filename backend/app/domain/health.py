"""健康检查领域接口。"""

from typing import Protocol


class ReadinessProbe(Protocol):
    """验证必需外部依赖是否可用。"""

    async def check(self) -> None:
        """依赖可用时正常返回，否则抛出异常。"""
        ...
