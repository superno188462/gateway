"""普通记录读取、父子引用和删除事务的服务契约测试。"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.embedding.rag import RagError, RagKnowledgeService, RagVectorUpsert


class _Session:
    def __init__(self, scalar_results: list[list[object]], scalar_result: object = None) -> None:
        self.scalar_results = iter(scalar_results)
        self.scalar_result = scalar_result
        self.deleted: list[object] = []
        self.flush_count = 0

    async def scalars(self, _statement: object) -> list[object]:
        return next(self.scalar_results)

    async def scalar(self, _statement: object) -> object:
        return self.scalar_result

    async def execute(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: [])

    async def delete(self, record: object) -> None:
        self.deleted.append(record)

    async def flush(self) -> None:
        self.flush_count += 1


class _SessionFactory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> "_SessionContext":
        return _SessionContext(self.session)

    def begin(self) -> "_SessionContext":
        return _SessionContext(self.session)


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_child_must_reference_document_in_the_same_version_scope() -> None:
    parent = SimpleNamespace(
        external_id="parent",
        record_kind="document",
        modality="text",
        namespace="knowledge",
        logical_document_id="doc-1",
        version_id="v1",
        is_published=False,
        parent_external_id=None,
    )
    service = RagKnowledgeService(
        _SessionFactory(_Session([[parent]])),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    child = RagVectorUpsert(
        external_id="child",
        title="child",
        content="chunk",
        metadata={},
        modality="text",
        embedding_inputs=["chunk"],
        parent_id="parent",
        namespace="knowledge",
        logical_document_id="doc-1",
        version_id="v2",
        staged=True,
    )

    with pytest.raises(RagError) as error:
        await service._validate_parent_records(uuid4(), uuid4(), [child])

    assert error.value.code == "parent_record_scope_conflict"


@pytest.mark.asyncio
async def test_unversioned_child_can_reference_unversioned_published_parent() -> None:
    parent = SimpleNamespace(
        external_id="parent",
        record_kind="document",
        modality="text",
        namespace=None,
        logical_document_id=None,
        version_id=None,
        is_published=True,
        parent_external_id=None,
    )
    service = RagKnowledgeService(
        _SessionFactory(_Session([[parent]])),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    child = RagVectorUpsert(
        external_id="child",
        title="child",
        content="chunk",
        metadata={},
        modality="text",
        embedding_inputs=["chunk"],
        parent_id="parent",
    )

    await service._validate_parent_records(uuid4(), uuid4(), [child])


@pytest.mark.asyncio
async def test_published_staged_version_replay_is_idempotent_and_immutable() -> None:
    now = datetime.now(UTC)
    existing = SimpleNamespace(
        id=uuid4(),
        external_id="child-v1",
        title="child",
        record_kind="vector",
        parent_external_id=None,
        namespace="knowledge",
        logical_document_id="doc-1",
        version_id="v1",
        modality="text",
        content="chunk",
        metadata_json={"source": "guide"},
        was_published=True,
        created_at=now,
    )

    def make_service() -> tuple[RagKnowledgeService, AsyncMock]:
        session = _Session([[existing], [], [existing]])
        gateway = SimpleNamespace(embed=AsyncMock())
        service = RagKnowledgeService(
            _SessionFactory(session),  # type: ignore[arg-type]
            gateway,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
        )
        service._require_enabled = AsyncMock()  # type: ignore[method-assign]
        service.get_knowledge_base = AsyncMock(
            return_value=SimpleNamespace(embedding_model="example/embed-v1")
        )  # type: ignore[method-assign]
        service._operation = AsyncMock()  # type: ignore[method-assign]
        return service, gateway.embed

    key = SimpleNamespace(project_id=uuid4(), owner_id=uuid4(), user_id=None)
    replay = RagVectorUpsert(
        external_id="child-v1",
        title="child",
        content="chunk",
        metadata={"source": "guide"},
        modality="text",
        embedding_inputs=["chunk"],
        namespace="knowledge",
        logical_document_id="doc-1",
        version_id="v1",
        staged=True,
    )
    service, embed = make_service()
    result = await service.upsert_vectors(
        key, uuid4(), [replay], trace_id="trace-replay"  # type: ignore[arg-type]
    )
    assert result[0].external_id == "child-v1"
    embed.assert_not_awaited()

    changed_service, changed_embed = make_service()
    changed = replace(replay, content="changed", embedding_inputs=["changed"])
    with pytest.raises(RagError) as error:
        await changed_service.upsert_vectors(
            key, uuid4(), [changed], trace_id="trace-replay-changed"  # type: ignore[arg-type]
        )
    assert error.value.code == "published_version_immutable"
    changed_embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_rejects_ambiguous_legacy_hidden_version_before_embedding() -> None:
    ambiguous = SimpleNamespace(
        external_id="child-v1",
        record_kind="vector",
        version_id="v1",
        namespace="knowledge",
        logical_document_id="doc-1",
        is_published=False,
        was_published=None,
    )
    gateway = SimpleNamespace(embed=AsyncMock())
    service = RagKnowledgeService(
        _SessionFactory(_Session([[ambiguous], [ambiguous]])),  # type: ignore[arg-type]
        gateway,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    service.get_knowledge_base = AsyncMock(
        return_value=SimpleNamespace(embedding_model="example/embed-v1")
    )  # type: ignore[method-assign]
    key = SimpleNamespace(project_id=uuid4(), owner_id=uuid4(), user_id=None)
    record = RagVectorUpsert(
        external_id="child-v1",
        title="child",
        content="chunk",
        metadata={},
        modality="text",
        embedding_inputs=["chunk"],
        namespace="knowledge",
        logical_document_id="doc-1",
        version_id="v1",
        staged=True,
    )

    with pytest.raises(RagError) as error:
        await service.upsert_vectors(
            key, uuid4(), [record], trace_id="trace-ambiguous-replay"  # type: ignore[arg-type]
        )

    assert error.value.code == "publication_history_unknown"
    gateway.embed.assert_not_awaited()


@pytest.mark.parametrize("chunk_count", [1, 3])
@pytest.mark.asyncio
async def test_update_vector_keeps_fetch_content_consistent_without_overwriting_legacy_doc(
    chunk_count: int,
) -> None:
    project_id = uuid4()
    base = SimpleNamespace(
        id=uuid4(),
        chunk_size=1000,
        vector_dimensions=2,
        embedding_model="example/embed-v1",
    )
    document = SimpleNamespace(
        id=uuid4(),
        external_id="external-1",
        title="source",
        created_by_user_id=uuid4(),
        created_by=uuid4(),
        version_id=None,
        was_published=True,
        content="entire legacy document",
        metadata_json={"document": "metadata"},
        updated_at=None,
    )
    chunk = SimpleNamespace(
        id=uuid4(),
        document_id=document.id,
        content="old chunk",
        modality="text",
        dimensions=2,
        metadata_json={"chunk": "metadata"},
        embedding=[0.0, 1.0],
        sequence=0,
        created_at=datetime.now(UTC),
        updated_at=None,
    )

    class Session:
        def __init__(self) -> None:
            self.scalar_results = iter([document, chunk, chunk_count])
            self.scalar_statements: list[str] = []

        async def execute(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(one_or_none=lambda: (chunk, document))

        async def scalar(self, _statement: object) -> object:
            self.scalar_statements.append(str(_statement))
            return next(self.scalar_results)

        async def flush(self) -> None:
            return None

    session = Session()

    class Context:
        async def __aenter__(self) -> Session:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Factory:
        def __call__(self) -> Context:
            return Context()

        def begin(self) -> Context:
            return Context()

    gateway = SimpleNamespace(
        embed=AsyncMock(return_value={"data": [{"embedding": [0.5, 0.5]}]})
    )
    service = RagKnowledgeService(
        Factory(), gateway, None  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    service.get_knowledge_base = AsyncMock(return_value=base)  # type: ignore[method-assign]
    service._operation = AsyncMock()  # type: ignore[method-assign]

    await service.update_vector(
        SimpleNamespace(project_id=project_id),  # type: ignore[arg-type]
        base.id,
        chunk.id,
        content="updated chunk",
        metadata={"chunk": "updated"},
        trace_id="trace-update",
    )

    assert chunk.content == "updated chunk"
    assert chunk.metadata_json == {"chunk": "updated"}
    assert "rag_documents" in session.scalar_statements[0]
    assert "rag_chunks" in session.scalar_statements[1]
    if chunk_count == 1:
        assert document.content == "updated chunk"
        assert document.metadata_json == {"chunk": "updated"}
    else:
        assert document.content == "entire legacy document"
        assert document.metadata_json == {"document": "metadata"}


@pytest.mark.parametrize(
    ("is_published", "was_published", "expected_code"),
    [
        (True, True, "published_version_immutable"),
        (False, True, "published_version_immutable"),
        (False, None, "publication_history_unknown"),
    ],
    ids=["active", "retired", "legacy-hidden-unknown"],
)
@pytest.mark.asyncio
async def test_update_vector_rejects_published_and_retired_versions_before_embedding(
    is_published: bool, was_published: bool | None, expected_code: str
) -> None:
    base = SimpleNamespace(
        id=uuid4(), chunk_size=1000, vector_dimensions=2, embedding_model="example/embed-v1"
    )
    document = SimpleNamespace(
        id=uuid4(),
        external_id="child-v1",
        title="child",
        created_by=uuid4(),
        version_id="v1",
        was_published=was_published,
        is_published=is_published,
    )
    chunk = SimpleNamespace(id=uuid4(), document_id=document.id, modality="text")

    class Session:
        async def execute(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(one_or_none=lambda: (chunk, document))

    session = Session()

    class Context:
        async def __aenter__(self) -> Session:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Factory:
        begin_calls = 0

        def __call__(self) -> Context:
            return Context()

        def begin(self) -> Context:
            self.begin_calls += 1
            return Context()

    factory = Factory()
    gateway = SimpleNamespace(embed=AsyncMock())
    service = RagKnowledgeService(
        factory, gateway, None  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    service.get_knowledge_base = AsyncMock(return_value=base)  # type: ignore[method-assign]

    with pytest.raises(RagError) as error:
        await service.update_vector(
            SimpleNamespace(project_id=uuid4()),  # type: ignore[arg-type]
            base.id,
            chunk.id,
            content="edited",
            metadata={},
            trace_id="trace-protected-patch",
        )

    assert error.value.code == expected_code
    gateway.embed.assert_not_awaited()
    assert factory.begin_calls == 0


@pytest.mark.asyncio
async def test_update_vector_rechecks_published_state_after_acquiring_write_locks() -> None:
    base = SimpleNamespace(
        id=uuid4(), chunk_size=1000, vector_dimensions=2, embedding_model="example/embed-v1"
    )
    before_publish = SimpleNamespace(
        id=uuid4(),
        external_id="child-v1",
        title="child",
        created_by=uuid4(),
        version_id="v1",
        was_published=False,
        is_published=False,
        metadata_json={},
    )
    after_publish = SimpleNamespace(
        id=before_publish.id,
        version_id="v1",
        was_published=True,
        is_published=True,
    )
    chunk = SimpleNamespace(
        id=uuid4(),
        document_id=before_publish.id,
        content="original",
        modality="text",
        dimensions=2,
        metadata_json={},
        embedding=[0.0, 1.0],
        sequence=0,
        created_at=datetime.now(UTC),
        updated_at=None,
    )

    class Session:
        def __init__(self) -> None:
            self.scalar_results = iter([after_publish, chunk])

        async def execute(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(one_or_none=lambda: (chunk, before_publish))

        async def scalar(self, _statement: object) -> object:
            return next(self.scalar_results)

    session = Session()

    class Context:
        async def __aenter__(self) -> Session:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Factory:
        def __call__(self) -> Context:
            return Context()

        def begin(self) -> Context:
            return Context()

    gateway = SimpleNamespace(
        embed=AsyncMock(return_value={"data": [{"embedding": [0.5, 0.5]}]})
    )
    service = RagKnowledgeService(
        Factory(), gateway, None  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    service.get_knowledge_base = AsyncMock(return_value=base)  # type: ignore[method-assign]

    with pytest.raises(RagError) as error:
        await service.update_vector(
            SimpleNamespace(project_id=uuid4()),  # type: ignore[arg-type]
            base.id,
            chunk.id,
            content="edited",
            metadata={},
            trace_id="trace-protected-patch-race",
        )

    assert error.value.code == "published_version_immutable"
    gateway.embed.assert_awaited_once()
    assert chunk.content == "original"


@pytest.mark.asyncio
async def test_fetch_records_preserves_deduplicated_input_order_and_missing_ids() -> None:
    first = SimpleNamespace(
        external_id="parent-b",
        record_kind="document",
        modality="text",
        parent_external_id=None,
        content="B",
        metadata_json={"order": 2},
    )
    second = SimpleNamespace(
        external_id="parent-a",
        record_kind="document",
        modality="text",
        parent_external_id=None,
        content="A",
        metadata_json={"order": 1},
    )
    service = RagKnowledgeService(
        _SessionFactory(_Session([[second, first]])),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service.get_knowledge_base = AsyncMock()  # type: ignore[method-assign]
    service._operation = AsyncMock()  # type: ignore[method-assign]
    key = SimpleNamespace(project_id=uuid4())

    records, missing = await service.fetch_records(
        key, uuid4(), ["parent-b", "missing", "parent-a", "parent-b"], trace_id="trace-fetch"  # type: ignore[arg-type]
    )

    assert [record["id"] for record in records] == ["parent-b", "parent-a"]
    assert missing == ["missing"]
    assert records[0]["metadata"] == {"order": 2}
    assert "embedding" not in records[0]


@pytest.mark.asyncio
async def test_delete_records_allows_child_and_parent_in_same_batch() -> None:
    parent = SimpleNamespace(external_id="p", parent_external_id=None)
    child = SimpleNamespace(external_id="c", parent_external_id="p")
    session = _Session([[parent, child], []])
    service = RagKnowledgeService(
        _SessionFactory(session),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    service._operation = AsyncMock()  # type: ignore[method-assign]
    key = SimpleNamespace(project_id=uuid4())

    result = await service.delete_records(
        key, uuid4(), ["p", "c", "missing"], "trace-delete"  # type: ignore[arg-type]
    )

    assert session.deleted == [child, parent]
    assert session.flush_count == 2
    assert result == {"deleted_ids": ["p", "c"], "missing_ids": ["missing"]}


@pytest.mark.asyncio
async def test_delete_records_rejects_parent_with_unrequested_children() -> None:
    parent = SimpleNamespace(external_id="p", parent_external_id=None)
    session = _Session([[parent], ["unrequested-child"]])
    service = RagKnowledgeService(
        _SessionFactory(session),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    key = SimpleNamespace(project_id=uuid4())

    with pytest.raises(RagError) as error:
        await service.delete_records(key, uuid4(), ["p"], "trace-delete")  # type: ignore[arg-type]

    assert error.value.status_code == 409
    assert error.value.code == "parent_record_in_use"
    assert not session.deleted


@pytest.mark.asyncio
async def test_publish_version_switches_parent_and_children_in_one_transaction() -> None:
    old_parent = SimpleNamespace(parent_external_id=None, is_published=True)
    old_child = SimpleNamespace(parent_external_id="old-parent", is_published=True)
    new_parent = SimpleNamespace(
        parent_external_id=None, is_published=False, was_published=False
    )
    new_child = SimpleNamespace(
        parent_external_id="new-parent", is_published=False, was_published=False
    )
    session = _Session(
        [[new_parent, new_child], [old_parent], [old_child]],
        scalar_result=SimpleNamespace(id=uuid4()),
    )
    service = RagKnowledgeService(
        _SessionFactory(session),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    service.get_knowledge_base = AsyncMock()  # type: ignore[method-assign]
    service._operation = AsyncMock()  # type: ignore[method-assign]

    result = await service.publish_version(
        SimpleNamespace(project_id=uuid4()),  # type: ignore[arg-type]
        uuid4(),
        namespace="knowledge",
        logical_document_id="doc-1",
        version_id="v2",
        trace_id="trace-publish",
    )

    assert old_parent.is_published is False
    assert old_child.is_published is False
    assert new_parent.is_published is True
    assert new_child.is_published is True
    assert result == {"version_id": "v2", "published_records": 2, "already_published": False}


@pytest.mark.asyncio
async def test_publish_rejects_a_previously_published_retired_version() -> None:
    retired_record = SimpleNamespace(
        parent_external_id=None,
        is_published=False,
        was_published=True,
    )
    session = _Session(
        [[retired_record]],
        scalar_result=SimpleNamespace(id=uuid4()),
    )
    service = RagKnowledgeService(
        _SessionFactory(session),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service._require_enabled = AsyncMock()  # type: ignore[method-assign]
    service.get_knowledge_base = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RagError) as error:
        await service.publish_version(
            SimpleNamespace(project_id=uuid4()),  # type: ignore[arg-type]
            uuid4(),
            namespace="knowledge",
            logical_document_id="doc-1",
            version_id="v1",
            trace_id="trace-publish-retired",
        )

    assert error.value.code == "published_version_retired"
