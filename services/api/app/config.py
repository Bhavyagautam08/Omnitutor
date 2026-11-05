from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str | None = None
    session_secret: str | None = None
    gemini_api_key: str | None = Field(default=None, validation_alias="APP_GEMINI_API_KEY")
    embedding_model: str = Field(default="gemini-embedding-001", validation_alias="EMBED_MODEL")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = Field(default="omnitutor_chunks", min_length=1)
    qdrant_vector_size: int = Field(default=768, gt=0)
    qdrant_batch_size: int = Field(default=128, gt=0, le=2048)
    qdrant_timeout_seconds: float = Field(default=10.0, gt=0)
    retrieval_top_k_max: int = Field(default=20, gt=0, le=100)

    @property
    def sqlalchemy_database_url(self) -> str:
        if not self.database_url:
            raise ValueError("DATABASE_URL is required for PostgreSQL access")
        url = make_url(self.database_url)
        query = dict(url.query)
        query.pop("schema", None)
        return url.set(drivername="postgresql+asyncpg", query=query).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
