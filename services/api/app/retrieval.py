from __future__ import annotations

from typing import Literal

from app.database import Database
from app.embeddings import GeminiEmbedder
from app.vector_store.qdrant import QdrantVectorStore

SourceType = Literal["PDF", "PPTX", "VIDEO"]


class RetrievalService:
    def __init__(self, database: Database, vector_store: QdrantVectorStore, embedder: GeminiEmbedder) -> None:
        self._database = database
        self._vector_store = vector_store
        self._embedder = embedder

    async def search(
        self,
        *,
        learner_id: str,
        notebook_id: str,
        query: str,
        top_k: int,
        source_id: str | None = None,
        source_type: SourceType | None = None,
        topic_id: str | None = None,
    ) -> dict[str, object]:
        if not await self._database.owns_notebook(learner_id, notebook_id):
            return {"not_found": True}

        query_vector = await self._embedder.embed_query(query)
        hits = await self._vector_store.search(
            query_vector,
            account_id=learner_id,
            notebook_id=notebook_id,
            limit=top_k,
            source_id=source_id,
            source_type=source_type,
            topic_id=topic_id,
        )
        canonical = await self._database.fetch_ready_chunks(
            learner_id=learner_id,
            notebook_id=notebook_id,
            chunk_ids=[hit.chunk_id for hit in hits],
        )
        canonical_by_id = {row["chunk_id"]: row for row in canonical}
        results: list[dict[str, object]] = []
        for hit in hits:
            row = canonical_by_id.get(hit.chunk_id)
            if row is None:
                continue
            results.append(
                {
                    "chunk_id": row["chunk_id"],
                    "source_id": row["source_id"],
                    "source_type": row["source_type"],
                    "filename": row["filename"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "content": row["content"],
                    "topic_id": row["topic_id"],
                    "score": hit.score,
                    "citation": {
                        "page": row["page"],
                        "slide": row["slide"],
                        "timestamp_start": row["timestamp_start"],
                        "timestamp_end": row["timestamp_end"],
                    },
                }
            )
        return {"not_found": False, "results": results, "candidates": len(hits)}
