"""OpenAI 兼容 Embedding 上游配置与模型路由。"""

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import EmbeddingProviderConfig
from app.services.llm.secrets import ProviderSecretCipher


class EmbeddingConfigurationError(RuntimeError):
    """Embedding 配置业务错误。"""


class EmbeddingConfigurationNotFoundError(EmbeddingConfigurationError):
    """配置不存在。"""


class EmbeddingConfigurationConflictError(EmbeddingConfigurationError):
    """重复连接配置。"""


class EmbeddingConfigurationValidationError(EmbeddingConfigurationError):
    """连接参数不合法。"""


@dataclass(frozen=True, slots=True)
class EmbeddingProviderInfo:
    id: UUID
    name: str
    supplier_name: str
    route_prefix: str | None
    base_url: str
    status: str
    priority: int
    api_key_configured: bool
    api_key: str | None = field(repr=False)
    last_tested_at: datetime | None
    last_test_success: bool | None
    last_test_message: str | None


@dataclass(frozen=True, slots=True)
class ResolvedEmbeddingModel:
    model: str
    upstream_model: str
    base_url: str
    api_key: str = field(repr=False)
    connection_name: str = ""
    supplier_name: str = ""
    route_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingRouteGroup:
    prefix: str | None
    connection_count: int
    supplier_names: tuple[str, ...]


class EmbeddingConfigurationService:
    """维护连接、加密 API Key，并按模型前缀和优先级解析路由。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: ProviderSecretCipher | None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def list_providers(self) -> list[EmbeddingProviderInfo]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(EmbeddingProviderConfig).order_by(
                    EmbeddingProviderConfig.priority,
                    EmbeddingProviderConfig.created_at,
                    EmbeddingProviderConfig.id,
                )
            )
            return [self._to_info(row) for row in rows]

    async def create_provider(
        self,
        name: str,
        supplier_name: str,
        route_prefix: str | None,
        base_url: str,
        api_key: str,
    ) -> EmbeddingProviderInfo:
        name, supplier_name = name.strip(), supplier_name.strip()
        route_prefix = self._normalize_prefix(route_prefix)
        base_url, api_key = self._normalize_url(base_url), api_key.strip()
        if not name or not supplier_name or not api_key:
            raise EmbeddingConfigurationValidationError("连接名称、供应商名称和 API Key 不能为空")
        cipher = self._require_cipher()
        try:
            async with self._session_factory.begin() as session:
                if await session.scalar(
                    select(EmbeddingProviderConfig.id).where(
                        EmbeddingProviderConfig.base_url == base_url,
                        EmbeddingProviderConfig.api_fingerprint
                        == cipher.fingerprint(base_url, api_key),
                    )
                ):
                    raise EmbeddingConfigurationConflictError("相同 Base URL 和 API Key 已存在")
                row = EmbeddingProviderConfig(
                    name=name,
                    supplier_name=supplier_name,
                    route_prefix=route_prefix,
                    base_url=base_url,
                    encrypted_api_key=cipher.encrypt(api_key),
                    api_fingerprint=cipher.fingerprint(base_url, api_key),
                )
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return self._to_info(row)
        except IntegrityError as error:
            raise EmbeddingConfigurationConflictError("相同 Base URL 和 API Key 已存在") from error

    async def update_provider(
        self,
        provider_id: UUID,
        *,
        name: str | None = None,
        supplier_name: str | None = None,
        route_prefix: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        status: str | None = None,
        priority: int | None = None,
    ) -> EmbeddingProviderInfo:
        cipher = self._require_cipher()
        try:
            async with self._session_factory.begin() as session:
                row = await session.get(EmbeddingProviderConfig, provider_id)
                if row is None:
                    raise EmbeddingConfigurationNotFoundError("Embedding API 不存在")
                if name is not None:
                    row.name = self._required_text(name, "连接名称", 100)
                if supplier_name is not None:
                    row.supplier_name = self._required_text(supplier_name, "供应商名称", 100)
                if route_prefix is not None:
                    row.route_prefix = self._normalize_prefix(route_prefix)
                if status is not None:
                    row.status = status
                if priority is not None:
                    row.priority = priority
                new_url = self._normalize_url(base_url) if base_url is not None else row.base_url
                new_key = api_key.strip() if api_key is not None and api_key.strip() else None
                if new_url != row.base_url or new_key is not None:
                    key = new_key or cipher.decrypt(row.encrypted_api_key)
                    fingerprint = cipher.fingerprint(new_url, key)
                    duplicate = await session.scalar(
                        select(EmbeddingProviderConfig.id).where(
                            EmbeddingProviderConfig.api_fingerprint == fingerprint,
                            EmbeddingProviderConfig.id != provider_id,
                        )
                    )
                    if duplicate:
                        raise EmbeddingConfigurationConflictError("相同 Base URL 和 API Key 已存在")
                    row.base_url = new_url
                    row.api_fingerprint = fingerprint
                    row.encrypted_api_key = cipher.encrypt(key)
                await session.flush()
                await session.refresh(row)
                return self._to_info(row)
        except IntegrityError as error:
            raise EmbeddingConfigurationConflictError("相同 Base URL 和 API Key 已存在") from error

    async def delete_provider(self, provider_id: UUID) -> None:
        async with self._session_factory.begin() as session:
            row = await session.get(EmbeddingProviderConfig, provider_id)
            if row is None:
                raise EmbeddingConfigurationNotFoundError("Embedding API 不存在")
            await session.delete(row)

    async def set_test_result(
        self, provider_id: UUID, success: bool, message: str
    ) -> EmbeddingProviderInfo:
        async with self._session_factory.begin() as session:
            row = await session.get(EmbeddingProviderConfig, provider_id)
            if row is None:
                raise EmbeddingConfigurationNotFoundError("Embedding API 不存在")
            row.last_tested_at = datetime.now(UTC)
            row.last_test_success = success
            row.last_test_message = message[:250]
            await session.flush()
            await session.refresh(row)
            return self._to_info(row)

    async def resolve_model_pool(self, model: str) -> tuple[ResolvedEmbeddingModel, ...]:
        normalized = model.strip()
        if not normalized:
            return ()
        prefix, separator, upstream_model = normalized.partition("/")
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(EmbeddingProviderConfig)
                .where(EmbeddingProviderConfig.status == "active")
                .order_by(
                    EmbeddingProviderConfig.priority,
                    EmbeddingProviderConfig.created_at,
                    EmbeddingProviderConfig.id,
                )
            )
            configs = list(rows)
        resolved: list[ResolvedEmbeddingModel] = []
        for row in configs:
            if row.route_prefix is not None and (
                not separator or row.route_prefix.casefold() != prefix.casefold()
            ):
                continue
            if row.route_prefix is None and separator:
                continue
            key = self._require_cipher().decrypt(row.encrypted_api_key)
            resolved.append(
                ResolvedEmbeddingModel(
                    model=normalized,
                    upstream_model=upstream_model if separator else normalized,
                    base_url=row.base_url,
                    api_key=key,
                    connection_name=row.name,
                    supplier_name=row.supplier_name,
                    route_prefix=row.route_prefix,
                )
            )
        return tuple(resolved)

    async def active_model_codes(self) -> tuple[str, ...]:
        """动态模型名无法枚举；模型前缀可经 provider-catalog 单独查询。"""
        return ()

    async def list_public_route_groups(self) -> tuple[EmbeddingRouteGroup, ...]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    EmbeddingProviderConfig.route_prefix,
                    EmbeddingProviderConfig.supplier_name,
                    func.count(EmbeddingProviderConfig.id),
                )
                .where(EmbeddingProviderConfig.status == "active")
                .group_by(
                    EmbeddingProviderConfig.route_prefix,
                    EmbeddingProviderConfig.supplier_name,
                )
            )
        groups: dict[str | None, list[tuple[str, int]]] = {}
        for prefix, supplier, count in rows.all():
            groups.setdefault(prefix, []).append((supplier, count))
        return tuple(
            EmbeddingRouteGroup(
                prefix=prefix,
                connection_count=sum(count for _, count in items),
                supplier_names=tuple(sorted({name for name, _ in items})),
            )
            for prefix, items in groups.items()
        )

    async def test_provider(self, provider_id: UUID, model: str, client: httpx.AsyncClient) -> str:
        async with self._session_factory() as session:
            row = await session.get(EmbeddingProviderConfig, provider_id)
            if row is None:
                raise EmbeddingConfigurationNotFoundError("Embedding API 不存在")
            key = self._require_cipher().decrypt(row.encrypted_api_key)
            endpoint = "/embeddings/multimodal" if row.route_prefix == "volc" else "/embeddings"
            url = row.base_url.rstrip("/") + endpoint
            upstream_model = model
            if row.route_prefix == "volc" and "/" in model:
                prefix, upstream_model = model.split("/", 1)
                if prefix.casefold() != "volc":
                    raise EmbeddingConfigurationValidationError("测试模型前缀与连接配置不匹配")
        if row.route_prefix == "volc":
            request_body = {
                "model": upstream_model,
                "encoding_format": "float",
                "input": [{"type": "text", "text": "gateway connectivity test"}],
            }
        else:
            request_body = {"model": upstream_model, "input": "gateway connectivity test"}
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json=request_body,
        )
        response.raise_for_status()
        result = response.json()
        data = result.get("data") if isinstance(result, dict) else None
        if row.route_prefix == "volc" and isinstance(data, dict):
            embedding = data.get("embedding")
        else:
            embedding = (
                data[0].get("embedding")
                if isinstance(data, list) and data and isinstance(data[0], dict)
                else None
            )
        if not isinstance(embedding, list) or not embedding:
            raise EmbeddingConfigurationValidationError("上游返回的 Embedding 向量无效")
        return f"连接成功，向量维度 {len(embedding)}"

    @staticmethod
    def _normalize_prefix(value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        prefix = value.strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", prefix):
            raise EmbeddingConfigurationValidationError("模型路由前缀格式不合法")
        return prefix.lower()

    @staticmethod
    def _normalize_url(value: str) -> str:
        url = value.strip().rstrip("/")
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise EmbeddingConfigurationValidationError("Base URL 必须是有效的 HTTP(S) URL")
        if parts.scheme == "http":
            host = parts.hostname or ""
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = host.lower() == "localhost"
            if not is_loopback:
                raise EmbeddingConfigurationValidationError(
                    "第三方供应商必须使用 HTTPS；HTTP 仅允许本机调试"
                )
        if not url:
            raise EmbeddingConfigurationValidationError("Base URL 不能为空")
        return url

    @staticmethod
    def _required_text(value: str, label: str, maximum: int) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > maximum:
            raise EmbeddingConfigurationValidationError(f"{label}长度必须为 1 到 {maximum} 个字符")
        return cleaned

    def _require_cipher(self) -> ProviderSecretCipher:
        if self._cipher is None:
            raise EmbeddingConfigurationValidationError(
                "Embedding API Key 管理未启用，请设置 LLM_PROVIDER_SECRET_KEY"
            )
        return self._cipher

    def _to_info(self, row: EmbeddingProviderConfig) -> EmbeddingProviderInfo:
        cipher = self._require_cipher()
        return EmbeddingProviderInfo(
            id=row.id,
            name=row.name,
            supplier_name=row.supplier_name,
            route_prefix=row.route_prefix,
            base_url=row.base_url,
            status=row.status,
            priority=row.priority,
            api_key_configured=bool(row.encrypted_api_key),
            api_key=cipher.decrypt(row.encrypted_api_key),
            last_tested_at=row.last_tested_at,
            last_test_success=row.last_test_success,
            last_test_message=row.last_test_message,
        )
