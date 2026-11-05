from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from dataclasses import asdict, dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.config import get_settings
from app.database import Database
from app.vector_store.qdrant import ChunkVector, QdrantVectorStore

logger = logging.getLogger("omnitutor.reindex")


@dataclass
class ReindexSummary:
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: bool = False


def _record_from_row(row: dict[str, Any], *, embedding_model: str, vector_size: int) -> ChunkVector:
    vector = row["embedding"]
    if not isinstance(vector, list) or len(vector) != vector_size:
        raise ValueError(f"expected a {vector_size}-dimensional stored embedding")
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("stored embedding contains non-finite values")
    return ChunkVector(
        chunk_id=row["chunk_id"],
        account_id=row["account_id"],
        notebook_id=row["notebook_id"],
        source_id=row["source_id"],
        source_type=row["source_type"],
        topic_id=row["topic_id"],
        embedding_model=embedding_model,
        embedding_version="legacy-postgres",
        page_number=row["page"],
        slide_number=row["slide"],
        timestamp_start=row["timestamp_start"],
        timestamp_end=row["timestamp_end"],
        chunk_kind=row["kind"],
        vector=vector,
    )


async def reindex_embeddings(
    database: Database,
    client: AsyncQdrantClient,
    store: QdrantVectorStore,
    *,
    embedding_model: str,
    vector_size: int,
    batch_size: int = 128,
    dry_run: bool = False,
) -> ReindexSummary:
    summary = ReindexSummary(dry_run=dry_run)
    batch: list[ChunkVector] = []

    async def flush() -> None:
        if not batch:
            return
        existing: set[str] = set()
        if await client.collection_exists(store.collection_name):
            collection = await client.get_collection(store.collection_name)
            vector_config = collection.config.params.vectors
            if (
                not isinstance(vector_config, models.VectorParams)
                or vector_config.size != store.vector_size
                or vector_config.distance != models.Distance.COSINE
            ):
                raise ValueError("Existing Qdrant collection has an incompatible vector configuration")
            points = await client.retrieve(
                collection_name=store.collection_name,
                ids=[store.point_id(record.chunk_id) for record in batch],
                with_payload=False,
                with_vectors=False,
            )
            existing = {point.id for point in points}
        existing_chunk_ids = {record.chunk_id for record in batch if store.point_id(record.chunk_id) in existing}
        if not dry_run:
            try:
                await store.upsert_many(batch)
            except (ResponseHandlingException, UnexpectedResponse):
                logger.exception("Qdrant batch upsert failed for %d canonical chunks", len(batch))
                summary.failed += len(batch)
                batch.clear()
                return
        summary.updated += len(existing_chunk_ids)
        summary.inserted += len(batch) - len(existing_chunk_ids)
        batch.clear()

    async for row in database.iter_reindex_rows():
        summary.processed += 1
        try:
            batch.append(_record_from_row(row, embedding_model=embedding_model, vector_size=vector_size))
        except (ValueError, TypeError, KeyError) as error:
            summary.skipped += 1
            logger.warning("Skipped invalid canonical chunk %s (%s)", row.get("chunk_id", "<unknown>"), error)
            continue
        if len(batch) >= batch_size:
            await flush()
    await flush()
    return summary


async def _run(dry_run: bool) -> int:
    settings = get_settings()
    database = Database.from_settings(settings)
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=max(1, int(settings.qdrant_timeout_seconds)),
    )
    store = QdrantVectorStore(
        client,
        collection_name=settings.qdrant_collection,
        vector_size=settings.qdrant_vector_size,
        batch_size=settings.qdrant_batch_size,
    )
    try:
        summary = await reindex_embeddings(
            database,
            client,
            store,
            embedding_model=settings.embedding_model,
            vector_size=settings.qdrant_vector_size,
            batch_size=settings.qdrant_batch_size,
            dry_run=dry_run,
        )
        print(json.dumps(asdict(summary), indent=2))
        return 1 if summary.failed else 0
    finally:
        await client.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotently index READY PostgreSQL embeddings into Qdrant.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and count records without writing vectors.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
