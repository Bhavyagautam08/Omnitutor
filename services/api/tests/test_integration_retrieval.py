from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import random
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.database import Database
from app.embeddings import GeminiEmbedder, StaticEmbedder
from app.main import create_app
from app.reindex import reindex_embeddings
from app.vector_store.qdrant import ChunkVector, QdrantVectorStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("OMNITUTOR_RUN_INTEGRATION") != "1",
        reason="set OMNITUTOR_RUN_INTEGRATION=1 with local PostgreSQL and Qdrant running",
    ),
]


def signed_cookie(learner_id: str, secret: str) -> str:
    payload = f"{learner_id}.{int(time.time()) + 600}".encode()
    expiry = payload.decode().rsplit(".", maxsplit=1)[1]
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest())
    return f"{learner_id}.{expiry}.{signature.rstrip(b'=').decode()}"


def vector(seed: int) -> list[float]:
    generator = random.Random(seed)
    return [generator.uniform(-1, 1) for _ in range(768)]


def qdrant_record(
    chunk_id: str,
    *,
    account_id: str,
    notebook_id: str,
    source_id: str,
    embedding: list[float],
) -> ChunkVector:
    return ChunkVector(
        chunk_id=chunk_id,
        account_id=account_id,
        notebook_id=notebook_id,
        source_id=source_id,
        source_type="PDF",
        page_number=3,
        topic_id=None,
        embedding_model="synthetic-test-vector",
        embedding_version="integration-v1",
        chunk_kind="TEXT",
        vector=embedding,
    )


@pytest.mark.asyncio
async def test_live_postgres_qdrant_reindex_and_authenticated_retrieval() -> None:
    base_settings = Settings()
    prefix = f"it-{uuid.uuid4().hex[:12]}"
    collection_name = f"omnitutor_it_{uuid.uuid4().hex[:12]}"
    session_secret = "integration-session-secret-long-enough"
    settings = base_settings.model_copy(
        update={
            "qdrant_collection": collection_name,
            "session_secret": session_secret,
            "gemini_api_key": None,
            "embedding_model": "synthetic-test-vector",
        }
    )
    database = Database.from_settings(settings)
    qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=max(1, int(settings.qdrant_timeout_seconds)),
    )
    store = QdrantVectorStore(qdrant, collection_name, settings.qdrant_vector_size, batch_size=16)

    user_a, user_b = f"{prefix}-user-a", f"{prefix}-user-b"
    notebook_a1, notebook_a2, notebook_b1 = f"{prefix}-a1", f"{prefix}-a2", f"{prefix}-b1"
    source_a1, source_a2, source_b1, source_failed = (
        f"{prefix}-src-a1",
        f"{prefix}-src-a2",
        f"{prefix}-src-b1",
        f"{prefix}-src-failed",
    )
    chunk_a1, chunk_a2, chunk_b1, chunk_failed, chunk_deleted = (
        f"{prefix}-chunk-a1",
        f"{prefix}-chunk-a2",
        f"{prefix}-chunk-b1",
        f"{prefix}-chunk-failed",
        f"{prefix}-chunk-deleted",
    )
    chunk_bad_dimension = f"{prefix}-chunk-bad-dimension"
    query_vector = vector(9021)

    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text('INSERT INTO "Learner" ("id", "name", "passwordHash") VALUES (:id, :name, :hash)'),
                [
                    {"id": user_a, "name": user_a, "hash": "integration-password-hash"},
                    {"id": user_b, "name": user_b, "hash": "integration-password-hash"},
                ],
            )
            await connection.execute(
                text('INSERT INTO "Course" ("id", "name", "ownerId") VALUES (:id, :name, :owner)'),
                [
                    {"id": notebook_a1, "name": "A notebook 1", "owner": user_a},
                    {"id": notebook_a2, "name": "A notebook 2", "owner": user_a},
                    {"id": notebook_b1, "name": "B notebook 1", "owner": user_b},
                ],
            )
            source_rows = [
                (source_a1, notebook_a1, "READY"),
                (source_a2, notebook_a2, "READY"),
                (source_b1, notebook_b1, "READY"),
                (source_failed, notebook_a1, "FAILED"),
            ]
            for source_id, notebook_id, source_status in source_rows:
                await connection.execute(
                    text(
                        'INSERT INTO "Source" '
                        '("id", "courseId", "type", "filename", "mimeType", "sizeBytes", "path",'
                        ' "status", "updatedAt") '
                        "VALUES (:id, :course_id, 'PDF', :filename, 'application/pdf', 100, "
                        ":path, :source_status, now())"
                    ),
                    {
                        "id": source_id,
                        "course_id": notebook_id,
                        "filename": f"{source_id}.pdf",
                        "path": f"data/uploads/{source_id}.pdf",
                        "source_status": source_status,
                    },
                )
            chunks = [
                (chunk_a1, source_a1, "READY chunk for user A notebook 1", query_vector),
                (chunk_a2, source_a2, "READY chunk for user A notebook 2", vector(11)),
                (chunk_b1, source_b1, "PRIVATE chunk for user B", vector(12)),
                (chunk_failed, source_failed, "FAILED-source content must never return", query_vector),
                (chunk_bad_dimension, source_a1, "Invalid stored vector must be skipped", [0.1, 0.2]),
            ]
            for chunk_id, source_id, content, embedding in chunks:
                await connection.execute(
                    text(
                        'INSERT INTO "Chunk" '
                        '("id", "sourceId", "kind", "seq", "content", "page", "conceptLabels", "embedding") '
                        "VALUES (:id, :source_id, 'TEXT', 0, :content, 3, ARRAY[]::TEXT[], "
                        "CAST(:embedding AS DOUBLE PRECISION[]))"
                    ),
                    {"id": chunk_id, "source_id": source_id, "content": content, "embedding": embedding},
                )

        dry_run = await reindex_embeddings(
            database,
            qdrant,
            store,
            embedding_model=settings.embedding_model,
            vector_size=768,
            batch_size=2,
            dry_run=True,
        )
        assert (dry_run.processed, dry_run.inserted, dry_run.skipped, dry_run.failed) == (4, 3, 1, 0)
        assert not await qdrant.collection_exists(collection_name)
        print("Dry-run reindex:", dry_run)

        first = await reindex_embeddings(
            database,
            qdrant,
            store,
            embedding_model=settings.embedding_model,
            vector_size=768,
            batch_size=2,
        )
        second = await reindex_embeddings(
            database,
            qdrant,
            store,
            embedding_model=settings.embedding_model,
            vector_size=768,
            batch_size=2,
        )
        assert (first.processed, first.inserted, first.skipped, first.failed) == (4, 3, 1, 0)
        assert (second.processed, second.inserted, second.updated, second.skipped, second.failed) == (4, 0, 3, 1, 0)
        assert (await qdrant.count(collection_name=collection_name, exact=True)).count == 3
        print("First reindex:", first)
        print("Idempotent rerun:", second)
        with pytest.raises(ValueError, match="expected a 768-dimensional"):
            await store.upsert_many(
                [
                    qdrant_record(
                        f"{prefix}-invalid-vector",
                        account_id=user_a,
                        notebook_id=notebook_a1,
                        source_id=source_a1,
                        embedding=[0.1, 0.2],
                    )
                ]
            )

        # Simulate stale/deleted and non-ready vector records that remain temporarily in Qdrant.
        await store.upsert_many(
            [
                qdrant_record(
                    chunk_failed,
                    account_id=user_a,
                    notebook_id=notebook_a1,
                    source_id=source_failed,
                    embedding=query_vector,
                ),
                qdrant_record(
                    chunk_deleted,
                    account_id=user_a,
                    notebook_id=notebook_a1,
                    source_id=f"{prefix}-deleted-source",
                    embedding=query_vector,
                ),
            ]
        )
        filler_records = [
            qdrant_record(
                f"{prefix}-filler-{index}",
                account_id=f"{prefix}-filler-account",
                notebook_id=f"{prefix}-filler-notebook",
                source_id=f"{prefix}-filler-source-{index}",
                embedding=vector(index + 100),
            )
            for index in range(123)
        ]
        await store.upsert_many(filler_records)
        await qdrant.update_collection(
            collection_name,
            hnsw_config=models.HnswConfigDiff(full_scan_threshold=10),
            optimizers_config=models.OptimizersConfigDiff(
                indexing_threshold=1,
                vacuum_min_vector_number=100,
                default_segment_number=1,
            ),
        )
        for _ in range(40):
            collection = await qdrant.get_collection(collection_name)
            if collection.indexed_vectors_count == 128 and collection.status.value == "green":
                break
            await asyncio.sleep(0.5)
        assert collection.indexed_vectors_count == 128
        assert collection.config.hnsw_config.m == 16
        assert collection.config.hnsw_config.ef_construct == 100
        print(
            "Live HNSW integration:",
            f"{collection.indexed_vectors_count}/{collection.points_count} indexed vectors;",
            f"payload indexes={len(collection.payload_schema)}",
        )

        app = create_app(
            settings,
            embedder_factory=lambda _: StaticEmbedder(
                {"Explain the concept": query_vector, "different query": vector(100)}
            ),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
                token_a = signed_cookie(user_a, session_secret)
                token_b = signed_cookie(user_b, session_secret)

                point_count_before_sync = (await qdrant.count(collection_name, exact=True)).count
                sync_source = await http.post(
                    f"/api/v1/notebooks/{notebook_a2}/sources/{source_a2}/vectors",
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert sync_source.status_code == 200, sync_source.text
                assert sync_source.json() == {"indexed": 1}
                assert (await qdrant.count(collection_name, exact=True)).count == point_count_before_sync
                synced_points = await qdrant.retrieve(
                    collection_name,
                    ids=[store.point_id(chunk_a2)],
                    with_payload=True,
                    with_vectors=False,
                )
                assert len(synced_points) == 1
                assert "content" not in (synced_points[0].payload or {})
                assert synced_points[0].payload["page_number"] == 3

                foreign_sync = await http.post(
                    f"/api/v1/notebooks/{notebook_a2}/sources/{source_a2}/vectors",
                    headers={"Cookie": f"omnitutor_session={token_b}"},
                )
                assert foreign_sync.status_code == 404
                wrong_notebook_sync = await http.post(
                    f"/api/v1/notebooks/{notebook_b1}/sources/{source_b1}/vectors",
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert wrong_notebook_sync.status_code == 404

                retrieval_started = time.perf_counter()
                own = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "Explain the concept", "top_k": 10},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                retrieval_latency_ms = (time.perf_counter() - retrieval_started) * 1000
                print(f"Authenticated retrieval latency with synthetic embeddings: {retrieval_latency_ms:.2f} ms")
                assert own.status_code == 200, own.text
                own_json = own.json()
                assert [result["chunk_id"] for result in own_json["results"]] == [chunk_a1]
                assert own_json["results"][0]["content"] == "READY chunk for user A notebook 1"
                assert own_json["results"][0]["citation"]["page"] == 3
                assert own_json["results"][0]["score"] > 0.99
                print("Synthetic authenticated API response:", own_json)

                cross_user = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "Explain the concept"},
                    headers={"Cookie": f"omnitutor_session={token_b}"},
                )
                assert cross_user.status_code == 404

                other_notebook = await http.post(
                    f"/api/v1/notebooks/{notebook_b1}/search",
                    json={"query": "Explain the concept"},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert other_notebook.status_code == 404

                forged_identity = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "Explain the concept", "account_id": user_a},
                    headers={"Cookie": f"omnitutor_session={token_b}"},
                )
                assert forged_identity.status_code == 422

                b_own = await http.post(
                    f"/api/v1/notebooks/{notebook_b1}/search",
                    json={"query": "Explain the concept"},
                    headers={"Cookie": f"omnitutor_session={token_b}"},
                )
                assert b_own.status_code == 200
                assert [item["chunk_id"] for item in b_own.json()["results"]] == [chunk_b1]

                own_second = await http.post(
                    f"/api/v1/notebooks/{notebook_a2}/search",
                    json={"query": "Explain the concept"},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert own_second.status_code == 200
                assert [item["chunk_id"] for item in own_second.json()["results"]] == [chunk_a2]

                delete_source = await http.delete(
                    f"/api/v1/notebooks/{notebook_a2}/sources/{source_a2}/vectors",
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert delete_source.status_code == 200, delete_source.text
                remaining = await qdrant.retrieve(
                    collection_name,
                    ids=[store.point_id(chunk_a2)],
                    with_payload=False,
                    with_vectors=False,
                )
                assert remaining == []

                unauthenticated = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "query"},
                    headers={"X-User-Id": user_a, "X-Account-Id": user_a},
                )
                assert unauthenticated.status_code == 401

                forged_account = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "query", "account_id": user_b},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert forged_account.status_code == 422

                malformed = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "   ", "top_k": 0},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert malformed.status_code == 422

                empty = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "Explain the concept", "source_id": f"{prefix}-missing-source"},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert empty.status_code == 200
                assert empty.json()["results"] == []

                app.state.embedder = GeminiEmbedder(
                    api_key=None,
                    model=settings.embedding_model,
                    dimension=settings.qdrant_vector_size,
                )
                missing_gemini = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "Explain the concept"},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert missing_gemini.status_code == 503
                assert missing_gemini.json()["detail"]["code"] == "gemini_not_configured"
                app.state.embedder = StaticEmbedder(
                    {"Explain the concept": query_vector, "different query": vector(100)}
                )

                async def fail_query(**_: object) -> object:
                    raise ResponseHandlingException(ConnectionError("synthetic unavailable"))

                query_points = app.state.vector_store._client.query_points
                app.state.vector_store._client.query_points = fail_query
                qdrant_failure = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "Explain the concept"},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert qdrant_failure.status_code == 503
                assert qdrant_failure.json()["detail"]["code"] == "qdrant_unavailable"
                app.state.vector_store._client.query_points = query_points

                async def fail_lookup(**_: object) -> list[dict[str, object]]:
                    raise OperationalError("SELECT", {}, ConnectionError("synthetic unavailable"))

                fetch_ready_chunks = app.state.database.fetch_ready_chunks
                app.state.database.fetch_ready_chunks = fail_lookup
                postgres_failure = await http.post(
                    f"/api/v1/notebooks/{notebook_a1}/search",
                    json={"query": "Explain the concept"},
                    headers={"Cookie": f"omnitutor_session={token_a}"},
                )
                assert postgres_failure.status_code == 503
                assert postgres_failure.json()["detail"]["code"] == "postgres_unavailable"
                app.state.database.fetch_ready_chunks = fetch_ready_chunks
    finally:
        if await qdrant.collection_exists(collection_name):
            await qdrant.delete_collection(collection_name)
        async with database.engine.begin() as connection:
            await connection.execute(
                text('DELETE FROM "Learner" WHERE "id" IN (:user_a, :user_b)'),
                {"user_a": user_a, "user_b": user_b},
            )
        await qdrant.close()
        await database.close()
