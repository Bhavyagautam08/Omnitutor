# Environment variables

The local environment file is the repository-root `.env`, copied from `.env.example`. The
`.env.example` file contains names, local defaults, and placeholders only; put real values in `.env`
locally and in the hosting provider's secret/environment settings when deploying. Never commit an
actual `.env` file.

The browser does not need database, Gemini, or Qdrant credentials. Keep those server-side.

## Variables for the Next.js web service

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL URL used by Prisma. Apply the checked-in migrations to this database before starting the app. |
| `APP_GEMINI_API_KEY` | Yes for AI features | Google AI Studio key used for ingestion, generation, and TypeScript embedding calls. `APP_GEMINI_API_KEYS` may be used instead for key rotation. |
| `LLM_MODEL_FAST` | Yes for AI features | Gemini model used for extraction and grading. |
| `LLM_MODEL_STRONG` | Yes for AI features | Gemini model used for tutor answers, quizzes, and course building. |
| `EMBED_MODEL` | Yes for vector search | Embedding model; the configured model must produce the existing 768-dimensional vectors. |
| `SESSION_SECRET` | Yes in deployment | Random secret of at least 32 characters used to sign learner sessions. Use the same value for the Next.js and Python services. |
| `PYTHON_API_URL` | Yes when using Python retrieval | HTTPS origin of the Python API, without a path, query, or credentials. Plain HTTP is accepted only for loopback development. |
| `REFUSAL_TAU` | No | Retrieval similarity floor; defaults to `0.60`. |
| `NEXT_PUBLIC_API_MODE` | No | Frontend API mode (`auto`, `mock`, or `live`); set this at **build time** for the Next.js application. |

`LLM_MODEL_VERIFY`, `LLM_MODEL_FALLBACK`, `LLM_TIMEOUT_MS`, `APP_GEMINI_API_KEYS`, `LLM_RPM`,
`LLM_TPM`, `LLM_RPD`, `LLM_CONCURRENCY`, `EMBED_RPM`, `EMBED_TPM`, `EMBED_RPD`, `LLM_CACHE`,
`NEXT_PUBLIC_MOCK_DELAY_MS`, and `COURSE_NAME` are optional. Their defaults and comments are in
`.env.example`.

## Variables for the Python API

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | The same PostgreSQL database as the web service. Python reads the Prisma-managed tables; Prisma remains the only migration owner. |
| `SESSION_SECRET` | Yes in deployment | Must exactly match the web service's session secret. |
| `QDRANT_URL` | Yes | Qdrant HTTPS endpoint. |
| `QDRANT_API_KEY` | Depends on Qdrant provider | API credential for a protected/managed Qdrant cluster. |
| `QDRANT_COLLECTION` | No | Vector collection name; defaults to `omnitutor_chunks`. |
| `QDRANT_VECTOR_SIZE` | No | Vector dimension; must remain `768` for the configured embedding model. |
| `APP_GEMINI_API_KEY` | Yes for search | Python uses this key to embed retrieval queries. |
| `EMBED_MODEL` | Yes for search | Must be the same embedding model used to create the indexed vectors. |

The Python API also accepts optional `QDRANT_BATCH_SIZE`, `QDRANT_TIMEOUT_SECONDS`, and
`RETRIEVAL_TOP_K_MAX` settings. See [services/api/README.md](../services/api/README.md) for its API,
indexing, and test details.

## What goes where

For a hosted setup, enter variables in each service's environment/secret settings:

- **Next.js service:** web variables above, including `DATABASE_URL`, `SESSION_SECRET`,
  `PYTHON_API_URL`, and `NEXT_PUBLIC_API_MODE`.
- **Python API service:** Python variables above. Give it the same `DATABASE_URL` and
  `SESSION_SECRET` as the web service.
- **PostgreSQL and Qdrant:** provision these as services; their connection details are entered into
  the two application services as above.

`PYTHON_API_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION`, and `QDRANT_VECTOR_SIZE`
are not additional files. They are environment-variable names: set them in the root `.env` for
local development, or in the appropriate hosted service's settings for deployment. Only
`PYTHON_API_URL` belongs on the web service; Qdrant settings belong on the Python API.
