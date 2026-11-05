// Shared plumbing for every API route (the _lib folder is private: not a route).
// Routes stay thin: parse input → call @sc/core → json(ResponseSchema, result).
import { connection, type NextRequest } from "next/server";
import { z } from "zod";
import { AppError, LlmError, SESSION_COOKIE, SESSION_TTL_S, readSession, sessionSecret } from "@sc/core";
import { Prisma } from "@sc/db";
import { API_ERROR_STATUS, type ApiErrorBody, type ApiErrorCode } from "@sc/schemas";

export function errorResponse(code: ApiErrorCode, message: string): Response {
  return Response.json({ error: { code, message } } satisfies ApiErrorBody, { status: API_ERROR_STATUS[code] });
}

/** Maps any thrown error to ApiErrorBody with the matching status. */
export function toErrorResponse(e: unknown): Response {
  if (e instanceof AppError) return errorResponse(e.code, e.message);
  if (e instanceof LlmError) {
    console.error(`[api] Gemini error (${e.kind}): ${e.message}`); // otherwise only "502" reaches the server log
    return errorResponse(e.kind === "rate_limit" ? "rate_limited" : e.kind === "config" ? "internal" : "llm_error", e.message);
  }
  if (e instanceof Prisma.PrismaClientKnownRequestError && e.code === "P2025") return errorResponse("not_found", "Record not found.");
  if (e instanceof Prisma.PrismaClientInitializationError) {
    return errorResponse("internal", "Database unavailable. Is Postgres running? (npm run db:up)");
  }
  console.error("[api] unhandled error", e);
  return errorResponse("internal", e instanceof Error ? e.message : String(e));
}

/** Wraps a handler: always runs at request time; any throw becomes ApiErrorBody. */
export function handle<C>(fn: (req: NextRequest, ctx: C) => Promise<Response>) {
  return async (req: NextRequest, ctx: C): Promise<Response> => {
    await connection();
    try {
      return await fn(req, ctx);
    } catch (e) {
      return toErrorResponse(e);
    }
  };
}

/** Responds with `data` after validating it against the endpoint's response schema (violations → 500). */
export function json<S extends z.ZodType>(schema: S, data: z.input<S>, status = 200): Response {
  const r = schema.safeParse(data);
  if (!r.success) {
    console.error("[api] response broke the contract", r.error);
    return errorResponse("internal", `Response broke the API contract:\n${z.prettifyError(r.error)}`);
  }
  return Response.json(r.data, { status });
}

/** The learner id of the request's valid session cookie, or null. */
export function sessionOf(req: NextRequest): string | null {
  return readSession(req.cookies.get(SESSION_COOKIE)?.value, sessionSecret());
}

/** The signed cookie value for forwarding to the trusted Python retrieval service. */
export function sessionTokenOf(req: NextRequest): string | null {
  return req.cookies.get(SESSION_COOKIE)?.value ?? null;
}

/** Sets (token) or clears (null) the session cookie on a response. HttpOnly; Secure when served over HTTPS. */
export function withSession(res: Response, req: NextRequest, token: string | null): Response {
  const secure = req.nextUrl.protocol === "https:" ? "; Secure" : "";
  const value = token ? `${token}; Max-Age=${SESSION_TTL_S}` : "; Max-Age=0";
  res.headers.append("Set-Cookie", `${SESSION_COOKIE}=${value}; Path=/; HttpOnly; SameSite=Lax${secure}`);
  return res;
}

/** Parses a JSON body with `schema`; 400 bad_request on invalid JSON or schema mismatch. */
export async function readBody<S extends z.ZodType>(req: Request, schema: S): Promise<z.infer<S>> {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    throw new AppError("bad_request", "Request body must be valid JSON.");
  }
  const r = schema.safeParse(body);
  if (!r.success) throw new AppError("bad_request", z.prettifyError(r.error));
  return r.data;
}

/** Parses the query string with `schema`; 400 bad_request on mismatch. */
export function readQuery<S extends z.ZodType>(req: NextRequest, schema: S): z.infer<S> {
  const r = schema.safeParse(Object.fromEntries(req.nextUrl.searchParams));
  if (!r.success) throw new AppError("bad_request", z.prettifyError(r.error));
  return r.data;
}
