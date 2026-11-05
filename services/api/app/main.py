from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.database import Database
from app.embeddings import GeminiEmbedder
from app.routes import router as retrieval_router
from app.vector_store.qdrant import QdrantVectorStore


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[..., AsyncQdrantClient] = AsyncQdrantClient,
    database_factory: Callable[[Settings], Database] = Database.from_settings,
    embedder_factory: Callable[[Settings], GeminiEmbedder] | None = None,
) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = database_factory(config)
        client = client_factory(
            url=config.qdrant_url,
            api_key=config.qdrant_api_key or None,
            timeout=max(1, int(config.qdrant_timeout_seconds)),
        )
        store = QdrantVectorStore(
            client,
            collection_name=config.qdrant_collection,
            vector_size=config.qdrant_vector_size,
            batch_size=config.qdrant_batch_size,
        )
        embedder = (
            embedder_factory(config)
            if embedder_factory
            else GeminiEmbedder(
                api_key=config.gemini_api_key,
                model=config.embedding_model,
                dimension=config.qdrant_vector_size,
            )
        )
        application.state.settings = config
        application.state.database = database
        application.state.vector_store = store
        application.state.embedder = embedder
        try:
            await database.ping()
            await store.ping()
            yield
        finally:
            await embedder.close()
            await client.close()
            await database.close()

    application = FastAPI(title="OmniTutor API", version="0.1.0", lifespan=lifespan)
    application.include_router(retrieval_router)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request is malformed or contains invalid search parameters.",
                },
                "issues": [{"location": list(item["loc"]), "type": item["type"]} for item in error.errors()],
            },
        )

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(_: Request, __: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "postgres_unavailable",
                    "message": "PostgreSQL is temporarily unavailable.",
                }
            },
        )

    async def qdrant_unavailable(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "qdrant_unavailable",
                    "message": "Qdrant is temporarily unavailable.",
                }
            },
        )

    application.add_exception_handler(ResponseHandlingException, qdrant_unavailable)
    application.add_exception_handler(UnexpectedResponse, qdrant_unavailable)

    @application.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", tags=["health"])
    async def ready(request: Request) -> dict[str, str]:
        await request.app.state.database.ping()
        await request.app.state.vector_store.ping()
        return {"status": "ready"}

    return application


app = create_app()
