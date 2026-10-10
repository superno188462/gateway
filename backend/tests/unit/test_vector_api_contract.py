"""向量数据库 API 的混合记录写入契约测试。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.services.embedding.rag import RagDocumentInfo, RagVectorUpsert
from app.services.embedding.vector_api import (
    FetchRequest,
    PublishVersionRequest,
    UpsertRequest,
    VectorDocument,
    upsert_vectors,
)


def test_vector_record_accepts_one_pre_split_text_and_metadata() -> None:
    record = VectorDocument(
        id="faq-1-002",
        content="单条已切分文本片段",
        metadata={"category": "faq", "source": "faq-1"},
    )

    assert record.id == "faq-1-002"
    assert record.content == "单条已切分文本片段"
    assert record.metadata["source"] == "faq-1"


def test_vector_record_rejects_blank_content() -> None:
    with pytest.raises(ValidationError, match="content 不能为空"):
        VectorDocument(id="faq-1", content="  ")


def test_upsert_rejects_duplicate_record_ids_in_one_batch() -> None:
    with pytest.raises(ValidationError, match="id 不能重复"):
        UpsertRequest(
            vectors=[
                VectorDocument(id="faq-1", content="第一段"),
                VectorDocument(id="faq-1", content="第二段"),
            ]
        )


def test_upsert_accepts_legacy_documents_field_as_pre_split_records() -> None:
    payload = UpsertRequest.model_validate({"documents": [{"id": "faq-1", "content": "单条切片"}]})

    assert len(payload.vectors) == 1
    assert payload.vectors[0].content == "单条切片"


def test_vector_record_cannot_exceed_global_maximum() -> None:
    with pytest.raises(ValidationError):
        VectorDocument(id="faq-1", content="x" * 6001)


def test_vector_record_accepts_remote_image_and_video() -> None:
    image = VectorDocument(
        id="image-1",
        modality="image",
        media_url="https://cdn.example.com/image.png",
        metadata={"category": "product"},
    )
    video = VectorDocument(
        id="video-1",
        modality="video",
        media_url="https://cdn.example.com/clip.mp4",
        video_options={"fps": 1, "max_video_tokens": 10_240},
    )

    assert image.modality == "image"
    assert video.modality == "video"
    assert video.video_options == {"fps": 1, "max_video_tokens": 10_240}
    with pytest.raises(ValidationError):
        VectorDocument(id="video-bad", modality="video", media_url="https://cdn.example.com/a.webm")


def test_document_record_skips_vector_fields_and_allows_parent_content_limit() -> None:
    record = VectorDocument(
        id="parent-1",
        record_kind="document",
        content="父块正文" * 4000,
        metadata={"source": "guide.md"},
        namespace="knowledge",
        document_id="doc-1",
        version_id="v2",
        staged=True,
    )

    assert len(record.content or "") == 16_000
    assert record.record_kind == "document"
    assert record.staged
    with pytest.raises(ValidationError):
        VectorDocument(id="too-long", record_kind="document", content="x" * 20_001)
    with pytest.raises(ValidationError, match="必须填写 namespace"):
        VectorDocument(id="staged-bad", record_kind="document", content="正文", staged=True)
    with pytest.raises(ValidationError, match="必须先暂存"):
        VectorDocument(
            id="published-directly",
            record_kind="document",
            content="正文",
            namespace="knowledge",
            document_id="doc-1",
            version_id="v3",
        )
    with pytest.raises(ValidationError, match="顶层父记录"):
        VectorDocument(
            id="nested-parent",
            record_kind="document",
            content="正文",
            parent_id="other-parent",
        )


def test_vector_child_requires_parent_and_version_references_outside_batch() -> None:
    parent = VectorDocument(
        id="parent-1",
        record_kind="document",
        content="完整父块",
        namespace="knowledge",
        document_id="doc-1",
        version_id="v1",
        staged=True,
    )
    child = VectorDocument(
        id="child-1",
        parent_id="parent-1",
        content="子块",
        namespace="knowledge",
        document_id="doc-1",
        version_id="v1",
        staged=True,
    )
    assert child.record_kind == "vector"
    unversioned_parent = VectorDocument(
        id="parent-immediate",
        record_kind="document",
        content="即时父块",
    )
    unversioned_child = VectorDocument(
        id="child-immediate",
        parent_id="parent-immediate",
        content="即时子片",
    )
    assert unversioned_parent.version_id is None
    assert unversioned_child.parent_id == "parent-immediate"
    with pytest.raises(ValidationError, match="先单独写入"):
        UpsertRequest(vectors=[parent, child])
    with pytest.raises(ValidationError, match="document 普通记录"):
        UpsertRequest(
            vectors=[
                VectorDocument(
                    id="parent-2", record_kind="document", content="父块", modality="image", media_url="https://cdn.example.com/a.png"
                )
            ]
        )


def test_fetch_and_publish_bound_the_external_id_contract() -> None:
    assert FetchRequest(ids=["parent-1", "child-1"]).ids == ["parent-1", "child-1"]
    assert PublishVersionRequest(
        namespace="knowledge", document_id="doc-1", version_id="v2"
    ).version_id == "v2"


@pytest.mark.asyncio
async def test_upsert_delegates_text_records_as_one_batch() -> None:
    collection_id = uuid4()
    project_id = uuid4()
    captured: list[RagVectorUpsert] = []

    class FakeService:
        async def list_knowledge_bases(self, requested_project_id: object) -> list[object]:
            assert requested_project_id == project_id
            return [
                type(
                    "Collection",
                    (),
                    {"id": collection_id, "name": "support", "chunk_size": 1000},
                )()
            ]

        async def upsert_vectors(
            self,
            _key: object,
            _base_id: object,
            vectors: list[RagVectorUpsert],
            *,
            trace_id: str,
        ) -> list[RagDocumentInfo]:
            captured.extend(vectors)
            assert trace_id == "trace-test"
            return [
                RagDocumentInfo(
                    id=uuid4(),
                    external_id=vector.external_id,
                    title=vector.title,
                    chunks=1 if vector.record_kind == "vector" else 0,
                    created_at=datetime.now(UTC),
                )
                for vector in vectors
            ]

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/vector-stores/collections/support/upsert",
            "raw_path": b"/v1/vector-stores/collections/support/upsert",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        },
        receive,
    )
    request.state.trace_id = "trace-test"
    payload = UpsertRequest(
        vectors=[
            VectorDocument(id="faq-1", content="第一段", metadata={"kind": "faq"}),
            VectorDocument(
                id="parent-1",
                record_kind="document",
                content="父块正文",
                metadata={"kind": "parent"},
            ),
        ]
    )

    response = await upsert_vectors(
        "support",
        payload,
        request,
        type("Key", (), {"project_id": project_id})(),
        FakeService(),  # type: ignore[arg-type]
    )

    assert len(captured) == 2
    assert [vector.content for vector in captured] == ["第一段", "父块正文"]
    assert [vector.embedding_inputs for vector in captured] == [
        ["第一段"],
        [],
    ]
    assert [vector.record_kind for vector in captured] == ["vector", "document"]
    assert response["collection"] == "support"
    assert len(response["upserted"]) == 2  # type: ignore[arg-type]
    assert response["upserted"][1]["vectors"] == 0  # type: ignore[index]


@pytest.mark.asyncio
async def test_upsert_rejects_wire_body_over_four_mib_before_collection_access() -> None:
    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": b" " * (4 * 1024 * 1024 + 1),
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/vector-stores/collections/support/upsert",
            "raw_path": b"/v1/vector-stores/collections/support/upsert",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        },
        receive,
    )
    request.state.trace_id = "trace-large-body"

    class UnusedService:
        async def list_knowledge_bases(self, _project_id: object) -> list[object]:
            pytest.fail("大请求体不能继续访问 collection")

    with pytest.raises(HTTPException) as error:
        await upsert_vectors(
            "support",
            UpsertRequest(vectors=[VectorDocument(id="one", content="text")]),
            request,
            type("Key", (), {"project_id": uuid4()})(),
            UnusedService(),  # type: ignore[arg-type]
        )
    assert error.value.status_code == 413
