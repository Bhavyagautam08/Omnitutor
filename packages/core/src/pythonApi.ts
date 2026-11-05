import { AppError } from "./errors";

type PythonCitation = {
  page: number | null;
  slide: number | null;
  timestamp_start: number | null;
  timestamp_end: number | null;
};

type PythonSearchResult = {
  chunk_id: string;
  source_id: string;
  source_type: "PDF" | "PPTX" | "VIDEO";
  filename: string;
  kind: "TEXT" | "FIGURE" | "SLIDE" | "TRANSCRIPT";
  title: string | null;
  content: string;
  topic_id: string | null;
  score: number;
  citation: PythonCitation;
};

type PythonSearchResponse = {
  candidates: number;
  results: PythonSearchResult[];
};

function apiUrl(): string {
  const value = process.env.PYTHON_API_URL?.trim() || "http://127.0.0.1:8012";
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new AppError("internal", "Python retrieval service URL is invalid.");
  }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash ||
    (url.protocol === "http:" && !loopback)
  ) {
    throw new AppError("internal", "Python retrieval service URL must be a loopback HTTP or HTTPS origin without embedded credentials.");
  }
  return url.origin;
}

async function request(
  path: string,
  method: "POST" | "DELETE",
  sessionToken: string,
  body?: object,
): Promise<Response> {
  if (!sessionToken || !/^[^.]+\.\d+\.[A-Za-z0-9_-]+$/.test(sessionToken)) {
    throw new AppError("unauthorized", "Sign in before using Python retrieval.");
  }
  try {
    return await fetch(`${apiUrl()}${path}`, {
      method,
      headers: {
        Cookie: `omnitutor_session=${sessionToken}`,
        ...(body && { "Content-Type": "application/json" }),
      },
      ...(body && { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(30_000),
    });
  } catch (error) {
    console.error("[python-api] retrieval service request failed", error);
    throw new AppError("internal", "Python retrieval service is unavailable.");
  }
}

async function ensureOk(response: Response): Promise<void> {
  if (response.ok) return;
  if (response.status === 401) throw new AppError("unauthorized", "Python retrieval rejected the current session.");
  if (response.status === 404) throw new AppError("not_found", "Notebook or source not found.");
  if (response.status === 422) throw new AppError("bad_request", "Python retrieval rejected invalid indexed data.");
  console.error(`[python-api] retrieval service returned HTTP ${response.status}`);
  throw new AppError("internal", "Python retrieval service could not complete the request.");
}

export async function searchWithPython(input: {
  notebookId: string;
  query: string;
  topK: number;
  sessionToken: string;
}): Promise<PythonSearchResponse> {
  const response = await request(
    `/api/v1/notebooks/${encodeURIComponent(input.notebookId)}/search`,
    "POST",
    input.sessionToken,
    { query: input.query, top_k: input.topK },
  );
  await ensureOk(response);
  const body: unknown = await response.json();
  if (
    typeof body !== "object" ||
    body === null ||
    !("candidates" in body) ||
    typeof body.candidates !== "number" ||
    !("results" in body) ||
    !Array.isArray(body.results)
  ) {
    throw new AppError("internal", "Python retrieval returned an invalid search response.");
  }
  return body as PythonSearchResponse;
}

export async function synchronizeSourceWithPython(input: {
  notebookId: string;
  sourceId: string;
  sessionToken: string;
}): Promise<number> {
  const response = await request(
    `/api/v1/notebooks/${encodeURIComponent(input.notebookId)}/sources/${encodeURIComponent(input.sourceId)}/vectors`,
    "POST",
    input.sessionToken,
  );
  await ensureOk(response);
  const body: unknown = await response.json();
  if (
    typeof body !== "object" ||
    body === null ||
    !("indexed" in body) ||
    typeof body.indexed !== "number" ||
    !Number.isInteger(body.indexed) ||
    body.indexed < 0
  ) {
    throw new AppError("internal", "Python retrieval returned an invalid source synchronization response.");
  }
  return body.indexed;
}

export async function deleteSourceFromPython(input: {
  notebookId: string;
  sourceId: string;
  sessionToken: string;
}): Promise<void> {
  const response = await request(
    `/api/v1/notebooks/${encodeURIComponent(input.notebookId)}/sources/${encodeURIComponent(input.sourceId)}/vectors`,
    "DELETE",
    input.sessionToken,
  );
  await ensureOk(response);
}
