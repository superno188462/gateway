"""项目模板与记忆资源用例。"""

from uuid import UUID

from app.resources.domain import (
    DIRECTORIES,
    VALID_DIRECTORIES,
    ResourceRecord,
    ResourceRepository,
    ResourceValidationError,
)

MAX_CONTENT_LENGTH = 100_000


class ResourceService:
    """验证稳定资源规则并调用注入的存储 Port。"""

    def __init__(self, repository: ResourceRepository) -> None:
        self._repository = repository

    @staticmethod
    def directories() -> list[dict[str, str]]:
        """返回固定目录定义；调用方不能通过 API 增删目录。"""
        return [
            {"resource_type": resource_type, "category": category, "label": label}
            for resource_type, category, label in DIRECTORIES
        ]

    async def list_for_project(
        self,
        project_id: UUID,
        user_id: UUID,
        is_admin: bool,
        resource_type: str | None,
        category: str | None,
    ) -> list[ResourceRecord]:
        """列出一个项目当前有效的资源，可按固定目录筛选。"""
        if (resource_type is None) != (category is None):
            raise ResourceValidationError("resource_type 和 category 必须同时提供")
        if resource_type is not None and (resource_type, category) not in VALID_DIRECTORIES:
            raise ResourceValidationError("资源目录无效")
        return await self._repository.list_for_project(project_id, user_id, is_admin, category)

    async def create(
        self,
        project_id: UUID,
        user_id: UUID,
        resource_type: str,
        category: str,
        name: str,
        content: str,
    ) -> ResourceRecord:
        """在固定目录中新建资源，当前有效名称在目录内唯一。"""
        self._validate_directory(resource_type, category)
        normalized_name = self._validate_name(name)
        self._validate_content(content)
        return await self._repository.create(
            project_id, user_id, resource_type, category, normalized_name, content
        )

    async def get(
        self, project_id: UUID, resource_id: UUID, user_id: UUID, is_admin: bool
    ) -> ResourceRecord:
        """按项目与资源 ID 获取有效文件；跨项目 ID 不可探测。"""
        return await self._repository.get_for_project(project_id, resource_id, user_id, is_admin)

    async def update(
        self,
        project_id: UUID,
        resource_id: UUID,
        user_id: UUID,
        is_admin: bool,
        expected_version: int,
        name: str,
        content: str,
    ) -> ResourceRecord:
        """校验版本后更新名称或正文；冲突时保留服务端原值。"""
        normalized_name = self._validate_name(name)
        self._validate_content(content)
        if expected_version < 1:
            raise ResourceValidationError("expected_version 必须大于 0")
        return await self._repository.update(
            project_id,
            resource_id,
            user_id,
            is_admin,
            expected_version,
            normalized_name,
            content,
        )

    async def soft_delete(
        self,
        project_id: UUID,
        resource_id: UUID,
        user_id: UUID,
        is_admin: bool,
        expected_version: int,
    ) -> None:
        """按版本软删除资源，删除项不出现在默认目录列表。"""
        if expected_version < 1:
            raise ResourceValidationError("expected_version 必须大于 0")
        await self._repository.soft_delete(
            project_id, resource_id, user_id, is_admin, expected_version
        )

    @staticmethod
    def _validate_directory(resource_type: str, category: str) -> None:
        if (resource_type, category) not in VALID_DIRECTORIES:
            raise ResourceValidationError("资源目录无效")

    @staticmethod
    def _validate_name(name: str) -> str:
        value = name.strip()
        if not value or len(value) > 200:
            raise ResourceValidationError("名称长度必须在 1 到 200 个字符之间")
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ResourceValidationError("名称不能是路径或包含路径分隔符")
        return value

    @staticmethod
    def _validate_content(content: str) -> None:
        if len(content) > MAX_CONTENT_LENGTH:
            raise ResourceValidationError(f"资源内容不能超过 {MAX_CONTENT_LENGTH} 个字符")
