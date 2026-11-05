# Deployment

## Current status

This repository is **not ready for an unattended public production deployment**. It has no
deployment-provider configuration, and the current application has two deployment constraints:

1. Uploaded originals are written to `data/uploads` on the Next.js server's local filesystem.
   Serverless or ephemeral filesystems lose those files; a persistent disk only supports a
   single-instance setup until storage moves to durable object storage.
2. Ingestion runs as an in-process background promise in the web service. A restart or deployment
   can interrupt it, and there is no durable job queue or worker to resume it.

The README also notes that authentication is intentionally minimal and login attempts are not rate
limited. Treat a hosted instance as a private demo/staging deployment until durable storage/jobs,
rate limiting, and production authentication safeguards are implemented. Do not deploy the current
app to Vercel/serverless and assume uploads or ingestion will persist.

## Services and configuration

A hosted instance needs:

- A long-running Node.js service for `apps/web`.
- A long-running Python 3.12+ FastAPI service for `services/api`.
- PostgreSQL (Neon is supported by the configured `DATABASE_URL`).
- Qdrant with a 768-dimensional cosine collection.
- Google AI Studio API access for the configured Gemini and embedding models.

The browser talks to Next.js. Next.js calls the Python retrieval service over an HTTPS origin;
Python validates the signed learner session, reads canonical data from PostgreSQL, embeds search
queries with Gemini, and queries Qdrant. Configure `SESSION_SECRET` identically in both application
services. Do not expose PostgreSQL or Qdrant directly to browsers.

Use [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) for the full variable matrix. The root
`.env.example` is for local development only. Production secrets belong in the hosting provider's
secret manager/environment settings; never commit them.

## Build and start

Use a persistent Node.js service, not a serverless function:

```powershell
npm ci
npm run build
npm run start -w @sc/web
```

Set `NEXT_PUBLIC_API_MODE` before the build. Configure a persistent disk mounted at the repository
root's `data` directory for `data/uploads`; do not store uploaded course material in an ephemeral
build directory. A single persistent web instance is the current upper bound, not a high-availability
configuration.

For the Python service, from `services/api`:

```powershell
uv sync --active --frozen --no-dev
uv run --active uvicorn app.main:app --host 0.0.0.0 --port $env:PORT
```

For Render, set the service root directory to `services/api`, the build command to
`pip install uv && uv sync --active --frozen --no-dev`, and the start command to
`uv run --active uvicorn app.main:app --host 0.0.0.0 --port $PORT`. `--active` tells uv to use
Render's activated environment instead of creating a second `.venv` under the service directory.
Set `/health/ready` as the health check path. The API checks PostgreSQL and Qdrant during startup,
so connection failures appear in the service logs and prevent it from becoming ready. The web
service has `/api/health`.

### Render demo deployment for the web app

To support uploads with the current local-file implementation, deploy the Next.js app as a second
Render Web Service instead of relying on the Vercel deployment:

1. Create a Web Service for the same repository and `deployment-ready` branch. Leave **Root
   Directory** unset so Render checks out the monorepo root.
2. Set **Build Command** to `npm ci && npm run build -w @sc/web`.
3. Set **Start Command** to `npm run start -w @sc/web`.
4. Add a persistent disk mounted at `/opt/render/project/src/data`. The Node service must run as a
   single instance. Persistent disks require a Render plan that supports disks.
5. Add the web-service environment variables listed in
   [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md). Set `NEXT_PUBLIC_API_MODE` before the
   build. Set `PYTHON_API_URL` to the HTTPS origin of the Python Render service. Use the same
   `DATABASE_URL` and `SESSION_SECRET` on both Render services.
6. Apply Prisma migrations once to that `DATABASE_URL` using `npm run db:deploy` from the repository
   root. Confirm the target database and branch before running this command.
7. Deploy, then verify the web service at `/api/health` and the Python service at `/health/ready`.

The browser uses the Next.js service's same-origin `/api/*` routes; those routes call the Python
service server-to-server for retrieval. The Vercel deployment may remain available as a UI preview,
but its uploads will not be durable with the current storage implementation. Use the Render web
service URL as the working demo/resume link after it is deployed.

## Initial provisioning order

1. Provision PostgreSQL, Qdrant, and Gemini access. Restrict PostgreSQL and Qdrant to application
   access; use TLS and provider-issued credentials.
2. Set production environment variables on both application services. In particular, use one strong
   `SESSION_SECRET` in both, set the web service's `PYTHON_API_URL` to the Python service's HTTPS
   origin, and set Qdrant variables only on the Python service.
3. Apply Prisma migrations **once** against the intended production database:

   ```powershell
   npm run db:deploy
   ```

   Verify that the command is using the production `DATABASE_URL`; never run migrations or tests
   against a database containing data you need to preserve.
4. Deploy the Python API and confirm `/health/ready`; deploy the web service and confirm
   `/api/health`.
5. Before adding real user material, configure backups, retention, access controls, and monitoring.
   Confirm that upload storage survives a service restart and test restore procedures.

The Python indexing/reindex commands are documented in
[services/api/README.md](../services/api/README.md). Do not point a dry-run, integration test, or
seed command at production unless you have reviewed exactly what it reads or writes.

## Before public production

At minimum, move uploads to durable private object storage, move ingestion into a durable queue and
worker, rate-limit authentication and resource-intensive endpoints, and establish backup/restore and
operational monitoring. Revisit horizontal scaling after removing assumptions about local files and
on-disk caches. Update this guide and add provider-specific configuration only after those choices
are implemented and verified.
