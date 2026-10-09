"""向量数据库 API 的预切片写入契约测试。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Request
from pydantic import ValidationError

from app.services.embedding.rag import RagDocumentInfo, RagVectorUpsert
from app.services.embedding.vector_api import UpsertRequest, VectorDocument, upsert_vectors


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
                    chunks=1,
                    created_at=datetime.now(UTC),
                )
                for vector in vectors
            ]

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
        }
    )
    request.state.trace_id = "trace-test"
    payload = UpsertRequest(
        vectors=[
            VectorDocument(id="faq-1", content="第一段", metadata={"kind": "faq"}),
            VectorDocument(id="faq-2", content="第二段", metadata={"kind": "faq"}),
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
    assert [vector.content for vector in captured] == ["第一段", "第二段"]
    assert [vector.embedding_inputs for vector in captured] == [
        ["第一段"],
        ["第二段"],
    ]
    assert response["collection"] == "support"
    assert len(response["upserted"]) == 2  # type: ignore[arg-type]
