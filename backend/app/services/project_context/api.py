"""项目上下文服务的 API Key 运行时接口。"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.application.api_keys import ApiKeyInvalidError, ApiKeyService, VerifiedApiKey
from app.application.projects import ProjectForbiddenError, ProjectNotFoundError, ProjectService
from app.container import (
    bearer_scheme,
    get_api_key_service,
    get_current_user,
    get_project_context_service,
    get_project_service,
    get_request_recorder,
    get_resource_service,
)
from app.infrastructure.db.models import User
from app.request_logging.application import GatewayRequestRecorder
from app.resources.application import ResourceService
from app.resources.domain import (
    ResourceConflictError,
    ResourceForbiddenError,
    ResourceNotFoundError,
    ResourceValidationError,
    ResourceVersionConflictError,
)
from app.services.project_context.application import (
    ContextConflictError,
    ContextForbiddenError,
    ContextNotFoundError,
    ProjectContextService,
)

router = APIRouter(prefix="/v1/context", tags=["Project Context Gateway"])
console_router = APIRouter(prefix="/api/admin/v1/context", tags=["Project Context Console"])
logger = logging.getLogger("gateway.service.project_context")
SERVICE_CODE = "project-context-v1"


class SessionMemoryWrite(BaseModel):
    value: Any = Field(
        description="兼容接口中的短期 JSON 值；新对话历史请使用 /messages。",
        examples=[{"step": "collecting_preferences", "language": "zh-CN"}],
    )


class SessionMessageInput(BaseModel):
    role: Literal["system", "user", "assistant", "tool"] = Field(
        description="OpenAI Chat Completions 消息角色。", examples=["user"]
    )
    content: str = Field(
        min_length=1,
        max_length=20_000,
        description="本条消息的文本正文。",
        examples=["我喜欢简洁的中文回答。"],
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="可选扩展字段，例如 tool_call_id；不参与网关解释。",
        examples=[{}],
    )


class SessionMessagesWrite(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "messages": [
                        {"role": "user", "content": "我喜欢简洁的中文回答。"},
                        {"role": "assistant", "content": "好的，我会尽量简洁地用中文回答。"},
                    ],
                }
            ]
        }
    }

    messages: list[SessionMessageInput] = Field(
        min_length=1,
        max_length=100,
        description="按数组顺序追加到会话的消息，单次最多 100 条。",
        examples=[
            [
                {"role": "user", "content": "我喜欢简洁的中文回答。"},
                {"role": "assistant", "content": "好的，我会尽量简洁地用中文回答。"},
            ]
        ],
    )


class LongTermMemoryWrite(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "content": "用户偏好简洁的中文回答。",
                    "tags": ["偏好", "表达方式"],
                    "metadata": {"source": "conversation", "confidence": 0.92},
                }
            ]
        }
    }

    content: str = Field(
        min_length=1,
        max_length=20_000,
        description="从对话中提炼、跨会话仍有价值的一条记忆文本。",
        examples=["用户偏好简洁的中文回答。"],
    )
    tags: list[str] = Field(default_factory=list, max_length=20, examples=[["偏好", "表达方式"]])
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="调用方自定义的来源、置信度等元数据。",
        examples=[{"source": "conversation", "confidence": 0.92}],
    )


class LongTermMemoryUpdate(LongTermMemoryWrite):
    expected_version: int = Field(ge=1)


class ProfileWrite(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "profile": {
                        "preferred_language": "zh-CN",
                        "interests": ["旅行", "摄影"],
                    }
                }
            ]
        }
    }

    profile: dict[str, Any] = Field(
        description="接入项目自行定义的用户画像 JSON 对象，网关不规定字段。",
        examples=[{"preferred_language": "zh-CN", "interests": ["旅行", "摄影"]}],
    )
    expected_version: int | None = Field(
        default=None,
        ge=1,
        description="更新已有画像时填写读取到的版本；首次创建时省略。",
        examples=[1],
    )


class TemplateWrite(BaseModel):
    """通过项目 API Key 创建项目提示词模板。"""

    category: Literal["system", "user", "assistant"] = Field(
        description="模板角色分类。", examples=["system"]
    )
    name: str = Field(min_length=1, max_length=200, description="项目内唯一的模板名称。")
    content: str = Field(max_length=100_000, description="模板正文，最多 100,000 字符。")


class TemplateUpdate(BaseModel):
    """携带当前版本更新模板名称与正文。"""

    expected_version: int = Field(ge=1, description="最近读取到的模板版本。")
    name: str = Field(min_length=1, max_length=200, description="项目内唯一的模板名称。")
    content: str = Field(max_length=100_000, description="模板正文，最多 100,000 字符。")


class TemplateResponse(BaseModel):
    """模板创建或读取时返回的项目资源信息。"""

    id: UUID
    project_id: UUID
    category: str
    name: str
    content: str
    version: int


class SessionMessageResponse(SessionMessageInput):
    id: UUID = Field(description="网关生成的消息 ID。")
    sequence: int = Field(description="数据库生成的全局递增顺序值；同一会话按此字段排序。")
    expires_at: datetime | None = Field(
        default=None, description="保留字段；当前数据不会自动过期，显式删除前为 null。"
    )


class SessionMessagesResponse(BaseModel):
    project_id: UUID = Field(description="由 Bearer 项目 API Key 确定的项目 ID。")
    external_user_id: str = Field(description="调用方系统中的用户标识。")
    session_id: str = Field(description="调用方系统中的会话标识。")
    messages: list[SessionMessageResponse] = Field(description="按会话顺序返回的最近消息。")


class ContextProjectResponse(BaseModel):
    id: UUID = Field(description="项目 ID。")
    name: str = Field(description="项目名称。")
    description: str | None = Field(description="项目描述。")
    visibility: Literal["public", "private"] = Field(description="项目可见性。")
    owner_id: UUID = Field(description="项目所有者 ID。")


class ContextProjectPageResponse(BaseModel):
    items: list[ContextProjectResponse] = Field(description="当前页可访问项目。")
    total: int = Field(description="全部匹配项目数。")
    offset: int = Field(description="本页偏移量。")
    limit: int = Field(description="本页条数上限。")


class ContextProfileResponse(BaseModel):
    profile: dict[str, Any] = Field(description="项目自定义 JSON 画像；不存在时为空对象。")
    version: int | None = Field(description="当前画像版本；未创建时为空。")


class ContextPermissionResponse(BaseModel):
    can_read_user_data: bool = Field(description="是否可查询短期记忆、长期记忆和画像。")
    can_edit: bool = Field(description="是否可修改模板或用户上下文数据。管理员始终为 false。")


def _error(error: RuntimeError | ValueError) -> HTTPException:
    if isinstance(error, ContextNotFoundError):
        return HTTPException(404, detail={"code": "context_not_found", "message": str(error)})
    if isinstance(error, ContextForbiddenError):
        return HTTPException(
            403, detail={"code": "context_service_unavailable", "message": str(error)}
        )
    if isinstance(error, ContextConflictError):
        return HTTPException(409, detail={"code": "version_conflict", "message": str(error)})
    return HTTPException(422, detail={"code": "invalid_context", "message": str(error)})


def _resource_error(error: RuntimeError) -> HTTPException:
    """将资源写入规则映射为稳定的上下文 API 错误。"""
    if isinstance(error, ResourceNotFoundError):
        return HTTPException(404, detail={"code": "resource_not_found", "message": str(error)})
    if isinstance(error, ResourceForbiddenError):
        return HTTPException(403, detail={"code": "resource_forbidden", "message": str(error)})
    if isinstance(error, ResourceConflictError):
        code = (
            "version_conflict"
            if isinstance(error, ResourceVersionConflictError)
            else "resource_name_conflict"
        )
        return HTTPException(409, detail={"code": code, "message": str(error)})
    if isinstance(error, ResourceValidationError):
        return HTTPException(422, detail={"code": "invalid_resource", "message": str(error)})
    raise error


async def _authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    key_service: ApiKeyService,
    recorder: GatewayRequestRecorder,
) -> tuple[VerifiedApiKey, str, float] | JSONResponse:
    request_id = request.state.trace_id
    started = time.perf_counter()
    if credentials is None or credentials.scheme.lower() != "bearer":
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code=SERVICE_CODE,
            latency_ms=0,
            error_code="missing_api_key",
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "invalid_api_key",
                    "message": "缺少 Bearer API Key",
                    "request_id": request_id,
                }
            },
        )
    try:
        key = await key_service.verify(credentials.credentials)
    except ApiKeyInvalidError:
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code=SERVICE_CODE,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error_code="invalid_api_key",
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "invalid_api_key",
                    "message": "API Key 无效、已撤销或已过期",
                    "request_id": request_id,
                }
            },
        )
    await recorder.start(
        request_id=request_id,
        project_id=key.project_id,
        api_key_id=key.id,
        service_code=SERVICE_CODE,
        description="项目上下文服务",
        actor_user_id=key.user_id,
        actor_username=key.username,
    )
    logger.info(
        "audit_stage stage=authentication result=allowed request_id=%s project_id=%s "
        "api_key_id=%s actor_user_id=%s actor_username=%s",
        request_id,
        key.project_id,
        key.id,
        key.user_id,
        key.username,
    )
    request.state.project_id = key.project_id
    await recorder.record_stage(request_id, "authentication", "allowed")
    return key, request_id, started


async def _finish(
    recorder: GatewayRequestRecorder,
    request_id: str,
    started: float,
    outcome: Literal["denied", "succeeded", "failed"],
    description: str,
    error_code: str | None = None,
) -> None:
    stage_outcome: Literal["denied", "succeeded", "failed"] = (
        "denied" if outcome == "denied" else outcome
    )
    await recorder.record_stage(
        request_id,
        "service_call",
        stage_outcome,
        error_code=error_code,
    )
    await recorder.finish(
        request_id,
        outcome,
        latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        description=description,
        error_code=error_code,
        error_message=description if error_code else None,
    )


async def _run(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    key_service: ApiKeyService,
    context: ProjectContextService,
    recorder: GatewayRequestRecorder,
    operation: Callable[[Any], Awaitable[Any]],
) -> Any:
    auth = await _authenticate(request, credentials, key_service, recorder)
    if isinstance(auth, JSONResponse):
        return auth
    key, request_id, started = auth
    try:
        result = await operation(key.project_id)
    except RuntimeError as error:
        await _finish(
            recorder,
            request_id,
            started,
            "denied" if isinstance(error, ContextForbiddenError) else "failed",
            str(error),
            "context_service_unavailable"
            if isinstance(error, ContextForbiddenError)
            else "context_error",
        )
        raise _error(error) from error
    except ValueError as error:
        await _finish(recorder, request_id, started, "failed", str(error), "invalid_context")
        raise _error(error) from error
    await _finish(recorder, request_id, started, "succeeded", "上下文服务调用成功")
    return result


@router.post(
    "/templates",
    response_model=TemplateResponse,
    status_code=201,
    summary="通过项目 API Key 创建提示词模板",
)
async def create_template(
    payload: TemplateWrite,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> TemplateResponse | JSONResponse:
    """使用 API Key 确定项目与创建者；仅 owner/editor 创建的 Key 可写入模板。"""
    auth = await _authenticate(request, credentials, key_service, recorder)
    if isinstance(auth, JSONResponse):
        return auth
    key, request_id, started = auth
    actor_user_id = key.user_id or key.owner_id
    try:
        item = await resource_service.create(
            key.project_id,
            actor_user_id,
            "template",
            payload.category,
            payload.name,
            payload.content,
        )
    except RuntimeError as error:
        await _finish(
            recorder,
            request_id,
            started,
            "denied" if isinstance(error, ResourceForbiddenError) else "failed",
            str(error),
            "resource_forbidden"
            if isinstance(error, ResourceForbiddenError)
            else "template_create_failed",
        )
        raise _resource_error(error) from error
    await _finish(recorder, request_id, started, "succeeded", "提示词模板创建成功")
    return TemplateResponse(
        id=item.id,
        project_id=item.project_id,
        category=item.category,
        name=item.name,
        content=item.content,
        version=item.version,
    )


@router.patch(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    summary="通过项目 API Key 修改提示词模板",
)
async def update_template(
    template_id: UUID,
    payload: TemplateUpdate,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> TemplateResponse | JSONResponse:
    """使用 Key 所属成员身份更新模板；版本不匹配时返回 409。"""
    auth = await _authenticate(request, credentials, key_service, recorder)
    if isinstance(auth, JSONResponse):
        return auth
    key, request_id, started = auth
    actor_user_id = key.user_id or key.owner_id
    try:
        existing = await resource_service.get(key.project_id, template_id, actor_user_id, False)
        if existing.resource_type != "template":
            raise ResourceNotFoundError("模板不存在")
        item = await resource_service.update(
            key.project_id,
            template_id,
            actor_user_id,
            False,
            payload.expected_version,
            payload.name,
            payload.content,
        )
    except RuntimeError as error:
        await _finish(
            recorder,
            request_id,
            started,
            "denied" if isinstance(error, ResourceForbiddenError) else "failed",
            str(error),
            "resource_forbidden"
            if isinstance(error, ResourceForbiddenError)
            else "template_update_failed",
        )
        raise _resource_error(error) from error
    await _finish(recorder, request_id, started, "succeeded", "提示词模板更新成功")
    return TemplateResponse(
        id=item.id,
        project_id=item.project_id,
        category=item.category,
        name=item.name,
        content=item.content,
        version=item.version,
    )


@router.delete(
    "/templates/{template_id}",
    status_code=204,
    response_model=None,
    summary="通过项目 API Key 删除提示词模板",
)
async def delete_template(
    template_id: UUID,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    resource_service: Annotated[ResourceService, Depends(get_resource_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
    expected_version: Annotated[int, Query(ge=1, description="最近读取到的模板版本。")],
) -> Response | JSONResponse:
    """使用 Key 所属成员身份软删除模板，防止误删其他项目资源。"""
    auth = await _authenticate(request, credentials, key_service, recorder)
    if isinstance(auth, JSONResponse):
        return auth
    key, request_id, started = auth
    actor_user_id = key.user_id or key.owner_id
    try:
        item = await resource_service.get(key.project_id, template_id, actor_user_id, False)
        if item.resource_type != "template":
            raise ResourceNotFoundError("模板不存在")
        await resource_service.soft_delete(
            key.project_id, template_id, actor_user_id, False, expected_version
        )
    except RuntimeError as error:
        await _finish(
            recorder,
            request_id,
            started,
            "denied" if isinstance(error, ResourceForbiddenError) else "failed",
            str(error),
            "resource_forbidden"
            if isinstance(error, ResourceForbiddenError)
            else "template_delete_failed",
        )
        raise _resource_error(error) from error
    await _finish(recorder, request_id, started, "succeeded", "提示词模板删除成功")
    return Response(status_code=204)


@router.get("/templates", summary="列出项目提示词模板")
async def list_templates(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    result = await _run(
        request, credentials, key_service, context, recorder, context.list_templates
    )
    if isinstance(result, JSONResponse):
        return result
    return [
        {
            "id": str(item.id),
            "category": item.category,
            "name": item.name,
            "content": item.content,
            "version": item.version,
        }
        for item in result
    ]


@router.get("/templates/by-id/{template_id}", summary="按 ID 读取项目提示词模板")
async def get_template_by_id(
    template_id: UUID,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    """使用项目 API Key 确定项目，再按模板稳定 ID 读取。"""
    item = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.get_template_by_id(project_id, template_id),
    )
    if isinstance(item, JSONResponse):
        return item
    return {
        "id": str(item.id),
        "category": item.category,
        "name": item.name,
        "content": item.content,
        "version": item.version,
    }


@router.get("/templates/{category}/{name}", summary="读取项目提示词模板")
async def get_template(
    category: str,
    name: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    item = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.get_template(project_id, category, name),
    )
    if isinstance(item, JSONResponse):
        return item
    return {
        "id": str(item.id),
        "category": item.category,
        "name": item.name,
        "content": item.content,
        "version": item.version,
    }


@router.get("/users/{external_user_id}/sessions/{session_id}/memories", summary="列出会话短期记忆")
async def list_session_memories(
    external_user_id: str,
    session_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    items = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.list_session_memories(project_id, external_user_id, session_id),
    )
    if isinstance(items, JSONResponse):
        return items
    return [
        {
            "key": item.memory_key,
            "value": json.loads(item.value_json),
            "version": item.version,
            "expires_at": item.expires_at,
        }
        for item in items
    ]


def _serialize_messages(
    project_id: UUID, external_user_id: str, session_id: str, items: list[Any]
) -> SessionMessagesResponse:
    """将 ORM 消息记录转换为稳定的运行时响应契约。"""
    return SessionMessagesResponse(
        project_id=project_id,
        external_user_id=external_user_id,
        session_id=session_id,
        messages=[
            SessionMessageResponse(
                id=item.id,
                sequence=item.sequence,
                role=item.role,
                content=item.content,
                metadata=item.metadata_json,
                expires_at=item.expires_at,
            )
            for item in items
        ],
    )


@router.get(
    "/users/{external_user_id}/sessions/{session_id}/messages",
    response_model=SessionMessagesResponse,
    summary="读取会话短期消息",
    description=(
        "返回该项目用户会话最近的消息，按追加顺序排列；数据保留到显式删除，"
        "项目由 API Key 确定。"
    ),
)
async def list_session_messages(
    external_user_id: str,
    session_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Any:
    result = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.list_session_messages(
            project_id, external_user_id, session_id, limit
        ),
    )
    if isinstance(result, JSONResponse):
        return result
    return _serialize_messages(request.state.project_id, external_user_id, session_id, result)


@router.post(
    "/users/{external_user_id}/sessions/{session_id}/messages",
    response_model=SessionMessagesResponse,
    status_code=201,
    summary="向会话追加短期消息",
    description="按请求数组顺序追加 1 到 100 条消息；网关不会自动删除，需调用 DELETE 接口清除。",
)
async def append_session_messages(
    external_user_id: str,
    session_id: str,
    payload: SessionMessagesWrite,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    result = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.append_session_messages(
            project_id,
            external_user_id,
            session_id,
            [message.model_dump() for message in payload.messages],
        ),
    )
    if isinstance(result, JSONResponse):
        return result
    return _serialize_messages(request.state.project_id, external_user_id, session_id, result)


@router.delete(
    "/users/{external_user_id}/sessions/{session_id}/messages",
    status_code=204,
    summary="清除会话短期消息",
    description="删除指定项目用户会话的全部消息；适用于会话结束或按用户请求清除数据。",
)
async def delete_session_messages(
    external_user_id: str,
    session_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Response:
    result = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.delete_session_messages(
            project_id, external_user_id, session_id
        ),
    )
    if isinstance(result, JSONResponse):
        return result
    return Response(status_code=204)


@router.put(
    "/users/{external_user_id}/sessions/{session_id}/memories/{memory_key}",
    summary="创建或更新短期记忆",
)
async def put_session_memory(
    external_user_id: str,
    session_id: str,
    memory_key: str,
    payload: SessionMemoryWrite,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    item = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.put_session_memory(
            project_id, external_user_id, session_id, memory_key, payload.value
        ),
    )
    if isinstance(item, JSONResponse):
        return item
    return {
        "key": item.memory_key,
        "value": payload.value,
        "version": item.version,
        "expires_at": item.expires_at,
    }


@router.delete(
    "/users/{external_user_id}/sessions/{session_id}/memories/{memory_key}",
    status_code=204,
    summary="删除短期记忆",
)
async def delete_session_memory(
    external_user_id: str,
    session_id: str,
    memory_key: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Response:
    result = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.delete_session_memory(
            project_id, external_user_id, session_id, memory_key
        ),
    )
    if isinstance(result, JSONResponse):
        return result
    return Response(status_code=204)


@router.get("/users/{external_user_id}/memories", summary="分页读取长期记忆")
async def list_long_memories(
    external_user_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Any:
    result = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.list_long_term_memories(
            project_id, external_user_id, (page - 1) * page_size, page_size
        ),
    )
    if isinstance(result, JSONResponse):
        return result
    items, total = result
    return {
        "items": [
            {
                "id": str(item.id),
                "content": item.content,
                "tags": item.tags,
                "metadata": item.metadata_json,
                "version": item.version,
            }
            for item in items
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.post("/users/{external_user_id}/memories", status_code=201, summary="创建长期记忆")
async def create_long_memory(
    external_user_id: str,
    payload: LongTermMemoryWrite,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    item = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.create_long_term_memory(
            project_id, external_user_id, payload.content, payload.tags, payload.metadata
        ),
    )
    if isinstance(item, JSONResponse):
        return item
    return {
        "id": str(item.id),
        "content": item.content,
        "tags": item.tags,
        "metadata": item.metadata_json,
        "version": item.version,
    }


@router.patch("/users/{external_user_id}/memories/{memory_id}", summary="更新长期记忆")
async def update_long_memory(
    external_user_id: str,
    memory_id: str,
    payload: LongTermMemoryUpdate,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    from uuid import UUID

    item = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.update_long_term_memory(
            project_id,
            external_user_id,
            UUID(memory_id),
            payload.expected_version,
            payload.content,
            payload.tags,
            payload.metadata,
        ),
    )
    if isinstance(item, JSONResponse):
        return item
    return {
        "id": str(item.id),
        "content": item.content,
        "tags": item.tags,
        "metadata": item.metadata_json,
        "version": item.version,
    }


@router.delete(
    "/users/{external_user_id}/memories/{memory_id}", status_code=204, summary="删除长期记忆"
)
async def delete_long_memory(
    external_user_id: str,
    memory_id: str,
    expected_version: Annotated[int, Query(ge=1)],
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Response:
    from uuid import UUID

    result = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.delete_long_term_memory(
            project_id, external_user_id, UUID(memory_id), expected_version
        ),
    )
    if isinstance(result, JSONResponse):
        return result
    return Response(status_code=204)


@router.get("/users/{external_user_id}/profile", summary="读取用户画像")
async def get_profile(
    external_user_id: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    item = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.get_profile(project_id, external_user_id),
    )
    if isinstance(item, JSONResponse):
        return item
    return (
        {"profile": item.profile, "version": item.version}
        if item
        else {"profile": {}, "version": None}
    )


@router.put("/users/{external_user_id}/profile", summary="创建或替换用户画像")
async def put_profile(
    external_user_id: str,
    payload: ProfileWrite,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> Any:
    item = await _run(
        request,
        credentials,
        key_service,
        context,
        recorder,
        lambda project_id: context.put_profile(
            project_id, external_user_id, payload.profile, payload.expected_version
        ),
    )
    if isinstance(item, JSONResponse):
        return item
    return {"profile": item.profile, "version": item.version, "updated_at": item.updated_at}


async def _check_console_read(
    project_id: UUID,
    current_user: User,
    project_service: ProjectService,
) -> None:
    """控制台读取仅允许项目 owner/editor 和全局管理员只读。"""
    try:
        await project_service.assert_context_access(project_id, current_user, write=False)
    except ProjectNotFoundError as error:
        raise HTTPException(
            404, detail={"code": "project_not_found", "message": str(error)}
        ) from error
    except ProjectForbiddenError as error:
        raise HTTPException(
            403, detail={"code": "context_forbidden", "message": str(error)}
        ) from error


@console_router.get(
    "/projects", response_model=ContextProjectPageResponse, summary="列出可管理的上下文项目"
)
async def list_context_projects(
    current_user: Annotated[User, Depends(get_current_user)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ContextProjectPageResponse:
    """返回已开通上下文服务且用户可查看的运行中项目；管理员看到全量只读项目。"""
    items, total = await context.list_projects_for_user(current_user, offset, limit)
    return ContextProjectPageResponse(
        items=[
            ContextProjectResponse(
                id=item.id,
                name=item.name,
                description=item.description,
                visibility=item.visibility,
                owner_id=item.owner_id,
            )
            for item in items
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@console_router.get(
    "/projects/{project_id}/users/{external_user_id}/profile",
    response_model=ContextProfileResponse,
    summary="控制台读取用户画像",
)
async def console_get_profile(
    project_id: UUID,
    external_user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
) -> ContextProfileResponse:
    """项目 owner/editor 可读；管理员可跨项目只读，调用方无需获得项目 API Key。"""
    await _check_console_read(project_id, current_user, project_service)
    item = await context.get_profile(project_id, external_user_id)
    return ContextProfileResponse(
        profile=item.profile if item else {}, version=item.version if item else None
    )


@console_router.get(
    "/projects/{project_id}/permissions",
    response_model=ContextPermissionResponse,
    summary="查询上下文控制台权限",
)
async def get_context_permissions(
    project_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> ContextPermissionResponse:
    """返回项目上下文页面的查询和编辑权限，不返回或验证项目 API Key。"""
    try:
        can_read, can_edit = await project_service.context_permissions(project_id, current_user)
    except ProjectNotFoundError as error:
        raise HTTPException(
            404, detail={"code": "project_not_found", "message": str(error)}
        ) from error
    except ProjectForbiddenError as error:
        raise HTTPException(
            403, detail={"code": "context_forbidden", "message": str(error)}
        ) from error
    return ContextPermissionResponse(can_read_user_data=can_read, can_edit=can_edit)


@console_router.get(
    "/projects/{project_id}/users/{external_user_id}/sessions/{session_id}/messages",
    response_model=SessionMessagesResponse,
    summary="控制台读取会话短期消息",
)
async def console_list_session_messages(
    project_id: UUID,
    external_user_id: str,
    session_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SessionMessagesResponse:
    """项目 owner/editor 可读；管理员只读查看，不需要暴露项目 API Key。"""
    await _check_console_read(project_id, current_user, project_service)
    items = await context.list_session_messages(project_id, external_user_id, session_id, limit)
    return _serialize_messages(project_id, external_user_id, session_id, items)


@console_router.get(
    "/projects/{project_id}/users/{external_user_id}/memories",
    summary="控制台分页读取长期记忆",
)
async def console_list_long_memories(
    project_id: UUID,
    external_user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    context: Annotated[ProjectContextService, Depends(get_project_context_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """项目 owner/editor 可读；管理员只读查看，不需要暴露项目 API Key。"""
    await _check_console_read(project_id, current_user, project_service)
    items, total = await context.list_long_term_memories(
        project_id, external_user_id, (page - 1) * page_size, page_size
    )
    return {
        "items": [
            {
                "id": str(item.id),
                "content": item.content,
                "tags": item.tags,
                "metadata": item.metadata_json,
                "version": item.version,
            }
            for item in items
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }
