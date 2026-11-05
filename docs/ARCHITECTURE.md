# Study Companion — Architecture (v2)

IIT Mandi Multimodal AI Hackathon 2026 · **Track D: Personalized Tutoring & Adaptive Learning**

This document is the shared reference for both teammates. The design is frozen: change it only by agreement, and record the change here.

> **Implementation status (audited against the code, 2026-10-14).** Everything below describes the code as built.
> Where the build differs from the original plan, §0 says how and why; nothing planned is described as built.
> The additive Python retrieval service is described in [services/api/README.md](../services/api/README.md);
> deployment constraints and service environment variables are in [DEPLOYMENT.md](DEPLOYMENT.md) and
> [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md).

---

## 0. Deviations from the original plan (all deliberate, all in the code)

| Planned | Built instead | Why |
|---|---|---|
| No auth; learner picker in the header | **Accounts** (Module 16): name + password, scrypt hashes, HMAC-signed HttpOnly session cookie; learner-scoped routes return 401 unless the session owns the learner. Passwordless learners (`POST /api/learners`, used by tools/tests) stay open and are not listed | Requested by the user: private learner data. Course material stays shared |
| Topics only | Topics **plus concepts (subtopics)**: each topic lists the concept labels ingestion gave its chunks, merged and ranked, each cited | Track D asks for topics/concepts/prerequisites; no extra Gemini call |
| One video call per file | One call per **≤ 60 s window** (`videoMetadata` offsets) | Timestamps stay accurate (mean error 0.3 s on the real video) |
| Blind verify with the generating model | Verify with a **different model** (`LLM_MODEL_VERIFY`, cross-model) | Track D lists cross-model verification; falls back to `LLM_MODEL_FAST` if unset |
| `tools/eval/ragas.py`, `tools/eval/score.ts`, `tools/sim` (teammate) | `tools/e2e/src/eval-rag.ts` (RAGAS metric **definitions** re-implemented with a Gemini judge; not the `ragas` package) and `tools/e2e/src/sim-students.ts` (no LLM) | The teammate deliverables did not arrive; one TypeScript toolchain |
| One shared course; learner picker | **Notebooks** (Module 18): each account owns any number of private courses ("notebooks"); one is active (`Learner.activeCourseId`), and every course-scoped route resolves it through `courseFor()`, so sources, topics, tutor history, quizzes and mastery are per notebook. Another account's notebook, file or data returns 401. Requests without a session use the shared evaluation course | Users asked for private, NotebookLM-style notebooks |
| Tutor adapts to mastery only | **Onboarding profile** (Module 17): tone (friendly / funny / serious / concise, which also sets the sampling temperature 0.45 / 0.8 / 0.15 / 0.2), explanation style, prior level and goal; added to the tutor's system prompt. The tutor prompt (v2) now *teaches*: it explains in its own words with cited material blocks and labelled outside analogies/examples | Personalised tutoring is the core of Track D |
| Evaluation numbers in docs only | **Evaluation page** (Module 19): `npm run eval:rag` / `eval:sim` write validated reports to `data/eval/` (in the repo); `GET /api/eval/report` serves them and `/evaluation` shows the metric matrix (overall × PDF / slides / video × test set), every question, and the simulator. The judge is `LLM_MODEL_VERIFY`, a different model from the tutor's | Track D requirement 5; judges can see the measured results |
| No course map (P1 only) | **Course map** (Module 20): `/map`, a radial mind map built from topics, concepts, prerequisites and mastery; no new API | Track D optional item; uses data the app already has |
| No revision material | **Flashcards** (Module 21): generated per topic from that topic's excerpts → code checks → verified by a second model; cited; Leitner boxes; each review is mastery evidence (weight 0.3) | Track D optional item (revision material) |
| One Gemini key | `APP_GEMINI_API_KEYS` rotation on 429 or a rejected key, `LLM_MODEL_FALLBACK` on 5xx/429/404, per-request timeout `LLM_TIMEOUT_MS` (120 s) | Free-tier quotas and overloaded models were the main failure mode in testing |

The teammate's early design notes (`docs/product-requirements.md`, `system-architecture.md`, `technical-design.md`, `database-and-rag.md`, `api-contracts.md`, `delivery-plan.md`, `development-deployment.md`, `evaluation.md`, `security-privacy.md`, `glossary-and-decisions.md`) describe a **different, unbuilt design** (pgvector, full-text hybrid search, per-course authorization). Each carries a banner saying so; this file and [API_SPEC.md](API_SPEC.md) are authoritative.

---

## 1. What we are building

Students upload a **lecture video, a textbook PDF and a slide deck**. The app turns them into one searchable knowledge base organised by topic. On top of that knowledge base it provides:

1. **Grounded tutor chat.** Every claim cites the exact PDF page, slide or video timestamp. Clicking a citation opens the source at that spot. Questions about things not in the material are declined or flagged.
2. **Adaptive quizzes.** MCQ, short-answer and numerical questions on topics the student picks. Every question is verified before it is shown, no question is repeated, and every piece of feedback carries a citation. After each quiz there is a report on weak topics and misconceptions.
3. **A learner model.** Mastery is tracked per topic (BKT) and updated after every quiz and every chat. A diagnostic quiz handles cold start.
4. **Evaluation.** RAGAS-style metrics on our own test set (including off-material questions), plus a simulated-student study comparing adaptive and random question selection.

How it is judged: KB & Grounding 20 · Assessment 15 · Personalization 20 · UX & Demo 15 · System Eval 15 · Tech 15.

---

## 2. System overview

```
                    ┌──────────── apps/web — Next.js (one local process) ─────────────────────────┐
                    │                                                                             │
  Browser ──────────┤  Pages: /sources  /tutor  /quiz  /quiz/[id]/report  /dashboard              │
  (signed in)      │  API routes (app/api/*) — thin: parse request, check the session cookie,    │
                    │  call @sc/core, return JSON. Also /login                                    │
                    └──────────────┬──────────────────────────────────────────────────────────────┘
                                   │ imports
                    ┌──────────────▼──────────── packages/core (@sc/core) ────────────────────────┐
                    │   ingest/{pdf,pptx,video} ─┐                                                 │
                    │   build (topics+prereqs)   │                                                 │
                    │   retrieve ─► tutor        ├─► llm.ts ──► Gemini API (ONLY file calling it)  │
                    │   quiz ─► bkt              ┘                                                 │
                    └──────────────┬──────────────────────────────┬──────────────────────────────┘
                                   │ @sc/db (Prisma)              │ @sc/schemas (Zod contract,
                             ┌─────▼─────┐                        │  shared by web + core + tools)
                             │ Postgres  │ docker-compose :5434   │
                             └───────────┘                        │
   data/uploads/ ◄── originals, served by /api/files/[id] (HTTP Range)
   tools/e2e/src/eval-rag.ts ──HTTP──► /api/eval/answer   tools/e2e/src/sim-students.ts (no LLM)
```

**Key decisions**

| Decision | Why |
|---|---|
| One Next.js process run locally, no serverless | Ingestion runs for minutes; serverless timeouts would kill it. Live deployment isn't required. |
| No worker or queue | `POST /api/sources` starts ingestion without awaiting it; the UI polls `Source.status`. That's enough for a demo. |
| Python retrieval is an additive service (`services/api`) | Authenticated search uses Gemini query embeddings and Qdrant, then re-fetches canonical content and ownership from PostgreSQL. It does not replace Prisma migrations or the TypeScript ingestion pipeline. |
| Hosted deployment is not production-ready | Originals use local disk and ingestion is an in-process background task; see [DEPLOYMENT.md](DEPLOYMENT.md) for the hosting constraints. |
| Postgres only, no pgvector | Embeddings are stored as `Float[]` and cosine is computed in JS. A few thousand chunks fit easily in memory. |
| `packages/core/src/llm.ts` is the only Gemini caller | One place for the rate limiter, cache, retries, key rotation, model fallback, request timeout, Zod validation, typed errors and model selection. It passes `apiKey` explicitly from `APP_GEMINI_API_KEY(S)`; the SDK would otherwise fall back to a machine-wide `GEMINI_API_KEY`. The SDK's own HTTP retries stay off (we never set `retryOptions`), so every request is counted by our limiter. |
| Originals kept in `data/uploads/` | Citations open the real file. Gemini Files API uploads expire after 48 h, but our copies don't. |
| Model IDs come from env | AI Studio projects differ in which models are enabled. |
| Turborepo monorepo (npm workspaces) | Clear ownership boundaries. Domain logic (`@sc/core`) has no Next.js imports, so the simulator and eval can import it directly. |
| One `.env` at the repo root | `apps/web/scripts/next.mjs` loads it before Next starts, and `packages/db/prisma.config.ts` loads it for the Prisma CLI. |
| Contract-first API with a typed client (`@sc/client`) | The UI never hand-writes fetch calls. Responses are validated against `@sc/schemas`; failures become typed `ApiError`s; `auto` mode mocks endpoints whose registry status is `planned`. |
| Registry status is enforced | A test fails unless every `live` endpoint is exported by its route file and no `planned` endpoint exists, so the frontend can trust `status`. |
| Thin routes over `@sc/core` services | Routes parse input (`readBody` / `readQuery`), call core, and return `json(ResponseSchema, data)` (validated). One `handle()` wrapper maps errors to `ApiErrorBody`. |
| Uploads are raw bytes, streamed to disk | No multipart buffering. A 500 MiB cap is enforced while streaming, and the content must match the extension (PDF / PPTX / MP4 checks, MP4 duration ≤ 20:00). |
| **Ingestion is explicit** (`POST /api/sources/[id]/ingest`) | Uploading is free; only the ingest call spends Gemini quota. This gives the user control, and tests never trigger API calls. The same call re-runs or retries. |
| **Quota guard + result cache in `@sc/core`** | `limiter.ts` enforces RPM/TPM/concurrency/daily budgets below the verified free tier (embeddings count per text). `cache.ts` stores deterministic results on disk, so re-ingesting an unchanged file costs 0 requests. |

---

## 3. Tech stack

| Layer | Choice (pinned) |
|---|---|
| Framework | Next.js 16.4 (App Router, Turbopack), React 19, TypeScript 5 |
| Styling | Tailwind CSS 4 |
| DB | Postgres 16 (docker-compose), Prisma 6.19 |
| LLM | Gemini via `@google/genai` 2.29. Models: `LLM_MODEL_FAST`, `LLM_MODEL_STRONG`, `EMBED_MODEL` (768-dim) |
| Validation | Zod 4 |
| Parsing | `pdf-lib` (split PDF into single pages), `jszip` (PPTX) |
| Charts | Recharts 3 |
| Scripts | `tsx` for TS tools (added in Module 1), Python 3 for RAGAS |

> Note: Next.js 16 differs from older versions. Read `node_modules/next/dist/docs/` before using an API you're unsure of. Cache Components is on, so dynamic route handlers call `await connection()`.

---

## 4. Repository layout (Turborepo + npm workspaces)

```
multimodal/
├─ apps/
│  └─ web/                         @sc/web — Next.js: UI pages + thin API routes
│     ├─ app/
│     │  ├─ api/
│     │  │  ├─ _lib/http.ts               # handle(), json(), readBody(), readQuery(): shared route plumbing (private folder)
│     │  │  ├─ health/route.ts            # ✅ DB + env (+ ?gemini=1 model check)
│     │  │  ├─ learners/route.ts          # ✅ list/create learners
│     │  │  ├─ sources/route.ts           # ✅ GET list · POST raw-bytes upload (?filename=)
│     │  │  ├─ sources/[id]/route.ts      # ✅ DELETE source (+ file)
│     │  │  ├─ sources/[id]/ingest/route.ts  # ✅ POST: start/re-run ingestion (job continues via after())
│     │  │  ├─ files/[id]/route.ts        # ✅ GET/HEAD originals with HTTP Range (video seek, PDF)
│     │  │  ├─ build/route.ts             # topics + prerequisites + chunk tagging (Module 9)
│     │  │  ├─ topics/route.ts            # (Module 9)
│     │  │  ├─ chat/route.ts              # grounded tutor + history (Module 10)
│     │  │  ├─ eval/answer/route.ts       # tutor answer + contexts for RAGAS (Module 10)
│     │  │  ├─ quiz/…                     # create / get / submit / report (Modules 12–13)
│     │  │  └─ mastery/route.ts           # dashboard data (Module 14)
│     │  └─ sources/ tutor/ quiz/ quiz/[id]/report/ dashboard/   # pages (teammate)
│     ├─ scripts/next.mjs                 # launches Next with the root .env loaded
│     ├─ instrumentation.ts               # at server start: interrupted ingestions → FAILED
│     └─ next.config.ts
├─ packages/
│  ├─ db/                          @sc/db — Prisma schema, migrations, client singleton
│  │  ├─ prisma/schema.prisma  prisma/seed.ts  prisma/verify.ts
│  │  ├─ prisma.config.ts                 # points the Prisma CLI at the root .env
│  │  └─ src/index.ts                     # prisma, Prisma, model types
│  ├─ schemas/                     @sc/schemas — SHARED CONTRACT (Zod) + paths + registry + mocks
│  │  └─ src/  common · paths · citation · course · tutor · quiz · mastery · eval · system · endpoints · mocks
│  ├─ client/                      @sc/client — typed HTTP client on the contract (used by UI + tools)
│  └─ core/                        @sc/core — all domain logic, no Next.js imports
│     └─ src/  llm · limiter · cache · errors · storage · media · course · learners · sources   (✅ 1–4.5)
│              ingest/{index,pdf}  (✅ Module 5: PDF) · retrieve · tutor · quiz · bkt   (Modules 6–13)
│              testing.ts                  # fixtures for tests (@sc/core/testing)
├─ tools/
│  ├─ smoke/                       @sc/smoke — Gemini smoke tests + list-models (Module 1)
│  ├─ e2e/                         @sc/e2e — end-to-end tests against the running server (npm run test:e2e)
│  │                                 + stage1-pdf.ts / stage1/expectations.json: real-data ingestion check (npm run stage1:pdf)
│  ├─ eval/                        ragas.py, score.ts, testset.json (teammate, Module 15)
│  └─ sim/                         simulated students (teammate, Module 15)
├─ data/uploads/                   uploaded originals (gitignored)
├─ data/cache/                     Gemini result cache + usage.json ledger (gitignored)
├─ services/api/                   Python FastAPI retrieval + Qdrant indexing service
├─ data/demo/                      demo dataset (gitignored, except README)
├─ docs/                           ARCHITECTURE · DEPLOYMENT · ENVIRONMENT_VARIABLES · PROGRESS · API_SPEC · INGESTION_SPEC · INTEGRATION_AUDIT · TEAMMATE_BRIEF · postman/
├─ docker-compose.yml              Postgres only, host port 5434
├─ turbo.json   package.json   tsconfig.base.json
├─ .env.example                    copy to .env (ONE env file for the whole repo)
└─ CLAUDE.md                       instructions for Claude Code
```

Workspace packages export TypeScript source directly (`"exports": "./src/index.ts"`), and Next compiles them through `transpilePackages`, so there's no build step for packages. Import them as `@sc/db`, `@sc/core`, `@sc/schemas` and `@sc/client`.

---

## 5. Data model (Prisma)

Source of truth: `packages/db/prisma/schema.prisma` (commented). Summary:

```
Course ─┬─< Source (type PDF|PPTX|VIDEO, filename, mimeType, sizeBytes, path,
        │           status PENDING|PROCESSING|READY|FAILED, error?, progressDone/Total,
        │           pageCount?, durationSec?, geminiFileName/Uri/ExpiresAt?)
        │       └─< Chunk (kind TEXT|FIGURE|SLIDE|TRANSCRIPT, seq, title?, content,
        │                  page? | slide? | tStart?,tEnd?, conceptLabels[], embedding Float[768], topicId?)
        ├─< Topic (seq, name, description, prereqIds[], embedding Float[768])
        │        └─< Question (type MCQ|SHORT|NUMERIC, difficulty 1-3, stem, stemHash, options?, answer,
        │                      distractorMisconceptions?, explanation, sourceChunkIds[], verified,
        │                      verifierAnswer?, assessmentId?, position?)
        ├─< Assessment (learnerId, kind DIAGNOSTIC|QUIZ|MOCK, status IN_PROGRESS|SUBMITTED,
        │               scopeTopicIds[], report?, submittedAt?)
        │        └─< Attempt (questionId, response, correct, score 0..1, feedback, misconception?)
        │                                                        @@unique([assessmentId, questionId])
        └─< ChatMessage (learnerId, role USER|ASSISTANT, content, blocks?, coverage?, refused, topScore?)
Learner (name, passwordHash? — set = private account)
         ├─< Mastery (topicId, p=0.3, n=0)                       @@unique([learnerId, topicId])
         └─< MasteryEvent (topicId, before, after, source QUIZ|CHAT, attemptId?, chatMessageId?)
```

**JSON shapes:**
- `answer`: MCQ `{index}` · NUMERIC `{value, unit?}` · SHORT `{reference, keyPoints[]}`
- `response`: `{index}` · `{value}` · `{text}`
- `distractorMisconceptions`: `{"<option index>": "<label>"}`
- `feedback`: `{text, citations: Citation[]}`
- `blocks`: `[{text, origin, citations: Citation[]}]`. Citations are snapshotted, so chat history survives re-ingestion.

**Delete behaviour:**
- Deleting a course cascades to everything under it.
- Deleting a source deletes its chunks.
- Deleting a topic untags its chunks and deletes its questions, mastery and events. So **build topics once, before learners start**.
- Once topics exist, ingesting (or re-ingesting) a source tags its new chunks to the nearest **stored** topic vector (`Topic.embedding`), the build's own rule, with no Gemini call. A rebuild is still needed to get topics *for* new material.
- Deleting a learner deletes all of their data.

**Citation invariant:** every chunk has exactly one locator. PDF chunks have `page`, slide chunks have `slide`, video chunks have `tStart`/`tEnd`. A citation is a `chunkId`, and the UI resolves it to `{sourceId, locator, excerpt}`.

---

## 6. Pipelines

### 6.1 Ingestion (`POST /api/sources`, then `POST /api/sources/[id]/ingest`)
1. ✅ (Module 4) Upload: stream the raw bytes to `data/uploads/<uuid>.<ext>` (500 MiB cap), check the content matches the extension, record `pageCount` / `durationSec`, and create a `Source` with status `PENDING`. **No Gemini call.**
2. ✅ (Module 5) Ingest on request: status `PROCESSING`, then return immediately while the job continues via Next's `after()` (`apps/web/app/api/sources/[id]/ingest/route.ts`). The UI polls `GET /api/sources`. A failing run → `FAILED` + `error`; re-running is cheap (cache). Server start marks interrupted runs `FAILED`.
3. Per type:
   - **PDF:** `pdf-lib` splits the file into single-page PDFs. Each page gets one Gemini call (concurrency 4), which returns `{blocks:[{kind:text|figure|table, content}], conceptLabels}`. Text blocks become chunks. **Each figure becomes its own chunk** holding a description of the diagram (this is how "use info from figures" is met). Locator: `page`.
   - **PPTX:** JSZip reads slide order from `ppt/presentation.xml` plus its rels, text from `<a:t>` runs, and images through the slide rels into `ppt/media/*`. Slides go to Gemini in batches of 5 (text + images) and come back as one chunk per slide, with figure descriptions folded in. Locator: `slide`.
   - **Video (mp4, ≤ 20 min):** upload through the Gemini Files API, poll until `ACTIVE`, then one call per **≤ 60 s window** (`videoMetadata` start/end offsets), each returning `segments:[{tStart,tEnd,transcript,onScreen,conceptLabels}]`; a window whose segments don't cover it is rejected. Locator: `tStart/tEnd`.
4. Embed every chunk (`EMBED_MODEL`, 768-dim) and store it as `Float[]`; if the course has topics, tag each chunk to its nearest topic vector.
5. Set status to `READY`, or to `FAILED` with an `error`.

### 6.2 Course build (`POST /api/build`)
One LLM call over an outline of every chunk (locator, title or excerpt, concept labels) returns **5–8 topics** with descriptions and **prerequisite links** (a prerequisite must be an earlier topic; others are dropped). Each topic's name + description is embedded and stored on the topic, and each chunk is tagged with its nearest topic by cosine. Refused with 409 if learners have progress on the topics, unless `force: true` (decision Q9).

**Concepts (subtopics):** `GET /api/topics` returns, per topic, up to 8 concepts: the ingestion concept labels of its chunks, merged case-, punctuation-, spacing- and plural-insensitively (`conceptKey`), ranked by how many chunks teach them, each with up to 2 citations. Pure code (`topicConcepts` in `build.ts`), no Gemini call.

### 6.3 Tutor (`POST /api/chat`)
0. Whole-message small talk ("hi", "thanks") gets a friendly reply without retrieval or an LLM call.
1. Embed the query (short follow-ups are retrieved together with the previous question) and take the top 6 chunks by cosine.
2. Load the learner's mastery and any weak prerequisites of the matched topics.
3. Make one structured call:
   ```
   { coverage: "SUPPORTED" | "PARTIAL" | "NOT_COVERED",
     blocks: [{ origin: "material" | "outside", text, citations: ["C1", "C4"] }],   // excerpt ids from the prompt
     learnerEvidence: [{ topic: "T3", signal: "understands" | "misconception" }] }  // topic keys from the prompt
   ```
4. Server-side guards (these make grounding trustworthy):
   - Drop any citation that isn't in the retrieved set.
   - Relabel a `material` block with no citations as `outside`.
   - **Refuse** if the top cosine score is below `REFUSAL_TAU` or `coverage == not_covered`.
   - Apply a damped BKT update (30% weight, at most one per topic per turn) from `learnerEvidence`. This is where "update after conversation" comes from.
   - The turn (question + answer) is stored only once answered, so a failed call leaves no unanswered question in the history.
5. **Citation panel:** PDF in an iframe at `#page=N` with the excerpt beside it; a slide as a reconstructed card; video in a `<video>` element seeked to `tStart` (needs HTTP Range on `/api/files/[id]`). **All three must work in the demo.**

### 6.4 Quiz (`POST /api/quiz`, `POST /api/quiz/[id]/submit`)
- **Topic selection:** weighted by `1 − mastery`. **Difficulty by mastery band:** < 0.4 easy, 0.4–0.7 medium, > 0.7 hard.
  - Diagnostic: 1 question per topic. Quiz: student-chosen scope. Mock: all topics, 10 questions.
- **Generate:** one batched call for the requested slots **+50% extra candidates** (each topic sees only its own excerpts). MCQ distractors each carry a *misconception label*. Code checks reject unknown topics, malformed answers, citations that aren't real excerpts of the question's own topic, and repeats. The prompt includes stems the student has already seen, and any question whose `stemHash` was seen before is rejected. This is how we guarantee no repeats.
- **Verify:** one batched *blind* call, made with **a different model** (`LLM_MODEL_VERIFY`), that answers the questions without seeing our answers. Questions where the two disagree are dropped. Numeric answers are compared in code with 1% tolerance.
- **Grade on submit:** the quiz is first claimed atomically (a concurrent submit gets 409, so mastery is never updated twice). MCQ and numeric are graded in code. Short answers are graded in one batched LLM call in which every grade must echo its answer's first words (misaligned grades are re-graded one by one); a blank short answer counts as unanswered. Feedback cites source chunks. Each answer triggers a BKT update.
- **Report:** per-topic score, mastery delta, misconceptions detected (from the chosen distractors), and citations to revisit.

### 6.5 Learner model (`packages/core/src/bkt.ts`)
Standard BKT per topic: prior `p0 = 0.3`, learn `0.15`, slip `0.1`, guess `0.25` (MCQ) / `0.1` (short) / `0.05` (numeric).
Each update writes `Mastery` and a `MasteryEvent(before, after, source)`, which the dashboard history chart reads.

---

## 7. Shared contract (`packages/schemas`, imported as `@sc/schemas`)

**Source of truth: `packages/schemas/src/*.ts`** (Zod). The API validates requests and responses with it, and the UI and `tools/eval` import its types. Change it only by agreement; `npm run test` must stay green.

| File | Shapes |
|---|---|
| `common.ts` | Enums (kept in sync with Prisma by a test), `Id`, `IsoDate`, `Probability`, `ApiErrorBody`, `API_ERROR_STATUS` |
| `paths.ts` | URL builders for every route (`paths.quiz(id)`, `paths.mastery(learnerId)`, …) |
| `system.ts` | `HealthQuery`, `HealthResponse` |
| `citation.ts` | `Citation`: PDF `{page}` · PPTX `{slide}` · VIDEO `{tStart,tEnd}`, plus `excerpt`. Helpers `formatLocator`, `citationHref`, `formatTime` |
| `course.ts` | `SourceView`, `UploadQuery`, `UploadResponse`, `DeleteSourceResponse`, `TopicView`, `BuildResponse`, `Learner`, `LearnerQuery`, `ACCEPTED_EXTENSIONS`, `MAX_UPLOAD_MB`, `MAX_VIDEO_MINUTES`, `sourceTypeFromFilename()` |
| `tutor.ts` | `ChatRequest`, `TutorBlock`, `TutorAnswer`, `ChatMessageView`, `MasteryUpdate` |
| `quiz.ts` | `CreateQuizRequest`, `QuizQuestion` (client-safe), `Quiz`, `QuizResponse`, `SubmitRequest`, `QuizReport` (`AttemptResult.options` for MCQ) |
| `mastery.ts` | `MasteryView`, `masteryBand()`, `MASTERY_BANDS` (0.4 / 0.7) |
| `eval.ts` | `EvalAnswerResponse`, `TestSetItem`, `TestSet`, `testSetGaps()` |
| `endpoints.ts` | Registry: method, route pattern, query, request, response, **status (`live`/`planned`)**, module; `NON_JSON_ROUTES` |
| `mocks.ts` | Mock response for every endpoint plus extra UI states (`@sc/schemas/mocks`); internally consistent (tested) |
| `packages/client` | `createApiClient({ mode, baseUrl })` → typed methods; `ApiError` (`code`, `status`) |

**Rules the contract enforces (tested):**
- **Grounding:** a `material` block cites at least one excerpt, and an `outside` block never cites. A refused answer has no material blocks, and `NOT_COVERED` is always refused.
- **One locator per citation:** a PDF citation needs `page`, a slide needs `slide`, a video needs `tStart ≤ tEnd`.
- **No answer leaks:** a `QuizQuestion` never contains the answer, explanation or misconception labels. Unknown keys are stripped.
- **Mastery bands:** `band` must match `p`, and the same thresholds drive quiz difficulty.
- **Errors:** every error response is `{ error: { code, message } }`, with code `bad_request | not_found | not_ready | rate_limited | llm_error | internal` and fixed statuses (400/404/409/429/502/500). Every success is 200.
- **Registry honesty:** `status: "live"` ⇔ the route file exports the method (test, both directions). `paths` builders match the route patterns and query schemas (test).
- **Response validation:** routes send data through `json(schema, data)`, so a contract violation is a 500, never a silently wrong payload. The client validates again.

---

## 8. API surface

| Method & path | Request → Response (`@sc/schemas`) | Module |
|---|---|---|
| `GET /api/health[?gemini=1]` | → `HealthResponse` (200 / 503) | 0 ✅ |
| `GET /api/learners` / `POST /api/learners` | → `Learner[]` (open profiles only) / `CreateLearnerRequest` → `Learner` (open profile, for tools/tests) | 4 ✅ |
| `POST /api/auth/signup` · `POST /api/auth/login` | `Credentials` → `Learner` + session cookie (400 name taken · 401 wrong name or password) | 16 ✅ |
| `POST /api/auth/logout` · `GET /api/auth/session` | → `SessionView` (`{ learner }`, null when signed out) | 16 ✅ |
| `POST /api/sources?filename=` | raw file bytes → `UploadResponse` (`PENDING`) | 4 ✅ |
| `GET /api/sources` | → `SourceView[]` | 4 ✅ |
| `DELETE /api/sources/[id]` | → `DeleteSourceResponse` (409 while PROCESSING) | 4 ✅ |
| `POST /api/sources/[id]/ingest` | → `UploadResponse` (`PROCESSING`); PDF, PPTX and MP4 | 5 ✅ 6 ✅ 7 ✅ |
| `GET`/`HEAD /api/files/[id]` | → original bytes; Range → 206 / 416 | 4 ✅ |
| `POST /api/build` | `BuildRequest` (`courseId?`, `force?`) → `BuildResponse` | 9 ✅ |
| `GET /api/topics` | `?courseId` → `TopicView[]` | 9 ✅ |
| `POST /api/chat` | `ChatRequest` → `TutorAnswer` | 10 ✅ |
| `GET /api/chat?learnerId=` | → `ChatMessageView[]` | 10 ✅ |
| `POST /api/eval/answer` | `EvalAnswerRequest` → `EvalAnswerResponse` | 10 ✅ |
| `POST /api/quiz` | `CreateQuizRequest` → `Quiz` | 12 ✅ |
| `GET /api/quiz/[id]` | → `Quiz` | 12 ✅ |
| `POST /api/quiz/[id]/submit` | `SubmitRequest` → `QuizReport` | 13 ✅ |
| `GET /api/quiz/[id]/report` | → `QuizReport` | 13 ✅ |
| `GET /api/mastery?learnerId=` | → `MasteryView` | 14 ✅ |

Learner-scoped routes (chat, chat history, quiz create/get/submit/report, mastery) return **401 `unauthorized`** when the learner is a private account and the session cookie isn't theirs. Course routes (sources, files, build, topics, search, eval) are not tied to an account.

Full details, examples and errors: [API_SPEC.md](API_SPEC.md). Postman: `docs/postman/study-companion.postman_collection.json`.

`courseId` is optional everywhere and defaults to the single course (`demo-course`).

---

## 9. Evaluation

*Planned:* `tools/eval/ragas.py` + `score.ts` + `tools/sim` from the teammate. *Built* (see §0). Results: the `/evaluation` page (reports in `data/eval/`, validated against `RagReport` / `SimReport` in `@sc/schemas`; a report that fails validation is reported, never shown):

- **Test sets:** `data/demo/testset.stage1.json` (team-built, with known page/slide/timestamp locations and off-material questions) plus the holdout set from the overfitting audit (`tools/e2e/holdout/`).
- `npm run eval:rag` (`tools/e2e/src/eval-rag.ts`) calls `/api/eval/answer` and scores **faithfulness, answer relevancy, context precision, context recall** using the RAGAS definitions with a Gemini judge (cached), plus refusal precision.
- `npm run stage1:tutor` reports **citation accuracy** and **refusal rates** on real questions; it is where `REFUSAL_TAU` (0.60) was calibrated.
- `npm run eval:sim` (`tools/e2e/src/sim-students.ts`) uses no LLM: simulated students with hidden true mastery over several sessions, **adaptive vs random** selection, reporting mastery gain, estimation error and repetition rate.
  *Caveat (stated in the README): the simulated students use different parameters from the tutor's BKT model.*
- Latest numbers: [PROGRESS.md](PROGRESS.md) and the README.

---

## 10. Build order (module by module, each one verified before the next)

Live status: [PROGRESS.md](PROGRESS.md). Teammate tasks run in parallel: [TEAMMATE_BRIEF.md](TEAMMATE_BRIEF.md).

| # | Module | Done when |
|---|---|---|
| 0 | Skeleton: Next + Postgres + Prisma + `/api/health` | health returns `db.ok: true` |
| 1 | Gemini smoke: `packages/core/src/llm.ts` + `tools/smoke` (text JSON, 1-page PDF, PPTX slide images, video upload, embeddings) | all 5 checks pass with our model IDs |
| 2 | Prisma schema (all models) + migration + seed learner | migrate succeeds, rows visible |
| 3 | `@sc/schemas` contract + mocks | both teammates sign off |
| 4 | Upload + `/api/files/[id]` with Range + learners API | file uploads; video seeks in the browser |
| 4.5 | Gemini rate limiter + result cache + usage ledger | under free-tier limits; re-runs free |
| 5 | PDF ingestion (explicit ingest endpoint) | chunks with page + figure chunks in the DB |
| 6 | PPTX ingestion | one chunk per slide, in the correct order |
| 7 | Video ingestion | timestamped segments |
| 8 | Embeddings + retrieval | top-6 for a known question includes the right chunk |
| 9 | Course build (topics + prereqs), `/api/build`, `/api/topics` | 5–8 sensible topics, chunks tagged |
| 10 | Tutor API + guards + `/api/eval/answer` (citation panel UI: teammate) | cited answer; off-material question refused |
| 11 | BKT module (unit tested) | known sequences give expected values |
| 12 | Quiz generate + verify + no-repeat | verified questions, no seen stems |
| 13 | Submit, grading, report | report with misconceptions + citations |
| 14 | Mastery API (dashboard UI: teammate) | mastery bars, history chart |
| 15 | Eval + simulator (scripts: teammate; run + `REFUSAL_TAU` calibration: Parth) | metrics table in the README |
| 16 | README, demo video, Devpost | submitted |

---

## 11. Ownership

- **Parth:** `packages/core` (ingest, llm, retrieve, tutor, quiz, bkt), `packages/db` (Prisma schema), API routes in `apps/web/app/api`, `packages/client`, `tools/e2e`.
- **Teammate:** dataset, test set, `tools/eval`, `tools/sim`, UI pages in `apps/web/app` (through `@sc/client`, `auto` mode), README, demo script. See [TEAMMATE_BRIEF.md](TEAMMATE_BRIEF.md).
- **Shared:** `packages/schemas`.

## 12. Demo dataset (suggested)
OS **CPU scheduling**: one NPTEL/OCW lecture (15–20 min mp4), one textbook chapter (20–30 pages), one deck of about 20 slides (.pptx). It has numericals (waiting and turnaround time), Gantt-chart diagrams and clear prerequisites (process → scheduling criteria → FCFS/SJF/RR → priority/MLFQ).

## 13. Explicitly cut
Worker/queue, Docker image for the app, LibreOffice, YouTube input, pgvector, hybrid search/RRF, excerpt highlighting, mathjs, question-bank pre-warm, embedding dedup, mastery decay, prerequisite propagation, streaming, deployment. (*Auth* was on this list; simple accounts were added later at the user's request, §0.)
*P1, only after the full loop works:* React Flow course map, Hindi/Hinglish toggle.

## 14. Running locally

Prerequisites: Node ≥ 20.12 (we use 24), Docker Desktop running.

```bash
npm install                 # installs all workspaces; also generates the Prisma client
cp .env.example .env        # fill APP_GEMINI_API_KEY + model IDs
npm run db:up               # Postgres on localhost:5434
npm run dev                 # turbo → http://localhost:3000
curl localhost:3000/api/health
```

| Command (repo root) | What it does |
|---|---|
| `npm run dev` | Regenerates the Prisma client, then starts Next (turbo) |
| `npm run typecheck` | `tsc` across all 7 workspaces |
| `npm run lint` | ESLint on the web app |
| `npm run db:up` / `db:down` | Start or stop the Postgres container |
| `npm run db:generate` | Regenerate the Prisma client. **On Windows, stop the dev server first** (it locks the engine DLL). |
| `npm run db:status` | Prisma migration status, which also confirms the CLI can reach the DB |
| `npm run db:migrate -- --name <x>` | Create + apply a migration after editing the schema (stop dev first) |
| `npm run db:seed` | Idempotent seed: `demo-course`, `demo-learner` |
| `npm run db:verify` | 12 schema behaviour checks on a throwaway course |
| `npm run db:studio` | Prisma's table browser (alternative to Beekeeper) |
| `npm run test` | Unit + contract + DB tests: schemas, client, core (270 on 2026-10-14; core's service tests need Postgres and never call Gemini) |
| `npm run test:e2e` | End-to-end tests against the running app (35 on 2026-10-14; `npm run dev` or `start` first; `E2E_SKIP_HEAVY=1` skips the 2 GiB streaming test) |
| `npm run build` / `npm run start -w @sc/web` | Production build / server (stop dev first on Windows) |
| `npm run db:deploy` | Apply existing migrations without creating new ones (teammate setup) |
| `npm run smoke [-- json pdf slides video embed]` | Gemini smoke test (all checks, or the named ones); prints today's usage |
| `npm run stage1:pdf` | **Spends quota on first run:** ingest the real Stage-1 PDF and score it against the 47-fact ground truth; second run must cost 0 |
| `npm run models:list` | List models available to `APP_GEMINI_API_KEY` |
| `npm run stage1:<name>` (`pdf`, `video`, `pptx`, `retrieval`, `build`, `tutor`, `quiz`, `grading`, `mastery`) | Real-data checks against pre-registered ground truth (spend quota only when inputs or prompts change; cached otherwise) |
| `npm run eval:rag` / `npm run eval:sim` | RAGAS-definition metrics with a Gemini judge / simulated students (no LLM) |

**Windows:** stop the dev server before `npm install` or `db:generate` (Prisma's engine DLL is locked while it runs).

**DB GUI (Beekeeper, DBeaver, etc.):** PostgreSQL · host `localhost` · port `5434` · user `study` · password `study` · database `study`.
