"""通用项目向量数据库 API。

该接口以 collection 名称屏蔽内部知识库 UUID，适合不使用 LangChain 的 Agent
项目通过一个可配置的 HTTP URL 接入。向量由网关配置的 Embedding 服务生成。
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.application.api_keys import VerifiedApiKey
from app.container import get_embedding_rag_service
from app.services.embedding.multimodal import EmbeddingInput, VideoUrl, validate_media_url
from app.services.embedding.rag import RagError, RagKnowledgeService, RagVectorUpsert
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
    """一条外部记录；vector 会嵌入，document 仅保存正文。"""

    id: str = Field(min_length=1, max_length=200, description="调用方提供的稳定记录 ID。")
    record_kind: Literal["vector", "document"] = Field(
        default="vector", description="vector 生成向量；document 只保存正文和 metadata。"
    )
    parent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="同项目、同 collection 下 document 记录的外部 ID；先写父记录再写子记录。",
    )
    namespace: str | None = Field(default=None, min_length=1, max_length=200)
    document_id: str | None = Field(default=None, min_length=1, max_length=200)
    version_id: str | None = Field(default=None, min_length=1, max_length=200)
    staged: bool = Field(
        default=False,
        description=(
            "暂存版本不参与普通 query/fetch；发布时需提供 namespace、document_id 和 version_id。"
        ),
    )
    modality: Literal["text", "image", "video"] = "text"
    content: str | None = Field(
        default=None,
        max_length=20_000,
        description=(
            "正文；vector 文本受 collection.chunk_size 限制，document 最多 20,000 个 Unicode 字符。"
        ),
    )
    media_url: str | None = Field(
        default=None,
        max_length=8000,
        description="图像或视频的可访问 HTTPS URL；原文件由调用方或对象存储管理。",
    )
    video_options: dict[str, object] | None = Field(
        default=None,
        description="可选视频抽帧参数，受豆包版本和火山方舟参数范围约束。",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="仅属于当前向量记录的 JSON 元数据，可用于查询过滤。",
    )
    title: str | None = Field(default=None, max_length=500, description="可选的来源标题。")

    @model_validator(mode="after")
    def validate_content(self) -> "VectorDocument":
        if self.staged and not (self.namespace and self.document_id and self.version_id):
            raise ValueError("staged 记录必须填写 namespace、document_id 和 version_id")
        if bool(self.namespace) != bool(self.document_id):
            raise ValueError("namespace 和 document_id 必须同时提供")
        if self.version_id and not (self.namespace and self.document_id):
            raise ValueError("version_id 必须同时提供 namespace 和 document_id")
        if self.version_id and not self.staged:
            raise ValueError("带版本字段的记录必须先暂存，再通过 publish 原子启用")
        if self.modality == "text":
            if self.content is not None and not self.content.strip():
                raise ValueError("content 不能为空")
            if not self.content:
                raise ValueError("文本记录必须填写 content")
            if self.media_url is not None:
                raise ValueError("文本记录不能填写 media_url")
            if self.video_options is not None:
                raise ValueError("video_options 仅适用于视频记录")
        else:
            if self.record_kind != "vector":
                raise ValueError("document 普通记录仅支持 text modality")
            if self.media_url is None:
                raise ValueError("图片或视频记录必须填写 media_url")
            validate_media_url(self.media_url)
            if not self.media_url.lower().startswith("https://"):
                raise ValueError("媒体地址必须使用 HTTPS")
            if self.modality == "video":
                VideoUrl.model_validate({"url": self.media_url, **(self.video_options or {})})
            elif self.video_options is not None:
                raise ValueError("video_options 仅适用于视频记录")
        if self.content is not None and not self.content.strip():
            raise ValueError("content 不能为空")
        if (
            self.record_kind == "vector"
            and self.modality == "text"
            and len(self.content or "") > 6000
        ):
            raise ValueError("向量文本不能超过 6000 个字符")
        if self.record_kind == "document" and (
            self.modality != "text" or self.media_url is not None or self.video_options is not None
        ):
            raise ValueError("document 普通记录只支持正文和 metadata")
        if self.record_kind == "document" and self.parent_id is not None:
            raise ValueError("第一版 document 普通记录必须是顶层父记录")
        return self


class UpsertRequest(BaseModel):
    """批量写入向量记录和普通正文记录；不负责文档切片。"""

    vectors: list[VectorDocument] = Field(
        min_length=1,
        max_length=64,
        description=(
            "1–64 条记录。record_kind=vector 每项生成向量；document 只存正文，不调用 Embedding。"
            "同批文本统一批量 Embedding；为兼容旧客户端也接受 documents 字段。"
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
        if any(item.parent_id in ids for item in self.vectors if item.parent_id):
            raise ValueError("父记录必须先单独写入，不能在同一批次内引用")
        if len(self.model_dump_json().encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("单次请求体不能超过 4 MiB")
        return self


class DeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=256)

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("每个 ID 必须为 1 到 200 个字符")
        return values


class FetchRequest(BaseModel):
    """按外部 ID 批量读取已发布的普通或向量记录，不返回 embedding。"""

    ids: list[str] = Field(min_length=1, max_length=256)

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("每个 ID 必须为 1 到 200 个字符")
        return values


class PublishVersionRequest(BaseModel):
    """原子启用一个暂存版本，并停用指定业务文档旧版本。"""

    namespace: str = Field(min_length=1, max_length=200)
    document_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=1, max_length=200)


class QueryRequest(BaseModel):
    """向量相似度查询；metadata 按 JSONB 包含语义过滤。"""

    query: str | list[EmbeddingInput] = Field(min_length=1, max_length=10_000)
    top_k: int = Field(default=5, ge=1, le=20)
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_query(self) -> "QueryRequest":
        if isinstance(self.query, str) and not self.query.strip():
            raise ValueError("query 不能为空")
        if isinstance(self.query, list) and not self.query:
            raise ValueError("query 至少需要一个多模态内容项")
        return self


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


async def _base(service: RagKnowledgeService, key: VerifiedApiKey, collection: str) -> Any:
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
    summary="批量写入普通记录和向量记录",
    description=(
        "vectors 每批最多 64 条、请求体最多 4 MiB。"
        "record_kind=vector 对预切片文本或媒体生成 Embedding；"
        "record_kind=document 只存正文和 metadata，"
        "正文最多 20,000 个 Unicode 码位并跳过 Embedding。"
        "向量文本不超过 collection.chunk_size。ID 在项目+collection 内唯一，"
        "同类型重复提交幂等替换；vector/document 类型不可互转。"
        "父记录须先单独写入，且必须是同项目、同 collection 的 document。"
        "Embedding 成功后，记录在一个数据库事务中替换；外部 Embedding 用量不可随事务回滚。"
        "提供 namespace、document_id、version_id 时必须设 staged=true，并调用 publish 后才可见。"
        "响应 id 保留为内部 UUID；external_id 才是调用方 ID，"
        "后续 fetch、delete 和 parent_id 均使用它。"
        "省略 record_kind 时默认为 vector，旧客户端也可继续传 documents 字段。"
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
        if len(await request.body()) > 4 * 1024 * 1024:
            raise RagError("request_too_large", "单次请求体不能超过 4 MiB", 413)
        base = await _base(service, key, collection)
        oversized = [
            document.id
            for document in payload.vectors
            if document.record_kind == "vector"
            and document.modality == "text"
            and document.content is not None
            and len(document.content.strip()) > base.chunk_size
        ]
        if oversized:
            raise RagError(
                "vector_too_large",
                f"已切分记录不能超过集合限制 {base.chunk_size} 个字符",
                413,
            )
        prepared: list[RagVectorUpsert] = []
        for document in payload.vectors:
            stored_content = (document.content or document.media_url or "").strip()
            metadata = dict(document.metadata)
            embedding_inputs: list[str | dict[str, object]]
            if document.record_kind == "document":
                embedding_inputs = []
            elif document.modality == "text":
                embedding_inputs = [stored_content]
            else:
                assert document.media_url is not None
                metadata["media_url"] = document.media_url
                media_type = "image_url" if document.modality == "image" else "video_url"
                embedding_inputs = [
                    {
                        "type": media_type,
                        media_type: {
                            "url": document.media_url,
                            **(document.video_options or {}),
                        },
                    }
                ]
                if document.content and document.content.strip():
                    embedding_inputs.append({"type": "text", "text": document.content.strip()})
            prepared.append(
                RagVectorUpsert(
                    external_id=document.id,
                    title=document.title or document.id,
                    content=stored_content,
                    metadata=metadata,
                    modality=document.modality,
                    embedding_inputs=embedding_inputs,
                    record_kind=document.record_kind,
                    parent_id=document.parent_id,
                    namespace=document.namespace,
                    logical_document_id=document.document_id,
                    version_id=document.version_id,
                    staged=document.staged,
                )
            )

        items = await service.upsert_vectors(
            key,
            base.id,
            prepared,
            trace_id=request.state.trace_id,
        )
        results = []
        for input_record, item in zip(payload.vectors, items, strict=True):
            results.append(
                {
                    "id": str(item.id),
                    "external_id": item.external_id,
                    "vectors": item.chunks,
                    "record_kind": input_record.record_kind,
                    "parent_id": input_record.parent_id,
                    # Preserve the old response field for existing clients.
                    "chunks": item.chunks,
                }
            )
        return {"collection": collection, "upserted": results}
    except RagError as error:
        raise _error(error) from error


@router.post(
    "/collections/{collection}/fetch",
    summary="按外部 ID 批量读取记录",
    description=(
        "精确读取已发布的 vector/document 正文和 metadata；不返回 embedding、不调用 Embedding。"
        "最多 256 个 ID，去重后按首次出现顺序返回。暂存记录与不存在的 ID 一样列入 missing_ids。"
    ),
)
async def fetch_collection_records(
    collection: str,
    payload: FetchRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> dict[str, object]:
    try:
        base = await _base(service, key, collection)
        records, missing_ids = await service.fetch_records(
            key, base.id, payload.ids, trace_id=request.state.trace_id
        )
        return {"collection": collection, "records": records, "missing_ids": missing_ids}
    except RagError as error:
        raise _error(error) from error


@router.post(
    "/collections/{collection}/publish",
    summary="原子发布暂存版本",
    description=(
        "在项目 API Key 所属项目和 collection 内，按 namespace、document_id 发布指定 version_id。"
        "目标版本的暂存记录在一个数据库事务中启用，旧版本退出 query/fetch；之后可另行清理旧记录。"
    ),
)
async def publish_collection_version(
    collection: str,
    payload: PublishVersionRequest,
    request: Request,
    key: Annotated[VerifiedApiKey, Depends(verify_project_key)],
    service: Annotated[RagKnowledgeService, Depends(get_embedding_rag_service)],
) -> dict[str, object]:
    try:
        base = await _base(service, key, collection)
        result = await service.publish_version(
            key,
            base.id,
            namespace=payload.namespace,
            logical_document_id=payload.document_id,
            version_id=payload.version_id,
            trace_id=request.state.trace_id,
        )
        return {"collection": collection, **result}
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
            payload.query
            if isinstance(payload.query, str)
            else [item.model_dump(mode="json", exclude_none=True) for item in payload.query],
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
        result = await service.delete_records(key, base.id, payload.ids, request.state.trace_id)
        deleted_ids = result.get("deleted_ids")
        deleted = len(deleted_ids) if isinstance(deleted_ids, list) else 0
        return {"collection": collection, "deleted": deleted, **result}
    except RagError as error:
        raise _error(error) from error
