"""应用依赖容器和 FastAPI 依赖适配器。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.api_keys import ApiKeyService
from app.application.auth import AdminBootstrapService, AuthService
from app.application.projects import ProjectService
from app.config import Settings
from app.domain.health import ReadinessProbe
from app.domain.service_catalog import ServiceCatalogItem
from app.infrastructure.db.engine import SqlAlchemyReadinessProbe, create_database_engine
from app.infrastructure.db.models import User
from app.request_logging.application import GatewayRequestRecorder
from app.resources.application import ResourceService
from app.resources.infrastructure import PostgresResourceRepository
from app.security import JwtService, PasswordService
from app.service_management.application import ProjectServiceManagement
from app.services.asr.application import AsrGatewayService
from app.services.asr.catalog import ASR_SERVICE
from app.services.asr.configuration import AsrConfigurationService
from app.services.embedding.application import EmbeddingGatewayService
from app.services.embedding.catalog import EMBEDDING_SERVICE, RAG_SERVICE
from app.services.embedding.configuration import EmbeddingConfigurationService
from app.services.embedding.provider import ConfiguredEmbeddingProvider
from app.services.embedding.rag import RagKnowledgeService
from app.services.llm.application import LlmGatewayService
from app.services.llm.catalog import LLM_SERVICE
from app.services.llm.configuration import LlmConfigurationService
from app.services.llm.providers.openai_compatible import ConfiguredLlmProvider
from app.services.llm.secrets import ProviderSecretCipher
from app.services.project_context.application import ProjectContextService
from app.services.project_context.catalog import PROJECT_CONTEXT_SERVICE
from app.technical_logging import TechnicalLogService
from app.usage.application import RequestLogService


@dataclass(slots=True)
class AppContainer:
    """应用生命周期内唯一的依赖注册表。

    配置、数据库引擎和基础设施服务在这里创建一次，并由 FastAPI 的 Depends
    适配器按请求注入。业务服务在后续阶段继续注册到同一个容器。
    """

    settings: Settings
    database_engine: AsyncEngine | None
    readiness_probe: ReadinessProbe
    session_factory: async_sessionmaker[AsyncSession] | None
    password_service: PasswordService
    jwt_service: JwtService
    admin_bootstrap: AdminBootstrapService | None
    auth_service: AuthService | None
    project_service: ProjectService | None
    resource_service: ResourceService | None
    project_context_service: ProjectContextService | None
    api_key_service: ApiKeyService | None
    service_management: ProjectServiceManagement | None
    llm_gateway_service: LlmGatewayService | None
    llm_configuration_service: LlmConfigurationService | None
    embedding_configuration_service: EmbeddingConfigurationService | None
    embedding_gateway_service: EmbeddingGatewayService | None
    embedding_rag_service: RagKnowledgeService | None
    asr_configuration_service: AsrConfigurationService | None
    asr_gateway_service: AsrGatewayService | None
    request_log_service: RequestLogService | None
    request_recorder: GatewayRequestRecorder | None
    technical_log_service: TechnicalLogService
    log_retention_task: asyncio.Task[None] | None
    http_client: httpx.AsyncClient | None

    @classmethod
    def build(
        cls,
        settings: Settings,
        readiness_probe: ReadinessProbe | None = None,
    ) -> "AppContainer":
        """创建容器并注册 B0 所需的单例服务。"""
        if readiness_probe is not None:
            return cls(
                settings=settings,
                database_engine=None,
                readiness_probe=readiness_probe,
                session_factory=None,
                password_service=PasswordService(),
                jwt_service=JwtService(
                    settings.jwt_secret_key,
                    settings.jwt_access_token_expire_minutes,
                ),
                admin_bootstrap=None,
                auth_service=None,
                project_service=None,
                resource_service=None,
                project_context_service=None,
                api_key_service=None,
                service_management=None,
                llm_gateway_service=None,
                llm_configuration_service=None,
                embedding_configuration_service=None,
                embedding_gateway_service=None,
                embedding_rag_service=None,
                asr_configuration_service=None,
                asr_gateway_service=None,
                request_log_service=None,
                request_recorder=None,
                technical_log_service=TechnicalLogService(settings.log_file_path),
                log_retention_task=None,
                http_client=None,
            )

        database_engine = create_database_engine(settings.database_url)
        session_factory = async_sessionmaker(database_engine, expire_on_commit=False)
        password_service = PasswordService()
        jwt_service = JwtService(settings.jwt_secret_key, settings.jwt_access_token_expire_minutes)
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            # Keep the gateway's provider egress direct during network-path diagnosis.
            trust_env=False,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                # Keep idle TLS sessions warm across ordinary pauses between requests.
                keepalive_expiry=60.0,
            ),
        )
        llm_configuration_service = LlmConfigurationService(
            session_factory,
            ProviderSecretCipher(settings.llm_provider_secret_key.get_secret_value())
            if settings.llm_provider_secret_key is not None
            else None,
        )
        embedding_configuration_service = EmbeddingConfigurationService(
            session_factory,
            ProviderSecretCipher(settings.llm_provider_secret_key.get_secret_value())
            if settings.llm_provider_secret_key is not None
            else None,
        )
        asr_configuration_service = (
            AsrConfigurationService(
                session_factory,
                ProviderSecretCipher(settings.llm_provider_secret_key.get_secret_value()),
            )
            if settings.llm_provider_secret_key is not None
            else None
        )
        request_log_service = RequestLogService(session_factory)
        request_recorder = GatewayRequestRecorder(session_factory)
        embedding_gateway_service = EmbeddingGatewayService(
            session_factory,
            ConfiguredEmbeddingProvider(embedding_configuration_service, http_client),
            request_recorder,
        )

        async def llm_catalog_provider() -> tuple[ServiceCatalogItem, ...]:
            """把当前启用的公开模型名接入通用服务目录。"""
            models = (
                await llm_configuration_service.active_model_codes()
                if llm_configuration_service is not None
                else ("mock-chat",)
            )
            return (
                ServiceCatalogItem(LLM_SERVICE.code, LLM_SERVICE.name, models),
                ServiceCatalogItem(
                    EMBEDDING_SERVICE.code,
                    EMBEDDING_SERVICE.name,
                    await embedding_configuration_service.active_model_codes()
                    if embedding_configuration_service is not None
                    else (),
                ),
                ServiceCatalogItem(
                    ASR_SERVICE.code,
                    ASR_SERVICE.name,
                    await asr_configuration_service.active_models()
                    if asr_configuration_service is not None
                    else (),
                    quota_unit=ASR_SERVICE.quota_unit,
                ),
                PROJECT_CONTEXT_SERVICE,
                RAG_SERVICE,
            )

        return cls(
            settings=settings,
            database_engine=database_engine,
            readiness_probe=SqlAlchemyReadinessProbe(database_engine),
            session_factory=session_factory,
            password_service=password_service,
            jwt_service=jwt_service,
            admin_bootstrap=AdminBootstrapService(
                session_factory,
                password_service,
                settings.admin_username,
                settings.admin_password,
            ),
            auth_service=AuthService(
                session_factory,
                password_service,
                jwt_service,
                {
                    "mock-llm-v1": settings.default_llm_monthly_token_limit,
                    "asr-v1": settings.default_asr_monthly_quota_seconds,
                    "embedding-v1": settings.default_embedding_monthly_token_limit,
                },
            ),
            project_service=ProjectService(session_factory),
            resource_service=ResourceService(PostgresResourceRepository(session_factory)),
            project_context_service=ProjectContextService(session_factory),
            api_key_service=(
                ApiKeyService(session_factory, settings.api_key_secret_key.get_secret_value())
                if settings.api_key_secret_key is not None
                else None
            ),
            service_management=ProjectServiceManagement(
                session_factory,
                (LLM_SERVICE,),
                catalog_provider=llm_catalog_provider,
            ),
            llm_gateway_service=LlmGatewayService(
                session_factory,
                ConfiguredLlmProvider(llm_configuration_service, http_client),
                request_recorder,
            ),
            llm_configuration_service=llm_configuration_service,
            embedding_configuration_service=embedding_configuration_service,
            embedding_gateway_service=embedding_gateway_service,
            embedding_rag_service=RagKnowledgeService(
                session_factory,
                embedding_gateway_service,
                request_recorder,
            ),
            asr_configuration_service=asr_configuration_service,
            asr_gateway_service=(
                AsrGatewayService(
                    session_factory,
                    asr_configuration_service,
                    request_recorder,
                )
                if asr_configuration_service is not None
                else None
            ),
            request_log_service=request_log_service,
            request_recorder=request_recorder,
            technical_log_service=TechnicalLogService(settings.log_file_path),
            log_retention_task=None,
            http_client=http_client,
        )

    async def startup(self) -> None:
        """执行启动前管理员状态校验和环境变量引导。"""
        if self.admin_bootstrap is not None:
            await self.admin_bootstrap.ensure()
        if self.request_log_service is not None:
            self.log_retention_task = asyncio.create_task(
                self.request_log_service.run_periodically(),
                name="gateway-request-log-retention",
            )

    async def close(self) -> None:
        """释放容器拥有的异步资源。"""
        if self.log_retention_task is not None:
            self.log_retention_task.cancel()
            try:
                await self.log_retention_task
            except asyncio.CancelledError:
                pass
            self.log_retention_task = None
        if self.database_engine is not None:
            await self.database_engine.dispose()
        if self.http_client is not None:
            await self.http_client.aclose()


def get_container(request: Request) -> AppContainer:
    """从应用状态取得当前应用的容器单例。"""
    return cast(AppContainer, request.app.state.container)


def get_settings(request: Request) -> Settings:
    """注入配置单例。"""
    return get_container(request).settings


def get_readiness_probe(request: Request) -> ReadinessProbe:
    """注入数据库就绪探针单例。"""
    return get_container(request).readiness_probe


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为业务请求提供短生命周期数据库会话。"""
    session_factory = get_container(request).session_factory
    if session_factory is None:
        raise RuntimeError("数据库会话未注册")
    async with session_factory() as session:
        yield session


def get_auth_service(request: Request) -> AuthService:
    """注入通用认证服务单例。"""
    service = get_container(request).auth_service
    if service is None:
        raise RuntimeError("认证服务未注册")
    return service


def get_project_service(request: Request) -> ProjectService:
    """注入项目服务单例。"""
    service = get_container(request).project_service
    if service is None:
        raise RuntimeError("项目服务未注册")
    return service


def get_resource_service(request: Request) -> ResourceService:
    """注入项目模板与记忆资源服务。"""
    service = get_container(request).resource_service
    if service is None:
        raise RuntimeError("项目资源服务未注册")
    return service


def get_project_context_service(request: Request) -> ProjectContextService:
    """注入项目上下文服务单例。"""
    service = get_container(request).project_context_service
    if service is None:
        raise RuntimeError("项目上下文服务未注册")
    return service


def get_api_key_service(request: Request) -> ApiKeyService:
    """注入项目 API Key 服务；未配置密钥时拒绝签发、查看和验证。"""
    from fastapi import HTTPException, status

    service = get_container(request).api_key_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "api_key_unavailable", "message": "请先配置 API_KEY_SECRET_KEY"},
        )
    return service


def get_service_management(request: Request) -> ProjectServiceManagement:
    """注入服务目录、项目申请和额度管理用例。"""
    service = get_container(request).service_management
    if service is None:
        raise RuntimeError("服务管理用例未注册")
    return service


def get_llm_gateway_service(request: Request) -> LlmGatewayService:
    """注入 LLM 网关用例。"""
    service = get_container(request).llm_gateway_service
    if service is None:
        raise RuntimeError("LLM 网关服务未注册")
    return service


def get_llm_configuration_service(request: Request) -> LlmConfigurationService:
    """注入 LLM 供应商配置用例；未配置加密主密钥时禁用管理端点。"""
    from fastapi import HTTPException, status

    service = get_container(request).llm_configuration_service
    if service is None or get_container(request).settings.llm_provider_secret_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "llm_provider_config_unavailable",
                "message": "请先配置 LLM_PROVIDER_SECRET_KEY",
            },
        )
    return service


def get_embedding_configuration_service(request: Request) -> EmbeddingConfigurationService:
    from fastapi import HTTPException, status

    container = get_container(request)
    service = container.embedding_configuration_service
    if service is None or container.settings.llm_provider_secret_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "embedding_provider_config_unavailable",
                "message": "请先配置 LLM_PROVIDER_SECRET_KEY",
            },
        )
    return service


def get_embedding_gateway_service(request: Request) -> EmbeddingGatewayService:
    service = get_container(request).embedding_gateway_service
    if service is None:
        raise RuntimeError("Embedding gateway service unavailable")
    return service


def get_embedding_rag_service(request: Request) -> RagKnowledgeService:
    service = get_container(request).embedding_rag_service
    if service is None:
        raise RuntimeError("RAG knowledge service unavailable")
    return service


def get_asr_configuration_service(request: Request) -> AsrConfigurationService:
    """注入 ASR 连接配置；复用供应商密钥服务端加密主密钥。"""
    from fastapi import HTTPException, status

    service = get_container(request).asr_configuration_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "asr_provider_config_unavailable",
                "message": "请先配置 LLM_PROVIDER_SECRET_KEY",
            },
        )
    return service


def get_asr_gateway_service(request: Request) -> AsrGatewayService:
    """注入 ASR 请求生命周期服务。"""
    service = get_container(request).asr_gateway_service
    if service is None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "asr_unavailable", "message": "ASR 服务尚未完成供应商配置"},
        )
    return service


def get_request_log_service(request: Request) -> RequestLogService:
    """注入请求日志查询和保留服务单例。"""
    service = get_container(request).request_log_service
    if service is None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "request_logs_unavailable", "message": "日志服务暂不可用"},
        )
    return service


def get_request_recorder(request: Request) -> GatewayRequestRecorder:
    """向所有网关服务注入共享的请求生命周期记录器。"""
    recorder = get_container(request).request_recorder
    if recorder is None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "request_recorder_unavailable", "message": "日志服务暂不可用"},
        )
    return recorder


def get_technical_log_service(request: Request) -> TechnicalLogService:
    """注入管理员只读技术日志查询服务。"""
    return get_container(request).technical_log_service


def get_http_client(request: Request) -> httpx.AsyncClient:
    """注入生命周期内共享的 HTTP 客户端。"""
    client = get_container(request).http_client
    if client is None:
        raise RuntimeError("HTTP 客户端未注册")
    return client


bearer_scheme = HTTPBearer(auto_error=False)
_bearer_dependency = Depends(bearer_scheme)
_auth_service_dependency = Depends(get_auth_service)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = _bearer_dependency,
    auth_service: AuthService = _auth_service_dependency,
) -> User:
    """校验 Bearer 令牌并返回当前用户。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_token", "message": "需要访问令牌"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await auth_service.require_user(credentials.credentials)


_current_user_dependency = Depends(get_current_user)


async def get_current_admin(
    user: User = _current_user_dependency,
) -> User:
    """校验 Bearer 令牌并要求管理员角色。"""
    if user.role != "admin":
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "admin_required", "message": "需要管理员权限"},
        )
    return user
