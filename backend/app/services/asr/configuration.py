"""ASR Provider 路由配置。"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models import AsrProviderConfig
from app.services.asr.adapter_registry import ASR_ADAPTERS, GENERIC_ADAPTER
from app.services.asr.volcengine import VolcAsrConnection
from app.services.llm.secrets import ProviderSecretCipher


class AsrConfigurationError(RuntimeError):
    """ASR 连接配置操作失败。"""


@dataclass(frozen=True, slots=True)
class AsrProviderInfo:
    id: UUID
    name: str
    supplier_name: str
    route_prefix: str
    model_name: str
    resource_id: str
    file_transcription_url: str
    realtime_url: str
    status: str
    api_key_configured: bool
    api_key: str | None = field(repr=False)
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedAsrModel:
    requested_model: str
    provider: VolcAsrConnection
    connection_name: str
    supplier_name: str


class AsrConfigurationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: ProviderSecretCipher,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def list_providers(self) -> list[AsrProviderInfo]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(AsrProviderConfig).order_by(
                    AsrProviderConfig.priority, AsrProviderConfig.created_at
                )
            )
            return [self._info(row) for row in rows]

    async def create_provider(
        self,
        *,
        name: str,
        supplier_name: str,
        model_name: str = "doubao-seed-asr-2.0",
        resource_id: str,
        file_transcription_url: str,
        realtime_url: str,
        api_key: str,
        route_prefix: str | None = None,
    ) -> AsrProviderInfo:
        values = [
            name.strip(),
            supplier_name.strip(),
            api_key.strip(),
        ]
        if any(not value for value in values):
            raise AsrConfigurationError("连接名称、供应商和 API Key 均不能为空")
        resource_id = resource_id.strip()
        if not resource_id or len(resource_id) > 200:
            raise AsrConfigurationError("Resource ID 不能为空且不能超过 200 个字符")
        file_transcription_url = self._validate_url(file_transcription_url)
        realtime_url = self._validate_url(realtime_url)
        identity_url = self._identity_url(resource_id, file_transcription_url, realtime_url)
        route_prefix = self._clean_prefix(route_prefix)
        model_name = self._clean_model_name(model_name)
        row = AsrProviderConfig(
            name=name.strip(),
            supplier_name=supplier_name.strip(),
            route_prefix=route_prefix,
            model_name=model_name,
            resource_id=resource_id,
            file_transcription_url=file_transcription_url,
            realtime_url=realtime_url,
            encrypted_api_key=self._cipher.encrypt(api_key.strip()),
            api_fingerprint=self._cipher.fingerprint(identity_url, api_key.strip()),
        )
        try:
            async with self._session_factory.begin() as session:
                await self._backfill_api_fingerprints(session)
                await self._ensure_api_unique(
                    session,
                    resource_id,
                    file_transcription_url,
                    realtime_url,
                    api_key.strip(),
                )
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return self._info(row)
        except IntegrityError as error:
            raise AsrConfigurationError(
                "仓库中已存在相同 Resource ID、端点和 API Key 的 ASR API"
            ) from error

    async def update_provider(self, provider_id: UUID, **changes: object) -> AsrProviderInfo:
        async with self._session_factory.begin() as session:
            row = await session.get(AsrProviderConfig, provider_id)
            if row is None:
                raise AsrConfigurationError("ASR 连接不存在")
            await self._backfill_api_fingerprints(session)
            for key in ("name", "supplier_name"):
                value = changes.get(key)
                if value is not None:
                    cleaned = str(value).strip()
                    if not cleaned:
                        raise AsrConfigurationError(f"{key} 不能为空")
                    setattr(row, key, cleaned)
            if changes.get("model_name") is not None:
                row.model_name = self._clean_model_name(str(changes["model_name"]))
            if "resource_id" in changes and changes["resource_id"] is not None:
                resource_id = str(changes["resource_id"]).strip()
                if not resource_id or len(resource_id) > 200:
                    raise AsrConfigurationError("Resource ID 不能为空且不能超过 200 个字符")
                row.resource_id = resource_id
            if (
                "file_transcription_url" in changes
                and changes["file_transcription_url"] is not None
            ):
                row.file_transcription_url = self._validate_url(
                    str(changes["file_transcription_url"])
                )
            if "realtime_url" in changes and changes["realtime_url"] is not None:
                row.realtime_url = self._validate_url(str(changes["realtime_url"]))
            if "route_prefix" in changes:
                row.route_prefix = self._clean_prefix(changes["route_prefix"])
            if changes.get("status") is not None:
                if changes["status"] not in {"active", "disabled"}:
                    raise AsrConfigurationError("状态无效")
                row.status = str(changes["status"])
            api_key = (
                str(changes["api_key"]).strip()
                if changes.get("api_key") is not None and str(changes["api_key"]).strip()
                else self._cipher.decrypt(row.encrypted_api_key)
            )
            await self._ensure_api_unique(
                session,
                row.resource_id,
                row.file_transcription_url,
                row.realtime_url,
                api_key,
                exclude_provider_id=row.id,
            )
            if changes.get("api_key") is not None and str(changes["api_key"]).strip():
                row.encrypted_api_key = self._cipher.encrypt(api_key)
            row.api_fingerprint = self._cipher.fingerprint(
                self._identity_url(row.resource_id, row.file_transcription_url, row.realtime_url),
                api_key,
            )
            await session.flush()
            await session.refresh(row)
            return self._info(row)

    async def delete_provider(self, provider_id: UUID) -> None:
        async with self._session_factory.begin() as session:
            row = await session.get(AsrProviderConfig, provider_id)
            if row is None:
                raise AsrConfigurationError("ASR 连接不存在")
            await session.delete(row)

    async def active_models(self) -> tuple[str, ...]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(AsrProviderConfig.model_name)
                .where(AsrProviderConfig.status == "active")
                .distinct()
                .order_by(AsrProviderConfig.model_name)
            )
            return tuple(rows)

    async def resolve_pool(self, requested_model: str) -> tuple[ResolvedAsrModel, ...]:
        prefix, separator, model_name = requested_model.partition("/")
        if separator:
            # Keep the old explicit route form working for existing callers.
            if prefix.lower() != "volc" or not model_name or "/" in model_name:
                return ()
            prefix = "volc"
        else:
            # Public model names are provider-independent; routing to the only
            # currently configured adapter happens inside the gateway.
            model_name = requested_model
            prefix = "volc"
        model_name = model_name.lower()
        adapter = ASR_ADAPTERS.get(prefix, GENERIC_ADAPTER)
        if adapter.adapter != "volc_websocket_v3":
            # Generic OpenAI-compatible ASR routing is reserved for future
            # connection groups; current admin configuration only permits Volc.
            return ()
        statement = select(AsrProviderConfig).where(AsrProviderConfig.status == "active")
        statement = statement.where(AsrProviderConfig.route_prefix == prefix)
        statement = statement.where(AsrProviderConfig.model_name == model_name)
        async with self._session_factory() as session:
            rows = await session.scalars(
                statement.order_by(
                    AsrProviderConfig.priority,
                    AsrProviderConfig.created_at,
                    AsrProviderConfig.id,
                )
            )
            return tuple(
                ResolvedAsrModel(
                    requested_model=requested_model,
                    provider=VolcAsrConnection(
                        api_key=self._cipher.decrypt(row.encrypted_api_key),
                        resource_id=row.resource_id,
                        file_transcription_url=row.file_transcription_url,
                        realtime_url=row.realtime_url,
                    ),
                    connection_name=row.name,
                    supplier_name=row.supplier_name,
                )
                for row in rows
            )

    def _info(self, row: AsrProviderConfig) -> AsrProviderInfo:
        return AsrProviderInfo(
            id=row.id,
            name=row.name,
            supplier_name=row.supplier_name,
            route_prefix=row.route_prefix,
            model_name=row.model_name,
            resource_id=row.resource_id,
            file_transcription_url=row.file_transcription_url,
            realtime_url=row.realtime_url,
            status=row.status,
            api_key_configured=bool(row.encrypted_api_key),
            api_key=self._cipher.decrypt(row.encrypted_api_key),
            created_at=row.created_at,
        )

    async def _backfill_api_fingerprints(self, session: AsyncSession) -> None:
        """为历史上缺少指纹的记录补充包含两种端点的身份指纹。"""
        rows = list(await session.scalars(select(AsrProviderConfig)))
        for row in rows:
            if row.api_fingerprint is not None:
                continue
            api_key = self._cipher.decrypt(row.encrypted_api_key)
            fingerprint = self._cipher.fingerprint(
                self._identity_url(row.resource_id, row.file_transcription_url, row.realtime_url),
                api_key,
            )
            row.api_fingerprint = fingerprint

    async def _ensure_api_unique(
        self,
        session: AsyncSession,
        resource_id: str,
        file_transcription_url: str,
        realtime_url: str,
        api_key: str,
        *,
        exclude_provider_id: UUID | None = None,
    ) -> None:
        """按 LLM 连接规则，阻止相同 URL 与 API Key 在不同分组中重复保存。"""
        identity_url = self._identity_url(
            resource_id,
            self._validate_url(file_transcription_url),
            self._validate_url(realtime_url),
        )
        fingerprint = self._cipher.fingerprint(identity_url, api_key)
        for row in await session.scalars(select(AsrProviderConfig)):
            if row.id == exclude_provider_id:
                continue
            row_identity = self._identity_url(
                row.resource_id,
                self._validate_url(row.file_transcription_url),
                self._validate_url(row.realtime_url),
            )
            if row_identity != identity_url:
                continue
            stored_key_matches = self._cipher.decrypt(row.encrypted_api_key) == api_key
            if row.api_fingerprint == fingerprint or stored_key_matches:
                raise AsrConfigurationError(
                    "仓库中已存在相同 Resource ID、端点和 API Key 的 ASR API"
                )

    @staticmethod
    def _identity_url(resource_id: str, file_url: str, realtime_url: str) -> str:
        """将火山资源和两种调用模式端点合并为稳定的密钥唯一性标识。"""
        return f"{resource_id}\0{file_url}\0{realtime_url}"

    @staticmethod
    def _validate_url(value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("wss://") or len(value) > 500:
            raise AsrConfigurationError("ASR WebSocket Base URL 必须为有效的 wss:// 地址")
        return value

    @staticmethod
    def _clean_prefix(value: object) -> str:
        prefix = str(value).strip().lower() if value is not None else ""
        if prefix != "volc":
            raise AsrConfigurationError("当前仅允许使用 volc 模型路由前缀")
        return prefix

    @staticmethod
    def _clean_model_name(value: str) -> str:
        model_name = value.strip().lower()
        if len(model_name) > 200 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", model_name):
            raise AsrConfigurationError(
                "公开模型名只能包含英文字母、数字、点、下划线和连字符，且不能以符号开头"
            )
        return model_name
