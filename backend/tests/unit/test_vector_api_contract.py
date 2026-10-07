"""向量数据库 API 的预切片写入契约测试。"""

import pytest
from pydantic import ValidationError

from app.services.embedding.vector_api import UpsertRequest, VectorDocument


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
