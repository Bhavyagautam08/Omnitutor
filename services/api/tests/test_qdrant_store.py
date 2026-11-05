import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from qdrant_client import AsyncQdrantClient, models

from app.config import Settings
from app.main import create_app
from app.vector_store.qdrant import ChunkVector, QdrantVectorStore


class FakeQdrant:
    def __init__(self) -> None:
        self.exists = False
        self.created_collection: dict[str, object] | None = None
        self.create_collection_calls = 0
        self.indexes: list[tuple[str, object]] = []
        self.upsert_batches: list[list[models.PointStruct]] = []
        self.query: dict[str, object] | None = None
        self.deleted: list[object] = []
        self.points: list[object] = []
        self.closed = False

    async def collection_exists(self, collection_name: str) -> bool:
        return self.exists

    async def create_collection(self, **kwargs: object) -> None:
        self.create_collection_calls += 1
        self.created_collection = kwargs
        self.exists = True

    async def create_payload_index(self, **kwargs: object) -> None:
        self.indexes.append((str(kwargs["field_name"]), kwargs["field_schema"]))

    async def get_collection(self, collection_name: str) -> object:
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=models.VectorParams(size=3, distance=models.Distance.COSINE))
            )
        )

    async def get_collections(self) -> object:
        return object()

    async def upsert(self, **kwargs: object) -> None:
        self.upsert_batches.append(kwargs["points"])  # type: ignore[arg-type]

    async def query_points(self, **kwargs: object) -> object:
        self.query = kwargs
        return SimpleNamespace(points=self.points)

    async def delete(self, **kwargs: object) -> None:
        self.deleted.append(kwargs["points_selector"])

    async def close(self) -> None:
        self.closed = True


class FakeDatabase:
    def __init__(self) -> None:
        self.closed = False

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def make_record(chunk_id: str) -> ChunkVector:
    return ChunkVector(
        chunk_id=chunk_id,
        account_id="account-1",
        notebook_id="notebook-1",
        source_id="source-1",
        source_type="PDF",
        topic_id="topic-1",
        embedding_model="gemini-embedding-001",
        embedding_version="v1",
        page_number=2,
        chunk_kind="TEXT",
        vector=[0.1, 0.2, 0.3],
    )


@pytest.mark.asyncio
async def test_creates_cosine_collection_and_filter_indexes() -> None:
    client = FakeQdrant()
    store = QdrantVectorStore(client, "chunks", vector_size=3)

    await asyncio.gather(store.ensure_collection(), store.ensure_collection())

    assert client.created_collection is not None
    assert client.create_collection_calls == 1
    index_count = len(client.indexes)
    await store.ensure_collection()
    assert len(client.indexes) == index_count
    config = client.created_collection["vectors_config"]
    assert config.size == 3
    assert config.distance == models.Distance.COSINE
    assert client.created_collection["hnsw_config"].m == 16
    assert client.created_collection["hnsw_config"].ef_construct == 100
    assert {field for field, _ in client.indexes} >= {
        "account_id",
        "notebook_id",
        "source_id",
        "source_type",
        "topic_id",
    }


@pytest.mark.asyncio
async def test_batch_upsert_uses_stable_ids_and_minimal_payload() -> None:
    client = FakeQdrant()
    store = QdrantVectorStore(client, "chunks", vector_size=3, batch_size=2)

    assert await store.upsert_many([make_record(f"chunk-{i}") for i in range(5)]) == 5

    assert [len(batch) for batch in client.upsert_batches] == [2, 2, 1]
    points = [point for batch in client.upsert_batches for point in batch]
    first_id = points[0].id
    await store.upsert_many([make_record("chunk-0")])
    assert client.upsert_batches[-1][0].id == first_id
    assert points[0].payload["chunk_id"] == "chunk-0"
    assert "content" not in points[0].payload
    assert points[0].payload["account_id"] == "account-1"


@pytest.mark.asyncio
async def test_search_always_filters_by_account_and_notebook() -> None:
    client = FakeQdrant()
    client.points = [
        SimpleNamespace(
            payload={
                "chunk_id": "chunk-1",
                "account_id": "account-1",
                "notebook_id": "notebook-1",
            },
            score=0.93,
        )
    ]
    store = QdrantVectorStore(client, "chunks", vector_size=3)

    hits = await store.search(
        [0.1, 0.2, 0.3],
        account_id="account-1",
        notebook_id="notebook-1",
        source_type="PDF",
        topic_id="topic-1",
        limit=4,
    )

    conditions = client.query["query_filter"].must
    filters = {item.key: item.match.value for item in conditions}
    assert filters == {
        "account_id": "account-1",
        "notebook_id": "notebook-1",
        "source_type": "PDF",
        "topic_id": "topic-1",
    }
    assert client.query["limit"] == 4
    assert client.query["search_params"].hnsw_ef == 128
    assert [hit.chunk_id for hit in hits] == ["chunk-1"]


@pytest.mark.asyncio
async def test_search_rejects_out_of_scope_points_even_if_qdrant_returns_them() -> None:
    client = FakeQdrant()
    client.points = [
        SimpleNamespace(
            payload={
                "chunk_id": "foreign-chunk",
                "account_id": "other-account",
                "notebook_id": "other-notebook",
            },
            score=0.99,
        )
    ]
    store = QdrantVectorStore(client, "chunks", vector_size=3)

    with pytest.raises(ValueError, match="outside the requested"):
        await store.search([0.1, 0.2, 0.3], account_id="account-1", notebook_id="notebook-1")


@pytest.mark.asyncio
async def test_search_rejects_missing_isolation_scope_and_bad_vectors() -> None:
    client = FakeQdrant()
    store = QdrantVectorStore(client, "chunks", vector_size=3)

    with pytest.raises(ValueError, match="account_id and notebook_id"):
        await store.search([0.1, 0.2, 0.3], account_id="", notebook_id="notebook-1")
    with pytest.raises(ValueError, match="expected a 3-dimensional"):
        await store.search([0.1, 0.2], account_id="account-1", notebook_id="notebook-1")
    with pytest.raises(ValueError, match="finite"):
        await store.upsert_many([make_record("bad").model_copy(update={"vector": [0.1, float("nan"), 0.3]})])
    assert client.created_collection is None


@pytest.mark.asyncio
async def test_deletion_uses_deterministic_chunk_ids_and_scoped_source_filter() -> None:
    client = FakeQdrant()
    client.exists = True
    store = QdrantVectorStore(client, "chunks", vector_size=3)

    await store.delete_chunks(["chunk-1"], account_id="account-1", notebook_id="notebook-1")
    await store.delete_source(account_id="account-1", notebook_id="notebook-1", source_id="source-1")
    await store.delete_account(account_id="account-1")

    conditions = client.deleted[0].filter.must
    assert {item.key: item.match.value for item in conditions[:2]} == {
        "account_id": "account-1",
        "notebook_id": "notebook-1",
    }
    assert isinstance(conditions[2], models.HasIdCondition)
    assert set(conditions[2].has_id) == {QdrantVectorStore._point_id("chunk-1")}
    source_conditions = client.deleted[1].filter.must
    assert {item.key: item.match.value for item in source_conditions} == {
        "account_id": "account-1",
        "notebook_id": "notebook-1",
        "source_id": "source-1",
    }
    account_conditions = client.deleted[2].filter.must
    assert {item.key: item.match.value for item in account_conditions} == {"account_id": "account-1"}


@pytest.mark.asyncio
async def test_empty_chunk_deletion_still_validates_scope() -> None:
    store = QdrantVectorStore(FakeQdrant(), "chunks", vector_size=3)

    with pytest.raises(ValueError, match="account_id and notebook_id"):
        await store.delete_chunks([], account_id="", notebook_id="notebook-1")


def test_fastapi_lifespan_initializes_and_closes_qdrant_client() -> None:
    client = FakeQdrant()
    database = FakeDatabase()
    app = create_app(
        Settings(
            _env_file=None,
            qdrant_url="http://qdrant.test",
            qdrant_collection="test_chunks",
            qdrant_vector_size=3,
        ),
        client_factory=lambda **_: client,  # type: ignore[arg-type]
        database_factory=lambda _: database,  # type: ignore[arg-type]
    )

    with TestClient(app) as http:
        assert http.get("/health/live").json() == {"status": "ok"}
        assert http.get("/health/ready").json() == {"status": "ready"}

    assert client.created_collection is None
    assert client.closed
    assert database.closed


@pytest.mark.asyncio
async def test_real_qdrant_search_and_deletion_isolate_notebooks() -> None:
    client = AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(client, "chunks", vector_size=3, batch_size=2)
    records = [
        make_record("own-source-chunk").model_copy(update={"vector": [1.0, 0.0, 0.0]}),
        make_record("other-notebook-chunk").model_copy(
            update={"notebook_id": "notebook-2", "vector": [0.99, 0.01, 0.0]}
        ),
        make_record("other-account-chunk").model_copy(update={"account_id": "account-2", "vector": [0.98, 0.02, 0.0]}),
    ]

    try:
        await store.upsert_many(records)
        hits = await store.search(
            [1.0, 0.0, 0.0],
            account_id="account-1",
            notebook_id="notebook-1",
            limit=3,
        )
        assert [hit.chunk_id for hit in hits] == ["own-source-chunk"]

        await store.delete_source(
            account_id="account-1",
            notebook_id="notebook-1",
            source_id="source-1",
        )
        hits_after_delete = await store.search(
            [1.0, 0.0, 0.0],
            account_id="account-1",
            notebook_id="notebook-1",
            limit=3,
        )
        assert hits_after_delete == []
    finally:
        await client.close()
