# Python backend and authenticated retrieval

This is an **additive Python vertical slice**, not a full migration. The existing Next.js API,
TypeScript domain services and Prisma data layer remain the active application and were not changed.
Python independently implements authenticated notebook retrieval and an embedding reindex command.

## Local setup

From the repository root, start the existing services and run the Python API on loopback:

```powershell
docker compose up -d db
docker compose --profile vector up -d qdrant
Set-Location services\api
uv sync --extra dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8012 --reload
```

The service reads the repository-root `.env`. It uses the existing `DATABASE_URL`, `EMBED_MODEL`,
`APP_GEMINI_API_KEY`, and `QDRANT_*` settings. Prisma remains the sole schema migration owner; the
Python service performs no DDL. It maps the existing `Learner`, `Course`, `Source`, and `Chunk`
tables with SQLAlchemy 2.x and asyncpg.

The Python service adapts `DATABASE_URL` for asyncpg by removing Prisma's `schema` option and the
PostgreSQL `channel_binding` option, which asyncpg does not support. Other connection options,
including Neon `sslmode=require`, are retained.

Set `SESSION_SECRET` to the same value for both services (minimum 32 characters). If unset, Python
reads the same `data/cache/session-secret` file used by the TypeScript session code. It does not
create a different secret and fails closed if no shared secret is available. `APP_GEMINI_API_KEY`
is needed for real query embeddings. Keep the API bound to loopback unless it is behind a trusted,
authenticated TLS gateway.

The only API endpoints are `GET /health/live`, `GET /health/ready`,
`GET /api/v1/notebooks/health`, and the authenticated search route described below.

## PostgreSQL and Qdrant responsibilities

| Prisma model | Python read mapping | Responsibility |
|---|---|---|
| `Learner` | `id`, `passwordHash` | Account identity; only a password-backed learner can authenticate |
| `Course` | `id`, `ownerId` | Notebook ownership |
| `Source` | `id`, `courseId`, `type`, `filename`, `status` | Source association and `READY` status |
| `Chunk` | `id`, `sourceId`, `kind`, `content`, `embedding`, `topicId`, locators | Canonical content, legacy vectors, citation provenance |

PostgreSQL is authoritative for user, notebook, source, chunk text and citation metadata. Qdrant
stores vectors and compact identifiers/provenance only; complete document text is never included in
payloads. Search first verifies notebook ownership, filters Qdrant by authenticated learner and
notebook IDs, then fetches each canonical chunk again with independent owner/notebook/readiness
constraints. Missing, deleted, moved, or non-`READY` records are discarded even if a stale Qdrant
point is returned.

```text
Caller + omnitutor_session cookie
       │
       ▼
FastAPI: verify existing Node HMAC-SHA256 session + private Learner
       │  PostgreSQL: verify Course.ownerId == Learner.id
       ▼
Gemini embedContent(RETRIEVAL_QUERY, EMBED_MODEL, 768 dimensions)
       ▼
Qdrant cosine/HNSW search (account_id + notebook_id filters)
       ▼
PostgreSQL canonical chunk re-fetch
  (same owner + notebook + Source.status=READY)
       ▼
Ranked canonical text and original source/page/slide/video-time citations
```

### Legacy embedding-model limitation

The Prisma `Chunk` schema stores vectors as PostgreSQL `DOUBLE PRECISION[]` but stores no
per-chunk embedding model or version. Existing TypeScript ingestion uses the configured `EMBED_MODEL`
and explicitly requests 768-dimensional output. The reindex command checks dimension and finite
values, then labels legacy vectors using the **current** `EMBED_MODEL` and
`embedding_version="legacy-postgres"`. That model attribution cannot be independently proven from
the database; verify the current setting matches the model that generated the stored vectors before
reindexing production content. Every Gemini query response is also checked for exactly 768 finite
values. A live Gemini dimension/API call still requires a configured credential.

Qdrant's payload never includes canonical content. The Python service does not write synchronization
markers or modify Prisma tables; deletion/re-ingestion can leave stale Qdrant points, which retrieval
safely drops by rechecking canonical PostgreSQL ownership and `READY` status.

## Authenticated search API

`POST /api/v1/notebooks/{notebook_id}/search` reads the existing `omnitutor_session` cookie. It
verifies the same signed learner-id/expiry/HMAC-SHA256 token as the TypeScript app and then validates
that the learner still exists and has a password-backed account. It does not trust identity headers
or body fields. Notebook ownership is checked before calling Gemini or Qdrant.

Request:

```json
{
  "query": "Explain the main idea",
  "top_k": 6,
  "source_type": "PDF"
}
```

`query` must contain 1–2,000 non-whitespace characters; `top_k` is 1–20. Optional filters:
`source_id`, `source_type` (`PDF`, `PPTX`, or `VIDEO`), and `topic_id`. Unknown fields, including
`account_id` and `user_id`, are rejected.

Response:

```json
{
  "query": "Explain the main idea",
  "notebook_id": "notebook-id",
  "top_k": 6,
  "candidates": 1,
  "results": [
    {
      "chunk_id": "chunk-id",
      "source_id": "source-id",
      "source_type": "PDF",
      "filename": "lesson.pdf",
      "kind": "TEXT",
      "title": null,
      "content": "Canonical chunk text fetched from PostgreSQL.",
      "topic_id": "topic-id",
      "score": 0.91,
      "citation": {
        "page": 4,
        "slide": null,
        "timestamp_start": null,
        "timestamp_end": null
      }
    }
  ]
}
```

Status/error codes: `401 unauthenticated`, `404 notebook_not_found` (unknown and foreign notebooks
are indistinguishable), `422 invalid_request`, `502 gemini_error`, `503 gemini_not_configured`,
`503 postgres_unavailable`, and `503 qdrant_unavailable`. Error bodies use a `detail` object with
`code` and `message`; request validation returns an `error` object and issue locations.

## Reindex existing embeddings

Run from `services\api`:

```powershell
uv run python -m app.reindex --dry-run
uv run python -m app.reindex
```

It selects only chunks joined to `READY` sources in notebooks with a non-null owner, checks
dimensions/finiteness and provenance, and generates stable UUID5 point IDs from canonical chunk
IDs. Upserts are batched and safe to rerun. The JSON summary includes `processed`, newly `inserted`,
existing `updated`, invalid-record `skipped`, and batch `failed` counts. A dry run reads PostgreSQL
and, if a compatible collection exists, checks point IDs; it creates no collection and writes no
Qdrant points. PostgreSQL embeddings are never deleted.

## Tests

Default offline/unit suite and static checks:

```powershell
Set-Location services\api
uv run pytest
uv run ruff check app tests
uv run ruff format --check .
uv lock --check
```

Run real PostgreSQL/Qdrant integration tests with the existing Prisma schema migrated and both
services running:

```powershell
$env:OMNITUTOR_RUN_INTEGRATION = "1"
uv run pytest -m integration -s
```

The live test inserts synthetic users, notebooks, sources and chunks, performs actual reindexing and
authenticated Qdrant/PostgreSQL retrieval, verifies server-built HNSW, and removes its fixtures and
temporary collection. Its embedding provider is deterministic test data; it does **not** make a
Gemini API call. The ordinary in-memory Qdrant test is exact/brute-force and is not evidence of
server HNSW.
