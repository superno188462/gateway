"""LLM 上游供应商连接与优先级管理用例。"""

import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import LlmProviderConfig
from app.services.llm.secrets import ProviderSecretCipher


class LlmConfigurationNotFoundError(RuntimeError):
    """供应商或模型配置不存在。"""


class LlmConfigurationConflictError(RuntimeError):
    """供应商名称或公开模型代码已被使用。"""


class LlmConfigurationValidationError(RuntimeError):
    """供应商或模型配置字段不合法。"""


@dataclass(frozen=True, slots=True)
class LlmProviderInfo:
    """供应商连接信息；只含 API Key 是否已配置，不含密文或明文。"""

    id: UUID
    name: str
    route_prefix: str | None
    base_url: str
    status: str
    priority: int
    api_key_configured: bool
    created_at: datetime
    updated_at: datetime
    last_tested_at: datetime | None
    last_test_success: bool | None
    last_test_message: str | None


@dataclass(frozen=True, slots=True)
class ResolvedLlmModel:
    """网关内部使用的供应商连接资料。"""

    model_code: str
    upstream_model: str
    base_url: str
    api_key: str


@dataclass(frozen=True, slots=True)
class ProviderTestResult:
    """不包含凭据或上游原始响应的连通性测试结果。"""

    success: bool
    message: str
    tested_at: datetime


@dataclass(frozen=True, slots=True)
class LlmProviderRouteGroup:
    """对调用方公开的路由组摘要，不包含供应商连接详情。"""

    prefix: str | None
    connection_count: int
    provider_names: tuple[str, ...]


class LlmConfigurationService:
    """维护 OpenAI 兼容供应商连接及其故障切换优先级。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: ProviderSecretCipher | None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def list_providers(self) -> list[LlmProviderInfo]:
        """按优先级列出供应商连接；绝不返回供应商 API Key。"""
        async with self._session_factory() as session:
            providers = await session.scalars(
                select(LlmProviderConfig).order_by(
                    LlmProviderConfig.priority, LlmProviderConfig.created_at, LlmProviderConfig.id
                )
            )
            return [self._provider_info(provider) for provider in providers]

    async def list_public_route_groups(self) -> tuple[LlmProviderRouteGroup, ...]:
        """返回启用路由组、供应商显示名及连接数，不暴露 URL、优先级或凭据。"""
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    LlmProviderConfig.route_prefix,
                    LlmProviderConfig.name,
                    func.count(LlmProviderConfig.id),
                )
                .where(LlmProviderConfig.status == "active")
                .group_by(LlmProviderConfig.route_prefix, LlmProviderConfig.name)
                .order_by(LlmProviderConfig.route_prefix, LlmProviderConfig.name)
            )
            grouped_rows: dict[str | None, list[tuple[str, int]]] = {}
            for prefix, name, count in rows.all():
                grouped_rows.setdefault(prefix, []).append((name, count))
            return tuple(
                LlmProviderRouteGroup(
                    prefix=prefix,
                    connection_count=sum(count for _, count in rows_for_group),
                    provider_names=tuple(name for name, _ in rows_for_group),
                )
                for prefix, rows_for_group in grouped_rows.items()
            )

    async def create_provider(
        self, name: str, base_url: str, api_key: str, route_prefix: str | None = None
    ) -> LlmProviderInfo:
        """创建供应商连接，并加密保存 API Key。"""
        name = name.strip()
        base_url = self._normalize_base_url(base_url)
        api_key = api_key.strip()
        route_prefix = self._normalize_route_prefix(route_prefix)
        if not name or len(name) > 100 or not api_key:
            raise LlmConfigurationValidationError("供应商名称和 API Key 不能为空")
        cipher = self._require_cipher()
        provider = LlmProviderConfig(
            name=name,
            route_prefix=route_prefix,
            base_url=base_url,
            encrypted_api_key=cipher.encrypt(api_key),
            status="active",
        )
        try:
            async with self._session_factory.begin() as session:
                session.add(provider)
                await session.flush()
                await session.refresh(provider)
                return self._provider_info(provider)
        except IntegrityError as error:
            raise LlmConfigurationConflictError("供应商名称已存在") from error

    async def update_provider(
        self,
        provider_id: UUID,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        route_prefix: str | None = None,
    ) -> LlmProviderInfo:
        """更新供应商配置；API Key 留空表示保留现有密钥。"""
        try:
            async with self._session_factory.begin() as session:
                provider = await session.get(LlmProviderConfig, provider_id)
                if provider is None:
                    raise LlmConfigurationNotFoundError("供应商不存在")
                if name is not None:
                    cleaned_name = name.strip()
                    if not cleaned_name or len(cleaned_name) > 100:
                        raise LlmConfigurationValidationError(
                            "供应商名称长度必须为 1 到 100 个字符"
                        )
                    provider.name = cleaned_name
                if route_prefix is not None:
                    provider.route_prefix = self._normalize_route_prefix(route_prefix)
                if base_url is not None:
                    provider.base_url = self._normalize_base_url(base_url)
                if api_key is not None and api_key.strip():
                    provider.encrypted_api_key = self._require_cipher().encrypt(api_key.strip())
                if status is not None:
                    provider.status = status
                if priority is not None:
                    if priority < 0 or priority > 1_000_000:
                        raise LlmConfigurationValidationError("优先级必须在 0 到 1,000,000 之间")
                    provider.priority = priority
                await session.flush()
                await session.refresh(provider)
                return self._provider_info(provider)
        except IntegrityError as error:
            raise LlmConfigurationConflictError("供应商名称已存在") from error

    async def delete_provider(self, provider_id: UUID) -> None:
        """删除供应商连接；历史映射数据由外键级联清理。"""
        async with self._session_factory.begin() as session:
            provider = await session.get(LlmProviderConfig, provider_id)
            if provider is None:
                raise LlmConfigurationNotFoundError("供应商不存在")
            await session.delete(provider)

    async def resolve_model_pool(self, model_code: str) -> tuple[ResolvedLlmModel, ...]:
        """按前缀分组并按优先级返回连接；只移除已配置的供应商前缀。"""
        requested_prefix: str | None = None
        upstream_model = model_code
        if "/" in model_code:
            requested_prefix, separator, upstream_model = model_code.partition("/")
            if not separator or not requested_prefix or not upstream_model:
                return ()
            requested_prefix = requested_prefix.lower()
        async with self._session_factory() as session:
            statement = select(LlmProviderConfig).where(LlmProviderConfig.status == "active")
            if requested_prefix is not None:
                statement = statement.where(LlmProviderConfig.route_prefix == requested_prefix)
            providers = await session.scalars(
                statement.order_by(
                    LlmProviderConfig.priority,
                    LlmProviderConfig.created_at,
                    LlmProviderConfig.id,
                )
            )
            return tuple(
                self._resolved_provider(provider, model_code, upstream_model)
                for provider in providers
            )

    async def test_provider(
        self, provider_id: UUID, model_name: str, http_client: httpx.AsyncClient
    ) -> ProviderTestResult:
        """使用管理员提供的模型名发送最小请求测试连接。"""
        model_name = model_name.strip()
        if not model_name or len(model_name) > 200:
            raise LlmConfigurationValidationError("测试模型名不能为空且长度不能超过 200 个字符")
        async with self._session_factory() as session:
            provider = await session.get(LlmProviderConfig, provider_id)
            if provider is None:
                raise LlmConfigurationNotFoundError("供应商不存在")
            api_key = self._require_cipher().decrypt(provider.encrypted_api_key)
        tested_at = datetime.now(UTC)
        try:
            response = await http_client.post(
                f"{provider.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
            )
            success = response.is_success
            message = "连接成功" if success else f"供应商返回 HTTP {response.status_code}"
        except httpx.HTTPError:
            success = False
            message = "连接失败，请检查 URL、网络和供应商状态"
        async with self._session_factory.begin() as session:
            provider = await session.get(LlmProviderConfig, provider_id)
            if provider is None:
                raise LlmConfigurationNotFoundError("供应商不存在")
            provider.last_tested_at = tested_at
            provider.last_test_success = success
            provider.last_test_message = message
        return ProviderTestResult(success=success, message=message, tested_at=tested_at)

    async def has_active_providers(self) -> bool:
        """判断是否至少存在一个启用的上游连接。"""
        async with self._session_factory() as session:
            has_provider = await session.scalar(
                select(LlmProviderConfig.id).where(LlmProviderConfig.status == "active").limit(1)
            )
            return has_provider is not None

    async def supports_model(self, model_code: str) -> bool:
        """Check the default pool or a configured prefix group without contacting vendors."""
        if "/" not in model_code:
            return await self.has_active_providers()
        prefix, separator, model_name = model_code.partition("/")
        if not separator or not prefix or not model_name:
            return False
        prefix = prefix.lower()
        async with self._session_factory() as session:
            provider_id = await session.scalar(
                select(LlmProviderConfig.id)
                .where(
                    LlmProviderConfig.status == "active",
                    LlmProviderConfig.route_prefix == prefix,
                )
                .limit(1)
            )
            return provider_id is not None

    async def active_model_codes(self) -> tuple[str, ...]:
        """返回静态模型发现结果；动态上游模型名无法预先枚举。"""
        return ("mock-chat",)

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise LlmConfigurationValidationError(
                "Base URL 必须是有效的 HTTP(S) 地址，不能包含账号或参数"
            )
        if parsed.scheme == "http":
            host = parsed.hostname or ""
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = host.lower() == "localhost"
            if not is_loopback:
                raise LlmConfigurationValidationError(
                    "第三方供应商必须使用 HTTPS；HTTP 仅允许本机调试"
                )
        return value.strip().rstrip("/")

    @staticmethod
    def _normalize_route_prefix(value: str | None) -> str | None:
        """Normalize optional route prefixes to lowercase, URL-safe group identifiers."""
        if value is None or not value.strip():
            return None
        prefix = value.strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", prefix) is None:
            raise LlmConfigurationValidationError(
                "供应商前缀只能以英文字母开头，并使用英文字母、数字、连字符或下划线"
            )
        return prefix

    def _require_cipher(self) -> ProviderSecretCipher:
        if self._cipher is None:
            raise LlmConfigurationValidationError(
                "请先在服务端配置 LLM_PROVIDER_SECRET_KEY，才能保存或使用供应商 API Key"
            )
        return self._cipher

    def _resolved_provider(
        self, provider: LlmProviderConfig, model_code: str, upstream_model: str
    ) -> ResolvedLlmModel:
        return ResolvedLlmModel(
            model_code=model_code,
            upstream_model=upstream_model,
            base_url=provider.base_url,
            api_key=self._require_cipher().decrypt(provider.encrypted_api_key),
        )

    @staticmethod
    def _provider_info(provider: LlmProviderConfig) -> LlmProviderInfo:
        return LlmProviderInfo(
            id=provider.id,
            name=provider.name,
            route_prefix=provider.route_prefix,
            base_url=provider.base_url,
            status=provider.status,
            priority=provider.priority,
            api_key_configured=bool(provider.encrypted_api_key),
            created_at=provider.created_at,
            updated_at=provider.updated_at,
            last_tested_at=provider.last_tested_at,
            last_test_success=provider.last_test_success,
            last_test_message=provider.last_test_message,
        )
