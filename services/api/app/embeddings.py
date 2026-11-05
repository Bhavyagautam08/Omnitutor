from __future__ import annotations

import math
from collections.abc import Sequence

from google import genai
from google.genai import types


class EmbeddingNotConfigured(Exception):
    pass


class EmbeddingProviderError(Exception):
    pass


class GeminiEmbedder:
    def __init__(self, *, api_key: str | None, model: str, dimension: int) -> None:
        self._api_key = api_key.strip() if api_key else None
        self._model = model
        self._dimension = dimension
        self._client: genai.Client | None = None

    async def embed_query(self, query: str) -> list[float]:
        if not self._api_key:
            raise EmbeddingNotConfigured
        if self._client is None:
            self._client = genai.Client(api_key=self._api_key)
        try:
            response = await self._client.aio.models.embed_content(
                model=self._model,
                contents=query,
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_QUERY",
                    output_dimensionality=self._dimension,
                ),
            )
        except Exception as error:
            raise EmbeddingProviderError(type(error).__name__) from error
        embeddings = response.embeddings or []
        if len(embeddings) != 1 or embeddings[0].values is None:
            raise EmbeddingProviderError("Gemini returned no query embedding")
        vector = list(embeddings[0].values)
        if len(vector) != self._dimension or not all(math.isfinite(value) for value in vector):
            raise EmbeddingProviderError("Gemini returned an invalid embedding dimension or value")
        return vector

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None


class StaticEmbedder:
    """Deterministic embedding provider for integration tests only."""

    def __init__(self, vectors: Sequence[float] | dict[str, Sequence[float]]) -> None:
        self._vectors = vectors

    async def embed_query(self, query: str) -> list[float]:
        vector = self._vectors[query] if isinstance(self._vectors, dict) else self._vectors
        return list(vector)

    async def close(self) -> None:
        return None
