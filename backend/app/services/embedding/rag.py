"""项目隔离的文本知识库、切片向量化和 pgvector 相似度检索。"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import cast as sa_cast
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.api_keys import VerifiedApiKey
from app.infrastructure.db.models import (
    ProjectServiceSubscription,
    RagChunk,
    RagDocument,
    RagKnowledgeBase,
)
from app.request_logging.application import GatewayRequestRecorder
from app.services.embedding.application import EmbeddingGatewayService
from app.services.embedding.catalog import RAG_SERVICE
from app.services.embedding.multimodal import VideoUrl, validate_media_url

logger = logging.getLogger("gateway.service.rag")


class RagError(RuntimeError):
    """RAG 请求安全错误。"""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class RagDocumentInfo:
    id: UUID
    external_id: str | None
    title: str
    chunks: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RagVectorInfo:
    """一个可独立读取和删除的向量切片，不返回向量数组本身。"""

    id: UUID
    document_id: UUID
    external_id: str | None
    title: str
    content: str
    metadata: dict[str, object]
    sequence: int
    modality: str
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RagVectorUpsert:
    """一条调用方已切分好的记录，以及生成其向量所需的输入。"""

    external_id: str
    title: str
    content: str
    metadata: dict[str, object]
    modality: str
    embedding_inputs: list[str | dict[str, object]]


class RagKnowledgeService:
    """纯检索 RAG 数据层；不负责拼接 Prompt 或调用生成模型。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_gateway: EmbeddingGatewayService,
        recorder: GatewayRequestRecorder,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_gateway = embedding_gateway
        self._recorder = recorder

    async def create_knowledge_base(
        self,
        key: VerifiedApiKey,
        *,
        name: str,
        description: str | None,
        model: str,
        chunk_size: int,
        chunk_overlap: int,
        trace_id: str,
    ) -> RagKnowledgeBase:
        await self._require_enabled(key.project_id)
        async with self._session_factory.begin() as session:
            base = RagKnowledgeBase(
                project_id=key.project_id,
                name=name.strip(),
                description=description.strip() if description else None,
                embedding_model=model.strip(),
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                created_by=key.user_id or key.owner_id,
            )
            session.add(base)
            await session.flush()
            await session.refresh(base)
            base_id = base.id
        await self._operation(key, trace_id, "rag.knowledge_base.create", "创建 RAG 知识库")
        return await self.get_knowledge_base(key.project_id, base_id)

    async def list_knowledge_bases(self, project_id: UUID) -> list[RagKnowledgeBase]:
        await self._require_enabled(project_id)
        async with self._session_factory() as session:
            items = await session.scalars(
                select(RagKnowledgeBase)
                .where(RagKnowledgeBase.project_id == project_id)
                .order_by(RagKnowledgeBase.created_at.desc(), RagKnowledgeBase.id.desc())
            )
            return list(items)

    async def get_knowledge_base(self, project_id: UUID, base_id: UUID) -> RagKnowledgeBase:
        async with self._session_factory() as session:
            base = await session.scalar(
                select(RagKnowledgeBase).where(
                    RagKnowledgeBase.id == base_id,
                    RagKnowledgeBase.project_id == project_id,
                )
            )
        if base is None:
            raise RagError("knowledge_base_not_found", "知识库不存在", 404)
        return base

    async def delete_knowledge_base(
        self, key: VerifiedApiKey, base_id: UUID, trace_id: str
    ) -> None:
        await self._require_enabled(key.project_id)
        async with self._session_factory.begin() as session:
            base = await session.scalar(
                select(RagKnowledgeBase)
                .where(
                    RagKnowledgeBase.id == base_id,
                    RagKnowledgeBase.project_id == key.project_id,
                )
                .with_for_update()
            )
            if base is None:
                raise RagError("knowledge_base_not_found", "知识库不存在", 404)
            await session.delete(base)
        await self._operation(key, trace_id, "rag.knowledge_base.delete", "删除 RAG 知识库")

    async def create_document(
        self,
        key: VerifiedApiKey,
        base_id: UUID,
        *,
        title: str,
        content: str,
        external_id: str | None,
        metadata: dict[str, object],
        trace_id: str,
        modality: str = "text",
        embedding_inputs: list[str | dict[str, object]] | None = None,
    ) -> RagDocumentInfo:
        await self._require_enabled(key.project_id)
        base = await self.get_knowledge_base(key.project_id, base_id)
        chunks = (
            self._chunk_text(content, base.chunk_size, base.chunk_overlap)
            if modality == "text"
            else [content.strip()]
        )
        if not chunks:
            raise RagError("empty_document", "文档内容为空", 422)
        if len(chunks) > 64:
            raise RagError("document_too_large", "单个文档最多切分为 64 个文本片段", 413)
        async with self._session_factory() as session:
            if external_id is not None:
                exists = await session.scalar(
                    select(RagDocument.id).where(
                        RagDocument.knowledge_base_id == base_id,
                        RagDocument.external_id == external_id,
                    )
                )
                if exists is not None:
                    raise RagError("document_id_conflict", "此 external_id 已存在", 409)
        embed_inputs: list[str | dict[str, object]] = (
            embedding_inputs if embedding_inputs is not None else list(chunks)
        )
        embedded = await self._embedding_gateway.embed(
            key,
            base.embedding_model,
            embed_inputs,
            None,
            f"emb_{uuid4().hex}",
        )
        data = embedded.get("data")
        if not isinstance(data, list) or len(data) != len(chunks):
            raise RagError("embedding_response_invalid", "Embedding 响应格式无效", 502)
        vectors: list[list[float]] = []
        for item in data:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list):
                raise RagError("embedding_response_invalid", "Embedding 响应缺少向量", 502)
            vectors.append([float(value) for value in vector])
        dimensions = len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors):
            raise RagError("embedding_dimensions_invalid", "Embedding 向量维度不一致", 502)
        async with self._session_factory.begin() as session:
            locked_base = await session.scalar(
                select(RagKnowledgeBase)
                .where(
                    RagKnowledgeBase.id == base_id,
                    RagKnowledgeBase.project_id == key.project_id,
                )
                .with_for_update()
            )
            if locked_base is None:
                raise RagError("knowledge_base_not_found", "知识库不存在", 404)
            if (
                locked_base.vector_dimensions is not None
                and locked_base.vector_dimensions != dimensions
            ):
                raise RagError(
                    "embedding_dimensions_conflict",
                    "该知识库已有不同向量维度的数据，请使用原模型或新建知识库",
                    409,
                )
            locked_base.vector_dimensions = dimensions
            document = RagDocument(
                knowledge_base_id=base_id,
                external_id=external_id,
                title=title.strip(),
                content=content,
                metadata_json=metadata,
                created_by=key.user_id or key.owner_id,
            )
            session.add(document)
            await session.flush()
            for sequence, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                session.add(
                    RagChunk(
                        knowledge_base_id=base_id,
                        document_id=document.id,
                        sequence=sequence,
                        content=chunk,
                        embedding=vector,
                        embedding_model=base.embedding_model,
                        dimensions=dimensions,
                        modality=modality,
                        metadata_json=metadata,
                    )
                )
            await session.flush()
            result = RagDocumentInfo(
                document.id, document.external_id, document.title, len(chunks), document.created_at
            )
        await self._operation(
            key, trace_id, "rag.document.create", f"新增 RAG 文档，片段数 {len(chunks)}"
        )
        return result

    async def upsert_vectors(
        self,
        key: VerifiedApiKey,
        base_id: UUID,
        vectors: list[RagVectorUpsert],
        *,
        trace_id: str,
    ) -> list[RagDocumentInfo]:
        """批量生成向量并原子替换记录；文本记录合并为一次 Embedding 请求。"""
        await self._require_enabled(key.project_id)
        if not vectors:
            raise RagError("empty_upsert", "至少需要一条向量记录", 422)
        external_ids = [item.external_id for item in vectors]
        if len(external_ids) != len(set(external_ids)):
            raise RagError("duplicate_vector_id", "同一批次中的向量记录 ID 不能重复", 422)
        base = await self.get_knowledge_base(key.project_id, base_id)

        embeddings: list[list[float] | None] = [None] * len(vectors)
        text_indexes = [index for index, item in enumerate(vectors) if item.modality == "text"]
        if text_indexes:
            response = await self._embedding_gateway.embed(
                key,
                base.embedding_model,
                [vectors[index].content for index in text_indexes],
                None,
                f"emb_{uuid4().hex}",
            )
            self._assign_batch_embeddings(response, text_indexes, embeddings)

        # 多模态组件在火山接口中共同组成一条向量；每条记录需独立请求。
        for index, item in enumerate(vectors):
            if item.modality == "text":
                continue
            response = await self._embedding_gateway.embed(
                key,
                base.embedding_model,
                item.embedding_inputs,
                None,
                f"emb_{uuid4().hex}",
            )
            self._assign_batch_embeddings(response, [index], embeddings)

        resolved_embeddings = [vector for vector in embeddings if vector is not None]
        if len(resolved_embeddings) != len(vectors):
            raise RagError("embedding_response_invalid", "Embedding 响应缺少向量", 502)
        dimensions = len(resolved_embeddings[0])
        if any(len(vector) != dimensions for vector in resolved_embeddings):
            raise RagError("embedding_dimensions_invalid", "Embedding 向量维度不一致", 502)

        inserted_documents: list[RagDocument] = []
        try:
            async with self._session_factory.begin() as session:
                locked_base = await session.scalar(
                    select(RagKnowledgeBase)
                    .where(
                        RagKnowledgeBase.id == base_id,
                        RagKnowledgeBase.project_id == key.project_id,
                    )
                    .with_for_update()
                )
                if locked_base is None:
                    raise RagError("knowledge_base_not_found", "向量集合不存在", 404)
                if (
                    locked_base.vector_dimensions is not None
                    and locked_base.vector_dimensions != dimensions
                ):
                    raise RagError(
                        "embedding_dimensions_conflict",
                        "该集合已有不同向量维度的数据，请使用原模型或新建集合",
                        409,
                    )
                locked_base.vector_dimensions = dimensions

                existing_rows = await session.scalars(
                    select(RagDocument)
                    .where(
                        RagDocument.knowledge_base_id == base_id,
                        RagDocument.external_id.in_(external_ids),
                    )
                    .with_for_update()
                )
                for document in existing_rows:
                    await session.delete(document)
                # 先删除旧记录并 flush，释放 (collection, external_id) 唯一键，
                # 再在同一事务内插入新版本；失败时整批回滚，不会留下空档。
                await session.flush()

                chunks: list[RagChunk] = []
                for item, embedding in zip(vectors, resolved_embeddings, strict=True):
                    document = RagDocument(
                        id=uuid4(),
                        knowledge_base_id=base_id,
                        external_id=item.external_id,
                        title=item.title,
                        content=item.content,
                        metadata_json=item.metadata,
                        created_by=key.user_id or key.owner_id,
                    )
                    inserted_documents.append(document)
                    chunks.append(
                        RagChunk(
                            knowledge_base_id=base_id,
                            document_id=document.id,
                            sequence=0,
                            content=item.content,
                            embedding=embedding,
                            embedding_model=base.embedding_model,
                            dimensions=dimensions,
                            modality=item.modality,
                            metadata_json=item.metadata,
                        )
                    )
                session.add_all(inserted_documents)
                await session.flush()
                session.add_all(chunks)
                await session.flush()
        except RagError:
            raise

        await self._operation(
            key,
            trace_id,
            "rag.vector.upsert",
            f"批量写入向量记录，数量 {len(inserted_documents)}",
        )
        return [
            RagDocumentInfo(
                document.id,
                document.external_id,
                document.title,
                1,
                document.created_at,
            )
            for document in inserted_documents
        ]

    @staticmethod
    def _assign_batch_embeddings(
        response: dict[str, object],
        target_indexes: list[int],
        embeddings: list[list[float] | None],
    ) -> None:
        """按 OpenAI Embeddings 的 index 字段将批量响应映射回输入记录。"""
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(target_indexes):
            raise RagError("embedding_response_invalid", "Embedding 返回的向量数量不正确", 502)
        mapped: dict[int, list[float]] = {}
        for fallback_index, item in enumerate(data):
            if not isinstance(item, dict):
                raise RagError("embedding_response_invalid", "Embedding 响应格式无效", 502)
            index = item.get("index", fallback_index)
            raw_vector = item.get("embedding")
            if (
                not isinstance(index, int)
                or index < 0
                or index >= len(target_indexes)
                or not isinstance(raw_vector, list)
                or not raw_vector
            ):
                raise RagError("embedding_response_invalid", "Embedding 响应格式无效", 502)
            try:
                mapped[index] = [float(value) for value in raw_vector]
            except (TypeError, ValueError) as error:
                raise RagError(
                    "embedding_response_invalid", "Embedding 向量包含无效数值", 502
                ) from error
        if len(mapped) != len(target_indexes):
            raise RagError("embedding_response_invalid", "Embedding 响应索引重复或缺失", 502)
        for response_index, target_index in enumerate(target_indexes):
            embeddings[target_index] = mapped[response_index]

    async def list_documents(self, project_id: UUID, base_id: UUID) -> list[RagDocumentInfo]:
        await self.get_knowledge_base(project_id, base_id)
        async with self._session_factory() as session:
            rows = await session.execute(
                select(RagDocument, RagChunk.id)
                .outerjoin(RagChunk, RagChunk.document_id == RagDocument.id)
                .where(RagDocument.knowledge_base_id == base_id)
                .order_by(RagDocument.created_at.desc(), RagDocument.id, RagChunk.sequence)
            )
            result: dict[UUID, RagDocumentInfo] = {}
            for document, chunk_id in rows.all():
                current = result.get(document.id)
                result[document.id] = RagDocumentInfo(
                    document.id,
                    document.external_id,
                    document.title,
                    (current.chunks if current else 0) + (1 if chunk_id else 0),
                    document.created_at,
                )
            return list(result.values())

    async def list_vectors(self, project_id: UUID, base_id: UUID) -> list[RagVectorInfo]:
        """列出集合内每个独立向量切片，保留文档信息供管理页面展示。"""
        await self.get_knowledge_base(project_id, base_id)
        async with self._session_factory() as session:
            rows = await session.execute(
                select(RagChunk, RagDocument)
                .join(RagDocument, RagDocument.id == RagChunk.document_id)
                .where(RagChunk.knowledge_base_id == base_id)
                .order_by(RagDocument.created_at.desc(), RagChunk.document_id, RagChunk.sequence)
            )
            return [
                RagVectorInfo(
                    chunk.id,
                    document.id,
                    document.external_id,
                    document.title,
                    chunk.content,
                    chunk.metadata_json,
                    chunk.sequence,
                    chunk.modality,
                    document.created_by,
                    chunk.created_at,
                    chunk.updated_at,
                )
                for chunk, document in rows.all()
            ]

    async def update_vector(
        self,
        key: VerifiedApiKey,
        base_id: UUID,
        vector_id: UUID,
        *,
        content: str,
        metadata: dict[str, object],
        trace_id: str,
    ) -> RagVectorInfo:
        """Update one vector slice and re-embed only when its text changed."""
        await self._require_enabled(key.project_id)
        base = await self.get_knowledge_base(key.project_id, base_id)
        normalized_content = content.strip()
        if not normalized_content:
            raise RagError("empty_vector", "文本片段不能为空", 422)
        if len(normalized_content) > 6000:
            raise RagError(
                "vector_too_large",
                "单条向量记录不能超过 6000 个字符",
                413,
            )

        async with self._session_factory() as session:
            row = await session.execute(
                select(RagChunk, RagDocument)
                .join(RagDocument, RagDocument.id == RagChunk.document_id)
                .join(RagKnowledgeBase, RagKnowledgeBase.id == RagChunk.knowledge_base_id)
                .where(
                    RagChunk.id == vector_id,
                    RagChunk.knowledge_base_id == base_id,
                    RagKnowledgeBase.project_id == key.project_id,
                )
            )
            current_row = row.one_or_none()
        if current_row is None:
            raise RagError("vector_not_found", "向量记录不存在", 404)
        current_chunk, current_document = current_row
        current_content = current_chunk.content
        current_modality = current_chunk.modality
        if current_modality == "text" and len(normalized_content) > base.chunk_size:
            raise RagError(
                "vector_too_large",
                f"单条文本向量记录不能超过 {base.chunk_size} 个字符",
                413,
            )
        current_dimensions = current_chunk.dimensions
        source_document_id = current_document.id
        source_external_id = current_document.external_id
        source_title = current_document.title
        source_created_by = current_document.created_by
        source_metadata = current_chunk.metadata_json

        source_media_url = source_metadata.get("media_url")
        if current_modality in {"image", "video"}:
            if not isinstance(source_media_url, str):
                raise RagError("media_source_missing", "媒体记录缺少源 URL", 409)
            updated_media_url = metadata.get("media_url", source_media_url)
            if not isinstance(updated_media_url, str):
                raise RagError("media_source_invalid", "媒体记录的 media_url 必须是字符串", 422)
            try:
                validate_media_url(updated_media_url)
                if not updated_media_url.lower().startswith("https://"):
                    raise ValueError("media_url 必须使用 HTTPS")
                if current_modality == "video":
                    VideoUrl.model_validate({"url": updated_media_url})
            except ValueError as error:
                raise RagError("media_source_invalid", str(error), 422) from error
            metadata = {**metadata, "media_url": updated_media_url}
        else:
            updated_media_url = None

        embedding: list[float] | None = None
        media_url_changed = updated_media_url != source_media_url
        if normalized_content != current_content or media_url_changed:
            embedding_inputs: list[str | dict[str, object]]
            if current_modality == "text":
                embedding_inputs = [normalized_content]
            else:
                assert updated_media_url is not None
                media_type = "image_url" if current_modality == "image" else "video_url"
                embedding_inputs = [
                    {"type": media_type, media_type: {"url": updated_media_url}}
                ]
                if normalized_content != updated_media_url:
                    embedding_inputs.append({"type": "text", "text": normalized_content})
            response = await self._embedding_gateway.embed(
                key,
                base.embedding_model,
                embedding_inputs,
                None,
                f"emb_{uuid4().hex}",
            )
            data = response.get("data")
            raw_vector = (
                data[0].get("embedding")
                if isinstance(data, list) and data and isinstance(data[0], dict)
                else None
            )
            if not isinstance(raw_vector, list):
                raise RagError("embedding_response_invalid", "Embedding 响应缺少向量", 502)
            embedding = [float(value) for value in raw_vector]
            expected_dimensions = base.vector_dimensions or current_dimensions
            if len(embedding) != expected_dimensions:
                raise RagError(
                    "embedding_dimensions_conflict",
                    "新文本生成的向量维度与集合不匹配",
                    409,
                )

        async with self._session_factory.begin() as session:
            vector = await session.scalar(
                select(RagChunk)
                .join(RagKnowledgeBase)
                .where(
                    RagChunk.id == vector_id,
                    RagChunk.knowledge_base_id == base_id,
                    RagKnowledgeBase.project_id == key.project_id,
                )
                .with_for_update()
            )
            if vector is None:
                raise RagError("vector_not_found", "向量记录不存在", 404)
            vector.content = normalized_content
            vector.metadata_json = metadata
            if embedding is not None:
                vector.embedding = embedding
            vector.updated_at = datetime.now(UTC)
            await session.flush()
            result = RagVectorInfo(
                vector.id,
                source_document_id,
                source_external_id,
                source_title,
                vector.content,
                vector.metadata_json,
                vector.sequence,
                vector.modality,
                source_created_by,
                vector.created_at,
                vector.updated_at,
            )
        await self._operation(key, trace_id, "rag.vector.update", "修改单条向量记录")
        return result

    async def delete_vector(
        self, key: VerifiedApiKey, base_id: UUID, vector_id: UUID, trace_id: str
    ) -> None:
        """删除一条切片向量；若它是文档最后一片，同时删除其文档外壳。"""
        await self._require_enabled(key.project_id)
        async with self._session_factory.begin() as session:
            vector = await session.scalar(
                select(RagChunk)
                .join(RagKnowledgeBase)
                .where(
                    RagChunk.id == vector_id,
                    RagChunk.knowledge_base_id == base_id,
                    RagKnowledgeBase.project_id == key.project_id,
                )
                .with_for_update()
            )
            if vector is None:
                raise RagError("vector_not_found", "向量记录不存在", 404)
            document_id = vector.document_id
            await session.delete(vector)
            await session.flush()
            remaining = await session.scalar(
                select(func.count())
                .select_from(RagChunk)
                .where(RagChunk.document_id == document_id)
            )
            if remaining == 0:
                document = await session.get(RagDocument, document_id)
                if document is not None:
                    await session.delete(document)
        await self._operation(key, trace_id, "rag.vector.delete", "删除单条向量记录")

    async def delete_document(
        self, key: VerifiedApiKey, base_id: UUID, document_id: UUID, trace_id: str
    ) -> None:
        await self._require_enabled(key.project_id)
        async with self._session_factory.begin() as session:
            document = await session.scalar(
                select(RagDocument)
                .join(RagKnowledgeBase)
                .where(
                    RagDocument.id == document_id,
                    RagDocument.knowledge_base_id == base_id,
                    RagKnowledgeBase.project_id == key.project_id,
                )
                .with_for_update()
            )
            if document is None:
                raise RagError("document_not_found", "文档不存在", 404)
            await session.delete(document)
        await self._operation(key, trace_id, "rag.document.delete", "删除 RAG 文档")

    async def search(
        self,
        key: VerifiedApiKey,
        base_id: UUID,
        query: str | list[dict[str, object]],
        top_k: int,
        trace_id: str,
        metadata_filter: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        await self._require_enabled(key.project_id)
        base = await self.get_knowledge_base(key.project_id, base_id)
        if isinstance(query, str):
            query_inputs: list[str | dict[str, object]] = [query]
        else:
            query_inputs = cast(list[str | dict[str, object]], query)
        embedded = await self._embedding_gateway.embed(
            key,
            base.embedding_model,
            query_inputs,
            None,
            f"emb_{uuid4().hex}",
        )
        data = embedded.get("data")
        vector = data[0].get("embedding") if isinstance(data, list) and data else None
        if not isinstance(vector, list):
            raise RagError("embedding_response_invalid", "Embedding 响应格式无效", 502)
        if base.vector_dimensions is not None and len(vector) != base.vector_dimensions:
            raise RagError("embedding_dimensions_conflict", "查询向量维度与知识库不匹配", 409)
        async with self._session_factory() as session:
            statement = (
                select(
                    RagChunk,
                    RagDocument.title,
                    RagChunk.embedding.cosine_distance(vector).label("distance"),
                )
                .join(RagDocument, RagDocument.id == RagChunk.document_id)
                .where(
                    RagChunk.knowledge_base_id == base_id,
                    RagChunk.dimensions == len(vector),
                )
            )
            if metadata_filter:
                statement = statement.where(
                    sa_cast(RagChunk.metadata_json, JSONB).contains(metadata_filter)
                )
            rows = await session.execute(
                statement.order_by(RagChunk.embedding.cosine_distance(vector)).limit(top_k)
            )
            results: list[dict[str, object]] = []
            for row in rows.all():
                chunk, title, distance = cast(tuple[RagChunk, str, float], row)
                results.append(
                    {
                        "chunk_id": str(chunk.id),
                        "document_id": str(chunk.document_id),
                        "title": title,
                        "content": chunk.content,
                        "modality": chunk.modality,
                        "score": max(-1.0, min(1.0, 1.0 - float(distance))),
                        "metadata": chunk.metadata_json,
                    }
                )
        await self._operation(
            key, trace_id, "rag.search", f"RAG 检索成功，返回 {len(results)} 个片段"
        )
        return results

    async def _require_enabled(self, project_id: UUID) -> None:
        async with self._session_factory() as session:
            status = await session.scalar(
                select(ProjectServiceSubscription.status).where(
                    ProjectServiceSubscription.project_id == project_id,
                    ProjectServiceSubscription.service_code == RAG_SERVICE.code,
                )
            )
        if status != "active":
            raise RagError("service_not_enabled", "项目尚未申请 RAG 知识库服务", 403)

    async def _operation(
        self,
        key: VerifiedApiKey,
        trace_id: str,
        operation: str,
        description: str,
        *,
        status: Literal["succeeded", "failed"] = "succeeded",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if key.user_id is None or key.username is None:
            return
        try:
            await self._recorder.record_project_operation(
                trace_id=trace_id,
                project_id=key.project_id,
                actor_user_id=key.user_id,
                actor_username=key.username,
                operation=operation,
                description=description,
                api_key_id=key.id,
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            logger.exception(
                "rag_operation_log_write_failed project_id=%s operation=%s",
                key.project_id,
                operation,
            )

    async def record_operation_failure(
        self, key: VerifiedApiKey, trace_id: str, operation: str, error: Exception
    ) -> None:
        """Persist a failed RAG operation with a safe error, never request content."""
        error_code = error.code if isinstance(error, RagError) else "rag_internal_error"
        error_message = str(error) if isinstance(error, RagError) else "RAG 服务内部处理失败"
        await self._operation(
            key,
            trace_id,
            operation,
            f"{operation} 失败",
            status="failed",
            error_code=error_code,
            error_message=error_message,
        )

    @staticmethod
    def _chunk_text(content: str, size: int, overlap: int) -> list[str]:
        text = content.strip()
        if not text:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            if end < len(text):
                paragraph = text.rfind("\n\n", start, end)
                sentence = max(
                    text.rfind("。", start, end),
                    text.rfind("！", start, end),
                    text.rfind("？", start, end),
                    text.rfind("\n", start, end),
                )
                split_at = paragraph + 2 if paragraph > start + size // 2 else sentence + 1
                if split_at > start + size // 2:
                    end = split_at
            piece = text[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
        return chunks
