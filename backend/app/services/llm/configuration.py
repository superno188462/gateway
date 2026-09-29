"""LLM 上游供应商和公开模型映射的管理用例。"""

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import LlmModelConfig, LlmProviderConfig
from app.services.llm.secrets import ProviderSecretCipher


class LlmConfigurationNotFoundError(RuntimeError):
    """供应商或模型配置不存在。"""


class LlmConfigurationConflictError(RuntimeError):
    """供应商名称或公开模型代码已被使用。"""


class LlmConfigurationValidationError(RuntimeError):
    """供应商或模型配置字段不合法。"""


@dataclass(frozen=True, slots=True)
class LlmModelInfo:
    """可展示的公开模型标识及其上游映射。"""

    id: UUID
    model_code: str
    upstream_model: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LlmProviderInfo:
    """供应商连接信息；只含 API Key 是否已配置，不含密文或明文。"""

    id: UUID
    name: str
    base_url: str
    status: str
    api_key_configured: bool
    created_at: datetime
    updated_at: datetime
    last_tested_at: datetime | None
    last_test_success: bool | None
    last_test_message: str | None
    models: tuple[LlmModelInfo, ...]


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


class LlmConfigurationService:
    """维护管理员配置的 OpenAI 兼容端点和模型映射。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: ProviderSecretCipher | None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def list_providers(self) -> list[LlmProviderInfo]:
        """列出供应商和模型；绝不返回供应商 API Key。"""
        async with self._session_factory() as session:
            providers = await session.scalars(
                select(LlmProviderConfig).order_by(
                    LlmProviderConfig.created_at, LlmProviderConfig.id
                )
            )
            result: list[LlmProviderInfo] = []
            for provider in providers:
                models = await session.scalars(
                    select(LlmModelConfig)
                    .where(LlmModelConfig.provider_id == provider.id)
                    .order_by(LlmModelConfig.model_code)
                )
                result.append(self._provider_info(provider, list(models)))
            return result

    async def create_provider(self, name: str, base_url: str, api_key: str) -> LlmProviderInfo:
        """创建供应商连接，并加密保存 API Key。"""
        name = name.strip()
        base_url = self._normalize_base_url(base_url)
        api_key = api_key.strip()
        if not name or len(name) > 100 or not api_key:
            raise LlmConfigurationValidationError("供应商名称和 API Key 不能为空")
        cipher = self._require_cipher()
        provider = LlmProviderConfig(
            name=name,
            base_url=base_url,
            encrypted_api_key=cipher.encrypt(api_key),
            status="active",
        )
        try:
            async with self._session_factory.begin() as session:
                session.add(provider)
                await session.flush()
                await session.refresh(provider)
                return self._provider_info(provider, [])
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
                if base_url is not None:
                    provider.base_url = self._normalize_base_url(base_url)
                if api_key is not None and api_key.strip():
                    provider.encrypted_api_key = self._require_cipher().encrypt(api_key.strip())
                if status is not None:
                    provider.status = status
                await session.flush()
                await session.refresh(provider)
                models = await session.scalars(
                    select(LlmModelConfig).where(LlmModelConfig.provider_id == provider.id)
                )
                return self._provider_info(provider, list(models))
        except IntegrityError as error:
            raise LlmConfigurationConflictError("供应商名称已存在") from error

    async def delete_provider(self, provider_id: UUID) -> None:
        """删除供应商及其模型映射。"""
        async with self._session_factory.begin() as session:
            provider = await session.get(LlmProviderConfig, provider_id)
            if provider is None:
                raise LlmConfigurationNotFoundError("供应商不存在")
            await session.delete(provider)

    async def create_model(
        self, provider_id: UUID, model_code: str, upstream_model: str
    ) -> LlmModelInfo:
        """新增网关公开模型名到供应商模型名的映射。"""
        model_code = model_code.strip()
        upstream_model = upstream_model.strip()
        if (
            not model_code
            or len(model_code) > 100
            or not upstream_model
            or len(upstream_model) > 200
        ):
            raise LlmConfigurationValidationError("公开模型名和上游模型名不能为空且长度需符合限制")
        try:
            async with self._session_factory.begin() as session:
                if await session.get(LlmProviderConfig, provider_id) is None:
                    raise LlmConfigurationNotFoundError("供应商不存在")
                model = LlmModelConfig(
                    provider_id=provider_id,
                    model_code=model_code,
                    upstream_model=upstream_model,
                    status="active",
                )
                session.add(model)
                await session.flush()
                await session.refresh(model)
                return self._model_info(model)
        except IntegrityError as error:
            raise LlmConfigurationConflictError("网关公开模型名已存在") from error

    async def update_model(
        self,
        model_id: UUID,
        *,
        model_code: str | None = None,
        upstream_model: str | None = None,
        status: str | None = None,
    ) -> LlmModelInfo:
        """修改公开模型别名、上游模型名或启用状态。"""
        try:
            async with self._session_factory.begin() as session:
                model = await session.get(LlmModelConfig, model_id)
                if model is None:
                    raise LlmConfigurationNotFoundError("模型配置不存在")
                if model_code is not None:
                    cleaned = model_code.strip()
                    if not cleaned or len(cleaned) > 100:
                        raise LlmConfigurationValidationError(
                            "公开模型名长度必须为 1 到 100 个字符"
                        )
                    model.model_code = cleaned
                if upstream_model is not None:
                    cleaned = upstream_model.strip()
                    if not cleaned or len(cleaned) > 200:
                        raise LlmConfigurationValidationError(
                            "上游模型名长度必须为 1 到 200 个字符"
                        )
                    model.upstream_model = cleaned
                if status is not None:
                    model.status = status
                await session.flush()
                await session.refresh(model)
                return self._model_info(model)
        except IntegrityError as error:
            raise LlmConfigurationConflictError("网关公开模型名已存在") from error

    async def delete_model(self, model_id: UUID) -> None:
        """删除模型映射。"""
        async with self._session_factory.begin() as session:
            model = await session.get(LlmModelConfig, model_id)
            if model is None:
                raise LlmConfigurationNotFoundError("模型配置不存在")
            await session.delete(model)

    async def resolve_model_pool(self, model_code: str) -> tuple[ResolvedLlmModel, ...]:
        """返回同一公开模型代码对应的所有启用上游连接。"""
        async with self._session_factory() as session:
            rows = await session.execute(
                select(LlmModelConfig, LlmProviderConfig)
                .join(LlmProviderConfig, LlmProviderConfig.id == LlmModelConfig.provider_id)
                .where(
                    LlmModelConfig.model_code == model_code,
                    LlmModelConfig.status == "active",
                    LlmProviderConfig.status == "active",
                )
                .order_by(LlmProviderConfig.id)
            )
            return tuple(self._resolved_model(pair) for pair in rows.all())

    async def test_provider(
        self, provider_id: UUID, http_client: httpx.AsyncClient
    ) -> ProviderTestResult:
        """使用该连接的启用模型发送最小 Chat Completions 请求测试连通性。"""
        async with self._session_factory() as session:
            provider = await session.get(LlmProviderConfig, provider_id)
            if provider is None:
                raise LlmConfigurationNotFoundError("供应商不存在")
            model = await session.scalar(
                select(LlmModelConfig)
                .where(
                    LlmModelConfig.provider_id == provider_id,
                    LlmModelConfig.status == "active",
                )
                .order_by(LlmModelConfig.created_at, LlmModelConfig.id)
            )
            api_key = self._require_cipher().decrypt(provider.encrypted_api_key)
        tested_at = datetime.now(UTC)
        if model is None:
            success = False
            message = "请先添加并启用一个模型映射，再测试连通性"
        else:
            try:
                response = await http_client.post(
                    f"{provider.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model.upstream_model,
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

    async def active_model_codes(self) -> tuple[str, ...]:
        """返回网关可调用的公开模型代码。"""
        async with self._session_factory() as session:
            codes = await session.scalars(
                select(LlmModelConfig.model_code)
                .join(LlmProviderConfig, LlmProviderConfig.id == LlmModelConfig.provider_id)
                .where(
                    LlmModelConfig.status == "active",
                    LlmProviderConfig.status == "active",
                )
            )
            return ("mock-chat", *tuple(sorted(set(codes))))

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

    def _require_cipher(self) -> ProviderSecretCipher:
        if self._cipher is None:
            raise LlmConfigurationValidationError(
                "请先在服务端配置 LLM_PROVIDER_SECRET_KEY，才能保存或使用供应商 API Key"
            )
        return self._cipher

    def _resolved_model(self, pair: tuple[LlmModelConfig, LlmProviderConfig]) -> ResolvedLlmModel:
        model, provider = pair
        return ResolvedLlmModel(
            model_code=model.model_code,
            upstream_model=model.upstream_model,
            base_url=provider.base_url,
            api_key=self._require_cipher().decrypt(provider.encrypted_api_key),
        )

    @staticmethod
    def _model_info(model: LlmModelConfig) -> LlmModelInfo:
        return LlmModelInfo(
            id=model.id,
            model_code=model.model_code,
            upstream_model=model.upstream_model,
            status=model.status,
            created_at=model.created_at,
        )

    @classmethod
    def _provider_info(
        cls, provider: LlmProviderConfig, models: list[LlmModelConfig]
    ) -> LlmProviderInfo:
        return LlmProviderInfo(
            id=provider.id,
            name=provider.name,
            base_url=provider.base_url,
            status=provider.status,
            api_key_configured=bool(provider.encrypted_api_key),
            created_at=provider.created_at,
            updated_at=provider.updated_at,
            last_tested_at=provider.last_tested_at,
            last_test_success=provider.last_test_success,
            last_test_message=provider.last_test_message,
            models=tuple(cls._model_info(model) for model in models),
        )
