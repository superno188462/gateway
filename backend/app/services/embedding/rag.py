"""项目隔离的文本知识库、切片向量化和 pgvector 相似度检索。"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import cast as sa_cast
from sqlalchemy import delete, func, select, tuple_
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
    """一条外部记录及可选的 Embedding 输入；document 记录没有 Embedding 输入。"""

    external_id: str
    title: str
    content: str
    metadata: dict[str, object]
    modality: str
    embedding_inputs: list[str | dict[str, object]]
    record_kind: Literal["vector", "document"] = "vector"
    parent_id: str | None = None
    namespace: str | None = None
    logical_document_id: str | None = None
    version_id: str | None = None
    staged: bool = False


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

    async def _validate_parent_records(
        self, project_id: UUID, base_id: UUID, records: list[RagVectorUpsert]
    ) -> None:
        """在消耗 Embedding 额度前验证父记录存在且处于相同版本范围。"""
        referenced_ids = {item.parent_id for item in records if item.parent_id}
        if not referenced_ids:
            return
        async with self._session_factory() as session:
            parents = await session.scalars(
                select(RagDocument).where(
                    RagDocument.knowledge_base_id == base_id,
                    RagDocument.external_id.in_(referenced_ids),
                )
            )
            parent_by_id = {item.external_id: item for item in parents}
        for item in records:
            if item.parent_id is None:
                continue
            parent = parent_by_id.get(item.parent_id)
            if parent is None:
                raise RagError(
                    "parent_record_not_found", "父记录不存在或不属于当前 collection", 404
                )
            if parent.record_kind != "document":
                raise RagError(
                    "parent_record_kind_invalid", "parent_id 必须指向 document 普通记录", 409
                )
            if (
                parent.namespace != item.namespace
                or parent.logical_document_id != item.logical_document_id
                or parent.is_published != (not item.staged)
                or parent.version_id != item.version_id
            ):
                raise RagError(
                    "parent_record_scope_conflict",
                    "父子记录必须属于相同 namespace、document_id 和发布版本",
                    409,
                )
            if parent.parent_external_id is not None:
                raise RagError("nested_parent_unsupported", "第一版父记录必须是顶层 document", 422)

    @staticmethod
    def _ensure_version_editable(document: RagDocument) -> None:
        """Reject writes to published or historically ambiguous versioned records."""
        if document.version_id is None:
            return
        if document.was_published is True:
            raise RagError(
                "published_version_immutable",
                "已发布版本不可修改；请使用新的 version_id 和外部 ID",
                409,
            )
        if document.was_published is None and not document.is_published:
            raise RagError(
                "publication_history_unknown",
                "旧隐藏版本无法判断是暂存还是已退役；请使用新的 version_id 和外部 ID 重建，"
                "或由管理员核实后修复其发布状态",
                409,
            )

    async def fetch_records(
        self,
        key: VerifiedApiKey,
        base_id: UUID,
        external_ids: list[str],
        *,
        include_staged: bool = False,
        trace_id: str,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """按 collection 外部 ID 批量返回正文；默认把暂存记录视为不存在。"""
        await self.get_knowledge_base(key.project_id, base_id)
        requested = list(dict.fromkeys(external_ids))
        async with self._session_factory() as session:
            statement = select(RagDocument).where(
                RagDocument.knowledge_base_id == base_id,
                RagDocument.external_id.in_(requested),
            )
            if not include_staged:
                statement = statement.where(RagDocument.is_published.is_(True))
            rows = await session.scalars(statement)
            by_external_id = {row.external_id: row for row in rows}
        records: list[dict[str, object]] = [
            {
                "id": external_id,
                "record_kind": by_external_id[external_id].record_kind,
                "parent_id": by_external_id[external_id].parent_external_id,
                "content": by_external_id[external_id].content,
                "metadata": by_external_id[external_id].metadata_json,
                "modality": by_external_id[external_id].modality,
            }
            for external_id in requested
            if external_id in by_external_id
        ]
        missing = [external_id for external_id in requested if external_id not in by_external_id]
        await self._operation(
            key,
            trace_id,
            "vector_store.records.fetch",
            f"批量读取记录，返回 {len(records)} 条",
        )
        return records, missing

    async def publish_version(
        self,
        key: VerifiedApiKey,
        base_id: UUID,
        *,
        namespace: str,
        logical_document_id: str,
        version_id: str,
        trace_id: str,
    ) -> dict[str, object]:
        """原子发布一组完整暂存记录，并让同一业务文档旧版本退出查询。"""
        await self._require_enabled(key.project_id)
        await self.get_knowledge_base(key.project_id, base_id)
        scope = (
            RagDocument.knowledge_base_id == base_id,
            RagDocument.namespace == namespace,
            RagDocument.logical_document_id == logical_document_id,
        )
        async with self._session_factory.begin() as session:
            # 串行化同一 collection 内的版本发布，避免并发发布不同版本后同时可见。
            collection_row = await session.scalar(
                select(RagKnowledgeBase)
                .where(
                    RagKnowledgeBase.id == base_id,
                    RagKnowledgeBase.project_id == key.project_id,
                )
                .with_for_update()
            )
            if collection_row is None:
                raise RagError("knowledge_base_not_found", "向量集合不存在", 404)
            target = list(
                await session.scalars(
                    select(RagDocument)
                    .where(*scope, RagDocument.version_id == version_id)
                    .with_for_update()
                )
            )
            if not target:
                raise RagError("staged_version_not_found", "指定版本不存在", 404)
            already_published = all(record.is_published for record in target)
            if not already_published and any(record.is_published for record in target):
                raise RagError("version_state_invalid", "目标版本包含不一致的发布状态", 409)
            if any(
                not record.is_published and record.was_published is None for record in target
            ):
                raise RagError(
                    "publication_history_unknown",
                    "旧隐藏版本无法判断是暂存还是已退役；请使用新的 version_id 和外部 ID 重建，"
                    "或由管理员核实后修复其发布状态",
                    409,
                )
            if not already_published and any(record.was_published for record in target):
                raise RagError(
                    "published_version_retired",
                    "已被新版本替代的历史版本不可重新发布；请写入新的 version_id",
                    409,
                )
            if not already_published:
                old_roots = await session.scalars(
                    select(RagDocument)
                    .where(
                        *scope,
                        RagDocument.is_published.is_(True),
                        RagDocument.parent_external_id.is_(None),
                    )
                    .with_for_update()
                )
                for record in old_roots:
                    record.is_published = False
                await session.flush()
                old_children = await session.scalars(
                    select(RagDocument)
                    .where(
                        *scope,
                        RagDocument.is_published.is_(True),
                        RagDocument.parent_external_id.is_not(None),
                    )
                    .with_for_update()
                )
                for record in old_children:
                    record.is_published = False
                await session.flush()

                new_roots = [record for record in target if record.parent_external_id is None]
                new_children = [
                    record for record in target if record.parent_external_id is not None
                ]
                for record in new_roots:
                    record.is_published = True
                    record.was_published = True
                await session.flush()
                for record in new_children:
                    record.is_published = True
                    record.was_published = True
                await session.flush()
            published_count = len(target)
        await self._operation(
            key,
            trace_id,
            "vector_store.version.publish",
            f"发布文档版本 {version_id}，记录数 {published_count}",
        )
        return {
            "version_id": version_id,
            "published_records": published_count,
            "already_published": already_published,
        }

    async def update_knowledge_base_settings(
        self,
        key: VerifiedApiKey,
        base_id: UUID,
        *,
        route_prefix: str | None,
        max_record_chars: int,
        trace_id: str,
    ) -> RagKnowledgeBase:
        """修改集合记录长度上限和模型路由前缀，不更换模型名或重嵌已有记录。

        ``route_prefix`` 为空时恢复自动路由；切换路由只影响之后生成的查询/向量，
        调用方必须确认既有向量仍与目标上游兼容，或自行重建向量。
        """
        await self._require_enabled(key.project_id)
        normalized_prefix: str | None = None
        if route_prefix is not None and route_prefix.strip():
            normalized_prefix = route_prefix.strip().lower()
            if not re.fullmatch(r"[a-z0-9._-]{1,64}", normalized_prefix):
                raise RagError("invalid_route_prefix", "模型路由前缀格式不合法", 422)
        if not 100 <= max_record_chars <= 6000:
            raise RagError("invalid_record_limit", "单条记录最大字符数必须在 100 到 6000 之间", 422)

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
            base.embedding_model = self._model_with_route_prefix(
                base.embedding_model, normalized_prefix
            )
            base.chunk_size = max_record_chars
            # Keep the legacy document-chunking path progressing sensibly if
            # its hidden overlap is larger than the newly selected record size.
            base.chunk_overlap = min(base.chunk_overlap, max_record_chars // 5)
            base.updated_at = datetime.now(UTC)
            await session.flush()
            await session.refresh(base)
            updated = base

        await self._operation(
            key,
            trace_id,
            "rag.knowledge_base.update_settings",
            "修改向量集合设置：单条记录长度上限和路由前缀",
        )
        return updated

    @staticmethod
    def _model_with_route_prefix(model: str, route_prefix: str | None) -> str:
        """只替换第一个斜线前的路由前缀，保留原模型标识。"""
        _existing_prefix, separator, model_name = model.partition("/")
        if not separator:
            model_name = model
        return f"{route_prefix}/{model_name}" if route_prefix else model_name

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
        """批量写入普通正文与向量记录；Embedding 在 DB 替换前完成，记录变更单事务提交。"""
        await self._require_enabled(key.project_id)
        if not vectors:
            raise RagError("empty_upsert", "至少需要一条向量记录", 422)
        external_ids = [item.external_id for item in vectors]
        if len(external_ids) != len(set(external_ids)):
            raise RagError("duplicate_vector_id", "同一批次中的向量记录 ID 不能重复", 422)
        base = await self.get_knowledge_base(key.project_id, base_id)
        version_keys = list(
            dict.fromkeys(
                (item.namespace, item.logical_document_id, item.version_id)
                for item in vectors
                if item.staged and item.namespace and item.logical_document_id and item.version_id
            )
        )
        async with self._session_factory() as session:
            existing_rows = await session.scalars(
                select(RagDocument).where(
                    RagDocument.knowledge_base_id == base_id,
                    RagDocument.external_id.in_(external_ids),
                )
            )
            existing_by_id = {item.external_id: item for item in existing_rows}
            for item in vectors:
                existing = existing_by_id.get(item.external_id)
                if existing is not None:
                    same_published_scope = (
                        existing.was_published is True
                        and existing.namespace == item.namespace
                        and existing.logical_document_id == item.logical_document_id
                        and existing.version_id == item.version_id
                    )
                    if same_published_scope:
                        # The exact replay check decides whether this is a no-op or mutation.
                        continue
                    self._ensure_version_editable(existing)
            existing_parent_ids = [
                item.external_id
                for item in vectors
                if item.external_id in existing_by_id
                and existing_by_id[item.external_id].record_kind == "document"
            ]
            existing_children: list[RagDocument] = (
                list(
                    await session.scalars(
                        select(RagDocument).where(
                            RagDocument.knowledge_base_id == base_id,
                            RagDocument.parent_external_id.in_(existing_parent_ids),
                        )
                    )
                )
                if existing_parent_ids
                else []
            )
            children_by_parent: dict[str, list[RagDocument]] = {}
            for child in existing_children:
                if child.parent_external_id:
                    children_by_parent.setdefault(child.parent_external_id, []).append(child)
            ambiguous_version_rows = (
                list(
                    await session.scalars(
                        select(RagDocument).where(
                            RagDocument.knowledge_base_id == base_id,
                            RagDocument.is_published.is_(False),
                            RagDocument.was_published.is_(None),
                            tuple_(
                                RagDocument.namespace,
                                RagDocument.logical_document_id,
                                RagDocument.version_id,
                            ).in_(version_keys),
                        )
                    )
                )
                if version_keys
                else []
            )
            if ambiguous_version_rows:
                raise RagError(
                    "publication_history_unknown",
                    "该版本含迁移前状态不明的隐藏记录；请使用新的 version_id 和外部 ID 重建，"
                    "或由管理员核实后修复其发布状态",
                    409,
                )
            published_version_rows = (
                list(
                    await session.scalars(
                        select(RagDocument).where(
                            RagDocument.knowledge_base_id == base_id,
                            RagDocument.was_published.is_(True),
                            tuple_(
                                RagDocument.namespace,
                                RagDocument.logical_document_id,
                                RagDocument.version_id,
                            ).in_(version_keys),
                        )
                    )
                )
                if version_keys
                else []
            )
        published_version_keys = {
            (item.namespace, item.logical_document_id, item.version_id)
            for item in published_version_rows
        }
        if published_version_keys:
            if (
                published_version_keys != set(version_keys)
                or any(not item.staged or item.version_id is None for item in vectors)
            ):
                raise RagError(
                    "published_version_immutable",
                    "该批次混合了已发布版本或未版本记录；请按已发布版本原样重放，"
                    "修改内容需使用新 version_id",
                    409,
                )
            replayed: list[RagDocumentInfo] = []
            for item in vectors:
                document = existing_by_id.get(item.external_id)
                if document is None or not self._same_published_record(document, item):
                    raise RagError(
                        "published_version_immutable",
                        "已发布版本只允许完全相同的幂等重放；修改内容请使用新的 version_id",
                        409,
                    )
                replayed.append(
                    RagDocumentInfo(
                        document.id,
                        document.external_id,
                        document.title,
                        1 if document.record_kind == "vector" else 0,
                        document.created_at,
                    )
                )
            await self._operation(
                key,
                trace_id,
                "rag.vector.upsert",
                f"幂等重放已发布版本，记录数 {len(replayed)}",
            )
            return replayed
        await self._validate_parent_records(key.project_id, base_id, vectors)
        if any(
            existing_by_id.get(item.external_id) is not None
            and existing_by_id[item.external_id].record_kind != item.record_kind
            for item in vectors
        ):
            raise RagError(
                "record_kind_conflict",
                "同一外部 ID 不能在 vector 与 document 类型之间转换",
                409,
            )
        for item in vectors:
            if item.record_kind != "document" or item.external_id not in children_by_parent:
                continue
            target_state = (
                item.namespace,
                item.logical_document_id,
                item.version_id,
                not item.staged,
            )
            if any(
                (
                    child.namespace,
                    child.logical_document_id,
                    child.version_id,
                    child.is_published,
                )
                != target_state
                for child in children_by_parent[item.external_id]
            ):
                raise RagError(
                    "parent_record_in_use",
                    "父记录已被子记录引用，不能直接改变其发布范围；请使用新版本 ID 并调用 publish",
                    409,
                )

        embeddings: list[list[float] | None] = [None] * len(vectors)
        vector_indexes = [
            index for index, item in enumerate(vectors) if item.record_kind == "vector"
        ]
        text_indexes = [
            index
            for index, item in enumerate(vectors)
            if item.record_kind == "vector" and item.modality == "text"
        ]
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
            if item.record_kind != "vector" or item.modality == "text":
                continue
            response = await self._embedding_gateway.embed(
                key,
                base.embedding_model,
                item.embedding_inputs,
                None,
                f"emb_{uuid4().hex}",
            )
            self._assign_batch_embeddings(response, [index], embeddings)

        resolved_embeddings = [embeddings[index] for index in vector_indexes]
        if any(vector is None for vector in resolved_embeddings):
            raise RagError("embedding_response_invalid", "Embedding 响应缺少向量", 502)
        first_embedding = resolved_embeddings[0] if resolved_embeddings else None
        dimensions = len(first_embedding) if first_embedding is not None else None
        concrete_embeddings = [vector for vector in resolved_embeddings if vector is not None]
        if dimensions is not None and any(
            len(vector) != dimensions for vector in concrete_embeddings
        ):
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
                if version_keys:
                    concurrent_published = await session.scalar(
                        select(RagDocument.id)
                        .where(
                            RagDocument.knowledge_base_id == base_id,
                            RagDocument.was_published.is_(True),
                            tuple_(
                                RagDocument.namespace,
                                RagDocument.logical_document_id,
                                RagDocument.version_id,
                            ).in_(version_keys),
                        )
                        .limit(1)
                    )
                    if concurrent_published is not None:
                        raise RagError(
                            "published_version_immutable",
                            "版本在本次写入期间已发布；请原样重试已存在记录，"
                            "修改内容需使用新 version_id",
                            409,
                        )
                if dimensions is not None and (
                    locked_base.vector_dimensions is not None
                    and locked_base.vector_dimensions != dimensions
                ):
                    raise RagError(
                        "embedding_dimensions_conflict",
                        "该集合已有不同向量维度的数据，请使用原模型或新建集合",
                        409,
                    )
                if dimensions is not None:
                    locked_base.vector_dimensions = dimensions

                existing_rows = await session.scalars(
                    select(RagDocument)
                    .where(
                        RagDocument.knowledge_base_id == base_id,
                        RagDocument.external_id.in_(external_ids),
                    )
                    .order_by(RagDocument.external_id)
                    .with_for_update()
                )
                existing_by_external_id = {
                    document.external_id: document for document in existing_rows
                }
                if any(
                    document.record_kind != item.record_kind
                    for item in vectors
                    if (document := existing_by_external_id.get(item.external_id)) is not None
                ):
                    raise RagError(
                        "record_kind_conflict",
                        "同一外部 ID 不能在 vector 与 document 类型之间转换",
                        409,
                    )
                for item in vectors:
                    document = existing_by_external_id.get(item.external_id)
                    if document is not None:
                        self._ensure_version_editable(document)

                chunks: list[RagChunk] = []
                for index, item in enumerate(vectors):
                    document = existing_by_external_id.get(item.external_id)
                    is_new_document = document is None
                    same_version_scope = (
                        not is_new_document
                        and document is not None
                        and document.namespace == item.namespace
                        and document.logical_document_id == item.logical_document_id
                        and document.version_id == item.version_id
                    )
                    if is_new_document:
                        document = RagDocument(
                            id=uuid4(),
                            knowledge_base_id=base_id,
                            external_id=item.external_id,
                            record_kind=item.record_kind,
                            created_by=key.user_id or key.owner_id,
                            was_published=not item.staged,
                        )
                        session.add(document)
                    else:
                        assert document is not None
                        await session.execute(
                            delete(RagChunk).where(RagChunk.document_id == document.id)
                        )
                    assert document is not None
                    document.record_kind = item.record_kind
                    document.parent_external_id = item.parent_id
                    document.namespace = item.namespace
                    document.logical_document_id = item.logical_document_id
                    document.version_id = item.version_id
                    document.is_published = not item.staged
                    if not same_version_scope:
                        document.was_published = not item.staged
                    else:
                        document.was_published = document.was_published or not item.staged
                    document.modality = item.modality
                    document.title = item.title
                    document.content = item.content
                    document.metadata_json = item.metadata
                    document.updated_at = datetime.now(UTC)
                    inserted_documents.append(document)
                    embedding = embeddings[index]
                    if item.record_kind == "vector":
                        assert embedding is not None and dimensions is not None
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
                await session.flush()
                session.add_all(chunks)
                await session.flush()
        except RagError:
            raise

        await self._operation(
            key,
            trace_id,
            "rag.vector.upsert",
            f"批量写入记录，数量 {len(inserted_documents)}",
        )
        return [
            RagDocumentInfo(
                document.id,
                document.external_id,
                document.title,
                1 if document.record_kind == "vector" else 0,
                document.created_at,
            )
            for document in inserted_documents
        ]

    @staticmethod
    def _same_published_record(document: RagDocument, item: RagVectorUpsert) -> bool:
        """已发布版本只接受精确重放，避免稳定 ID 被重试请求重新隐藏或覆盖。"""
        return (
            document.was_published is True
            and document.record_kind == item.record_kind
            and document.parent_external_id == item.parent_id
            and document.namespace == item.namespace
            and document.logical_document_id == item.logical_document_id
            and document.version_id == item.version_id
            and document.modality == item.modality
            and document.title == item.title
            and document.content == item.content
            and document.metadata_json == item.metadata
        )

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
        self._ensure_version_editable(current_document)
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
                embedding_inputs = [{"type": media_type, media_type: {"url": updated_media_url}}]
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
            document = await session.scalar(
                select(RagDocument)
                .where(
                    RagDocument.id == source_document_id,
                    RagDocument.knowledge_base_id == base_id,
                )
                .with_for_update()
            )
            if document is None:
                raise RagError("vector_not_found", "向量记录不存在", 404)
            self._ensure_version_editable(document)
            vector = await session.scalar(
                select(RagChunk)
                .where(
                    RagChunk.id == vector_id,
                    RagChunk.document_id == document.id,
                    RagChunk.knowledge_base_id == base_id,
                )
                .with_for_update()
            )
            if vector is None:
                raise RagError("vector_not_found", "向量记录不存在", 404)
            vector.content = normalized_content
            vector.metadata_json = metadata
            if embedding is not None:
                vector.embedding = embedding
            updated_at = datetime.now(UTC)
            vector.updated_at = updated_at
            chunk_count = await session.scalar(
                select(func.count())
                .select_from(RagChunk)
                .where(RagChunk.document_id == vector.document_id)
            )
            if document is not None and document.external_id is not None and chunk_count == 1:
                # Generic external-ID records have one chunk, so fetch and query must agree.
                # Legacy document-level records with multiple chunks keep their full body intact.
                document.content = normalized_content
                document.metadata_json = metadata
                document.updated_at = updated_at
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
            document_id = await session.scalar(
                select(RagChunk.document_id)
                .join(RagKnowledgeBase)
                .where(
                    RagChunk.id == vector_id,
                    RagChunk.knowledge_base_id == base_id,
                    RagKnowledgeBase.project_id == key.project_id,
                )
            )
            if document_id is None:
                raise RagError("vector_not_found", "向量记录不存在", 404)
            document = await session.scalar(
                select(RagDocument)
                .where(
                    RagDocument.id == document_id,
                    RagDocument.knowledge_base_id == base_id,
                )
                .with_for_update()
            )
            if document is None:
                raise RagError("vector_not_found", "向量记录不存在", 404)
            vector = await session.scalar(
                select(RagChunk)
                .where(
                    RagChunk.id == vector_id,
                    RagChunk.document_id == document.id,
                    RagChunk.knowledge_base_id == base_id,
                )
                .with_for_update()
            )
            if vector is None:
                raise RagError("vector_not_found", "向量记录不存在", 404)
            await session.delete(vector)
            await session.flush()
            remaining = await session.scalar(
                select(func.count())
                .select_from(RagChunk)
                .where(RagChunk.document_id == document_id)
            )
            if remaining == 0:
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

    async def delete_records(
        self,
        key: VerifiedApiKey,
        base_id: UUID,
        external_ids: list[str],
        trace_id: str,
    ) -> dict[str, object]:
        """幂等批量删除外部记录；被未选子记录引用的父记录返回 409。"""
        await self._require_enabled(key.project_id)
        requested = list(dict.fromkeys(external_ids))
        if not requested:
            raise RagError("empty_delete", "至少需要一个记录 ID", 422)
        async with self._session_factory.begin() as session:
            records = list(
                await session.scalars(
                    select(RagDocument)
                    .where(
                        RagDocument.knowledge_base_id == base_id,
                        RagDocument.external_id.in_(requested),
                    )
                    .order_by(RagDocument.external_id)
                    .with_for_update()
                )
            )
            found_ids: set[str] = {
                record.external_id for record in records if record.external_id is not None
            }
            referenced: list[str | None] = (
                list(
                    await session.scalars(
                        select(RagDocument.external_id).where(
                            RagDocument.knowledge_base_id == base_id,
                            RagDocument.parent_external_id.in_(found_ids),
                            RagDocument.external_id.not_in(found_ids),
                        )
                    )
                )
                if found_ids
                else []
            )
            if list(referenced):
                raise RagError(
                    "parent_record_in_use",
                    "父记录仍被未同时删除的子记录引用",
                    409,
                )
            child_records = [record for record in records if record.parent_external_id is not None]
            parent_records = [record for record in records if record.parent_external_id is None]
            for record in child_records:
                await session.delete(record)
            await session.flush()
            for record in parent_records:
                await session.delete(record)
            await session.flush()
            deleted_ids = [record.external_id for record in records if record.external_id]
        await self._operation(
            key,
            trace_id,
            "vector_store.records.delete",
            f"批量删除记录，数量 {len(deleted_ids)}",
        )
        return {
            "deleted_ids": deleted_ids,
            "missing_ids": [item for item in requested if item not in found_ids],
        }

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
                    RagDocument.external_id,
                    RagDocument.parent_external_id,
                    RagChunk.embedding.cosine_distance(vector).label("distance"),
                )
                .join(RagDocument, RagDocument.id == RagChunk.document_id)
                .where(
                    RagChunk.knowledge_base_id == base_id,
                    RagChunk.dimensions == len(vector),
                    RagDocument.is_published.is_(True),
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
                chunk, title, chunk_doc_external_id, parent_id, distance = cast(
                    tuple[RagChunk, str, str | None, str | None, float], row
                )
                results.append(
                    {
                        "chunk_id": str(chunk.id),
                        "id": chunk_doc_external_id,
                        "record_kind": "vector",
                        "parent_id": parent_id,
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
