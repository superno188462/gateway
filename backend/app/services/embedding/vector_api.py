"""通用项目向量数据库 API。

该接口以 collection 名称屏蔽内部知识库 UUID，适合不使用 LangChain 的 Agent
项目通过一个可配置的 HTTP URL 接入。向量由网关配置的 Embedding 服务生成。
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from app.application.api_keys import VerifiedApiKey
from app.container import get_embedding_rag_service
from app.services.embedding.rag import RagError, RagKnowledgeService
from app.services.embedding.rag_api import verify_project_key

router = APIRouter(prefix="/v1/vector-stores", tags=["Vector Database"])


class CollectionCreateRequest(BaseModel):
    """创建集合；`chunk_size` 同时作为单条已切分记录的字符上限。"""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    chunk_size: int = Field(default=1000, ge=100, le=6000)
    chunk_overlap: int = Field(default=120, ge=0, le=2000)


class CollectionResponse(BaseModel):
    id: UUID
    collection: str
    description: str | None
    model: str
    vector_dimensions: int | None
    chunk_size: int
    chunk_overlap: int
    created_at: str


class VectorDocument(BaseModel):
    """一条调用方已完成切分的向量记录，不代表完整原始文档。"""

    id: str = Field(min_length=1, max_length=200, description="调用方提供的稳定记录 ID。")
    content: str = Field(
        min_length=1,
        max_length=6000,
        description="调用方已切分好的单条文本片段；不得超过集合的 chunk_size。",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="仅属于当前向量记录的 JSON 元数据，可用于查询过滤。",
    )
    title: str | None = Field(default=None, max_length=500, description="可选的来源标题。")

    @model_validator(mode="after")
    def validate_content(self) -> "VectorDocument":
        if not self.content.strip():
            raise ValueError("content 不能为空")
        return self


class UpsertRequest(BaseModel):
    """批量写入已切分记录；每个输入项恰好生成一条向量。"""

    vectors: list[VectorDocument] = Field(
        min_length=1,
        max_length=64,
        description=(
            "调用方已切分好的向量记录列表，每项独立生成一条向量并使用自己的 metadata。"
            "为兼容旧客户端也接受 documents 字段。"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_documents_field(cls, value: Any) -> Any:
        """继续接受旧版客户端的 `documents` 字段，字段值语义为预切片记录。"""
        if isinstance(value, dict) and "vectors" not in value and "documents" in value:
            return {**value, "vectors": value["documents"]}
        return value

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "UpsertRequest":
        ids = [document.id for document in self.vectors]
        if len(ids) != len(set(ids)):
            raise ValueError("同一批次中的向量记录 id 不能重复")
        return self


class DeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=256)


class QueryRequest(BaseModel):
    """向量相似度查询；metadata 按 JSONB 包含语义过滤。"""

    query: str = Field(min_length=1, max_length=10_000)
    top_k: int = Field(default=5, ge=1, le=20)
    metadata: dict[str, Any] | None = None


def _collection(item: Any) -> CollectionResponse:
    return CollectionResponse(
        id=item.id,
        collection=item.name,
        description=item.description,
        model=item.embedding_model,
        vector_dimensions=item.vector_dimensions,
        chunk_size=item.chunk_size,
        chunk_overlap=item.chunk_overlap,
        created_at=item.created_at.isoformat(),
    )


async def _base(
    service: RagKnowledgeService, key: VerifiedApiKey, collection: str
) -> Any:
    items = await service.list_knowledge_bases(key.project_id)
    item = next((item for item in items if item.name == collection), None)
    if item is None:
        raise RagError("collection_not_found", "向量集合不存在", 404)
    return item


def _error(error: RagError) -> HTTPException:
    return HTTPException(error.status_code, detail={"code": error.code, "message": str(error)})


@router.post("/collections", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
async def create_collection(
    payload: CollectionCreateRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> CollectionResponse:
    try:
        item = await service.create_knowledge_base(
            key,
            name=payload.name,
            description=payload.description,
            model=payload.model,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            trace_id=request.state.trace_id,
        )
        return _collection(item)
    except RagError as error:
        raise _error(error) from error


@router.get("/collections", response_model=list[CollectionResponse])
async def list_collections(
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> list[CollectionResponse]:
    try:
        return [_collection(item) for item in await service.list_knowledge_bases(key.project_id)]
    except RagError as error:
        raise _error(error) from error


@router.delete("/collections/{collection}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection: str,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> None:
    try:
        base = await _base(service, key, collection)
        await service.delete_knowledge_base(key, base.id, request.state.trace_id)
    except RagError as error:
        raise _error(error) from error


@router.post(
    "/collections/{collection}/upsert",
    summary="批量写入已切分的向量记录",
    description=(
        "每个 vectors 项都必须是调用方预先切好的单条文本片段。网关不会对其再次切片；"
        "每项独立生成一个向量并保存该项的 metadata。记录长度不能超过集合 chunk_size。"
        "旧客户端仍可使用 documents 作为字段名。"
    ),
)
async def upsert_vectors(
    collection: str,
    payload: UpsertRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> dict[str, object]:
    try:
        base = await _base(service, key, collection)
        oversized = [
            document.id
            for document in payload.vectors
            if len(document.content.strip()) > base.chunk_size
        ]
        if oversized:
            raise RagError(
                "vector_too_large",
                f"已切分记录不能超过集合限制 {base.chunk_size} 个字符",
                413,
            )
        results = []
        existing = {
            item.external_id: item
            for item in await service.list_documents(key.project_id, base.id)
        }
        for document in payload.vectors:
            old = existing.get(document.id)
            if old is not None:
                await service.delete_document(key, base.id, old.id, request.state.trace_id)
            item = await service.create_document(
                key,
                base.id,
                title=document.title or document.id,
                content=document.content,
                external_id=document.id,
                metadata=document.metadata,
                trace_id=request.state.trace_id,
            )
            results.append(
                {
                    "id": str(item.id),
                    "external_id": item.external_id,
                    "vectors": item.chunks,
                    # Preserve the old response field for existing clients.
                    "chunks": item.chunks,
                }
            )
        return {"collection": collection, "upserted": results}
    except RagError as error:
        raise _error(error) from error


@router.post("/collections/{collection}/query")
async def query_collection(
    collection: str,
    payload: QueryRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> dict[str, object]:
    try:
        base = await _base(service, key, collection)
        results = await service.search(
            key,
            base.id,
            payload.query,
            payload.top_k,
            request.state.trace_id,
            metadata_filter=payload.metadata,
        )
        return {"collection": collection, "results": results}
    except RagError as error:
        raise _error(error) from error


@router.post("/collections/{collection}/delete")
async def delete_documents(
    collection: str,
    payload: DeleteRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> dict[str, object]:
    try:
        base = await _base(service, key, collection)
        documents = await service.list_documents(key.project_id, base.id)
        deleted = 0
        for document in documents:
            if document.external_id in payload.ids:
                await service.delete_document(key, base.id, document.id, request.state.trace_id)
                deleted += 1
        return {"collection": collection, "deleted": deleted}
    except RagError as error:
        raise _error(error) from error
