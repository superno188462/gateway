"""模板与记忆资源的业务分类和存储边界。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

DIRECTORIES: tuple[tuple[str, str, str], ...] = (
    ("memory", "sessions", "会话"),
    ("memory", "profiles", "用户画像"),
    ("memory", "longterm", "长期记忆"),
    ("template", "system", "System 模板"),
    ("template", "user", "User 模板"),
    ("template", "assistant", "Assistant 模板"),
)
VALID_DIRECTORIES = {(resource_type, category) for resource_type, category, _ in DIRECTORIES}


class ResourceNotFoundError(RuntimeError):
    """项目或有效资源不存在。"""


class ResourceForbiddenError(RuntimeError):
    """当前用户无权查看或修改项目资源。"""


class ResourceConflictError(RuntimeError):
    """资源名称重复或写入版本已过期。"""


class ResourceVersionConflictError(ResourceConflictError):
    """客户端提交的资源版本已过期。"""


class ResourceValidationError(RuntimeError):
    """资源分类、名称或内容不符合约束。"""


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    """稳定的项目资源视图；版本号用于拒绝陈旧写入。"""

    id: UUID
    project_id: UUID
    resource_type: str
    category: str
    name: str
    content: str
    version: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


class ResourceRepository(Protocol):
    """项目资源持久化 Port，所有操作都必须验证项目访问权限。"""

    async def list_for_project(
        self, project_id: UUID, user_id: UUID, is_admin: bool, category: str | None
    ) -> list[ResourceRecord]: ...

    async def get_for_project(
        self, project_id: UUID, resource_id: UUID, user_id: UUID, is_admin: bool
    ) -> ResourceRecord: ...

    async def create(
        self,
        project_id: UUID,
        user_id: UUID,
        resource_type: str,
        category: str,
        name: str,
        content: str,
    ) -> ResourceRecord: ...

    async def update(
        self,
        project_id: UUID,
        resource_id: UUID,
        user_id: UUID,
        is_admin: bool,
        expected_version: int,
        name: str,
        content: str,
    ) -> ResourceRecord: ...

    async def soft_delete(
        self,
        project_id: UUID,
        resource_id: UUID,
        user_id: UUID,
        is_admin: bool,
        expected_version: int,
    ) -> None: ...
