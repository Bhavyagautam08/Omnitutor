from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from qdrant_client import AsyncQdrantClient, models

SourceType = Literal["PDF", "PPTX", "VIDEO"]
ChunkKind = Literal["TEXT", "FIGURE", "SLIDE", "TRANSCRIPT"]


class ChunkVector(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    notebook_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_type: SourceType
    topic_id: str | None = None
    embedding_model: str = Field(min_length=1)
    embedding_version: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    slide_number: int | None = Field(default=None, ge=1)
    timestamp_start: float | None = Field(default=None, ge=0)
    timestamp_end: float | None = Field(default=None, ge=0)
    chunk_kind: ChunkKind
    vector: list[float]

    @model_validator(mode="after")
    def validate_locator(self) -> ChunkVector:
        locators = (self.page_number, self.slide_number, self.timestamp_start, self.timestamp_end)
        if self.source_type == "PDF" and (self.page_number is None or any(x is not None for x in locators[1:])):
            raise ValueError("PDF vectors require only page_number provenance")
        if self.source_type == "PPTX" and (
            self.slide_number is None or self.page_number is not None or any(x is not None for x in locators[2:])
        ):
            raise ValueError("PPTX vectors require only slide_number provenance")
        if self.source_type == "VIDEO" and (
            self.page_number is not None
            or self.slide_number is not None
            or self.timestamp_start is None
            or self.timestamp_end is None
            or self.timestamp_start > self.timestamp_end
        ):
            raise ValueError("video vectors require a valid timestamp range")
        return self


class SearchHit(BaseModel):
    chunk_id: str
    score: float
    payload: dict[str, object]


class QdrantVectorStore:
    """Indexed vector operations; PostgreSQL remains the canonical store for chunk text."""

    _PAYLOAD_INDEXES = {
        "account_id": models.PayloadSchemaType.KEYWORD,
        "notebook_id": models.PayloadSchemaType.KEYWORD,
        "source_id": models.PayloadSchemaType.KEYWORD,
        "source_type": models.PayloadSchemaType.KEYWORD,
        "topic_id": models.PayloadSchemaType.KEYWORD,
        "embedding_model": models.PayloadSchemaType.KEYWORD,
        "embedding_version": models.PayloadSchemaType.KEYWORD,
        "page_number": models.PayloadSchemaType.INTEGER,
        "slide_number": models.PayloadSchemaType.INTEGER,
        "timestamp_start": models.PayloadSchemaType.FLOAT,
        "timestamp_end": models.PayloadSchemaType.FLOAT,
        "chunk_kind": models.PayloadSchemaType.KEYWORD,
    }

    def __init__(
        self,
        client: AsyncQdrantClient,
        collection_name: str,
        vector_size: int,
        batch_size: int = 128,
    ) -> None:
        if vector_size <= 0:
            raise ValueError("vector_size must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._client = client
        self._collection = collection_name
        self._vector_size = vector_size
        self._batch_size = batch_size
        self._collection_ready = False
        self._collection_lock = asyncio.Lock()

    @property
    def collection_name(self) -> str:
        return self._collection

    @property
    def vector_size(self) -> int:
        return self._vector_size

    @staticmethod
    def point_id(chunk_id: str) -> str:
        return QdrantVectorStore._point_id(chunk_id)

    async def ping(self) -> None:
        await self._client.get_collections()

    async def ensure_collection(self) -> None:
        if self._collection_ready:
            return
        async with self._collection_lock:
            if self._collection_ready:
                return
            if not await self._client.collection_exists(self._collection):
                await self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=models.VectorParams(
                        size=self._vector_size,
                        distance=models.Distance.COSINE,
                    ),
                    hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
                )
            else:
                collection = await self._client.get_collection(self._collection)
                vector_config = collection.config.params.vectors
                if not isinstance(vector_config, models.VectorParams):
                    raise ValueError("Qdrant collection must use one unnamed dense vector")
                if vector_config.size != self._vector_size:
                    raise ValueError(
                        f"Qdrant collection dimension is {vector_config.size}; "
                        f"configured dimension is {self._vector_size}"
                    )
                if vector_config.distance != models.Distance.COSINE:
                    raise ValueError("Qdrant collection must use cosine distance")

            for field_name, schema in self._PAYLOAD_INDEXES.items():
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field_name,
                    field_schema=schema,
                    wait=True,
                )
            self._collection_ready = True

    async def upsert_many(self, records: Sequence[ChunkVector]) -> int:
        for record in records:
            self._validate_vector(record.vector)
        if not records:
            return 0
        await self.ensure_collection()
        for start in range(0, len(records), self._batch_size):
            points = [
                models.PointStruct(
                    id=self._point_id(record.chunk_id),
                    vector=record.vector,
                    payload=record.model_dump(exclude={"vector"}, mode="json"),
                )
                for record in records[start : start + self._batch_size]
            ]
            await self._client.upsert(
                collection_name=self._collection,
                points=points,
                wait=True,
            )
        return len(records)

    async def search(
        self,
        query_vector: Sequence[float],
        *,
        account_id: str,
        notebook_id: str,
        limit: int = 6,
        source_type: SourceType | None = None,
        topic_id: str | None = None,
        source_id: str | None = None,
    ) -> list[SearchHit]:
        if not account_id.strip() or not notebook_id.strip():
            raise ValueError("account_id and notebook_id are required for every vector search")
        if limit <= 0:
            raise ValueError("limit must be positive")
        self._validate_vector(query_vector)
        await self.ensure_collection()
        response = await self._client.query_points(
            collection_name=self._collection,
            query=list(query_vector),
            query_filter=self._filter(
                account_id=account_id,
                notebook_id=notebook_id,
                source_type=source_type,
                topic_id=topic_id,
                source_id=source_id,
            ),
            search_params=models.SearchParams(hnsw_ef=128, exact=False),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        hits: list[SearchHit] = []
        for point in response.points:
            payload = dict(point.payload or {})
            chunk_id = payload.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ValueError("Qdrant result is missing its canonical chunk_id")
            if payload.get("account_id") != account_id or payload.get("notebook_id") != notebook_id:
                raise ValueError("Qdrant returned a point outside the requested account/notebook scope")
            hits.append(SearchHit(chunk_id=chunk_id, score=point.score, payload=payload))
        return hits

    async def delete_chunks(self, chunk_ids: Sequence[str], *, account_id: str, notebook_id: str) -> None:
        self._validate_scope(account_id, notebook_id)
        if not chunk_ids:
            return
        if not await self._client.collection_exists(self._collection):
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        *self._filter(account_id=account_id, notebook_id=notebook_id).must,
                        models.HasIdCondition(has_id=[self._point_id(chunk_id) for chunk_id in chunk_ids]),
                    ]
                )
            ),
            wait=True,
        )

    async def delete_source(self, *, account_id: str, notebook_id: str, source_id: str) -> None:
        self._validate_scope(account_id, notebook_id)
        if not source_id.strip():
            raise ValueError("source_id is required for source deletion")
        if not await self._client.collection_exists(self._collection):
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=self._filter(
                    account_id=account_id,
                    notebook_id=notebook_id,
                    source_id=source_id,
                )
            ),
            wait=True,
        )

    async def delete_account(self, *, account_id: str) -> None:
        if not account_id.strip():
            raise ValueError("account_id is required for account deletion")
        if not await self._client.collection_exists(self._collection):
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="account_id", match=models.MatchValue(value=account_id))]
                )
            ),
            wait=True,
        )

    def _validate_vector(self, vector: Sequence[float]) -> None:
        if len(vector) != self._vector_size:
            raise ValueError(f"expected a {self._vector_size}-dimensional vector, received {len(vector)}")
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("vectors must contain only finite numbers")

    @staticmethod
    def _validate_scope(account_id: str, notebook_id: str) -> None:
        if not account_id.strip() or not notebook_id.strip():
            raise ValueError("account_id and notebook_id are required for scoped operations")

    @staticmethod
    def _point_id(chunk_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"omnitutor:chunk:{chunk_id}"))

    @staticmethod
    def _filter(
        *,
        account_id: str,
        notebook_id: str,
        source_type: SourceType | None = None,
        topic_id: str | None = None,
        source_id: str | None = None,
    ) -> models.Filter:
        must = [
            models.FieldCondition(key="account_id", match=models.MatchValue(value=account_id)),
            models.FieldCondition(key="notebook_id", match=models.MatchValue(value=notebook_id)),
        ]
        for key, value in (
            ("source_type", source_type),
            ("topic_id", topic_id),
            ("source_id", source_id),
        ):
            if value is not None:
                must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
        return models.Filter(must=must)
