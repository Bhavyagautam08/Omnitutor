from __future__ import annotations

import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy.exc import SQLAlchemyError

from app.auth import require_authenticated_learner
from app.database import Database
from app.embeddings import EmbeddingNotConfigured, EmbeddingProviderError, GeminiEmbedder
from app.reindex import _record_from_row
from app.retrieval import RetrievalService
from app.vector_store.qdrant import QdrantVectorStore

router = APIRouter(prefix="/api/v1/notebooks", tags=["retrieval"])
SourceType = Literal["PDF", "PPTX", "VIDEO"]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=20)
    source_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_type: SourceType | None = None
    topic_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must contain non-whitespace characters")
        return normalized


@router.post("/{notebook_id}/search")
async def search_notebook(
    notebook_id: str,
    body: SearchRequest,
    request: Request,
    learner_id: str = Depends(require_authenticated_learner),
) -> dict[str, object]:
    database: Database = request.app.state.database
    vector_store: QdrantVectorStore = request.app.state.vector_store
    embedder: GeminiEmbedder = request.app.state.embedder
    service = RetrievalService(database, vector_store, embedder)

    try:
        result = await service.search(
            learner_id=learner_id,
            notebook_id=notebook_id,
            query=body.query,
            top_k=body.top_k,
            source_id=body.source_id,
            source_type=body.source_type,
            topic_id=body.topic_id,
        )
    except EmbeddingNotConfigured as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "gemini_not_configured", "message": "Query embedding is not configured."},
        ) from error
    except EmbeddingProviderError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "gemini_error", "message": "Gemini could not generate the query embedding."},
        ) from error
    except (ResponseHandlingException, UnexpectedResponse) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "qdrant_unavailable", "message": "Vector search is temporarily unavailable."},
        ) from error
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "postgres_unavailable", "message": "Canonical document lookup is temporarily unavailable."},
        ) from error

    if result["not_found"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "notebook_not_found", "message": "Notebook not found."},
        )
    return {
        "query": body.query,
        "notebook_id": notebook_id,
        "top_k": body.top_k,
        "candidates": result["candidates"],
        "results": result["results"],
    }


@router.get("/health")
async def retrieval_health(request: Request) -> dict[str, str]:
    database: Database = request.app.state.database
    vector_store: QdrantVectorStore = request.app.state.vector_store
    await database.ping()
    await vector_store.ping()
    return {"status": "ready"}


@router.post("/{notebook_id}/sources/{source_id}/vectors")
async def sync_source_vectors(
    notebook_id: str,
    source_id: str,
    request: Request,
    learner_id: str = Depends(require_authenticated_learner),
) -> dict[str, int]:
    database: Database = request.app.state.database
    vector_store: QdrantVectorStore = request.app.state.vector_store
    settings = request.app.state.settings
    if not await database.owns_source(learner_id, notebook_id, source_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "source_not_found", "message": "Source not found."},
        )

    rows = await database.source_index_rows(
        learner_id=learner_id,
        notebook_id=notebook_id,
        source_id=source_id,
    )
    try:
        records = [
            _record_from_row(
                row,
                embedding_model=settings.embedding_model,
                vector_size=settings.qdrant_vector_size,
            )
            for row in rows
        ]
        if any(
            len(record.vector) != settings.qdrant_vector_size
            or not all(math.isfinite(value) for value in record.vector)
            for record in records
        ):
            raise ValueError("Source contains an invalid vector.")
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_stored_embedding", "message": "Source contains an invalid stored embedding."},
        ) from error

    await vector_store.delete_source(
        account_id=learner_id,
        notebook_id=notebook_id,
        source_id=source_id,
    )
    await vector_store.upsert_many(records)
    return {"indexed": len(records)}


@router.delete("/{notebook_id}/sources/{source_id}/vectors")
async def delete_source_vectors(
    notebook_id: str,
    source_id: str,
    request: Request,
    learner_id: str = Depends(require_authenticated_learner),
) -> dict[str, str]:
    database: Database = request.app.state.database
    vector_store: QdrantVectorStore = request.app.state.vector_store
    if not await database.owns_source(learner_id, notebook_id, source_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "source_not_found", "message": "Source not found."},
        )
    await vector_store.delete_source(
        account_id=learner_id,
        notebook_id=notebook_id,
        source_id=source_id,
    )
    return {"status": "deleted"}
