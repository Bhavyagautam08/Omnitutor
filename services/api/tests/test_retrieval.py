import pytest

from app.retrieval import RetrievalService
from app.vector_store.qdrant import SearchHit


class FixtureDatabase:
    async def owns_notebook(self, learner_id: str, notebook_id: str) -> bool:
        return learner_id == "learner-1" and notebook_id == "notebook-1"

    async def fetch_ready_chunks(
        self, *, learner_id: str, notebook_id: str, chunk_ids: list[str]
    ) -> list[dict[str, object]]:
        return [
            {
                "chunk_id": "slide-chunk",
                "source_id": "pptx-source",
                "source_type": "PPTX",
                "filename": "slides.pptx",
                "kind": "SLIDE",
                "title": "Concurrency",
                "content": "Slide text",
                "topic_id": "topic-1",
                "page": None,
                "slide": 5,
                "timestamp_start": None,
                "timestamp_end": None,
            },
            {
                "chunk_id": "video-chunk",
                "source_id": "video-source",
                "source_type": "VIDEO",
                "filename": "lecture.mp4",
                "kind": "TRANSCRIPT",
                "title": None,
                "content": "Transcript text",
                "topic_id": None,
                "page": None,
                "slide": None,
                "timestamp_start": 12.5,
                "timestamp_end": 24.0,
            },
        ]


class FixtureVectorStore:
    async def search(self, *args: object, **kwargs: object) -> list[SearchHit]:
        return [
            SearchHit(chunk_id="slide-chunk", score=0.9, payload={}),
            SearchHit(chunk_id="video-chunk", score=0.8, payload={}),
        ]


class FixtureEmbedder:
    async def embed_query(self, query: str) -> list[float]:
        return [0.0] * 768


@pytest.mark.asyncio
async def test_retrieval_preserves_slide_and_video_citations() -> None:
    service = RetrievalService(
        FixtureDatabase(),  # type: ignore[arg-type]
        FixtureVectorStore(),  # type: ignore[arg-type]
        FixtureEmbedder(),  # type: ignore[arg-type]
    )

    result = await service.search(
        learner_id="learner-1",
        notebook_id="notebook-1",
        query="query",
        top_k=2,
    )

    assert result["results"][0]["citation"]["slide"] == 5
    assert result["results"][1]["citation"]["timestamp_start"] == 12.5
    assert result["results"][1]["citation"]["timestamp_end"] == 24.0
