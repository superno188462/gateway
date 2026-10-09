"""通过项目 API Key 管理项目 RAG 知识库并执行相似度检索。"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.api_keys import ApiKeyInvalidError, ApiKeyService, VerifiedApiKey
from app.container import (
    bearer_scheme,
    get_api_key_service,
    get_embedding_rag_service,
    get_request_recorder,
)
from app.request_logging.application import GatewayRequestRecorder
from app.services.embedding.rag import RagDocumentInfo, RagError, RagKnowledgeService, RagVectorInfo

router = APIRouter(prefix="/v1/rag", tags=["RAG Knowledge Base"])


async def verify_project_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    key_service: Annotated[ApiKeyService, Depends(get_api_key_service)],
    recorder: Annotated[GatewayRequestRecorder, Depends(get_request_recorder)],
) -> VerifiedApiKey:
    request_id = request.state.trace_id
    if credentials is None or credentials.scheme.lower() != "bearer":
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code="rag-v1",
            latency_ms=0,
            error_code="missing_api_key",
        )
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_api_key", "message": "缺少 Bearer API Key"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await key_service.verify(credentials.credentials)
    except ApiKeyInvalidError as error:
        await recorder.record_auth_rejection(
            request_id=request_id,
            service_code="rag-v1",
            latency_ms=0,
            error_code="invalid_api_key",
        )
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_api_key", "message": "API Key 无效、已撤销或已过期"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def get_rag_knowledge_service(request: Request) -> RagKnowledgeService:
    return get_embedding_rag_service(request)


def _raise_rag_error(error: RagError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error)},
    )


async def _log_operation_failure(
    service: RagKnowledgeService,
    key: VerifiedApiKey,
    request: Request,
    operation: str,
    error: Exception,
) -> None:
    await service.record_operation_failure(key, request.state.trace_id, operation, error)


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    chunk_size: int = Field(default=1000, ge=100, le=6000)
    chunk_overlap: int = Field(default=120, ge=0, le=2000)

    @model_validator(mode="after")
    def validate_overlap(self) -> "KnowledgeBaseCreateRequest":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class KnowledgeBaseResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    model: str
    chunk_size: int
    chunk_overlap: int
    vector_dimensions: int | None
    created_at: str

    @classmethod
    def from_entity(cls, item: Any) -> "KnowledgeBaseResponse":
        return cls(
            id=item.id,
            name=item.name,
            description=item.description,
            model=item.embedding_model,
            chunk_size=item.chunk_size,
            chunk_overlap=item.chunk_overlap,
            vector_dimensions=item.vector_dimensions,
            created_at=item.created_at.isoformat(),
        )


class KnowledgeBaseSettingsUpdateRequest(BaseModel):
    """只允许更新单条记录长度上限与模型路由前缀，模型名不可修改。"""

    model_config = ConfigDict(extra="forbid")

    route_prefix: str | None = Field(
        description="新的路由前缀；null 或空字符串表示自动路由。模型名保持不变。"
    )
    chunk_size: int = Field(
        ge=100,
        le=6000,
        description="单条已切分记录的最大字符数，仅限制后续写入；不会改写已有记录。",
    )


class DocumentCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    external_id: str | None = Field(default=None, max_length=200)
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content 不能为空")
        return value


class DocumentResponse(BaseModel):
    id: UUID
    external_id: str | None
    title: str
    chunks: int
    created_at: str

    @classmethod
    def from_info(cls, item: RagDocumentInfo) -> "DocumentResponse":
        return cls(
            id=item.id,
            external_id=item.external_id,
            title=item.title,
            chunks=item.chunks,
            created_at=item.created_at.isoformat(),
        )


class VectorResponse(BaseModel):
    """单条向量切片的管理视图，不包含高维 embedding 数组。"""

    id: UUID = Field(description="该条切片向量的稳定 ID，可用于查看、修改或删除。")
    document_id: UUID = Field(description="来源文档容器 ID，用于追溯原始内容。")
    external_id: str | None = Field(description="调用方提供的来源 ID；同一旧文档的切片可能相同。")
    title: str = Field(description="来源记录标题。")
    content: str = Field(description="该条向量对应的文本切片，不包含向量数组。")
    modality: str = Field(description="记录模态：text、image 或 video。")
    metadata: dict[str, object] = Field(description="该切片的 metadata 对象。")
    sequence: int = Field(description="该切片在来源文档中的从零开始序号。")
    created_by_user_id: UUID = Field(description="创建来源记录的用户 ID。")
    created_at: str = Field(description="切片创建时间，ISO 8601 格式。")
    updated_at: str = Field(description="该条切片最后修改时间，ISO 8601 格式。")

    @classmethod
    def from_info(cls, item: RagVectorInfo) -> "VectorResponse":
        return cls(
            id=item.id,
            document_id=item.document_id,
            external_id=item.external_id,
            title=item.title,
            content=item.content,
            modality=item.modality,
            metadata=item.metadata,
            sequence=item.sequence,
            created_by_user_id=item.created_by_user_id,
            created_at=item.created_at.isoformat(),
            updated_at=item.updated_at.isoformat(),
        )


class VectorUpdateRequest(BaseModel):
    """可编辑字段；正文变化会重新计算 Embedding 并计入用量。"""

    content: str = Field(min_length=1, max_length=6000, description="单条切片文本。")
    metadata: dict[str, object] = Field(description="替换该条记录的 Metadata JSON 对象。")

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content 不能为空")
        return value


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query 不能为空")
        return value


@router.post("/knowledge-bases", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(
    payload: KnowledgeBaseCreateRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> KnowledgeBaseResponse:
    try:
        result = await service.create_knowledge_base(
            key,
            name=payload.name,
            description=payload.description,
            model=payload.model,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            trace_id=request.state.trace_id,
        )
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.knowledge_base.create", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.knowledge_base.create", error)
        raise
    return KnowledgeBaseResponse.from_entity(result)


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> list[KnowledgeBaseResponse]:
    try:
        items = await service.list_knowledge_bases(key.project_id)
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.knowledge_base.list", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.knowledge_base.list", error)
        raise
    return [KnowledgeBaseResponse.from_entity(item) for item in items]


@router.delete("/knowledge-bases/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    knowledge_base_id: UUID,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> None:
    try:
        await service.delete_knowledge_base(key, knowledge_base_id, request.state.trace_id)
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.knowledge_base.delete", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.knowledge_base.delete", error)
        raise


@router.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_document(
    knowledge_base_id: UUID,
    payload: DocumentCreateRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> DocumentResponse:
    try:
        result = await service.create_document(
            key,
            knowledge_base_id,
            title=payload.title,
            content=payload.content,
            external_id=payload.external_id,
            metadata=payload.metadata,
            trace_id=request.state.trace_id,
        )
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.document.create", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.document.create", error)
        raise
    return DocumentResponse.from_info(result)


@router.get("/knowledge-bases/{knowledge_base_id}/documents", response_model=list[DocumentResponse])
async def list_documents(
    knowledge_base_id: UUID,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> list[DocumentResponse]:
    try:
        items = await service.list_documents(key.project_id, knowledge_base_id)
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.document.list", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.document.list", error)
        raise
    return [DocumentResponse.from_info(item) for item in items]


@router.get(
    "/knowledge-bases/{knowledge_base_id}/vectors",
    response_model=list[VectorResponse],
    summary="列出集合内的独立向量记录",
    description="兼容管理页面逐条查看历史切片；既有文档列表接口保持原有聚合响应。",
)
async def list_vectors(
    knowledge_base_id: UUID,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> list[VectorResponse]:
    try:
        items = await service.list_vectors(key.project_id, knowledge_base_id)
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.vector.list", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.vector.list", error)
        raise
    return [VectorResponse.from_info(item) for item in items]


@router.delete(
    "/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    knowledge_base_id: UUID,
    document_id: UUID,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> None:
    try:
        await service.delete_document(key, knowledge_base_id, document_id, request.state.trace_id)
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.document.delete", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.document.delete", error)
        raise


@router.delete(
    "/knowledge-bases/{knowledge_base_id}/vectors/{vector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一条向量记录",
    description="只删除指定切片；若它是关联文档的最后一片，则同时清理空文档。",
)
async def delete_vector(
    knowledge_base_id: UUID,
    vector_id: UUID,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> None:
    try:
        await service.delete_vector(key, knowledge_base_id, vector_id, request.state.trace_id)
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.vector.delete", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.vector.delete", error)
        raise


@router.patch(
    "/knowledge-bases/{knowledge_base_id}/vectors/{vector_id}",
    response_model=VectorResponse,
    summary="修改一条向量记录",
    description="更新文本或 Metadata；文本变化时重新生成向量并计入 Embedding 用量。",
)
async def update_vector(
    knowledge_base_id: UUID,
    vector_id: UUID,
    payload: VectorUpdateRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> VectorResponse:
    try:
        result = await service.update_vector(
            key,
            knowledge_base_id,
            vector_id,
            content=payload.content,
            metadata=payload.metadata,
            trace_id=request.state.trace_id,
        )
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.vector.update", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.vector.update", error)
        raise
    return VectorResponse.from_info(result)


@router.post("/knowledge-bases/{knowledge_base_id}/search")
async def search_knowledge_base(
    knowledge_base_id: UUID,
    payload: SearchRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_rag_knowledge_service)],
) -> dict[str, object]:
    try:
        results = await service.search(
            key,
            knowledge_base_id,
            payload.query,
            payload.top_k,
            request.state.trace_id,
        )
    except RagError as error:
        await _log_operation_failure(service, key, request, "rag.search", error)
        raise _raise_rag_error(error) from error
    except Exception as error:
        await _log_operation_failure(service, key, request, "rag.search", error)
        raise
    return {
        "knowledge_base_id": str(knowledge_base_id),
        "model": (
            await service.get_knowledge_base(key.project_id, knowledge_base_id)
        ).embedding_model,
        "results": results,
    }
