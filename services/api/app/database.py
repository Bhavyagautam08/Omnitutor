from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from sqlalchemy import (
    ARRAY,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    bindparam,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings

metadata = MetaData()

# These are read-only mappings of the existing Prisma tables. This metadata is never created
# or migrated by the Python service.
learner_table = Table(
    "Learner",
    metadata,
    Column("id", Text, primary_key=True),
    Column("passwordHash", Text),
)
course_table = Table(
    "Course",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("ownerId", Text, ForeignKey("Learner.id")),
)
source_table = Table(
    "Source",
    metadata,
    Column("id", Text, primary_key=True),
    Column("courseId", Text, ForeignKey("Course.id"), nullable=False),
    Column("type", ENUM("PDF", "PPTX", "VIDEO", name="SourceType", create_type=False), nullable=False),
    Column("filename", Text, nullable=False),
    Column(
        "status",
        ENUM("PENDING", "PROCESSING", "READY", "FAILED", name="SourceStatus", create_type=False),
        nullable=False,
    ),
)
chunk_table = Table(
    "Chunk",
    metadata,
    Column("id", Text, primary_key=True),
    Column("sourceId", Text, ForeignKey("Source.id"), nullable=False),
    Column("kind", ENUM("TEXT", "FIGURE", "SLIDE", "TRANSCRIPT", name="ChunkKind", create_type=False), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("title", Text),
    Column("content", Text, nullable=False),
    Column("page", Integer),
    Column("slide", Integer),
    Column("tStart", Float),
    Column("tEnd", Float),
    Column("embedding", ARRAY(Float), nullable=False),
    Column("topicId", Text),
)


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(create_async_engine(settings.sqlalchemy_database_url, pool_pre_ping=True))

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def is_private_learner(self, learner_id: str) -> bool:
        statement = select(learner_table.c.id).where(
            learner_table.c.id == learner_id,
            learner_table.c.passwordHash.is_not(None),
            learner_table.c.passwordHash != "",
        )
        async with self.engine.connect() as connection:
            return (await connection.execute(statement)).scalar_one_or_none() is not None

    async def owns_notebook(self, learner_id: str, notebook_id: str) -> bool:
        statement = select(course_table.c.id).where(
            course_table.c.id == notebook_id,
            course_table.c.ownerId == learner_id,
        )
        async with self.engine.connect() as connection:
            return (await connection.execute(statement)).scalar_one_or_none() is not None

    async def owns_source(self, learner_id: str, notebook_id: str, source_id: str) -> bool:
        statement = (
            select(source_table.c.id)
            .select_from(source_table.join(course_table, source_table.c.courseId == course_table.c.id))
            .where(
                source_table.c.id == source_id,
                source_table.c.courseId == notebook_id,
                course_table.c.ownerId == learner_id,
            )
        )
        async with self.engine.connect() as connection:
            return (await connection.execute(statement)).scalar_one_or_none() is not None

    async def fetch_ready_chunks(
        self,
        *,
        learner_id: str,
        notebook_id: str,
        chunk_ids: Sequence[str],
    ) -> list[dict[str, object]]:
        if not chunk_ids:
            return []
        statement = (
            select(
                chunk_table.c.id.label("chunk_id"),
                chunk_table.c.sourceId.label("source_id"),
                chunk_table.c.kind.label("kind"),
                chunk_table.c.title.label("title"),
                chunk_table.c.content.label("content"),
                chunk_table.c.page.label("page"),
                chunk_table.c.slide.label("slide"),
                chunk_table.c.tStart.label("timestamp_start"),
                chunk_table.c.tEnd.label("timestamp_end"),
                chunk_table.c.topicId.label("topic_id"),
                source_table.c.type.label("source_type"),
                source_table.c.filename.label("filename"),
            )
            .select_from(
                chunk_table.join(source_table, chunk_table.c.sourceId == source_table.c.id).join(
                    course_table, source_table.c.courseId == course_table.c.id
                )
            )
            .where(
                chunk_table.c.id.in_(bindparam("chunk_ids", expanding=True)),
                course_table.c.id == notebook_id,
                course_table.c.ownerId == learner_id,
                source_table.c.status == "READY",
            )
        )
        async with self.engine.connect() as connection:
            rows = await connection.execute(statement, {"chunk_ids": list(chunk_ids)})
            return [dict(row) for row in rows.mappings()]

    async def iter_reindex_rows(self) -> AsyncIterator[dict[str, object]]:
        statement = (
            select(
                chunk_table.c.id.label("chunk_id"),
                chunk_table.c.sourceId.label("source_id"),
                chunk_table.c.kind.label("kind"),
                chunk_table.c.content.label("content"),
                chunk_table.c.page.label("page"),
                chunk_table.c.slide.label("slide"),
                chunk_table.c.tStart.label("timestamp_start"),
                chunk_table.c.tEnd.label("timestamp_end"),
                chunk_table.c.topicId.label("topic_id"),
                chunk_table.c.embedding.label("embedding"),
                source_table.c.type.label("source_type"),
                source_table.c.status.label("source_status"),
                course_table.c.id.label("notebook_id"),
                course_table.c.ownerId.label("account_id"),
            )
            .select_from(
                chunk_table.join(source_table, chunk_table.c.sourceId == source_table.c.id).join(
                    course_table, source_table.c.courseId == course_table.c.id
                )
            )
            .where(source_table.c.status == "READY", course_table.c.ownerId.is_not(None))
            .order_by(course_table.c.ownerId, course_table.c.id, source_table.c.id, chunk_table.c.seq)
        )
        async with self.engine.connect() as connection:
            async with connection.stream(statement) as result:
                async for row in result.mappings():
                    yield dict(row)

    async def source_index_rows(self, *, learner_id: str, notebook_id: str, source_id: str) -> list[dict[str, object]]:
        statement = (
            select(
                chunk_table.c.id.label("chunk_id"),
                chunk_table.c.sourceId.label("source_id"),
                chunk_table.c.kind.label("kind"),
                chunk_table.c.page.label("page"),
                chunk_table.c.slide.label("slide"),
                chunk_table.c.tStart.label("timestamp_start"),
                chunk_table.c.tEnd.label("timestamp_end"),
                chunk_table.c.topicId.label("topic_id"),
                chunk_table.c.embedding.label("embedding"),
                source_table.c.type.label("source_type"),
                source_table.c.status.label("source_status"),
                course_table.c.id.label("notebook_id"),
                course_table.c.ownerId.label("account_id"),
            )
            .select_from(
                chunk_table.join(source_table, chunk_table.c.sourceId == source_table.c.id).join(
                    course_table, source_table.c.courseId == course_table.c.id
                )
            )
            .where(
                source_table.c.id == source_id,
                source_table.c.courseId == notebook_id,
                source_table.c.status.in_(("PROCESSING", "READY")),
                course_table.c.ownerId == learner_id,
            )
            .order_by(chunk_table.c.seq)
        )
        async with self.engine.connect() as connection:
            rows = await connection.execute(statement)
            return [dict(row) for row in rows.mappings()]


def local_session_secret_path() -> Path:
    from app.config import REPOSITORY_ROOT

    return REPOSITORY_ROOT / "data" / "cache" / "session-secret"
