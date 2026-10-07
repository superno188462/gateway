"""登录态项目 RAG 控制台接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.application.api_keys import VerifiedApiKey
from app.application.projects import ProjectForbiddenError, ProjectNotFoundError, ProjectService
from app.container import get_current_user, get_embedding_rag_service, get_project_service
from app.infrastructure.db.models import User
from app.services.embedding.rag import RagError, RagKnowledgeService
from app.services.embedding.rag_api import (
    DocumentCreateRequest,
    DocumentResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    SearchRequest,
    VectorResponse,
    VectorUpdateRequest,
)

router = APIRouter(prefix="/api/v1/rag/projects/{project_id}", tags=["RAG Console"])


class PermissionsResponse(BaseModel):
    can_read: bool
    can_edit: bool


class SearchConsoleResponse(BaseModel):
    knowledge_base_id: UUID
    model: str
    results: list[dict[str, object]]


async def _actor(
    project_id: UUID,
    user: User,
    projects: ProjectService,
    *,
    write: bool,
) -> VerifiedApiKey:
    try:
        project = await projects.get_for_user(project_id, user)
        members = await projects.list_members(project_id, user)
    except ProjectNotFoundError as error:
        raise HTTPException(
            404, detail={"code": "project_not_found", "message": str(error)}
        ) from error
    except ProjectForbiddenError as error:
        raise HTTPException(
            403, detail={"code": "project_forbidden", "message": str(error)}
        ) from error
    role = next((member.role for member in members if member.user_id == user.id), None)
    can_edit = role in {"owner", "editor"}
    can_read = role in {"owner", "editor", "viewer"} or user.role == "admin"
    if not can_read or (write and not can_edit):
        raise HTTPException(
            403, detail={"code": "rag_forbidden", "message": "项目成员无权执行此操作"}
        )
    return VerifiedApiKey(
        id=None,
        project_id=project_id,
        owner_id=project.owner_id,
        name="console-session",
        user_id=user.id,
        username=user.username,
    )


def _map_error(error: RagError) -> HTTPException:
    return HTTPException(error.status_code, detail={"code": error.code, "message": str(error)})


async def _log_operation_failure(
    service: RagKnowledgeService,
    actor: VerifiedApiKey,
    request: Request,
    operation: str,
    error: Exception,
) -> None:
    await service.record_operation_failure(actor, request.state.trace_id, operation, error)


@router.get("/permissions", response_model=PermissionsResponse)
async def permissions(
    project_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
) -> PermissionsResponse:
    await _actor(project_id, user, projects, write=False)
    members = await projects.list_members(project_id, user)
    role = next((member.role for member in members if member.user_id == user.id), None)
    return PermissionsResponse(can_read=True, can_edit=role in {"owner", "editor"})


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    project_id: UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> list[KnowledgeBaseResponse]:
    actor = await _actor(project_id, user, projects, write=False)
    try:
        items = await service.list_knowledge_bases(project_id)
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.knowledge_base.list", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.knowledge_base.list", error)
        raise
    return [KnowledgeBaseResponse.from_entity(item) for item in items]


@router.post("/knowledge-bases", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(
    project_id: UUID,
    payload: KnowledgeBaseCreateRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> KnowledgeBaseResponse:
    actor = await _actor(project_id, user, projects, write=True)
    try:
        item = await service.create_knowledge_base(
            actor,
            name=payload.name,
            description=payload.description,
            model=payload.model,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            trace_id=request.state.trace_id,
        )
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.knowledge_base.create", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.knowledge_base.create", error)
        raise
    return KnowledgeBaseResponse.from_entity(item)


@router.delete("/knowledge-bases/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    project_id: UUID,
    knowledge_base_id: UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> None:
    actor = await _actor(project_id, user, projects, write=True)
    try:
        await service.delete_knowledge_base(actor, knowledge_base_id, request.state.trace_id)
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.knowledge_base.delete", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.knowledge_base.delete", error)
        raise


@router.get("/knowledge-bases/{knowledge_base_id}/documents", response_model=list[DocumentResponse])
async def list_documents(
    project_id: UUID,
    knowledge_base_id: UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> list[DocumentResponse]:
    actor = await _actor(project_id, user, projects, write=False)
    try:
        items = await service.list_documents(project_id, knowledge_base_id)
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.document.list", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.document.list", error)
        raise
    return [DocumentResponse.from_info(item) for item in items]


@router.get(
    "/knowledge-bases/{knowledge_base_id}/vectors",
    response_model=list[VectorResponse],
    summary="列出集合内的独立向量记录",
    description="按数据库中的向量切片逐条返回，包含来源标题、正文和 Metadata。",
)
async def list_vectors(
    project_id: UUID,
    knowledge_base_id: UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> list[VectorResponse]:
    actor = await _actor(project_id, user, projects, write=False)
    try:
        items = await service.list_vectors(project_id, knowledge_base_id)
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.vector.list", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.vector.list", error)
        raise
    return [VectorResponse.from_info(item) for item in items]


@router.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=DocumentResponse,
    status_code=201,
)
async def create_document(
    project_id: UUID,
    knowledge_base_id: UUID,
    payload: DocumentCreateRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> DocumentResponse:
    actor = await _actor(project_id, user, projects, write=True)
    try:
        item = await service.create_document(
            actor,
            knowledge_base_id,
            title=payload.title,
            content=payload.content,
            external_id=payload.external_id,
            metadata=payload.metadata,
            trace_id=request.state.trace_id,
        )
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.document.create", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.document.create", error)
        raise
    return DocumentResponse.from_info(item)


@router.delete(
    "/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    project_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> None:
    actor = await _actor(project_id, user, projects, write=True)
    try:
        await service.delete_document(actor, knowledge_base_id, document_id, request.state.trace_id)
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.document.delete", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.document.delete", error)
        raise


@router.delete(
    "/knowledge-bases/{knowledge_base_id}/vectors/{vector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一条向量记录",
    description="仅删除指定切片；若它是关联文档的最后一片，同时清理空文档。",
)
async def delete_vector(
    project_id: UUID,
    knowledge_base_id: UUID,
    vector_id: UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> None:
    actor = await _actor(project_id, user, projects, write=True)
    try:
        await service.delete_vector(actor, knowledge_base_id, vector_id, request.state.trace_id)
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.vector.delete", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.vector.delete", error)
        raise


@router.patch(
    "/knowledge-bases/{knowledge_base_id}/vectors/{vector_id}",
    response_model=VectorResponse,
    summary="修改一条向量记录",
    description="更新文本或 Metadata；文本变化时重新生成向量并计入 Embedding 用量。",
)
async def update_vector(
    project_id: UUID,
    knowledge_base_id: UUID,
    vector_id: UUID,
    payload: VectorUpdateRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> VectorResponse:
    actor = await _actor(project_id, user, projects, write=True)
    try:
        result = await service.update_vector(
            actor,
            knowledge_base_id,
            vector_id,
            content=payload.content,
            metadata=payload.metadata,
            trace_id=request.state.trace_id,
        )
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.vector.update", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.vector.update", error)
        raise
    return VectorResponse.from_info(result)


@router.post("/knowledge-bases/{knowledge_base_id}/search", response_model=SearchConsoleResponse)
async def search(
    project_id: UUID,
    knowledge_base_id: UUID,
    payload: SearchRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> SearchConsoleResponse:
    actor = await _actor(project_id, user, projects, write=False)
    try:
        results = await service.search(
            actor, knowledge_base_id, payload.query, payload.top_k, request.state.trace_id
        )
        base = await service.get_knowledge_base(project_id, knowledge_base_id)
    except RagError as error:
        await _log_operation_failure(service, actor, request, "rag.search", error)
        raise _map_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, actor, request, "rag.search", error)
        raise
    return SearchConsoleResponse(
        knowledge_base_id=knowledge_base_id, model=base.embedding_model, results=results
    )
